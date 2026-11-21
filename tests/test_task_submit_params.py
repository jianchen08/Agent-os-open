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
import sys
import tempfile
from pathlib import Path

from tests import _task_birth_workspace as _birth_ws  # noqa: E402
from tests._stdlib_guard import ensure_stdlib_module

# 车道共跑防线：某条导入链会让插件同名模块（pipeline/types.py，包内相对
# 导入顶层即炸）劫持裸名 "types"。导入 SimpleNamespace 前恢复 stdlib 绑定。
ensure_stdlib_module("types")
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
# isolation 是包（import isolation.workspace），需其父目录入 sys.path
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared"


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
    service 参数为历史脚手架（GAP-1 后无消费方，保留签名降低用例面改动）。
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

    tool_module._get_service_provider = _ProviderShim
    tool._get_task_service = lambda: service

    # _validate_target_agent 已 async 化（P4：registry 路径经 agent_manager
    # agent.get 服务查询，2026-08-20）——mock 需为 async 函数
    async def _fake_validate_target(target_id, level):
        return (True, "", "")

    tool._validate_target_agent = _fake_validate_target

    # 归属闸已 async 化（state 层级键读面）——归属行为矩阵另测
    async def _allow_ownership(level, pid):
        return (True, None)

    tool._check_parent_ownership = _allow_ownership

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
    # 出生协议第三段（工作空间同步创建，ADR-2026-10-17）：准备器缺席即出生失败
    _birth_ws.inject_preparer(tool_module)

    # agent 配置查询钩子缺省注入（配置单源 = 注入钩子，无磁盘回退）：完备性
    # 左值的身份字段源——需要特定配置/故障形态的用例自行覆盖注入
    async def _default_lookup(agent_id: str):
        return {
            "level": "L3",
            "is_active": True,
            "tool_ids": ["file_write"],
            "system_prompt": "persona-stub",
        }

    tool_module.set_agent_registry_lookup(_default_lookup)
    return tool, captured


