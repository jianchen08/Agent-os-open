"""渠道入站桥（channel_common 共享包）——把 IM 入站消息接进 AgentOS 会话管道。

单一事实源：四渠道（dingtalk/feishu/wecom/qq）共用的入站消费与回复回流实现，
各渠道插件目录不得再放同名 inbound_bridge.py（scripts/check_channel_copy_guard.py
守卫复制回潮）。路径注入契约与共享包其余模块一致：server.py 以 sys.path.append
引入，绝不 insert(0)。

链路（零内核改动，全部复用既有内核能力契约，ADR
2026-09-20-channel-inbound-session-mapping）::

    IM 平台 → 渠道 stream/回调 → input_adapter 队列（既有）
      → 本桥消费者：conversation_key → pipeline_id 映射（用户空间持久化）
      → chat.send_message capability（与前端 WS 派发同链路；create/inject 双分支）
      → 内核跑一轮 agent
      → run.completed / run.failed 域事件（manifest 须声明 domain_event hook）
      → pipeline-state.list 读 raw_result（STATE_BASELINE_KEYS 基线出口）
      → output adapter 投递回 IM

会话确定规则：conversation_key = 渠道平台会话标识（各渠道 _raw_to_state 派生，
见各 adapter），一一映射引擎 pipeline_id；thread 是 pipeline 经内核
pipeline_sessions 反查的派生物（chat 契约黑盒），插件不持有。映射落
<USER_ROOT>/data/channels/<plugin_id>.json（server.py 接线时解析 user_data_dir），
sidecar 重启后懒加载恢复；注入失败（管道被清理/协议错误）自动降级 create
重建并回写映射。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 消息注入的租户锚用户（合成用户，与 task_system/review_system 约定同构）。
SYSTEM_USER_ID = "channel_system"

#: 触发回复回流的域事件（其余 run.* 事件与本桥无关）。
_REPLY_EVENTS = ("run.completed", "run.failed")


class ConversationMappingStore:
    """conversation_key → 会话映射条目的 JSON 持久化。

    条目形如 ``{"pipeline_id": str, "reply_target": str, "reply_ctx": dict}``。
    ``path`` 为 None 时退化为纯内存映射（无用户空间可用的降级语义，重启丢失）。
    写入为整文件原子替换（先写临时文件再 os.replace 语义由调用方保证与否不影响
    正确性——本存储只在 sidecar 单进程内读写）。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("会话映射文件读取失败，按空映射启动: %s (%s)", self._path, exc)
            return
        if isinstance(data, dict):
            self._entries = data

    def get(self, key: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        entry = self._entries.get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self._ensure_loaded()
        self._entries[key] = dict(entry)
        self._flush()

    def find_by_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        """按 pipeline_id 反查映射条目（sidecar 重启后内存反向索引丢失的兜底）。"""
        self._ensure_loaded()
        for entry in self._entries.values():
            if entry.get("pipeline_id") == pipeline_id:
                return dict(entry)
        return None

    def all_items(self) -> list[tuple[str, dict[str, Any]]]:
        """全部 (conversation_key, entry)（thread 反查索引用）。"""
        self._ensure_loaded()
        return [(k, dict(v)) for k, v in self._entries.items()]

    def _flush(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("会话映射文件写入失败（内存映射仍有效）: %s (%s)", self._path, exc)


class ChannelInboundBridge:
    """渠道入站桥：消费 input_adapter 队列 → 注入会话管道 → 终态回复 IM。

    Args:
        channel_id: 渠道插件 id（如 "channel_qq"），映射文件命名与日志用。
        input_adapter: 队列缓冲型输入适配器（receive() 阻塞取下一条）。
        output_adapter: 缓冲型输出适配器（deliver_to() 投递回复）。
        get_capability: SDK 能力句柄解析器（plugin.get_capability，协程内懒解析）。
        store_path: 映射持久化路径；None = 纯内存降级。
    """

    def __init__(
        self,
        *,
        channel_id: str,
        input_adapter: Any,
        output_adapter: Any,
        get_capability: Callable[[str], Any],
        store_path: Path | None = None,
        interaction_poll_interval: float = 15.0,
    ) -> None:
        self._channel_id = channel_id
        self._input = input_adapter
        self._output = output_adapter
        self._get_capability = get_capability
        self._store = ConversationMappingStore(store_path)
        self._consumer_task: asyncio.Task[None] | None = None
        self._interaction_poll_task: asyncio.Task[None] | None = None
        self._interaction_poll_interval = interaction_poll_interval
        #: 已推送过 IM 的挂起交互 request_id（防重推；内存态即可——重复推送
        #: 的最坏情形只是多一条提醒，不值得持久化）。
        self._pushed_interactions: set[str] = set()

    async def start(self) -> None:
        """启动消费者协程（幂等；重复调用不叠加任务）。"""
        if self._consumer_task is not None and not self._consumer_task.done():
            return
        self._consumer_task = asyncio.create_task(self._consume(), name=f"{self._channel_id}-inbound")
        if self._interaction_poll_task is None or self._interaction_poll_task.done():
            self._interaction_poll_task = asyncio.create_task(
                self._interaction_poll_loop(), name=f"{self._channel_id}-interactions"
            )

    async def stop(self) -> None:
        """停止消费者协程（幂等；等待在途消息处理完毕）。"""
        for attr in ("_consumer_task", "_interaction_poll_task"):
            task: asyncio.Task[None] | None = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _consume(self) -> None:
        while True:
            state = await self._input.receive()
            try:
                await self._dispatch(state)
            except Exception:
                # 单条消息派发失败不得终止消费者（队列里还有后续消息）；
                # 异常已带上下文记日志，消息丢弃由调用方凭日志感知。
                logger.exception("[%s] 入站消息派发失败，本条丢弃", self._channel_id)

    async def _dispatch(self, state: dict[str, Any]) -> None:
        """把一条入站 state 投递到其会话对应的引擎管道。"""
        text = str(state.get("user_input", "") or "").strip()
        if not text:
            logger.warning("[%s] 入站消息无文本内容，跳过", self._channel_id)
            return
        sender = str(state.get("_channel_user_id", "") or "")
        conv_key = str(state.get("_conversation_key", "") or "") or f"u{sender}"
        entry = self._store.get(conv_key)
        # 交互优先拦截：会话有挂起交互时，本条消息是交互应答而非新输入
        # （应答成功即返回——管道续跑后的最终回复走 run 终态正常回流）。
        if entry is not None and entry.get("thread_id"):
            if await self._try_respond_interaction(entry, conv_key, text):
                return
        chat = self._get_capability("chat")
        params: dict[str, Any] = {
            "message": text,
            "user_id": SYSTEM_USER_ID,
            "background": True,
            "state": {
                "channel": {
                    "type": self._channel_id,
                    "conversation_key": conv_key,
                    "sender": sender,
                }
            },
        }
        pipeline_id: str | None = None
        if entry is not None and entry.get("pipeline_id"):
            params["pipeline_id"] = str(entry["pipeline_id"])
            try:
                await chat.call("send_message", params)
                pipeline_id = str(entry["pipeline_id"])
            except Exception as exc:
                # 注入失败（管道被清理/协议错误）→ 降级 create 重建会话。
                logger.warning(
                    "[%s] 会话 %s 注入管道 %s 失败，重建会话: %s",
                    self._channel_id, conv_key, entry["pipeline_id"], exc,
                )
                entry = None
                params.pop("pipeline_id")
        if pipeline_id is None:
            params["create"] = True
            resp = await chat.call("send_message", params)
            pipeline_id = str((resp or {}).get("pipeline_id", "") or "")
            if not pipeline_id:
                raise RuntimeError(
                    f"[{self._channel_id}] chat.send_message 创建分支未返回 pipeline_id: {resp!r}"
                )
            entry = {
                "pipeline_id": pipeline_id,
                "reply_target": str(state.get("_reply_target", "") or sender),
                "reply_ctx": dict(state.get("_reply_ctx", {}) or {}),
            }
            self._store.put(conv_key, entry)
        logger.info(
            "[%s] 会话 %s 消息已投递管道 %s", self._channel_id, conv_key, pipeline_id
        )
        # thread_id 懒解析（一次成功即止）：交互挂起记录按 thread 归属会话。
        if entry is not None and not entry.get("thread_id"):
            await self._resolve_thread_id(conv_key, entry, pipeline_id)

    # ── 交互推 IM + IM 回复作为交互结果 ──────────────────────────

    async def _resolve_thread_id(self, conv_key: str, entry: dict[str, Any], pipeline_id: str) -> None:
        """经 pipeline-state.list 读回管道归属 thread（基线出口键，一次即止）。"""
        try:
            rows = await self._get_capability("pipeline-state").call("list", {})
        except Exception as exc:
            logger.warning("[%s] thread 解析读取 state 失败: %s", self._channel_id, exc)
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if (
                isinstance(row, dict)
                and str(row.get("pipeline_id", "")) == pipeline_id
                and row.get("thread_id")
            ):
                entry["thread_id"] = str(row["thread_id"])
                self._store.put(conv_key, entry)
                return

    def _thread_index(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """thread_id → (conversation_key, entry) 反查表（含 thread 的映射条目）。"""
        index: dict[str, tuple[str, dict[str, Any]]] = {}
        for conv_key, entry in self._store.all_items():
            thread = str(entry.get("thread_id", "") or "")
            if thread:
                index[thread] = (conv_key, entry)
        return index

    async def _resolve_missing_threads(self) -> None:
        """批量补齐缺 thread_id 的映射条目（一次 list 服务所有会话）。"""
        missing = [
            (conv_key, entry)
            for conv_key, entry in self._store.all_items()
            if not entry.get("thread_id")
        ]
        if not missing:
            return
        try:
            rows = await self._get_capability("pipeline-state").call("list", {})
        except Exception as exc:
            logger.warning("[%s] thread 批量解析读取 state 失败: %s", self._channel_id, exc)
            return
        if not isinstance(rows, list):
            return
        by_pid = {
            str(row.get("pipeline_id", "")): str(row.get("thread_id", "") or "")
            for row in rows
            if isinstance(row, dict)
        }
        for conv_key, entry in missing:
            thread = by_pid.get(str(entry.get("pipeline_id", "")), "")
            if thread:
                entry["thread_id"] = thread
                self._store.put(conv_key, entry)

    async def _interaction_poll_loop(self) -> None:
        """周期把挂起交互推送到 IM（仅限本渠道映射内的会话）。"""
        while True:
            try:
                await self._push_pending_interactions()
            except Exception:
                # 轮询失败不终止循环（能力未就绪/网络抖动等，下轮再试）。
                logger.exception("[%s] 交互轮询推送失败", self._channel_id)
            await asyncio.sleep(self._interaction_poll_interval)

    async def _push_pending_interactions(self) -> None:
        await self._resolve_missing_threads()
        index = self._thread_index()
        if not index:
            return
        pending = await self._interaction_capability().call(
            "get_pending", {"limit": 100}, timeout=86500.0
        )
        records = pending.get("requests", []) if isinstance(pending, dict) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            md = record.get("message_data") or {}
            if str(md.get("interaction_mode", "")) == "notification":
                continue  # 非阻塞通知无需应答，不占交互环
            match = index.get(str(md.get("thread_id", "") or ""))
            if match is None:
                continue
            request_id = str(record.get("id", "") or "")
            if not request_id or request_id in self._pushed_interactions:
                continue
            conv_key, entry = match
            text = self._format_interaction(record)
            await self._output.deliver_to(
                str(entry.get("reply_target", "") or ""),
                text,
                dict(entry.get("reply_ctx", {}) or {}),
            )
            self._pushed_interactions.add(request_id)
            logger.info(
                "[%s] 交互 %s 已推送会话 %s", self._channel_id, request_id, conv_key
            )

    def _interaction_capability(self) -> Any:
        return self._get_capability("human-interaction")

    def _format_interaction(self, record: dict[str, Any]) -> str:
        md = record.get("message_data") or {}
        lines = [f"[需要你的输入] {md.get('title') or '请选择'}"]
        if md.get("description"):
            lines.append(str(md["description"]))
        options = md.get("options") or []
        if options:
            lines.append("可选项（直接回复你的决定或说明即可）：")
            for opt in options:
                label = (opt or {}).get("label") if isinstance(opt, dict) else str(opt)
                lines.append(f"- {label}")
        else:
            questions = md.get("questions") or []
            if questions:
                lines.append("请直接回复：" + " / ".join(str(q) for q in questions))
            else:
                lines.append("请直接回复文字。")
        return "\n".join(lines)

    async def _try_respond_interaction(self, entry: dict[str, Any], conv_key: str, text: str) -> bool:
        """把入站文本作为该会话挂起交互的用户自定义回复提交；成功返回 True。

        不做选项匹配——文本原样作为 feedback 交回（questions 型为 answers），
        由 agent 自行理解；应答成功后管道在 wait 处续跑，最终回复经 run
        终态正常回流。应答失败（已超时/不存在）返回 False，本条消息转
        普通输入派发。
        """
        thread = str(entry.get("thread_id", "") or "")
        pending = await self._interaction_capability().call(
            "get_pending", {"limit": 100}, timeout=86500.0
        )
        records = pending.get("requests", []) if isinstance(pending, dict) else []
        target: dict[str, Any] | None = None
        for record in records:
            if not isinstance(record, dict):
                continue
            md = record.get("message_data") or {}
            if (
                str(md.get("thread_id", "") or "") == thread
                and str(md.get("interaction_mode", "")) != "notification"
            ):
                target = record  # 取最后一条（最新挂起）
        if target is None:
            return False
        md = target.get("message_data") or {}
        if md.get("questions"):
            resp_data: dict[str, Any] = {"response_type": "answered", "answers": [text]}
        else:
            resp_data = {"response_type": "answered", "feedback": text}
        result = await self._interaction_capability().call(
            "respond",
            {"request_id": str(target.get("id", "")), "resp_data": {"response": resp_data}},
            timeout=86500.0,
        )
        ok = bool(result) and not (isinstance(result, dict) and result.get("error"))
        if ok:
            logger.info(
                "[%s] 会话 %s 交互 %s 已提交用户自定义回复",
                self._channel_id, conv_key, target.get("id"),
            )
        else:
            logger.warning(
                "[%s] 会话 %s 交互应答未成功（%r），转普通输入处理",
                self._channel_id, conv_key, result,
            )
        return ok

    async def handle_domain_event(self, params: dict[str, Any]) -> None:
        """SDK on_domain_event 处理器入口：run 终态 → 回复回流。

        事件载荷为平铺标签（内核 domain_event 契约：``event`` + 各标签
        ``pipeline_id``/``state`` 等置于顶层）。仅处理本渠道派发过的管道
        （映射反查不中即忽略——内核广播全租户事件）。
        """
        event = str(params.get("event", "") or "")
        if event not in _REPLY_EVENTS:
            return
        pipeline_id = str(params.get("pipeline_id", "") or "")
        if not pipeline_id:
            return
        entry = self._store.find_by_pipeline(pipeline_id)
        if entry is None:
            return
        if event == "run.failed":
            state_tag = params.get("state")
            err = ""
            if isinstance(state_tag, dict):
                err = str(state_tag.get("raw_error", "") or "")
            text = f"处理失败：{err or '未知错误'}"
        else:
            text = await self._fetch_reply(pipeline_id)
        if not text:
            logger.warning("[%s] 管道 %s 终态无回复文本，跳过投递", self._channel_id, pipeline_id)
            return
        await self._output.deliver_to(
            str(entry.get("reply_target", "") or ""),
            text,
            dict(entry.get("reply_ctx", {}) or {}),
        )

    async def _fetch_reply(self, pipeline_id: str) -> str:
        """经 pipeline-state.list 读回指定管道的 raw_result（终态回复文本）。"""
        rows = await self._get_capability("pipeline-state").call("list", {})
        if not isinstance(rows, list):
            return ""
        for row in rows:
            if isinstance(row, dict) and str(row.get("pipeline_id", "")) == pipeline_id:
                return str(row.get("raw_result", "") or "")
        return ""
