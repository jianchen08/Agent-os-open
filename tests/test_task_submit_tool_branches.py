# @feature: FP-MIGR task_submit 分支补测 | @ci: python-coverage
"""task_submit 工具未覆盖分支的行为测试（解析/闸门/降级分支矩阵）。

与 test_task_submit_params.py（参数可用性矩阵）主题不重叠，本文件覆盖：
1. 短 id 解析矩阵（_resolve_short_id：精确/前缀唯一/歧义/无命中/空串）
2. workspace 安全校验分支（盘符根/系统目录/配置工作空间根/无效路径/降级跳过）
3. goal 与 acceptance_criteria 形态归一（JSON 串/纯文本/非 dict/部分无效指标剔除）
4. 继承参数校验与源解析（pipe 源查找、workspace 跨容器/已消失拒绝）
5. agent registry 注入往返、服务故障 fail-closed / 未命中 NOT_FOUND（agent 配置
   单源 = 注入钩子，无磁盘回退——解析权归 agent_manager）
6. state 桥降级语义（读取失败 None、依赖 fail-closed、父存在性兜底、项目归属单跳继承）
7. 派发编排隔离语义（出生后登记失败不影响提交、描述超长/L2 越权闸门）

注：本文件为 tests/ 根新增文件，**需登记插桩车道白名单**（scripts/coverage_exempt.py
的既有车道清单，由仓库维护者登记）；登记前不在插桩车道内。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
# 拍平布局（ADR 2026-10-13-plugin-flat-layout）：isolation 包与 project_registry
# 等平铺模块同住共享根，父目录（共享根）入 sys.path 即覆盖两者
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

_MOD_NAME = "task_submit_tool_branches_test_mod"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    """模块级 sys.path 注入（teardown 恢复）。

    tasks 目录含 workspace.py（模块）而 system 目录含 workspace/（包），两者对
    `import workspace` 解析冲突——用 fixture 管理并恢复，不污染同进程其它测试。
    """
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SHARED_DIR):
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


def make_tool(tool_module):
    """构造 TaskSubmitTool，monkeypatch 校验钩子（沿用 params 文件惯例）。

    chat sender 假实现按统一出生协议分段响应：create → 身份登记（no_dispatch）
    → 执行派发（background）→ 提交者管道登记（no_dispatch + task.owned.*）。
    captured["params"] 保留出生登记调用，captured["dispatch"] 保留执行派发调用，
    captured["calls"] 为全部调用。
    """
    captured: dict = {"calls": []}

    tool = tool_module.TaskSubmitTool()

    async def _fake_validate_target(target_id, level):
        return (True, "", "")

    tool._validate_target_agent = _fake_validate_target

    async def _allow_ownership(level, pid):
        return (True, None)

    tool._check_parent_ownership = _allow_ownership

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

    # 出生协议第三段（工作空间同步创建，ADR-2026-10-17）：准备器缺席即出生失败
    from tests import _task_birth_workspace as _birth_ws  # noqa: PLC0415

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


def base_inputs(**overrides):
    inputs = {
        "goal_title": "测试任务",
        "goal_description": "分支补测",
        "target_type": "agent",
        # P3 迁移后编码执行者驻 mode_coding 包（键形态经注入钩子解析）
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
    """set_agent_registry_lookup 注入后按 id 查询：命中得 dict，未命中得 (None, "")。"""
    tool_module.set_agent_registry_lookup(registry_hook({"orch": {"level": "L2"}}))
    tool = tool_module.TaskSubmitTool()
    cfg, unavailable = await tool._lookup_agent_config("orch")
    assert unavailable == ""
    assert cfg == {"level": "L2"}
    cfg, unavailable = await tool._lookup_agent_config("nobody")
    assert cfg is None
    assert unavailable == ""


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


# ── goal / acceptance_criteria 形态归一 ──────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # JSON 串形态解析为对象
        ('{"title": "t1", "description": "d1"}', {"title": "t1", "description": "d1"}),
        # 纯文本（非 JSON）→ 单字符串主形态：title=首行、description=全文
        ("整理仓库目录", {"title": "整理仓库目录", "description": "整理仓库目录"}),
    ],
)
def test_parse_goal_input_string_forms(tool_module, raw, expected):
    assert tool_module.TaskSubmitTool._parse_goal_input({"goal": raw}) == expected


# ── 平铺 description 别名归一（BUG-31：模型用 description 传描述被静默丢弃）──


def test_parse_goal_input_flat_description_alias(tool_module, caplog):
    """goal_description 缺席时平铺 description 作为显式别名映射进 goal.description。"""
    with caplog.at_level(logging.INFO):
        goal = tool_module.TaskSubmitTool._parse_goal_input(
            {"goal_title": "t", "description": "别名描述"}
        )
    assert goal == {"title": "t", "description": "别名描述"}
    assert any("description 别名" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "goal_description,expected_desc",
    [
        # 非空白 goal_description 优先于别名
        ("正式描述", "正式描述"),
        # 空白 goal_description 无语义内容，视同缺席由别名兜住
        ("  ", "别名描述"),
    ],
    ids=["explicit_wins", "blank_falls_to_alias"],
)
def test_parse_goal_input_goal_description_priority(tool_module, goal_description, expected_desc):
    """description 同传时 goal_description 的优先级行为。"""
    goal = tool_module.TaskSubmitTool._parse_goal_input(
        {
            "goal_title": "t",
            "goal_description": goal_description,
            "description": "别名描述",
        }
    )
    assert goal == {"title": "t", "description": expected_desc}


def test_parse_goal_input_no_description_either_form_keeps_empty(tool_module):
    """两种名字都无描述 → goal.description 为空串（由闸门拒绝，不在解析层编造）。"""
    goal = tool_module.TaskSubmitTool._parse_goal_input({"goal_title": "t"})
    assert goal == {"title": "t", "description": ""}


@pytest.mark.asyncio
async def test_flat_description_subtask_births_with_alias_content(tool_module):
    """execute 级行为回归：BUG-31 现场 payload 形态（平铺 description）→ 任务出生且描述承载内容。"""
    tool, captured = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=1)
    inputs.pop("goal_description")
    inputs["description"] = "写一篇 600 字左右的微型小说，主题「雨夜站台」"
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert (
        captured["params"]["state"]["task.description"]
        == "写一篇 600 字左右的微型小说，主题「雨夜站台」"
    )


@pytest.mark.asyncio
async def test_missing_description_error_lists_received_keys(tool_module):
    """两种名字都无描述 → 仍拒绝，报错带实际收到的参数键（不含注入键）便于模型自纠。"""
    tool, _ = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=1)
    inputs.pop("goal_description")
    result = await tool.execute(inputs)
    assert not result.success
    assert result.error_code == "MISSING_DESCRIPTION"
    assert "goal_description" in result.error
    assert "goal_title" in result.error
    # 注入键是系统行为不是模型入参，不出现在给模型的纠错信息里
    assert "user_id" not in result.error
    assert "parent_agent_level" not in result.error


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


# ── L1 parent_task_id 存在性（state 单通道）──────────────────


@pytest.mark.asyncio
async def test_l1_unknown_parent_task_id_rejected(tool_module):
    """L1 指定的父任务在 state 聚合中不存在（无读取桥）→ 拒绝挂载。"""
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, parent_task_id="ghost00000000")
    )
    assert not result.success
    assert result.error_code == "L2_CANNOT_SPECIFY_PARENT_TASK_ID"


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
async def test_l1_parent_exists_in_state_channel(tool_module, monkeypatch, rows, parent_id):
    """父任务存在性以 state 为唯一通道：行命中即放行（task = pipeline）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    assert await tool._validate_parent_task_id(1, parent_id) is True


