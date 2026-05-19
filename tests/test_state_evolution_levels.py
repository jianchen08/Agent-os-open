"""
状态演变测试体系（AC-7）— 覆盖4个运行状态级别。

Level 1: 初始状态 — 系统刚启动，无任何数据
Level 2: 轻量状态 — 1-5个会话，每个会话少量消息
Level 3: 中等状态 — 20-50个会话，部分会话有较长历史
Level 4: 重度状态 — 100+会话、多Agent并发、大量消息历史

每个级别覆盖：
- WebSocket 连接建立和心跳
- 消息发送和接收（含流式）
- 会话列表加载和分页
- 消息历史分页和加载
- Minimax role 转换（管道层）
- 消息格式统一（reasoning/thinking 独立渲染）
- Worktree 自动清理
- 错误处理和恢复
"""
import asyncio
import time

import pytest

from src.conversation.manager import ConversationManager, Message
from src.errors import (
    AppError,
    ConnectionError_,
    ErrorRecovery,
    MessageValidationError,
    RetryPolicy,
    SessionNotFoundError,
)
from src.pipeline.minimax import (
    ensure_alternating_roles,
    normalize_messages_for_minimax,
    normalize_role_from_minimax,
    normalize_role_to_minimax,
    validate_minimax_messages,
)
from src.schemas.message import (
    MESSAGE_TYPE_UI_MAP,
    MessageSubtype,
    MessageType,
    UnifiedMessage,
    create_cancelled_message,
    create_completed_message,
    create_executing_message,
    create_failed_message,
    create_message,
    create_progress_message,
    create_thinking_message,
    create_waiting_message,
    format_timestamp,
    validate_message_dict,
)
from src.websocket.handler import MockWebSocket, WebSocketManager
from src.worktree.manager import WorktreeManager


# =============================================================================
# 辅助工具
# =============================================================================


def _make_event(msg_type: MessageType, content: dict | None = None) -> dict:
    """构造标准 WebSocket 事件字典。"""
    return {
        "type": msg_type.value,
        "data": content or {},
    }


def _assert_valid_unified_message(msg: UnifiedMessage, expected_type: MessageType) -> None:
    """断言 UnifiedMessage 结构完整且类型正确。"""
    assert isinstance(msg, UnifiedMessage)
    assert msg.type == expected_type
    assert msg.timestamp, "timestamp 不应为空"
    assert msg.status == expected_type.value, f"status 应跟随 type: {msg.status}"
    assert isinstance(msg.content, dict)


# =============================================================================
# Level 1: 初始状态 — 系统刚启动，无任何数据
# =============================================================================


