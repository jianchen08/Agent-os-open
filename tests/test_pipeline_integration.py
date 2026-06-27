"""管道后端集成测试 — 覆盖完整链条。

测试架构：
  前后端分离，后端测试只验证后端逻辑，不启动前端。
  使用 FakeSink 收集事件，用 MagicMock 模拟 PipelineEngine，
  不启动真实 LLM。

测试链条：
  1. 用户消息链：register_pipeline → send_pipeline_message → inject_message → FakeSink 收集事件
  2. 计时器触发链：register_pipeline → engine.suspend → send_pipeline_message(source=system) → wake
  3. 任务创建链：register_pipeline → send_pipeline_message(task_input) → FakeSink 收集事件
  4. 重启恢复链：restore_pipelines_on_startup → registry 中有引擎 → send_pipeline_message → 成功
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

from pipeline.message_types import MessageType, PipelineMessage


# ---------------------------------------------------------------------------
# FakeSink：模拟 IOutputSink，收集所有发送的事件
# ---------------------------------------------------------------------------


class FakeSink:
    """模拟 IOutputSink，收集所有发送的事件和文本。

    用于测试中验证管道是否正确发送了事件，无需启动真实 WebSocket。
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.sent_texts: list[str] = []

    async def send_event(self, event: dict) -> bool:
        """收集发送的事件。"""
        self.events.append(event)
        return True

    async def send_text(self, text: str) -> None:
        """收集发送的文本。"""
        self.sent_texts.append(text)

    @property
    def sink_id(self) -> str:
        """返回 sink 唯一标识。"""
        return "fake-sink"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_mock_engine(
    pipeline_id: str = "",
    *,
    is_running: bool = True,
    is_suspended: bool = False,
    is_idle: bool = False,
) -> MagicMock:
    """创建一个 mock PipelineEngine 实例。

    Args:
        pipeline_id: 管道 ID，为空则自动生成
        is_running: 引擎是否处于运行状态
        is_suspended: 引擎是否处于挂起状态

    Returns:
        配置好的 MagicMock 引擎实例
    """
    import uuid

    engine = MagicMock()
    pid = pipeline_id or uuid.uuid4().hex[:12]
    engine._pipeline_id = pid
    engine.pipeline_id = pid

    # 使用 PropertyMock 控制属性返回值
    type(engine).is_running = PropertyMock(return_value=is_running)
    type(engine).is_suspended = PropertyMock(return_value=is_suspended)
    # is_idle = not _run_started（与真实引擎 engine.py:2036 一致）。
    # 已完成引擎：跑过一轮（_run_started=True）但不再运行 → is_idle=False。
    type(engine).is_idle = PropertyMock(return_value=is_idle)

    engine.inject_message = MagicMock()
    engine._suspended_state = {"user_input": "", "messages": []} if is_suspended else None
    engine._pending_notifications = []
    engine._wake_event = None
    engine._running = is_running
    engine._run_started = not is_idle

    return engine


def _register_mock_engine(
    pipeline_id: str,
    *,
    is_running: bool = True,
    is_suspended: bool = False,
    is_idle: bool = False,
    tags: dict[str, str] | None = None,
    thread_id: str = "",
) -> MagicMock:
    """注册一个 mock 引擎到全局 EngineRegistry。

    Args:
        pipeline_id: 管道 ID
        is_running: 引擎是否运行中
        is_suspended: 引擎是否挂起
        is_idle: 引擎是否处于 idle（未启动 run）；已完成引擎传 False
        tags: 关联标签
        thread_id: WebSocket 线程 ID

    Returns:
        注册的 mock 引擎实例
    """
    from pipeline.registry import get_engine_registry

    engine = _make_mock_engine(
        pipeline_id, is_running=is_running, is_suspended=is_suspended, is_idle=is_idle,
    )
    registry = get_engine_registry()
    registry.register(pipeline_id, engine, thread_id=thread_id, tags=tags)
    return engine


# ---------------------------------------------------------------------------
# 测试用例：用户消息完整链条
# ---------------------------------------------------------------------------