@pytest.mark.asyncio
async def test_parent_exists_in_state_bridge_down_returns_false(tool_module, monkeypatch):
    """state 桥不可用（读取返回非 list）→ 兜底判不存在（False）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: None)
    tool = tool_module.TaskSubmitTool()
    assert await tool._parent_exists_in_state("ghost00000000") is False


# ── 父任务归属闸（state 层级键，fail-closed）──────────────────
# 行为矩阵主车道：tests/tools/test_task_submit_permission_p0.py（P0-3 权限面专文件）。


# ── 项目挂靠解析分支 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_binding_resolves_workspace_via_service(tool_module, monkeypatch, tmp_path):
    """挂靠解析经 project.list_paths 服务（T6a）：workspace 锚到登记项目文件夹。

    原共享层 sys.path 自举分支随服务化退场（跨插件读面收口项目域主，
    消费方不再直读共享根登记文件）。
    """
    folder = tmp_path / "proj_folder_service"
    folder.mkdir()
    paths = {"proj00000002": str(folder)}

    async def fake_project_service(method: str, args: dict):
        if method == "list_paths":
            return {"paths": dict(paths)}
        if method == "get":
            pid = str(args.get("project_id") or "")
            return (
                {"found": True, "project": {"id": pid, "workflow_state": "running", "mode_tags": []}}
                if pid in paths
                else {"found": False}
            )
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(tool_module, "_project_service", fake_project_service)
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)

    result = await tool.execute(
        base_inputs(parent_agent_level=1, project_id="proj00000002")
    )
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == str(folder)


@pytest.mark.asyncio
async def test_project_folder_missing_rejected(tool_module, monkeypatch, tmp_path):
    """登记在场但文件夹已删除 → PROJECT_FOLDER_MISSING fail-honest。"""
    async def fake_project_service(method: str, args: dict):
        if method == "list_paths":
            return {"paths": {"proj00000003": str(tmp_path / "deleted_folder")}}
        if method == "get":
            pid = str(args.get("project_id") or "")
            return (
                {"found": True, "project": {"id": pid, "workflow_state": "running", "mode_tags": []}}
                if pid == "proj00000003"
                else {"found": False}
            )
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(tool_module, "_project_service", fake_project_service)
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
#
# 2026-10-20 用户裁定《继承机制简化方案》：inherit_from 单参数 = 源任务 ID；
# inherit_mode 撤下；工具按 id 正向查源任务 state 行 → 整份复制其坐标。


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
def test_inherit_source_id_flat_and_nested(tool_module, inp, expected):
    """扁平 inherit_from 与旧式 inherit.from 同口径提取。"""
    assert tool_module._inherit_source_id(inp) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inherit_from,inherit_mode",
    [
        ("", "pipe"),  # 空 id = 未填继承 = 普通新任务
        ("src0000000001", 123),  # 撤下的 inherit_mode 被忽略（不报错）
        ("src0000000001", None),  # 同上
    ],
)
async def test_inherit_retired_mode_ignored_or_empty_is_plain_task(
    tool_module, monkeypatch, inherit_from, inherit_mode
):
    """inherit_from 为空 = 普通新任务；inherit_mode 已撤下 → 忽略不报错。

    2026-10-20 用户裁定：inherit_mode 撤下（pipe 是空壳，上下文重新开始即
    重新开始）。旧调用方仍在传该键时按忽略处理并留 WARN，不打断在跑路径。
    """
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, inherit_from=inherit_from, inherit_mode=inherit_mode)
    )
    if inherit_from:
        # 源任务查无 → 显式报错（不静默当普通任务）；撤下的 mode 不参与判定
        assert not result.success
        assert result.error_code == "INHERIT_SOURCE_NOT_FOUND", result.error
        return
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert "inherit" not in ec["workspace"]
    assert ec["workspace"]["source_path"] == ""


@pytest.mark.asyncio
async def test_inherit_source_found_in_state_copies_coordinate(tool_module, monkeypatch, tmp_proj):
    """inherit_from 命中源任务 → 整份复制其坐标（不新建 worktree）。"""
    rows = [
        {
            "pipeline_id": "srcpipe000001",
            "task.id": "srcpipe000001",
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": json.dumps(
                {"path": str(tmp_proj), "mode": "plain", "project_root": str(tmp_proj)}
            ),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(task_id="parent_p", inherit_from="srcpipe000001")
    )
    assert result.success, result.error
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == str(tmp_proj)
    assert ec["workspace"]["inherit"]["source_task_id"] == "srcpipe000001"


@pytest.mark.asyncio
async def test_inherit_source_missing_rejected_not_degraded(tool_module, monkeypatch):
    """inherit_from 查无源任务 → 显式报错（不静默当普通任务提交）。

    旧实现对 pipe 继承源缺失只降级为「对话历史为空」——静默降级会让人以为
    继承了其实没继承。
    """
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "unrelated000001"}]
    )
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(task_id="parent_p", inherit_from="srcpipe000001")
    )
    assert not result.success
    assert result.error_code == "INHERIT_SOURCE_NOT_FOUND", result.error


@pytest.mark.asyncio
async def test_inherit_foreign_project_rejected(tool_module, monkeypatch, tmp_path_factory):
    """源任务坐标属于另一个项目基座 → 拒绝继承，防产物落错项目。

    归属判据取坐标里的 project_root（坐标对坐标），不取插件代码位置——后者
    在装机用户空间按文件层级数推导，会把仓外项目一律误判跨容器。
    """
    base = tmp_path_factory.mktemp("own_project")
    outside = tmp_path_factory.mktemp("outside_container")
    rows = [
        {
            "pipeline_id": "xcontainer123",
            "task.id": "xcontainer123",
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": json.dumps(
                {"path": str(outside), "mode": "plain", "project_root": str(outside)}
            ),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = await tool._resolve_inherit_coordinate(
        "xcontainer123", {"parent_ws_meta": {"path": str(base), "project_root": str(base)}}
    )
    assert meta is None
    assert fail is not None
    assert "另一个项目基座" in fail.error


@pytest.mark.asyncio
async def test_inherit_workspace_vanished_rejected(tool_module, monkeypatch, tmp_proj):
    """源任务工作空间目录已消失 → 拒绝继承并引导改派（不静默回落）。"""
    vanished = Path(tmp_proj) / "gone_dir"
    rows = [
        {
            "pipeline_id": "vanished12345",
            "task.id": "vanished12345",
            "run_status": "completed",
            "task.status": "completed",
            "task.ws_meta": json.dumps({"path": str(vanished), "mode": "plain"}),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool = tool_module.TaskSubmitTool()
    meta, fail = await tool._resolve_inherit_coordinate("vanished12345")
    assert meta is None
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
async def test_empty_target_type_internalized_dispatches(tool_module):
    """空 target_type → 常量内化缺省归一 agent（ec9ac5fe7，ADR 2026-10-19
    task-submit-agent-determines-orchestration）：显式空串走 falsy 归一路径，
    agent.id 计入完备性左值，派发放行——与编排侧回归锚（缺省不入参）互补。"""
    tool, captured = make_tool(tool_module)
    inputs = base_inputs(parent_agent_level=1)
    inputs["target_type"] = ""
    result = await tool.execute(inputs)
    assert result.success, result.error
    assert captured["dispatch"]["pipeline_id"] == "a1b2c3d4e5f6"


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


# ── agent registry 故障 fail-closed 与未命中（agent 配置单源 = 注入钩子）──


@pytest.mark.asyncio
async def test_registry_service_crash_fails_closed_unavailable(tool_module):
    """registry 服务抛异常（agent_manager 不可用）→ fail-closed 显式不可用，
    不冒充"Agent 不存在"（层级闸门无配置可判，拒绝派发并如实归因）。"""
    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)

    async def boom(target_id):
        raise RuntimeError("agent_manager 服务不可用")

    tool_module.set_agent_registry_lookup(boom)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, target_id="no_such_agent_zzz")
    )
    assert not result.success
    assert result.error_code == "AGENT_CONFIG_UNAVAILABLE"
    assert "agent_manager" in result.error


@pytest.mark.asyncio
async def test_registry_service_miss_reports_not_found(tool_module):
    """registry 正常应答未命中（None）→ TARGET_AGENT_NOT_FOUND。"""

    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)

    async def miss(target_id):
        return None

    # 覆盖缺省钩子须在 make_tool 之后（缺省钩子对所有 id 应答命中）
    tool_module.set_agent_registry_lookup(miss)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, target_id="no_such_agent_zzz")
    )
    assert not result.success
    assert result.error_code == "TARGET_AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_registry_hook_missing_fails_closed_unavailable(tool_module):
    """钩子未注入（接线缺失）→ AGENT_CONFIG_UNAVAILABLE，绝不静默放行。"""
    tool, _ = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module._agent_registry_lookup = None
    result = await tool.execute(
        base_inputs(parent_agent_level=1, target_id="any_agent_here")
    )
    assert not result.success
    assert result.error_code == "AGENT_CONFIG_UNAVAILABLE"
    assert "未注入" in result.error


def _mode_agent_registry_hook():
    """模式键目标的最小注册表替身（身份性字段齐备，供层级闸与完备性左值）。"""
    return registry_hook(
        {
            "mode_coding/code_writer": {
                "level": "L3",
                "is_active": True,
                "tool_ids": ["file_write"],
                "system_prompt": "persona-stub",
            },
            "mode_coding/programming_orchestrator_agent_v2": {
                "level": "L2",
                "is_active": True,
                "tool_ids": ["file_write"],
                "system_prompt": "persona-stub",
            },
            "mode_godot/godot_expert": {
                "level": "L3",
                "is_active": True,
                "tool_ids": ["file_write"],
                "system_prompt": "persona-stub",
            },
            "mode_godot/godot_orchestrator_agent": {
                "level": "L2",
                "is_active": True,
                "tool_ids": ["file_write"],
                "system_prompt": "persona-stub",
            },
        }
    )


@pytest.mark.asyncio
async def test_mode_agent_key_dispatch_via_registry_hook(tool_module):
    """模式命名空间键全链派发：mode_coding/code_writer（L3 执行者）
    经注入钩子解析配置，L3 目标对 L2 提交者放行并按新键派发。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module.set_agent_registry_lookup(_mode_agent_registry_hook())
    result = await tool.execute(
        base_inputs(parent_agent_level=2, target_id="mode_coding/code_writer", task_id="ctx-task-1")
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_coding/code_writer"


@pytest.mark.asyncio
async def test_mode_orchestrator_dispatch_from_main_via_registry_hook(tool_module):
    """模式命名空间键全链派发：mode_coding/programming_orchestrator_agent_v2
    （L2 编排者）作为 main（L1）直接下级派发成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module.set_agent_registry_lookup(_mode_agent_registry_hook())
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
async def test_mode_godot_expert_dispatch_via_registry_hook(tool_module):
    """模式命名空间键全链派发（mode_godot 包，P3-⑤ 立项）：mode_godot/godot_expert
    （L3 执行者）→ L2 提交者派单成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module.set_agent_registry_lookup(_mode_agent_registry_hook())
    result = await tool.execute(
        base_inputs(parent_agent_level=2, target_id="mode_godot/godot_expert", task_id="ctx-task-1")
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_godot/godot_expert"


@pytest.mark.asyncio
async def test_mode_godot_orchestrator_dispatch_from_main_via_registry_hook(tool_module):
    """模式命名空间键全链派发（mode_godot 包）：mode_godot/godot_orchestrator_agent
    （L2 执行型编排者）作为 main（L1）直接下级派发成功。"""
    tool, captured = make_tool(tool_module)
    restore_real_target_validation(tool)
    tool_module.set_agent_registry_lookup(_mode_agent_registry_hook())
    result = await tool.execute(
        base_inputs(
            parent_agent_level=1,
            target_id="mode_godot/godot_orchestrator_agent",
        )
    )
    assert result.success, result.error
    assert captured["dispatch"]["agent_id"] == "mode_godot/godot_orchestrator_agent"


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


# ── 派发 execution_context 模式键透传（BUG-61：模式面板按 state.mode 过滤行源）──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode_value", "target_id"),
    [("coding", "mode_coding/code_writer"), ("godot", "mode_godot/godot_expert")],
)
async def test_dispatch_execution_context_carries_mode(
    tool_module, monkeypatch, mode_value, target_id
):
    """mode 输入随派发透传进 execution_context（与聊天主链消息级 EC 同形）。

    内核 1a2 把消息级 execution_context 并入出生管道 initial state 后，
    mode_material_inject 据 execution_context.mode 回写 state.mode；模式面板
    （mode_coding 等）的会话/看板行源按 state.mode 过滤——派发链丢键即
    全面板零呈现（BUG-61 根因）。用例取同包配对（批 B8 闸②：mode ≠ 目标
    归属包在门口即拒，透传语义只对合法组合成立）。
    """
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    # 面板派发形态 = L1 根任务（mode_* _issue_args 同口径）
    result = await tool.execute(
        base_inputs(parent_agent_level=1, mode=mode_value, target_id=target_id)
    )
    assert result.success, result.error
    assert captured["dispatch"]["execution_context"]["mode"] == mode_value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_value", [None, "", "   "])
async def test_dispatch_execution_context_without_mode_has_no_key(
    tool_module, monkeypatch, mode_value
):
    """未声明 mode（缺键/空串/空白）→ execution_context 不带 mode 键（零注入语义）。"""
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    overrides = {} if mode_value is None else {"mode": mode_value}
    result = await tool.execute(base_inputs(parent_agent_level=1, **overrides))
    assert result.success, result.error
    assert "mode" not in captured["dispatch"]["execution_context"]


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
    """根任务 inherit_from：源任务坐标整份复制（不复制不初始化），继承不算显式。"""
    rows = [
        {
            "pipeline_id": "wsrc000000001",
            "task.id": "wsrc000000001",
            "run_status": "failed",
            "task.status": "failed",
            "task.ws_meta": json.dumps(
                {"path": str(tmp_proj), "mode": "plain", "project_root": str(tmp_proj)}
            ),
        }
    ]
    monkeypatch.setattr(tool_module, "_state_reader", lambda: rows)
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, inherit_from="wsrc000000001")
    )
    assert result.success, result.error
    # 继承坐标写入 execution_context；无显式声明 → explicit=False
    ec = captured["dispatch"]["execution_context"]
    assert ec["workspace"]["source_path"] == str(tmp_proj)
    assert ec["workspace"]["explicit"] is False
    # 出生 state 携整份复制的坐标 + 执行锚点（工作空间已就位 → 无创建动作）
    birth_state = captured["params"]["state"]
    assert birth_state["task.ws_meta"]["path"] == str(tmp_proj)
    assert birth_state["workspace"] == str(tmp_proj)
    assert birth_state["task.inherit_from"] == "wsrc000000001"
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


