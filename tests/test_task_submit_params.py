# @feature: FP-MIGR 任务提交参数解耦 | @ci: python-coverage
"""task_submit 参数可用性矩阵测试。

验证（project = 文件夹+登记，ADR 2026-08-27）：
1. 项目挂靠：L1 显式 project_id → workspace 解析为项目文件夹（explicit worktree
   源）+ state 带 task.parent_project_id；登记缺失/文件夹缺失/显式 workspace
   冲突均 fail-honest 拒绝
2. L2/L3：显式 project_id 越权拒绝；不传时系统沿父链继承（父 state 行
   task.parent_project_id 单跳）；无归属链保持独立
3. 普通子任务：workspace / workspace_mode / isolation_level 继承父任务，
   显式填写拒绝（SUBTASK_INHERITS_PARAMS），除非 inherit 管道继承
4. 普通根任务：三者均可填
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from tests._stdlib_guard import ensure_stdlib_module

# 车道共跑防线：某条导入链会让插件同名模块（pipeline/types.py，包内相对
# 导入顶层即炸）劫持裸名 "types"。导入 SimpleNamespace 前恢复 stdlib 绑定。
ensure_stdlib_module("types")
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

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

    GAP-1 统一后：提交不再走 YAML/task_service 写路径，workspace/隔离语义经
    chat.send_message 的 execution_context 透传（执行管道 workspace_lifecycle
    消费）——测试注入假 chat sender 捕获派发 params，断言 execution_context。
    2026-08-22：任务 id 统一写提交者管道 state（task.owned.*）——普通任务
    创建后追加一次 no_dispatch 登记调用；captured["params"] 保留首次（创建）
    调用，captured["calls"] 为全部调用。
    """
    tool = tool_module.TaskSubmitTool()
    captured: dict = {"calls": []}

    class _ProviderShim:
        def get(self, key: str):
            # workspace_lifecycle_manager 等 0.2 sidecar 不可达 → 文档化降级
            return None

    tool_module._get_service_provider = lambda: _ProviderShim()
    tool._get_task_service = lambda: service

    # _validate_target_agent 已 async 化（P4：registry 路径经 agent_manager
    # agent.get 服务查询，2026-08-20）——mock 需为 async 函数
    async def _fake_validate_target(target_id, level):
        return (True, "", "")

    tool._validate_target_agent = _fake_validate_target
    tool._check_parent_ownership = lambda level, pid: (True, None)

    async def fake_sender(params: dict) -> dict:
        captured["calls"].append(params)
        if params.get("create"):
            # 出生登记（统一出生协议①）：create + no_dispatch，引擎生成管道
            # id；state 断言面（captured["params"] 保留出生调用）
            captured["params"] = params
            return {"status": "created", "pipeline_id": "a1b2c3d4e5f6"}
        if params.get("no_dispatch"):
            # 身份登记/任务登记调用：只写 state 不派发
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        # 执行派发（统一出生协议③）：execution_context 断言面
        captured["dispatch"] = params
        # 模拟注入分支响应（background 派发立即返回）
        return {"status": "dispatched", "pipeline_id": params.get("pipeline_id", "")}

    tool_module.set_chat_sender(fake_sender)
    return tool, captured


@pytest.fixture(autouse=True)
def _reset_chat_sender(tool_module):
    """模块级全局 chat sender 用后复位——不泄漏到后续测试文件。"""
    yield
    tool_module._chat_sender = None


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


# ── 项目挂靠（project_id：L1 显式 / L2L3 父链继承）─────────────


