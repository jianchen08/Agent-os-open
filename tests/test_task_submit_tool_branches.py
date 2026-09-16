# @feature: FP-MIGR task_submit 分支补测 | @ci: python-coverage
"""task_submit 工具未覆盖分支的行为测试（解析/闸门/降级分支矩阵）。

与 test_task_submit_params.py（参数可用性矩阵）主题不重叠，本文件覆盖：
1. 短 id 解析矩阵（_resolve_short_id：精确/前缀唯一/歧义/无命中/空串）
2. workspace 安全校验分支（盘符根/系统目录/配置工作空间根/无效路径/降级跳过）
3. goal 与 acceptance_criteria 形态归一（JSON 串/纯文本/非 dict/部分无效指标剔除）
4. 继承参数校验与源解析（pipe 源查找、workspace 跨容器/已消失拒绝）
5. agent registry 注入往返、服务故障/未命中磁盘回退（含 config_id 扫描与损坏 yaml 跳过）
6. state 桥降级语义（读取失败 None、依赖 fail-closed、父存在性兜底、项目归属单跳继承）
7. 派发编排隔离语义（出生后登记失败不影响提交、描述超长/L2 越权闸门）

注：本文件为 tests/ 根新增文件，**需登记插桩车道白名单**（scripts/coverage_exempt.py
的既有车道清单，由仓库维护者登记）；登记前不在插桩车道内。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from tests._stdlib_guard import ensure_stdlib_module

# 车道共跑防线：某条导入链会让插件同名模块（pipeline/types.py，包内相对
# 导入顶层即炸）劫持裸名 "types"。导入 SimpleNamespace/ModuleType 前恢复 stdlib 绑定。
ensure_stdlib_module("types")
from types import ModuleType, SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
# isolation 是包（import isolation.workspace）、project_registry 是共享平铺模块，
# 均需其父目录入 sys.path
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

_MOD_NAME = "task_submit_tool_branches_test_mod"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    """模块级 sys.path 注入（teardown 恢复）。

    tasks 目录含 workspace.py（模块）而 system 目录含 workspace/（包），两者对
    `import workspace` 解析冲突——用 fixture 管理并恢复，不污染同进程其它测试。
    """
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SYSTEM_DIR, _SHARED_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)


@pytest.fixture(scope="module")
def tool_module():
    """用 importlib 以唯一模块名加载 tool.py（避免与其它插件的平铺 tool 冲突）。

    按 importlib 标准做法登记进 sys.modules（coverage 按模块名过滤/双实例防线
    都依赖该登记），模块面结束后移除。
    """
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _TOOL_DIR / "tool.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(_MOD_NAME, None)
        raise
    yield mod
    sys.modules.pop(_MOD_NAME, None)


@pytest.fixture(autouse=True)
def _reset_module_injections(tool_module, tmp_path_factory, monkeypatch):
    """模块级注入点（chat sender / registry lookup / state reader）用后复位。

    全部是 tool 模块实例上的全局钩子，不复位会跨用例顺序耦合。
    另：用户根钉到空 tmp——mode 键磁盘回退的出厂命中断言不得依赖机器真实
    播种副本（随内核生命周期演进）。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path_factory.mktemp("user-root")))
    yield
    tool_module._chat_sender = None
    tool_module._agent_registry_lookup = None
    tool_module._state_reader = None


class FakeTask:
    """最小任务对象（metadata 承载 submitted_by_level 等归属字段）。"""

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


def make_tool(tool_module, service: FakeTaskService | None = None):
    """构造 TaskSubmitTool，monkeypatch 服务提供者与校验钩子（沿用 params 文件惯例）。

    chat sender 假实现按统一出生协议分段响应：create → 身份登记（no_dispatch）
    → 执行派发（background）→ 提交者管道登记（no_dispatch + task.owned.*）。
    captured["params"] 保留出生登记调用，captured["dispatch"] 保留执行派发调用，
    captured["calls"] 为全部调用。
    """
    service = service or FakeTaskService()
    captured: dict = {"calls": []}

    tool = tool_module.TaskSubmitTool()
    tool._get_task_service = lambda: service

    async def _fake_validate_target(target_id, level):
        return (True, "", "")

    tool._validate_target_agent = _fake_validate_target
    tool._check_parent_ownership = lambda level, pid: (True, None)

    async def fake_sender(params: dict) -> dict:
        captured["calls"].append(params)
        if params.get("create"):
            captured["params"] = params
            return {"status": "created", "pipeline_id": "a1b2c3d4e5f6"}
        if params.get("no_dispatch"):
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        captured["dispatch"] = params
        return {"status": "dispatched", "pipeline_id": params.get("pipeline_id", "")}

    tool_module.set_chat_sender(fake_sender)
    return tool, captured


def base_inputs(**overrides):
    inputs = {
        "goal_title": "测试任务",
        "goal_description": "分支补测",
        "target_type": "agent",
        # P3 迁移后编码执行者驻 mode_coding 包（真实磁盘可解析，完备性身份
        # 字段经模式包键回退链取得）
        "target_id": "mode_coding/code_writer",
        "parent_agent_level": 2,
    }
    inputs.update(overrides)
    return inputs


def registry_hook(config_by_id: dict):
    """构造 agent_registry 查询钩子（约定签名 async (agent_id) -> dict | None）。"""

    async def lookup(agent_id: str):
        return config_by_id.get(agent_id)

    return lookup


def restore_real_target_validation(tool):
    """去掉 make_tool 注入的实例级 mock，恢复类级真实目标校验路径。"""
    del tool._validate_target_agent


@pytest.fixture
def tmp_proj():
    """仓库根内的临时目录（同容器校验要求源空间在仓库内）。"""
    d = tempfile.mkdtemp(dir=str(_REPO_ROOT))
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


# ── 服务提供者 shim 与注入点往返 ──────────────────────────────


def test_service_provider_shim_has_no_sidecar_services(tool_module):
    """shim 对任意 sidecar 服务键返回 None（文档化降级：调用方自持守卫）。"""
    provider = tool_module._get_service_provider()
    assert provider is not None
    for key in ("task_worker", "agent_registry", "workspace_lifecycle_manager"):
        assert provider.get(key) is None