# ── 提交者层级出生写全（归属闸真值）──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submitter_level,extra",
    [
        # L1 根任务（面板/LLM 根提交，无父）
        (1, {}),
        # L2 子任务（父任务 id 经注入回退 = task_id）
        (2, {"pipeline_id": "owner-pipe-1", "task_id": "parent-task-1"}),
    ],
)
async def test_submitted_by_level_written_to_birth_state(tool_module, submitter_level, extra):
    """task.submitted_by_level 随出生 state 一次写全（= 提交者 Agent 层级）。

    归属闸 _check_parent_ownership 据该键判定父任务层级链（子任务挂载
    权限面唯一真值，无 YAML 通道）。
    """
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=submitter_level, **extra)
    )
    assert result.success, result.error
    state = captured["params"]["state"]
    assert state["task.submitted_by_level"] == submitter_level


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
    """state 行在场但无命中（pipeline_id / task.owned.* 均不匹配）→ 判不存在。"""
    monkeypatch.setattr(
        tool_module, "_state_reader", lambda: [{"pipeline_id": "other000000001"}]
    )
    tool = tool_module.TaskSubmitTool()
    assert await tool._validate_parent_task_id(1, "ghost00000000") is False


# ── 4.4 plan 态派发白名单闸（ADR 2026-09-17 决策 2）──────────────


