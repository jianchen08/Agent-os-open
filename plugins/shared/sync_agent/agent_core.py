"""sync_agent 端侧引擎 —— 变更捕获、HLC-LWW 下行合并、outbox 上行、心跳、自举。

方向拆解（ADR 2026-10-15-multi-device-sync-hlc-lww D3/D6）：
- **上行**：本地可同步层变更捕获（config yaml 面 = ConfigWatcher 快照对比；
  DB 逻辑记录面 = 管道步骤 capture_step + 域事件 capture_session，R2 设计
  定稿；文件类 git 面 = 属主臂 capture_file_scopes，变更=commit/删除=墓碑，
  T2 拍板③b）→ 打 HLC → outbox 追加（断网不丢）→ 批量 POST relay（失败指数
  退避，退避期间不推进消费）；本端捕获同刀前移资产裁决状态（他端更旧事件
  到达即 stale，本地后改不被覆盖）。
- **下行**：按本地 cursor 增量拉取 → 逐事件 observe（保因果）→ 按资产类型
  分发合并——config 走 HLC-LWW 落盘（后改赢；无 HLC 不合并——裸墙钟 LWW 已
  被否）；session/message/task 走**只读镜像**落地（他端 DB 逻辑记录，独立
  命名空间不碰本端内核写面，属主唯一写者无真冲突，HLC 门槛只防乱序重放）；
  文件类 git 面 = 读者臂 pull_file_scopes（transport 供 bundle → fetch 恒
  ff → 镜像物化，零 merge/rebase）；非冲突改动各自落地。
- **自举**：端侧状态全新 → 注册设备 → 拉入网基线（层级权威①，relay 裁定）
  → 全量应用 → 游标定位账本头 → 转增量。
- **心跳**：周期上报档位/版本（在链存在性数据源，D1）。

relay 不可达不阻塞本机任何执行（离线第一）：捕获是同步快路径，推送/拉取
失败只退避不上抛。时序全部经 sleep_fn/clock 注入（测试零真实等待）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from _root_api import (
    ASSET_CHANNELS,
    HlcTimestamp,
    HybridLogicalClock,
    OutboxStore,
    SyncEvent,
    asset_channel,
    new_ulid,
    now_iso_utc,
    validate_event,
)
from capture import (
    fingerprint_message,
    message_record,
    session_record,
    task_record,
    task_snapshot,
    task_snapshot_fingerprint,
)
from exec_channel import ExecChannelConfig, ExecTokenChannel, build_capability
from file_channel import FileChannelConfig, FileTransport, GitChannelError, GitFileChannel
from registry_replica import RegistryReplica

logger = logging.getLogger(__name__)

#: 可同步层资产类型 = 通道注册表全集（路由中间件单点，ADR 2026-10-17 D2）。
SUPPORTED_ASSET_TYPES = frozenset(ASSET_CHANNELS)

#: 退避参数（秒）：1 → 2 → 4 → … 封顶 300；成功归零（trace_exporter 同律）。
_BACKOFF_BASE = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX = 300.0

#: 本机内核监听端口（与 kernel main.rs 同名同缺省）：自报地址的端口来源。
_KERNEL_PORT_ENV = "AGENTOS_KERNEL_PORT"
_KERNEL_PORT_DEFAULT = "9100"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ConfigWatcher:
    """config yaml 面变更捕获（快照对比轮询；平台安全，无内核 watcher 依赖）。"""

    def __init__(self, watch_root: Path, pattern: str = "**/*.yaml") -> None:
        self._root = Path(watch_root)
        self._pattern = pattern

    @property
    def root(self) -> Path:
        return self._root

    def snapshot(self) -> dict[str, str]:
        """当前 {相对路径: sha256}（watch_root 不存在 = 空快照）。"""
        if not self._root.is_dir():
            return {}
        result: dict[str, str] = {}
        for path in sorted(self._root.glob(self._pattern)):
            if path.is_file():
                rel = path.relative_to(self._root).as_posix()
                result[rel] = sha256_hex(path.read_bytes())
        return result

    def read(self, rel_path: str) -> bytes | None:
        path = self._root / rel_path
        try:
            return path.read_bytes()
        except OSError:
            return None

    def write(self, rel_path: str, content: bytes) -> None:
        path = self._root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@dataclass
class AgentConfig:
    """引擎装配参数（server.py 从 settings/env 解析）。"""

    relay_url: str
    token: str
    device_id: str
    device_tier: str
    app_version: str
    watch_root: Path
    pattern: str = "**/*.yaml"
    poll_interval_secs: float = 5.0
    heartbeat_interval_secs: float = 30.0
    batch_max_events: int = 100
    file_channel: FileChannelConfig | None = None
    exec_channel: ExecChannelConfig | None = None
    exec_verification_path: Path | None = None
    registry_replica_path: Path | None = None
    """全网注册表副本落盘路径（A 档对等信任；None = 副本面关，借权核验全走
    relay 回退）。"""
    """本端数据根（用户数据目录）：储存状态上报的取样点。"""
    data_root: Path | None = None


@dataclass
class EngineStatus:
    """运行观测面（get_status 工具消费）。"""

    bootstrap_done: bool = False
    captured_total: int = 0
    applied_total: int = 0
    stale_total: int = 0
    flushed_total: int = 0
    failed_flushes: int = 0
    pulled_total: int = 0
    heartbeats_total: int = 0
    mirror_total: int = 0
    quarantined_total: int = 0
    last_error: str = ""
    cursor: int = 0
    pending: int = 0


class RelayStatusError(RuntimeError):
    """relay 返回非 2xx（带状态码：413 等确定性拒绝与网络失败分道处置）。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"relay {status}: {body[:200]}")
        self.status = status


