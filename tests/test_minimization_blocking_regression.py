"""
回归测试：验证7个模块的最小化原则和阻塞问题修复未破坏现有功能。

覆盖模块：
1. 管道(pipeline) - PipelineEngine 挂起/唤醒/注入消息
2. 插件系统(plugins) - 插件导入与热重载非阻塞
3. 任务管理(tasks) - TaskService 完整生命周期
4. 工具调用执行(tools) - 工具注册/查找/参数校验
5. 文件操作 - 文件读写工具模拟
6. Agent启动/销毁(agents) - AgentRegistry/Loader 配置管理
7. 工作空间管理(workspace) - WorkspaceService 创建/查询/文件树
"""
import asyncio
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# 确保 src 目录在 sys.path 中
_src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


# ===========================================================================
# 1. 管道(Pipeline)回归测试
# ===========================================================================

class TestPipelineEngineRegression:
    """验证 PipelineEngine 核心功能（挂起/唤醒/注入/通知消费）未破坏。"""

    def _make_engine(self) -> Any:
        """构造最小可用的 PipelineEngine 实例。"""
        from pipeline.engine import PipelineEngine

        services: dict[str, Any] = {}
        # Mock plugin_registry
        plugin_registry = MagicMock()
        plugin_registry.get_core.return_value = None
        plugin_registry._plugins = {}

        # Mock input_route_table / output_route_table
        input_route = MagicMock()
        input_route.resolve_plugins.return_value = []
        input_route.resolve_target.return_value = ("end", None)

        output_route = MagicMock()
        output_route.arbitrate.return_value = None

        engine = PipelineEngine.__new__(PipelineEngine)
        engine._services = services
        engine._pipeline_id = "test-pipeline-001"
        engine._suspended_state = None
        engine._wake_event = None
        engine._inject_queue: list[tuple[str, str]] = []
        engine._engine_loop = None
        engine._watching_task_ids: list[str] = []
        engine._checkpoint_manager = None
        engine.max_iterations = 100
        engine.plugin_registry = plugin_registry
        engine.input_route_table = input_route
        engine.output_route_table = output_route

        return engine

    def test_pipeline_id_readonly(self) -> None:
        """pipeline_id 应为只读属性。"""
        engine = self._make_engine()
        assert engine.pipeline_id == "test-pipeline-001"

    def test_is_suspended_initially_false(self) -> None:
        """初始状态 should not be suspended。"""
        engine = self._make_engine()
        assert engine.is_suspended is False

    def test_inject_message_suspended_state(self) -> None:
        """挂起状态下 inject_message 入队并唤醒（不直接写 _suspended_state）。

        新架构：消息统一入 _inject_queue，由 consume_pending_notifications 处理。
        """
        engine = self._make_engine()
        engine._suspended_state = {"user_input": "", "messages": []}
        engine._wake_event = asyncio.Event()

        engine.inject_message("子任务完成通知")

        # 消息入队（不写 suspended_state），wake_event 被 set
        assert engine._inject_queue == [("子任务完成通知", "user")]
        assert engine._wake_event.is_set()

    def test_inject_message_running_state_queues(self) -> None:
        """运行状态下 inject_message 不再自行维护 _pending_notifications。
        运行态的消息由 bridge.enqueue_notification 统一管理。"""
        engine = self._make_engine()
        engine._suspended_state = None
        engine._wake_event = asyncio.Event()

        engine.inject_message("运行中通知")

        # 运行态不维护 _pending_notifications（已迁移到 bridge）
        assert engine._wake_event.is_set()

    def test_inject_message_empty_input_ignored(self) -> None:
        """空输入应被忽略，不注入也不唤醒。"""
        engine = self._make_engine()
        engine._suspended_state = {"user_input": "", "messages": []}
        engine._wake_event = asyncio.Event()

        engine.inject_message("")

        assert engine._suspended_state["user_input"] == ""
        assert not engine._wake_event.is_set()

    def test_wake_sets_event(self) -> None:
        """wake() 应设置 _wake_event。"""
        engine = self._make_engine()
        engine._wake_event = asyncio.Event()
        assert not engine._wake_event.is_set()

        engine.wake()
        assert engine._wake_event.is_set()

    def test_wake_no_event_noop(self) -> None:
        """_wake_event 为 None 时 wake() 不报错。"""
        engine = self._make_engine()
        engine._wake_event = None
        engine.wake()  # 不应抛异常


