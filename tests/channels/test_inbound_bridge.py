# @feature: FP-0.2.二 内部模块 manifest(通道) | @ci: python-coverage
"""渠道入站桥（channel_common/inbound_bridge）行为测试。

断言可观察行为：会话映射 create→inject、注入失败自愈、回复回流、失败通知。
fakes 仅落在内核能力边界（chat / pipeline-state capability）——那是本桥的
外部依赖；存储走 tmp_path 真实文件 I/O，消费者走真实协程循环。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_CC = str(Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "channel_common")
if _CC not in sys.path:
    sys.path.append(_CC)

from inbound_bridge import SYSTEM_USER_ID, ChannelInboundBridge, ConversationMappingStore  # noqa: E402
from output_adapter import BufferedChannelOutputAdapter  # noqa: E402

# ── 内核能力边界替身 ──────────────────────────────────────────────


class FakeChatCapability:
    """chat.send_message 替身：记录调用、按配置应答/失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.created: list[str] = []
        self.fail_inject_pipelines: set[str] = set()
        self.fail_all = False
        self.bad_create_response = False
        self._next = 1

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        assert method == "send_message"
        self.calls.append((method, dict(params)))
        if self.fail_all:
            raise RuntimeError("kernel unavailable")
        pid = str(params.get("pipeline_id", ""))
        if pid and pid in self.fail_inject_pipelines:
            raise RuntimeError("protocol error: pipeline session not found")
        if params.get("create"):
            if self.bad_create_response:
                return {"status": "created"}
            new_pid = f"{self._next:012x}"
            self._next += 1
            self.created.append(new_pid)
            return {"status": "created", "pipeline_id": new_pid}
        return {"status": "dispatched", "pipeline_id": pid}


class FakeStateCapability:
    """pipeline-state.list 替身：返回预置行并记录调用次数。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.list_calls = 0

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        assert method == "list"
        self.list_calls += 1
        return self.rows


class FakeInteractionCapability:
    """human-interaction 替身：挂起列表 + respond 记录。"""

    def __init__(self, pending: list[dict[str, Any]]) -> None:
        self.pending = pending
        self.respond_calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if method == "get_pending":
            return {"requests": self.pending}
        if method == "respond":
            self.respond_calls.append(dict(params))
            return True
        raise AssertionError(f"unexpected method {method}")


class FakeInputAdapter:
    """队列输入替身：enqueue 驱动真实消费者协程。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive(self) -> dict[str, Any]:
        return await self._queue.get()

    def enqueue(self, state: dict[str, Any]) -> None:
        self._queue.put_nowait(state)


class FakeOutputAdapter:
    def __init__(self) -> None:
        self.delivered: list[dict[str, Any]] = []

    async def deliver_to(
        self, target_user_id: str, text: str, reply_ctx: dict[str, Any] | None = None
    ) -> None:
        self.delivered.append({"target": target_user_id, "text": text, "reply_ctx": reply_ctx})


# ── 测试脚手架 ───────────────────────────────────────────────────


def _make_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "user_input": "你好",
        "_channel_user_id": "10001",
        "_conversation_key": "u10001",
        "_reply_target": "10001",
        "_reply_ctx": {"_message_type": "private"},
    }
    state.update(overrides)
    return {k: v for k, v in state.items() if v is not None}


def _make_bridge(
    chat: FakeChatCapability,
    state_cap: FakeStateCapability,
    output: FakeOutputAdapter,
    store_path: Path | None,
    interaction: FakeInteractionCapability | None = None,
    poll_interval: float = 15.0,
) -> tuple[ChannelInboundBridge, dict[str, Any]]:
    """构造桥并返回 (bridge, handles)——测试可经 handles 换装能力替身。"""
    handles: dict[str, Any] = {
        "chat": chat,
        "pipeline-state": state_cap,
        "human-interaction": interaction or FakeInteractionCapability([]),
    }
    bridge = ChannelInboundBridge(
        channel_id="channel_qq",
        input_adapter=FakeInputAdapter(),
        output_adapter=output,
        get_capability=lambda name: handles[name],
        store_path=store_path,
        interaction_poll_interval=poll_interval,
    )
    return bridge, handles


