# @feature: FP-MIGR 0.1→0.2迁移 | @ci: python-coverage
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
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# 插件目录（fixture 内注入 sys.path 并在 teardown 恢复——模块级插入会污染
# 同进程其它测试：tasks/workspace.py 与 system/workspace/ 包对 `import workspace`
# 解析冲突，破坏 channel_api 的 routes_workspaces 等）
_PLUGIN_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出被其它插件测试污染的同名裸模块，确保本插件目录的模块按自身 sys.path 解析。

    0.2 各插件平铺 import（from service import ...），同名模块在跨文件收集时会
    互相覆盖 sys.modules 缓存：例如 security_check 插件的 service.py 先被收集导入后，
    本测试 `from service import TaskService` 会命中错误模块而 ImportError。conftest 的
    逐出仅在收集时生效，而本文件的裸模块导入发生在测试运行期（函数体内），故在此
    每个测试前重新置本插件目录于 sys.path 最前，并逐出本插件用到的同名裸模块，
    强制按本目录重新解析。Windows/Ubuntu 通用。

    http_api 亦逐出：triggers_ext/http_api.py 同名模块若先被其它测试缓存，
    本插件测试运行期 `import http_api` 会命中错误副本（AttributeError:
    no attribute '_task_to_response'/'_capability'）。

    teardown 除恢复 sys.path 外**还原逐出前的模块代际**：本 fixture 的逐出+重导
    会让 task_types 等"全仓唯一源"模块产生同源双份（新代际覆盖 sys.modules 缓存），
    若不还原，后续文件的模块级绑定（如 tools/task/tool.py 收集期绑定的
    TaskStatus）与新代际做枚举身份比较会失配——合并跑「单文件绿合并红」的
    串扰根因之一。还原后进程回到本文件运行前的模块状态。
    """
    d = str(_PLUGIN_DIR)
    _was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    _evict_names = (
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
        "http_api",
    )
    _evicted: dict[str, ModuleType] = {}
    for m in _evict_names:
        if m in sys.modules:
            _evicted[m] = sys.modules.pop(m)
    yield
    # teardown：恢复 sys.path（仅移除本 fixture 插入的项）
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)
    # 还原逐出前的模块代际（本测试期间重导的同名模块一并回滚）
    for m in _evict_names:
        if m in _evicted:
            sys.modules[m] = _evicted[m]
        else:
            sys.modules.pop(m, None)


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
        tools = config.get("capabilities", {}).get("services", [])
        assert len(tools) > 0, "Must have at least one tool"

    def test_has_lifecycle_hooks(self, config: dict) -> None:
        """声明了生命周期钩子。"""
        hooks = config.get("capabilities", {}).get("lifecycle_hooks", [])
        assert "on_load" in hooks
        assert "on_unload" in hooks

    def test_tools_cover_core_operations(self, config: dict) -> None:
        """工具列表覆盖核心操作（create/get/transition/list/cancel）。"""
        tool_names = {t["name"] for t in config["capabilities"]["services"]}
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
# 6. State Machine 验证（SimpleStateMachine 已删除，仅保留异常与转换表）
# ═══════════════════════════════════════════════════════════

class TestStateMachine:
    """状态机异常与转换表定义（SimpleStateMachine 类已随死代码清理移除）。"""

    def test_invalid_transition_error_importable(self) -> None:
        """InvalidTransitionError 仍可导入（service 层依赖）。"""
        from state_machine import InvalidTransitionError
        assert issubclass(InvalidTransitionError, Exception)

    def test_all_status_transitions_defined(self) -> None:
        """7 种状态全部在 _TASK_TRANSITIONS 转换表中有定义。"""
        from state_machine import _TASK_TRANSITIONS

        expected_states = {"pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"}
        defined = set(_TASK_TRANSITIONS.keys())
        assert expected_states.issubset(defined), f"Missing states: {expected_states - defined}"


# ── state 聚合出口：state 行优先、owned 行兜底（2026-08-23 归属链修复）────
# 有父任务会同时有：自己管道的 state 行（task.* + lineage.parent_pipeline_id）
# 和提交者管道的 task.owned.<id>.* 登记键。出口必须以 state 行为准（归属/状态
# 更真），owned 行只兜底无 state 行的任务（容器任务/出生字段丢失的存量任务），
# 否则同 id 双行 + 前端 taskById 覆盖 + 面板重复节点。
class TestListTasksFromStateDedup:
    def _rows(self) -> list[dict]:
        return [
            # 子任务自己的执行管道行（state 真值：父归属 + 真实 scope）
            {
                "pipeline_id": "child-pipe",
                "lineage.parent_pipeline_id": "parent-pipe",
                "task.goal": "子任务",
                "task.status": "running",
                "task.scope": "non_container",
                "task.submitted_by": "u1",
            },
            # 提交者管道行（task.owned 登记：与 state 行同 id + 一个纯容器声明）
            {
                "pipeline_id": "parent-pipe",
                "task.owned.child-pipe.title": "子任务",
                "task.owned.child-pipe.status": "running",
                "task.owned.child-pipe.scope": "non_container",
                "task.owned.proj-1.title": "容器项目",
                "task.owned.proj-1.status": "active",
                # proj-1 无 scope 键 → 容器缺省
            },
        ]

    async def test_state_row_wins_over_owned_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        rows = self._rows()

        class FakeState:
            async def call(self, _name: str, _params: dict) -> list:
                return rows

        monkeypatch.setattr(http_api, "_capability", lambda _name: FakeState())
        out = await http_api._list_tasks_from_state()
        assert out is not None, "capability 可用时聚合不得降级为 None"
        by_id = {str(t["id"]): t for t in out}
        # 子任务只出一行（state 行），不与 owned 登记重复
        assert len([t for t in out if str(t["id"]) == "child-pipe"]) == 1
        child = by_id["child-pipe"]
        assert child["parent_task_id"] == "parent-pipe"
        # 登记声明（无 state 行）仍出口（scope 键已随容器任务退役）
        proj = by_id["proj-1"]
        assert proj["parent_task_id"] is None


# ── 归属会话透传（ADR 2026-08-21）────────────────────────────────
# 手动创建任务把用户会话随 chat.send_message 创建参数透传：内核创建分支以真实
# 会话 link pipeline_sessions（runs 快照/前端导航据此归属）；响应层 thread_id
# 从 metadata.session_id 回退（任务记录只写 metadata.session_id）。
class TestOwnershipSessionFlow:
    def test_task_response_thread_id_falls_back_to_metadata_session(self) -> None:
        from http_api import _task_to_response

        resp = _task_to_response({"id": "t1", "title": "T", "metadata": {"session_id": "thread-s1"}})
        assert resp.thread_id == "thread-s1"

    def test_task_response_keeps_top_level_thread_id(self) -> None:
        from http_api import _task_to_response

        resp = _task_to_response(
            {"id": "t1", "title": "T", "thread_id": "thread-s2", "metadata": {"session_id": "thread-s1"}}
        )
        assert resp.thread_id == "thread-s2"

    async def test_submit_task_event_inject_mode_reruns_existing_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注入模式（task_id 非空）：以 task_id 作 pipeline_id 走注入分支。"""
        import http_api

        calls: list[dict] = []

        class FakeChat:
            async def call(self, name: str, params: dict) -> dict:
                calls.append(params)
                return {"pipeline_id": params.get("pipeline_id", "")}

        monkeypatch.setattr(http_api, "_capability", lambda _name: FakeChat())

        pid = await http_api._submit_task_event(title="任务", task_id="pipe-9")
        assert pid == "pipe-9"
        assert len(calls) == 1
        assert calls[0]["pipeline_id"] == "pipe-9"
        assert calls[0]["background"] is True
        assert "create" not in calls[0]

    async def test_submit_task_event_inject_mode_missing_pipeline_id_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        class FakeChat:
            async def call(self, name: str, params: dict) -> dict:
                return {"status": "dispatched"}

        monkeypatch.setattr(http_api, "_capability", lambda _name: FakeChat())

        assert await http_api._submit_task_event(title="任务", task_id="pipe-9") == ""

    async def test_submit_task_event_capability_missing_raises_birth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """chat capability 未注入 → TaskBirthError（不降级不吞异常）。"""
        import http_api
        from task_birth import TaskBirthError

        def _missing(name: str):
            raise KeyError(name)

        monkeypatch.setattr(http_api, "_capability", _missing)
        with pytest.raises(TaskBirthError, match="chat capability 未注入"):
            await http_api._submit_task_event(title="任务", task_id="pipe-9", user_id="u1")

    def test_capability_takes_handle_from_main_plugin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_capability 从 __main__.plugin 取句柄（sidecar 启动实例）。

        import server 会重新执行 server.py 顶层、得到第二个空 AgentOSPlugin
        （capabilities 永远为空）——回归防护：必须走 __main__。
        """
        import http_api

        fake = object()

        class FakeMain:
            plugin = type(
                "Plugin",
                (),
                {
                    "get_capability": lambda self, n: (
                        fake if n == "pipeline-state" else (_ for _ in ()).throw(KeyError(n))
                    )
                },
            )()

        monkeypatch.setitem(sys.modules, "__main__", FakeMain)
        assert http_api._capability("pipeline-state") is fake
        with pytest.raises(KeyError):
            http_api._capability("chat")


class TestPanelCreateViaTaskSubmitTool:
    """面板创建 = task_submit 工具的表单提交（单一业务入口）。

    断言可观察行为：HTTP 端点把表单映射为工具入参经 tool-executor 提交，
    人类注入参数（parent_agent_level=1 + 认证 user_id）随行；工具拒绝信封
    映射为带 error_code 的 APIError（fail-closed，不降级不吞）。
    """

    @staticmethod
    def _fake_executor(calls: list[dict], envelope: dict) -> Any:
        class FakeExecutor:
            async def call(
                self, method: str, params: dict, timeout: float | None = None
            ) -> dict:
                assert method == "invoke"
                calls.append(params)
                return envelope

        def _cap(name: str) -> Any:
            if name == "tool-executor":
                return FakeExecutor()
            raise KeyError(name)

        return _cap

    async def test_create_task_maps_form_to_tool_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        calls: list[dict] = []
        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                calls,
                {
                    "success": True,
                    "output": {"task_id": "abc123def456", "title": "T", "status": "running"},
                },
            ),
        )

        resp = await http_api.create_task(
            {"title": "T", "description": "D", "agent_id": "general_agent"},
            {"sub": "user-1", "username": "alice"},
        )

        assert len(calls) == 1
        invoke = calls[0]
        assert invoke["tool_name"] == "task_submit"
        assert "plugin_id" not in invoke  # 注册表反查，不硬编码插件 id
        args = invoke["args"]
        assert args["goal_title"] == "T"
        assert args["goal_description"] == "D"
        assert args["target_type"] == "agent"
        assert args["target_id"] == "general_agent"
        assert args["parent_agent_level"] == 1  # 人类 = L1 之上（闸门按 L1 放行）
        assert args["user_id"] == "user-1"  # 认证身份作提交者
        assert resp.id == "abc123def456"

    async def test_create_task_tool_rejection_maps_to_api_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """工具拒绝信封 → APIError 400，error_code/message 原样透传。"""
        import http_api

        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                [],
                {
                    "success": False,
                    "error": "必须提供任务描述（goal_description，1-2000 字符）",
                    "error_code": "MISSING_DESCRIPTION",
                },
            ),
        )

        from http_api import APIError

        with pytest.raises(APIError) as ei:
            await http_api.create_task({"title": "T", "agent_id": "general_agent"})
        assert ei.value.status_code == 400
        assert ei.value.error_code == "MISSING_DESCRIPTION"
        assert "任务描述" in ei.value.message

    async def test_create_task_dispatch_failed_maps_to_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """派发失败（管道未出生）= 服务端故障面，映射 500 而非 400。"""
        import http_api

        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                [],
                {"success": False, "error": "执行管道未能创建", "error_code": "DISPATCH_FAILED"},
            ),
        )

        from http_api import APIError

        with pytest.raises(APIError) as ei:
            await http_api.create_task(
                {"title": "T", "description": "D", "agent_id": "general_agent"}
            )
        assert ei.value.status_code == 500
        assert ei.value.error_code == "DISPATCH_FAILED"

    async def test_create_task_capability_missing_maps_to_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        def _missing(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(http_api, "_capability", _missing)

        from http_api import APIError

        with pytest.raises(APIError) as ei:
            await http_api.create_task(
                {"title": "T", "description": "D", "agent_id": "general_agent"}
            )
        assert ei.value.status_code == 503

    async def test_create_root_task_maps_workspace_mode_thread_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """根任务表单全字段映射：workspace_mode 此前被静默丢弃，现必须透传。"""
        import http_api

        calls: list[dict] = []
        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                calls,
                {"success": True, "output": {"task_id": "def456abc123", "title": "R"}},
            ),
        )

        resp = await http_api.create_root_task(
            {
                "title": "R",
                "description": "D",
                "target_id": "general_agent",
                "workspace": "D:/proj",
                "workspace_mode": "plain",
                "isolation_level": "isolated",
                "project_id": "proj00112233",
                "thread_id": "thread-s1",
            }
        )

        args = calls[0]["args"]
        assert args["workspace_mode"] == "plain"
        assert args["isolation_level"] == "isolated"
        assert args["workspace"] == "D:/proj"
        assert args["thread_id"] == "thread-s1"  # 会话归属锚点（工具透传到出生协议）
        assert args["project_id"] == "proj00112233"
        assert resp.id == "def456abc123"

    async def test_create_root_task_maps_project_title_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """新建项目字段透传：project_title/project_path 进工具入参（创建即挂靠）。"""
        import http_api

        calls: list[dict] = []
        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                calls,
                {"success": True, "output": {"task_id": "def456abc123", "title": "R"}},
            ),
        )

        resp = await http_api.create_root_task(
            {
                "title": "R",
                "description": "D",
                "target_id": "general_agent",
                "project_title": "新项目",
                "project_path": "D:/new_proj",
                "thread_id": "thread-s1",
            }
        )

        args = calls[0]["args"]
        assert args["project_title"] == "新项目"
        assert args["project_path"] == "D:/new_proj"
        assert "project_id" not in args
        assert resp.id == "def456abc123"

    async def test_create_root_task_omits_project_title_when_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空新建项目字段不进工具入参（与 project_id 同规则）。"""
        import http_api

        calls: list[dict] = []
        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                calls, {"success": True, "output": {"task_id": "abc123def456"}}
            ),
        )

        await http_api.create_root_task(
            {
                "title": "R",
                "description": "D",
                "target_id": "general_agent",
                "project_title": "",
                "project_path": "",
                "thread_id": "thread-s1",
            }
        )

        args = calls[0]["args"]
        assert "project_title" not in args
        assert "project_path" not in args

    async def test_create_root_task_omits_empty_optionals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空可选字段不进工具入参（inherit 空对象不得触发继承校验分支）。"""
        import http_api

        calls: list[dict] = []
        monkeypatch.setattr(
            http_api,
            "_capability",
            self._fake_executor(
                calls, {"success": True, "output": {"task_id": "abc123def456"}}
            ),
        )

        await http_api.create_root_task(
            {
                "title": "R",
                "description": "D",
                "target_id": "general_agent",
                "thread_id": "thread-s1",
            }
        )

        args = calls[0]["args"]
        assert "project_id" not in args
        assert "inherit" not in args


class TestGetTaskStateRead:
    """任务域单任务读面（get/submit/phase）与列表同源：state 单一真值，无 YAML 兜底。"""

    @staticmethod
    def _state_rows(rows: list[dict[str, Any]] | None) -> Any:
        async def fake_state() -> Any:
            return rows

        return fake_state

    async def test_get_task_serves_state_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        row = {
            "id": "pipe1abc123",
            "title": "T",
            "status": "completed",
            "thread_id": "thread-s1",
            "pipeline_run_id": "pipe1abc123",
            "metadata": {},
        }
        monkeypatch.setattr(
            http_api, "_list_tasks_from_state", self._state_rows([row])
        )

        resp = await http_api.get_task("pipe1abc123")
        assert resp.id == "pipe1abc123"
        assert resp.status == "completed"
        assert resp.thread_id == "thread-s1"

    async def test_get_task_404_when_state_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """state 未出口即 404——任务域唯一数据源，无 YAML 兜底面。"""
        import http_api

        monkeypatch.setattr(http_api, "_list_tasks_from_state", self._state_rows(None))

        from http_api import APIError

        with pytest.raises(APIError) as ei:
            await http_api.get_task("missing12hex")
        assert ei.value.status_code == 404

    async def test_submit_task_state_gate_allows_pending_and_gates_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重跑状态门读 state 行：pending/failed 放行，running 拒绝。"""
        import http_api

        def _row(status: str) -> dict[str, Any]:
            return {"id": "pipe1abc123", "title": "T", "status": status, "metadata": {}}

        calls: list[dict] = []

        class FakeChat:
            async def call(self, name: str, params: dict) -> dict:
                calls.append(params)
                return {"pipeline_id": params.get("pipeline_id", "")}

        monkeypatch.setattr(http_api, "_capability", lambda _name: FakeChat())

        # pending 放行 → 注入派发
        monkeypatch.setattr(
            http_api, "_list_tasks_from_state", self._state_rows([_row("pending")])
        )
        resp = await http_api.submit_task("pipe1abc123", {"sub": "u1"})
        assert calls[0]["pipeline_id"] == "pipe1abc123"
        assert calls[0]["background"] is True
        assert resp.status == "pending"

        # running 拒绝
        monkeypatch.setattr(
            http_api, "_list_tasks_from_state", self._state_rows([_row("running")])
        )
        from http_api import APIError

        with pytest.raises(APIError) as ei:
            await http_api.submit_task("pipe1abc123")
        assert ei.value.status_code == 400
        assert ei.value.error_code == "API_VAL_2003"

    async def test_get_task_phase_maps_state_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import http_api

        monkeypatch.setattr(
            http_api,
            "_list_tasks_from_state",
            self._state_rows([{"id": "pipe1abc123", "title": "T", "status": "running"}]),
        )

        out = await http_api.get_task_phase("pipe1abc123")
        assert out["currentPhase"] == "execute"
        assert out["phaseStatus"] == "running"
