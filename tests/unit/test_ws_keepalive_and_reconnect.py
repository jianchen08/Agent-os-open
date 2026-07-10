"""WS 连接保活与断连重连恢复的回归测试。

BUG-FIX-fix_20260628_no_keepalive_disconnect / _reconnect_lost_when_thread_id_empty:
覆盖两个修复点：
1. 长时间无 chunk 时 engine 发 stream_keepalive（防前端误断连）。
2. _resume_pipeline_for_thread 在 entry.thread_id 为空时用 tags["session_id"]
   兜底匹配，并无条件重建 sink（防断连后输出永久接不回）。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix A：stream_keepalive 保活
# ---------------------------------------------------------------------------

class TestStreamKeepalive:
    """覆盖 engine._stream_keepalive_loop 的发/不发逻辑。"""

    @pytest.mark.asyncio
    async def test_emits_keepalive_when_idle_over_threshold(self) -> None:
        """静默超过阈值时发 stream_keepalive 事件。"""
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_keepalive_1", stop_check=lambda: False)
        bridge = MagicMock()
        bridge._stream_started = True
        bridge.output_sink.is_dead = False  # sink 未熔断，keepalive 正常发送
        bridge._make_event = lambda et, data: {"type": et, "data": data}
        sent: list[dict] = []

        async def _send(ev):
            sent.append(ev)
            return True

        bridge.send_event = _send
        streaming._bridge = bridge
        # 把阈值/间隔降到测试可接受范围
        streaming._KEEPALIVE_IDLE_THRESHOLD = 0.0  # 立即视为静默
        streaming._KEEPALIVE_INTERVAL = 0.01
        streaming._last_chunk_monotonic = time.monotonic() - 100  # 很久没 chunk

        task = asyncio.create_task(streaming._stream_keepalive_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib_suppress():
            await task

        keepalives = [e for e in sent if e["type"] == "stream_keepalive"]
        assert len(keepalives) >= 1, "静默超阈值时应发 stream_keepalive"
        # 契约：前端 handleStreamKeepalive 依赖 data.pipeline_id
        assert keepalives[0]["data"]["pipeline_id"] == "p_keepalive_1"

    @pytest.mark.asyncio
    async def test_suppressed_when_chunks_recent(self) -> None:
        """chunk 密集（最近收过）时抑制保活包。"""
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_keepalive_2", stop_check=lambda: False)
        bridge = MagicMock()
        bridge._stream_started = True
        bridge.output_sink.is_dead = False  # sink 未熔断
        bridge._make_event = lambda et, data: {"type": et, "data": data}
        sent: list[dict] = []

        async def _send(ev):
            sent.append(ev)
            return True

        bridge.send_event = _send
        streaming._bridge = bridge
        # 阈值设大，last_chunk 设为"刚刚"，确保 _idle < threshold
        streaming._KEEPALIVE_IDLE_THRESHOLD = 100.0
        streaming._KEEPALIVE_INTERVAL = 0.01
        streaming._last_chunk_monotonic = time.monotonic()  # 刚刚收过 chunk

        task = asyncio.create_task(streaming._stream_keepalive_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib_suppress():
            await task

        keepalives = [e for e in sent if e["type"] == "stream_keepalive"]
        assert keepalives == [], "chunk 密集时不应发保活包"

    @pytest.mark.asyncio
    async def test_not_sent_when_stream_not_started(self) -> None:
        """流式未开始/已结束（bridge._stream_started=False）时不发保活包。"""
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_keepalive_3", stop_check=lambda: False)
        bridge = MagicMock()
        bridge._stream_started = False  # 流式已结束
        bridge._make_event = lambda et, data: {"type": et, "data": data}
        sent: list[dict] = []

        async def _send(ev):
            sent.append(ev)
            return True

        bridge.send_event = _send
        streaming._bridge = bridge

        streaming._KEEPALIVE_IDLE_THRESHOLD = 0.0
        streaming._KEEPALIVE_INTERVAL = 0.01
        streaming._last_chunk_monotonic = time.monotonic() - 100

        task = asyncio.create_task(streaming._stream_keepalive_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib_suppress():
            await task

        assert sent == [], "bridge._stream_started=False 时不应发保活包"

    @pytest.mark.asyncio
    async def test_stops_when_sink_dead(self) -> None:
        """sink 熔断（is_dead=True，用户长期离线）时停止 keepalive，不发保活包。

        回归红线：验证死 sink 的 keepalive 循环真正退出（task done），
        而非被吞掉失败继续无限重试。fix: engine_streaming._stream_keepalive_loop
        在 is_dead 时 break，避免向已下线用户燃烧 CPU（日志曾见 4511 次失败）。
        """
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_keepalive_dead", stop_check=lambda: False)
        bridge = MagicMock()
        bridge._stream_started = True
        bridge.output_sink.is_dead = True  # sink 已熔断
        bridge.output_sink._thread_id = "tid_dead"
        bridge._make_event = lambda et, data: {"type": et, "data": data}
        sent: list[dict] = []

        async def _send(ev):
            sent.append(ev)
            return True

        bridge.send_event = _send
        streaming._bridge = bridge
        streaming._KEEPALIVE_IDLE_THRESHOLD = 0.0
        streaming._KEEPALIVE_INTERVAL = 0.01
        streaming._last_chunk_monotonic = time.monotonic() - 100

        task = asyncio.create_task(streaming._stream_keepalive_loop())
        # 给循环足够时间：若未 break，会发多个 keepalive；若 break 则 task 早结束
        await asyncio.sleep(0.1)
        # 回归核心：task 已因 break 正常结束（done），不是被外部 cancel
        assert task.done(), "死 sink 时 keepalive 应主动 break 退出"
        assert not task.cancelled(), "应是主动 break 而非 cancelled"
        keepalives = [e for e in sent if e["type"] == "stream_keepalive"]
        assert keepalives == [], "sink 熔断后不应发保活包"

    @pytest.mark.asyncio
    async def test_on_chunk_cooperative_stop_via_callback(self) -> None:
        """stop_check 回调返回 True 时，on_chunk raise CancelledError 协作式中断。

        验证 stop_check 注入的单向依赖：StreamingOutput 不依赖引擎内部状态，
        只通过回调判定停止。
        """
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_stop_1", stop_check=lambda: True)

        with pytest.raises(asyncio.CancelledError):
            streaming._on_chunk({"type": "text", "content": "x"})

    @pytest.mark.asyncio
    async def test_shutdown_cancels_keepalive_and_is_idempotent(self) -> None:
        """shutdown 取消 keepalive 协程且可重复调用、无任务时也不报错。"""
        from pipeline.engine_streaming import StreamingOutput

        streaming = StreamingOutput("p_shutdown_1", stop_check=lambda: False)
        # 无任务时 shutdown 不抛
        await streaming.shutdown()
        assert streaming._keepalive_task is None

        async def _loop():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        streaming._keepalive_task = asyncio.create_task(_loop())
        await streaming.shutdown()
        assert streaming._keepalive_task is None


# ---------------------------------------------------------------------------
# Fix C：断连重连恢复（entry.thread_id 为空时用 session_id 兜底匹配）
# ---------------------------------------------------------------------------

class TestResumePipelineOnReconnect:
    """覆盖 _resume_pipeline_for_thread 的匹配与 sink 重建。"""

    def _make_notifier(self) -> object:
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        # send_to_thread 走真实路径会找不到连接，这里 stub 成成功
        notifier.send_to_thread = AsyncMock(return_value=True)
        return notifier

    def _make_entry(self, *, thread_id: str, session_id: str,
                    running: bool, bridge_sink: object | None) -> object:
        from pipeline.pipeline_entry import PipelineEntry

        engine = MagicMock()
        engine.is_running = running
        engine.is_suspended = False

        bridge = MagicMock()
        bridge.output_sink = bridge_sink

        entry = PipelineEntry(
            engine=engine,
            bridge=bridge,
            thread_id=thread_id,
            tags={"session_id": session_id, "task_id": "t1"},
        )
        return entry

    def test_matches_via_session_id_when_thread_id_empty(self, monkeypatch) -> None:
        """entry.thread_id 为空时，用 tags['session_id'] 匹配并重建 sink。"""
        import pipeline.registry as registry_mod
        import pipeline.stream_bridge as stream_bridge_mod

        notifier = self._make_notifier()
        entry = self._make_entry(
            thread_id="",  # ← 关键：thread_id 为空（日志里的 no-thread 场景）
            session_id="sess_xyz",
            running=True,
            bridge_sink=MagicMock(),  # 旧 sink
        )

        fake_registry = MagicMock()
        fake_registry._engines = {"p_resume_1": entry}
        # 函数内 from pipeline.registry import get_engine_registry，patch 该模块属性
        monkeypatch.setattr(
            registry_mod, "get_engine_registry", lambda: fake_registry, raising=False,
        )
        # create_targeted_sink 在函数内 from ... import 局部绑定，patch 模块属性
        new_sink = MagicMock()
        monkeypatch.setattr(
            stream_bridge_mod, "create_targeted_sink",
            lambda notifier_, tid: new_sink if tid == "sess_xyz" else None,
            raising=False,
        )

        notifier._resume_pipeline_for_thread("sess_xyz")

        # 断言：旧 sink 被新 sink 替换，entry.thread_id 被补全
        assert entry.bridge.output_sink is new_sink
        assert entry.thread_id == "sess_xyz"

    def test_replaces_sink_unconditionally_not_wait_for_dead(self, monkeypatch) -> None:
        """重连即接管：旧 sink 未达 dead 阈值也应重建（不等连续失败 5 次）。"""
        import pipeline.registry as registry_mod
        import pipeline.stream_bridge as stream_bridge_mod

        notifier = self._make_notifier()
        old_sink = MagicMock()
        old_sink.is_dead = False  # ← 未达 dead 阈值
        entry = self._make_entry(
            thread_id="tid_running",
            session_id="tid_running",
            running=True,
            bridge_sink=old_sink,
        )

        fake_registry = MagicMock()
        fake_registry._engines = {"p_resume_2": entry}
        monkeypatch.setattr(
            registry_mod, "get_engine_registry", lambda: fake_registry, raising=False,
        )
        new_sink = MagicMock()
        monkeypatch.setattr(
            stream_bridge_mod, "create_targeted_sink",
            lambda notifier_, tid: new_sink,
            raising=False,
        )

        notifier._resume_pipeline_for_thread("tid_running")

        assert entry.bridge.output_sink is new_sink, "即使未 dead 也应重建 sink"

    def test_resumes_all_matching_not_just_first(self, monkeypatch) -> None:
        """同 session 下多个活跃 pipeline 应全部恢复，而非只恢复第一个。"""
        import pipeline.registry as registry_mod
        import pipeline.stream_bridge as stream_bridge_mod

        notifier = self._make_notifier()
        entry_a = self._make_entry(
            thread_id="tid_multi", session_id="tid_multi",
            running=True, bridge_sink=MagicMock(),
        )
        entry_b = self._make_entry(
            thread_id="tid_multi", session_id="tid_multi",
            running=True, bridge_sink=MagicMock(),
        )

        fake_registry = MagicMock()
        fake_registry._engines = {"pa": entry_a, "pb": entry_b}
        monkeypatch.setattr(
            registry_mod, "get_engine_registry", lambda: fake_registry, raising=False,
        )
        sinks = iter([MagicMock(), MagicMock()])
        monkeypatch.setattr(
            stream_bridge_mod, "create_targeted_sink",
            lambda notifier_, tid: next(sinks),
            raising=False,
        )

        notifier._resume_pipeline_for_thread("tid_multi")

        assert entry_a.bridge.output_sink is not None
        assert entry_b.bridge.output_sink is not None
        assert entry_a.bridge.output_sink is not entry_b.bridge.output_sink

    def test_skips_idle_engine(self, monkeypatch) -> None:
        """非 running/suspended 的引擎不应被恢复（避免无谓 sink 替换）。"""
        import pipeline.registry as registry_mod
        import pipeline.stream_bridge as stream_bridge_mod

        notifier = self._make_notifier()
        old_sink = MagicMock()
        entry = self._make_entry(
            thread_id="tid_idle", session_id="tid_idle",
            running=False,  # ← 空闲
            bridge_sink=old_sink,
        )

        fake_registry = MagicMock()
        fake_registry._engines = {"p_idle": entry}
        monkeypatch.setattr(
            registry_mod, "get_engine_registry", lambda: fake_registry, raising=False,
        )
        created: list = []
        monkeypatch.setattr(
            stream_bridge_mod, "create_targeted_sink",
            lambda notifier_, tid: created.append(tid) or MagicMock(),
            raising=False,
        )

        notifier._resume_pipeline_for_thread("tid_idle")

        assert entry.bridge.output_sink is old_sink, "空闲引擎的 sink 不应被替换"
        assert created == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def contextlib_suppress():
    """contextlib.suppress 的薄封装，避免在每个测试 import。"""
    import contextlib
    return contextlib.suppress(asyncio.CancelledError, Exception)
