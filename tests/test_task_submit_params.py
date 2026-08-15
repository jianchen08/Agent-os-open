# @feature: FP-MIGR 任务提交参数解耦 | @ci: python-plugins-test
"""task_submit 参数可用性矩阵测试。

验证（worktree 选择与隔离分离，agent 直接选，按任务类型限定范围）：
1. 容器任务：workspace_mode / isolation_level 拒绝（CONTAINER_PARAM_FORBIDDEN）
2. 容器直接子任务：workspace_mode / isolation_level 可自选（worktree 与执行环境）；
   workspace 可显式指定但 worktree 源空间必须与容器一致（否则
   CONTAINER_CHILD_WORKSPACE_MISMATCH）；inherit workspace 同规则
3. 普通子任务：workspace / workspace_mode / isolation_level 继承父任务，
   显式填写拒绝（SUBTASK_INHERITS_PARAMS），除非 inherit 管道继承
4. 普通根任务：三者均可填

[来源: 任务提交参数解耦设计（worktree 与隔离拆分）]
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
# isolation 是包（import isolation.workspace），需其父目录入 sys.path
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    """模块级 sys.path 注入（teardown 恢复）。

    注意：tasks 目录含 workspace.py（模块），system 目录含 workspace/（包）——
    两者对 `import workspace` 解析冲突。本测试仅 tool.py 懒加载需要 tasks 目录，
    用 fixture 管理并恢复 sys.path，避免污染同进程其它测试
    （如 channel_api 的 routes_workspaces 依赖 system/workspace/ 包）。
    """
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SYSTEM_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)


@pytest.fixture(scope="module")
def tool_module():
    """用 importlib 以唯一模块名加载 tool.py（避免与其它插件的平铺 tool 冲突）。"""
    spec = importlib.util.spec_from_file_location(
        "task_submit_tool_params_test_mod", _TOOL_DIR / "tool.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTask:
    """最小任务对象（metadata 承载执行参数与 ws_meta）。"""

    def __init__(self, task_id: str, metadata: dict | None = None):
        self.id = task_id
        self.title = "t"
        self.description = ""
        self.metadata = metadata or {}
        self.status = SimpleNamespace(value="pending")


class FakeTaskService:
    def __init__(self, tasks: dict[str, FakeTask] | None = None):
        self._tasks = tasks or {}
        self.created: list[dict] = []

    def get_task(self, task_id: str):
        return self._tasks.get(task_id)

    async def create_task(self, **kwargs):
        task = FakeTask(f"new_{len(self.created)}", kwargs.get("metadata") or {})
        self.created.append(kwargs)
        return task

    async def hard_delete(self, task_id: str):
        pass

    async def save_task(self, task):
        return task

    async def bind_pipeline_run(self, task_id: str, pipeline_id: str):
        pass

    def get_root_task_id(self, task_id: str):
        return "root_x"


def make_tool(tool_module, service: FakeTaskService):
    """构造 TaskSubmitTool，monkeypatch 服务提供者与校验钩子。

    0.2 收尾：提交即落库（start_run 占位已移除），task_data 在 _init_workspace
    处消费——测试 monkeypatch _init_workspace 捕获 task_data。
    """
    tool = tool_module.TaskSubmitTool()
    captured: dict = {}

    class _ProviderShim:
        def get(self, key: str):
            # workspace_lifecycle_manager 等 0.2 sidecar 不可达 → 文档化降级
            return None

    tool_module._get_service_provider = lambda: _ProviderShim()
    tool._get_task_service = lambda: service
    tool._validate_target_agent = lambda target_id, level: (True, "", "")
    tool._check_parent_ownership = lambda level, pid: (True, None)

    async def fake_init_workspace(task, workspace, task_data, task_service, *, is_container=False):
        captured["workspace"] = workspace
        captured["task_data"] = task_data
        captured["is_container"] = is_container
        return task, None

    tool._init_workspace = fake_init_workspace
    return tool, captured


def base_inputs(**overrides):
    inputs = {
        "goal_title": "测试任务",
        "goal_description": "验证参数可用性矩阵",
        "target_type": "agent",
        "target_id": "code_writer",
        "parent_agent_level": 2,
    }
    inputs.update(overrides)
    return inputs


def ws_meta(path: str) -> dict:
    """构造 ws_meta（project_root 即源空间）。"""
    return {"mode": "project_root", "path": path, "project_root": path}


@pytest.fixture
def tmp_proj():
    """仓库根内的临时目录（同容器校验要求源空间在仓库内）。"""
    d = tempfile.mkdtemp(dir=str(_REPO_ROOT))
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


# ── 容器任务 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_container_forbids_workspace_mode_and_isolation(tool_module):
    """容器任务：workspace_mode / isolation_level 拒绝（容器不直接执行）。"""
    tool, captured = make_tool(tool_module, FakeTaskService())

    for param, value in (("workspace_mode", "worktree"), ("isolation_level", "isolated")):
        inputs = base_inputs(task_scope="container", parent_agent_level=1, **{param: value})
        result = await tool.execute(inputs)
        assert not result.success, f"容器任务应拒绝 {param}"
        assert result.error_code == "CONTAINER_PARAM_FORBIDDEN", result.error


@pytest.mark.asyncio
async def test_container_allows_workspace_source(tool_module, tmp_proj):
    """容器任务：workspace 可填（作为容器空间源项目），且恒为隔离复制。"""
    tool, captured = make_tool(tool_module, FakeTaskService())

    inputs = base_inputs(task_scope="container", parent_agent_level=1, workspace=tmp_proj)
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["workspace"] == tmp_proj
    # 容器任务不可选隔离 → 恒 isolated（复制到容器空间）
    assert captured["task_data"]["isolation_mode"] == "isolated"


# ── 容器直接子任务 ─────────────────────────────────────────────


def container_child_service(tmp_proj, extra_tasks=None):
    """父=容器任务（带 ws_meta），可选额外任务（inherit 源）。"""
    tasks = {
        "container_p": FakeTask("container_p", {"task_scope": "container", "submitted_by_level": 1, "ws_meta": ws_meta(tmp_proj)})
    }
    if extra_tasks:
        tasks.update(extra_tasks)
    return FakeTaskService(tasks)


@pytest.mark.asyncio
async def test_container_child_can_choose_mode_and_isolation(tool_module, tmp_proj):
    """容器直接子任务：workspace_mode / isolation_level 可自选（worktree 与执行环境）。"""
    service = container_child_service(tmp_proj)
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(
        task_id="container_p",
        workspace_mode="worktree",
        isolation_level="isolated",
    )
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["task_data"]["workspace_mode"] == "worktree"
    assert captured["task_data"]["isolation_level"] == "isolated"
    # 不填 workspace → 继承容器空间（空值由父链解析）
    assert captured["task_data"]["workspace"] == ""


@pytest.mark.asyncio
async def test_container_child_workspace_setting_rejected(tool_module, tmp_proj):
    """容器直接子任务：workspace 不可设置（工作空间继承容器），显式指定一律拒绝。"""
    service = container_child_service(tmp_proj)
    tool, _ = make_tool(tool_module, service)

    # 即使路径与容器源空间一致，也不允许设置（继承即可，无需指定）
    inputs = base_inputs(task_id="container_p", workspace=tmp_proj)
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "CONTAINER_CHILD_PARAM_FORBIDDEN", result.error


@pytest.mark.asyncio
async def test_container_child_inherit_workspace_source_check(tool_module, tmp_proj):
    """容器直接子任务 inherit workspace：源空间与容器一致 → 放行；不一致 → 拒绝。"""
    # 一致：源任务 ws_meta.project_root == 容器源空间
    same_src = FakeTask("old_same", {"task_scope": "non_container", "ws_meta": ws_meta(tmp_proj)})
    service = container_child_service(tmp_proj, {"old_same": same_src})
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(task_id="container_p", inherit_from="old_same", inherit_mode="workspace")
    result = await tool.execute(inputs)
    assert result.success, result.error

    # 不一致：源任务源空间指向别处
    other = FakeTask("old_other", {"task_scope": "non_container", "ws_meta": ws_meta(str(_REPO_ROOT / "elsewhere"))})
    service2 = container_child_service(tmp_proj, {"old_other": other})
    tool2, _ = make_tool(tool_module, service2)

    inputs = base_inputs(task_id="container_p", inherit_from="old_other", inherit_mode="workspace")
    result = await tool2.execute(inputs)
    assert not result.success
    assert result.error_code == "CONTAINER_CHILD_WORKSPACE_MISMATCH", result.error


@pytest.mark.asyncio
async def test_container_child_inherit_pipe_allowed(tool_module, tmp_proj):
    """容器直接子任务：inherit pipe（对话历史）与空间无关，放行。"""
    service = container_child_service(tmp_proj)
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(task_id="container_p", inherit_from="old_task", inherit_mode="pipe")
    result = await tool.execute(inputs)
    assert result.success, f"inherit pipe 应放行: {result.error}"


@pytest.mark.asyncio
async def test_container_child_without_params_submits_inherited(tool_module, tmp_proj):
    """容器直接子任务不带空间/隔离字段：正常提交，工作空间继承容器（空值）。"""
    service = container_child_service(tmp_proj)
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(task_id="container_p")
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["task_data"]["workspace"] == ""
    assert captured["task_data"]["workspace_mode"] == ""
    assert captured["task_data"]["isolation_level"] == ""


# ── 普通子任务（继承父任务，除非管道继承）─────────────────────


def ordinary_child_service(extra_tasks=None):
    tasks = {
        "parent_p": FakeTask("parent_p", {"task_scope": "non_container", "submitted_by_level": 1})
    }
    if extra_tasks:
        tasks.update(extra_tasks)
    return FakeTaskService(tasks)


@pytest.mark.asyncio
async def test_ordinary_subtask_forbids_explicit_params(tool_module):
    """普通子任务：workspace / workspace_mode / isolation_level 继承父任务，显式填写拒绝。"""
    service = ordinary_child_service()
    tool, _ = make_tool(tool_module, service)

    for param, value in (
        ("workspace", "/tmp/x"),
        ("workspace_mode", "worktree"),
        ("isolation_level", "isolated"),
    ):
        inputs = base_inputs(task_id="parent_p", **{param: value})
        result = await tool.execute(inputs)
        assert not result.success, f"普通子任务应拒绝显式 {param}"
        assert result.error_code == "SUBTASK_INHERITS_PARAMS", result.error


@pytest.mark.asyncio
async def test_ordinary_subtask_without_params_submits(tool_module):
    """普通子任务不带空间/隔离字段：正常提交（继承父任务，空值由父链解析）。"""
    service = ordinary_child_service()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(task_id="parent_p")
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["task_data"]["workspace"] == ""
    assert captured["task_data"]["workspace_mode"] == ""


@pytest.mark.asyncio
async def test_ordinary_subtask_inherit_workspace_rejected(tool_module, tmp_proj):
    """普通子任务：inherit workspace 拒绝（只能继承管道）。"""
    old = FakeTask("old_task", {"task_scope": "non_container", "ws_meta": ws_meta(tmp_proj)})
    service = ordinary_child_service({"old_task": old})
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(task_id="parent_p", inherit_from="old_task", inherit_mode="workspace")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "SUBTASK_INHERITS_PARAMS", result.error

    # 组合模式含 workspace 同样拒绝
    inputs = base_inputs(task_id="parent_p", inherit_from="old_task", inherit_mode=["pipe", "workspace"])
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "SUBTASK_INHERITS_PARAMS", result.error


@pytest.mark.asyncio
async def test_ordinary_subtask_inherit_pipe_allowed(tool_module, tmp_proj):
    """普通子任务：inherit pipe（对话历史）是唯一允许的继承，放行。"""
    old = FakeTask("old_task", {"task_scope": "non_container", "ws_meta": ws_meta(tmp_proj)})
    service = ordinary_child_service({"old_task": old})
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(task_id="parent_p", inherit_from="old_task", inherit_mode="pipe")
    result = await tool.execute(inputs)
    assert result.success, f"inherit pipe 应放行: {result.error}"


# ── 普通根任务（L1，无父任务）：三者均可填 ─────────────────────


@pytest.mark.asyncio
async def test_ordinary_root_task_accepts_all_three(tool_module, tmp_proj):
    """普通根任务：workspace / workspace_mode / isolation_level 三者可填并进入 task_data。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(
        parent_agent_level=1,
        workspace=tmp_proj,
        workspace_mode="worktree",
        isolation_level="isolated",
    )
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["task_data"]["workspace"] == tmp_proj
    assert captured["task_data"]["workspace_mode"] == "worktree"
    assert captured["task_data"]["isolation_level"] == "isolated"
    # metadata 落账（供运行期 isolation_guard / workspace 插件读取）
    meta = service.created[0]["metadata"]
    assert meta["workspace"] == tmp_proj
    assert meta["workspace_mode"] == "worktree"
    assert meta["isolation_level"] == "isolated"


@pytest.mark.asyncio
async def test_ordinary_root_task_plain_mode(tool_module, tmp_proj):
    """普通根任务：plain 拓扑显式选择（与隔离解耦，两者独立落账）。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(
        parent_agent_level=1,
        workspace=tmp_proj,
        workspace_mode="plain",
        isolation_level="non_isolated",
    )
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["task_data"]["workspace_mode"] == "plain"
    assert captured["task_data"]["isolation_level"] == "non_isolated"
    assert service.created[0]["metadata"]["workspace_mode"] == "plain"


# ── schema ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_exposes_workspace_mode(tool_module):
    """工具 schema：workspace_mode 参数存在且枚举合法；三参数放开到全层级可见。"""
    tool = tool_module.TaskSubmitTool()
    definition = tool.get_tool_definition()
    props = definition.input_schema["properties"]
    assert "workspace_mode" in props
    assert props["workspace_mode"]["enum"] == ["worktree", "plain"]
    restrictions = definition.param_level_restrictions
    assert restrictions["workspace"]["max_visible_level"] == 3
    assert restrictions["workspace_mode"]["max_visible_level"] == 3
    assert restrictions["isolation_level"]["max_visible_level"] == 3