@pytest.mark.asyncio
async def test_agent_registry_lookup_injection_roundtrip(tool_module):
    """set_agent_registry_lookup 注入后按 id 查询；未命中返回 None（磁盘回退信号）。"""
    tool_module.set_agent_registry_lookup(registry_hook({"orch": {"level": "L2"}}))
    tool = tool_module.TaskSubmitTool()
    cfg = await tool._get_agent_config_from_registry("orch")
    assert cfg is not None
    assert cfg.level == "L2"
    assert cfg.is_active is True
    assert await tool._get_agent_config_from_registry("nobody") is None


def test_state_reader_injection_roundtrip(tool_module):
    """set_state_reader 注入后 _get_state_reader 可取回（读取桥接线契约）。"""
    reader = lambda: []  # noqa: E731
    tool_module.set_state_reader(reader)
    assert tool_module._get_state_reader() is reader


# ── 短 id 解析矩阵（_resolve_short_id）────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,candidate,expected",
    [
        # 空候选原样返回
        ([{"pipeline_id": "aaaaaaaa0001dead"}], "", ""),
        # 精确命中 pipeline_id → 原样
        ([{"pipeline_id": "aaaaaaaa0001"}], "aaaaaaaa0001", "aaaaaaaa0001"),
        # 精确命中 task.owned.<id>.* 键 → 原样
        ([{"task.owned.ownedbb000001.title": "x"}], "ownedbb000001", "ownedbb000001"),
        # ≤12 位前缀唯一命中 pipeline_id → 全 id
        ([{"pipeline_id": "cccccccc0001dead"}], "cccccccc0001", "cccccccc0001dead"),
        # ≤12 位前缀唯一命中 task.owned.<id> → owned 全 id
        ([{"task.owned.ownedcc000001.status": "running"}], "ownedcc", "ownedcc000001"),
        # 多命中（两条 pipeline_id 同前缀）→ AMBIGUOUS 标记
        (
            [{"pipeline_id": "dddd00000001aaaa"}, {"pipeline_id": "dddd00000001bbbb"}],
            "dddd00000001",
            "AMBIGUOUS:dddd00000001",
        ),
        # 无命中（≤12 位）→ 原样返回交给既有校验报错
        ([{"pipeline_id": "eeeeeeee0001"}], "ffffffffffff", "ffffffffffff"),
        # 候选超过 12 位不做前缀扫描 → 原样返回
        ([{"pipeline_id": "aaaaaaaa0001dead"}], "aaaaaaaa0001dead", "aaaaaaaa0001dead"),
    ],
)
async def test_resolve_short_id_matrix(tool_module, rows, candidate, expected):
    assert await tool_module._resolve_short_id(rows, candidate) == expected


def test_short_id_truncates_full_and_keeps_short(tool_module):
    """_short_id：全 id 截 12 位；短 id 与空串幂等（原样返回）。"""
    assert tool_module._short_id("a1b2c3d4e5f67890") == "a1b2c3d4e5f6"
    assert tool_module._short_id("a1b2c3d4e5f6") == "a1b2c3d4e5f6"
    assert tool_module._short_id("") == ""


# ── state 聚合读取桥 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_state_rows_filters_non_dict_rows(tool_module):
    """读取桥只保留 dict 行（跨边界脏数据不进闸门）。"""
    tool_module.set_state_reader(
        lambda: [{"pipeline_id": "p1"}, "junk", 42, None]
    )
    tool = tool_module.TaskSubmitTool()
    rows = await tool._read_state_rows()
    assert rows == [{"pipeline_id": "p1"}]


@pytest.mark.asyncio
async def test_read_state_rows_reader_crash_returns_none(tool_module, monkeypatch):
    """读取桥抛异常 → None（fail-closed 语义由调用方决定，不崩提交）。"""

    def boom():
        raise RuntimeError("pipeline-state 不可用")

    monkeypatch.setattr(tool_module, "_state_reader", boom)
    tool = tool_module.TaskSubmitTool()
    assert await tool._read_state_rows() is None


@pytest.mark.asyncio
async def test_dependency_check_fail_closed_when_state_bridge_down(tool_module, monkeypatch):
    """state 桥不可用（读取异常）时依赖校验 fail-closed：全部依赖判缺失并拒绝。"""

    def boom():
        raise RuntimeError("桥断")

    monkeypatch.setattr(tool_module, "_state_reader", boom)
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, dependencies=["dep000000001"]))
    assert not result.success
    assert result.error_code == "DEPENDENCY_NOT_FOUND"
    assert "dep000000001" in result.error


# ── _get_task_service 懒解析（service_access 懒加载 + 缓存）──


def test_get_task_service_resolves_via_service_access_and_caches(tool_module, monkeypatch):
    """首次经 service_access 解析并缓存；再次调用不再触达解析源。"""
    svc = FakeTaskService()
    calls: list[int] = []
    fake_mod = ModuleType("service_access")

    def get_task_service():
        calls.append(1)
        return svc

    fake_mod.get_task_service = get_task_service
    monkeypatch.setitem(sys.modules, "service_access", fake_mod)

    tool = tool_module.TaskSubmitTool()
    assert tool._get_task_service() is svc
    assert tool._get_task_service() is svc
    assert calls == [1]


def test_get_task_service_none_is_not_cached(tool_module, monkeypatch):
    """service_access 返回 None（sidecar 未接线）→ 不缓存，每次重查。"""
    fake_mod = ModuleType("service_access")
    fake_mod.get_task_service = lambda: None
    monkeypatch.setitem(sys.modules, "service_access", fake_mod)

    tool = tool_module.TaskSubmitTool()
    assert tool._get_task_service() is None
    assert tool._task_service is None
    assert tool._get_task_service() is None


# ── goal / acceptance_criteria 形态归一 ──────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # JSON 串形态解析为对象
        ('{"title": "t1", "description": "d1"}', {"title": "t1", "description": "d1"}),
        # 纯文本（非 JSON）→ 包装为 title
        ("整理仓库目录", {"title": "整理仓库目录"}),
    ],
)
def test_parse_goal_input_string_forms(tool_module, raw, expected):
    assert tool_module.TaskSubmitTool._parse_goal_input({"goal": raw}) == expected


