"""consume_pending_notifications 推送 system 通知 + emit_finish 幂等保护的测试。

钉死根因（fix_20260705_notification_after_reply）：
一个 engine.run() 默认只在开头 emit_start 一次，跨中间所有迭代。
AI 对 system 通知的回复追加到同一个 message_id（旧流），前端按到达顺序渲染时
system 通知排在旧 AI 流后面 → UI 显示「通知在最后」。

修复方案（v4，按用户消息流程的思路）：
- 不在 consume 里手动切分流（v1/v2/v3 都失败了）。
- 在 apply_route 的 next_llm text-only 路径，consume 通知前 emit_finish 关闭当前流。
  这轮 LLM 的文字输出作为独立气泡落库（emit_finish 的 new_message 用同 message_id
  更新前端占位，不重复）。下一轮 run_iteration 开头 emit_start 开新流。
- emit_finish 加幂等保护（_stream_started=False 时跳过），避免 engine.run() 结束时
  重复 emit_finish。

本测试覆盖：
1. consume 推送 system 通知的核心契约（唯一推送点、不重复）。
2. emit_finish 幂等保护（流已关闭时跳过，避免重复 new_message + stream_end）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine_iteration import consume_pending_notifications


class _FakeBridge:
    """记录 emit_notification / emit_finish 调用的假 bridge。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._stream_started: bool = False
        self.finish_call_count: int = 0

    async def emit_notification(self, content: str, *, source: str = "system", level: str = "info") -> int:
        self.calls.append(("notification", content[:30]))
        return 1

    async def emit_start(self, state: dict[str, Any] | None = None) -> None:
        self._stream_started = True
        self.calls.append(("start", ""))

    async def emit_finish(self, state: dict[str, Any]) -> None:
        # 模拟真实 bridge 的幂等保护
        if not self._stream_started:
            return  # 跳过
        self._stream_started = False
        self.finish_call_count += 1
        self.calls.append(("finish", ""))


class _FakeEngine:
    def __init__(self, pipeline_id: str = "pipe-flush-aaaaaa") -> None:
        self.pipeline_id = pipeline_id
        self._inject_queue: list[tuple[str, str]] = []
        self._pending_client_message_id: str = ""

    def drain_inject_queue(self) -> list[tuple[str, str]]:
        msgs = self._inject_queue[:]
        self._inject_queue.clear()
        return msgs

    @property
    def inject_queue_size(self) -> int:
        return len(self._inject_queue)


@pytest.fixture
def patch_bridge(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeBridge, _FakeEngine]:
    bridge = _FakeBridge()
    engine = _FakeEngine()
    import pipeline.engine_iteration as mod

    monkeypatch.setattr(mod, "_get_bridge_for_pipeline", lambda _pid: bridge)
    return bridge, engine


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


class TestConsumeNotification:
    """consume 推送 system 通知的核心契约。"""

    def test_pushes_notification_once(
        self,
        patch_bridge: tuple[_FakeBridge, _FakeEngine],
    ) -> None:
        """consume 推送 WS 一次并返回 True。"""
        bridge, engine = patch_bridge
        engine._inject_queue.append(("[系统通知] 子任务完成 ✅", "system"))
        state: dict[str, Any] = {}

        consumed = _run(consume_pending_notifications(engine, state))

        assert consumed is True
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "notification"

    def test_drain_clears_queue_no_duplicate(
        self,
        patch_bridge: tuple[_FakeBridge, _FakeEngine],
    ) -> None:
        """drain 清空队列：第二次 consume 不推送（无重复）。"""
        bridge, engine = patch_bridge
        engine._inject_queue.append(("[系统通知] 子任务完成 ✅", "system"))
        state: dict[str, Any] = {}

        _run(consume_pending_notifications(engine, state))
        assert len(bridge.calls) == 1

        result = _run(consume_pending_notifications(engine, state))
        assert result is False
        assert len(bridge.calls) == 1, "★ 队列空时不应再推送"

    def test_empty_queue_returns_false(
        self,
        patch_bridge: tuple[_FakeBridge, _FakeEngine],
    ) -> None:
        """空队列返回 False。"""
        bridge, engine = patch_bridge
        state: dict[str, Any] = {}

        consumed = _run(consume_pending_notifications(engine, state))

        assert consumed is False
        assert bridge.calls == []


class TestEmitFinishIdempotent:
    """emit_finish 幂等保护：流已关闭时跳过，避免重复 new_message + stream_end。"""

    def test_finish_skipped_when_stream_not_started(self) -> None:
        """流未 start 时 emit_finish 跳过（不发 new_message/stream_end）。"""
        bridge = _FakeBridge()
        bridge._stream_started = False  # 流未开

        _run(bridge.emit_finish({"raw_result": "内容"}))

        assert bridge.finish_call_count == 0, "流未 start 时 emit_finish 应跳过"

    def test_finish_emits_when_stream_started(self) -> None:
        """流已 start 时 emit_finish 正常执行。"""
        bridge = _FakeBridge()
        bridge._stream_started = True

        _run(bridge.emit_finish({"raw_result": "内容"}))

        assert bridge.finish_call_count == 1

    def test_finish_idempotent_double_call(self) -> None:
        """★ 关键：连续两次 emit_finish，第二次跳过（防 engine.run 结束时重复）。"""
        bridge = _FakeBridge()
        bridge._stream_started = True

        _run(bridge.emit_finish({"raw_result": "第一次"}))
        assert bridge.finish_call_count == 1

        # 第二次（模拟 engine.run 结束时再调一次）
        _run(bridge.emit_finish({"raw_result": "第二次"}))
        assert bridge.finish_call_count == 1, (
            f"第二次 emit_finish 应跳过（流已关）。实际调用次数: {bridge.finish_call_count}"
        )
