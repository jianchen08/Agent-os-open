"""管道引擎的流式输出口（output port）。

把"流式输出"从引擎核心拆出来：引擎只管"执行循环"，本模块负责把执行过程
按 FIFO 有序、可保活地推到 bridge（→ sink → WebSocket）。

为什么单独成模块（架构理由，不是行数理由）：
- 流式输出是引擎的【出口】，不是引擎的【内部职责】。引擎核心（生命周期 + 循环）
  不应感知 chunk 队列、消费者协程、keepalive 这些传输层细节。
- 依赖方向：本模块依赖 bridge（出口目标）+ 引擎注入的 stop_check 回调，
  不反向依赖引擎内部状态机。引擎通过 self._streaming 委托，保持单向依赖。

职责边界：
- 持有：当前 bridge、chunk 队列、消费者/keepalive 协程、流式上下文（on_chunk 标志）。
- 对外：start(emit stream_start + 安装 on_chunk + 起协程)、emit_finish/error/suspend、
  drain(挂起前排空)、save/restore/set 上下文、shutdown(收尾取消协程)。
- 不持有：引擎的 state 机、生命周期、stop 信号判定（后者通过 stop_check 回调注入）。
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class StreamingOutput:
    """引擎流式输出口：管理 bridge、chunk 队列、消费者与 keepalive 协程。

    引擎在 run/resume 时调 start()；每个流式 chunk 经 on_chunk 入队 → 消费者
    FIFO 投递到 bridge；长静默期 keepalive 推保活包防前端断连；finish/error/
    suspend 时发终止事件；shutdown 取消协程防泄漏。

    Args:
        pipeline_id: 管道 ID（日志与事件信封用）。
        stop_check: 引擎注入的"是否收到停止信号"回调。on_chunk 每个 chunk 调用，
            命中即 raise CancelledError 协作式中断（见 _on_chunk 文档）。
        on_user_stop: 引擎注入的"标记用户主动停止"回调，无参数。on_chunk 命中
            stop 信号、raise CancelledError 之前同步调用一次，让引擎的 run()
            CancelledError 分支能据此走"安静退出"而非"失败"路径。可选（不注入
            则协作式中断仍按普通取消处理）。
    """

    # keepalive：距上个 chunk 超过该秒数即开始周期性发保活包。
    # 阈值远小于前端心跳超时（45s），确保静默期前端始终有"连接活着"的信号。
    _KEEPALIVE_IDLE_THRESHOLD: float = 8.0
    _KEEPALIVE_INTERVAL: float = 5.0
    # chunk 队列容量：流式高峰防背压，满了丢弃（仅 WARNING 不阻塞引擎）。
    _CHUNK_QUEUE_MAXSIZE: int = 10000

    def __init__(
        self,
        pipeline_id: str,
        stop_check: Callable[[], bool],
        on_user_stop: Callable[[], None] | None = None,
    ) -> None:
        self._pipeline_id = pipeline_id
        self._stop_check = stop_check
        # 用户主动停止标记回调：协作式中断前回写引擎标志，保持单向依赖
        # （streaming 不直接持有引擎引用，只回调）。
        self._on_user_stop = on_user_stop

        # 当前 bridge 引用（引擎 run 时从 registry 解析后注入，见 attach_bridge）。
        self._bridge: Any = None
        # chunk 有序队列 + 单消费者协程（start 时创建，shutdown 释放）。
        self._chunk_queue: asyncio.Queue[dict | None] | None = None
        self._chunk_consumer_task: asyncio.Task[None] | None = None
        # keepalive 协程（与消费者同生命周期）。
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_chunk_monotonic: float = 0.0
        # 流式上下文：跨 run/resume 保存的 on_chunk 回调与 streaming 标志。
        self._streaming_on_chunk: Any = None
        self._streaming_flag: bool = False

    # ------------------------------------------------------------------
    # bridge 绑定与查询
    # ------------------------------------------------------------------

    @property
    def bridge(self) -> Any:
        """当前绑定的 bridge（引擎/外部模块通过此访问发终止事件）。"""
        return self._bridge

    def attach_bridge(self, bridge: Any) -> None:
        """绑定当前 bridge（引擎从 registry 解析后注入）。"""
        self._bridge = bridge

    def reset_for_run(self) -> None:
        """新一轮 run 前重置流式上下文标志（不碰协程/队列，那由 start/shutdown 管）。"""
        self._streaming_flag = False
        self._streaming_on_chunk = None

    # ------------------------------------------------------------------
    # start：发 stream_start + 安装 on_chunk + 起协程
    # ------------------------------------------------------------------

    async def start(self, state: dict[str, Any]) -> None:
        """发 emit_start 并安装 on_chunk 适配器，启动消费者 + keepalive 协程。

        在引擎首次迭代前调用，确保 bridge 就绪后立即推 stream_start。

        Args:
            state: 管道状态字典；on_chunk 适配器写入 state["on_chunk"] 供
                LLM 适配器读取，state["preset_ai_record_id"] 由 bridge 写入。
        """
        bridge = self._bridge
        if bridge is None:
            return

        await bridge.emit_start(state)

        # 启动 chunk 有序队列 + 单消费者协程
        if self._chunk_queue is None:
            self._chunk_queue = asyncio.Queue(maxsize=self._CHUNK_QUEUE_MAXSIZE)
        if self._chunk_consumer_task is None or self._chunk_consumer_task.done():
            self._chunk_consumer_task = asyncio.create_task(self._chunk_consumer())

        # 启动 keepalive 协程（长静默期防前端断连，见 _stream_keepalive_loop）
        self._last_chunk_monotonic = _time.monotonic()
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._stream_keepalive_loop())

        # 安装 on_chunk 到 state（LLM 适配器读取 state["on_chunk"]）
        if state.get("on_chunk") is None:
            state["on_chunk"] = self._on_chunk
            state["streaming"] = True
            self._streaming_on_chunk = self._on_chunk
            self._streaming_flag = True

    # ------------------------------------------------------------------
    # on_chunk：同步回调适配器（LLM 适配器调用）
    # ------------------------------------------------------------------

    def _on_chunk(self, chunk: dict) -> None:
        """同步→异步适配器：LLM 适配器的 on_chunk 是同步回调。

        通过 asyncio.Queue 保证 chunk FIFO 有序投递到 bridge.emit_chunk。

        协作式停止兜底：当 stop_generation 信号已写入 state（deliver_signal），
        且当前 await 不可中断（httpx C 层 socket recv 实测卡死，见 adapter.py BUG
        注释），cancel engine_task 无法打断。此时本回调是唯一可靠的停止检查点：
        每个 chunk 同步调用它，命中信号即 raise CancelledError。

        为什么用 raise CancelledError 而非返回 "stop"：adapter 的 on_chunk 返回
        "stop" 语义已被"流式重复检测"占用（会设 stream_repetition=True，llm_core
        误判为重复而重试）。本回调与 adapter 流式循环同在 engine_task 协程栈内
        （_run_loop → llm_core.execute → _call_llm → adapter.completion 同步调
        on_chunk），raise CancelledError 冒泡到 _run_loop 的 except CancelledError，
        复用既有的停止清理路径。与 cancel 互补：cancel 打断 await，协作式兜底 httpx
        不可中断。

        失败隔离：推送失败只 log warning，不影响 engine 运行。

        Args:
            chunk: LLM 适配器回调的 chunk 字典
        """
        # 协作式停止：每个 chunk 检查 stop_generation 信号（只读不删，由插件消费）。
        if self._stop_check():
            logger.info(
                "[Streaming] on_chunk 检测到 stop_generation 信号，协作式中断流式: pipeline=%s chunk_type=%s",
                self._pipeline_id[:12],
                chunk.get("type", "?"),
            )
            # 标记本次取消来自用户"停止生成"，供引擎 run() 的 CancelledError
            # 分支走安静退出（不 fail_task / 不写 RAW_ERROR / 不 emit_error）。
            if self._on_user_stop is not None:
                try:
                    self._on_user_stop()
                except Exception:
                    logger.debug(
                        "[Streaming] on_user_stop 回调失败（忽略，仍按停止处理）: pipeline=%s",
                        self._pipeline_id[:12],
                    )
            raise asyncio.CancelledError("stop_generation signal")

        if self._bridge is None or self._chunk_queue is None:
            return

        try:
            self._chunk_queue.put_nowait(chunk)
            # 更新 keepalive 心跳基准：每个真实 chunk 入队即重置，
            # 使 _stream_keepalive_loop 在 chunk 密集时不发保活包。
            self._last_chunk_monotonic = _time.monotonic()
        except asyncio.QueueFull:
            logger.warning(
                "[Streaming] chunk queue 满，丢弃 chunk: pipeline=%s",
                self._pipeline_id[:12],
            )
        except Exception as exc:
            logger.debug(
                "[Streaming] on_chunk 入队失败: %s pipeline=%s",
                exc,
                self._pipeline_id[:12],
            )

    # ------------------------------------------------------------------
    # 消费者协程
    # ------------------------------------------------------------------

    async def _chunk_consumer(self) -> None:
        """单消费者协程：从 Queue 取 chunk，FIFO 有序调用 bridge.emit_chunk。

        保证 thinking_start → thinking_chunk → thinking_end 等多事件 chunk 时序正确。
        """
        bridge = self._bridge
        queue = self._chunk_queue
        if bridge is None or queue is None:
            return

        while True:
            try:
                chunk = await queue.get()
            except asyncio.CancelledError:
                break
            if chunk is None:  # 哨兵值，通知退出
                break
            try:
                await bridge.emit_chunk(chunk)
            except Exception as exc:
                logger.warning(
                    "[Streaming] chunk_consumer emit_chunk 失败（非致命）: %s pipeline=%s",
                    exc,
                    self._pipeline_id[:12],
                )

    # ------------------------------------------------------------------
    # drain：挂起前排空队列
    # ------------------------------------------------------------------

    async def drain(self, timeout: float = 2.0) -> None:
        """等待 chunk queue 清空，确保 emit_suspend 前所有流式 chunk 已投递。

        每 5ms 让出事件循环，让 _chunk_consumer 取出并投递 queue 里残留 chunk。
        带超时兜底，防止 consumer 卡住时管道永久挂起。

        Args:
            timeout: 最长等待秒数（默认 2s）
        """
        queue = self._chunk_queue
        if queue is None:
            return
        deadline = _time.monotonic() + timeout
        while not queue.empty():
            if _time.monotonic() > deadline:
                remaining = queue.qsize()
                if remaining:
                    logger.warning(
                        "[Streaming] chunk queue drain 超时，仍有 %d 个 chunk 未消费: pipeline=%s",
                        remaining,
                        self._pipeline_id[:12],
                    )
                return
            await asyncio.sleep(0.005)

    # ------------------------------------------------------------------
    # keepalive 协程
    # ------------------------------------------------------------------

    async def _stream_keepalive_loop(self) -> None:
        """流式 keepalive 协程：长静默期周期性推 stream_keepalive 防断连。

        LLM reasoning 热身（glm-5.2 实测首 chunk 前静默 20~30s）、长 tool_call 组装期间，
        后端向该 pipeline 零事件输出，前端只能靠心跳确认连接存活。心跳窗口边缘（ack 稍慢
        即误断），静默期一旦网络/调度抖动，前端会判定连接死亡主动断连。
        因此流式活跃但 chunk 静默超过阈值时，每 _KEEPALIVE_INTERVAL 秒推一个
        stream_keepalive，填补静默期（stream_keepalive 事件前端已有 handler：
        streaming/index.ts）。chunk 密集时（_on_chunk 持续重置基准）自动不发。

        与 _chunk_consumer 同生命周期：start 时启动，shutdown 取消。沿用 adapter
        _stream_heartbeat 的 CancelledError 退出范式。
        """
        bridge = self._bridge
        if bridge is None:
            return
        try:
            while True:
                await asyncio.sleep(self._KEEPALIVE_INTERVAL)
                # 流式未开始或已结束：不发保活包（emit_finish/emit_suspend 已置 False）
                if not getattr(bridge, "_stream_started", False):
                    continue
                # sink 熔断（用户长期离线，连续推送失败达阈值）：停止 keepalive，避免
                # 向已下线用户无限重试燃烧 CPU。下一个 turn 的 start() 会重建 keepalive
                # task（见 start() 的 done() 检查），重连后 sink 计数归零，自然恢复。
                if getattr(bridge.output_sink, "is_dead", False):
                    logger.info(
                        "[Streaming] sink 已熔断，停止 keepalive: pipeline=%s thread_id=%s",
                        self._pipeline_id[:12],
                        (getattr(bridge.output_sink, "_thread_id", "") or "(empty)")[:12],
                    )
                    break
                _idle = _time.monotonic() - self._last_chunk_monotonic
                # chunk 密集（最近收过）：抑制保活包
                if _idle < self._KEEPALIVE_IDLE_THRESHOLD:
                    continue
                # 静默超阈值：推保活包。失败隔离，不阻断循环。
                try:
                    await bridge.send_event(
                        bridge._make_event(
                            "stream_keepalive",
                            {
                                "idle_seconds": round(_idle, 1),
                                "pipeline_id": self._pipeline_id,
                            },
                        )
                    )
                except Exception:
                    logger.debug(
                        "[Streaming] keepalive 推送失败（非致命）: pipeline=%s",
                        self._pipeline_id[:12],
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # 终止事件透传（finish/error/suspend）
    # ------------------------------------------------------------------

    async def emit_finish(self, state: dict[str, Any]) -> None:
        """透传 bridge.emit_finish（流式正常结束）。"""
        if self._bridge is not None:
            await self._bridge.emit_finish(state)

    async def emit_error(self, exc: BaseException) -> None:
        """透传 bridge.emit_error（流式异常结束）。"""
        if self._bridge is not None:
            await self._bridge.emit_error(exc)

    async def emit_suspend(self, state: dict[str, Any]) -> None:
        """透传 bridge.emit_suspend（流式挂起）。"""
        if self._bridge is not None:
            await self._bridge.emit_suspend(state)

    async def emit_start(self, state: dict[str, Any]) -> None:
        """透传 bridge.emit_start（resume 路径直接发，不走 start 协程编排）。"""
        if self._bridge is not None:
            await self._bridge.emit_start(state)

    # ------------------------------------------------------------------
    # 流式上下文保存/恢复（跨 run/resume）
    # ------------------------------------------------------------------

    def save_context(self, state: dict[str, Any]) -> None:
        """从 state 保存流式上下文（挂起前调用）。"""
        on_chunk = state.get("on_chunk")
        if on_chunk is not None:
            self._streaming_on_chunk = on_chunk
            self._streaming_flag = state.get("streaming", True)

    def restore_context(self, state: dict[str, Any]) -> None:
        """恢复流式上下文到 state（resume 时调用）。"""
        if self._streaming_on_chunk is not None and "on_chunk" not in state:
            state["on_chunk"] = self._streaming_on_chunk
            state["streaming"] = self._streaming_flag

    def set_streaming_context(self, on_chunk: Any, streaming: bool = True) -> None:
        """外部设置流式上下文（替代直接写私有字段）。"""
        self._streaming_on_chunk = on_chunk
        self._streaming_flag = streaming

    # ------------------------------------------------------------------
    # shutdown：取消协程防泄漏
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """取消消费者与 keepalive 协程（run 结束/挂起/cleanup 时调用，防泄漏）。

        向队列投哨兵 None 让消费者自然退出（带 2s 超时兜底），超时则 cancel；
        keepalive 直接 cancel（循环靠 CancelledError 退出）。
        """
        if self._chunk_queue is not None and self._chunk_consumer_task is not None:
            try:
                self._chunk_queue.put_nowait(None)
                await asyncio.wait_for(self._chunk_consumer_task, timeout=2.0)
            except Exception:
                self._chunk_consumer_task.cancel()
            self._chunk_queue = None
            self._chunk_consumer_task = None

        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None