@pytest.mark.parametrize("bad", ["file_check", ["file_check"], 42])
def test_normalize_acceptance_criteria_non_dict_reset_to_empty(tool_module, bad):
    """非 dict 的 acceptance_criteria 归一为空 dict（不炸、不拒绝——按未传处理）。"""
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(bad)
    assert normalized == {}
    assert fail is None


def test_partial_invalid_metric_ids_pruned_valid_kept(tool_module, monkeypatch):
    """部分 key 非法 → 剔除无效项保留有效项（降级不阻断）。"""
    monkeypatch.setattr(
        tool_module, "_load_metric_definitions", lambda: {"file_check": {"name": "file_check"}}
    )
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria(
        {
            "file_check": {"input_params": {"path": "a.txt"}},
            "pass_threshold": {},
        }
    )
    assert fail is None
    assert list(normalized) == ["file_check"]
    assert normalized["file_check"]["input_params"]["path"] == "a.txt"


def test_all_invalid_metric_ids_rejected(tool_module, monkeypatch):
    """全部 key 非法 → INVALID_METRIC_ID 拒绝（不静默清空后提交）。"""
    monkeypatch.setattr(
        tool_module, "_load_metric_definitions", lambda: {"file_check": {"name": "file_check"}}
    )
    normalized, fail = tool_module.TaskSubmitTool._normalize_acceptance_criteria({"nope": {}})
    assert normalized == {}
    assert fail is not None
    assert fail.error_code == "INVALID_METRIC_ID"


def test_validate_metric_ids_empty_criteria_passthrough(tool_module, monkeypatch):
    """空验收标准直通（valid 集合在场也不产生剔除记录）。"""
    monkeypatch.setattr(
        tool_module, "_load_metric_definitions", lambda: {"file_check": {"name": "file_check"}}
    )
    filtered, invalid = tool_module._validate_metric_ids({})
    assert filtered == {}
    assert invalid == []


def test_metric_without_required_schema_passes(tool_module, monkeypatch):
    """指标定义无 required → 不校验参数（纯数据驱动，无 schema 即无硬约束）。"""
    monkeypatch.setattr(
        tool_module,
        "_load_metric_definitions",
        lambda: {"free_metric": {"name": "free_metric", "input_schema": {}}},
    )
    result = tool_module._validate_required_metric_params({"free_metric": {}})
    assert result is None


def test_metric_non_dict_config_treated_as_missing_params(tool_module, monkeypatch):
    """指标配置为非 dict → 按空参数处理，必填字段判缺失拒绝（fail-closed）。"""
    monkeypatch.setattr(
        tool_module,
        "_load_metric_definitions",
        lambda: {"file_check": {"input_schema": {"required": ["path"]}}},
    )
    result = tool_module._validate_required_metric_params({"file_check": "garbage"})
    assert result is not None
    assert result.error_code == "INVALID_METRIC_PARAMS"
    assert "path" in result.error


# ── 描述长度硬限制（边界性质：2000 放行 / 2001 拒绝）─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["x" * 2001, "长" * 2500])
async def test_description_too_long_rejected(tool_module, description):
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, goal_description=description)
    )
    assert not result.success
    assert result.error_code == "DESCRIPTION_TOO_LONG"
    assert "2000" in result.error


@pytest.mark.asyncio
async def test_description_at_limit_passes(tool_module):
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, goal_description="x" * 2000)
    )
    assert result.success, result.error
    assert captured["params"]["state"]["task.description"] == "x" * 2000


# ── L2/L3 显式 parent_task_id 闸门 ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [2, 3])
async def test_l2l3_explicit_parent_task_id_rejected(tool_module, level):
    """L2/L3 显式指定 parent_task_id 一律拦截（归属由系统注入）。"""
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=level, parent_task_id="someoneelse1", task_id="ctx-task-1")
    )
    assert not result.success
    assert result.error_code == "L2_CANNOT_SPECIFY_PARENT_TASK_ID"
    assert f"L{level}" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [2, 3])
async def test_validate_parent_task_id_requires_injected_parent_for_l2l3(tool_module, level):
    """L2/L3 无 parent_task_id（注入链断裂）→ 纵深防御拒绝。"""
    tool = tool_module.TaskSubmitTool()
    assert await tool._validate_parent_task_id(level, None) is False


# ── L1 parent_task_id 存在性（服务 + state 兜底）─────────────


@pytest.mark.asyncio
async def test_l1_unknown_parent_task_id_rejected(tool_module):
    """L1 指定的父任务在服务与 state 聚合中都不存在（无读取桥）→ 拒绝挂载。"""
    tool, _ = make_tool(tool_module, FakeTaskService())
    result = await tool.execute(
        base_inputs(parent_agent_level=1, parent_task_id="ghost00000000")
    )
    assert not result.success
    assert result.error_code == "L2_CANNOT_SPECIFY_PARENT_TASK_ID"


@pytest.mark.asyncio
async def test_l1_parent_exists_in_service_passes(tool_module):
    """父任务在服务中存在 → 存在性通过（不走 state 兜底）。"""
    tool = tool_module.TaskSubmitTool()
    tool._task_service = FakeTaskService({"p1": FakeTask("p1")})
    assert await tool._validate_parent_task_id(1, "p1") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,parent_id",
    [
        # state 行 pipeline_id 精确命中
        ([{"pipeline_id": "pipeabc123456"}], "pipeabc123456"),
        # state 行 task.owned.<id>.* 键命中（容器登记型任务）
        ([{"task.owned.ownedabc12345.title": "x"}], "ownedabc12345"),
    ],
)
async def test_l1_parent_exists_in_state_fallback(tool_module, monkeypatch, rows, parent_id):
    """服务缺失的父任务经 state 聚合兜底确认存在（登记型任务 = task.owned.*）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    tool._task_service = FakeTaskService()  # 服务无记录 → 走 state 兜底
    assert await tool._validate_parent_task_id(1, parent_id) is True


@pytest.mark.asyncio
async def test_parent_exists_in_state_bridge_down_returns_false(tool_module, monkeypatch):
    """state 桥不可用（读取返回非 list）→ 兜底判不存在（False）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: None)
    tool = tool_module.TaskSubmitTool()
    assert await tool._parent_exists_in_state("ghost00000000") is False