def _pending_record(
    thread: str = "thread-abc",
    options: list[dict[str, str]] | None = None,
    mode: str = "choice",
    rid: str = "req-1",
    questions: list[str] | None = None,
) -> dict[str, Any]:
    md: dict[str, Any] = {
        "interaction_mode": mode,
        "title": "危险操作审批",
        "description": "是否继续？",
        "thread_id": thread,
    }
    if options is not None or questions is None:
        md["options"] = (
            options if options is not None
            else [{"id": "1", "label": "批准"}, {"id": "2", "label": "拒绝"}]
        )
    if questions is not None:
        md["questions"] = questions
    return {"id": rid, "message_data": md}


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ── 会话映射：create → inject ────────────────────────────────────


@pytest.mark.asyncio
async def test_first_message_creates_and_second_injects_same_conversation(tmp_path: Path) -> None:
    """同会话两条消息：首条 create 建管道，次条注入同管道；映射持久化可重载。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "channels" / "channel_qq.json"
    bridge, _ = _make_bridge(chat, state_cap, output, store_path)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="第一条"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        bridge._input.enqueue(_make_state(user_input="第二条"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()

    _, params1 = chat.calls[0]
    _, params2 = chat.calls[1]
    pid = chat.created[0]
    assert params1["create"] is True
    assert "pipeline_id" not in params1
    assert params2.get("pipeline_id") == pid
    assert "create" not in params2
    # 注入契约：与前端同链路 + 合成用户锚 + 渠道溯源 state
    assert params1["user_id"] == SYSTEM_USER_ID
    assert params1["background"] is True
    assert params1["state"]["channel"] == {
        "type": "channel_qq",
        "conversation_key": "u10001",
        "sender": "10001",
    }
    # 消息文本原样进入会话（不做二次包装）
    assert params1["message"] == "第一条"
    assert params2["message"] == "第二条"
    # 映射持久化：全新 store 实例（模拟 sidecar 重启）按 pipeline 反查可得
    entry = ConversationMappingStore(store_path).find_by_pipeline(pid)
    assert entry is not None
    assert entry["reply_target"] == "10001"
    assert entry["reply_ctx"] == {"_message_type": "private"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_overrides", "expected_key"),
    [
        (
            {
                "_conversation_key": "g88",
                "_reply_target": "88",
                "_reply_ctx": {"_message_type": "group", "_group_id": 88},
            },
            "g88",
        ),
        ({"_conversation_key": "", "_channel_user_id": "20002"}, "u20002"),
    ],
)
async def test_conversation_keys_route_to_distinct_pipelines(
    tmp_path: Path, state_overrides: dict[str, Any], expected_key: str
) -> None:
    """不同会话键各建各的管道；缺会话键时回退按发送者建会话。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="a", _conversation_key="u10001"))  # noqa: SLF001
        bridge._input.enqueue(_make_state(user_input="b", **state_overrides))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()
    assert len(chat.created) == 2
    keys = {p["state"]["channel"]["conversation_key"] for _, p in chat.calls}
    assert keys == {"u10001", expected_key}


