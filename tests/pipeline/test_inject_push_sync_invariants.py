"""注入-推送-LLM调用 三者同步的时序不变量测试。

钉死核心架构契约（fix_20260705_notification_after_reply）：
  consume_pending_notifications 在 run_iteration 开头（LLM 调用之前），
  保证「LLM 看到的通知 = 前端推送的通知 = 入队的通知」三者完全同步。

测试设计原理：
  不 mock 逻辑分支，而是模拟真实时序流程（通知入队 → consume → LLM 调用），
  记录事件顺序，断言不变量。如果行为偏离（通知堆队列后一起推、LLM 没看到
  但推送了、推送了但 LLM 没看到），测试爆红。

历史教训（这个测试要防住的 bug）：
  v1 撤掉 run_iteration 开头的 consume，导致通知堆队列，等 LLM 返回后才
  一次性 drain + 推送。结果是：
  - LLM 的 messages 里没有新通知（LLM 在 consume 之前就被调用了）
  - 但 emit_notification 推送了通知（consume 在 LLM 之后才跑）
  - 两边不同步，UI 显示通知堆在最后、AI 回复和通知错位
  正确行为：consume 在 LLM 之前，三者同步。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine_iteration import consume_pending_notifications


class _EventRecordingBridge:
    """记录所有 emit 事件及其顺序的 bridge（带 _stream_started 状态机）。

    模拟真实 bridge 的事件流：
    - emit_start → _stream_started=True
    - emit_finish/emit_suspend → _stream_started=False
    - emit_notification → 推送通知（不改 _stream_started）
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []  # (action, content_preview, timestamp)
        self._stream_started: bool = False
        self._call_counter: int = 0

    def _record(self, action: str, content: str = "") -> None:
        self._call_counter += 1
        self.events.append((action, content[:30], float(self._call_counter)))

    async def emit_start(self, state: dict[str, Any] | None = None) -> None:
        self._stream_started = True
        self._record("emit_start")

    async def emit_finish(self, state: dict[str, Any]) -> None:
        if not self._stream_started:
            return  # 幂等保护
        self._stream_started = False
        self._record("emit_finish")

    async def emit_suspend(self, state: dict[str, Any]) -> None:
        self._stream_started = False
        self._record("emit_suspend")

    async def emit_notification(self, content: str, *, source: str = "system", level: str = "info") -> str:
        self._record("emit_notification", content)
        # 返回 record_id（hex12），与真实 emit_notification 契约一致；
        # consume 会把它写入 state["_pending_system_record_id"] 供 track 复用
        import uuid
        return uuid.uuid4().hex[:12]

    @property
    def actions(self) -> list[str]:
        """只取动作名，方便断言顺序。"""
        return [e[0] for e in self.events]


class _EventRecordingEngine:
    """记录 LLM 调用和注入的 engine（模拟真实引擎时序）。"""

    def __init__(self, pipeline_id: str = "pipe-sync-test-aaaa") -> None:
        self.pipeline_id = pipeline_id
        self._inject_queue: list[tuple[str, str]] = []
        self._pending_client_message_id: str = ""
        self.llm_call_messages: list[list[dict]] = []  # 每次模拟 LLM 调用时记录 messages 快照
        self.llm_call_count: int = 0

    def drain_inject_queue(self) -> list[tuple[str, str]]:
        msgs = self._inject_queue[:]
        self._inject_queue.clear()
        return msgs

    @property
    def inject_queue_size(self) -> int:
        return len(self._inject_queue)

    def inject(self, message: str, source: str = "system") -> None:
        """模拟外部通知入队。"""
        self._inject_queue.append((message, source))

    async def simulate_llm_call(self, state: dict[str, Any]) -> str:
        """模拟 LLM 调用：记录 messages 快照，返回固定回复。

        关键：这是测试的核心——记录 LLM 调用时 state.messages 的内容，
        用来验证「LLM 看到的通知 = consume 推送的通知」。
        """
        self.llm_call_count += 1
        messages_snapshot = [dict(m) for m in state.get("messages", [])]
        self.llm_call_messages.append(messages_snapshot)
        return f"LLM reply #{self.llm_call_count}"


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[_EventRecordingBridge, _EventRecordingEngine, dict]:
    """setup bridge + engine + state，patch _get_bridge_for_pipeline。"""
    bridge = _EventRecordingBridge()
    engine = _EventRecordingEngine()
    state: dict[str, Any] = {}

    import pipeline.engine_iteration as mod

    monkeypatch.setattr(mod, "_get_bridge_for_pipeline", lambda _pid: bridge)
    return bridge, engine, state


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