@pytest.fixture
def project_registry_env(tool_module, monkeypatch, tmp_path):
    """临时项目登记（隔离真实登记目录）+ state reader 注入。

    返回 (登记 id→path 映射 dict 可变引用, captured rows 设置器)。
    """
    _SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    import project_registry as pr

    folder = tmp_path / "proj_folder"
    folder.mkdir()
    paths = {"proj00112233": str(folder)}
    monkeypatch.setattr(pr, "load_project_paths", lambda: dict(paths))

    rows: list[dict] = []

    async def fake_reader():
        return list(rows)

    # 规范 monkeypatch（自动还原）：替换读取源而非整个 getter 函数，
    # 防止泄漏到同文件后续用例（顺序耦合）。
    monkeypatch.setattr(tool_module, "_state_reader", fake_reader)
    return paths, rows


@pytest.mark.asyncio
async def test_l1_project_task_anchors_to_project_folder(tool_module, tmp_path, project_registry_env):
    """L1 挂项目：workspace 解析为项目文件夹（explicit worktree 源），state 带挂靠键。"""
    paths, _rows = project_registry_env
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(parent_agent_level=1, project_id="proj00112233")
    result = await tool.execute(inputs)
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    # 挂项目 = 显式 workspace（项目文件夹）+ 默认 worktree（从项目仓库分叉）
    assert ec["workspace"]["source_path"] == paths["proj00112233"]
    assert ec["workspace"]["mode"] == "worktree"
    assert ec["workspace"]["explicit"] is True
    state = captured["params"]["state"]
    assert state["task.parent_project_id"] == "proj00112233"


@pytest.mark.asyncio
async def test_project_id_not_in_registry_rejected(tool_module, project_registry_env):
    """project_id 不在登记中（项目已删除）→ PROJECT_NOT_FOUND fail-honest。"""
    service = FakeTaskService()
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(parent_agent_level=1, project_id="ffffffffffff")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "PROJECT_NOT_FOUND", result.error


@pytest.mark.asyncio
async def test_project_workspace_conflict_rejected(tool_module, tmp_path, project_registry_env):
    """挂项目 + 显式给了不同 workspace 路径 → 语义冲突拒绝。"""
    other = tmp_path / "other_ws"
    other.mkdir()
    service = FakeTaskService()
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(parent_agent_level=1, project_id="proj00112233", workspace=str(other))
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "PROJECT_WS_CONFLICT", result.error


@pytest.mark.asyncio
async def test_l2_explicit_project_id_rejected(tool_module, project_registry_env):
    """L2 显式指定 project_id → 越权拒绝（归属由系统沿父链继承）。"""
    service = FakeTaskService()
    tool, _ = make_tool(tool_module, service)

    inputs = base_inputs(parent_agent_level=2, project_id="proj00112233", task_id="parent-task-1")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "L2_CANNOT_SPECIFY_PROJECT_ID", result.error


@pytest.mark.asyncio
async def test_l2_child_inherits_project_from_parent_state(tool_module, tmp_path, project_registry_env):
    """L2 子任务：不传 project_id，系统读父任务 state 行 task.parent_project_id 继承写入。"""
    paths, rows = project_registry_env
    rows.append({"pipeline_id": "parent-task-1", "task.parent_project_id": "proj00112233"})
    print("DBG_BEFORE_MAKETOOL:", id(tool_module), tool_module._get_state_reader)
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)
    print("DBG_AFTER_MAKETOOL:", tool_module._get_state_reader())

    inputs = base_inputs(parent_agent_level=2, task_id="parent-task-1")
    result = await tool.execute(inputs)
    assert result.success, result.error
    state = captured["params"]["state"]
    assert state["task.parent_project_id"] == "proj00112233"
    # 继承注入的 workspace（项目文件夹）不算显式指定——子任务闸门放行
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == paths["proj00112233"]