class TestLevel1InitialState:
    """Level 1: 初始状态测试。

    系统刚启动，无任何数据。验证空状态下的正确行为。
    """

    # --- WebSocket 连接建立和心跳 ---

    def test_websocket_manager_initial_state(self) -> None:
        """初始状态下 WebSocketManager 无任何连接。"""
        mgr = WebSocketManager()
        assert mgr.global_connection_count == 0
        assert mgr.session_connection_count == 0

    async def test_first_websocket_connection(self) -> None:
        """首次 WebSocket 连接建立成功。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)
        assert mgr.global_connection_count == 1
        assert mgr.get_global_websocket("user_1") is ws

    async def test_websocket_heartbeat_update(self) -> None:
        """心跳更新后时间戳正确。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)
        mgr.update_heartbeat("user_1")
        assert "user_1" in mgr._heartbeat_timestamps

    async def test_websocket_heartbeat_timeout(self) -> None:
        """心跳超时后连接被清理。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)
        # 模拟超时：手动设置时间戳为很久以前
        mgr._heartbeat_timestamps["user_1"] = time.time() - 9999
        timed_out = mgr.check_heartbeats()
        assert "user_1" in timed_out
        assert mgr.global_connection_count == 0

    # --- 会话列表为空时的正确处理 ---

    def test_empty_session_list(self) -> None:
        """初始状态下会话列表为空。"""
        cm = ConversationManager()
        result = cm.list_threads(page=1, page_size=20)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["has_next"] is False

    def test_empty_session_list_pagination(self) -> None:
        """空会话列表分页查询返回空。"""
        cm = ConversationManager()
        result = cm.list_threads(page=2, page_size=10)
        assert result["items"] == []
        assert result["total"] == 0

    # --- 消息历史分页和加载（空状态） ---

    def test_message_history_nonexistent_thread(self) -> None:
        """查询不存在的会话消息返回 None。"""
        cm = ConversationManager()
        result = cm.get_messages("nonexistent_thread", page=1)
        assert result is None

    # --- 消息格式统一 ---

    def test_thinking_message_format(self) -> None:
        """THINKING 类型消息格式正确（reasoning/thinking 独立渲染基础）。"""
        msg = create_thinking_message("正在分析需求...")
        _assert_valid_unified_message(msg, MessageType.THINKING)
        assert msg.content["text"] == "正在分析需求..."
        # THINKING 消息无 subtype，前端可据此独立渲染
        assert msg.subtype is None

    def test_executing_message_format(self) -> None:
        """EXECUTING 类型消息格式正确。"""
        msg = create_executing_message("bash")
        _assert_valid_unified_message(msg, MessageType.EXECUTING)
        assert msg.content["tool_name"] == "bash"

    def test_completed_message_format(self) -> None:
        """COMPLETED 类型消息格式正确。"""
        msg = create_completed_message("任务完成")
        _assert_valid_unified_message(msg, MessageType.COMPLETED)
        assert msg.content["result"] == "任务完成"

    def test_failed_message_format(self) -> None:
        """FAILED 类型消息格式正确，包含 ERROR subtype。"""
        msg = create_failed_message("连接超时")
        _assert_valid_unified_message(msg, MessageType.FAILED)
        assert msg.subtype == MessageSubtype.ERROR
        assert msg.content["error"] == "连接超时"

    def test_message_serialization_roundtrip(self) -> None:
        """消息序列化和反序列化一致。"""
        original = create_message(
            MessageType.THINKING,
            content={"text": "test"},
            metadata={"task_id": "t1"},
        )
        d = original.to_dict()
        restored = UnifiedMessage.from_dict(d)
        assert restored.type == original.type
        assert restored.content == original.content
        assert restored.metadata == original.metadata

    # --- UI 状态映射完整性 ---

    def test_ui_map_covers_all_message_types(self) -> None:
        """UI 状态映射覆盖所有 MessageType。"""
        for mt in MessageType:
            assert mt in MESSAGE_TYPE_UI_MAP, f"缺少 {mt} 的 UI 映射"
            ui = MESSAGE_TYPE_UI_MAP[mt]
            assert "color" in ui
            assert "icon" in ui
            assert "label" in ui

    # --- Minimax role 转换（基础） ---

    def test_minimax_role_normalization_basic(self) -> None:
        """基础角色转换正确。"""
        assert normalize_role_to_minimax("user") == "user"
        assert normalize_role_to_minimax("assistant") == "assistant"
        assert normalize_role_to_minimax("system") == "system"
        assert normalize_role_to_minimax("tool_call") == "function"

    def test_minimax_role_from_api(self) -> None:
        """从 Minimax API 角色转换回内部格式。"""
        assert normalize_role_from_minimax("user") == "user"
        assert normalize_role_from_minimax("assistant") == "assistant"
        assert normalize_role_from_minimax("function") == "function_call"

    # --- Worktree 自动清理（空状态） ---

    def test_worktree_no_active_entries(self) -> None:
        """初始状态下无活跃工作树。"""
        wm = WorktreeManager()
        assert wm.active_count == 0
        assert wm.total_count == 0
        assert wm.list_active() == []

    def test_worktree_auto_cleanup_empty(self) -> None:
        """空状态下自动清理返回空列表。"""
        wm = WorktreeManager()
        cleaned = wm.auto_cleanup_stale()
        assert cleaned == []

    # --- 错误处理和恢复 ---

    def test_error_creation(self) -> None:
        """应用错误创建和属性正确。"""
        err = AppError("test error", code="TEST", recoverable=True)
        assert str(err) == "test error"
        assert err.code == "TEST"
        assert err.recoverable is True
        d = err.to_dict()
        assert d["code"] == "TEST"

    def test_connection_error_is_recoverable(self) -> None:
        """连接错误可恢复。"""
        err = ConnectionError_()
        assert err.recoverable is True
        assert err.code == "CONNECTION_ERROR"

    def test_validation_error_not_recoverable(self) -> None:
        """验证错误不可恢复。"""
        err = MessageValidationError()
        assert err.recoverable is False


# =============================================================================
# Level 2: 轻量状态 — 1-5个会话，每个会话少量消息
# =============================================================================


class TestLevel2LightState:
    """Level 2: 轻量状态测试。

    1-5个会话，每个会话少量消息。验证基础功能正确性。
    """

    @pytest.fixture
    def conversations(self) -> ConversationManager:
        """创建包含 3 个会话的 ConversationManager。"""
        cm = ConversationManager()
        for i in range(1, 4):
            thread = cm.create_thread(title=f"测试会话 {i}", agent_id=f"agent_{i}")
            cm.add_messages_to_thread(
                thread.thread_id, count=i * 2, prefix=f"会话{i}消息"
            )
        return cm

    @pytest.fixture
    def ws_manager(self) -> WebSocketManager:
        """创建带 1 个全局连接的 WebSocketManager。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="light_user")
        mgr.register_global("light_user", ws)
        return mgr

    # --- WebSocket 连接建立和心跳 ---

    async def test_websocket_session_connection(self) -> None:
        """会话级 WebSocket 连接注册和消息推送。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)

        ws_session = MockWebSocket()
        mgr.register_session("thread_1", ws_session)
        assert mgr.session_connection_count == 1

        event = _make_event(MessageType.THINKING, {"text": "思考中..."})
        success = await mgr.send_to_thread("thread_1", event)
        assert success is True
        assert len(ws_session.sent_messages) == 1

    async def test_websocket_message_receive(self) -> None:
        """通过全局连接发送消息，接收方收到。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)

        event = _make_event(MessageType.COMPLETED, {"result": "done"})
        success = await mgr.send_to_user("user_1", event)
        assert success is True
        assert len(ws.sent_messages) == 1

    # --- 会话列表加载 ---

    def test_session_list_loads_all(self, conversations: ConversationManager) -> None:
        """轻量状态下会话列表加载全部会话。"""
        result = conversations.list_threads(page=1, page_size=20)
        assert result["total"] == 3
        assert len(result["items"]) == 3
        assert result["has_next"] is False

    def test_session_list_order_by_update_time(
        self, conversations: ConversationManager
    ) -> None:
        """会话列表按更新时间倒序排列。"""
        result = conversations.list_threads(page=1, page_size=20)
        items = result["items"]
        # 最新创建的排在前面
        for i in range(len(items) - 1):
            assert items[i]["updated_at"] >= items[i + 1]["updated_at"]

    # --- 消息渲染 ---

    def test_message_rendering_basic(self, conversations: ConversationManager) -> None:
        """基础消息渲染 — 消息包含必要字段。"""
        threads = conversations.list_threads(page=1, page_size=20)
        thread_id = threads["items"][0]["thread_id"]

        msgs = conversations.get_messages(thread_id, page=1, page_size=50)
        assert msgs is not None
        assert msgs["total"] > 0
        for m in msgs["items"]:
            assert "msg_id" in m
            assert "role" in m
            assert "content" in m
            assert "type" in m
            assert "created_at" in m

    # --- 流式输出基础功能 ---

    def test_streaming_message_basic(self) -> None:
        """流式消息块追加到同一消息。"""
        cm = ConversationManager()
        thread = cm.create_thread(title="流式测试")

        # 发送流式块
        msg1 = cm.send_streaming_chunk(thread.thread_id, "Hello", msg_id="stream_1")
        assert msg1 is not None
        assert msg1.content == "Hello"

        msg2 = cm.send_streaming_chunk(thread.thread_id, " World", msg_id="stream_1")
        assert msg2 is not None
        assert msg2.content == "Hello World"
        assert msg2.msg_id == "stream_1"

    # --- 消息格式统一（reasoning/thinking 独立渲染） ---

    def test_thinking_vs_executing_distinct(self) -> None:
        """THINKING 和 EXECUTING 消息可区分 — 支持独立渲染。"""
        thinking = create_thinking_message("分析中...")
        executing = create_executing_message("bash")

        # 类型不同
        assert thinking.type != executing.type
        # content 字段不同（text vs tool_name）
        assert "text" in thinking.content
        assert "tool_name" in executing.content
        # UI 映射不同
        assert MESSAGE_TYPE_UI_MAP[MessageType.THINKING] != MESSAGE_TYPE_UI_MAP[
            MessageType.EXECUTING
        ]

    def test_progress_message_subtype(self) -> None:
        """PROGRESS 消息有独立 subtype，支持进度条渲染。"""
        msg = create_progress_message(progress=50.0, description="处理中")
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.PROGRESS
        assert msg.content["progress"] == 50.0

    # --- Minimax role 转换 ---

    def test_minimax_normalize_message_list(self) -> None:
        """消息列表角色批量转换为 Minimax 格式。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool_call", "content": "result"},
            {"role": "assistant", "content": "response"},
        ]
        normalized = normalize_messages_for_minimax(messages)
        assert normalized[0]["role"] == "user"
        assert normalized[1]["role"] == "function"  # tool_call -> function
        assert normalized[2]["role"] == "assistant"
        # 原始列表不被修改
        assert messages[1]["role"] == "tool_call"

    def test_minimax_validate_valid_messages(self) -> None:
        """合法消息列表验证通过。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "response"},
        ]
        errors = validate_minimax_messages(messages)
        assert errors == []

    def test_minimax_validate_invalid_role(self) -> None:
        """无效角色验证失败。"""
        messages = [{"role": "invalid_role", "content": "test"}]
        errors = validate_minimax_messages(messages)
        assert len(errors) > 0

    # --- Worktree 自动清理 ---

    def test_worktree_create_and_cleanup(self) -> None:
        """工作树创建后可正确清理。"""
        wm = WorktreeManager()
        entry = wm.create(task_id="task_1")
        assert wm.active_count == 1

        success = wm.cleanup(entry.tree_id)
        assert success is True
        assert wm.active_count == 0
        assert entry.status == "cleaned"

    def test_worktree_cleanup_by_task(self) -> None:
        """按任务 ID 清理工作树。"""
        wm = WorktreeManager()
        wm.create(task_id="task_1")
        assert wm.active_count == 1

        success = wm.cleanup_by_task("task_1")
        assert success is True
        assert wm.active_count == 0

    # --- 错误处理和恢复 ---

    async def test_retry_on_recoverable_error(self) -> None:
        """可恢复错误自动重试。"""
        policy = RetryPolicy(max_retries=3, base_delay=0.01, exponential=False)
        recovery = ErrorRecovery(retry_policy=policy)

        call_count = 0

        async def flaky_func() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError_("连接失败")
            return "success"

        result = await recovery.execute_with_retry(flaky_func)
        assert result == "success"
        assert call_count == 3

    async def test_no_retry_on_non_recoverable(self) -> None:
        """不可恢复错误不重试。"""
        policy = RetryPolicy(max_retries=3, base_delay=0.01)
        recovery = ErrorRecovery(retry_policy=policy)

        async def fail_func() -> None:
            raise MessageValidationError("格式错误")

        with pytest.raises(MessageValidationError):
            await recovery.execute_with_retry(fail_func)