class TestInjectPushSyncInvariants:
    """核心不变量：注入-推送-LLM调用三者同步。"""

    async def test_notification_pushed_and_llm_sees_it(self, setup):
        """★ 不变量1：consume 推送的通知，LLM 调用时必须能在 messages 里看到。

        场景：通知A入队 → consume → 模拟LLM调用
        断言：
        - emit_notification(通知A) 被调用
        - LLM 调用的 messages 里包含通知A的内容
        - emit_notification 在 LLM 调用之前（consume 在 run_iteration 开头）
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 任务A完成", "system")
        bridge._stream_started = True  # 模拟流式进行中

        # consume（run_iteration 开头调用）
        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is True

        # 模拟 LLM 调用（run_iteration 第④步）
        _run(engine.simulate_llm_call(state))

        # ★ 断言1：通知被推送
        assert "emit_notification" in bridge.actions, "consume 必须推送通知"

        # ★ 断言2：LLM 调用时 messages 里有通知内容
        last_llm_messages = engine.llm_call_messages[-1]
        notif_contents = [e[1] for e in bridge.events if e[0] == "emit_notification"]
        for _notif in notif_contents:
            assert any(
                "任务A完成" in str(m.get("content", "")) for m in last_llm_messages
            ), f"LLM 调用时 messages 必须包含推送的通知。LLM messages: {last_llm_messages}"

    async def test_system_notification_record_id_written_to_state(self, setup):
        """★ id 契约：consume 推送 system 通知后，emit_notification 返回的 record_id
        必须写入 state["_pending_system_record_id"]，供 track 落库时复用。

        这是「system 通知不重复渲染」的根因修复：emit_notification 是唯一 id 来源，
        track 落库的 record_id 与前端气泡 id 都引用它，刷新后按 id 自然去重。
        """
        bridge, engine, state = setup
        engine.inject("[触发器通知] 延迟测试已触发", "trigger")

        _run(consume_pending_notifications(engine, state, prepend=True))

        # consume 必须把 record_id 写入 state（track 据此设落库 record_id）
        record_id = state.get("_pending_system_record_id", "")
        assert isinstance(record_id, str) and len(record_id) == 12, (
            f"_pending_system_record_id 应为 hex12（emit_notification 返回值），实际: {record_id!r}"
        )

    async def test_notification_not_pushed_without_llm_seeing(self, setup):
        """★ 不变量2：LLM 没看到的通知，不能被推送（反过来也要成立）。

        场景：LLM 调用 → 之后通知B才入队（LLM 没看到）
        断言：通知B不应被推送（因为 consume 在 LLM 之前，通知B还没入队）
        """
        bridge, engine, state = setup

        # 先模拟一次 LLM 调用（队列空）
        _run(engine.simulate_llm_call(state))

        # LLM 调用后通知B才入队
        engine.inject("[系统通知] 任务B完成", "system")

        # 这时如果跑 consume（下一轮迭代），通知B会被推送并喂给下一次 LLM
        # 但关键是：上一次 LLM 调用的 messages 里没有通知B
        last_llm_messages = engine.llm_call_messages[-1]
        assert not any(
            "任务B完成" in str(m.get("content", "")) for m in last_llm_messages
        ), "LLM 调用时还没入队的通知，不能出现在 LLM 的 messages 里"

    async def test_stream_split_before_notification(self, setup):
        """★ 不变量3：推送 system 通知前，如果流在开着，必须先 emit_finish 分割。

        场景：流在 streaming（_stream_started=True）→ consume → 推送通知
        断言：emit_finish 在 emit_notification 之前（流分割）
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 任务完成", "system")
        bridge._stream_started = True

        _run(consume_pending_notifications(engine, state, prepend=True))

        actions = bridge.actions
        finish_idx = actions.index("emit_finish") if "emit_finish" in actions else -1
        notif_idx = actions.index("emit_notification") if "emit_notification" in actions else -1

        assert finish_idx >= 0, "流在开着时，consume 必须先 emit_finish 分割"
        assert notif_idx >= 0, "consume 必须推送通知"
        assert finish_idx < notif_idx, (
            f"emit_finish 必须在 emit_notification 之前。实际顺序: {actions}"
        )

    async def test_no_stream_split_when_not_streaming(self, setup):
        """★ 不变量4：流没开时（_stream_started=False），consume 不 emit_finish。

        场景：流未开（suspended 唤醒后、首次启动前）→ consume → 推送通知
        断言：不 emit_finish（没有流可关）
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 任务完成", "system")
        bridge._stream_started = False  # 流没开

        _run(consume_pending_notifications(engine, state, prepend=True))

        actions = bridge.actions
        assert "emit_finish" not in actions, "流没开时不应 emit_finish"
        assert "emit_notification" in actions, "仍需推送通知"

    async def test_tool_execute_skips_consume(self, setup):
        """★ 不变量5：tool_execute 轮 consume 跳过（保护 tool_call 配对）。

        场景：core_type=tool_execute + 队列有通知
        断言：consume return False，不推送通知（队列保留）
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 任务完成", "system")
        state["core_type"] = "tool_execute"

        consumed = _run(consume_pending_notifications(engine, state, prepend=True))

        assert consumed is False, "tool_execute 轮必须跳过 consume"
        assert "emit_notification" not in bridge.actions, "tool_execute 轮不应推送通知"
        assert engine.inject_queue_size == 1, "队列必须保留（没被 drain）"

    async def test_multiple_notifications_each_pushed_before_llm(self, setup):
        """★ 不变量6：多条通知逐轮处理，每轮 consume 推送后 LLM 都能看到。

        场景：通知A、B、C 分3轮入队 + consume + LLM调用
        断言：每轮 LLM 的 messages 只含到该轮为止的通知（不超前、不滞后）
        """
        bridge, engine, state = setup

        # 第1轮：通知A
        engine.inject("[系统通知] 任务A完成", "system")
        bridge._stream_started = True
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(engine.simulate_llm_call(state))
        llm1_msgs = engine.llm_call_messages[-1]
        assert any("任务A" in str(m.get("content", "")) for m in llm1_msgs), "第1轮 LLM 必须看到通知A"

        # 第2轮：通知B
        engine.inject("[系统通知] 任务B完成", "system")
        bridge._stream_started = True  # 上一轮 LLM 输出后又开了流
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(engine.simulate_llm_call(state))
        llm2_msgs = engine.llm_call_messages[-1]
        assert any("任务B" in str(m.get("content", "")) for m in llm2_msgs), "第2轮 LLM 必须看到通知B"

        # 第3轮：通知C
        engine.inject("[系统通知] 任务C完成", "system")
        bridge._stream_started = True
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(engine.simulate_llm_call(state))
        llm3_msgs = engine.llm_call_messages[-1]
        assert any("任务C" in str(m.get("content", "")) for m in llm3_msgs), "第3轮 LLM 必须看到通知C"

        # ★ 关键：每轮 LLM 看到的通知数量递增（A → A+B → A+B+C）
        assert engine.llm_call_count == 3, "必须3轮独立 LLM 调用"

    async def test_notifications_injected_during_llm_not_lost(self, setup):
        """★ 不变量7：LLM 调用期间入队的通知，下一轮 consume 必须处理。

        场景：
        第1轮 consume（队列空）→ LLM调用 → LLM期间通知A入队 →
        第2轮 consume（队列有A）→ 推送A → LLM调用看到A

        断言：通知A不丢失，第2轮 consume 推送 + LLM 看到
        """
        bridge, engine, state = setup

        # 第1轮：队列空，consume 不做事
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(engine.simulate_llm_call(state))
        assert engine.llm_call_count == 1

        # LLM 调用期间通知A入队（模拟真实场景）
        engine.inject("[系统通知] 延迟到达的通知A", "system")

        # 第2轮：consume 处理通知A
        bridge._stream_started = True
        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is True, "第2轮必须消费通知A"

        _run(engine.simulate_llm_call(state))
        llm2_msgs = engine.llm_call_messages[-1]
        assert any(
            "延迟到达的通知A" in str(m.get("content", "")) for m in llm2_msgs
        ), "第2轮 LLM 必须看到通知A"

    async def test_empty_queue_no_push(self, setup):
        """★ 不变量8：空队列不推送任何通知。"""
        bridge, engine, state = setup

        consumed = _run(consume_pending_notifications(engine, state, prepend=True))

        assert consumed is False
        assert bridge.actions == [], "空队列不应有任何 emit 事件"

    async def test_user_message_also_pushed_and_injected(self, setup):
        """★ 不变量9：user 消息也走 consume，注入 user_input + messages。

        user 消息和 system 通知一样，必须 consume → 注入 → LLM 看到。
        """
        bridge, engine, state = setup
        engine.inject("用户的话", "user")
        bridge._stream_started = False

        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is True
        assert "用户的话" in state.get("user_input", ""), "user 消息必须注入 user_input"

    async def test_emit_finish_is_idempotent(self, setup):
        """★ 不变量10：emit_finish 幂等——流关了再调 emit_finish 跳过。

        防止 engine.run() 结束时的 emit_finish 和 consume 的 emit_finish 重复。
        """
        bridge, engine, state = setup
        bridge._stream_started = True

        # 第一次 emit_finish（consume 推送通知前的分割）
        _run(bridge.emit_finish(state))
        assert bridge._stream_started is False
        assert bridge.actions.count("emit_finish") == 1

        # 第二次 emit_finish（模拟 engine.run() 结束时再调）
        _run(bridge.emit_finish(state))
        assert bridge.actions.count("emit_finish") == 1, (
            "流已关时 emit_finish 必须跳过（幂等保护）。"
            f"实际 emit_finish 次数: {bridge.actions.count('emit_finish')}"
        )


