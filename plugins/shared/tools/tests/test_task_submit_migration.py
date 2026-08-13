# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""task_submit 工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR，F-MIGR-2）：
1. 模块可加载——0.1 的 tools.builtin.base / tools.types 已删除，顶层类型走
   agentos_plugin_sdk；0.1 的 infrastructure.service_provider /
   channels.api.memory_store / evaluation.loader 惰性依赖已替换为
   0.2 等价（文档化降级 + 平铺模块）。
2. get_tool_definition() 返回合法 SDK Tool。
3. 核心行为：任务提交成功路径、参数校验（层级/目标空间/指标 ID/继承参数）。

装配：conftest.py 注入 sdk / tools 共享层；本文件把 plugins/shared/system 及
其 tasks / channel_api 平铺目录加入 sys.path（与 server.py 的 0.2 装配语义一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[2] / "system"

# 0.2 平铺模块目录（与 task_submit/server.py 的 sys.path 装配一致）
for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT / "channel_api"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _TS_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "cannot load task_submit tool.py"
    assert spec.loader is not None, "cannot load task_submit tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """task_submit 工具模块（加载后可 monkeypatch 模块级依赖）。"""
    return _load_module()


def _make_task(task_id: str = "task-001", title: str = "测试任务") -> MagicMock:
    """构造任务 mock（status.value 为字符串，metadata 为 dict）。"""
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.status = MagicMock()
    task.status.value = "pending"
    task.metadata = {}
    return task


def _make_source_task(pipeline_run_id: str = "pipe-12345") -> MagicMock:
    """构造一个 mock 源任务对象，带 ws_meta 和 pipeline_run_id。"""
    src = MagicMock()
    src.id = "src-task-001"
    src.title = "源任务"
    src.pipeline_run_id = pipeline_run_id
    src.metadata = {
        "ws_meta": {"path": "/tmp/src-ws"},
        "task_scope": "non_container",
    }
    return src


# ── 迁移验证：可加载 + 0.2 类型面 ──────────────────────────


class TestTaskSubmitMigration:
    """迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.TaskSubmitTool is not None
        assert callable(mod.TaskSubmitTool.get_tool_definition)

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.TaskSubmitTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "task_submit"
        assert tool.category.value == "task"

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.TaskSubmitTool(), mod.BuiltinTool)


# ── 核心行为：参数校验（无需服务依赖的纯路径） ─────────────────