def test_parent_ownership_without_service_passes_through(tool_module):
    """无任务服务时归属校验不阻断（交由后续存在性校验）。"""
    tool = tool_module.TaskSubmitTool()
    tool._task_service = None

    def _no_service():
        return None

    tool._get_task_service = _no_service
    ok, err = tool._check_parent_ownership(2, "someparent1234")
    assert ok is True
    assert err is None


# ── 项目挂靠解析分支 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_binding_bootstraps_shared_root(tool_module, monkeypatch, tmp_path):
    """共享层未入 sys.path 时挂靠解析自举注入（防线上 sys.path 组装缺失）。"""
    import project_registry as pr

    folder = tmp_path / "proj_folder_bootstrap"
    folder.mkdir()
    monkeypatch.setattr(pr, "load_project_paths", lambda: {"proj00000002": str(folder)})
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)

    shared_root = str(_SHARED_DIR)
    removed_count = 0
    bootstrapped = False
    # 工具模块导入时已无条件自举过一次共享层（重复驻留），须清空全部条目
    while shared_root in sys.path:
        sys.path.remove(shared_root)
        removed_count += 1
    try:
        result = await tool.execute(
            base_inputs(parent_agent_level=1, project_id="proj00000002")
        )
        # 挂靠解析路径自举注入共享层（非残留条目的效果）
        bootstrapped = shared_root in sys.path
    finally:
        if removed_count and shared_root not in sys.path:
            sys.path.insert(0, shared_root)
    assert result.success, result.error
    assert bootstrapped, "挂靠解析未把共享层自举进 sys.path"
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == str(folder)


@pytest.mark.asyncio
async def test_project_folder_missing_rejected(tool_module, monkeypatch, tmp_path):
    """登记在场但文件夹已删除 → PROJECT_FOLDER_MISSING fail-honest。"""
    import project_registry as pr

    monkeypatch.setattr(
        pr, "load_project_paths", lambda: {"proj00000003": str(tmp_path / "deleted_folder")}
    )
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, project_id="proj00000003"))
    assert not result.success
    assert result.error_code == "PROJECT_FOLDER_MISSING"


@pytest.mark.asyncio
async def test_l2_child_project_inheritance_without_parent_row(tool_module, monkeypatch):
    """父任务行不在 state 聚合（单跳读无命中）→ 子任务不挂项目（独立链）。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "other000000001"}]
    )
    tool, captured = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=2, task_id="parent-task-1"))
    assert result.success, result.error
    assert "task.parent_project_id" not in captured["params"]["state"]


# ── inherit 参数校验与源解析 ─────────────────────────────────


@pytest.mark.parametrize(
    "inp,expected",
    [
        ({"inherit": {"from": "src1", "mode": "pipe"}}, "src1"),
        ({"inherit": {}}, ""),
        ({"inherit": {"from": "", "mode": "pipe"}}, ""),
        ({"inherit_from": "flat1"}, "flat1"),
        ({}, ""),
    ],
)
def test_inherit_from_id_of_flat_and_nested(tool_module, inp, expected):
    """扁平 inherit_from 与旧式 inherit.from 同口径提取。"""
    assert tool_module._inherit_from_id_of(inp) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inherit_from,inherit_mode",
    [
        ("", "pipe"),  # 缺 from
        ("src0000000001", 123),  # mode 非法类型
        ("src0000000001", None),  # mode 缺失
    ],
)
async def test_inherit_invalid_params_rejected(tool_module, inherit_from, inherit_mode):
    """inherit 缺 from / mode 类型非法 → INVALID_INHERIT_PARAMS。"""
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, inherit_from=inherit_from, inherit_mode=inherit_mode)
    )
    assert not result.success
    assert result.error_code == "INVALID_INHERIT_PARAMS"


@pytest.mark.asyncio
async def test_inherit_pipe_source_found_in_state(tool_module, monkeypatch):
    """pipe 继承源在 state 聚合命中 → 观测到源管道 id，提交继续。"""
    monkeypatch.setattr(
        tool_module,
        "_state_reader",
        lambda: [{"pipeline_id": "srcpipe000001", "task.id": "srcpipe000001"}],
    )
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(task_id="parent_p", inherit_from="srcpipe000001", inherit_mode="pipe")
    )
    assert result.success, result.error
    assert captured["dispatch"]["pipeline_id"] == "a1b2c3d4e5f6"


@pytest.mark.asyncio
async def test_inherit_pipe_source_missing_degrades_to_empty_history(tool_module, monkeypatch):
    """pipe 继承源不在 state 聚合 → 对话历史为空但不阻断提交（观测性降级）。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "unrelated000001"}]
    )
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(task_id="parent_p", inherit_from="srcpipe000001", inherit_mode="pipe")
    )
    assert result.success, result.error


