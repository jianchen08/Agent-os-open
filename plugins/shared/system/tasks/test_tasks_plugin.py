# @feature: FP-MIGR 0.1→0.2迁移 | @ci: python-plugins-test
"""tasks 插件迁移验证测试。

验证内容：
1. 文件结构完整性——所有老代码文件已复制平铺
2. plugin.json 格式正确
3. 导入链路正常——所有模块可导入
4. 核心业务逻辑——TaskService CRUD + 状态转换 + 级联取消
5. TimerManager 基本功能
6. server.py 工具注册完整性

[来源: docs/working/module_migration_plan.md §4.1, §10.1]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# 确保插件目录在 sys.path 前面
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出被其它插件测试污染的同名裸模块，确保本插件目录的模块按自身 sys.path 解析。

    0.2 各插件平铺 import（from service import ...），同名模块在跨文件收集时会
    互相覆盖 sys.modules 缓存：例如 security_check 插件的 service.py 先被收集导入后，
    本测试 `from service import TaskService` 会命中错误模块而 ImportError。conftest 的
    逐出仅在收集时生效，而本文件的裸模块导入发生在测试运行期（函数体内），故在此
    每个测试前重新置本插件目录于 sys.path 最前，并逐出本插件用到的同名裸模块，
    强制按本目录重新解析。Windows/Ubuntu 通用。
    """
    d = str(_PLUGIN_DIR)
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    for m in (
        "task_types",
        "state_machine",
        "storage",
        "service",
        "timer_manager",
        "agents_types",
        "enum_utils",
        "workspace",
        "service_access",
        "_task_cleanup",
        "_task_crud",
        "_task_state",
        "server",
    ):
        sys.modules.pop(m, None)
    yield


# ═══════════════════════════════════════════════════════════
# 1. 文件结构验证
# ═══════════════════════════════════════════════════════════

class TestFileStructure:
    """验证迁移后的文件结构完整性。"""

    REQUIRED_FILES = [
        "plugin.json",
        "server.py",
        "task_types.py",
        "state_machine.py",
        "storage.py",
        "service.py",
        "service_access.py",
        "timer_manager.py",
        "workspace.py",
    ]

    def test_plugin_json_exists(self) -> None:
        """plugin.json 存在且非空。"""
        path = _PLUGIN_DIR / "plugin.json"
        assert path.exists(), "plugin.json must exist"
        assert path.stat().st_size > 0, "plugin.json must not be empty"

    @pytest.mark.parametrize("filename", REQUIRED_FILES)
    def test_required_file_exists(self, filename: str) -> None:
        """每个必需文件都存在且非空。"""
        path = _PLUGIN_DIR / filename
        assert path.exists(), f"{filename} must exist in plugin directory"
        assert path.stat().st_size > 0, f"{filename} must not be empty"

    def test_no_extra_src_layer(self) -> None:
        """老代码直接平铺在插件目录根下，没有多余的 src/ 层级。"""
        src_layer = _PLUGIN_DIR / "src"
        assert not src_layer.exists(), "No src/ layer under plugin directory"


# ═══════════════════════════════════════════════════════════
# 2. plugin.json 格式验证
# ═══════════════════════════════════════════════════════════

class TestPluginJson:
    """验证 plugin.json 格式和内容。"""

    @pytest.fixture(scope="class")
    def config(self) -> dict:
        return json.loads((_PLUGIN_DIR / "plugin.json").read_text())

    def test_valid_json(self, config: dict) -> None:
        """plugin.json 是合法 JSON。"""
        assert isinstance(config, dict)

    def test_has_required_fields(self, config: dict) -> None:
        """包含必需字段：id, name, plugin_type, entry。"""
        assert "id" in config
        assert "name" in config
        assert "plugin_type" in config
        assert "entry" in config

    def test_plugin_type_is_system(self, config: dict) -> None:
        """plugin_type 为 system。"""
        assert config["plugin_type"] == "system"

    def test_has_tools(self, config: dict) -> None:
        """声明了工具列表。"""
        tools = config.get("capabilities", {}).get("tools", [])
        assert len(tools) > 0, "Must have at least one tool"

    def test_has_lifecycle_hooks(self, config: dict) -> None:
        """声明了生命周期钩子。"""
        hooks = config.get("capabilities", {}).get("lifecycle_hooks", [])
        assert "on_load" in hooks
        assert "on_unload" in hooks

    def test_tools_cover_core_operations(self, config: dict) -> None:
        """工具列表覆盖核心操作（create/get/transition/list/cancel）。"""
        tool_names = {t["name"] for t in config["capabilities"]["tools"]}
        expected = {"task.create", "task.get", "task.transition", "task.list", "task.cancel"}
        missing = expected - tool_names
        assert not missing, f"Missing core tools: {missing}"