class SyncAgentEngine:
    """端侧同步引擎（capture/flush/pull/heartbeat 单拍循环 + 自举）。"""

    def __init__(
        self,
        config: AgentConfig,
        *,
        state: Any,
        outbox: OutboxStore,
        post_json: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
        get_json: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        clock_ms: Callable[[], int] | None = None,
        clock_mono: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], str] = now_iso_utc,
        file_transport: FileTransport | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._outbox = outbox
        self._post_json: Callable[[str, str, dict[str, Any]], Awaitable[None]] = (
            post_json if post_json is not None else _aiohttp_post_json
        )
        self._get_json: Callable[[str, str], Awaitable[dict[str, Any]]] = (
            get_json if get_json is not None else _aiohttp_get_json
        )
        self._sleep = sleep_fn
        self._clock_mono = clock_mono
        self._now = now_fn
        self._watcher = ConfigWatcher(config.watch_root, config.pattern)
        self._file_transport = file_transport
        self._file_channel: GitFileChannel | None = None
        if config.file_channel is not None:
            channel = GitFileChannel(config.file_channel)
            if channel.probe():
                self._file_channel = channel
            else:
                logger.warning(
                    "[sync_agent] file channel disabled (git executable missing) — "
                    "config/db faces unaffected"
                )
        # exec 通道面（2c D8/D13'）：能力子键上报 + 属主端验证集周期拉取。
        # 未启用（缺 exec_channel 配置或缺持久化路径）= 双面全关（注册表零噪音）。
        self._exec_tokens: ExecTokenChannel | None = None
        if config.exec_channel is not None and config.exec_channel.enabled and (
            config.exec_verification_path is not None
        ):
            self._exec_tokens = ExecTokenChannel(config.exec_verification_path)
        # 注册表副本面（A 档对等信任）：周期拉取 /registry 持久复制；未配置路径
        # = 面关（借权核验全走 relay /verify 回退，与旧版同语义）。
        self._registry: RegistryReplica | None = (
            RegistryReplica(config.registry_replica_path)
            if config.registry_replica_path is not None
            else None
        )

        restored = state.load_clock()
        initial = restored[1] if restored and restored[0] == config.device_id else None
        self._clock = HybridLogicalClock(
            config.device_id, clock_ms=clock_ms, initial=initial
        )
        self._baseline: dict[str, str] = self._watcher.snapshot()
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False
        # 退避面（flush/pull 各自独立：拉取不受上行故障连坐；file 通道独立退避）。
        self._flush_failures = 0
        self._pull_failures = 0
        self._next_flush_at = 0.0
        self._next_pull_at = 0.0
        self._file_pull_failures = 0
        self._next_file_pull_at = 0.0
        self._exec_pull_failures = 0
        self._next_exec_pull_at = 0.0
        self._registry_pull_failures = 0
        self._next_registry_pull_at = 0.0
        self._next_heartbeat_at = 0.0
        self.status = EngineStatus(cursor=state.load_cursor() or 0)

    # ── 生命周期 ─────────────────────────────────────────────

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def file_channel(self) -> GitFileChannel | None:
        """git 文件通道原语（None = 停用/未配置；transport 供给方消费）。"""
        return self._file_channel

    @property
    def exec_token_channel(self) -> ExecTokenChannel | None:
        """属主端执行凭据验证面（None = exec 通道未启用；本端 exec server 消费）。"""
        return self._exec_tokens

    @property
    def registry_replica(self) -> RegistryReplica | None:
        """全网注册表副本（None = 未配置路径；借权本地核验与设备页副本回退消费）。"""
        return self._registry

    def _exec_capability(self) -> dict[str, Any]:
        """exec_channel 能力子键（未启用 = 空 dict，调用方据此省略键）。"""
        if self._config.exec_channel is None:
            return {}
        return build_capability(self._config.exec_channel)

    @property
    def pending_count(self) -> int:
        return len(self._outbox)

    def load_pending(self) -> int:
        """启动恢复 outbox（断网/重启续传）。"""
        return self._outbox.load()

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 —— 循环体故障不拖死端侧同步
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("[sync_agent] tick 异常")
            await self._sleep(self._config.poll_interval_secs)

    # ── 自举（入网初始化）────────────────────────────────────

    def needs_bootstrap(self) -> bool:
        """端侧状态全新（无游标且无资产）→ 需要入网自举。"""
        return self._state.load_cursor() is None and self._state.asset_count() == 0

    async def bootstrap(self) -> None:
        """注册 → 拉入网基线（层级权威①，relay 裁定）→ 应用 → 游标定位账本头。"""
        await self._post_json(
            f"{self._config.relay_url}/devices",
            self._config.token,
            {
                "device_id": self._config.device_id,
                "tier": self._config.device_tier,
                "app_version": self._config.app_version,
                "capabilities": self._capabilities_payload(),
                "address": self._self_address(),
                "storage": self._storage_report(),
            },
        )
        payload = await self._get_json(f"{self._config.relay_url}/bootstrap", self._config.token)
        applied = 0
        for wire in payload.get("events", []):
            if self._ingest_wire(wire):
                applied += 1
        cursor = int(payload.get("cursor", 0) or 0)
        self._state.save_cursor(cursor)
        self.status.cursor = cursor
        self.status.bootstrap_done = True
        self._baseline = self._watcher.snapshot()
        logger.info(
            "[sync_agent] 入网自举完成: device=%s baseline_applied=%s cursor=%s",
            self._config.device_id, applied, cursor,
        )

    # ── 单拍（capture → flush → pull → heartbeat）───────────

    async def tick(self) -> None:
        if self.needs_bootstrap():
            try:
                await self.bootstrap()
            except Exception as exc:  # noqa: BLE001 —— relay 未可达，下拍重试
                self.status.last_error = f"bootstrap: {type(exc).__name__}: {exc}"
                logger.warning("[sync_agent] 入网自举未完成（relay 未就绪？），下拍重试: %s", exc)
                return
        self.capture_local_changes()
        self.capture_file_scopes()
        await self._flush_when_due()
        await self._pull_when_due()
        await self.pull_file_scopes()
        await self._heartbeat_when_due()
        await self._pull_registry_when_due()
        await self._pull_exec_tokens_when_due()

    def _capabilities_payload(self) -> dict[str, Any]:
        """注册/心跳共用的 capabilities 载荷（asset_types + 可选 exec_channel 子键）。

        exec 通道未启用 = 子键整体缺席（D8：注册表零噪音，非空 dict 占位）。
        """
        capabilities: dict[str, Any] = {"asset_types": sorted(SUPPORTED_ASSET_TYPES)}
        exec_capability = self._exec_capability()
        if exec_capability:
            capabilities["exec_channel"] = exec_capability
        return capabilities

    def capture_local_changes(self) -> int:
        """本地可同步层变更 → 打 HLC → outbox + 资产状态前移（同步快路径）。"""
        current = self._watcher.snapshot()
        changed = {
            rel: digest
            for rel, digest in current.items()
            if self._baseline.get(rel) != digest
        }
        for rel in sorted(changed):
            content = self._watcher.read(rel)
            if content is None:
                continue
            stamp = self._clock.now()
            self._state.save_clock(stamp)
            key = f"config/{rel}"
            previous = self._state.get_asset(key)
            event = SyncEvent(
                ulid=new_ulid(),
                device_id=self._config.device_id,
                asset_type="config",
                asset_id=rel,
                version=(previous["version"] + 1) if previous else 1,
                updated_at=self._now(),
                payload={"content": content.decode("utf-8")},
                hlc=stamp,
            )
            dropped = self._outbox.append(event.to_wire())
            if dropped:
                logger.warning("[sync_agent] outbox 超限丢弃 %s 条（容量语义）", dropped)
            self._state.put_asset(
                asset_key=key,
                hlc=stamp,
                version=event.version,
                content_hash=changed[rel],
                ulid=event.ulid,
            )
            self._baseline[rel] = changed[rel]
            self.status.captured_total += 1
        return len(changed)

    # ── 文件通道 git 仓库化（T2，拍板③b：file 通道 git 形态）──

    def capture_file_scopes(self) -> int:
        """属主臂：git scope 变更捕获（变更=commit，删除=墓碑即提交）。

        零变更零 commit（git status 内置检测）；通道故障只记 last_error
        不阻塞本拍其余面（离线第一同律）。
        """
        if self._file_channel is None:
            return 0
        captured = 0
        for scope in sorted(self._file_channel.owner_scopes):
            try:
                if self._file_channel.capture_commit(scope) is not None:
                    captured += 1
            except GitChannelError as exc:
                self.status.last_error = f"file_channel/{scope}: {exc}"
                logger.warning("[sync_agent] file scope %s 捕获失败: %s", scope, exc)
        return captured

    async def pull_file_scopes(self) -> None:
        """读者臂：transport 拉增量 bundle → fetch（恒 ff）→ 镜像物化。

        transport 未接（relay bundle 传输 = 下一刀）→ 诚实空转；失败独立
        退避，不与 config 拉取连坐。读者路径无 commit 入口（属主写他端只读）。
        """
        if self._file_channel is None or self._file_transport is None:
            return
        if self._clock_mono() < self._next_file_pull_at:
            return
        for scope in sorted(self._file_channel.reader_scopes):
            try:
                bundle = await self._file_transport(scope, self._file_channel.head(scope))
                if bundle is not None:
                    self._file_channel.apply_bundle(scope, bundle)
            except Exception as exc:  # noqa: BLE001 —— 网络面/git 面故障统一退避
                self._file_pull_failures += 1
                self.status.last_error = f"file_pull/{scope}: {exc}"
                self._next_file_pull_at = self._clock_mono() + self._backoff_delay(
                    self._file_pull_failures
                )
                logger.warning(
                    "[sync_agent] file scope %s 拉取失败（第 %s 次）: %s",
                    scope,
                    self._file_pull_failures,
                    exc,
                )
                return
        self._file_pull_failures = 0

    # ── DB 逻辑记录捕获（T1，两通道架构 DB 通道）──────────────

    def _enqueue_record(
        self,
        *,
        asset_type: str,
        asset_id: str,
        payload: dict[str, Any],
        content_hash: str = "",
    ) -> bool:
        """DB 通道捕获引擎（唯一上行口）：打 HLC → outbox + 资产状态前移。

        通道代码一份（ADR 2026-10-17 D3）：注册项差异只进 capture.py 的载荷
        构造；断网期事件留 outbox（与 config 面同一投递面）。
        """
        stamp = self._clock.now()
        self._state.save_clock(stamp)
        key = f"{asset_type}/{asset_id}"
        previous = self._state.get_asset(key)
        event = SyncEvent(
            ulid=new_ulid(),
            device_id=self._config.device_id,
            asset_type=asset_type,
            asset_id=asset_id,
            version=(previous["version"] + 1) if previous else 1,
            updated_at=self._now(),
            payload=payload,
            hlc=stamp,
        )
        dropped = self._outbox.append(event.to_wire())
        if dropped:
            logger.warning("[sync_agent] outbox 超限丢弃 %s 条（容量语义）", dropped)
        self._state.put_asset(
            asset_key=key,
            hlc=stamp,
            version=event.version,
            content_hash=content_hash,
            ulid=event.ulid,
        )
        self.status.captured_total += 1
        return True

    def capture_session(self, session_id: str, *, deleted: bool = False) -> int:
        """域事件路径捕获（session.created/deleted → 会话注册项）。

        登记幂等（已有资产且非墓碑 → 零事件）；墓碑 = 属主追加（版本递增，
        不受幂等短路）。
        """
        if not session_id:
            return 0
        if not deleted and self._state.get_asset(f"session/{session_id}") is not None:
            return 0
        asset_id, payload = session_record(session_id, tombstone=deleted, origin="domain_event")
        return 1 if self._enqueue_record(asset_type="session", asset_id=asset_id, payload=payload) else 0

    def capture_step(self, state: Any) -> dict[str, Any]:
        """管道步骤捕获（autonomous.yaml post 组接线；引擎投喂 working state）。

        捕获失败不阻塞管道（离线第一同律）：零抛出零 state_updates，畸形
        state 零事件。捕获序：会话骨架 → 任务漂移 → 消息 diff（热路径
        长度+尾指纹未变即零扫描）。
        """
        try:
            summary = self._capture_step_inner(state)
            if summary is not None:
                logger.info(
                    "[sync_agent] 捕获步: pipeline=%s session=%s messages=%s "
                    "新增消息=%s 任务漂移=%s 会话登记=%s",
                    *summary,
                )
        except Exception as exc:  # noqa: BLE001 —— 捕获是旁路面，异常不进执行路径
            logger.exception("[sync_agent] 捕获步异常（零阻塞管道）")
            self.status.last_error = f"capture_step: {type(exc).__name__}: {exc}"
        return {}

    def _capture_step_inner(self, state: Any) -> tuple[str, str, int, int, bool, bool] | None:
        if not isinstance(state, dict):
            return None
        pipeline_id = str(state.get("pipeline_id") or "")
        if not pipeline_id:
            return None
        captured_messages = 0
        task_drift = False
        # 会话骨架：首见 session_id 登记（幂等）。
        session_id = str(state.get("session_id") or "")
        session_registered = bool(session_id) and self.capture_session(session_id) > 0
        # 任务漂移：task.* 快照指纹变 = 任务态变迁（属主终态权威）。
        task_snap = task_snapshot(state)
        if task_snap.get("task.status") is not None:
            snap_fp = task_snapshot_fingerprint(task_snap)
            previous = self._state.get_asset(f"task/{pipeline_id}")
            if previous is None or previous["content_hash"] != snap_fp:
                asset_id, payload = task_record(pipeline_id, task_snap)
                self._enqueue_record(
                    asset_type="task", asset_id=asset_id, payload=payload, content_hash=snap_fp
                )
                task_drift = True
        # 消息 diff：新指纹一对象一事件（压缩/重生成 = 新指纹新对象）。
        messages = state.get("messages")
        if isinstance(messages, list):
            count = len(messages)
            last_fp = fingerprint_message(messages[-1]) if count else ""
            if self._state.capture_hot(pipeline_id) != (count, last_fp):
                seen = self._state.seen_fingerprints(pipeline_id)
                fresh: list[tuple[dict[str, Any], str]] = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    fp = fingerprint_message(msg)
                    if fp not in seen:
                        fresh.append((msg, fp))
                        seen.add(fp)
                for msg, fp in fresh:
                    raw_seq = msg.get("seq")
                    seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
                    asset_id, payload = message_record(pipeline_id, seq, msg, fp)
                    self._enqueue_record(
                        asset_type="message", asset_id=asset_id, payload=payload, content_hash=fp
                    )
                self._state.mark_seen_fingerprints(pipeline_id, {fp for _, fp in fresh})
                if count:
                    self._state.save_capture_hot(pipeline_id, count, last_fp)
                captured_messages = len(fresh)
        return (
            pipeline_id,
            session_id,
            len(messages) if isinstance(messages, list) else -1,
            captured_messages,
            task_drift,
            session_registered,
        )

    async def _flush_when_due(self) -> None:
        if not len(self._outbox) or self._clock_mono() < self._next_flush_at:
            return
        batch = self._outbox.peek(self._config.batch_max_events)
        try:
            quarantined = await self._post_batch_in_order(batch)
        except Exception as exc:  # noqa: BLE001 —— 网络失败统一退避，事件不丢
            self._flush_failures += 1
            self.status.failed_flushes += 1
            self.status.last_error = f"flush: {type(exc).__name__}: {exc}"
            self._next_flush_at = self._clock_mono() + self._backoff_delay(self._flush_failures)
            logger.warning(
                "[sync_agent] 上行失败（第 %s 次），退避 %.1fs: %s",
                self._flush_failures, self._backoff_delay(self._flush_failures), exc,
            )
            return
        self._flush_failures = 0
        self.status.last_error = f"quarantine: {quarantined} 事件超限隔离" if quarantined else ""

    async def _post_batch_in_order(self, batch: list[dict[str, Any]]) -> int:
        """按序投递一批事件，返回本批隔离数。

        413 对半拆批重试（relay 体限拒绝的是请求体不是业务，拆小即过）；
        降到单事件仍 413 = 事件本体超限，隔离留痕后放行队列——毒事件滞留
        队首 = 全通道永久堵死（2026-10-25 生产实证：relay 体限 2MB 撞巨型
        消息记录，连续 21+ 次失败退避 300s）。前缀游标语义（OutboxStore.commit
        只收前缀）约束：先投前半、成功即 commit，再投后半——乱序 commit 会把
        未投事件标成已投。非 413 异常原样上抛（退避语义归 _flush_when_due）。
        """
        try:
            await self._post_json(
                f"{self._config.relay_url}/events",
                self._config.token,
                {"events": batch},
            )
        except RelayStatusError as exc:
            if exc.status != 413:
                raise
            if len(batch) == 1:
                self._quarantine(batch[0], exc)
                return 1
            mid = len(batch) // 2
            logger.warning("[sync_agent] 批量 %d 条超限（413），对半拆批重试", len(batch))
            quarantined = await self._post_batch_in_order(batch[:mid])
            quarantined += await self._post_batch_in_order(batch[mid:])
            return quarantined
        self._outbox.commit(len(batch))
        self.status.flushed_total += len(batch)
        return 0

    def _quarantine(self, event: dict[str, Any], exc: Exception) -> None:
        """单事件确定性拒收（413）：死信留痕 + 计数，前缀 commit 放行队列。

        事件本体落 `<outbox>.dead.jsonl`（一行一事件 JSON）供事后取证重放；
        隔离 ≠ 静默丢弃（留痕告警 + 状态计数双证）。
        """
        dead_path = self._outbox.path.with_name(self._outbox.path.name + ".dead.jsonl")
        dead_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dead_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._outbox.commit(1)
        self.status.quarantined_total += 1
        logger.warning(
            "[sync_agent] 单事件被 relay 确定性拒收（%s），隔离落死信 %s "
            "| asset_type=%s asset_id=%s",
            exc, dead_path.name, event.get("asset_type"), event.get("asset_id"),
        )

    async def _pull_when_due(self) -> None:
        if self._clock_mono() < self._next_pull_at:
            return
        watermark = self._state.load_cursor()
        if watermark is None:
            return
        try:
            payload = await self._get_json(
                f"{self._config.relay_url}/events?since_cursor={watermark}", self._config.token
            )
        except Exception as exc:  # noqa: BLE001 —— relay 不可达退避，游标不动
            self._pull_failures += 1
            self.status.last_error = f"pull: {type(exc).__name__}: {exc}"
            self._next_pull_at = self._clock_mono() + self._backoff_delay(self._pull_failures)
            return
        self._pull_failures = 0
        highest = watermark
        for wire in payload.get("events", []):
            self._ingest_wire(wire)
            highest = max(highest, int(wire.get("cursor", 0) or 0))
        if highest != watermark:
            self._state.save_cursor(highest)
            self.status.cursor = highest

    def _ingest_wire(self, wire: dict[str, Any]) -> bool:
        """下行单事件：observe（保因果）→ HLC-LWW 合并。返回是否落地写盘。"""
        try:
            event = validate_event(wire)
        except ValueError as exc:
            logger.warning("[sync_agent] 下行事件 schema 违约丢弃: %s", exc)
            return False
        if event.device_id == self._config.device_id:
            self.status.pulled_total += 1
            return False
        if self._state.applied_outcome(event.ulid) is not None:
            return False
        if event.hlc is not None:
            self._clock.observe(event.hlc)
            self._state.save_clock(self._clock_state_stamp())
        outcome = self._merge(event)
        self._state.mark_applied(ulid=event.ulid, outcome=outcome, applied_at=self._now())
        self.status.pulled_total += 1
        if outcome == "applied":
            self.status.applied_total += 1
            return True
        if outcome == "stale":
            self.status.stale_total += 1
        return False

    def _clock_state_stamp(self) -> HlcTimestamp:
        physical, logical = self._clock.state
        return HlcTimestamp(physical_ms=physical, logical=logical, device_id=self._config.device_id)

    def _merge(self, event: SyncEvent) -> str:
        """按通道路由合并（路由中间件端侧臂，ADR 2026-10-17 D2/D3）。

        file 通道 = HLC-LWW 落盘（后改赢；无 HLC 不合并——裸墙钟 LWW 已被否）；
        db 通道 = 只读镜像通用落地（属主唯一写者无真冲突，HLC 门槛只防乱序
        重放）。落败旧值留 relay 账本可查，本地只记 stale。
        """
        if event.asset_type not in SUPPORTED_ASSET_TYPES:
            return "unsupported_type"
        local = self._state.get_asset(event.asset_key)
        if local is not None and event.hlc is not None and event.hlc <= local["hlc"]:
            return "stale"
        if asset_channel(event.asset_type) == "db":
            return self._merge_db_record(event)
        return self._merge_file(event)

    def _merge_file(self, event: SyncEvent) -> str:
        """file 通道合并（config yaml 面既有路径，泛化挂点）。"""
        content = event.payload.get("content")
        if not isinstance(content, str):
            logger.warning(
                "[sync_agent] 事件 %s payload 无 content，不落盘（file 通道契约）",
                event.ulid,
            )
            return "no_content"
        data = content.encode("utf-8")
        rel = event.asset_id
        if not rel:
            return "no_asset_id"
        self._watcher.write(rel, data)
        self._state.put_asset(
            asset_key=event.asset_key,
            hlc=event.hlc if event.hlc is not None else self._clock_state_stamp(),
            version=event.version,
            content_hash=sha256_hex(data),
            ulid=event.ulid,
        )
        self._baseline[rel] = sha256_hex(data)
        return "applied"

    def _merge_db_record(self, event: SyncEvent) -> str:
        """db 通道合并：通用逻辑记录 → 单一镜像表（无 per-type 分支）。"""
        payload = event.payload
        table = payload.get("table")
        row = payload.get("row")
        if not isinstance(table, str) or not table or not isinstance(row, dict):
            logger.warning(
                "[sync_agent] 事件 %s DB 通道载荷缺 table/row，不落地（契约违约）",
                event.ulid,
            )
            return "no_record"
        view: tuple[str, int] | None = None
        raw_view = payload.get("view")
        if (
            isinstance(raw_view, dict)
            and isinstance(raw_view.get("group"), str)
            and raw_view.get("group")
            and isinstance(raw_view.get("seq"), int)
            and not isinstance(raw_view.get("seq"), bool)
        ):
            view = (str(raw_view["group"]), int(raw_view["seq"]))
        if event.hlc is not None and view is not None:
            # view 收敛路径的第二道门槛：改写事件键随内容变（不同 asset_key
            # 绕过资产级门槛），乱序重放按呈现维度现存 HLC 判 stale。
            current = self._state.get_mirror_view_hlc(*view)
            if current is not None and event.hlc <= current:
                return "stale"
        self._state.upsert_mirror_record(
            asset_key=event.asset_key,
            table_name=table,
            record_pk=str(payload.get("pk") or event.asset_id),
            row=row,
            owner_device=event.device_id,
            deleted=bool(payload.get("tombstone", False)),
            updated_at=event.updated_at,
            view=view,
            hlc=event.hlc if event.hlc is not None else self._clock_state_stamp(),
        )
        self.status.mirror_total += 1
        return "applied"

    async def _heartbeat_when_due(self) -> None:
        if self._clock_mono() < self._next_heartbeat_at:
            return
        self._next_heartbeat_at = self._clock_mono() + self._config.heartbeat_interval_secs
        try:
            await self._post_json(
                f"{self._config.relay_url}/devices/heartbeat",
                self._config.token,
                {
                    "device_id": self._config.device_id,
                    "tier": self._config.device_tier,
                    "app_version": self._config.app_version,
                    "capabilities": self._capabilities_payload(),
                    "address": self._self_address(),
                    "storage": self._storage_report(),
                },
            )
        except Exception as exc:  # noqa: BLE001 —— 心跳失败只告警（下周期重试）
            self.status.last_error = f"heartbeat: {type(exc).__name__}: {exc}"
            return
        self.status.heartbeats_total += 1

    async def _pull_exec_tokens_when_due(self) -> None:
        """属主端验证集周期拉取（D13'；节奏 = 心跳间隔，独立退避不与上行连坐）。

        拉取失败旧集保留（ExecTokenChannel.refresh 传播前不触碰内存集），
        退避窗口内不重试；exec 通道未启用 = 零拉取。
        """
        if self._exec_tokens is None or self._clock_mono() < self._next_exec_pull_at:
            return
        self._next_exec_pull_at = self._clock_mono() + self._config.heartbeat_interval_secs
        try:
            payload = await self._get_json(
                f"{self._config.relay_url}/exec_tokens", self._config.token
            )
            tokens = payload.get("tokens", [])
            self._exec_tokens.refresh(
                lambda: tokens if isinstance(tokens, list) else [], now_iso=self._now()
            )
        except Exception as exc:  # noqa: BLE001 —— relay 不可达统一退避，旧集保留
            self._exec_pull_failures += 1
            self.status.last_error = f"exec_pull: {type(exc).__name__}: {exc}"
            self._next_exec_pull_at = self._clock_mono() + self._backoff_delay(
                self._exec_pull_failures
            )
            logger.warning(
                "[sync_agent] exec 验证集拉取失败（第 %s 次）: %s",
                self._exec_pull_failures, exc,
            )
            return
        self._exec_pull_failures = 0

    async def pull_routing_view(self) -> dict[str, Any]:
        """云端权威设备注册表只读视图拉取（R30 数据面透传通道）。

        复用既有出站通道原语（``_get_json``：同 relay_url/token/HTTP 面）打云端
        ``GET {relay_url}/routing_view``——per-device token 自管的注册表读面
        （云端 GET /devices 为内核 admin JWT 门，端侧 sync token 唯一可达面）。
        失败原样上抛（降级裁决归调用方，通道层不吞）。
        """
        return await self._get_json(
            f"{self._config.relay_url}/routing_view", self._config.token
        )

    async def _pull_registry_when_due(self) -> None:
        """全网注册表副本周期拉取（A 档注册表复制；节奏 = 心跳间隔，独立退避）。

        失败旧副本保留（持久文件在，中枢离线核验不失效），退避窗口内不重试；
        副本路径未配置 = 面关零拉取。
        """
        if self._registry is None or self._clock_mono() < self._next_registry_pull_at:
            return
        self._next_registry_pull_at = self._clock_mono() + self._config.heartbeat_interval_secs
        try:
            payload = await self._get_json(
                f"{self._config.relay_url}/registry", self._config.token
            )
            raw = payload.get("devices")
            self._registry.save(
                devices=[d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else [],
                fetched_at=str(payload.get("fetched_at") or "") or self._now(),
            )
        except Exception as exc:  # noqa: BLE001 —— relay 不可达统一退避，旧副本保留
            self._registry_pull_failures += 1
            self.status.last_error = f"registry_pull: {type(exc).__name__}: {exc}"
            self._next_registry_pull_at = self._clock_mono() + self._backoff_delay(
                self._registry_pull_failures
            )
            logger.warning(
                "[sync_agent] 注册表副本拉取失败（第 %s 次，旧副本保留）: %s",
                self._registry_pull_failures, exc,
            )
            return
        self._registry_pull_failures = 0

    def _backoff_delay(self, failures: int) -> float:
        return min(_BACKOFF_BASE * _BACKOFF_FACTOR ** (failures - 1), _BACKOFF_MAX)

    def _self_address(self) -> str:
        """本机内核的对端可达地址（尽力而为，注册表侧可选字段）。

        IP = 去往 relay 主机的路由出口 IP（UDP connect 不发包取本端源址，局域网
        可靠）；端口 = 本机内核监听端口（AGENTOS_KERNEL_PORT，与内核缺省 9100 同源）。
        算得回环 = relay 与本机同主机（云端中枢自报 127.0.0.1:9100 的生产实证：
        他端拿到必不可达，还占掉一张「连接此内核」死卡）→ 改报 relay 源站
        （scheme://host[:非默认端口]，同机部署里中枢的对外服务面正是他端可达
        地址）；源站同为回环/localhost（中枢经 loopback 自配对，生产实证二）→
        如实报空串——设备卡回落心跳语义，不广播死地址。无路由/无主机同报空串。
        """
        host = urlparse(self._config.relay_url).hostname
        if not host:
            return ""
        ip = _lan_ip_toward(host)
        if not ip:
            return ""
        port = (os.environ.get(_KERNEL_PORT_ENV) or "").strip() or _KERNEL_PORT_DEFAULT
        if port.isdigit() and not _is_loopback_ip(ip):
            return f"http://{ip}:{port}"
        parsed = urlparse(self._config.relay_url)
        origin_host = parsed.hostname or ""
        if origin_host and not _is_loopback_ip(origin_host) and origin_host != "localhost":
            port_part = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{origin_host}{port_part}"
        return ""

    def _storage_report(self) -> dict[str, int] | None:
        """本端储存状态（数据根所在盘）：取不到（路径缺失等 OSError）返回 None。"""
        if self._config.data_root is None:
            return None
        try:
            usage = shutil.disk_usage(self._config.data_root)
        except OSError:
            return None
        return {"free_bytes": usage.free, "total_bytes": usage.total}

    def status_dict(self) -> dict[str, Any]:
        s = self.status
        status: dict[str, Any] = {
            "device_id": self._config.device_id,
            "tier": self._config.device_tier,
            "relay_url": self._config.relay_url,
            "storage": self._storage_report(),
            "bootstrap_done": s.bootstrap_done,
            "pending": self.pending_count,
            "cursor": s.cursor,
            "captured_total": s.captured_total,
            "applied_total": s.applied_total,
            "stale_total": s.stale_total,
            "flushed_total": s.flushed_total,
            "failed_flushes": s.failed_flushes,
            "pulled_total": s.pulled_total,
            "heartbeats_total": s.heartbeats_total,
            "mirror_total": s.mirror_total,
            "quarantined_total": s.quarantined_total,
            "last_error": s.last_error,
        }
        if self._file_channel is not None:
            status["file_channel"] = self._file_channel.status()
        if self._registry is not None:
            status["registry_replica"] = {
                "fetched_at": self._registry.fetched_at(),
                "devices": len(self._registry.devices()),
            }
        if self._exec_tokens is not None:
            status["exec_channel"] = {
                "capability": self._exec_capability(),
                "verification": self._exec_tokens.status(),
            }
        return status


def _lan_ip_toward(host: str) -> str | None:
    """去往 host 的路由出口本端 IP（UDP connect 仅做路由选择，不发包）。

    无路由/解析失败返回 None（调用方降级为不报地址）。回环目标返回回环 IP——
    同机判定归调用方（_self_address 据此换报 relay 源站）。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def _is_loopback_ip(ip: str) -> bool:
    """回环 IP 判定（宽松前缀够用：IPv4 127/8 与 ::1）。"""
    return ip == "::1" or ip.startswith("127.")


async def _aiohttp_post_json(url: str, token: str, payload: dict[str, Any]) -> None:
    """默认出站 POST（aiohttp）：非 2xx 抛 RelayStatusError（带状态码分道）。

    413 = relay 体限确定性拒绝（flush 拆批/隔离处置）；其余非 2xx 同走类型化
    异常但按瞬态退避（与网络失败同语义）。
    """
    import aiohttp  # noqa: PLC0415 —— 延迟导入：核心单测不触发网络栈

    timeout = aiohttp.ClientTimeout(total=30.0)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        if resp.status >= 300:
            body = await resp.text()
            raise RelayStatusError(resp.status, body)


async def _aiohttp_get_json(url: str, token: str) -> dict[str, Any]:
    """默认出站 GET（aiohttp）：非 2xx 抛异常。"""
    import aiohttp  # noqa: PLC0415 —— 延迟导入：核心单测不触发网络栈

    timeout = aiohttp.ClientTimeout(total=30.0)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        if resp.status >= 300:
            body = await resp.text()
            raise RuntimeError(f"relay {resp.status}: {body[:200]}")
        return await resp.json(content_type=None)