class _FakeProject:
    def __init__(self, workflow_state: str = "plan", mode_tags: list[str] | None = None):
        self.id = "proj00000009"
        self.workflow_state = workflow_state
        self.path = "D:/x/p"
        self.mode_tags = list(mode_tags or [])
        self.default_mode = ""


@pytest.mark.asyncio
async def test_plan_state_gate_rejects_non_whitelisted_executor(
    tool_module, monkeypatch, tmp_path
):
    """plan 态 + 白名单外执行者 → PROJECT_PLAN_LOCKED（状态闸机械化拦截）。"""
    folder = tmp_path / "plan_gate_proj"
    folder.mkdir()

    async def fake_project_service(method: str, args: dict):
        if method == "list_paths":
            return {"paths": {"proj00000009": str(folder)}}
        if method == "get":
            pid = str(args.get("project_id") or "")
            return (
                {"found": True, "project": {"id": pid, "workflow_state": "plan", "mode_tags": []}}
                if pid == "proj00000009"
                else {"found": False}
            )
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(tool_module, "_project_service", fake_project_service)
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, _ = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, project_id="proj00000009")
    )
    assert not result.success
    assert result.error_code == "PROJECT_PLAN_LOCKED"
    assert "plan→running" in result.error


def _stub_project_service(
    monkeypatch: pytest.MonkeyPatch,
    tool_module: Any,
    paths: dict[str, str],
    states: dict[str, str],
) -> None:
    """project.* 服务桩（T6a）：list_paths 按登记映射、get 按 states 回工作流态。"""

    async def fake_project_service(method: str, args: dict):
        if method == "list_paths":
            return {"paths": dict(paths)}
        if method == "get":
            pid = str(args.get("project_id") or "")
            known = pid in states
            return (
                {"found": True, "project": {"id": pid, "workflow_state": states[pid], "mode_tags": []}}
                if known
                else {"found": False}
            )
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(tool_module, "_project_service", fake_project_service)


