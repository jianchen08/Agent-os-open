"""管道流式事件桥接模块。

将 engine 的 on_chunk 同步回调转换为前端 WebSocket 协议事件，
通过 IOutputSink 抽象统一发送到 DirectWebSocketSink（主管道）或 TargetedSink（子管道）。
消除了 start_server.py 和 task_worker.py 中约 300 行重复的流式事件发送逻辑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IOutputSink 协议与实现
# ---------------------------------------------------------------------------

@runtime_checkable
class IOutputSink(Protocol):
    """输出目标协议，抽象 WebSocket 直连与广播两种发送方式。"""

    async def send_event(self, event: dict) -> bool:
        """发送事件到前端，成功返回 True，失败返回 False。"""
        ...

    @property
    def sink_id(self) -> str:
        """返回输出目标的唯一标识，用于日志和调试。"""
        ...


class DirectWebSocketSink:
    """直接 WebSocket 连接输出目标，包装单个 WebSocket 连接。"""

    def __init__(self, websocket: Any) -> None:
        """初始化 WebSocket 输出目标。

        Args:
            websocket: WebSocket 连接对象，需支持 send_text 方法
        """
        self._websocket = websocket
        # 尝试获取 websocket 的唯一标识
        self._sink_id = getattr(websocket, "client", None)
        if self._sink_id is not None:
            self._sink_id = str(self._sink_id)
        else:
            self._sink_id = f"ws-{id(websocket):#x}"

    @property
    def sink_id(self) -> str:
        """返回 WebSocket 连接标识。"""
        return self._sink_id

    async def send_event(self, event: dict) -> bool:
        """通过 WebSocket 发送 JSON 事件。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        try:
            await asyncio.wait_for(
                self._websocket.send_text(json.dumps(event, ensure_ascii=False, default=str)),
                timeout=5.0,
            )
            return True
        except (asyncio.TimeoutError, Exception):
            return False