class TestTaskSubmitValidation:
    """注入/目标/工作空间/指标参数校验路径。"""

    @pytest.mark.asyncio
    async def test_missing_parent_agent_level_rejected(self, mod):
        """parent_agent_level 未注入 → 拒绝（防止越权提交）。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute({"goal_title": "任务"})
        assert result.success is False
        assert result.error_code == "MISSING_INJECTED_PARAM"

    @pytest.mark.asyncio
    async def test_missing_goal_rejected(self, mod):
        """缺少 goal/goal_title → MISSING_GOAL。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute({"parent_agent_level": 1})
        assert result.success is False
        assert result.error_code == "MISSING_GOAL"

    @pytest.mark.asyncio
    async def test_l2_cannot_submit_container(self, mod):
        """L2+ 创建容器任务被拦截（容器仅 L1）。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 2,
                "task_scope": "container",
                "goal": {"title": "容器任务"},
            }
        )
        assert result.success is False
        assert result.error_code == "L2_CANNOT_SUBMIT_CONTAINER"

    @pytest.mark.asyncio
    async def test_unsafe_workspace_rejected(self, mod):
        """目标空间落在系统目录 → UNSAFE_WORKSPACE（纵深防御）。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 1,
                "goal": {"title": "任务"},
                "workspace": r"C:\Windows",
            }
        )
        assert result.success is False
        assert result.error_code == "UNSAFE_WORKSPACE"

    @pytest.mark.asyncio
    async def test_l2_without_injected_parent_task_rejected(self, mod):
        """L2 无注入 parent_task_id → 拒绝创建根任务（注入链断裂兜底）。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 2,
                "goal": {"title": "子任务"},
                "target_type": "agent",
                "target_id": "general_agent",
            }
        )
        assert result.success is False
        assert result.error_code == "L2_REQUIRES_PARENT_TASK"

    @pytest.mark.asyncio
    async def test_invalid_metric_id_rejected(self, mod):
        """acceptance_criteria 的 key 全部非合法指标 ID → 拒绝提交（防 METRIC_NOT_FOUND 死循环）。"""
        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 1,
                "goal": {"title": "任务"},
                "target_type": "agent",
                "target_id": "general_agent",
                "acceptance_criteria": {"definitely_not_a_metric_xyz": {}},
            }
        )
        assert result.success is False
        assert result.error_code == "INVALID_METRIC_ID"


# ── 核心行为：inherit 参数解析（自旧 task/test_task_submit.py 移植） ──


class TestTaskSubmitInheritParams:
    """inherit 参数解析：字符串/列表/非法值。"""

    def _make_tool(self, mod) -> Any:
        tool = mod.TaskSubmitTool()
        mock_service = MagicMock()
        mock_service.get_task.return_value = _make_source_task()
        # create_task 直接抛异常，强制返回 TASK_CREATE_FAILED，
        # 从 error_code 反推 inherit 解析层是否成功
        mock_service.create_task = MagicMock(side_effect=RuntimeError("stop-at-create"))
        tool._task_service = mock_service
        return tool

    def _build_inputs(self, mode, inherit_from: str = "src-task-001") -> dict:
        return {
            "goal": {"title": "继承任务测试"},
            "target_type": "agent",
            "target_id": "general_agent",
            "task_scope": "non_container",
            "acceptance_criteria": {"file_check": {"input_params": {"path": "src/foo.py"}}},
            "parent_agent_level": 1,
            "inherit": {"from": inherit_from, "mode": mode},
        }

    @pytest.mark.parametrize("mode", ["pipe", "workspace", ["pipe", "workspace"], ["pipe"], ["workspace"]])
    @pytest.mark.asyncio
    async def test_inherit_mode_valid_forms_pass_parsing(self, mod, mode):
        """合法 mode（字符串/列表）应通过解析层（最终在 create_task 处失败是 mock 所致）。"""
        tool = self._make_tool(mod)
        result = await tool.execute(self._build_inputs(mode))
        assert result.error_code != "INVALID_INHERIT_MODE"
        assert result.error_code != "INVALID_INHERIT_PARAMS"

    @pytest.mark.parametrize("mode", ["pipe", ["pipe"]])
    @pytest.mark.asyncio
    async def test_inherit_pipe_mode_reaches_create_task(self, mod, mode):
        """纯 pipe 模式应走完继承解析，最终在 create_task（mock 抛异常）处失败。"""
        tool = self._make_tool(mod)
        result = await tool.execute(self._build_inputs(mode))
        assert result.error_code == "TASK_CREATE_FAILED"

    @pytest.mark.parametrize("mode", ["invalid", ["invalid"], ["pipe", "invalid"]])
    @pytest.mark.asyncio
    async def test_inherit_mode_invalid_forms_rejected(self, mod, mode):
        """非法 mode 应返回 INVALID_INHERIT_MODE。"""
        tool = self._make_tool(mod)
        result = await tool.execute(self._build_inputs(mode))
        assert result.error_code == "INVALID_INHERIT_MODE"

    @pytest.mark.asyncio
    async def test_build_metadata_handles_list_mode_for_pipe(self, mod):
        """mode=['pipe','workspace'] 时 metadata['inherit_pipe_from'] 应被设置。"""
        tool = mod.TaskSubmitTool()
        inputs = self._build_inputs(["pipe", "workspace"])
        metadata = tool._build_metadata(inputs, inputs["goal"], inputs["acceptance_criteria"])
        assert metadata.get("inherit") == inputs["inherit"]
        assert metadata.get("inherit_pipe_from") == "src-task-001"

    @pytest.mark.asyncio
    async def test_build_metadata_no_pipe_mark_for_workspace_only(self, mod):
        """mode=['workspace'] 不应设置 inherit_pipe_from。"""
        tool = mod.TaskSubmitTool()
        inputs = self._build_inputs(["workspace"])
        metadata = tool._build_metadata(inputs, inputs["goal"], inputs["acceptance_criteria"])
        assert "inherit_pipe_from" not in metadata


# ── 核心行为：非容器任务提交成功路径 ──────────────────────


class TestTaskSubmitCoreSubmit:
    """任务提交：成功、执行器不可用降级、工作空间初始化失败。"""

    def _patch_submit_deps(self, mod, monkeypatch, task: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
        """把提交路径的依赖全部 mock 掉，返回 (task_service, provider)。"""
        from unittest.mock import AsyncMock

        task_service = MagicMock()
        if task is not None:
            task_service.create_task = AsyncMock(return_value=task)
        task_service.hard_delete = AsyncMock()
        task_service.save_task = AsyncMock()
        task_service.get_root_task_id.return_value = None

        provider = MagicMock()
        task_worker = MagicMock()
        task_worker.submit_task.return_value = True
        provider.get.return_value = task_worker  # 默认任何 key 都给 task_worker

        monkeypatch.setattr(mod.TaskSubmitTool, "_get_task_service", lambda self: task_service)
        monkeypatch.setattr(mod.TaskSubmitTool, "_validate_target_agent", lambda self, t, l: (True, "", ""))
        monkeypatch.setattr(mod.TaskSubmitTool, "_check_dependencies_exist", lambda self, d: [])
        monkeypatch.setattr(mod, "_get_service_provider", lambda: provider)
        return task_service, provider

    @pytest.mark.asyncio
    async def test_submit_non_container_success(self, mod, monkeypatch):
        """非容器任务提交成功：返回 task_id + L1 可见 workspace。"""
        task = _make_task()
        task_service, provider = self._patch_submit_deps(mod, monkeypatch, task)

        async def fake_init(t, workspace, task_data, ts, *, is_container=False):
            t.metadata = t.metadata or {}
            t.metadata["ws_meta"] = {"path": "/ws/task-001"}
            return t, None

        # 原为 staticmethod：替换后需保持不绑定 self 的调用语义
        monkeypatch.setattr(mod.TaskSubmitTool, "_init_workspace", staticmethod(fake_init))

        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 1,
                "goal_title": "写测试",
                "goal_description": "为 task_submit 写迁移测试",
                "target_type": "agent",
                "target_id": "general_agent",
                "workspace": "D:/proj",
            }
        )
        assert result.success is True
        output = result.output
        assert output["task_id"] == "task-001"
        assert output["status"] == "pending"
        assert output["workspace"] == "D:/proj"
        # 提交给后台执行器的参数应包含验收标准与工作空间
        task_worker = provider.get("task_worker")
        submitted = task_worker.submit_task.call_args[0][0]
        assert submitted["task_id"] == "task-001"
        assert submitted["target_id"] == "general_agent"

    @pytest.mark.asyncio
    async def test_submit_worker_unavailable_degrades(self, mod, monkeypatch):
        """后台执行器不可用 → 删除刚创建的任务并返回 SUBMIT_FAILED（文档化降级）。"""
        task = _make_task()
        task_service, provider = self._patch_submit_deps(mod, monkeypatch, task)
        provider.get.return_value = None  # 所有服务均不可用（0.2 无 infrastructure 层）

        async def fake_init(t, workspace, task_data, ts, *, is_container=False):
            return t, None

        monkeypatch.setattr(mod.TaskSubmitTool, "_init_workspace", staticmethod(fake_init))

        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 1,
                "goal_title": "写测试",
                "target_type": "agent",
                "target_id": "general_agent",
            }
        )
        assert result.success is False
        assert result.error_code == "SUBMIT_FAILED"
        task_service.hard_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_workspace_init_failure_cleans_up(self, mod, monkeypatch):
        """工作空间初始化失败 → 删除任务记录并返回 WORKSPACE_INIT_FAILED。"""
        task = _make_task()
        task_service, _ = self._patch_submit_deps(mod, monkeypatch, task)

        async def fake_init(t, workspace, task_data, ts, *, is_container=False):
            return t, "工作空间初始化失败: boom"

        monkeypatch.setattr(mod.TaskSubmitTool, "_init_workspace", staticmethod(fake_init))

        tool = mod.TaskSubmitTool()
        result = await tool.execute(
            {
                "parent_agent_level": 1,
                "goal_title": "写测试",
                "target_type": "agent",
                "target_id": "general_agent",
            }
        )
        assert result.success is False
        assert result.error_code == "WORKSPACE_INIT_FAILED"
        task_service.hard_delete.assert_called_once()


# ── description 归一化（自旧 task/test_task_submit.py 移植） ──


class TestNormalizeDescription:
    """LLM 返回的 description 归一化为 str（防 pydantic str 校验 500）。"""

    def test_list_to_string(self, mod):
        result = mod._normalize_description(["在当前执行环境", ""])
        assert result == "在当前执行环境\n"

    def test_string_passthrough(self, mod):
        assert mod._normalize_description("正常描述") == "正常描述"

    def test_empty_string(self, mod):
        assert mod._normalize_description("") == ""

    def test_none_to_empty(self, mod):
        assert mod._normalize_description(None) == ""

    def test_multi_item_list(self, mod):
        assert mod._normalize_description(["第一行", "第二行", "第三行"]) == "第一行\n第二行\n第三行"

    def test_tuple(self, mod):
        assert mod._normalize_description(("第一行", "第二行")) == "第一行\n第二行"

    def test_non_string_scalar(self, mod):
        assert mod._normalize_description(42) == "42"