@pytest.fixture(autouse=True)
def _reset_chat_sender(tool_module, tmp_path_factory, monkeypatch):
    """模块级全局 chat sender 用后复位——不泄漏到后续测试文件。

    用户根钉到空 tmp：mode 键完备性回退链的出厂命中不得依赖机器真实播种副本。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path_factory.mktemp("user-root")))
    yield
    tool_module._chat_sender = None
    tool_module._workspace_preparer = None
    tool_module._agent_registry_lookup = None


def base_inputs(**overrides):
    inputs = {
        "goal_title": "测试任务",
        "goal_description": "验证参数可用性矩阵",
        "target_type": "agent",
        "target_id": "mode_coding/code_writer",
        "parent_agent_level": 2,
        # 提交者会话坐标（生产由 param_inject 注入；完备性闸门按 state 键
        # 计必需）——无会话锚场景的用例显式覆盖
        "session_id": "sess-1",
        # 提交者身份（生产由调用上下文注入；内核 chat.send_message 出生硬校验非空）
        "user_id": "u_test",
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
    shared_dir = _REPO_ROOT / "plugins" / "shared"
    if str(shared_dir) not in sys.path:
        sys.path.insert(0, str(shared_dir))

    folder = tmp_path / "proj_folder"
    folder.mkdir()
    paths = {"proj00112233": str(folder)}

    rows: list[dict] = []

    async def fake_reader():
        return list(rows)

    # project.* 服务桩（T6a）：登记读面（list_paths）+ 项目行读面（get，
    # plan 态闸/mode 闸消费——登记外 id 按 found=false 放行非项目流）。
    # plan 态闸对 rows 中带 task.parent_project_id 且该 id 登记外（found=false）
    # 的派发不过闸，与"未挂靠项目"同语义。
    async def fake_project_service(method: str, args: dict):
        if method == "list_paths":
            return {"paths": dict(paths)}
        if method == "get":
            pid = str(args.get("project_id") or "")
            known = pid in paths
            return {
                "found": known,
                "project": {"id": pid, "workflow_state": "running", "mode_tags": []}
                if known
                else {},
            }
        raise AssertionError(f"unexpected method: {method}")

    # 规范 monkeypatch（自动还原）：替换读取源而非整个 getter 函数，
    # 防止泄漏到同文件后续用例（顺序耦合）。
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", fake_reader)
    monkeypatch.setattr(tool_module, "_project_service", fake_project_service)
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
    # 挂项目 = 显式 workspace（项目文件夹）+ 默认 plain（直接在项目目录工作，
    # 2026-11-11 用户裁定；worktree 仅显式选择）
    assert ec["workspace"]["source_path"] == paths["proj00112233"]
    assert ec["workspace"]["mode"] == "plain"
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
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

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
async def test_llm_path_subtask_birth_binds_thread_to_session_anchor(tool_module):
    """LLM 路径子任务：出生登记 thread_id 取注入 session_id（会话锚点）。

    pipeline_sessions 落真会话映射（非自环）——用户向子管道发消息时内核
    按会话成员校验放行，消息一对一进子管道；自环绑定会被内核判"不属于
    该 thread"而回落主管道（写错桶）。
    """
    service = ordinary_child_service()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(task_id="parent_p", session_id="thread-user-1")
    result = await tool.execute(inputs)
    assert result.success, result.error
    # captured["params"] = 出生登记调用（统一出生协议①）
    assert captured["params"]["thread_id"] == "thread-user-1"


@pytest.mark.asyncio
async def test_panel_thread_id_takes_precedence_over_injected_session(tool_module):
    """thread_id 显式在场（面板透传）优先于注入 session_id。"""
    service = ordinary_child_service()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(
        task_id="parent_p", session_id="thread-injected", thread_id="thread-panel"
    )
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["params"]["thread_id"] == "thread-panel"


@pytest.mark.asyncio
# sync_agent 1.5.1 已松声明（session_id 移出 required_state_inputs——运行时本就
# 空转容错，无会话锚出生=合法派发形态）：xfail 退役转正式回归护栏
async def test_subtask_without_session_anchor_keeps_independent_birth(tool_module):
    """两键皆缺（无会话根任务）：thread_id 省略——出生保持独立自环。"""
    service = ordinary_child_service()
    tool, captured = make_tool(tool_module, service)

    inputs = base_inputs(task_id="parent_p", session_id=None)
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert "thread_id" not in captured["params"]


@pytest.mark.asyncio
async def test_ordinary_subtask_can_inherit_failed_task_workspace(tool_module, tmp_proj, monkeypatch):
    """普通子任务：inherit_from 接续失败任务的工作空间——放行（闸门不对称消解）。

    2026-10-20 用户裁定：旧闸门只放行「继承管道」、工作空间继承一律拒绝，
    同一参数组合在主会话路径放行、子任务路径报错——接续失败任务恰恰是子任务
    的高频场景，闸门按任务类型分叉即行为不可预期。
    """
    service = ordinary_child_service()
    tool, captured = make_tool(tool_module, service)
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_state_reader",
        lambda: [
            {
                "pipeline_id": "old_task",
                "task.id": "old_task",
                "run_status": "failed",
                "task.status": "failed",
                "task.ws_meta": ws_meta(tmp_proj),
            }
        ],
    )

    inputs = base_inputs(task_id="parent_p", inherit_from="old_task")
    result = await tool.execute(inputs)
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == tmp_proj
    assert ec["workspace"]["inherit"]["source_task_id"] == "old_task"


@pytest.mark.asyncio
async def test_ordinary_subtask_inherit_missing_source_rejected(tool_module, tmp_proj, monkeypatch):
    """普通子任务：inherit_from 查无源任务 → 显式报错（不静默当普通任务）。"""
    service = ordinary_child_service()
    tool, _ = make_tool(tool_module, service)
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: [])

    inputs = base_inputs(task_id="parent_p", inherit_from="ghost_task")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "INHERIT_SOURCE_NOT_FOUND", result.error


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
    # 任务域字段出生即入 state（task=pipeline，单一真值在 state）
    state = captured["params"]["state"]
    assert state["task.goal"] == "测试任务"


@pytest.mark.asyncio
async def test_ordinary_root_task_workspace_without_mode_defaults_plain(tool_module, tmp_proj):
    """显式 workspace 但未传 workspace_mode：缺省落 plain（2026-11-11 用户裁定——
    多数任务不存在同文件域并行冲突，worktree 降为显式选择）。性质断言：
    显式传 worktree 仍按原语义落 worktree（能力不回退），缺省与显式可区分。"""
    service = FakeTaskService()
    tool, captured = make_tool(tool_module, service)

    # 缺省：显式 workspace + 未传 mode → plain（直接在目标目录干活）
    result = await tool.execute(base_inputs(parent_agent_level=1, workspace=tmp_proj))
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == tmp_proj
    assert ec["workspace"]["mode"] == "plain"
    assert ec["workspace"]["explicit"] is True

    # 对照组：同一输入显式传 worktree → 仍落 worktree（显式语义不变）
    service2 = FakeTaskService()
    tool2, captured2 = make_tool(tool_module, service2)
    result2 = await tool2.execute(
        base_inputs(parent_agent_level=1, workspace=tmp_proj, workspace_mode="worktree")
    )
    assert result2.success, result2.error
    ec2 = captured2["dispatch"]["execution_context"]
    assert ec2["workspace"]["mode"] == "worktree"



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
    # 五次调用：出生四段（①出生登记 ②身份登记 ③工作空间登记 ④执行派发，
    # ADR 2026-10-17 增工作空间段）+ ⑤no_dispatch 登记到提交者管道
    assert len(captured["calls"]) == 5, captured["calls"]
    create_call = captured["calls"][0]
    assert create_call.get("create") is True
    assert captured["calls"][1]["state"] == {"task.id": "a1b2c3d4e5f6"}
    reg = captured["calls"][4]
    assert reg["no_dispatch"] is True
    assert reg["pipeline_id"] == "owner-pipe-1"
    # state 键用全 id（引擎生成即 12 位短 id）；LLM 面返回同值（不再截断）
    full_pid = "a1b2c3d4e5f6"
    assert result.output["pipeline_id"] == full_pid
    assert reg["state"][f"task.owned.{full_pid}.title"] == "测试任务"
    assert reg["state"][f"task.owned.{full_pid}.submitted_by"] == "u_test"
    # owned.status 镜像退役（ADR 2026-11-02-state-key-governance，6d8375003）：
    # 登记键 = {title, created_at, submitted_by} 纯声明，任务状态单一真源
    # = 子管道行 task.status
    assert f"task.owned.{full_pid}.status" not in reg["state"]


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
    assert "expected" in fail.error


def test_format_valid_registered_and_submittable(tool_module):
    """format_valid 已注册（审查报告 20261018 §6 第一批 4a）：合法指标 ID 可提交。

    code_writer.yaml recommended_metrics 与执行报告模板引用该指标；注册前该 key
    按 INVALID_METRIC_ID 全拒（合法集合来自 evaluation_metrics.yaml 单源）。
    required=[path] 与 code_writer 默认参数形态（path + type）对位。
    """
    valid = tool_module._get_valid_metric_ids()
    assert valid is not None
    assert "format_valid" in valid, "format_valid 须在 evaluation_metrics.yaml 注册"
    assert "ghost_metric_xyz" not in valid, "集合来自 yaml 单源（非硬编码清单）"
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"format_valid": {"input_params": {"path": "src/", "type": "python"}}}
    )
    assert fail is None
    assert normalized["format_valid"]["input_params"]["path"] == "src/"


# ─────────────────── semantic_check 口径统一（yaml 单源回归锚） ───────────────────


def test_semantic_check_expected_only_passes_output_optional(tool_module):
    """semantic_check 带 yaml 必填的 expected（expect 映射形态，output 可选）→ 放行。"""
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {
            "semantic_check": {
                "input_params": {
                    "expected": "1200-1800字剧情连续",
                    "check": "match",
                }
            }
        }
    )
    assert fail is None
    assert normalized["semantic_check"]["input_params"]["expected"] == "1200-1800字剧情连续"


def test_semantic_check_missing_expected_rejected_with_real_required(tool_module):
    """semantic_check 缺 expected → 拒绝；文案含动态生成的真实必填示例。"""
    _, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"semantic_check": {"input_params": {"output": "第二十五章正文"}}}
    )
    assert fail is not None
    assert fail.error_code == "INVALID_METRIC_PARAMS"
    assert "expected" in fail.error
    assert '"expected"' in fail.error
    assert "期望的语义描述" in fail.error


def test_metric_required_examples_skips_unknown_and_optional(tool_module):
    """示例生成器：定义缺失/无必填的指标不生成条目；正常指标占位说明来自属性描述。"""
    assert tool_module._metric_required_examples(["ghost_metric"]) == ""
    examples = tool_module._metric_required_examples(["file_check"])
    assert '"path"' in examples
    assert "文件或目录路径" in examples
    assert "criteria" not in examples


def test_error_examples_follow_yaml_definitions(tool_module, monkeypatch):
    """错误文案示例动态跟随指标定义（required/description 改动即生效，零硬编码）。"""
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_load_metric_definitions",
        lambda: {
            "custom_metric": {
                "input_schema": {
                    "type": "object",
                    "properties": {"foo": {"type": "string", "description": "自定义必填字段"}},
                    "required": ["foo"],
                }
            }
        },
    )
    _, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"custom_metric": {"input_params": {}}}
    )
    assert fail is not None
    assert fail.error_code == "INVALID_METRIC_PARAMS"
    assert '"foo"' in fail.error
    assert "自定义必填字段" in fail.error
    # 内置指标示例不出现（示例只随本次问题指标生成，不硬编码清单）
    assert "file_check" not in fail.error


def test_metric_params_validation_skips_without_definitions(tool_module, monkeypatch):
    """指标定义加载失败（fail-open）→ 不拦截（与指标 ID 校验同款降级语义）。"""
    monkeypatch.setattr(sys.modules["acceptance"], "_load_metric_definitions", lambda: {})
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {"file_check": {}}
    )
    assert fail is None
    assert normalized == {"file_check": {}}

# ─────────────────── inherit 源解析（state 单一真值） ───────────────────
#
# 2026-10-20 用户裁定《继承机制简化方案》：inherit_from 单参数 = 源任务 ID →
# 工具按 id 正向查源任务 state 行 → 整份复制其 task.ws_meta（mode/path/
# project_root/branch/tree_path 一字不改）；三道校验 = 源已终态 / 坐标活着
# （目录存在 + git 身份有效）/ 同 project_root 基座；坐标已死显式报错不静默
# 回落（旧实现的 worktree→project_root 无条件降级已被裁定否决）。


def test_inherit_source_missing_rejected(tool_module, monkeypatch):
    """state 聚合无源任务 → 失败信封（不静默当普通任务）。"""
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: [])
    import asyncio

    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("ffffffffffff"))
    assert meta is None
    assert fail is not None
    assert "不存在" in fail.error


def test_inherit_running_source_rejected(tool_module, monkeypatch, tmp_path_factory):
    """源任务仍在运行 → 拒绝继承（防同树双写）。"""
    import asyncio

    ws = tmp_path_factory.mktemp("running_src_ws")
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_state_reader",
        lambda: [
            {
                "pipeline_id": "runsrc123456",
                "task.id": "runsrc123456",
                "run_status": "running",
                "task.status": "running",
                "task.ws_meta": {"mode": "plain", "path": str(ws), "project_root": str(ws)},
            }
        ],
    )
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("runsrc123456"))
    assert meta is None
    assert fail is not None
    assert "仍在运行" in fail.error


def test_inherit_source_state_row_serves_ws_meta(tool_module, monkeypatch):
    """源任务 state 行带 task.ws_meta（JSON 字符串形态）→ 整份取出坐标。

    无父链坐标可判归属（inputs 未带基座）→ 不做跨项目判定，放行。
    """
    import asyncio
    import json as _json

    rows = [
        {
            "pipeline_id": "srcfull123456",
            "task.id": "srcfull123456",
            "run_status": "failed",
            "task.ws_meta": _json.dumps({"path": str(_REPO_ROOT), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("srcfull123456"))
    assert fail is None
    assert meta is not None
    assert meta["path"] == str(_REPO_ROOT)
    assert meta["mode"] == "plain"


def _ws_rows(task_id: str, ws: Path, project_root: Path | None = None) -> list[dict]:
    """构造带 task.ws_meta 的 state 行（JSON 字符串形态，同真实写面）。

    project_root 缺省 = ws 自身（plain 源：项目目录即工作目录）。
    """
    import json as _json

    root = str(project_root or ws)
    return [
        {
            "pipeline_id": task_id,
            "task.id": task_id,
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": _json.dumps({"mode": "plain", "path": str(ws), "project_root": root}),
        }
    ]


def test_inherit_same_project_root_allowed_outside_plugin_layout(
    tool_module, monkeypatch, tmp_path_factory
):
    """源与本任务同 project_root（仓外，如 D 盘项目）→ 放行继承。

    归属判据只能来自工作空间坐标：装机用户空间插件目录是平铺的
    （<user_root>/plugins/task_submit/tool.py，无 shared/ 这层），按插件代码
    文件层级数推导出的"容器根"落到 <user_root> 的上级（如 AppData/Roaming），
    于是任何不在该目录下的工作空间（用户在 D 盘的项目）一律被误判跨容器拒绝。
    """
    import asyncio

    project = tmp_path_factory.mktemp("d_drive_project") / "proj"
    ws = project / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_state_reader",
        lambda: _ws_rows("samebase1234", ws, project_root=project),
    )
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(
        tool._resolve_inherit_coordinate(
            "samebase1234",
            {"parent_ws_meta": {"mode": "plain", "path": str(ws), "project_root": str(project)}},
        )
    )
    assert fail is None
    assert meta is not None
    assert meta["path"] == str(ws)


def test_inherit_sibling_workspace_same_project_allowed(
    tool_module, monkeypatch, tmp_path_factory
):
    """同项目下的兄弟任务工作空间 → 放行（基座按 project_root 坐标对坐标比）。

    性质断言：基座取 project_root 时，同项目兄弟任务的工作空间不会被误判
    跨项目（旧判据按路径包含关系比，worktree 源在 .agentos-workspaces 之下
    必然被判死）。
    """
    import asyncio

    project = tmp_path_factory.mktemp("proj_with_ws")
    parent_ws = project / "workspace" / "parent"
    sibling_ws = project / "workspace" / "sibling"
    parent_ws.mkdir(parents=True)
    sibling_ws.mkdir(parents=True)
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_state_reader",
        lambda: _ws_rows("sibling1234", sibling_ws, project_root=project),
    )
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(
        tool._resolve_inherit_coordinate(
            "sibling1234",
            {"parent_ws_meta": {"mode": "plain", "path": str(parent_ws), "project_root": str(project)}},
        )
    )
    assert fail is None
    assert meta is not None
    assert meta["path"] == str(sibling_ws)


def test_inherit_foreign_project_root_rejected(tool_module, monkeypatch, tmp_path_factory):
    """源工作空间属于另一个项目基座 → 拒绝继承，防产物落错项目。

    文案回归防线：不得把工作空间路径当"容器名"打印（路径是坐标不是容器标识）。
    """
    import asyncio

    base = tmp_path_factory.mktemp("proj_a")
    other = tmp_path_factory.mktemp("proj_b") / "unrelated"
    other.mkdir(parents=True)
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: _ws_rows("otherbase123", other))
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(
        tool._resolve_inherit_coordinate(
            "otherbase123",
            {"parent_ws_meta": {"mode": "plain", "path": str(base), "project_root": str(base)}},
        )
    )
    assert meta is None
    assert fail is not None
    assert "另一个项目基座" in fail.error
    assert "属于其它容器" not in fail.error


def test_inherit_workspace_under_repo_root_but_other_project_rejected(
    tool_module, monkeypatch, tmp_proj
):
    """回归防线：仓内目录不再因"在插件代码布局之下"而被放行。

    性质断言：归属判据与插件代码位置解耦——基座恒取坐标里的 project_root。
    """
    import asyncio

    project = Path(tmp_proj) / "proj"
    other = Path(tmp_proj) / "unrelated_dir"
    project.mkdir()
    other.mkdir()
    monkeypatch.setattr(
        sys.modules["acceptance"],
        "_state_reader",
        lambda: _ws_rows("inrepo12345", other, project_root=other),
    )
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(
        tool._resolve_inherit_coordinate(
            "inrepo12345",
            {
                "parent_ws_meta": {
                    "mode": "plain",
                    "path": str(Path(tmp_proj) / "base_dir"),
                    "project_root": str(project),
                }
            },
        )
    )
    assert meta is None
    assert fail is not None
    assert "另一个项目基座" in fail.error


def test_inherit_without_any_base_allowed(tool_module, monkeypatch, tmp_path_factory):
    """无父链坐标、无显式 workspace → 无基座可比，不做跨项目判定。

    判据缺失不得退化成"按代码布局猜一个容器根"（那正是本缺陷本身）。
    """
    import asyncio

    ws = tmp_path_factory.mktemp("no_base_ws")
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: _ws_rows("nobase1234567", ws))
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("nobase1234567", {}))
    assert fail is None
    assert meta is not None
    assert meta["path"] == str(ws)


def test_inherit_state_bridge_down_fail_honest(tool_module, monkeypatch):
    """桥未就绪（None）→ 显式失败信封（不静默当任务不存在）。"""
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: None)
    import asyncio

    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("ffffffffffff"))
    assert meta is None
    assert fail is not None
    assert "state 聚合不可用" in fail.error


def test_inherit_worktree_source_serves_own_path(tool_module, monkeypatch, tmp_proj):
    """worktree 源任务 → 继承坐标取其 worktree 自身路径（不再降级 project_root）。

    性质断言：返回路径含 __wt_ 段且等于源 worktree 路径——旧实现把 worktree
    源悄悄换成 project_root，"接续失败任务现场"语义落空（2026-10-20 事故根因）。
    """
    import asyncio
    import json as _json

    project = Path(tmp_proj) / "proj"
    wt = Path(tmp_proj) / "proj__wt_deadbeef"
    project.mkdir()
    (project / ".git").mkdir()
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x", encoding="utf-8")
    rows = [
        {
            "pipeline_id": "wtsrc12345678",
            "task.id": "wtsrc12345678",
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": _json.dumps(
                {
                    "path": str(wt),
                    "mode": "worktree",
                    "branch": "task/wtsrc12345678",
                    "project_root": str(project),
                }
            ),
        }
    ]
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("wtsrc12345678"))
    assert fail is None
    assert meta is not None
    assert meta["path"] == str(wt)
    assert "__wt_" in meta["path"]
    assert meta["branch"] == "task/wtsrc12345678"


def test_inherit_worktree_source_lost_git_rejected(tool_module, monkeypatch, tmp_proj):
    """worktree 源 .git 失效 → git 身份失效拒绝（后续合并找不到分支）。"""
    import asyncio
    import json as _json

    project = Path(tmp_proj) / "bare_proj"
    wt = Path(tmp_proj) / "bare_proj__wt_deadbeef"
    project.mkdir()
    wt.mkdir()
    rows = [
        {
            "pipeline_id": "bare123456789",
            "task.id": "bare123456789",
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": _json.dumps(
                {"path": str(wt), "mode": "worktree", "project_root": str(project)}
            ),
        }
    ]
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("bare123456789"))
    assert meta is None
    assert fail is not None
    assert "git 身份已失效" in fail.error


def test_inherit_dead_coordinate_rejected_not_downgraded(tool_module, monkeypatch, tmp_proj):
    """坐标已死（源已合并清理，目录不存在）→ 显式报错，不静默回落 project_root。"""
    import asyncio
    import json as _json

    project = Path(tmp_proj) / "proj"
    wt = Path(tmp_proj) / "proj__wt_gone"
    project.mkdir()
    rows = [
        {
            "pipeline_id": "dead123456789",
            "task.id": "dead123456789",
            "run_status": "completed",
            "task.status": "completed",
            "task.ws_meta": _json.dumps(
                {
                    "path": str(wt),
                    "mode": "worktree",
                    "branch": "task/dead123456789",
                    "project_root": str(project),
                }
            ),
        }
    ]
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("dead123456789"))
    assert meta is None
    assert fail is not None
    assert "工作空间已不存在" in fail.error


def test_inherit_parent_ws_meta_never_serves_as_source_coordinates(
    tool_module, monkeypatch, tmp_proj
):
    """源任务行只有 lineage.parent_ws_meta（父链坐标）→ fail-closed。

    父链坐标不是源任务自身坐标：曾把继承路径静默漂移到父会话目录
    （2026-08-30 诊断：同参三次提交第 3 次合并目标漂到会话工作区）。
    """
    import asyncio
    import json as _json

    rows = [
        {
            "pipeline_id": "owncoord12345",
            "task.id": "owncoord12345",
            "run_status": "failed",
            "task.status": "failed",
            "lineage.parent_ws_meta": _json.dumps(
                {"path": str(Path(tmp_proj) / "sessions"), "mode": "plain"}
            ),
        }
    ]
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = asyncio.run(tool._resolve_inherit_coordinate("owncoord12345"))
    assert meta is None
    assert fail is not None
    assert "没有工作空间坐标" in fail.error