class TargetedSink:
    """定向输出目标，按 thread_id 直接路由事件到对应 WebSocket 连接。

    路由失败时记录错误并返回 False，不广播。
    广播是消息串扰的根因，已被删除。
    """

    def __init__(self, notifier: Any, thread_id: str) -> None:
        """初始化定向输出目标。

        Args:
            notifier: 具备 send_to_thread 和 send_to_user 方法的通知器对象
            thread_id: 目标会话的 ws_thread_id（通过 pipeline_thread_map 映射得到）
        """
        self._notifier = notifier
        self._thread_id = thread_id
        self._fail_count: int = 0

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    async def send_event(self, event: dict) -> bool:
        """直接路由事件到指定 thread_id 的 WebSocket 连接。

        路由失败时记录错误，不广播。广播会导致消息串扰。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        if not self._thread_id:
            self._fail_count += 1
            if self._fail_count <= 3:
                logger.error(
                    "TargetedSink: thread_id 为空，事件丢失 #%d type=%s pipeline=%s",
                    self._fail_count,
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            return False

        try:
            ok = await self._notifier.send_to_thread(self._thread_id, event)
            if ok:
                return True
            self._fail_count += 1
            if self._fail_count <= 3:
                logger.error(
                    "TargetedSink: 定向推送失败 #%d thread_id=%s type=%s pipeline=%s",
                    self._fail_count,
                    self._thread_id[:12],
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            return False
        except Exception:
            self._fail_count += 1
            logger.error(
                "TargetedSink: 推送异常 #%d thread_id=%s type=%s err=%s",
                self._fail_count,
                self._thread_id[:12],
                event.get("type", "?"),
                exc_info=True,
            )
            return False


# ---------------------------------------------------------------------------
# PipelineStreamBridge 核心类
# ---------------------------------------------------------------------------

class PipelineStreamBridge:
    """管道流式事件桥接器，将 engine 的 on_chunk 回调桥接到 IOutputSink。

    核心职责：
    1. 提供 on_chunk 同步回调供 engine.run() 使用
    2. 通过 drain_loop 异步消费队列、格式化事件、经 sink 发送到前端
    3. 管理 thinking 状态追踪、文本累积、心跳保活、挂起超时检测
    """

    def __init__(
        self,
        pipeline_id: str,
        output_sink: IOutputSink,
        message_id: str | None = None,
    ) -> None:
        """初始化管道流式桥接器。

        Args:
            pipeline_id: 管道 ID，附加到每个事件的 data 中
            output_sink: 输出目标，负责实际发送事件
            message_id: 消息 ID，不传则自动生成
        """
        self.pipeline_id = pipeline_id
        self.output_sink = output_sink
        self.message_id = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        self._sent_tool_starts: set[str] = set()

        # 内部状态
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._chunk_event: asyncio.Event = asyncio.Event()
        self._thinking_active: bool = False
        self._accumulated_content: list[str] = []
        self._thinking_content_parts: list[str] = []
        self._stream_started: bool = False

    def on_chunk(self, chunk: dict) -> None:
        """同步回调，将 chunk 放入内部队列并立即唤醒 drain_loop。

        此方法直接传给 engine.run(on_chunk=bridge.on_chunk)，
        由 LLM Adapter 在流式生成时同步调用。

        Args:
            chunk: 管道事件字典，包含 type 和 content 等字段
        """
        _ct = chunk.get("type", "?")
        if _ct in ("tool_start", "tool_result"):
            logger.debug(
                "on_chunk: type=%s tool=%s msg=%s pipeline=%s",
                _ct, chunk.get('tool_name'),
                self.message_id[:12], self.pipeline_id[:12],
            )
        self._queue.put_nowait(chunk)
        self._chunk_event.set()

    def stop(self) -> None:
        """发送哨兵值 None 终止 drain_loop。"""
        self._queue.put_nowait(None)

    async def _send_event(self, event: dict) -> bool:
        """通过 output_sink 发送事件，记录发送失败日志。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        success = await self.output_sink.send_event(event)
        if not success:
            logger.debug(
                "事件发送失败: sink=%s, event_type=%s",
                self.output_sink.sink_id,
                event.get("type", "unknown"),
            )
        return success

    async def _send_stream_start(self) -> None:
        """发送 stream_start 事件，通知前端开始接收流式输出。"""
        self._stream_started = True
        await self._send_event({
            "type": "stream_start",
            "data": {
                "message_id": self.message_id,
                "pipeline_id": self.pipeline_id,
            },
        })

    async def _close_thinking_if_active(self, duration_ms: Any = None) -> None:
        """如果 thinking 处于活跃状态，发送 thinking_end 事件关闭。

        Args:
            duration_ms: thinking 持续时间（毫秒），可为 None
        """
        if self._thinking_active:
            self._thinking_active = False
            await self._send_event({
                "type": "thinking_end",
                "data": {
                    "message_id": self.message_id,
                    "duration_ms": duration_ms,
                    "pipeline_id": self.pipeline_id,
                },
            })

    async def _handle_chunk(self, chunk: dict) -> None:
        """处理单个 chunk 事件，转换为前端协议格式并发送。

        Args:
            chunk: 包含 type 和 content 等字段的管道事件字典
        """
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")

        if chunk_type == "text" and content:
            self._accumulated_content.append(content)
            await self._send_event({
                "type": "stream_chunk",
                "data": {
                    "message_id": self.message_id,
                    "content": content,
                    "pipeline_id": self.pipeline_id,
                },
            })

        elif chunk_type == "thinking" and content:
            self._thinking_content_parts.append(content)
            if not self._thinking_active:
                self._thinking_active = True
                await self._send_event({
                    "type": "thinking_start",
                    "data": {
                        "message_id": self.message_id,
                        "pipeline_id": self.pipeline_id,
                    },
                })
            await self._send_event({
                "type": "thinking_chunk",
                "data": {
                    "message_id": self.message_id,
                    "content": content,
                    "pipeline_id": self.pipeline_id,
                },
            })

        elif chunk_type == "thinking_end":
            await self._close_thinking_if_active(chunk.get("duration_ms"))

        elif chunk_type == "tool_start":
            _call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            self._sent_tool_starts.add(_call_id)
            logger.info(
                "tool_start: tool=%s call_id=%s pipeline=%s",
                chunk.get('tool_name'), _call_id, self.pipeline_id[:12],
            )
            await self._send_event({
                "type": "tool_start",
                "data": {
                    "message_id": self.message_id,
                    "tool_name": chunk.get("tool_name", "unknown"),
                    "args": chunk.get("args"),
                    "call_id": chunk.get("call_id"),
                    "pipeline_id": self.pipeline_id,
                },
            })

        elif chunk_type == "tool_result":
            _result_call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            if _result_call_id not in self._sent_tool_starts:
                logger.info(
                    "FIXUP: tool_result without tool_start: tool=%s pipeline=%s",
                    chunk.get('tool_name'), self.pipeline_id[:12],
                )
                self._sent_tool_starts.add(_result_call_id)
                await self._send_event({
                    "type": "tool_start",
                    "data": {
                        "message_id": self.message_id,
                        "tool_name": chunk.get("tool_name", "unknown"),
                        "args": None,
                        "call_id": chunk.get("call_id"),
                        "pipeline_id": self.pipeline_id,
                    },
                })
            await self._send_event({
                "type": "tool_result",
                "data": {
                    "message_id": self.message_id,
                    "tool_name": chunk.get("tool_name", "unknown"),
                    "success": chunk.get("success", True),
                    "result": chunk.get("result"),
                    "duration_ms": chunk.get("duration_ms"),
                    "call_id": chunk.get("call_id"),
                    "pipeline_id": self.pipeline_id,
                },
            })

        elif chunk_type == "iteration":
            # 迭代开始时关闭旧的 thinking
            await self._close_thinking_if_active(None)
            await self._send_event({
                "type": "iteration",
                "data": {
                    "message_id": self.message_id,
                    "iteration": chunk.get("iteration", 0),
                    "max_iterations": chunk.get("max_iterations", 0),
                    "pipeline_id": self.pipeline_id,
                },
            })

    async def drain_loop(
        self,
        engine_task: asyncio.Task,
        *,
        heartbeat_callback: Callable[[], Coroutine[Any, Any, None]] | None = None,
        heartbeat_interval: float = 5.0,
        suspend_check: Callable[[], bool] | None = None,
        call_timeout: float | None = None,
    ) -> dict:
        """异步消费队列，格式化事件，通过 sink 发送到前端。

        核心消费循环：从内部队列取出 chunk → 转换为前端协议事件 → 经 sink 发送。
        支持心跳保活和挂起超时检测。

        Args:
            engine_task: 管道引擎的异步 Task，用于判断管道是否结束
            heartbeat_callback: 可选的心跳回调协程，在 TimeoutError 时调用
            heartbeat_interval: 心跳间隔秒数，默认 5.0
            suspend_check: 可选的挂起检测函数，返回 True 表示管道已挂起
            call_timeout: 可选的 LLM 活动超时秒数，挂起期间不计入

        Returns:
            dict 包含:
            - accumulated_content: str 累积的完整文本
            - thinking_content_parts: list[str] thinking 内容片段
        """
        logger.info(
            "drain_loop 开始: msg=%s pipeline=%s sink=%s",
            self.message_id[:16], self.pipeline_id[:12], type(self.output_sink).__name__,
        )
        # 1. 发送 stream_start
        try:
            await self._send_stream_start()
            logger.info(
                "drain_loop: stream_start 已发送: msg=%s pipeline=%s sink=%s",
                self.message_id[:12], self.pipeline_id[:12], self.output_sink.sink_id,
            )
        except Exception as _e:
            logger.error("drain_loop: stream_start 失败: %s", _e, exc_info=True)
            return {"accumulated_content": "", "thinking_content_parts": [], "timed_out": False}

        last_keepalive = asyncio.get_event_loop().time()
        # 挂起超时检测需要独立追踪最后活跃时间
        _last_active = asyncio.get_event_loop().time() if call_timeout is not None else 0.0

        _suspend_detected_at: float | None = None

        # 2. 主循环：消费队列
        _chunk_count = 0
        while not engine_task.done() or not self._queue.empty():
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                now = asyncio.get_event_loop().time()

                # 管道挂起且无更多 chunk → 结束流式输出
                # 主管道挂起后 engine_task 不会 done，需要通过 suspend_check 检测。
                # 延迟 1 秒确认挂起，避免在挂起过程中还有残留 chunk。
                if (
                    not engine_task.done()
                    and self._queue.empty()
                    and suspend_check is not None
                    and suspend_check()
                ):
                    if _suspend_detected_at is None:
                        _suspend_detected_at = now
                    elif now - _suspend_detected_at > 1.0:
                        logger.info(
                            "drain_loop: 管道已挂起，结束流式输出: pipeline=%s chunks=%d",
                            self.pipeline_id[:12], _chunk_count,
                        )
                        break
                else:
                    _suspend_detected_at = None

                # 心跳保活
                if heartbeat_callback is not None and now - last_keepalive > heartbeat_interval:
                    last_keepalive = now
                    try:
                        await heartbeat_callback()
                    except Exception:
                        pass
                    try:
                        await self._send_event({
                            "type": "stream_keepalive",
                            "data": {
                                "message_id": self.message_id,
                                "pipeline_id": self.pipeline_id,
                            },
                        })
                    except Exception:
                        pass

                # 挂起超时检测：管道挂起时不计入超时，超时后直接 return
                if call_timeout is not None:
                    if suspend_check is not None and suspend_check():
                        _last_active = now
                    else:
                        elapsed = now - _last_active
                        if elapsed > call_timeout:
                            logger.warning(
                                "LLM 活动超时 (%.1fs/%.1fs): pipeline=%s",
                                elapsed, call_timeout, self.pipeline_id,
                            )
                            # BUG-FIX-fix_20260510_streaming_stuck:
                            # 问题根因: 超时路径直接 return，跳过 stream_end 发送，
                            #          导致前端 streamingTabs[pipelineId] 永远为 true，输入框卡在执行中状态。
                            # 修复方案: 超时退出前补发 stream_end，确保前端总是收到配对的 start/end。
                            # 影响范围: 所有标签页的流式指示器和停止按钮。
                            await self._close_thinking_if_active(None)
                            full_content = "".join(self._accumulated_content)
                            try:
                                await self._send_event({
                                    "type": "stream_end",
                                    "data": {
                                        "message_id": self.message_id,
                                        "full_content": full_content,
                                        "pipeline_id": self.pipeline_id,
                                        "timed_out": True,
                                    },
                                })
                            except Exception as _send_err:
                                logger.debug("超时 stream_end 发送失败: %s", _send_err)
                            return {
                                "accumulated_content": full_content,
                                "thinking_content_parts": list(self._thinking_content_parts),
                                "timed_out": True,
                            }

                continue

            # 收到 chunk，更新活跃时间
            if call_timeout is not None:
                _last_active = asyncio.get_event_loop().time()

            if chunk is None:
                break

            _chunk_type = chunk.get("type", "?")
            if _chunk_type in ("tool_start", "tool_result"):
                logger.debug(
                    "drain: type=%s tool=%s pipeline=%s",
                    _chunk_type, chunk.get('tool_name'), self.pipeline_id[:12],
                )

            _chunk_count += 1
            await self._handle_chunk(chunk)

        # 3. 管道结束后关闭可能仍活跃的 thinking
        await self._close_thinking_if_active(None)

        # 4. 发送 stream_end
        full_content = "".join(self._accumulated_content)
        _end_ok = await self._send_event({
            "type": "stream_end",
            "data": {
                "message_id": self.message_id,
                "full_content": full_content,
                "pipeline_id": self.pipeline_id,
            },
        })
        logger.info(
            "drain_loop: stream_end %s: msg=%s pipeline=%s content=%d chars chunks=%d/%d",
            "OK" if _end_ok else "FAILED",
            self.message_id[:12], self.pipeline_id[:12],
            len(full_content), _chunk_count, len(self._accumulated_content),
        )

        return {
            "accumulated_content": full_content,
            "thinking_content_parts": list(self._thinking_content_parts),
            "timed_out": False,
        }

    async def send_new_message(self, content: str, sequence: int = 1) -> None:
        """发送 new_message 最终消息，包含完整的助手消息数据。

        Args:
            content: 消息内容
            sequence: 消息序号，默认为 1
        """
        await self._send_event({
            "type": "new_message",
            "data": {
                "id": self.message_id,
                "role": "assistant",
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence": sequence,
                "pipeline_id": self.pipeline_id,
            },
        })
