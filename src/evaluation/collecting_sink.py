"""评估专用收集型 sink。

评估子管道是"一次性阻塞取结果"场景：外部（EvaluationEngine）需要一个能
await 到 evaluator_agent 输出 raw_result 的句柄，而不是 fire-and-forget 推到
WebSocket。本 sink 实现该契约：

- 挂载到评估管道的 bridge（经 ensure_bridge(output_sink=self)），
  与 WebSocket 的 TargetedSink 走同一套 IOutputSink 协议（pipeline/sink.py:32）。
- 捕获 bridge 的 emit_finish / emit_error 终止事件（stream_end / stream_error），
  在事件到达时 resolve 一个 future，供评估侧 await。
- 从事件 data.raw_result（new_message.data.content / stream_end.data.full_content）
  提取 evaluator 的最终输出文本。

设计约束（对齐项目 I1~I4 不变量）：
- 评估侧不持有 engine 引用、不调 engine.run；只 register + send + await sink。
- 本 sink 是 IOutputSink 的一个消费者实现，不依赖任何 pipeline 私有成员。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class CollectingSink:
    """收集 evaluator_agent 流式输出、暴露 awaitable 结果的 sink。

    生命周期：
    1. send 路径调 ensure_bridge(output_sink=self) 时被灌入 bridge
    2. bridge 的 emit_finish 推 stream_end（含 full_content）→ resolve result
    3. bridge 的 emit_error 推 stream_error → resolve with error
    4. 评估侧 await self.result() 拿到 (raw_result, error) 元组后消费

    Args:
        pipeline_id: 关联的评估管道 ID，用于日志。
    """

    def __init__(self, pipeline_id: str = "") -> None:
        self._pipeline_id = pipeline_id
        # raw_result 累积：优先用终止事件的完整内容；为兼容异常截断，也累加流式 chunk。
        self._content_parts: list[str] = []
        self._final_content: str | None = None
        self._error: str | None = None
        # future 懒创建：CollectingSink 可能在无事件循环时被实例化，
        # 而 send_event / result 一定在异步上下文里调用。
        self._done: asyncio.Future[None] | None = None

    def _get_done(self) -> asyncio.Future[None]:
        if self._done is None:
            self._done = asyncio.get_running_loop().create_future()
        return self._done

    @property
    def sink_id(self) -> str:
        return f"collecting:{self._pipeline_id[:12] or 'no-pipeline'}"

    async def send_event(self, event: dict) -> bool:
        """IOutputSink 协议实现：消费 bridge 推来的事件。

        只关心三类：
        - new_message / stream_end：取 full_content / content 作为最终输出
        - stream_chunk：增量累积（兜底，正常路径下终止事件已带完整内容）
        - stream_error：终止并置 error
        """
        if not isinstance(event, dict):
            return False
        event_type = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}

        try:
            if event_type in ("new_message", "stream_end"):
                content = data.get("full_content") or data.get("content") or ""
                if content:
                    self._final_content = content
                # stream_end = 本轮流式终止信号，resolve future
                if event_type == "stream_end":
                    self._resolve_done()
            elif event_type == "stream_chunk":
                chunk = data.get("content", "")
                if chunk:
                    self._content_parts.append(chunk)
            elif event_type == "stream_error":
                self._error = data.get("error") or "评估管道执行失败（stream_error）"
                self._resolve_done()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CollectingSink] 事件处理异常 pipeline=%s type=%s err=%s",
                self._pipeline_id[:12],
                event_type,
                exc,
            )
            return False
        return True

    def _resolve_done(self) -> None:
        """单次 resolve；后续重复终止事件被幂等忽略。

        send_event 一定在异步上下文里被调用（bridge 推事件），故此处
        _get_done() 能安全取到 running loop 创建的 future。
        """
        done = self._get_done()
        if not done.done():
            done.set_result(None)

    async def result(self, *, timeout: float | None = None) -> tuple[str | None, str | None]:
        """await 到评估管道终止，返回 (raw_result, error)。

        raw_result 取终止事件的完整内容；若终止事件未带内容（异常截断），
        回退到流式 chunk 累积值。

        Args:
            timeout: 最大等待秒数；None 表示不限（依赖评估 agent 自身 max_iterations 兜底）。

        Returns:
            (raw_result, error)：正常结束 error=None；出错 raw_result 可能仍为 chunk 累积值。
        """
        if timeout is not None:
            await asyncio.wait_for(asyncio.shield(self._get_done()), timeout=timeout)
        else:
            await self._get_done()
        raw = self._final_content
        if raw is None and self._content_parts:
            raw = "".join(self._content_parts)
        return raw, self._error
