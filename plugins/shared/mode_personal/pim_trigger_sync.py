"""pim 到点触发器同步层——事项变更 ↔ triggers_ext 的 trigger-control 服务面。

职责边界：

- **到点判定归触发器**：本模块只把事项的 ``due/at`` 注册成 per-item
  schedule/delay 触发器，不轮询、不出卡；
- **目标管道跟随会话**：订阅 ``session.created`` / ``session.active_changed`` /
  ``session.deleted``，只跟随主 agent（``agent.id=agentos``）管道；旧目标管道
  由 ``trigger.retarget`` 写 CANCELLED，不产生副本；
- **周期再武装**：订阅 ``trigger.fired``，repeat 事项推进到下一次并重注册；
  LLM 选择不打扰也不会让周期序列断掉；
- **幂等**：trigger_id 由 ``entity:id`` 哈希确定性生成，重启/重建重复注册
  覆盖同一 id；``last_reminded_for`` 标记同一次 occurrence 不重复注册。

错误是值/日志：服务面不可用时只记录并保留 dirty 项，等下一次会话事件重试，
不阻断事项存储主路径。
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pim_parse import parse_iso_dt
from pim_recurrence import next_occurrence
from pim_store import EVENT_ACTIVE_STATUSES, TODO_ACTIVE_STATUSES, PimStore

from agentos_plugin_sdk import get_settings

logger = logging.getLogger(__name__)

_PIM_SOURCE = "mode_personal"
_ENTITIES = ("todo", "event")


def trigger_id_for(entity: str, item_id: str) -> str:
    """确定性触发器 ID（同事项重建覆盖同一 id，不产生副本）。"""
    digest = hashlib.sha1(f"{entity}:{item_id}".encode()).hexdigest()[:12]
    return f"trigger_schedule_{digest}"


def _local_zone() -> Any:
    """APP_TIMEZONE 时区；配置不可用回退系统本地时区（最后 UTC）。"""
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001 — 时区是展示/比较辅助，缺失不阻断同步
        return datetime.now().astimezone().tzinfo or UTC


def _fact_message(entity: str, record: dict[str, Any], occurrence: str) -> str:
    """事实消息：只带事项坐标与到点时刻，不携带「请出卡」指令。"""
    field = "due" if entity == "todo" else "at"
    lines = [
        f"[日程到点] entity={entity} id={record.get('id', '')} "
        f"title={record.get('title', '')} {field}={occurrence}"
    ]
    repeat = str(record.get("repeat") or "")
    if repeat:
        lines.append(f"repeat={repeat}")
    lines.append("无需打扰（已处理/免问/不紧急）时可不通知。")
    return "\n".join(lines)


def _first_scheduled_slot(repeat: str, base: datetime) -> datetime | None:
    """repeat 首个 >= base 的默认发生槽（无结构化时刻的到点合成）。

    每日 = 当日 09:00（base 本身）；weekly:N 推进到首个目标周几；monthly:N
    推进到首个目标日（当月 N 日早于 base 则顺延下月）。非法 repeat = None。
    """
    if repeat == "daily":
        return base
    if repeat.startswith("weekly:"):
        try:
            weekday = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        if not 0 <= weekday <= 6:
            return None
        slot = base
        while slot.weekday() != weekday:
            slot += timedelta(days=1)
        return slot
    if repeat.startswith("monthly:"):
        try:
            day = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        if not 1 <= day <= 31:
            return None
        year, month = base.year, base.month
        for _ in range(1200):  # 百年量级上界（防呆；正常首个合法月即命中）
            if day <= calendar.monthrange(year, month)[1]:
                slot = datetime(year, month, day, base.hour, base.minute)
                if slot >= base:
                    return slot
            month += 1
            if month > 12:
                month, year = 1, year + 1
        return None
    return None


class PimTriggerSync:
    """事项变更 → 触发器注册/注销/迁移；触发完成 → repeat 再武装。"""

    def __init__(
        self,
        store: PimStore,
        capability_caller: Any,
        clock: Any | None = None,
        session_creator: Any | None = None,
        state_caller: Any | None = None,
    ) -> None:
        """session_creator：`session.create` 能力 caller（可选，兜底承载）。

        ADR 2026-11-03-pim-trigger-anchor：事项无来源会话且无活跃会话时，
        经内核 session.create 新建承载管道（触发器注入要求管道挂真实会话）。
        约定签名 ``async def creator(title: str) -> {"pipeline_ids": [...]}``；
        None = 旧内核能力缺席，保持 pending 降级（不抛）。

        state_caller：`pipeline-state` 标准能力 caller（可选，主 agent 管道
        核验用）。trigger-control 与 pipeline-state 是两个独立能力面：前者
        methods 白名单只有 register/unregister/list/retarget，state 查询必须
        走后者短名 `list`；None = 状态通道未注入，核验放行（不阻断同步）。
        """
        self._store = store
        self._call = capability_caller
        self._clock = clock or datetime.now
        self._session_creator = session_creator
        self._state_call = state_caller
        self._latest_pipeline_id = ""
        self._latest_session_id = ""
        self._dirty: set[tuple[str, str]] = set()
        self._pending: set[tuple[str, str]] = set()
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    # ── store 观察者入口（同步）──

    def on_change(
        self,
        entity: str,
        key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """store 变更观察者：入 dirty 并调度异步同步（不阻塞存储主路径）。"""
        if entity not in _ENTITIES:
            return
        self._dirty.add((entity, key))
        self._schedule_flush()

    async def stop(self) -> None:
        """停机：取消在途 flush（不注销已注册触发器）。"""
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

    # ── 域事件入口 ──

    async def on_domain_event(self, params: dict[str, Any]) -> None:
        """内核域事件：会话目标跟随 / 触发完成再武装。"""
        event = str(params.get("event") or "")
        if event in ("session.created", "session.active_changed"):
            pipeline_id = str(params.get("pipeline_id") or "")
            if not pipeline_id:
                return
            if not await self._is_main_pipeline(pipeline_id):
                return
            self._latest_pipeline_id = pipeline_id
            self._latest_session_id = str(params.get("session_id") or "")
            await self._reconcile_all()
            await self._flush_pending()
            return
        if event == "session.deleted":
            if str(params.get("session_id") or "") == self._latest_session_id:
                self._latest_pipeline_id = ""
                self._latest_session_id = ""
            return
        if event == "trigger.fired":
            await self._on_trigger_fired(params)

    async def _on_trigger_fired(self, params: dict[str, Any]) -> None:
        if str(params.get("trigger_source") or "") != _PIM_SOURCE:
            return
        entity = str(params.get("entity") or "")
        item_id = str(params.get("item_id") or "")
        if entity not in _ENTITIES or not item_id:
            return
        record = self._store.get(entity, item_id)
        if record is None:
            return
        occurrence = str(params.get("occurrence") or "") or self._occurrence(entity, record)
        if not occurrence:
            return
        self._store.mark_reminded(entity, item_id, occurrence)
        if entity != "todo":
            return
        repeat = str(record.get("repeat") or "")
        if not repeat:
            return
        if str(record.get("status") or "") not in TODO_ACTIVE_STATUSES:
            return
        due = str(record.get("due") or "")
        if due and due != occurrence:
            # due 已对齐其他坐标（人工改期等）→ 迟到 fired 不做序列推进
            return
        next_value = next_occurrence(repeat, occurrence, self._clock())
        if next_value is None:
            return
        self._store.update("todo", item_id, {"due": next_value})

    # ── 同步主路径 ──

    def _schedule_flush(self) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 同步调用面（测试/CLI）：等下一次事件或显式 flush
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush())

    async def _flush(self) -> None:
        async with self._lock:
            batch = list(self._dirty)
            self._dirty.clear()
            for entity, item_id in batch:
                try:
                    await self._sync_item(entity, item_id)
                except Exception:  # noqa: BLE001 — 单事项失败不影响其余事项
                    logger.exception(
                        "[pim_trigger_sync] 同步失败（保留待下次事件重试）| entity=%s id=%s",
                        entity,
                        item_id,
                    )
                    self._dirty.add((entity, item_id))

    async def _flush_pending(self) -> None:
        pending = list(self._pending)
        for entity, item_id in pending:
            try:
                await self._sync_item(entity, item_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[pim_trigger_sync] pending 同步失败 | entity=%s id=%s", entity, item_id
                )

    async def _reconcile_all(self) -> None:
        """会话目标变化后对账全部 pim 触发器（迁移/清理陈旧项）。"""
        try:
            res = await self._call("list", {})
        except Exception as exc:  # noqa: BLE001 — 服务面未就绪：等下次事件
            logger.warning(
                "[pim_trigger_sync] trigger-control.list 不可用，跳过对账 | err=%s", exc
            )
            return
        items = res.get("items") if isinstance(res, dict) else None
        if not isinstance(items, list):
            return
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("source") != _PIM_SOURCE:
                continue
            entity = str(metadata.get("entity") or "")
            item_id = str(metadata.get("item_id") or "")
            if entity not in _ENTITIES or not item_id or (entity, item_id) in seen:
                continue
            seen.add((entity, item_id))
            self._dirty.add((entity, item_id))
        if seen:
            await self._flush()

    async def _sync_item(self, entity: str, item_id: str) -> None:
        record = self._store.get(entity, item_id)
        trigger_id = trigger_id_for(entity, item_id)
        existing = await self._find_trigger(item_id)
        if record is not None and str(record.get("reminder_mode") or "direct") == "direct":
            # P0④分流：direct（含缺省）到点直接弹卡（今日页轮询面），不经 LLM
            # 事实消息管道 → 不注册触发器；此前注册过的（改档遗留）注销。
            if existing is not None and existing.get("status") in ("active", "pending"):
                await self._call("unregister", {"trigger_id": trigger_id})
            self._pending.discard((entity, item_id))
            return
        occurrence = self._occurrence(entity, record) if record is not None else ""
        if record is None or not occurrence or not self._is_active(entity, record):
            if existing is not None and existing.get("status") in ("active", "pending"):
                await self._call("unregister", {"trigger_id": trigger_id})
            self._pending.discard((entity, item_id))
            return
        if str(record.get("last_reminded_for") or "") == occurrence:
            self._pending.discard((entity, item_id))
            return
        if (
            existing is not None
            and existing.get("status") == "fired"
            and str((existing.get("metadata") or {}).get("occurrence") or "") == occurrence
        ):
            self._pending.discard((entity, item_id))
            return
        # 挂载序（ADR 2026-11-03-pim-trigger-anchor）：来源会话（创建事项的
        # 管道，钉住不随活跃面漂移）> 最近活跃主会话（存量无来源行兼容）>
        # session.create 兜底新建承载管道（记回 source_pipeline_id 幂等锚）。
        # 全不可得 → pending（creator 缺席降级，不抛）。
        pipeline_id = str(record.get("source_pipeline_id") or "") or self._latest_pipeline_id
        if not pipeline_id and self._session_creator is not None:
            try:
                created = await self._session_creator(
                    f"日程承载：{record.get('title', '')}"
                )
                pipes = (
                    created.get("pipeline_ids")
                    if isinstance(created, dict)
                    else None
                )
                new_pid = str((pipes or [""])[0]) if pipes else ""
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[pim_trigger_sync] session.create 兜底失败（pending 降级）"
                    "| entity=%s id=%s err=%s",
                    entity, item_id, exc,
                )
                new_pid = ""
            if new_pid:
                pipeline_id = new_pid
                try:
                    self._store.update(
                        entity, item_id, {"source_pipeline_id": new_pid}
                    )
                    record = self._store.get(entity, item_id) or record
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[pim_trigger_sync] 兜底管道记回失败（注册照常）"
                        "| entity=%s id=%s err=%s",
                        entity, item_id, exc,
                    )
                finally:
                    # 记回本身会触发 on_change 再入 dirty（store.update 观察者）
                    # ——清掉脏锚，防下一轮 flush 对同一事项重复 register。
                    self._dirty.discard((entity, item_id))
                    self._pending.discard((entity, item_id))
        if not pipeline_id:
            self._pending.add((entity, item_id))
            return
        if (
            existing is not None
            and existing.get("pipeline_id") != pipeline_id
            and existing.get("status") in ("active", "pending")
        ):
            moved = await self._call(
                "retarget",
                {"trigger_id": trigger_id, "pipeline_id": pipeline_id},
            )
            if not isinstance(moved, dict) or not moved.get("success"):
                logger.warning(
                    "[pim_trigger_sync] retarget 失败 | trigger=%s new=%s result=%r",
                    trigger_id,
                    pipeline_id,
                    moved,
                )
        payload = self._registration_payload(entity, record, occurrence, pipeline_id)
        result = await self._call("register", payload)
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError(f"trigger.register 失败: {result!r}")
        self._pending.discard((entity, item_id))
        logger.info(
            "[pim_trigger_sync] 触发器已同步 | entity=%s id=%s occurrence=%s pipeline=%s",
            entity,
            item_id,
            occurrence,
            pipeline_id,
        )

    async def _find_trigger(self, item_id: str) -> dict[str, Any] | None:
        try:
            res = await self._call("list", {"item_id": item_id})
        except Exception:  # noqa: BLE001 — 服务面未就绪按“不存在”处理
            return None
        items = res.get("items") if isinstance(res, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and str(item.get("trigger_id") or "").startswith(
                "trigger_schedule_"
            ):
                return item
        return None

    def _registration_payload(
        self,
        entity: str,
        record: dict[str, Any],
        occurrence: str,
        pipeline_id: str,
    ) -> dict[str, Any]:
        due = parse_iso_dt(occurrence)
        if due is None:
            raise ValueError(f"无法解析到点时刻: {occurrence!r}")
        zone = _local_zone()
        due_aware = due.replace(tzinfo=zone) if due.tzinfo is None else due
        now = self._clock()
        now_aware = now.replace(tzinfo=zone) if now.tzinfo is None else now
        base: dict[str, Any] = {
            "trigger_id": trigger_id_for(entity, str(record["id"])),
            "pipeline_id": pipeline_id,
            "message": _fact_message(entity, record, occurrence),
            "name": f"pim:{entity}:{record.get('id', '')}",
            "metadata": {
                "source": _PIM_SOURCE,
                "entity": entity,
                "item_id": str(record["id"]),
                "session_id": self._latest_session_id,
                "occurrence": occurrence,
                "repeat": str(record.get("repeat") or ""),
                "item_title": str(record.get("title") or ""),
            },
        }
        if due_aware <= now_aware:
            base["trigger_type"] = "delay"
            base["delay_seconds"] = 1
        else:
            base["trigger_type"] = "schedule"
            base["scheduled_at"] = due_aware.isoformat()
        return base

    def _occurrence(self, entity: str, record: dict[str, Any]) -> str:
        """到点坐标：due/at 透传；repeat 卡无结构化时刻 → 默认槽合成。

        合成规则（P0①，时间来源=卡片结构化时刻或合理默认）：当日 09:00 起
        首个匹配槽；未到点用该槽，已过取下一次发生（确定性随 clock 注入）。
        """
        field = "due" if entity == "todo" else "at"
        value = str(record.get(field) or "")
        if value:
            return value
        repeat = str(record.get("repeat") or "")
        if not repeat:
            return ""
        now = self._clock()
        base = now.replace(hour=9, minute=0, second=0, microsecond=0)
        slot = _first_scheduled_slot(repeat, base)
        if slot is None:
            return ""
        if slot > now:
            return slot.strftime("%Y-%m-%d %H:%M")
        nxt = next_occurrence(repeat, slot.strftime("%Y-%m-%d %H:%M"), now)
        return str(nxt or "")

    @staticmethod
    def _is_active(entity: str, record: dict[str, Any]) -> bool:
        if entity == "todo":
            return str(record.get("status") or "") in TODO_ACTIVE_STATUSES
        return str(record.get("status") or "") in EVENT_ACTIVE_STATUSES

    async def _is_main_pipeline(self, pipeline_id: str) -> bool:
        """按管道 state 的 agent.id 判定主 agent 管道（无 state 行的新管道放行）。

        state 查询走独立 pipeline-state 标准能力句柄（`trigger-control`
        provides 白名单只收 register/unregister/list/retarget 短名，state
        查询搭 trigger-control 通道会被 method 白名单拒绝）。
        """
        if self._state_call is None:
            return True  # 状态通道未注入（旧装配）：核验放行，不阻断同步
        try:
            rows = await self._state_call("list", {})
        except Exception as exc:  # noqa: BLE001 — 核验失败按可用性优先放行
            logger.warning(
                "[pim_trigger_sync] pipeline-state.list 不可用，跳过 agent 核验 | err=%s", exc
            )
            return True
        if not isinstance(rows, list):
            return True
        matched = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("pipeline_id") or "") != pipeline_id:
                continue
            matched = True
            if str(row.get("agent.id") or "") == "agentos":
                return True
        return not matched