@pytest.mark.asyncio
async def test_inject_failure_falls_back_to_create_and_updates_mapping(tmp_path: Path) -> None:
    """注入失败（管道被清理）→ 降级 create 重建，映射回写新管道。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    dead_pid = "aaaaaaaaaaaa"
    ConversationMappingStore(store_path).put(
        "u10001", {"pipeline_id": dead_pid, "reply_target": "10001", "reply_ctx": {}}
    )
    chat.fail_inject_pipelines.add(dead_pid)
    bridge, _ = _make_bridge(chat, state_cap, output, store_path)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="还在吗"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()

    assert chat.calls[0][1].get("pipeline_id") == dead_pid
    assert "create" not in chat.calls[0][1]
    assert chat.calls[1][1].get("create") is True
    new_pid = chat.created[-1]
    assert new_pid != dead_pid
    entry = ConversationMappingStore(store_path).get("u10001")
    assert entry is not None
    assert entry["pipeline_id"] == new_pid


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", [{"user_input": ""}, {"user_input": "   "}, {"user_input": None}])
async def test_empty_input_is_skipped(tmp_path: Path, bad_state: dict[str, Any]) -> None:
    """空文本消息不派发（拒收语义在 adapter 侧已标记，桥不再转发）。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(**bad_state))  # noqa: SLF001
        await asyncio.sleep(0.05)
    finally:
        await bridge.stop()
    assert chat.calls == []


@pytest.mark.asyncio
async def test_consumer_survives_dispatch_errors(tmp_path: Path) -> None:
    """单条派发失败不得终止消费者（后续消息照常处理）。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    chat.fail_all = True
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="一"))  # noqa: SLF001
        bridge._input.enqueue(_make_state(user_input="二"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()
    assert [p["message"] for _, p in chat.calls] == ["一", "二"]


# ── 回复回流：域事件 → pipeline-state 读回 → 投递 ────────────────


@pytest.mark.asyncio
async def test_run_completed_delivers_reply_via_state_readback(tmp_path: Path) -> None:
    """run.completed：按映射反查 + raw_result 读回 → deliver_to 投递。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, handles = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        state_cap.rows = [{"pipeline_id": pid, "raw_result": "你好，我是 AgentOS"}]
        handles["pipeline-state"] = state_cap
        await bridge.handle_domain_event({"event": "run.completed", "pipeline_id": pid})
    finally:
        await bridge.stop()
    # list 调用 ≥1 = 回复读回查（另有派发后 thread 解析的一次，属正常开销）
    assert state_cap.list_calls >= 1
    assert output.delivered == [
        {"target": "10001", "text": "你好，我是 AgentOS", "reply_ctx": {"_message_type": "private"}}
    ]


@pytest.mark.asyncio
async def test_run_failed_notifies_without_state_readback(tmp_path: Path) -> None:
    """run.failed：失败文本取自事件 state 标签的 raw_error，零回查。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        await bridge.handle_domain_event({
            "event": "run.failed",
            "pipeline_id": pid,
            "state": {"raw_error": "LLM 连接超时"},
        })
    finally:
        await bridge.stop()
    # 失败通知的失败文本取自事件 state 标签（零回复读回查）；若存在的额外
    # list 调用是派发后 thread 解析，属另一职责，不破坏本语义。
    assert output.delivered[0]["text"] == "处理失败：LLM 连接超时"
    assert output.delivered[0]["target"] == "10001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_params",
    [
        {"event": "run.suspended", "pipeline_id": "ffffffffffff"},
        {"event": "run.completed", "pipeline_id": "ffffffffffff"},  # 非本渠道管道
        {"event": "run.completed"},  # 无 pipeline_id
    ],
)
async def test_unrelated_events_are_ignored(tmp_path: Path, event_params: dict[str, Any]) -> None:
    """非本渠道/非终态事件不投递不回查（内核广播全租户事件，桥必须过滤）。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.handle_domain_event(event_params)
    assert output.delivered == []
    assert state_cap.list_calls == 0