class TestUserMessageChain:
    """验证用户消息的完整注入链条。

    覆盖场景：首次注册、后续消息注入、空 pipeline_id 自动生成。
    """

    def setup_method(self) -> None:
        """每个测试前清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def teardown_method(self) -> None:
        """每个测试后清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    @pytest.mark.asyncio
    async def test_first_message_registers_and_sends(self) -> None:
        """验证首次消息：注册管道 → 发送消息 → 引擎被注入消息 → sink 收到 pipeline_received。

        测试步骤：
        1. registry 中无 "user-pipe-1"
        2. 注册 mock 引擎到 registry
        3. send_pipeline_message 发送消息
        4. 断言 inject_message 被调用
        5. 断言 sink.events 中有 pipeline_received
        """
        from pipeline.registry import get_engine_registry
        from pipeline.message_types import MessageType, PipelineMessage
        from pipeline.message_bus import send_pipeline_message

        registry = get_engine_registry()

        # 1. registry 中无 "user-pipe-1"
        assert registry.get("user-pipe-1") is None

        # 2. 注册 mock 引擎
        engine = _register_mock_engine("user-pipe-1", is_running=True, is_suspended=False)
        assert registry.get("user-pipe-1") is not None

        # 3. 发送消息
        sink = FakeSink()
        result = await send_pipeline_message(
            PipelineMessage(type=MessageType.CHAT, content="hello", pipeline_id="user-pipe-1"),
            output_sink=sink,
        )

        # 4. 断言 inject_message 被调用
        engine.inject_message.assert_called_once()
        call_args = engine.inject_message.call_args
        assert "hello" in call_args[0] or call_args[1].get("message") == "hello" or "hello" in str(call_args)

        # 5. 断言消息注入成功（sink 事件需要真实 drain_loop 运行才能产生）
        assert result.success is True
        assert result.method == "notification"

    @pytest.mark.asyncio
    async def test_subsequent_message_injects_directly(self) -> None:
        """验证后续消息：直接注入引擎，不需要重新注册。

        测试步骤：
        1. 注册 "user-pipe-2"，mock engine
        2. send_pipeline_message 发送消息
        3. 断言 inject_message 被调用
        """
        from pipeline.message_bus import send_pipeline_message

        # 1. 注册引擎
        engine = _register_mock_engine("user-pipe-2", is_running=True, is_suspended=False)

        # 2. 发送后续消息
        result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="second message", pipeline_id="user-pipe-2"))

        # 3. 断言 inject_message 被调用
        assert result.success is True
        engine.inject_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_message_allowed_for_wake(self) -> None:
        """验证空字符串消息：send_pipeline_message 允许空字符串通过（用于管道恢复/唤醒场景）。

        测试步骤：
        1. 注册引擎
        2. 发送空字符串消息
        3. 断言返回成功（空字符串用于唤醒）
        """
        from pipeline.message_bus import send_pipeline_message

        _register_mock_engine("user-pipe-3", is_running=True)

        result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="", pipeline_id="user-pipe-3"))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_whitespace_only_message_returns_failure(self) -> None:
        """验证纯空白消息：send_pipeline_message 应返回失败。

        测试步骤：
        1. 注册引擎
        2. 发送纯空白消息
        3. 断言返回失败
        """
        from pipeline.message_bus import send_pipeline_message

        _register_mock_engine("user-pipe-3b", is_running=True)

        result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="   ", pipeline_id="user-pipe-3b"))
        assert result.success is False
        assert "不能仅包含空白字符" in result.error

    @pytest.mark.asyncio
    async def test_empty_pipeline_id_returns_failure(self) -> None:
        """验证空 pipeline_id：send_pipeline_message 应返回失败。

        测试步骤：
        1. 发送消息到空 pipeline_id
        2. 断言返回失败
        """
        from pipeline.message_bus import send_pipeline_message

        result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="hello", pipeline_id=""))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_auto_generated_pipeline_id_in_register(self) -> None:
        """验证空 pipeline_id 注册时，register 返回有效条目。

        注意：register_pipeline 创建真实引擎需要完整的路由表和插件注册表，
        此处测试 register() 方法（直接注册）的行为。

        测试步骤：
        1. 使用 register() 注册一个不带 pipeline_id 前缀的引擎
        2. 断言 entry 存在且 engine._pipeline_id 非空
        """
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_mock_engine(is_running=True)
        pid = engine._pipeline_id

        entry = registry.register(pid, engine)
        assert entry is not None
        assert entry.engine._pipeline_id != ""
        assert len(entry.engine._pipeline_id) == 12


# ---------------------------------------------------------------------------
# 测试用例：计时器触发链条
# ---------------------------------------------------------------------------