@pytest.mark.asyncio
async def test_plan_state_gate_allows_research_dispatch(tool_module, monkeypatch, tmp_path):
    """plan 态 + 白名单执行者（research_agent）→ 闸放行，链路继续走完。"""
    folder = tmp_path / "plan_gate_research"
    folder.mkdir()
    _stub_project_service(
        monkeypatch, tool_module, {"proj00000009": str(folder)}, {"proj00000009": "plan"}
    )
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(
            parent_agent_level=1,
            project_id="proj00000009",
            target_id="research_agent",
        )
    )
    assert result.success, result.error
    assert "dispatch" in captured


@pytest.mark.asyncio
async def test_plan_state_gate_inactive_on_running_project(tool_module, monkeypatch, tmp_path):
    """running 态项目 → 闸不生效（执行类派发照常）。"""
    folder = tmp_path / "plan_gate_running"
    folder.mkdir()
    _stub_project_service(
        monkeypatch, tool_module, {"proj00000009": str(folder)}, {"proj00000009": "running"}
    )
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, project_id="proj00000009")
    )
    assert result.success, result.error
    assert "dispatch" in captured


@pytest.mark.asyncio
async def test_plan_state_gate_skips_without_project(tool_module):
    """不挂项目 → 闸直接放行（返回 None）。"""
    gate = tool_module.TaskSubmitTool._gate_project_plan_state
    assert await gate("", "agent", "mode_coding/code_writer") is None