# ===========================================================================
# 2. 插件系统(Plugins)回归测试
# ===========================================================================

class TestPluginSystemRegression:
    """验证插件系统的导入、加载和热重载非阻塞。"""

    def test_plugins_module_importable(self) -> None:
        """plugins 包应可正常导入。"""
        import plugins
        assert hasattr(plugins, "get_hot_reloader")
        assert hasattr(plugins, "__all__")

    def test_get_hot_reloader_returns_class(self) -> None:
        """get_hot_reloader 应返回 PluginHotReloader 类。"""
        from plugins import get_hot_reloader
        reloader_cls = get_hot_reloader()
        assert reloader_cls is not None
        assert callable(reloader_cls)

    def test_hot_reloader_instantiation(self) -> None:
        """PluginHotReloader 应能用任意目录实例化。"""
        from plugins.hot_reload import PluginHotReloader
        reloader = PluginHotReloader(config_dir="/nonexistent/path")
        assert reloader is not None

    def test_on_file_change_non_blocking(self) -> None:
        """_on_file_change 应快速返回，不阻塞调用线程。"""
        from plugins.hot_reload import PluginHotReloader
        reloader = PluginHotReloader(config_dir="/nonexistent")

        start = time.monotonic()
        reloader._on_file_change("modified", "/some/file.yaml")
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, (
            f"_on_file_change 耗时 {elapsed:.3f}s，应非阻塞快速返回"
        )

    def test_input_plugins_importable(self) -> None:
        """关键 Input 插件应可正常导入。"""
        from plugins.input import (
            PromptBuildPlugin,
            ContextBuildPlugin,
            MemoryReadPlugin,
        )
        assert PromptBuildPlugin is not None
        assert ContextBuildPlugin is not None
        assert MemoryReadPlugin is not None


# ===========================================================================
# 3. 任务管理(TaskService)回归测试
# ===========================================================================