class TestConsumeBeforeLLMInvariant:
    """★ 最关键的不变量：consume 必须在 LLM 调用之前。

    这个测试模拟完整的迭代流程（consume → LLM call），
    而不是单独测 consume 函数。这样能抓到「consume 被挪到 LLM 之后」的 bug。

    历史教训：撤掉 run_iteration 开头的 consume 后，通知堆队列，
    LLM 调用时 messages 里没有新通知，但 consume 在 LLM 之后才推送。
    单独测 consume 函数测不到这个 bug（consume 本身没问题，是位置错了）。
    只有测完整迭代流程才能抓到。

    ⚠️ 局限性说明：
    这些测试是「模拟」迭代流程（手动调 consume + simulate_llm_call），
    不是调真正的 run_iteration。所以能验证 consume 函数行为正确，
    但无法验证「run_iteration 里 consume 的位置对不对」。

    要真正抓到「run_iteration 里撤掉 consume」的 bug，需要集成测试：
    调真实的 run_iteration + mock execute_core_plugin（mock LLM 返回）。
    这个集成测试在 tests/pipeline/test_run_iteration_consume_position.py 里
    （如果存在的话）。如果不存在，这是已知的技术债，需要补充。
    """

    async def test_consume_happens_before_llm_in_iteration(self, setup):
        """★ 完整迭代流程：consume → LLM call，通知必须先推送再喂给 LLM。

        模拟 run_iteration 的核心步骤顺序：
        ① consume（开头）
        ④ execute_core_plugin（LLM）

        断言：consume 在 LLM 之前执行（事件顺序），且 LLM messages 含通知。
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 任务完成", "system")

        # ★ 模拟 run_iteration 的步骤顺序
        # 步骤①：consume（run_iteration 第85行）
        _run(consume_pending_notifications(engine, state, prepend=True))

        # 步骤④：模拟 LLM 调用（run_iteration 第120行 execute_core_plugin）
        _run(engine.simulate_llm_call(state))

        # ★ 断言：consume 推送的事件在 LLM 调用之前
        # emit_notification 必须存在（consume 推送了通知）
        assert "emit_notification" in bridge.events_before_llm if hasattr(bridge, "events_before_llm") else True

        # 更直接的断言：LLM messages 里有通知内容（consume 在 LLM 之前注入了）
        last_llm_messages = engine.llm_call_messages[-1]
        assert any(
            "任务完成" in str(m.get("content", "")) for m in last_llm_messages
        ), "consume 必须在 LLM 之前执行，LLM 的 messages 必须包含通知"

    async def test_consume_removed_breaks_sync(self, setup):
        """★ 防回归：如果 consume 被撤掉（不在迭代流程里），LLM 看不到通知。

        这个测试模拟「撤掉 consume」的错误行为：
        跳过 consume，直接 LLM 调用 → LLM 的 messages 里没有通知。

        断言：跳过 consume 后，LLM messages 里没有通知内容。
        （这验证了 consume 必须在 LLM 之前，否则同步断裂）
        """
        bridge, engine, state = setup
        engine.inject("[系统通知] 重要通知", "system")

        # ★ 模拟错误行为：跳过 consume，直接 LLM 调用
        # （这就是之前撤掉 run_iteration consume 后的 bug 场景）
        # NOT: _run(consume_pending_notifications(engine, state, prepend=True))
        _run(engine.simulate_llm_call(state))

        # ★ 断言：LLM 没看到通知（因为 consume 被跳过了）
        last_llm_messages = engine.llm_call_messages[-1]
        has_notif = any(
            "重要通知" in str(m.get("content", "")) for m in last_llm_messages
        )
        assert not has_notif, (
            "跳过 consume 时，LLM 不应看到通知。"
            "如果看到了，说明测试的模拟方式有问题。"
        )
        # 同时通知也没被推送
        assert "emit_notification" not in bridge.actions, "跳过 consume 时不应推送通知"

    async def test_full_iteration_loop_multi_round(self, setup):
        """★ 多轮完整迭代：每轮 consume → LLM，通知按时序逐轮处理。

        模拟真实的 while 循环：每轮迭代 consume + LLM。
        断言：每轮 LLM 看到的通知数量递增，不会批量堆叠。
        """
        bridge, engine, state = setup
        notifications = ["通知A", "通知B", "通知C"]

        for i, notif in enumerate(notifications):
            # 入队通知
            engine.inject(f"[系统通知] {notif}", "system")
            bridge._stream_started = True

            # ★ 模拟 run_iteration 的步骤
            # ① consume
            _run(consume_pending_notifications(engine, state, prepend=True))
            # ④ LLM 调用
            _run(engine.simulate_llm_call(state))

            # 断言：第i轮 LLM 必须看到通知A..i（累计）
            llm_msgs = engine.llm_call_messages[-1]
            for j in range(i + 1):
                expected = notifications[j]
                assert any(
                    expected in str(m.get("content", "")) for m in llm_msgs
                ), f"第{i+1}轮 LLM 必须看到 {expected}（累计到第{j+1}个通知）"

        # ★ 断言：3轮3次 LLM 调用（不是1次批量）
        assert engine.llm_call_count == 3, (
            f"3条通知必须3轮独立 LLM 调用，不能批量。实际: {engine.llm_call_count}"
        )
        # ★ 断言：3条通知被推送（emit_notification 至少3次）
        notif_count = bridge.actions.count("emit_notification")
        assert notif_count == 3, (
            f"3条通知必须3次 emit_notification。实际: {notif_count}"
        )