@pytest.mark.asyncio
async def test_plan_state_gate_service_unavailable_fail_closed(
    tool_module, monkeypatch, tmp_path
):
    """project 服务缺席（裸宿主装载未注入）→ 闸 fail-closed 拒绝（T6a，
    原 shared_root 守卫自举分支随服务化退场——读面收口后不可直读兜底）。"""
    monkeypatch.setattr(tool_module, "_project_service", None)
    gate = tool_module.TaskSubmitTool._gate_project_plan_state
    # 白名单外执行者 → 拦截（服务不可用信封，不静默放行）
    result = await gate("proj00000010", "agent", "mode_coding/code_writer")
    assert result is not None and not result.success
    assert result.error_code == "PROJECT_SERVICE_UNAVAILABLE"


def _seed_user_mode_package(
    monkeypatch: pytest.MonkeyPatch, user_root: Path, dirname: str, mode_yaml: str
) -> None:
    """播种用户根模式包（plugin.json 包标记 + mode.yaml 声明）。

    collect_planning_executors 经 AGENTOS_USER_ROOT 双根枚举真实读取——
    用真实包声明驱动闸聚合，不 mock mode_keys（文件面即依赖真相）。
    """
    pkg = user_root / "plugins" / "modes" / dirname
    pkg.mkdir(parents=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    (pkg / "mode.yaml").write_text(mode_yaml, encoding="utf-8")
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))