@pytest.mark.asyncio
async def test_l2_child_without_project_ancestry_stays_independent(tool_module, project_registry_env):
    """父任务无项目归属 → 子任务也不挂项目（独立链不凭空长出 project_id）。"""
    _paths, rows = project_registry_env
    rows.append({"pipeline_id": "parent-task-1"})
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(parent_agent_level=2, task_id="parent-task-1")
    result = await tool.execute(inputs)
    assert result.success, result.error
    state = captured["params"]["state"]
    assert "task.parent_project_id" not in state


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
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == ""
    assert ec["workspace"]["explicit"] is False
    # 普通任务默认隔离执行（0.1 coordinator.default_level 对齐）
    assert ec["isolation"]["level"] == "isolated"


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
    """普通根任务：workspace / workspace_mode / isolation_level 三者可填并进入 execution_context。"""
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
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == tmp_proj
    assert ec["workspace"]["mode"] == "worktree"
    assert ec["workspace"]["explicit"] is True
    assert ec["isolation"]["level"] == "isolated"
    # 任务域字段出生即入 state（GAP-1：task=pipeline，YAML metadata 写路径退役）
    state = captured["params"]["state"]
    assert state["task.goal"] == "测试任务"


@pytest.mark.asyncio
async def test_ordinary_root_task_without_workspace_leaves_mode_empty(tool_module):
    """普通根任务无显式空间字段：mode 留空——执行管道落「工作空间根/{task_id}」
    默认目录（plain 拓扑）。worktree 仅显式 workspace / workspace_mode 下成立，
    不做缺省声明（无 workspace 即无 worktree）。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    result = await tool.execute(base_inputs(parent_agent_level=1))
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["explicit"] is False
    assert ec["workspace"]["source_path"] == ""
    assert ec["workspace"]["mode"] == ""


@pytest.mark.asyncio
async def test_workspace_mode_without_source_passthrough(tool_module):
    """无 workspace 路径但显式选了拓扑：mode 按声明透传（显式选择才成立）。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    result = await tool.execute(base_inputs(parent_agent_level=1, workspace_mode="worktree"))
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["mode"] == "worktree"
    assert ec["workspace"]["explicit"] is False
    assert ec["workspace"]["source_path"] == ""


@pytest.mark.asyncio
async def test_ordinary_root_task_plain_mode(tool_module, tmp_proj):
    """普通根任务：plain 拓扑显式选择（与隔离解耦，两者独立透传）。"""
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
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["mode"] == "plain"
    assert ec["isolation"]["level"] == "non_isolated"


@pytest.mark.asyncio
async def test_ordinary_task_registers_owned_to_owner_pipeline(tool_module):
    """普通任务（有提交者管道）：出生执行管道后，任务 id 登记到提交者管道 state。

    语义（2026-08-22 定案）：任务 id 统一写"自己的管道"（提交者管道）state——
    task.owned.<id> 自持（本管道插件也能读它处理它）；执行管道的 task.id 身份
    已由统一出生协议写全（= 管道 id）。
    """
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(
        parent_agent_level=2,
        pipeline_id="owner-pipe-1",
        session_id="sess-1",
        task_id="parent-task-1",
    )
    result = await tool.execute(inputs)
    assert result.success, result.error
    # 四次调用：出生三段（①出生登记 ②身份登记 ③执行派发）+ ④no_dispatch
    # 登记到提交者管道
    assert len(captured["calls"]) == 4, captured["calls"]
    create_call = captured["calls"][0]
    assert create_call.get("create") is True
    assert captured["calls"][1]["state"] == {"task.id": "a1b2c3d4e5f6"}
    reg = captured["calls"][3]
    assert reg["no_dispatch"] is True
    assert reg["pipeline_id"] == "owner-pipe-1"
    # state 键用全 id（引擎生成即 12 位短 id）；LLM 面返回同值（不再截断）
    full_pid = "a1b2c3d4e5f6"
    assert result.output["pipeline_id"] == full_pid
    assert reg["state"][f"task.owned.{full_pid}.title"] == "测试任务"
    assert reg["state"][f"task.owned.{full_pid}.status"] == "running"