# ═══════════════════════════════════════════════════════════
# 3. 导入链路验证
# ═══════════════════════════════════════════════════════════

class TestImports:
    """验证所有模块导入正常。"""

    def test_import_task_types(self) -> None:
        """task_types.py 可正常导入。"""
        from task_types import TaskModel, TaskPriority, TaskStatus, create_task  # noqa: F401

    def test_import_state_machine(self) -> None:
        """state_machine.py 可正常导入。"""
        from state_machine import SimpleStateMachine, get_task_state_machine  # noqa: F401

    def test_import_storage(self) -> None:
        """storage.py 可正常导入。"""
        from storage import TaskStorage  # noqa: F401

    def test_import_service(self) -> None:
        """service.py 可正常导入（含 mixin 链路）。"""
        from service import TaskService  # noqa: F401

    def test_import_timer_manager(self) -> None:
        """timer_manager.py 可正常导入。"""
        from timer_manager import TimerManager  # noqa: F401

    def test_import_agents_types(self) -> None:
        """agents_types.py 可正常导入（AgentLevel 适配）。"""
        from agents_types import AgentLevel  # noqa: F401


# ═══════════════════════════════════════════════════════════
# 4. TaskService 核心业务逻辑
# ═══════════════════════════════════════════════════════════

def _make_service():
    """创建使用临时目录的 TaskService 实例。"""
    from service import TaskService
    tmp_dir = tempfile.mkdtemp(prefix="test_task_plugin_")
    return TaskService(data_dir=tmp_dir)


class TestTaskServiceCreate:
    """TaskService 创建与查询。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_create_task_defaults(self) -> None:
        """创建任务默认 PENDING 状态。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="测试")
        assert task.status == TaskStatus.PENDING
        assert task.title == "测试"

    @pytest.mark.asyncio
    async def test_create_task_with_parent(self) -> None:
        """创建子任务带 parent_task_id。"""
        parent = await self.svc.create_task(title="父")
        child = await self.svc.create_task(title="子", parent_task_id=parent.id)
        assert child.parent_task_id == parent.id

    def test_get_task_not_found(self) -> None:
        """获取不存在的任务返回 None。"""
        assert self.svc.get_task("不存在") is None

    @pytest.mark.asyncio
    async def test_list_subtasks(self) -> None:
        """列出子任务。"""
        parent = await self.svc.create_task(title="Parent")
        await self.svc.create_task(title="C1", parent_task_id=parent.id)
        await self.svc.create_task(title="C2", parent_task_id=parent.id)
        children = self.svc.list_subtasks(parent.id)
        assert len(children) == 2


class TestTaskServiceTransitions:
    """TaskService 状态转换。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_start_task(self) -> None:
        """pending → running。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="启动")
        await self.svc.start_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_pause_resume(self) -> None:
        """running → stopped → running。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="暂停")
        await self.svc.start_task(task.id)
        await self.svc.pause_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.STOPPED
        await self.svc.resume_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_fail_task_with_reason(self) -> None:
        """running → failed，带错误信息。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="失败")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, reason="出错了")
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.FAILED
        assert fetched.error == "出错了"

    @pytest.mark.asyncio
    async def test_complete_evaluation_passed(self) -> None:
        """evaluating → completed（通过）。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="通过")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=True, result={"score": 0.95})
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED
        assert fetched.result == {"score": 0.95}

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self) -> None:
        """非法转换抛出 InvalidTransitionError。"""
        from state_machine import InvalidTransitionError
        from task_types import TaskStatus
        task = await self.svc.create_task(title="非法")
        with pytest.raises(InvalidTransitionError):
            await self.svc.force_transition(task.id, TaskStatus.EVALUATING)

    @pytest.mark.asyncio
    async def test_reset_to_pending(self) -> None:
        """failed → pending（强制重置）。"""
        from task_types import TaskStatus
        task = await self.svc.create_task(title="重置")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id)
        await self.svc.reset_to_pending(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.PENDING


class TestTaskServiceCascadeCancel:
    """级联取消。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_cascade_cancel(self) -> None:
        """级联取消子任务。"""
        from task_types import TaskStatus
        parent = await self.svc.create_task(title="父")
        c1 = await self.svc.create_task(title="C1", parent_task_id=parent.id)
        c2 = await self.svc.create_task(title="C2", parent_task_id=parent.id)
        await self.svc.start_task(c1.id)
        await self.svc.start_task(c2.id)
        count = await self.svc.cancel_task_cascade(parent.id, reason="测试")
        assert count == 2
        assert self.svc.get_task(c1.id).status == TaskStatus.STOPPED
        assert self.svc.get_task(c2.id).status == TaskStatus.STOPPED

    @pytest.mark.asyncio
    async def test_deeply_nested_cascade(self) -> None:
        """深层嵌套级联取消。"""
        root = await self.svc.create_task(title="根")
        child = await self.svc.create_task(title="子", parent_task_id=root.id)
        grand = await self.svc.create_task(title="孙", parent_task_id=child.id)
        await self.svc.start_task(child.id)
        await self.svc.start_task(grand.id)
        count = await self.svc.cancel_task_cascade(root.id)
        assert count == 2