def _plan_state_project(monkeypatch: pytest.MonkeyPatch, folder: Path, tool_module: Any) -> None:
    """登记 plan 态项目（T6a 服务桩形态：沿用本文件 plan 闸既有假登记模式）。"""
    folder.mkdir()
    _stub_project_service(
        monkeypatch, tool_module, {"proj00000011": str(folder)}, {"proj00000011": "plan"}
    )


# ── 4.4.1 plan 态白名单注册化聚合三态（批 B4：基线 ∪ Σ 注册贡献）──────────
# 三态同键对比：执行者键都用默认 target（mode_coding/code_writer，真实磁盘
# 可解析），差异只在注册声明面——拒绝/放行的唯一变量 = 聚合贡献。


@pytest.mark.asyncio
async def test_plan_state_gate_baseline_only_mode_key_rejected(
    tool_module, monkeypatch, tmp_path
):
    """仅基线（出厂六包零声明 = 注册零贡献）→ 模式执行者仍拒（fail-closed）。"""
    _plan_state_project(monkeypatch, tmp_path / "plan_gate_b4_baseline", tool_module)
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, _captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, project_id="proj00000011")
    )
    assert not result.success
    assert result.error_code == "PROJECT_PLAN_LOCKED"
    assert "environment_setup_agent" in result.error  # 错误信息列放行集


@pytest.mark.asyncio
async def test_plan_state_gate_registered_contribution_allowed(
    tool_module, monkeypatch, tmp_path
):
    """基线+贡献：注册包声明 mode_coding/code_writer → 同键 plan 态放行
    （真实用户根包声明，mode_keys 磁盘聚合）。"""
    _seed_user_mode_package(
        monkeypatch,
        tmp_path / "user_root_contrib",
        "mode_mixed",
        "mode: mixed\nname: M\nplanning_executors: [mode_coding/code_writer]\n",
    )
    _plan_state_project(monkeypatch, tmp_path / "plan_gate_b4_contrib", tool_module)
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(parent_agent_level=1, project_id="proj00000011")
    )
    assert result.success, result.error
    assert "dispatch" in captured


@pytest.mark.asyncio
async def test_plan_state_gate_invalid_declaration_entries_contribute_nothing(
    tool_module, monkeypatch, tmp_path
):
    """非法形态条目 = 零贡献：同声明内合法条目过闸、非法条目不豁免
    （fail-closed；声明面全量拒收由 agent_manager/parity 同源闸执法）。"""
    _seed_user_mode_package(
        monkeypatch,
        tmp_path / "user_root_invalid",
        "mode_mixed",
        "mode: mixed\nname: M\n"
        "planning_executors: ['mode_mixed/planner', '../evil', 'Mode_X/agent']\n",
    )
    _plan_state_project(monkeypatch, tmp_path / "plan_gate_b4_invalid", tool_module)
    gate = tool_module.TaskSubmitTool._gate_project_plan_state
    # 合法形态条目过闸（返回 None = 放行）
    assert await gate("proj00000011", "agent", "mode_mixed/planner") is None
    # 非法形态条目不在放行集（穿越键 / 大写模式名键）；基线外未声明键同样不豁免
    for target_id in ("../evil", "Mode_X/agent", "mode_coding/code_writer"):
        result = await gate("proj00000011", "agent", target_id)
        assert result is not None and not result.success, target_id
        assert result.error_code == "PROJECT_PLAN_LOCKED", target_id