@pytest.mark.asyncio
async def test_dispatch_failure_returns_failure_envelope(tool_module):
    """派发失败（管道未创建）= 核心流程失败：失败信封（DISPATCH_FAILED），不以 success 掩盖。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    async def failing_sender(params: dict) -> dict:
        if params.get("no_dispatch"):
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        return {"status": "error", "message": "engine unavailable"}

    tool_module.set_chat_sender(failing_sender)
    result = await tool.execute(base_inputs(parent_agent_level=1))
    assert not result.success
    assert result.error_code == "DISPATCH_FAILED"
    # 失败可见：错误文案含原因与重试指引，不出现「已提交」成功语
    assert "执行管道未能创建" in result.error
    assert "重试" in result.error
    assert "已提交" not in result.error


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


# ─────────────────── 评估指标必填 input_params 提交期校验 ───────────────────


def test_metric_missing_required_params_rejected(tool_module):
    """file_check 缺 path（input_params 整体缺失）→ 提交期拒绝 INVALID_METRIC_PARAMS。

    缺必填参数的任务在评估期每轮失败且 LLM 无法自修（参数烙在出生 state），
    必须在提交期拦住——这是「评估器 path 参数缺失死循环」的根源修复。
    """
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"file_check": {}}
    )
    assert fail is not None
    assert not fail.success
    assert fail.error_code == "INVALID_METRIC_PARAMS"
    assert "path" in fail.error
    assert "input_params" in fail.error


def test_metric_required_params_present_passes(tool_module):
    """必填参数齐备 → 原样放行（不补全不覆盖，只认大模型输入）。"""
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"file_check": {"input_params": {"path": "result.txt", "check": "exists"}}}
    )
    assert fail is None
    assert normalized["file_check"]["input_params"]["path"] == "result.txt"


def test_metric_required_params_data_driven_across_metrics(tool_module):
    """校验纯数据驱动（读指标定义 input_schema.required）：semantic_check 同样拦截。"""
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"semantic_check": {"input_params": {"check": "intent"}}}
    )
    assert fail is not None
    assert fail.error_code == "INVALID_METRIC_PARAMS"
    assert "output" in fail.error


def test_metric_params_validation_skips_without_definitions(tool_module, monkeypatch):
    """指标定义加载失败（fail-open）→ 不拦截（与指标 ID 校验同款降级语义）。"""
    monkeypatch.setattr(tool_module, "_load_metric_definitions", lambda: {})
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"file_check": {}}
    )
    assert fail is None
    assert normalized == {"file_check": {}}


# ─────────────────── inherit 源解析（state 单一真值） ───────────────────


def test_inherit_source_missing_rejected(tool_module, monkeypatch):
    """state 聚合无源任务 → 失败信封（不再读 YAML 存量，0.2 任务可被解析）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    import asyncio

    tool = tool_module.TaskSubmitTool()
    path, fail = asyncio.run(tool._extract_inherited_workspace("ffffffffffff"))
    assert path is None
    assert fail is not None
    assert "不存在或无元数据" in fail.error


def test_inherit_source_state_row_serves_ws_meta(tool_module, monkeypatch):
    """源任务 state 行带 ws_meta（JSON 字符串形态）→ 取出路径；plain 模式无 git 校验。

    路径取容器内真实目录（仓库根）——跨容器守卫按真实容器根判定。
    """
    import asyncio
    import json as _json

    rows = [
        {
            "pipeline_id": "srcfull123456",
            "task.id": "srcfull123456",
            "ws_meta": _json.dumps({"path": str(_REPO_ROOT), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    path, fail = asyncio.run(tool._extract_inherited_workspace("srcfull123456"))
    assert fail is None
    assert path == str(_REPO_ROOT)


def test_inherit_state_bridge_down_fail_honest(tool_module, monkeypatch):
    """桥未就绪（None）→ 显式失败信封（不静默当任务不存在）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: None)
    import asyncio

    tool = tool_module.TaskSubmitTool()
    path, fail = asyncio.run(tool._extract_inherited_workspace("ffffffffffff"))
    assert path is None
    assert fail is not None
    assert "state 聚合不可用" in fail.error