# =============================================================================
# Level 3: 中等状态 — 20-50个会话，部分会话有较长历史
# =============================================================================


class TestLevel3MediumState:
    """Level 3: 中等状态测试。

    20-50个会话，部分会话有较长历史。验证分页和性能。
    """

    @pytest.fixture
    def conversations(self) -> ConversationManager:
        """创建包含 30 个会话的 ConversationManager，部分有长历史。"""
        cm = ConversationManager()
        for i in range(1, 31):
            thread = cm.create_thread(title=f"会话 {i}", agent_id=f"agent_{i % 5}")
            # 每第 5 个会话有较长历史（50条消息）
            msg_count = 50 if i % 5 == 0 else 5
            cm.add_messages_to_thread(
                thread.thread_id, count=msg_count, prefix=f"会话{i}"
            )
        return cm

    @pytest.fixture
    def ws_manager(self) -> WebSocketManager:
        """创建带多个全局连接的 WebSocketManager。"""
        mgr = WebSocketManager()
        for i in range(3):
            ws = MockWebSocket(user_id=f"user_{i}")
            mgr.register_global(f"user_{i}", ws)
        return mgr

    # --- WebSocket 连接建立和心跳 ---

    async def test_multiple_global_connections(self) -> None:
        """多个全局连接同时存在。"""
        mgr = WebSocketManager()
        for i in range(5):
            ws = MockWebSocket(user_id=f"user_{i}")
            mgr.register_global(f"user_{i}", ws)
        assert mgr.global_connection_count == 5

    async def test_websocket_fallback_to_global(self) -> None:
        """会话连接不存在时回退到全局连接。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="user_1")
        mgr.register_global("user_1", ws)

        event = _make_event(MessageType.WAITING, {"reason": "等待输入"})
        success = await mgr.send_to_thread("nonexistent_thread", event)
        assert success is True
        assert len(ws.sent_messages) == 1

    async def test_websocket_send_to_nonexistent_user(self) -> None:
        """向不存在的用户发送返回 False。"""
        mgr = WebSocketManager()
        event = _make_event(MessageType.COMPLETED, {"result": "done"})
        success = await mgr.send_to_user("ghost_user", event)
        assert success is False

    # --- 会话列表分页 ---

    def test_thread_pagination_first_page(
        self, conversations: ConversationManager
    ) -> None:
        """会话列表分页 — 第一页。"""
        result = conversations.list_threads(page=1, page_size=10)
        assert len(result["items"]) == 10
        assert result["total"] == 30
        assert result["page"] == 1
        assert result["has_next"] is True

    def test_thread_pagination_last_page(
        self, conversations: ConversationManager
    ) -> None:
        """会话列表分页 — 最后一页。"""
        result = conversations.list_threads(page=3, page_size=10)
        assert len(result["items"]) == 10
        assert result["has_next"] is False

    def test_thread_pagination_beyond_range(
        self, conversations: ConversationManager
    ) -> None:
        """分页超出范围返回空列表。"""
        result = conversations.list_threads(page=10, page_size=10)
        assert result["items"] == []
        assert result["total"] == 30

    def test_thread_pagination_page_size_limit(
        self, conversations: ConversationManager
    ) -> None:
        """分页大小受限（MAX_PAGE_SIZE）。"""
        result = conversations.list_threads(page=1, page_size=200)
        assert len(result["items"]) <= 100  # MAX_PAGE_SIZE

    # --- 消息历史分页和加载 ---

    def test_message_pagination_long_history(
        self, conversations: ConversationManager
    ) -> None:
        """长历史会话的消息分页正确。"""
        # 找一个有 50 条消息的会话
        threads = conversations.list_threads(page=1, page_size=30)
        long_thread = None
        for t in threads["items"]:
            if t["message_count"] == 50:
                long_thread = t
                break
        assert long_thread is not None

        result = conversations.get_messages(long_thread["thread_id"], page=1, page_size=20)
        assert result is not None
        assert result["total"] == 50
        assert len(result["items"]) == 20
        assert result["has_next"] is True

    def test_message_pagination_second_page(
        self, conversations: ConversationManager
    ) -> None:
        """消息分页第二页内容正确。"""
        threads = conversations.list_threads(page=1, page_size=30)
        long_thread = next(
            t for t in threads["items"] if t["message_count"] == 50
        )

        p1 = conversations.get_messages(long_thread["thread_id"], page=1, page_size=20)
        p2 = conversations.get_messages(long_thread["thread_id"], page=2, page_size=20)
        assert p1 is not None and p2 is not None

        # 两页消息不重复
        p1_ids = {m["msg_id"] for m in p1["items"]}
        p2_ids = {m["msg_id"] for m in p2["items"]}
        assert p1_ids.isdisjoint(p2_ids)

    # --- 多会话切换不丢失状态 ---

    def test_multi_session_switch_preserves_state(
        self, conversations: ConversationManager
    ) -> None:
        """多会话切换时各会话消息不丢失。"""
        threads = conversations.list_threads(page=1, page_size=30)
        ids = [t["thread_id"] for t in threads["items"]]

        # 轮流查询各会话消息
        message_counts: dict[str, int] = {}
        for tid in ids:
            msgs = conversations.get_messages(tid, page=1, page_size=100)
            assert msgs is not None
            message_counts[tid] = msgs["total"]

        # 再查一次确认数量不变
        for tid in ids:
            msgs = conversations.get_messages(tid, page=1, page_size=100)
            assert msgs is not None
            assert msgs["total"] == message_counts[tid], (
                f"会话 {tid} 消息数量变化"
            )

    # --- 性能验证（响应<1s） ---

    def test_thread_list_performance(
        self, conversations: ConversationManager
    ) -> None:
        """30 个会话的列表查询在 1s 内完成。"""
        start = time.time()
        for _ in range(100):
            conversations.list_threads(page=1, page_size=20)
        elapsed = time.time() - start
        avg_ms = (elapsed / 100) * 1000
        assert avg_ms < 1000, f"平均响应时间 {avg_ms:.1f}ms 超过 1s"

    def test_message_pagination_performance(
        self, conversations: ConversationManager
    ) -> None:
        """消息分页查询在 1s 内完成。"""
        threads = conversations.list_threads(page=1, page_size=30)
        long_thread = next(
            t for t in threads["items"] if t["message_count"] == 50
        )
        start = time.time()
        for _ in range(100):
            conversations.get_messages(
                long_thread["thread_id"], page=1, page_size=20
            )
        elapsed = time.time() - start
        avg_ms = (elapsed / 100) * 1000
        assert avg_ms < 1000, f"平均响应时间 {avg_ms:.1f}ms 超过 1s"

    # --- 消息格式统一 ---

    def test_all_message_types_serializable(self) -> None:
        """所有消息类型都可正确序列化和反序列化。"""
        messages = [
            create_thinking_message("思考"),
            create_executing_message("tool"),
            create_waiting_message("等待"),
            create_completed_message("完成"),
            create_failed_message("失败"),
            create_cancelled_message("取消"),
            create_progress_message(50.0, "半程"),
        ]
        for msg in messages:
            d = msg.to_dict()
            assert validate_message_dict(d), f"{msg.type} 序列化后验证失败"
            restored = UnifiedMessage.from_dict(d)
            assert restored.type == msg.type

    # --- Minimax role 转换 ---

    def test_minimax_ensure_alternating_roles(self) -> None:
        """连续相同角色的消息被合并。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "world"},
            {"role": "assistant", "content": "response"},
        ]
        result = ensure_alternating_roles(messages)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert "hello" in result[0]["content"]
        assert "world" in result[0]["content"]

    def test_minimax_validate_empty_list(self) -> None:
        """空消息列表验证失败。"""
        errors = validate_minimax_messages([])
        assert len(errors) > 0

    # --- Worktree 自动清理 ---

    def test_worktree_multiple_entries(self) -> None:
        """多个工作树管理正确。"""
        wm = WorktreeManager()
        entries = []
        for i in range(5):
            entry = wm.create(task_id=f"task_{i}")
            entries.append(entry)
        assert wm.active_count == 5

        # 清理部分
        wm.cleanup(entries[0].tree_id)
        wm.cleanup(entries[2].tree_id)
        assert wm.active_count == 3

    def test_worktree_get_by_task(self) -> None:
        """按任务 ID 查找工作树。"""
        wm = WorktreeManager()
        entry = wm.create(task_id="task_abc")
        found = wm.get_by_task("task_abc")
        assert found is not None
        assert found.tree_id == entry.tree_id

    # --- 错误处理和恢复 ---

    def test_session_not_found_error(self) -> None:
        """会话未找到抛出正确异常。"""
        err = SessionNotFoundError("thread_123")
        assert err.code == "SESSION_NOT_FOUND"
        assert "thread_123" in str(err)

    def test_error_recovery_tracking(self) -> None:
        """错误恢复器正确追踪错误次数。"""
        recovery = ErrorRecovery()
        err = ConnectionError_()
        recovery.record_error(err)
        recovery.record_error(err)
        assert recovery.get_error_count("CONNECTION_ERROR") == 2
        assert recovery.can_retry(err) is True  # 2 < 3 (default max)