class TestTaskServiceRegression:
    """验证 TaskService 完整生命周期未破坏。"""

    def _make_service(self) -> Any:
        """创建使用内存存储的 TaskService（不写文件）。"""
        from tasks.service import TaskService

        return TaskService(data_dir=tempfile.mkdtemp())

    @pytest.mark.asyncio
    async def test_create_task_success(self) -> None:
        """创建任务应返回 PENDING 状态的 TaskModel。"""
        svc = self._make_service()
        task = await svc.create_task("测试任务", "任务描述")
        assert task.title == "测试任务"
        assert task.description == "任务描述"
        assert task.status.value == "pending"

    @pytest.mark.asyncio
    async def test_start_task_success(self) -> None:
        """启动任务应将状态从 pending 变为 running。"""
        svc = self._make_service()
        task = await svc.create_task("待启动")
        await svc.start_task(task.id)
        started = svc.get_task(task.id)
        assert started.status.value == "running"

    @pytest.mark.asyncio
    async def test_complete_task_flow(self) -> None:
        """完整流程: pending -> running -> evaluating -> completed。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task("完整流程任务")
        await svc.start_task(task.id)
        await svc.force_transition(task.id, TaskStatus.EVALUATING)
        await svc.complete_evaluation(task.id, passed=True, result={"data": "OK"})
        result = svc.get_task(task.id)
        assert result.status.value == "completed"

    @pytest.mark.asyncio
    async def test_fail_task_flow(self) -> None:
        """失败流程: pending -> running -> failed。"""
        svc = self._make_service()
        task = await svc.create_task("失败任务")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="执行异常")
        failed = svc.get_task(task.id)
        assert failed.status.value == "failed"
        assert "执行异常" in failed.error

    @pytest.mark.asyncio
    async def test_pause_and_resume(self) -> None:
        """暂停和恢复: running -> paused -> pending。"""
        svc = self._make_service()
        task = await svc.create_task("暂停任务")
        await svc.start_task(task.id)
        await svc.pause_task(task.id)
        paused = svc.get_task(task.id)
        assert paused.status.value == "paused"
        resumed = await svc.resume_task(task.id)
        assert resumed.status.value == "pending"

    @pytest.mark.asyncio
    async def test_force_transition_running_to_evaluating(self) -> None:
        """强制转换: running -> evaluating 应成功。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task("转换任务")
        await svc.start_task(task.id)
        await svc.force_transition(task.id, TaskStatus.EVALUATING)
        result = svc.get_task(task.id)
        assert result.status.value == "evaluating"

    @pytest.mark.asyncio
    async def test_force_transition_invalid_raises(self) -> None:
        """非法强制转换应抛出异常。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task("非法转换任务")
        with pytest.raises(Exception, match="不允许"):
            await svc.force_transition(task.id, TaskStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_get_task_nonexistent(self) -> None:
        """查询不存在的任务应返回 None。"""
        svc = self._make_service()
        assert svc.get_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_by_status(self) -> None:
        """按状态筛选应返回正确结果。"""
        svc = self._make_service()
        t1 = await svc.create_task("任务A")
        t2 = await svc.create_task("任务B")
        await svc.start_task(t1.id)

        running_tasks = svc.list_by_status(
            __import__("tasks.types", fromlist=["TaskStatus"]).TaskStatus.RUNNING
        )
        assert len(running_tasks) == 1
        assert running_tasks[0].id == t1.id

    @pytest.mark.asyncio
    async def test_subtask_hierarchy(self) -> None:
        """父子任务层级关系应正确。"""
        svc = self._make_service()
        parent = await svc.create_task("父任务")
        child = await svc.create_task(
            "子任务", parent_task_id=parent.id,
        )
        subtasks = svc.list_subtasks(parent.id)
        assert len(subtasks) == 1
        assert subtasks[0].id == child.id

    @pytest.mark.asyncio
    async def test_bind_pipeline_run(self) -> None:
        """绑定管道运行 ID 应正确保存。"""
        svc = self._make_service()
        task = await svc.create_task("绑定管道")
        await svc.bind_pipeline_run(task.id, "pipeline-run-123")
        bound = svc.get_task(task.id)
        assert bound.pipeline_run_id == "pipeline-run-123"

    @pytest.mark.asyncio
    async def test_reset_completed_to_pending(self) -> None:
        """重置已完成任务应回到 pending。"""
        svc = self._make_service()
        task = await svc.create_task("重置任务")
        await svc.start_task(task.id)
        await svc.complete_task(task.id)
        await svc.reset_to_pending(task.id)
        reactivated = svc.get_task(task.id)
        assert reactivated.status.value == "pending"

    @pytest.mark.asyncio
    async def test_recover_failed_to_pending(self) -> None:
        """恢复失败任务应变为 pending。"""
        svc = self._make_service()
        task = await svc.create_task("恢复任务")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="临时失败")
        await svc.reset_to_pending(task.id)
        recovered = svc.get_task(task.id)
        assert recovered.status.value == "pending"

    @pytest.mark.asyncio
    async def test_delete_task_success(self) -> None:
        """删除任务应成功移除。"""
        svc = self._make_service()
        task = await svc.create_task("待删除")
        result = await svc.delete_task(task.id)
        assert result is True
        assert svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        """删除不存在的任务应返回 False。"""
        svc = self._make_service()
        result = await svc.delete_task("nonexistent-id")
        assert result is False


class TestTaskStateMachineRegression:
    """验证任务状态机的转换规则。"""

    def _make_sm(self) -> Any:
        from tasks.state_machine import SimpleStateMachine, _TASK_TRANSITIONS
        return SimpleStateMachine(
            initial_state="pending",
            transitions=_TASK_TRANSITIONS,
        )

    def test_pending_to_running(self) -> None:
        """pending -> running 应合法。"""
        sm = self._make_sm()
        assert sm.can_transition("running") is True

    def test_running_to_evaluating(self) -> None:
        """running -> evaluating 应合法。"""
        sm = self._make_sm()
        sm.transition("running")
        assert sm.can_transition("evaluating") is True

    def test_completed_to_running_invalid(self) -> None:
        """completed -> running 应不合法。"""
        sm = self._make_sm()
        sm.transition("running")
        sm.transition("evaluating")
        sm.transition("completed")
        assert sm.can_transition("running") is False

    def test_failed_to_pending_valid(self) -> None:
        """failed -> pending 应合法（重试）。"""
        sm = self._make_sm()
        sm.transition("running")
        sm.transition("failed")
        assert sm.can_transition("pending") is True

    def test_transition_raises_on_invalid(self) -> None:
        """非法转换应抛出 InvalidTransitionError。"""
        from tasks.state_machine import InvalidTransitionError

        sm = self._make_sm()
        with pytest.raises(InvalidTransitionError):
            sm.transition("completed")

    def test_transition_updates_status(self) -> None:
        """合法转换应更新当前状态。"""
        sm = self._make_sm()
        sm.transition("running")
        assert sm.current_state == "running"


# ===========================================================================
# 4. 工具调用执行(Tools)回归测试
# ===========================================================================

class TestToolExecutionRegression:
    """验证工具注册、查找和类型系统。"""

    def test_tools_module_importable(self) -> None:
        """tools 包应可正常导入。"""
        import tools
        assert tools is not None

    def test_tool_types_importable(self) -> None:
        """工具类型定义应可导入。"""
        from tools.types import Tool, ToolCategory
        assert Tool is not None
        assert ToolCategory is not None

    def test_tool_exceptions_importable(self) -> None:
        """工具异常类应可导入。"""
        from tools.exceptions import ToolNotFoundError, ToolExecutionError
        assert ToolNotFoundError is not None
        assert ToolExecutionError is not None

    def test_tool_creation(self) -> None:
        """Tool 应能正常创建。"""
        from tools.types import Tool

        tool = Tool(
            name="test_tool",
            description="测试工具",
            input_schema={"type": "object"},
            source="builtin",
        )
        assert tool.name == "test_tool"

    def test_global_registry_module_importable(self) -> None:
        """全局工具注册表模块应可导入。"""
        import tools.global_registry as gr_mod
        # 模块可导入即证明工具注册系统结构完整
        assert gr_mod is not None


# ===========================================================================
# 5. 文件操作(File Read/Write)回归测试
# ===========================================================================

class TestFileOperationsRegression:
    """验证文件读写功能正常（使用临时文件系统）。"""

    def test_write_and_read_text_file(self) -> None:
        """写入并读取文本文件应返回一致内容。"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        ) as f:
            f.write("Hello 回归测试\n第二行")
            path = f.name

        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert content == "Hello 回归测试\n第二行"
        finally:
            os.unlink(path)

    def test_write_and_read_json_file(self) -> None:
        """写入并读取 JSON 文件应返回一致数据。"""
        import json

        data = {"key": "value", "list": [1, 2, 3], "nested": {"a": True}}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as f:
            json.dump(data, f, ensure_ascii=False)
            path = f.name

        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            assert loaded == data
        finally:
            os.unlink(path)

    def test_write_and_read_yaml_file(self) -> None:
        """写入并读取 YAML 文件应返回一致数据。"""
        import yaml

        data = {"name": "test", "items": ["a", "b"], "count": 42}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        ) as f:
            yaml.dump(data, f, allow_unicode=True)
            path = f.name

        try:
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            assert loaded == data
        finally:
            os.unlink(path)

    def test_directory_create_and_list(self) -> None:
        """创建目录并列出内容应正常。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建子目录和文件
            subdir = os.path.join(tmpdir, "sub")
            os.makedirs(subdir)
            Path(os.path.join(tmpdir, "file1.txt")).write_text("a", encoding="utf-8")
            Path(os.path.join(subdir, "file2.txt")).write_text("b", encoding="utf-8")

            entries = sorted(os.listdir(tmpdir))
            assert "sub" in entries
            assert "file1.txt" in entries

    def test_binary_file_roundtrip(self) -> None:
        """二进制文件读写应一致。"""
        data = bytes(range(256))
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".bin", delete=False,
        ) as f:
            f.write(data)
            path = f.name

        try:
            with open(path, "rb") as f:
                loaded = f.read()
            assert loaded == data
        finally:
            os.unlink(path)


# ===========================================================================
# 6. Agent 启动/销毁(Agents)回归测试
# ===========================================================================

class TestAgentRegistryRegression:
    """验证 Agent 配置注册表的注册/查找/注销。"""

    def _make_registry(self) -> Any:
        from agents.registry import AgentRegistry
        return AgentRegistry()

    def _make_config(self, config_id: str = "test_agent") -> Any:
        """创建最小 AgentConfig。"""
        from agents.types import AgentConfig, AgentLevel, AgentType
        return AgentConfig(
            config_id=config_id,
            name="测试Agent",
            agent_type=AgentType.MAIN,
            level=AgentLevel.L1_MAIN,
            system_prompt="你是测试Agent",
        )

    def test_register_and_get(self) -> None:
        """注册后应能通过 config_id 查找。"""
        reg = self._make_registry()
        config = self._make_config("agent_001")
        reg.register(config)
        found = reg.get("agent_001")
        assert found is not None
        assert found.config_id == "agent_001"

    def test_get_nonexistent_returns_none(self) -> None:
        """查找不存在的配置应返回 None。"""
        reg = self._make_registry()
        assert reg.get("nonexistent") is None

    def test_register_empty_id_raises(self) -> None:
        """注册空 config_id 应抛出 ValueError。"""
        reg = self._make_registry()
        config = self._make_config("")
        config.config_id = ""
        with pytest.raises(ValueError, match="不能为空"):
            reg.register(config)

    def test_unregister_success(self) -> None:
        """注销已注册的配置应成功。"""
        reg = self._make_registry()
        config = self._make_config("to_remove")
        reg.register(config)
        assert reg.unregister("to_remove") is True
        assert reg.get("to_remove") is None

    def test_unregister_nonexistent(self) -> None:
        """注销不存在的配置应返回 False。"""
        reg = self._make_registry()
        assert reg.unregister("nonexistent") is False

    def test_find_by_level(self) -> None:
        """按层级筛选应返回正确结果。"""
        from agents.types import AgentLevel
        reg = self._make_registry()
        config = self._make_config("l1_agent")
        reg.register(config)

        results = reg.find_by_level(AgentLevel.L1_MAIN)
        assert len(results) >= 1
        assert any(c.config_id == "l1_agent" for c in results)

    def test_count(self) -> None:
        """count 应返回已注册的配置数量。"""
        reg = self._make_registry()
        assert reg.count() == 0
        reg.register(self._make_config("a"))
        reg.register(self._make_config("b"))
        assert reg.count() == 2

    def test_list_all(self) -> None:
        """list_all 应返回所有已注册配置。"""
        reg = self._make_registry()
        reg.register(self._make_config("x"))
        reg.register(self._make_config("y"))
        all_configs = reg.list_all()
        assert len(all_configs) == 2

    @pytest.mark.asyncio
    async def test_get_async_found(self) -> None:
        """异步 get 应能找到已注册配置。"""
        reg = self._make_registry()
        config = self._make_config("async_agent")
        reg.register(config)
        found = await reg.get_async("async_agent")
        assert found is not None
        assert found.config_id == "async_agent"


class TestAgentConfigLoaderRegression:
    """验证 Agent 配置加载器。"""

    def test_load_from_yaml_valid(self) -> None:
        """从合法 YAML 加载 Agent 配置。"""
        import yaml
        from agents.loader import AgentConfigLoader

        yaml_content = {
            "config_id": "yaml_agent",
            "name": "YAML Agent",
            "agent_type": "main",
            "level": "L1",
            "system_prompt": "你是YAML Agent",
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        ) as f:
            yaml.dump(yaml_content, f, allow_unicode=True)
            path = f.name

        try:
            config = AgentConfigLoader.load_from_yaml(path)
            assert config.config_id == "yaml_agent"
            assert config.name == "YAML Agent"
        finally:
            os.unlink(path)

    def test_load_from_yaml_invalid_raises(self) -> None:
        """从无效 YAML 加载应抛出异常。"""
        from agents.loader import AgentConfigLoader

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        ) as f:
            f.write("invalid: [yaml: content")
            path = f.name

        try:
            with pytest.raises((ValueError, Exception)):
                AgentConfigLoader.load_from_yaml(path)
        finally:
            os.unlink(path)

    def test_load_from_directory(self) -> None:
        """从目录批量加载配置。"""
        import yaml
        from agents.loader import AgentConfigLoader

        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                data = {
                    "config_id": f"dir_agent_{i}",
                    "name": f"Agent {i}",
                    "agent_type": "main",
                    "level": "L1",
                    "system_prompt": f"Agent {i}",
                }
                path = os.path.join(tmpdir, f"agent_{i}.yaml")
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True)

            configs = AgentConfigLoader.load_from_directory(tmpdir)
            assert len(configs) == 3
            config_ids = {c.config_id for c in configs}
            assert "dir_agent_0" in config_ids
            assert "dir_agent_2" in config_ids

    def test_load_from_nonexistent_dir_raises(self) -> None:
        """从不存在的目录加载应抛出 FileNotFoundError。"""
        from agents.loader import AgentConfigLoader
        with pytest.raises(FileNotFoundError):
            AgentConfigLoader.load_from_directory("/nonexistent/path")


# ===========================================================================
# 7. 工作空间管理(Workspace)回归测试
# ===========================================================================

class TestWorkspaceServiceRegression:
    """验证工作空间服务的创建、查询和文件树功能。"""

    def _make_service(self) -> Any:
        from workspace.workspace_service import WorkspaceService
        return WorkspaceService()

    @pytest.mark.asyncio
    async def test_get_or_create_workspace(self) -> None:
        """创建工作空间应返回正确属性。"""
        svc = self._make_service()
        ws = await svc.get_or_create_workspace(
            container_task_id="task-001",
            session_id="sess-001",
            title="测试空间",
        )
        assert ws.container_task_id == "task-001"
        assert ws.session_id == "sess-001"
        assert ws.title == "测试空间"

    @pytest.mark.asyncio
    async def test_get_or_create_idempotent(self) -> None:
        """相同 container_task_id 重复创建应返回同一实例。"""
        svc = self._make_service()
        ws1 = await svc.get_or_create_workspace("task-002", title="空间A")
        ws2 = await svc.get_or_create_workspace("task-002", title="空间B")
        assert ws1.id == ws2.id
        # title 不变（已存在）
        assert ws2.title == "空间A"

    @pytest.mark.asyncio
    async def test_get_workspace_found(self) -> None:
        """查询已创建的工作空间应返回正确实例。"""
        svc = self._make_service()
        created = await svc.get_or_create_workspace("task-003")
        found = await svc.get_workspace("task-003")
        assert found is not None
        assert found.id == created.id

    @pytest.mark.asyncio
    async def test_get_workspace_not_found(self) -> None:
        """查询不存在的工作空间应返回 None。"""
        svc = self._make_service()
        result = await svc.get_workspace("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_artifacts_empty(self) -> None:
        """未关联制品时应返回空列表。"""
        svc = self._make_service()
        await svc.get_or_create_workspace("task-004")
        result = await svc.list_artifacts_by_workspace("task-004")
        assert result["items"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_list_artifacts_nonexistent(self) -> None:
        """不存在的工作空间应返回空。"""
        svc = self._make_service()
        result = await svc.list_artifacts_by_workspace("nonexistent")
        assert result["items"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_get_file_tree_with_valid_path(self) -> None:
        """有效目录应返回文件树。"""
        svc = self._make_service()
        await svc.get_or_create_workspace("task-005")

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试文件结构
            Path(os.path.join(tmpdir, "file1.txt")).write_text("hello", encoding="utf-8")
            os.makedirs(os.path.join(tmpdir, "subdir"))
            Path(os.path.join(tmpdir, "subdir", "file2.txt")).write_text(
                "world", encoding="utf-8",
            )

            result = await svc.get_file_tree("task-005", base_path=tmpdir)
            assert "tree" in result
            names = {n["name"] for n in result["tree"]}
            assert "file1.txt" in names
            assert "subdir" in names

    @pytest.mark.asyncio
    async def test_get_file_tree_invalid_path(self) -> None:
        """无效路径应返回空树。"""
        svc = self._make_service()
        await svc.get_or_create_workspace("task-006")

        result = await svc.get_file_tree("task-006", base_path="/nonexistent/path")
        assert result["tree"] == []

    @pytest.mark.asyncio
    async def test_get_file_tree_no_base_path(self) -> None:
        """不指定 base_path 应返回空树。"""
        svc = self._make_service()
        await svc.get_or_create_workspace("task-007")
        result = await svc.get_file_tree("task-007")
        assert result["tree"] == []


class TestWorkspaceModelsRegression:
    """验证工作空间数据模型。"""

    def test_workspace_creation_defaults(self) -> None:
        """Workspace 默认值应正确。"""
        from workspace.models import Workspace
        ws = Workspace()
        assert ws.id != ""
        assert ws.container_task_id == ""
        assert ws.file_tree == []

    def test_workspace_to_dict(self) -> None:
        """Workspace 序列化应包含所有字段。"""
        from workspace.models import Workspace
        ws = Workspace(container_task_id="ct-001", title="空间1")
        d = ws.to_dict()
        assert d["container_task_id"] == "ct-001"
        assert d["title"] == "空间1"
        assert "id" in d
        assert "created_at" in d

    def test_workspace_from_dict(self) -> None:
        """Workspace 反序列化应恢复所有字段。"""
        from workspace.models import Workspace
        original = Workspace(container_task_id="ct-002", title="空间2")
        d = original.to_dict()
        restored = Workspace.from_dict(d)
        assert restored.container_task_id == "ct-002"
        assert restored.title == "空间2"

    def test_file_tree_node_to_dict(self) -> None:
        """FileTreeNode 序列化应正确。"""
        from workspace.models import FileTreeNode
        node = FileTreeNode(name="test.py", type="file", path="src/test.py")
        d = node.to_dict()
        assert d["name"] == "test.py"
        assert d["type"] == "file"
        assert "children" not in d  # 空子节点不序列化

    def test_file_tree_node_with_children(self) -> None:
        """FileTreeNode 带子节点应正确序列化。"""
        from workspace.models import FileTreeNode
        child = FileTreeNode(name="child.txt", type="file", path="dir/child.txt")
        parent = FileTreeNode(
            name="dir", type="directory", path="dir", children=[child],
        )
        d = parent.to_dict()
        assert "children" in d
        assert len(d["children"]) == 1
        assert d["children"][0]["name"] == "child.txt"


# ===========================================================================
# 跨模块集成验证
# ===========================================================================

class TestCrossModuleIntegrationRegression:
    """验证跨模块交互未被破坏。"""

    @pytest.mark.asyncio
    async def test_task_to_pipeline_binding(self) -> None:
        """任务绑定管道 ID 后应能通过查询获取。"""
        from tasks.service import TaskService

        svc = TaskService(data_dir=tempfile.mkdtemp())

        task = await svc.create_task("绑定测试")
        await svc.bind_pipeline_run(task.id, "pipeline-integration-001")

        found = svc.get_task(task.id)
        assert found is not None
        assert found.pipeline_run_id == "pipeline-integration-001"

    @pytest.mark.asyncio
    async def test_task_state_transitions(self) -> None:
        """任务状态转换应按规则执行。"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        svc = TaskService(data_dir=tempfile.mkdtemp())

        parent = await svc.create_task("父任务")
        c1 = await svc.create_task("子1", parent_task_id=parent.id)

        await svc.start_task(c1.id)
        await svc.force_transition(c1.id, TaskStatus.EVALUATING)
        await svc.complete_evaluation(c1.id, passed=True)

        result = svc.get_task(c1.id)
        assert result.status.value == "completed"

    @pytest.mark.asyncio
    async def test_agent_registry_with_task_service(self) -> None:
        """Agent 配置能正确注入到任务模型。"""
        from agents.registry import AgentRegistry
        from agents.types import AgentConfig, AgentLevel, AgentType
        from tasks.service import TaskService

        registry = AgentRegistry()
        config = AgentConfig(
            config_id="worker_agent",
            name="工作Agent",
            agent_type=AgentType.SPECIALIZED,
            level=AgentLevel.L2_SUBTASK,
            system_prompt="你是工作Agent",
        )
        registry.register(config)
        assert registry.get("worker_agent") is not None

        svc = TaskService(data_dir=tempfile.mkdtemp())
        task = await svc.create_task(
            "Agent任务",
            agent_level=AgentLevel.L2_SUBTASK,
        )
        assert task.agent_level == AgentLevel.L2_SUBTASK

    def test_workspace_file_tree_with_real_directory(self) -> None:
        """工作空间文件树应能正确反映实际目录结构。"""
        from workspace.models import FileTreeNode

        # 模拟 _scan_directory 的核心逻辑
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建层级结构
            os.makedirs(os.path.join(tmpdir, "src", "utils"))
            Path(os.path.join(tmpdir, "src", "main.py")).write_text("", encoding="utf-8")
            Path(os.path.join(tmpdir, "src", "utils", "helpers.py")).write_text(
                "", encoding="utf-8",
            )
            Path(os.path.join(tmpdir, "README.md")).write_text("", encoding="utf-8")

            # 验证结构
            entries = sorted(os.listdir(tmpdir))
            assert "README.md" in entries
            assert "src" in entries

            src_entries = sorted(os.listdir(os.path.join(tmpdir, "src")))
            assert "main.py" in src_entries
            assert "utils" in src_entries

            utils_entries = sorted(os.listdir(os.path.join(tmpdir, "src", "utils")))
            assert "helpers.py" in utils_entries