@pytest.mark.asyncio
async def test_reply_path_survives_sidecar_restart(tmp_path: Path) -> None:
    """sidecar 重启（内存索引丢失）后，事件仍经持久化映射完成回复投递。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    bridge, _ = _make_bridge(chat, state_cap, output, store_path)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
    finally:
        await bridge.stop()

    fresh_state = FakeStateCapability([{"pipeline_id": pid, "raw_result": "重启后依然回复"}])
    fresh_output = FakeOutputAdapter()
    fresh_bridge, _ = _make_bridge(chat, fresh_state, fresh_output, store_path)
    await fresh_bridge.handle_domain_event({"event": "run.completed", "pipeline_id": pid})
    assert fresh_output.delivered[0]["text"] == "重启后依然回复"
    assert fresh_output.delivered[0]["target"] == "10001"


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path: Path) -> None:
    """start 幂等：重复调用不叠加消费者任务（队列不双消费）。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        await asyncio.sleep(0.05)
    finally:
        await bridge.stop()
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_create_without_pipeline_id_raises(tmp_path: Path) -> None:
    """创建分支响应缺 pipeline_id = 协议错误：本条丢弃留痕、不写坏映射、消费者存活。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    chat.bad_create_response = True
    bridge, _ = _make_bridge(chat, state_cap, output, store_path)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
    finally:
        await bridge.stop()
    assert ConversationMappingStore(store_path).get("u10001") is None
    # 消费者未死：修好后下一条消息照常建会话
    chat.bad_create_response = False
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="再来"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()
    assert len(chat.created) == 1


@pytest.mark.asyncio
async def test_completed_event_without_reply_text_is_skipped(tmp_path: Path) -> None:
    """run.completed 但终态无 raw_result（如纯工具轮失败兜底）→ 不投递。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        state_cap.rows = [{"pipeline_id": pid, "raw_result": ""}]
        await bridge.handle_domain_event({"event": "run.completed", "pipeline_id": pid})
    finally:
        await bridge.stop()
    assert output.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",  # 响应形状异常
        [{"pipeline_id": "ffffffffffff", "raw_result": "别人的"}],  # 无匹配行
        [{"raw_result": 1}],  # 行缺 pipeline_id
    ],
)
async def test_fetch_reply_degrades_to_empty(tmp_path: Path, rows: Any) -> None:
    """pipeline-state 响应异常/无匹配 → 视为无回复，不投递不抛错。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    bridge, _ = _make_bridge(chat, state_cap, output, tmp_path / "m.json")
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        state_cap.rows = rows
        await bridge.handle_domain_event({"event": "run.completed", "pipeline_id": pid})
    finally:
        await bridge.stop()
    assert output.delivered == []


def test_store_write_failure_keeps_memory_state(tmp_path: Path) -> None:
    """映射文件不可写 → 告警且内存映射仍有效（降级不丢当前会话）。"""
    blocking = tmp_path / "dir-file"
    blocking.write_text("x", encoding="utf-8")  # 用普通文件当父目录 → mkdir/write 必失败
    store = ConversationMappingStore(blocking / "m.json")
    store.put("u1", {"pipeline_id": "a" * 12, "reply_target": "1", "reply_ctx": {}})
    assert store.get("u1") is not None


# ── 映射存储与输出投递入口 ───────────────────────────────────────


def test_store_roundtrip_and_corrupt_file_degrades(tmp_path: Path) -> None:
    """存储读写回环；坏文件按空映射启动（fail-safe 不炸装载）。"""
    path = tmp_path / "m.json"
    store = ConversationMappingStore(path)
    store.put("u1", {"pipeline_id": "a" * 12, "reply_target": "1", "reply_ctx": {"k": 1}})
    reloaded = ConversationMappingStore(path)
    assert reloaded.get("u1") == {
        "pipeline_id": "a" * 12,
        "reply_target": "1",
        "reply_ctx": {"k": 1},
    }
    assert reloaded.find_by_pipeline("a" * 12) is not None
    assert reloaded.find_by_pipeline("b" * 12) is None

    path.write_text("{not json", encoding="utf-8")
    broken = ConversationMappingStore(path)
    assert broken.get("u1") is None
    broken.put("u2", {"pipeline_id": "c" * 12, "reply_target": "2", "reply_ctx": {}})
    assert ConversationMappingStore(path).get("u2") is not None


def test_store_without_path_is_memory_only() -> None:
    pathless = ConversationMappingStore(None)
    pathless.put("u1", {"pipeline_id": "a" * 12, "reply_target": "1", "reply_ctx": {}})
    assert pathless.get("u1") is not None


class _RecordingOutput(BufferedChannelOutputAdapter):
    """记录 _deliver 入参的最小输出实现（测 deliver_to 公共契约）。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[Any, str, dict[str, Any]]] = []

    async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
        self.sent.append((target, text, state))


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_target", ["10001", "abc"])
async def test_deliver_to_resolves_target_and_forwards_ctx(raw_target: str) -> None:
    """deliver_to 走 _resolve_target 规范化（非数字目标跳过）并透传 reply_ctx。"""

    class _IntTarget(_RecordingOutput):
        def _resolve_target(self, raw_user_id: str) -> int | None:
            return int(raw_user_id) if raw_user_id.isdigit() else None

    out = _IntTarget()
    await out.deliver_to(raw_target, "hi", {"_message_type": "group", "_group_id": 88})
    if raw_target == "10001":
        assert out.sent == [(10001, "hi", {"_message_type": "group", "_group_id": 88})]
    else:
        assert out.sent == []


