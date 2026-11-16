"""E 事件汇聚导出核心（outbox 模式）——域事件 → 本地缓冲 → 批量上云。

设计依据：docs/working/分布式执行设计_事件汇聚E与插件宿主F_20261012.md §3.1–3.4。
组件（本模块只做机制，不碰内核）：

- :class:`OutboxStore` —— JSONL 追加式缓冲：断网时事件留文件天然不丢；成功
  投递后重写文件（去掉已投递前缀）即游标推进，崩溃窗口 ≤ 一次重复投递，由
  云端 ULID 幂等吸收（§3.2，at-least-once + 幂等收 = 恰好一次效果）。
  ``max_bytes`` 有界：压实后仍超限丢最旧并告警（有界缓冲语义）。
  （2026-10-15 起实现平移至共享根 ``outbox_store.py`` 单点化——sync_agent
  按同款机制复用，本模块 re-export 保持既有导入面不变。）
- :class:`EventFilter` —— 事件名 fnmatch 过滤（默认 ``run.*`` 任务/管道生命
  周期族），privacy 过滤的配置化落点（§3.5：不配置即不导出）。
- :class:`TraceExporter` —— 大小/时间双阈值攒批 → POST 信封
  ``{origin_host, origin_kind, protocol_version, events[]}``；失败指数退避
  重试（sleep/时钟可注入），退避期间不推进消费、事件留 outbox，恢复续传。

本机编排完全自治：云不可达不阻塞本机任何执行（§3.4）——入队是同步追加快
路径，推送只在后台循环发生且失败只退避不上抛。
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from _root_api import OutboxStore, new_ulid

logger = logging.getLogger(__name__)

#: 协议版本（信封 protocol_version；ingestion 按 §3.2 做版本宽容）。
PROTOCOL_VERSION = 1

#: 出站 HTTP 超时（秒）——云端批间隔上限量级，不宜更短制造半开连接。
_POST_TIMEOUT_SECS = 30.0

__all__ = ["OutboxStore", "EventFilter", "TraceExporter"]


class EventFilter:
    """事件名过滤（fnmatch 通配，大小写敏感）。

    patterns 为空 = 什么都不导出（fail-closed：不配置过滤规则就不出本机，
    §3.5 脱敏语义）。
    """

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = [p for p in patterns if p]

    def matches(self, event: str) -> bool:
        return any(fnmatch.fnmatchcase(event, p) for p in self._patterns)


class TraceExporter:
    """批量导出循环：双阈值攒批 + 指数退避 + 断网续传。

    Args:
        ingestion_url: 云端 ingestion 端点 URL（POST）。
        token: Bearer token（env 注入，不落配置明文）。
        origin_host / origin_kind: 来源标两级（§3.3）。
        outbox: 本地缓冲。
        event_filter: 事件名过滤器（本组件持有）。
        batch_max_events: 大小阈值（单批最大事件数）。
        flush_interval_secs: 时间阈值（队首事件最长等待）。
        post_json: 出站 POST 函数 ``(url, token, payload) -> None``（2xx 成功、
            否则抛异常）；默认 aiohttp 实现，测试注入桩或指向真实 HTTP 服务。
        sleep_fn: 退避睡眠函数（可注入 fake clock；测试记录请求延迟断言时序
            不变量，运行态为 ``asyncio.sleep``）。
        clock: 单调时钟（可注入；运行态 ``time.monotonic``）。
        poll_interval_secs: 后台循环空转粒度（空队列时的心跳上限）。
    """

    def __init__(
        self,
        *,
        ingestion_url: str,
        token: str,
        origin_host: str,
        origin_kind: str,
        outbox: OutboxStore,
        event_filter: EventFilter,
        batch_max_events: int = 100,
        flush_interval_secs: float = 30.0,
        post_json: Callable[[str, str, dict[str, Any]], Any] | None = None,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval_secs: float = 0.5,
    ) -> None:
        self._url = ingestion_url
        self._token = token
        self._origin_host = origin_host
        self._origin_kind = origin_kind
        self._outbox = outbox
        self._filter = event_filter
        self._batch_max = max(1, batch_max_events)
        self._flush_interval = flush_interval_secs
        self._poll_interval = poll_interval_secs
        self._post_json = post_json if post_json is not None else _aiohttp_post_json
        self._sleep = sleep_fn
        self._clock = clock
        # 退避参数（秒）：1 → 2 → 4 → … 封顶 300；成功归零。
        self._backoff_base = 1.0
        self._backoff_factor = 2.0
        self._backoff_max = 300.0
        self._consecutive_failures = 0
        #: 入队时刻（单调钟），与 outbox 待投递队列一一对应（恢复/丢最旧同步裁剪）。
        self._enqueue_ages: deque[float] = deque()
        self._wakeup = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False
        # 观测计数面（get_status 工具消费）。
        self.enqueued_total = 0
        self.flushed_total = 0
        self.failed_flushes = 0
        self.dropped_total = 0
        self.last_error = ""
        self.last_flush_at = ""

    # ── 入队与恢复 ──────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        """待投递事件数（状态观测面）。"""
        return len(self._outbox)

    def enqueue(self, event: str, tags: dict[str, Any], emitted_at: str) -> bool:
        """过滤 → ULID → outbox 追加。过滤不中返回 False（不烧 ULID）。"""
        if not self._filter.matches(event):
            return False
        dropped = self._outbox.append(
            {"ulid": new_ulid(), "event": event, "emitted_at": emitted_at, "tags": tags}
        )
        for _ in range(dropped):
            self._enqueue_ages.popleft()
        self.dropped_total += dropped
        self._enqueue_ages.append(self._clock())
        self.enqueued_total += 1
        self._wakeup.set()
        return True

    def load_pending(self) -> int:
        """启动恢复：outbox 文件 → 待投递队列；恢复事件按已到期处理（立即出批）。"""
        restored = self._outbox.load()
        now = self._clock()
        for _ in range(restored):
            self._enqueue_ages.append(now - self._flush_interval)
        return restored

    # ── 后台循环 ────────────────────────────────────────────

    def start(self) -> None:
        """启动后台导出循环（幂等）。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """停循环；未投递事件留 outbox，下次装载续传（不做关停期长冲刷）。"""
        self._stopping = True
        self._wakeup.set()
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
            timeout = self._next_wake_timeout()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout)
            except TimeoutError:
                pass
            self._wakeup.clear()
            await self.flush_when_ready()

    def _next_wake_timeout(self) -> float | None:
        """距"时间阈值到点"的秒数（无待投递返回 None，挂起等事件唤醒）。"""
        if not self._enqueue_ages:
            return None
        remaining = self._flush_interval - (self._clock() - self._enqueue_ages[0])
        return max(0.0, min(remaining, self._poll_interval))

    def _batch_ready(self) -> bool:
        """双阈值判定：大小（待投递 ≥ 批上限）或时间（队首等待 ≥ 间隔）任一命中。"""
        if not self._enqueue_ages:
            return False
        if len(self._enqueue_ages) >= self._batch_max:
            return True
        return self._clock() - self._enqueue_ages[0] >= self._flush_interval

    async def flush_when_ready(self) -> bool:
        """就绪则出批；失败（网络/落盘）在退避后重试同批，直到成功或不再就绪。

        端点持续不可达或 outbox 持续落盘失败时本方法持续退避重试（outbox 语义：
        失败不推进消费、事件不丢），由调用方 stop() 打断。

        Returns:
            本轮是否成功投递了至少一批。
        """
        delivered = False
        while self._batch_ready():
            if await self._flush_once():
                delivered = True
            # 失败已在 _flush_once 内退避；条件重判后重试同批（保持顺序）。
        return delivered

    async def _flush_once(self) -> bool:
        batch = self._outbox.peek(min(self._batch_max, len(self._enqueue_ages)))
        if not batch:
            self._enqueue_ages.clear()
            return False
        envelope = {
            "origin_host": self._origin_host,
            "origin_kind": self._origin_kind,
            "protocol_version": PROTOCOL_VERSION,
            "events": batch,
        }
        try:
            await self._post_json(self._url, self._token, envelope)
        except Exception as exc:  # noqa: BLE001 —— 网络失败统一退避，事件不丢
            await self._record_flush_failure(exc, stage="批量投递失败")
            return False
        try:
            self._outbox.commit(len(batch))
        except OSError as exc:
            # 落盘失败（commit/_rewrite）与网络失败同款记账退避：循环续跑不终结
            # 后台 task。失败批已在 POST 成功后弹出（投递已完成），文件保留旧
            # 内容 → 重启重传由云端 ULID 幂等吸收，at-least-once 不破。
            await self._record_flush_failure(exc, stage="outbox 落盘失败")
            return False
        for _ in range(len(batch)):
            self._enqueue_ages.popleft()
        self._consecutive_failures = 0
        self.flushed_total += len(batch)
        self.last_error = ""
        logger.info("[trace_exporter] 已投递 %s 条事件到 %s", len(batch), self._url)
        return True

    async def _record_flush_failure(self, exc: Exception, *, stage: str) -> None:
        """投递面失败统一记账（网络/磁盘同管道）：连续计数、last_error、WARNING
        告警、指数退避；成功路径归零（_flush_once 成功分支）。"""
        self._consecutive_failures += 1
        self.failed_flushes += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        delay = min(
            self._backoff_base * self._backoff_factor ** (self._consecutive_failures - 1),
            self._backoff_max,
        )
        logger.warning(
            "[trace_exporter] %s（第 %s 次），%.1fs 后重试: %s",
            stage, self._consecutive_failures, delay, exc,
        )
        await self._sleep(delay)


async def _aiohttp_post_json(url: str, token: str, payload: dict[str, Any]) -> None:
    """默认出站 POST（aiohttp）：非 2xx 抛异常（退避语义与网络失败一致）。"""
    import aiohttp  # noqa: PLC0415 —— 延迟导入：核心单测不触发网络栈

    timeout = aiohttp.ClientTimeout(total=_POST_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        if resp.status >= 300:
            body = await resp.text()
            raise RuntimeError(f"ingestion {resp.status}: {body[:200]}")