class TestTaskServiceDelete:
    """删除任务。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_delete_normal(self) -> None:
        """删除普通任务。"""
        task = await self.svc.create_task(title="普通")
        assert await self.svc.delete_task(task.id) is True
        assert self.svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self) -> None:
        """删除不存在的返回 False。"""
        assert await self.svc.delete_task("不存在") is False

    @pytest.mark.asyncio
    async def test_delete_container_soft(self) -> None:
        """容器任务软删除。"""
        container = await self.svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        await self.svc.create_task(title="子", parent_task_id=container.id)
        result = await self.svc.delete_task(container.id)
        assert result is True
        fetched = self.svc.get_task(container.id)
        assert fetched is not None
        assert fetched.metadata.get("soft_deleted") is True


# ═══════════════════════════════════════════════════════════
# 5. TimerManager 验证
# ═══════════════════════════════════════════════════════════

class TestTimerManager:
    """TimerManager 基本功能。"""

    def test_singleton(self) -> None:
        """TimerManager 是单例。"""
        from timer_manager import TimerManager
        TimerManager.reset_instance()
        m1 = TimerManager.get_instance()
        m2 = TimerManager.get_instance()
        assert m1 is m2

    def test_default_config(self) -> None:
        """默认配置加载。"""
        from timer_manager import TimerManager
        TimerManager.reset_instance()
        mgr = TimerManager.get_instance()
        assert mgr.task_max_duration > 0
        assert mgr.idle_threshold > 0

    @pytest.mark.asyncio
    async def test_create_and_cancel_timer(self) -> None:
        """创建和取消计时器。"""
        from timer_manager import TimerManager
        TimerManager.reset_instance()
        mgr = TimerManager.get_instance()
        await mgr.create_timer("test-task-1", timeout=300)
        assert mgr.get_timer_status("test-task-1") is not None
        assert await mgr.cancel_timer("test-task-1") is True
        assert mgr.get_timer_status("test-task-1") is None


# ═══════════════════════════════════════════════════════════
# 6. State Machine 验证
# ═══════════════════════════════════════════════════════════

class TestStateMachine:
    """状态机测试。"""

    def test_valid_transition(self) -> None:
        """合法转换。"""
        from state_machine import get_task_state_machine
        sm = get_task_state_machine()
        assert sm.can_transition("running")
        sm.transition("running")
        assert sm.current_state == "running"

    def test_invalid_transition(self) -> None:
        """非法转换抛出异常。"""
        from state_machine import InvalidTransitionError, get_task_state_machine
        sm = get_task_state_machine()
        # pending → evaluating 是非法的
        assert not sm.can_transition("evaluating")
        with pytest.raises(InvalidTransitionError):
            sm.transition("evaluating")

    def test_all_status_transitions_defined(self) -> None:
        """7 种状态全部在转换表中有定义。"""
        from state_machine import get_task_state_machine

        sm = get_task_state_machine()
        expected_states = {"pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"}
        defined = set(sm.transitions.keys())
        assert expected_states.issubset(defined), f"Missing states: {expected_states - defined}"