# ── 交互推 IM + IM 回复作为交互结果 ──────────────────────────────


@pytest.mark.asyncio
async def test_thread_id_resolved_after_create(tmp_path: Path) -> None:
    """首条 create 后经 pipeline-state 读回 thread_id 落入映射（轮询自愈补齐）。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, poll_interval=0.05)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        state_cap.rows = [{"pipeline_id": pid, "thread_id": "thread-abc"}]
        await _wait_until(
            lambda: (ConversationMappingStore(store_path).get("u10001") or {}).get("thread_id")
            == "thread-abc"
        )
    finally:
        await bridge.stop()
    entry = ConversationMappingStore(store_path).get("u10001")
    assert entry is not None
    assert entry["thread_id"] == "thread-abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["2", "拒绝", "先等等"])
async def test_inbound_reply_submitted_as_custom_reply(
    tmp_path: Path, reply: str
) -> None:
    """挂起交互存在时，IM 回文不做选项匹配、原样作为用户自定义回复提交。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([_pending_record()])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001",
         "reply_ctx": {"_message_type": "private"}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input=reply))  # noqa: SLF001
        await _wait_until(lambda: len(interaction.respond_calls) >= 1)
    finally:
        await bridge.stop()
    assert len(chat.calls) == 0  # 未投递管道
    call = interaction.respond_calls[0]
    assert call["request_id"] == "req-1"
    inner = call["resp_data"]["response"]
    assert inner["response_type"] == "answered"
    assert inner["feedback"] == reply


@pytest.mark.asyncio
async def test_inbound_free_text_becomes_feedback_when_no_option_matches(tmp_path: Path) -> None:
    """不匹配任何选项的文本 → 以 feedback 形式提交。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([_pending_record()])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="先等等，我确认一下细节"))  # noqa: SLF001
        await _wait_until(lambda: len(interaction.respond_calls) >= 1)
    finally:
        await bridge.stop()
    inner = interaction.respond_calls[0]["resp_data"]["response"]
    assert inner.get("selected_option") is None
    assert inner["feedback"] == "先等等，我确认一下细节"


@pytest.mark.asyncio
async def test_no_pending_interaction_dispatches_normally(tmp_path: Path) -> None:
    """无挂起交互 → 走正常管道派发，不碰交互能力。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
    finally:
        await bridge.stop()
    assert interaction.respond_calls == []
    assert chat.calls[0][1]["pipeline_id"] == "a" * 12