# ── 4.4.1 mode 登记两闸（批 B8：显式 mode 集合/归属轻校验，四态）──────────


def _mode_gate_project(
    monkeypatch: pytest.MonkeyPatch, tool_module: Any, folder: Path, mode_tags: list[str]
) -> None:
    """登记带 mode_tags 的项目（T6a 服务桩形态：沿用本文件 mode 闸既有假登记模式）。"""
    folder.mkdir()
    _stub_project_service(
        monkeypatch,
        tool_module,
        {"proj00000012": str(folder)},
        {"proj00000012": "running"},
    )

    base = tool_module._project_service

    async def _mode_tags_override(method: str, args: dict):
        pid = str(args.get("project_id") or "")
        if method == "get" and pid == "proj00000012":
            return {"found": True, "project": {"id": pid, "workflow_state": "running", "mode_tags": mode_tags}}
        return await base(method, args)

    monkeypatch.setattr(tool_module, "_project_service", _mode_tags_override)


@pytest.mark.asyncio
async def test_mode_gate_outside_project_tags_rejected(tool_module, monkeypatch, tmp_path):
    """闸① 集合外拒：挂靠项目登记 tags 且显式 mode ∉ 集合 → 拒，错误信息
    指明登记集合与调整通道（可指导改正）。"""
    _mode_gate_project(monkeypatch, tool_module, tmp_path / "mode_gate_tags", ["coding", "research"])
    gate = tool_module.TaskSubmitTool._gate_mode_registration
    result = await gate("proj00000012", "game", "mode_coding/code_writer")
    assert result is not None and not result.success
    assert result.error_code == "MODE_NOT_IN_PROJECT_TAGS"
    assert "coding" in result.error and "research" in result.error
    assert "project_create" in result.error  # 调整通道在错误信息中可达


@pytest.mark.asyncio
async def test_mode_gate_unregistered_project_tags_skipped(tool_module, monkeypatch, tmp_path):
    """闸① legacy 宽容：挂靠项目未登记 tags → 集合闸跳过（mode 任意值放行）。"""
    _mode_gate_project(monkeypatch, tool_module, tmp_path / "mode_gate_notags", [])
    gate = tool_module.TaskSubmitTool._gate_mode_registration
    assert await gate("proj00000012", "game", "research_agent") is None


@pytest.mark.asyncio
async def test_mode_gate_target_pack_mismatch_rejected(tool_module):
    """闸② 归属不一致拒：target 为 mode_X/<stem> 而显式 mode ≠ X → 拒
    （不依赖项目挂靠；裸名执行者无归属包，闸②不适用）。"""
    gate = tool_module.TaskSubmitTool._gate_mode_registration
    result = await gate("", "game", "mode_coding/code_writer")
    assert result is not None and not result.success
    assert result.error_code == "MODE_TARGET_PACK_MISMATCH"
    assert "mode_coding" in result.error
    # 裸名系统执行者（无归属包）+ 任意 mode → 闸②不适用
    assert await gate("", "game", "research_agent") is None


@pytest.mark.asyncio
async def test_mode_gate_no_mode_is_noop(tool_module):
    """不填 mode = 现状不变（零隐式填充零新逻辑）：闸直接放行。"""
    gate = tool_module.TaskSubmitTool._gate_mode_registration
    assert await gate("proj00000012", "", "mode_coding/code_writer") is None
    assert await gate("", "", "mode_coding/code_writer") is None


@pytest.mark.asyncio
async def test_mode_gate_registered_mode_passes_and_dispatches(
    tool_module, monkeypatch, tmp_path
):
    """登记内 mode + 归属一致 → 两闸全过，链路继续走完（显式 mode ∈ 集合）。"""
    _mode_gate_project(monkeypatch, tool_module, tmp_path / "mode_gate_pass", ["coding"])
    monkeypatch.setattr(tool_module, "_state_reader", lambda: [])
    tool, captured = make_tool(tool_module)
    result = await tool.execute(
        base_inputs(
            parent_agent_level=1,
            project_id="proj00000012",
            mode="coding",
        )
    )
    assert result.success, result.error
    assert "dispatch" in captured
    assert captured["dispatch"]["execution_context"]["mode"] == "coding"