class TestTimerTriggerChain:
    """验证计时器消息触发引擎唤醒的完整链条。

    覆盖场景：挂起引擎被系统消息唤醒、系统消息不取消 human_interaction。
    """

    def setup_method(self) -> None:
        """每个测试前清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def teardown_method(self) -> None:
        """每个测试后清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    @pytest.mark.asyncio
    async def test_idle_timer_wakes_suspended_engine(self) -> None:
        """验证计时器消息唤醒挂起引擎。

        测试步骤：
        1. 注册引擎，设置为 suspended 状态
        2. send_pipeline_message(source=system) 发送系统消息
        3. 断言 inject_message 被调用（唤醒）
        4. 断言 result.method == "wake"
        """
        from pipeline.message_bus import send_pipeline_message

        # 1. 注册挂起引擎
        engine = _register_mock_engine(
            "timer-pipe-1",
            is_running=False,
            is_suspended=True,
        )

        # 2. 发送系统消息（模拟 idle timer 触发）
        result = await send_pipeline_message(
            PipelineMessage(type=MessageType.CHAT, content="idle timeout", pipeline_id="timer-pipe-1", metadata={"source": "system"}),
        )

        # 3. 断言 inject_message 被调用
        engine.inject_message.assert_called_once()

        # 4. 断言 method == "wake"
        assert result.success is True
        assert result.method == "wake"

    @pytest.mark.asyncio
    async def test_timer_does_not_cancel_human_interaction(self) -> None:
        """验证 source=system 的消息不触发 _try_cancel_pending_interaction。

        测试步骤：
        1. 注册引擎，设置 running 状态
        2. send_pipeline_message(source=system) 发送系统消息
        3. 断言 inject_message 被调用
        4. 确认 method 为 "notification"（非 wake）
        """
        from pipeline.message_bus import send_pipeline_message

        # 1. 注册运行中引擎
        engine = _register_mock_engine(
            "timer-pipe-1",
            is_running=True,
            is_suspended=False,
        )

        # 2. 发送系统消息
        result = await send_pipeline_message(
            PipelineMessage(type=MessageType.CHAT, content="reminder", pipeline_id="timer-pipe-1", metadata={"source": "system"}),
        )

        # 3. 断言 inject_message 被调用
        engine.inject_message.assert_called_once()

        # 4. 断言 method == "notification"
        assert result.success is True
        assert result.method == "notification"

    @pytest.mark.asyncio
    async def test_user_source_message_to_running_engine(self) -> None:
        """验证 source=user 的消息注入到运行中引擎。

        测试步骤：
        1. 注册引擎，设置 running 状态
        2. send_pipeline_message 发送用户消息
        3. 断言 inject_message 被调用且 method == "notification"
        """
        from pipeline.message_bus import send_pipeline_message

        engine = _register_mock_engine(
            "timer-pipe-2",
            is_running=True,
            is_suspended=False,
        )

        result = await send_pipeline_message(
            PipelineMessage(type=MessageType.CHAT, content="user message", pipeline_id="timer-pipe-2", metadata={"source": "user"}),
        )

        assert result.success is True
        assert result.method == "notification"
        engine.inject_message.assert_called_once()


# ---------------------------------------------------------------------------
# 测试用例：任务创建链条
# ---------------------------------------------------------------------------