@pytest.mark.asyncio
async def test_poller_pushes_pending_interaction_once(tmp_path: Path) -> None:
    """轮询把本渠道会话的挂起交互推到 IM；同一请求不重推。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([_pending_record()])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001",
         "reply_ctx": {"_message_type": "private"}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(
        chat, state_cap, output, store_path, interaction=interaction, poll_interval=0.05
    )
    await bridge.start()
    try:
        await _wait_until(lambda: len(output.delivered) >= 1)
        await asyncio.sleep(0.15)  # 跨多个轮询周期
    finally:
        await bridge.stop()
    assert len(output.delivered) == 1
    text = output.delivered[0]["text"]
    assert "[需要你的输入]" in text
    assert "危险操作审批" in text
    assert "- 批准" in text
    assert "- 拒绝" in text
    assert output.delivered[0]["target"] == "10001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_kwargs", "should_push"),
    [
        ({"mode": "notification", "rid": "req-n"}, False),  # 非阻塞通知不进交互环
        ({"thread": "thread-other", "rid": "req-o"}, False),  # 别的会话的交互
        ({"mode": "choice", "rid": "req-c"}, True),
    ],
)
async def test_poller_filters_notifications_and_foreign_threads(
    tmp_path: Path, record_kwargs: dict[str, Any], should_push: bool
) -> None:
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([_pending_record(**record_kwargs)])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(
        chat, state_cap, output, store_path, interaction=interaction, poll_interval=0.05
    )
    await bridge.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await bridge.stop()
    assert (len(output.delivered) == 1) is should_push


# ── 交互环路防御分支 ─────────────────────────────────────────────


class _RaisingStateCapability:
    """list 恒抛异常（降级路径触发器）。"""

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        raise RuntimeError("state unavailable")


class _NonListStateCapability:
    """list 返回非列表（响应形状异常）。"""

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        return "garbage"


class _FlakyInteractionCapability(FakeInteractionCapability):
    """respond 可配置失败（降级为普通输入的路径）。"""

    def __init__(self, pending: list[dict[str, Any]], respond_ok: bool = True) -> None:
        super().__init__(pending)
        self.respond_ok = respond_ok

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if method == "respond":
            self.respond_calls.append(dict(params))
            return self.respond_ok
        return await super().call(method, params, timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("state_cls", [_RaisingStateCapability, _NonListStateCapability])
async def test_thread_resolution_degrades_on_state_failure(tmp_path: Path, state_cls: Any) -> None:
    """state 读失败/形状异常 → 解析静默降级（不抛、不写坏映射）。"""
    chat, state_cap, output = FakeChatCapability(), _NonListStateCapability(), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    bridge, handles = _make_bridge(chat, state_cap, output, store_path, poll_interval=0.05)
    handles["pipeline-state"] = state_cls()
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        await asyncio.sleep(0.15)  # 给轮询解析与异常路径执行机会
    finally:
        await bridge.stop()
    entry = ConversationMappingStore(store_path).get("u10001")
    assert entry is not None
    assert not entry.get("thread_id")


@pytest.mark.asyncio
async def test_poll_loop_survives_capability_errors(tmp_path: Path) -> None:
    """交互能力异常 → 轮询循环记日志继续（不终止）。"""

    class _BoomInteraction(FakeInteractionCapability):
        async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            raise RuntimeError("capability down")

    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(
        chat, state_cap, output, store_path,
        interaction=_BoomInteraction([]), poll_interval=0.05,
    )
    await bridge.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await bridge.stop()
    assert output.delivered == []


@pytest.mark.asyncio
async def test_poller_skips_non_dict_records(tmp_path: Path) -> None:
    """挂起列表混入非字典条目 → 跳过不炸。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability(["garbage", _pending_record(rid="req-ok")])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(
        chat, state_cap, output, store_path, interaction=interaction, poll_interval=0.05
    )
    await bridge.start()
    try:
        await _wait_until(lambda: len(output.delivered) >= 1)
    finally:
        await bridge.stop()
    assert output.delivered[0]["text"].startswith("[需要你的输入]")