# =============================================================================
# Level 4: 重度状态 — 100+会话、多Agent并发、大量消息历史
# =============================================================================


class TestLevel4HeavyState:
    """Level 4: 重度状态测试。

    100+会话、多Agent并发、大量消息历史。验证性能和稳定性。
    """

    @pytest.fixture
    def conversations(self) -> ConversationManager:
        """创建包含 120 个会话的 ConversationManager。"""
        cm = ConversationManager()
        for i in range(1, 121):
            thread = cm.create_thread(
                title=f"重度会话 {i}",
                agent_id=f"agent_{i % 10}",
            )
            # 每第 10 个会话有 200 条消息
            msg_count = 200 if i % 10 == 0 else 10
            cm.add_messages_to_thread(
                thread.thread_id, count=msg_count, prefix=f"会话{i}"
            )
        return cm

    @pytest.fixture
    def ws_manager(self) -> WebSocketManager:
        """创建带多个用户和会话连接的 WebSocketManager。"""
        mgr = WebSocketManager()
        # 10 个全局连接
        for i in range(10):
            ws = MockWebSocket(user_id=f"heavy_user_{i}")
            mgr.register_global(f"heavy_user_{i}", ws)
        # 20 个会话连接
        for i in range(20):
            ws = MockWebSocket()
            mgr.register_session(f"heavy_thread_{i}", ws)
        return mgr

    # --- WebSocket 消息不串扰 ---

    async def test_websocket_no_cross_talk(self) -> None:
        """不同会话的 WebSocket 消息不串扰。"""
        mgr = WebSocketManager()
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        mgr.register_session("thread_A", ws1)
        mgr.register_session("thread_B", ws2)

        event_a = _make_event(MessageType.THINKING, {"text": "线程A"})
        event_b = _make_event(MessageType.EXECUTING, {"tool_name": "线程B"})

        await mgr.send_to_thread("thread_A", event_a)
        await mgr.send_to_thread("thread_B", event_b)

        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1
        # 各自收到自己的消息
        import json
        msg_a = json.loads(ws1.sent_messages[0])
        assert msg_a["type"] == "thinking"
        msg_b = json.loads(ws2.sent_messages[0])
        assert msg_b["type"] == "executing"

    async def test_websocket_stale_connection_cleanup(self) -> None:
        """失效连接在发送失败后自动清理。"""
        mgr = WebSocketManager()
        ws_bad = MockWebSocket()
        mgr.register_session("thread_1", ws_bad)

        # 关闭连接使其失效
        await ws_bad.close()

        event = _make_event(MessageType.COMPLETED, {"result": "done"})
        # send_to_thread 尝试发送失败 -> 清理 stale -> 回退到全局连接（无）-> 返回 False
        success = await mgr.send_to_thread("thread_1", event)
        assert success is False
        # 失效连接已被清理出活跃列表
        remaining = mgr._active_connections.get("thread_1", [])
        assert ws_bad not in remaining

    async def test_global_send_failure_cleanup(self) -> None:
        """全局连接发送失败后自动清理。"""
        mgr = WebSocketManager()
        ws = MockWebSocket(user_id="bad_user")
        await ws.close()  # 关闭连接使其发送失败
        mgr.register_global("bad_user", ws)

        event = _make_event(MessageType.COMPLETED)
        success = await mgr.send_to_user("bad_user", event)
        assert success is False
        assert mgr.global_connection_count == 0

    # --- 会话列表大量数据 ---

    def test_thread_list_120_sessions(
        self, conversations: ConversationManager
    ) -> None:
        """120 个会话的列表查询正确。"""
        result = conversations.list_threads(page=1, page_size=50)
        assert result["total"] == 120
        assert len(result["items"]) == 50
        assert result["has_next"] is True

    def test_thread_list_full_pagination(
        self, conversations: ConversationManager
    ) -> None:
        """120 个会话完整分页遍历。"""
        all_ids: set[str] = set()
        page = 1
        while True:
            result = conversations.list_threads(page=page, page_size=50)
            for item in result["items"]:
                all_ids.add(item["thread_id"])
            if not result["has_next"]:
                break
            page += 1

        assert len(all_ids) == 120, "分页遍历应覆盖所有 120 个会话"

    # --- 消息历史大量数据 ---

    def test_long_history_pagination(
        self, conversations: ConversationManager
    ) -> None:
        """200 条消息的历史完整分页。"""
        threads = conversations.list_threads(page=1, page_size=120)
        long_thread = next(
            (t for t in threads["items"] if t["message_count"] == 200),
            None,
        )
        assert long_thread is not None

        all_ids: set[str] = set()
        page = 1
        while True:
            result = conversations.get_messages(
                long_thread["thread_id"], page=page, page_size=50
            )
            assert result is not None
            for m in result["items"]:
                all_ids.add(m["msg_id"])
            if not result["has_next"]:
                break
            page += 1

        assert len(all_ids) == 200, "消息分页应覆盖所有 200 条消息"

    # --- 性能不退化 ---

    def test_thread_list_performance_heavy(
        self, conversations: ConversationManager
    ) -> None:
        """120 个会话的列表查询平均响应时间合理。"""
        start = time.time()
        for _ in range(100):
            conversations.list_threads(page=1, page_size=20)
        elapsed = time.time() - start
        avg_ms = (elapsed / 100) * 1000
        # 允许更多时间但不应退化到秒级
        assert avg_ms < 2000, f"平均响应时间 {avg_ms:.1f}ms 退化严重"

    def test_message_pagination_performance_heavy(
        self, conversations: ConversationManager
    ) -> None:
        """200 条消息的分页查询平均响应时间合理。"""
        threads = conversations.list_threads(page=1, page_size=120)
        long_thread = next(
            (t for t in threads["items"] if t["message_count"] == 200),
            None,
        )
        assert long_thread is not None

        start = time.time()
        for _ in range(100):
            conversations.get_messages(
                long_thread["thread_id"], page=2, page_size=50
            )
        elapsed = time.time() - start
        avg_ms = (elapsed / 100) * 1000
        assert avg_ms < 2000, f"平均响应时间 {avg_ms:.1f}ms 退化严重"

    # --- 多Agent并发 ---

    def test_multi_agent_sessions_isolated(
        self, conversations: ConversationManager
    ) -> None:
        """不同 Agent 的会话数据隔离。"""
        # 分页获取全部 120 个会话（MAX_PAGE_SIZE=100，需两页）
        all_items: list[dict] = []
        page = 1
        while True:
            result = conversations.list_threads(page=page, page_size=100)
            all_items.extend(result["items"])
            if not result["has_next"]:
                break
            page += 1

        agent_sessions: dict[str, list[str]] = {}
        for t in all_items:
            aid = t["agent_id"]
            agent_sessions.setdefault(aid, []).append(t["thread_id"])

        # 10 个 Agent 各有 12 个会话
        assert len(agent_sessions) == 10
        for aid, sessions in agent_sessions.items():
            assert len(sessions) == 12, f"Agent {aid} 应有 12 个会话"

    # --- 消息格式统一（大量消息） ---

    def test_mass_message_format_consistency(self) -> None:
        """大量消息格式一致性验证。"""
        for _ in range(200):
            msg = create_message(
                MessageType.EXECUTING,
                content={"tool_name": "test"},
                metadata={"task_id": "t1", "agent_id": "a1"},
            )
            d = msg.to_dict()
            assert validate_message_dict(d)
            assert isinstance(d["timestamp"], str)
            assert d["type"] == "executing"

    # --- Minimax role 转换（大量消息） ---

    def test_minimax_normalize_large_batch(self) -> None:
        """大量消息批量转换性能和正确性。"""
        messages = []
        for i in range(500):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"msg_{i}"})

        start = time.time()
        normalized = normalize_messages_for_minimax(messages)
        elapsed = time.time() - start

        assert len(normalized) == 500
        assert elapsed < 1.0, f"500 条消息转换耗时 {elapsed:.2f}s"
        for msg in normalized:
            assert msg["role"] in ("user", "assistant")

    # --- Worktree 自动清理（大量工作树） ---

    def test_worktree_heavy_create_and_cleanup(self) -> None:
        """100 个工作树的创建和清理。"""
        wm = WorktreeManager()
        entries = []
        for i in range(100):
            entry = wm.create(task_id=f"task_{i}")
            entries.append(entry)

        assert wm.active_count == 100

        # 批量清理前 50 个
        for entry in entries[:50]:
            wm.cleanup(entry.tree_id)
        assert wm.active_count == 50

        # 自动清理超时（模拟）
        for entry in entries[50:]:
            entry.created_at = time.time() - 99999
        cleaned = wm.auto_cleanup_stale()
        assert len(cleaned) == 50
        assert wm.active_count == 0

    def test_worktree_double_cleanup_safe(self) -> None:
        """重复清理同一工作树安全无异常。"""
        wm = WorktreeManager()
        entry = wm.create(task_id="task_1")
        assert wm.cleanup(entry.tree_id) is True
        assert wm.cleanup(entry.tree_id) is True  # 幂等
        assert wm.active_count == 0

    # --- 错误处理和恢复（重试耗尽） ---

    async def test_retry_exhausted_raises(self) -> None:
        """重试次数耗尽后抛出原始异常。"""
        policy = RetryPolicy(max_retries=2, base_delay=0.01, exponential=False)
        recovery = ErrorRecovery(retry_policy=policy)

        async def always_fail() -> None:
            raise ConnectionError_("始终失败")

        with pytest.raises(ConnectionError_) as exc_info:
            await recovery.execute_with_retry(always_fail)
        assert "始终失败" in str(exc_info.value)

    def test_error_recovery_reset(self) -> None:
        """错误恢复器重置后状态清空。"""
        recovery = ErrorRecovery()
        recovery.record_error(ConnectionError_())
        recovery.record_error(ConnectionError_())
        assert recovery.get_error_count("CONNECTION_ERROR") == 2

        recovery.reset()
        assert recovery.get_error_count("CONNECTION_ERROR") == 0

    # --- 综合场景：并发 WebSocket + 消息 + Worktree ---

    async def test_concurrent_operations(self) -> None:
        """综合并发操作：同时进行 WebSocket、消息和 Worktree 操作。"""
        ws_mgr = WebSocketManager()
        cm = ConversationManager()
        wt_mgr = WorktreeManager()

        # 准备数据
        thread = cm.create_thread(title="并发测试")
        wt_mgr.create(task_id="concurrent_task")
        ws = MockWebSocket(user_id="concurrent_user")
        ws_mgr.register_global("concurrent_user", ws)

        # 并发操作
        async def ws_send() -> bool:
            event = _make_event(MessageType.THINKING, {"text": "思考中"})
            return await ws_mgr.send_to_user("concurrent_user", event)

        async def add_msg() -> None:
            cm.add_message(thread.thread_id, "user", "并发消息")

        async def cleanup_wt() -> bool:
            return wt_mgr.cleanup_by_task("concurrent_task")

        results = await asyncio.gather(ws_send(), add_msg(), cleanup_wt())

        assert results[0] is True  # WebSocket 发送成功
        assert thread.message_count == 1  # 消息添加成功
        assert wt_mgr.active_count == 0  # Worktree 清理成功