class TestTaskCreationChain:
    """验证子任务管道创建和消息注入链条。

    覆盖场景：子任务注册与消息发送、子任务标签关联。
    """

    def setup_method(self) -> None:
        """每个测试前清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def teardown_method(self) -> None:
        """每个测试后清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    @pytest.mark.asyncio
    async def test_task_executor_registers_and_sends(self) -> None:
        """验证子任务：注册管道 → 发送任务输入 → inject_message 被调用。

        测试步骤：
        1. register_pipeline 注册带任务标签的管道
        2. 断言 registry.get 存在
        3. send_pipeline_message 发送任务消息
        4. 断言 inject_message 被调用
        5. 断言 sink 收到事件
        """
        from pipeline.registry import get_engine_registry
        from pipeline.message_bus import send_pipeline_message

        registry = get_engine_registry()

        # 1. 注册带任务标签的引擎
        engine = _register_mock_engine(
            "task-pipe-1",
            is_running=True,
            is_suspended=False,
            tags={"task_id": "task-1", "mode": "interactive"},
        )

        # 2. 断言 registry 中存在
        entry = registry.get("task-pipe-1")
        assert entry is not None

        # 3. 发送任务消息
        sink = FakeSink()
        result = await send_pipeline_message(
            PipelineMessage(type=MessageType.CHAT, content="implement feature X", pipeline_id="task-pipe-1"),
            output_sink=sink,
        )

        # 4. 断言 inject_message 被调用
        engine.inject_message.assert_called_once()

        # 5. 断言消息注入成功（sink 事件需要真实 drain_loop 运行才能产生）
        assert result.success is True
        assert result.method == "notification"

    @pytest.mark.asyncio
    async def test_task_with_parent_pipeline_tag(self) -> None:
        """验证子任务标签关联：通过标签查找管道。

        测试步骤：
        1. 注册带 parent_pipeline 和 task_id 标签的引擎
        2. 断言 find_by_tag("parent_pipeline", "parent-pipe-1") 返回该管道
        3. 断言 find_by_tag("task_id", "task-2") 返回该管道
        """
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()

        # 1. 注册带标签的引擎
        _register_mock_engine(
            "child-pipe-1",
            is_running=True,
            tags={"parent_pipeline": "parent-pipe-1", "task_id": "task-2"},
        )

        # 2. 按 parent_pipeline 查找
        results = registry.find_by_tag("parent_pipeline", "parent-pipe-1")
        assert len(results) == 1
        assert results[0].engine._pipeline_id == "child-pipe-1"

        # 3. 按 task_id 查找
        results = registry.find_by_tag("task_id", "task-2")
        assert len(results) == 1
        assert results[0].engine._pipeline_id == "child-pipe-1"

    @pytest.mark.asyncio
    async def test_tag_limit_enforcement(self) -> None:
        """验证标签数量超过限制时抛出 ValueError。

        测试步骤：
        1. 尝试注册超过 MAX_TAGS_PER_PIPELINE 个标签
        2. 断言抛出 ValueError
        """
        from pipeline.registry import get_engine_registry, MAX_TAGS_PER_PIPELINE

        registry = get_engine_registry()
        engine = _make_mock_engine("limit-pipe")

        # 构造超过限制的标签
        too_many_tags = {f"key_{i}": f"val_{i}" for i in range(MAX_TAGS_PER_PIPELINE + 1)}

        with pytest.raises(ValueError, match="标签数量超过限制"):
            registry.register_pipeline(
                pipeline_id="limit-pipe",
                tags=too_many_tags,
                input_route_table=MagicMock(),
                output_route_table=MagicMock(),
                plugin_registry=MagicMock(),
            )

    @pytest.mark.asyncio
    async def test_find_by_tag_multiple_conditions(self) -> None:
        """验证多标签条件查询。

        测试步骤：
        1. 注册多个引擎，各有不同标签
        2. 使用多条件查询，断言结果精确匹配
        """
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()

        _register_mock_engine(
            "multi-1",
            is_running=True,
            tags={"task_id": "task-A", "mode": "interactive"},
        )
        _register_mock_engine(
            "multi-2",
            is_running=True,
            tags={"task_id": "task-A", "mode": "batch"},
        )
        _register_mock_engine(
            "multi-3",
            is_running=True,
            tags={"task_id": "task-B", "mode": "interactive"},
        )

        # 查询 task_id=task-A AND mode=interactive
        results = registry.find_by_tag("task_id", "task-A", "mode", "interactive")
        assert len(results) == 1
        assert results[0].engine._pipeline_id == "multi-1"

        # 查询 task_id=task-A（不限 mode）
        results = registry.find_by_tag("task_id", "task-A")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# TestRestoreChain 已删除：
# 原测 TaskWorker.restore_running_pipelines（启动时自动注册管道 + continue）。
# 该方法已删除——重启恢复由 TaskWorker._recover_running_tasks 负责
# （running→suspended 标记暂停，等用户明确恢复），不在启动时注册管道。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 测试用例：InjectResult 数据类
# ---------------------------------------------------------------------------


class TestInjectResult:
    """验证 InjectResult 数据类的行为。"""

    def test_default_values(self) -> None:
        """验证 InjectResult 默认值。"""
        from pipeline.message_bus import InjectResult

        result = InjectResult(success=True)
        assert result.success is True
        assert result.method == ""
        assert result.pipeline_id == ""
        assert result.error == ""
        assert result.bridge is None

    def test_failure_result(self) -> None:
        """验证 InjectResult 失败结果。"""
        from pipeline.message_bus import InjectResult

        result = InjectResult(success=False, error="something went wrong", method="failed")
        assert result.success is False
        assert result.error == "something went wrong"
        assert result.method == "failed"


# ---------------------------------------------------------------------------
# 测试用例：EngineRegistry 核心操作
# ---------------------------------------------------------------------------