@pytest.mark.asyncio
async def test_format_with_questions_and_bare_request(tmp_path: Path) -> None:
    """questions 型交互提示直接回复；两者皆无给兜底文案。"""
    bridge = ChannelInboundBridge(
        channel_id="c", input_adapter=None, output_adapter=None,
        get_capability=lambda _name: None, store_path=None,
    )  # noqa: SLF001 — 仅测格式化纯函数面
    with_q = bridge._format_interaction({  # noqa: SLF001
        "message_data": {"interaction_mode": "conversation", "title": "补充信息",
                          "thread_id": "t", "questions": ["目标平台是什么？"]}
    })
    assert "请直接回复：目标平台是什么？" in with_q
    bare = bridge._format_interaction({  # noqa: SLF001
        "message_data": {"interaction_mode": "choice", "title": "确认", "thread_id": "t"}
    })
    assert "请直接回复文字。" in bare


@pytest.mark.asyncio
async def test_inbound_text_answers_questions_style_interaction(tmp_path: Path) -> None:
    """questions 型（无选项）挂起 → 文本作为 answers 提交。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability([
        _pending_record(options=[], questions=["目标平台是什么？"], rid="req-q"),
    ])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="微信"))  # noqa: SLF001
        await _wait_until(lambda: len(interaction.respond_calls) >= 1)
    finally:
        await bridge.stop()
    inner = interaction.respond_calls[0]["resp_data"]["response"]
    assert inner.get("answers") == ["微信"]


@pytest.mark.asyncio
async def test_respond_failure_falls_back_to_normal_dispatch(tmp_path: Path) -> None:
    """应答提交失败（已超时/不存在）→ 本条消息转正常输入派发。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = _FlakyInteractionCapability([_pending_record()], respond_ok=False)
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="1"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
    finally:
        await bridge.stop()
    assert len(interaction.respond_calls) == 1
    assert chat.calls[0][1].get("pipeline_id") == "a" * 12


@pytest.mark.asyncio
async def test_dispatch_resolves_thread_from_preexisting_state(tmp_path: Path) -> None:
    """派发时 state 已含该管道行 → 派发后单管道解析路径落 thread_id。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    store_path = tmp_path / "m.json"
    bridge, _ = _make_bridge(chat, state_cap, output, store_path)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state())  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 1)
        pid = chat.created[-1]
        state_cap.rows = [{"pipeline_id": pid, "thread_id": "thread-fast"}]
        bridge._input.enqueue(_make_state(user_input="第二条"))  # noqa: SLF001
        await _wait_until(lambda: len(chat.calls) >= 2)
    finally:
        await bridge.stop()
    entry = ConversationMappingStore(store_path).get("u10001")
    assert entry is not None
    assert entry["thread_id"] == "thread-fast"


@pytest.mark.asyncio
async def test_try_respond_skips_non_dict_pending_records(tmp_path: Path) -> None:
    """挂起列表混入非字典条目 → 应答匹配跳过它、命中有效记录。"""
    chat, state_cap, output = FakeChatCapability(), FakeStateCapability([]), FakeOutputAdapter()
    interaction = FakeInteractionCapability(["garbage", _pending_record(rid="req-valid")])
    store_path = tmp_path / "m.json"
    ConversationMappingStore(store_path).put(
        "u10001",
        {"pipeline_id": "a" * 12, "reply_target": "10001", "reply_ctx": {}, "thread_id": "thread-abc"},
    )
    bridge, _ = _make_bridge(chat, state_cap, output, store_path, interaction=interaction)
    await bridge.start()
    try:
        bridge._input.enqueue(_make_state(user_input="批准"))  # noqa: SLF001
        await _wait_until(lambda: len(interaction.respond_calls) >= 1)
    finally:
        await bridge.stop()
    assert interaction.respond_calls[0]["request_id"] == "req-valid"
    assert chat.calls == []