@pytest.mark.asyncio
async def test_inherit_workspace_cross_container_rejected(tool_module, monkeypatch, tmp_path_factory):
    """源任务工作空间在其它容器（仓库根外）→ 拒绝继承，防产物落错容器。"""
    outside = tmp_path_factory.mktemp("outside_container")
    rows = [
        {
            "pipeline_id": "xcontainer123",
            "task.id": "xcontainer123",
            "ws_meta": json.dumps({"path": str(outside), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    path, fail = await tool._extract_inherited_workspace("xcontainer123")
    assert path is None
    assert fail is not None
    assert "其它容器" in fail.error


@pytest.mark.asyncio
async def test_inherit_workspace_vanished_rejected(tool_module, monkeypatch, tmp_proj):
    """源任务工作空间目录已消失 → 拒绝继承并引导重提。"""
    vanished = Path(tmp_proj) / "gone_dir"
    rows = [
        {
            "pipeline_id": "vanished12345",
            "task.id": "vanished12345",
            "ws_meta": json.dumps({"path": str(vanished), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    path, fail = await tool._extract_inherited_workspace("vanished12345")
    assert path is None
    assert fail is not None
    assert "已不存在" in fail.error


# ── 依赖短 id 解析（歧义/非字符串保真）───────────────────────


@pytest.mark.asyncio
async def test_ambiguous_dependency_short_id_kept_then_rejected(tool_module, monkeypatch):
    """歧义短 id 依赖保真保留原样 → 依赖存在性校验判缺失拒绝（不猜测改写）。"""
    monkeypatch.setattr(
        tool_module,
        "_state_reader",
        lambda: [{"pipeline_id": "dup0000000001"}, {"pipeline_id": "dup0000000002"}],
    )
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, dependencies=["dup"]))
    assert not result.success
    assert result.error_code == "DEPENDENCY_NOT_FOUND"
    assert "dup" in result.error


@pytest.mark.asyncio
async def test_non_string_dependency_kept_verbatim(tool_module, monkeypatch):
    """非字符串依赖项原样保留（不做解析不改写）→ 存在性校验判缺失拒绝。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "real0000000001"}]
    )
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, dependencies=[42]))
    assert not result.success
    assert result.error_code == "DEPENDENCY_NOT_FOUND"
    assert "42" in result.error


# ── 目标与依赖闸门 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_target_type_rejected(tool_module):
    tool, _ = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=1)
    inputs["target_type"] = ""
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "MISSING_TARGET_TYPE"


@pytest.mark.asyncio
async def test_missing_target_id_rejected(tool_module):
    tool, _ = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=1)
    inputs["target_id"] = ""
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "MISSING_TARGET_ID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level_str,is_active,expected_code",
    [
        # 目标是 L1（主调度层）→ 不接受执行委托
        ("L1", True, "TARGET_AGENT_IS_L1"),
        # 目标级别不比提交者低（L2→L2）→ 层级闸门拒绝
        ("L2", True, "TARGET_AGENT_LEVEL_INVALID"),
        # level 非法（无法判定层级闸门）→ 显式拒绝而非静默放行
        ("X", True, "TARGET_AGENT_LEVEL_MISSING"),
        # 级别合法但已禁用 → 拒绝派发
        ("L3", False, "TARGET_AGENT_INACTIVE"),
    ],
)
async def test_target_agent_gates_reject(tool_module, level_str, is_active, expected_code):
    """目标 Agent 层级/禁用闸门（registry 路径真实校验，非 mock 短路）。

    统一 L2 提交者 + 注入 task_id（否则根任务闸门先于目标闸门拒绝）。
    """
    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module.set_agent_registry_lookup(
        registry_hook({"code_writer": {"level": level_str, "is_active": is_active}})
    )
    result = await tool.execute(
        base_inputs(parent_agent_level=2, target_id="code_writer", task_id="ctx-task-1")
    )
    assert not result.success
    assert result.error_code == expected_code


# ── agent registry 故障降级与磁盘回退 ────────────────────────


@pytest.mark.asyncio
async def test_registry_service_crash_falls_back_to_disk_not_found(tool_module):
    """registry 服务抛异常 → 降级磁盘回退；磁盘也无 → TARGET_AGENT_NOT_FOUND。"""
    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)

    async def boom(target_id):
        raise RuntimeError("agent_manager 服务不可用")

    tool_module.set_agent_registry_lookup(boom)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, target_id="no_such_agent_zzz")
    )
    assert not result.success
    assert result.error_code == "TARGET_AGENT_NOT_FOUND"
    assert "no_such_agent_zzz" in result.error


@pytest.mark.asyncio
async def test_registry_service_miss_falls_back_to_disk(tool_module):
    """registry 未命中（返回 None，服务正常）→ 同样磁盘回退判定不存在。"""

    async def miss(target_id):
        return None

    tool_module.set_agent_registry_lookup(miss)
    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, target_id="no_such_agent_zzz")
    )
    assert not result.success
    assert result.error_code == "TARGET_AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_disk_lookup_resolves_mode_agent_key_real_dispatch(tool_module):
    """模式命名空间键全链派发：mode_coding/code_writer（出厂包内 L3 执行者）
    → 磁盘回退解析包内 yaml，L3 目标对 L2 提交者放行并按新键派发。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    # 不注入 registry 钩子 → 磁盘回退；task_id 注入父链（L2 不能建根任务）
    result = await tool.execute(
        base_inputs(parent_agent_level=2, target_id="mode_coding/code_writer", task_id="ctx-task-1")
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_coding/code_writer"


@pytest.mark.asyncio
async def test_disk_lookup_dispatches_mode_orchestrator_from_main(tool_module):
    """模式命名空间键全链派发：mode_coding/programming_orchestrator_agent_v2
    （出厂包内 L2 编排者）作为 main（L1）直接下级派发成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    # main（L1）建根任务：不传 task_id（父链参数为 L2+ 专属）
    result = await tool.execute(
        base_inputs(
            parent_agent_level=1,
            target_id="mode_coding/programming_orchestrator_agent_v2",
        )
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_coding/programming_orchestrator_agent_v2"


@pytest.mark.asyncio
async def test_disk_lookup_dispatches_mode_godot_expert_real_dispatch(tool_module):
    """模式命名空间键全链派发（mode_godot 包，P3-⑤ 立项）：mode_godot/godot_expert
    （出厂包内 L3 执行者）→ 磁盘回退解析包内 yaml，L2 提交者派单成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    result = await tool.execute(
        base_inputs(parent_agent_level=2, target_id="mode_godot/godot_expert", task_id="ctx-task-1")
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_godot/godot_expert"


@pytest.mark.asyncio
async def test_disk_lookup_dispatches_mode_godot_orchestrator_from_main(tool_module):
    """模式命名空间键全链派发（mode_godot 包）：mode_godot/godot_orchestrator_agent
    （出厂包内 L2 执行型编排者）作为 main（L1）直接下级派发成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    result = await tool.execute(
        base_inputs(
            parent_agent_level=1,
            target_id="mode_godot/godot_orchestrator_agent",
        )
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_godot/godot_orchestrator_agent"


def test_disk_lookup_skips_corrupt_yaml_during_config_id_scan(tool_module, monkeypatch):
    """config_id 扫描期单个 yaml 损坏 → 跳过继续（except-continue 不阻断查找）。"""
    import yaml as real_yaml

    real_safe_load = real_yaml.safe_load

    def selective_safe_load(stream):
        name = str(getattr(stream, "name", "")).replace("\\", "/")
        if name.endswith("system/evolution_agent.yaml"):
            raise real_yaml.YAMLError("模拟配置损坏")
        return real_safe_load(stream)

    monkeypatch.setattr(real_yaml, "safe_load", selective_safe_load)
    found, level_str, level, is_active, corrupt_path = (
        tool_module.TaskSubmitTool._lookup_agent_from_disk("evolution")
    )
    # 唯一 config_id 命中的文件在扫描期损坏被跳过 → 与"不存在"同型返回（无损坏归因路径）
    assert found is False
    assert corrupt_path == ""


@pytest.mark.parametrize(
    ("target_id", "yaml_rel", "expected_level"),
    [
        # 扁平布局：文件名与 id 无关、内容 config_id 命中
        ("cfgscan_target_a", "writer_settings.yaml", "L3"),
        # 深层嵌套 + 不同 level 值（防对单一文件名/层级硬编码）
        ("cfgscan_target_b", "nested/deep/inner_probe.yaml", "L2"),
    ],
)
def test_disk_lookup_hits_by_config_id_content_when_filename_misses(
    tool_module, monkeypatch, tmp_path, target_id, yaml_rel, expected_level
):
    """config_id 兜底扫描（靶行 tool.py 2315-2316：命中赋值 + break）。

    文件名 rglob 未命中（无 `{target_id}.yaml`）→ 遍历全部 yaml 逐个
    safe_load，按内容 `config_id == target_id` 命中并停扫。config_dir 由模块
    `__file__` 上溯五级推导，把 `__file__` 注入 tmp 假树后对真实落盘 yaml 走
    真扫描（文件 IO 为真实依赖，仅定位锚点是注入的）。
    """
    config_dir = tmp_path / "config" / "agents"
    yaml_file = config_dir / yaml_rel
    yaml_file.parent.mkdir(parents=True)
    yaml_file.write_text(
        f"config_id: {target_id}\nlevel: {expected_level}\nis_active: true\n",
        encoding="utf-8",
    )
    # 对照：文件名与内容都不命中的普通 yaml，不得被误收
    (config_dir / "plain_no_config_id.yaml").write_text("level: L1\n", encoding="utf-8")

    fake_tool_py = tmp_path / "plugins" / "shared" / "tools" / "task_submit" / "tool.py"
    monkeypatch.setattr(tool_module, "__file__", str(fake_tool_py))

    config, corrupt_path = tool_module.TaskSubmitTool._load_agent_yaml_dict(target_id)

    assert corrupt_path == "", "按内容命中是健康路径，不得带损坏归因"
    assert isinstance(config, dict)
    assert config.get("config_id") == target_id, "必须按内容命中目标 yaml，而非旁落普通文件"
    assert config.get("level") == expected_level


def test_disk_lookup_resolves_mode_agent_key(tool_module, monkeypatch, tmp_path):
    """两级解析第二级：config/agents 未命中 → 模式包 agents/<stem>.yaml。

    mode_X/<stem> 键经 mode_keys 双根解析（用户副本优先），读出 level/is_active
    供派发层级闸门判定（模式体系设计稿 §3.3）。
    """
    pkg = tmp_path / "plugins" / "modes" / "mode_ghostmode"
    (pkg / "agents").mkdir(parents=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    (pkg / "agents" / "exec1.yaml").write_text("level: L3\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    found, level_str, level, is_active, corrupt_path = (
        tool_module.TaskSubmitTool._lookup_agent_from_disk("mode_ghostmode/exec1")
    )
    assert found is True
    assert (level_str, level, is_active, corrupt_path) == ("L3", 3, True, "")


# ── 派发编排隔离语义 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_owned_registration_failure_does_not_block_dispatch(tool_module):
    """提交者管道登记（task.owned.*）写失败 → 仅降级日志，提交结果不受影响。"""
    tool, captured = make_tool(tool_module)

    async def flaky_sender(params: dict) -> dict:
        state = params.get("state") or {}
        if params.get("no_dispatch") and any(
            k.startswith("task.owned.") for k in state
        ):
            raise RuntimeError("state 写入超时")
        if params.get("create"):
            captured["params"] = params
            return {"status": "created", "pipeline_id": "a1b2c3d4e5f6"}
        if params.get("no_dispatch"):
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        captured["dispatch"] = params
        return {"status": "dispatched", "pipeline_id": params.get("pipeline_id", "")}

    tool_module.set_chat_sender(flaky_sender)
    result = await tool.execute(
        base_inputs(
            parent_agent_level=2,
            pipeline_id="owner-pipe-1",
            session_id="sess-1",
            task_id="parent-task-1",
        )
    )
    assert result.success, result.error
    assert result.output["pipeline_id"] == "a1b2c3d4e5f6"
    # 出生三段（含执行派发）已完成；登记段失败不影响
    assert captured["dispatch"]["pipeline_id"] == "a1b2c3d4e5f6"


# ── workspace 安全校验分支 ───────────────────────────────────


@pytest.mark.parametrize("empty", [""])
def test_validate_workspace_path_empty_passes(tool_module, empty):
    """空 workspace 无需校验（未声明工作空间是合法形态）。"""
    assert tool_module._validate_workspace_path(empty) is None


@pytest.mark.parametrize("bad", [123, ["not-a-path"]])
def test_validate_workspace_path_invalid_type_rejected(tool_module, bad):
    """非路径类型（normpath 抛 TypeError）→ 显式报错而非崩溃。"""
    err = tool_module._validate_workspace_path(bad)
    assert err is not None
    assert "目标空间路径无效" in err


@pytest.mark.skipif(os.name != "nt", reason="Windows 盘符根目录检查")
@pytest.mark.parametrize("drive_root", ["C:\\", "D:\\"])
def test_validate_workspace_path_drive_root_rejected(tool_module, drive_root):
    """盘符根目录（C:\\ 与 D:\\）拒绝——任务必须在具体项目子目录执行。"""
    err = tool_module._validate_workspace_path(drive_root)
    assert err is not None
    assert "磁盘根目录" in err


@pytest.mark.skipif(os.name != "nt", reason="Windows 系统目录名单")
@pytest.mark.parametrize("sys_dir", [r"C:\Windows", r"C:\Program Files"])
def test_validate_workspace_path_system_dir_rejected(tool_module, sys_dir):
    """系统关键目录拒绝。"""
    err = tool_module._validate_workspace_path(sys_dir)
    assert err is not None
    assert "系统目录" in err


def test_validate_workspace_path_unix_root_rejected(tool_module, monkeypatch):
    """POSIX 形态根目录 "/" 拒绝（非 Windows 平台分支）。"""
    import posixpath

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os.path, "normpath", posixpath.normpath)
    err = tool_module._validate_workspace_path("/")
    assert err is not None
    assert "根目录" in err


def test_validate_workspace_path_config_root_rejected(tool_module, monkeypatch, tmp_proj):
    """workspace == 当前配置的工作空间根目录 → 拒绝（系统管理根不可作任务目标）。"""
    import isolation.workspace as iw

    monkeypatch.setattr(iw, "get_workspace_config_root", lambda: str(tmp_proj))
    err = tool_module._validate_workspace_path(tmp_proj)
    assert err is not None
    assert "工作空间根目录" in err


def test_validate_workspace_path_config_root_read_failure_skips_check(
    tool_module, monkeypatch, tmp_proj
):
    """配置工作空间根读取失败 → 跳过该检查（文档化降级，不阻断正常路径）。"""
    import isolation.workspace as iw

    def boom():
        raise RuntimeError("登记不可用")

    monkeypatch.setattr(iw, "get_workspace_config_root", boom)
    assert tool_module._validate_workspace_path(tmp_proj) is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows 系统目录名单")
async def test_execute_rejects_system_dir_workspace(tool_module):
    """端到端：系统目录作为 workspace → UNSAFE_WORKSPACE 失败信封。"""
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, workspace=r"C:\Windows"))
    assert not result.success
    assert result.error_code == "UNSAFE_WORKSPACE"


# ── _build_metadata / 任务级 execution_context（遗留装配面）──


def test_build_metadata_skips_non_dict_criteria(tool_module):
    """非 dict 验收标准跳过存储（告警不落账），其余字段照常装配。"""
    tool = tool_module.TaskSubmitTool()
    md = tool._build_metadata(
        {"session_id": "s-1", "parent_agent_level": 2, "workspace": ""},
        {"title": "t"},
        "not-a-dict",
    )
    assert "acceptance_criteria" not in md
    assert "evaluation_metric_ids" not in md
    assert md["session_id"] == "s-1"
    assert md["submitted_by_level"] == 2


def test_build_metadata_carries_task_level_execution_context(tool_module):
    """显式 workspace 落任务级 execution_context（供执行器透传）。"""
    tool = tool_module.TaskSubmitTool()
    md = tool._build_metadata(
        {"workspace": "/w", "workspace_mode": "plain", "isolation_level": "isolated"},
        {"title": "t"},
        {"file_check": {}},
    )
    assert md["acceptance_criteria"] == {"file_check": {}}
    assert md["evaluation_metric_ids"] == ["file_check"]
    assert md["execution_context"]["workspace"]["source_path"] == "/w"


@pytest.mark.parametrize(
    "inputs,expected_ws",
    [
        # 显式 workspace：带 source_path，拓扑缺省 worktree
        (
            {"workspace": "/w", "workspace_mode": "", "parent_task_id": "p1"},
            {"source_path": "/w", "mode": "worktree", "explicit": True},
        ),
        # 无显式 workspace 但有拓扑声明：source_path 留空
        (
            {"workspace_mode": "worktree"},
            {"source_path": "", "mode": "worktree"},
        ),
    ],
)
def test_task_level_execution_context_workspace_forms(tool_module, inputs, expected_ws):
    ec = tool_module.TaskSubmitTool._task_level_execution_context(inputs)
    assert ec["workspace"] == expected_ws
    if "parent_task_id" in inputs:
        assert ec["parent_task_id"] == inputs["parent_task_id"]
    # isolation 只在显式选择时落
    assert ("isolation" in ec) == bool(inputs.get("isolation_level"))


# ── 指标定义加载 fail-open 与评估说明展开 ────────────────────


def test_load_metric_definitions_missing_file_returns_empty(tool_module, monkeypatch, tmp_path):
    """指标定义文件缺失 → 空表（fail-open：不阻断提交，不编造指标）。"""
    monkeypatch.setattr(
        tool_module, "_metrics_config_path", lambda: tmp_path / "no_such_metrics.yaml"
    )
    assert tool_module._load_metric_definitions() == {}
    # 空表语义：不产生合法指标集合（校验跳过）
    assert tool_module._get_valid_metric_ids() is None


def test_build_evaluation_criteria_prompt_renders_known_metric(tool_module, monkeypatch):
    """已知指标按定义展开：说明 + 评估参数 JSON + 固定标题。"""
    monkeypatch.setattr(
        tool_module,
        "_load_metric_definitions",
        lambda: {
            "file_check": {
                "name": "file_check",
                "description": "文件检查",
            }
        },
    )
    prompt = tool_module._build_evaluation_criteria_prompt(
        {"file_check": {"input_params": {"path": "result.txt"}}}
    )
    assert tool_module._EVALUATION_PROMPT_HEADER in prompt
    assert "- file_check：文件检查" in prompt
    assert '"path"' in prompt and "result.txt" in prompt


@pytest.mark.parametrize(
    "definitions,criteria",
    [
        # 定义表为空（加载失败降级）→ 不展开
        ({}, {"file_check": {}}),
        # criteria 引用未定义指标 → 该项跳过后无内容
        ({"other_metric": {"description": "x"}}, {"file_check": {}}),
    ],
)
def test_build_evaluation_criteria_prompt_unknown_degrades_to_empty(
    tool_module, monkeypatch, definitions, criteria
):
    monkeypatch.setattr(tool_module, "_load_metric_definitions", lambda: definitions)
    assert tool_module._build_evaluation_criteria_prompt(criteria) == ""
    # 无验收标准同样不生成说明
    assert tool_module._build_evaluation_criteria_prompt({}) == ""


@pytest.mark.parametrize("bad_ec", ["not-a-dict", None])
def test_build_workspace_guidance_rejects_non_dict(tool_module, bad_ec):
    """execution_context 非 dict → 无工作空间提示（防御式空串）。"""
    assert tool_module._build_workspace_guidance(bad_ec) == ""


# ── 基础字段与注入参数闸门（execute 级）──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        # 缺标题
        ({"goal_title": ""}, "MISSING_GOAL"),
        # 描述空白（标题承载不了执行语义）
        ({"goal_description": "   "}, "MISSING_DESCRIPTION"),
    ],
)
async def test_base_gate_rejections(tool_module, overrides, expected_code):
    tool, _ = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, **overrides))
    assert not result.success
    assert result.error_code == expected_code


@pytest.mark.asyncio
async def test_missing_injected_level_rejected(tool_module):
    """parent_agent_level 未注入（调用链断裂）→ 系统错误信封，不猜测层级。"""
    tool, _ = make_tool(tool_module)
    inputs = base_inputs()
    inputs.pop("parent_agent_level")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "MISSING_INJECTED_PARAM"


@pytest.mark.asyncio
async def test_l2_without_injected_task_id_cannot_create_root(tool_module):
    """L2 无可注入父任务（注入链断裂）→ 拒绝创建根任务。"""
    tool, _ = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=2)
    inputs.pop("task_id", None)
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "L2_REQUIRES_PARENT_TASK"


@pytest.mark.asyncio
async def test_all_invalid_metric_ids_rejected_at_execute(tool_module, monkeypatch):
    """execute 级：验收标准全部 key 非法 → 短路返回 INVALID_METRIC_ID。"""
    monkeypatch.setattr(
        tool_module, "_load_metric_definitions", lambda: {"file_check": {"name": "file_check"}}
    )
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, acceptance_criteria={"pass_threshold": {}})
    )
    assert not result.success
    assert result.error_code == "INVALID_METRIC_ID"


# ── goal 形态归一补全（非 dict 非串）─────────────────────────


@pytest.mark.parametrize("bad_goal", [42, ["t", "d"]])
def test_parse_goal_input_non_dict_non_string_rejected(tool_module, bad_goal):
    """goal 为非 dict 且非串的异常形态 → 归一为 None（告警不炸）。"""
    assert tool_module.TaskSubmitTool._parse_goal_input({"goal": bad_goal}) is None


# ── 根任务 workspace 继承成功路径 ────────────────────────────


@pytest.mark.asyncio
async def test_root_task_inherits_workspace_from_source(tool_module, monkeypatch, tmp_proj):
    """根任务 inherit workspace：源任务坐标复用（不复制不初始化），继承不算显式。"""
    rows = [
        {
            "pipeline_id": "wsrc000000001",
            "task.id": "wsrc000000001",
            "ws_meta": json.dumps({"path": str(tmp_proj), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, inherit_from="wsrc000000001", inherit_mode="workspace")
    )
    assert result.success, result.error
    # 继承坐标写入 execution_context；无显式声明 → explicit=False
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == str(tmp_proj)
    assert ec["workspace"]["explicit"] is False
    # 继承回写 inputs 后随出生 state 可见（metadata 面不再双写）
    assert captured["dispatch"]["pipeline_id"] == "a1b2c3d4e5f6"


# ── 血缘 parent_ws_meta 出生写全 ─────────────────────────────


@pytest.mark.asyncio
async def test_parent_ws_meta_written_to_birth_state(tool_module):
    """父链工作空间坐标随出生契约写全（子任务 workspace_lifecycle 共享决策依据）。"""
    tool, captured = make_tool(tool_module)
    ws_meta = {"path": "D:/somewhere/parent_ws", "mode": "plain"}
    result = await tool.execute(
        base_inputs(
            parent_agent_level=2,
            pipeline_id="owner-pipe-1",
            task_id="parent-task-1",
            parent_ws_meta=ws_meta,
        )
    )
    assert result.success, result.error
    state = captured["params"]["state"]
    assert state["lineage.parent_ws_meta"] == ws_meta
    assert state["lineage.parent_pipeline_id"] == "owner-pipe-1"


# ── 依赖短 id 解析成功路径 ───────────────────────────────────


@pytest.mark.asyncio
async def test_dependency_short_id_resolves_and_passes_check(tool_module, monkeypatch):
    """唯一前缀命中 → 依赖解析为全 id 并通过存在性校验（出生 state 带全 id）。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "real0000000001"}]
    )
    tool, captured = make_tool(tool_module)
    result = await tool.execute(base_inputs(parent_agent_level=1, dependencies=["real"]))
    assert result.success, result.error
    assert captured["params"]["state"]["task.dependencies"] == ["real0000000001"]


@pytest.mark.asyncio
async def test_parent_exists_in_state_no_match_returns_false(tool_module, monkeypatch):
    """state 行在场但无命中（pipeline_id / task.owned.* 均不匹配）→ 兜底判不存在。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "other000000001"}]
    )
    tool = tool_module.TaskSubmitTool()
    tool._task_service = FakeTaskService()  # 服务无记录 → 走 state 兜底
    assert await tool._validate_parent_task_id(1, "ghost00000000") is False


# ── _attach_inherit_metadata pipe 带宽标记 ───────────────────


@pytest.mark.parametrize(
    "mode",
    ["pipe", ["pipe", "workspace"]],
    ids=["string-mode", "list-mode"],
)
def test_build_metadata_marks_pipe_inherit_bandwidth(tool_module, mode):
    """inherit 含 pipe（字符串或列表形态）→ 附带宽照键 inherit_pipe_from。"""
    tool = tool_module.TaskSubmitTool()
    md = tool._build_metadata(
        {"inherit_from": "src1", "inherit_mode": mode},
        {"title": "t"},
        {},
    )
    assert md["inherit"] == {"from": "src1", "mode": mode}
    assert md["inherit_pipe_from"] == "src1"