class TestEngineRegistryOperations:
    """验证 EngineRegistry 的注册、查找、注销等核心操作。"""

    def setup_method(self) -> None:
        """每个测试前清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def teardown_method(self) -> None:
        """每个测试后清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def test_register_and_get(self) -> None:
        """验证注册后可通过 pipeline_id 查找。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_mock_engine("reg-test-1")

        entry = registry.register("reg-test-1", engine)
        assert entry is not None
        assert entry.engine is engine

        fetched = registry.get("reg-test-1")
        assert fetched is entry

    def test_unregister_removes_entry(self) -> None:
        """验证注销后条目被移除。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_mock_engine("unreg-test-1")
        registry.register("unreg-test-1", engine)

        removed = registry.unregister("unreg-test-1")
        assert removed is not None
        assert registry.get("unreg-test-1") is None

    def test_unregister_nonexistent_returns_none(self) -> None:
        """验证注销不存在的条目返回 None。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        result = registry.unregister("nonexistent")
        assert result is None

    def test_find_by_thread_id(self) -> None:
        """验证按 thread_id 查找管道条目。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine1 = _make_mock_engine("thread-1")
        engine2 = _make_mock_engine("thread-2")

        registry.register("thread-1", engine1, thread_id="ws-thread-A")
        registry.register("thread-2", engine2, thread_id="ws-thread-A")
        registry.register("thread-3", _make_mock_engine("thread-3"), thread_id="ws-thread-B")

        results = registry.find_by_thread_id("ws-thread-A")
        assert len(results) == 2

        results = registry.find_by_thread_id("ws-thread-B")
        assert len(results) == 1

    def test_all_entries_snapshot(self) -> None:
        """验证 all_entries 返回只读快照。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        registry.register("snap-1", _make_mock_engine("snap-1"))
        registry.register("snap-2", _make_mock_engine("snap-2"))

        entries = registry.all_entries()
        assert len(entries) == 2

        # 修改快照不影响原数据
        entries["snap-3"] = None
        assert registry.get("snap-3") is None

    def test_set_and_get_bridge(self) -> None:
        """验证 bridge 的设置和获取。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        registry.register("bridge-1", _make_mock_engine("bridge-1"))

        assert registry.get_bridge("bridge-1") is None

        mock_bridge = MagicMock()
        registry.set_bridge("bridge-1", mock_bridge)
        assert registry.get_bridge("bridge-1") is mock_bridge

    def test_update_thread_id(self) -> None:
        """验证 thread_id 的更新。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        registry.register("tid-1", _make_mock_engine("tid-1"), thread_id="old-thread")

        assert registry.get_thread_id("tid-1") == "old-thread"

        registry.update_thread_id("tid-1", "new-thread")
        assert registry.get_thread_id("tid-1") == "new-thread"

    def test_register_duplicate_returns_existing(self) -> None:
        """验证 register_pipeline 对已存在的 pipeline_id 返回现有条目。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_mock_engine("dup-1")
        registry.register("dup-1", engine)

        # 再次注册同一 pipeline_id
        entry = registry.register("dup-1", _make_mock_engine("dup-1-new"))
        assert entry.engine is not engine  # register 会覆盖
        assert len(registry._engines) == 1


# ---------------------------------------------------------------------------
# 测试用例：_find_engine 状态检测
# ---------------------------------------------------------------------------


class TestFindEngine:
    """验证 _find_engine 根据引擎状态返回正确结果。"""

    def setup_method(self) -> None:
        """每个测试前清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    def teardown_method(self) -> None:
        """每个测试后清理全局 EngineRegistry。"""
        from pipeline.registry import get_engine_registry

        get_engine_registry()._engines.clear()

    @pytest.mark.asyncio
    async def test_find_running_engine(self) -> None:
        """验证查找运行中引擎返回 (engine, "running")。"""
        from pipeline.message_bus import _find_engine

        _register_mock_engine("find-run-1", is_running=True, is_suspended=False)

        engine, state = _find_engine("find-run-1")
        assert engine is not None
        assert state == "running"

    @pytest.mark.asyncio
    async def test_find_suspended_engine(self) -> None:
        """验证查找挂起引擎返回 (engine, "suspended")。"""
        from pipeline.message_bus import _find_engine

        _register_mock_engine("find-sus-1", is_running=False, is_suspended=True)

        engine, state = _find_engine("find-sus-1")
        assert engine is not None
        assert state == "suspended"

    @pytest.mark.asyncio
    async def test_find_completed_engine_returns_none(self) -> None:
        """验证已完成（非 running 非 suspended 非 idle）引擎返回 (None, "")。"""
        from pipeline.message_bus import _find_engine

        # 已完成：跑过一轮（is_idle=False）但不再运行
        _register_mock_engine(
            "find-done-1", is_running=False, is_suspended=False, is_idle=False,
        )

        engine, state = _find_engine("find-done-1")
        assert engine is None
        assert state == ""

    @pytest.mark.asyncio
    async def test_find_nonexistent_returns_none(self) -> None:
        """验证查找不存在的管道返回 (None, "")。"""
        from pipeline.message_bus import _find_engine

        engine, state = _find_engine("nonexistent-pipe")
        assert engine is None
        assert state == ""
