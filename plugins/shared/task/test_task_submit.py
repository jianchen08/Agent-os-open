# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""
task_submit 工具的单元测试

覆盖 inherit 参数面场景（2026-10-20 用户裁定《继承机制简化方案》）：
1. Schema：inherit_from 单参数（源任务 ID）；inherit_mode 已撤下
2. 解析层：未填 inherit_from = 普通新任务；填了但查无源任务 = 显式报错
   （不静默当普通任务）；撤下的 inherit_mode 被忽略（旧调用方兼容）

说明：
- Schema 层测试直接调用 get_tool_definition() 检查定义
- 解析层测试通过 mock 跑 execute()，让代码走到 inherit 解析后立即
  在派发段失败（mock 抛异常），从 error_code 反推解析层行为
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── task_submit 是独立插件目录，动态 import 其 tool.py ──
# tool.py 加载为唯一模块名，避免与其它插件目录的 tool.py 冲突。
_TASK_SUBMIT_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_TASKS_DIR = Path(__file__).resolve().parent.parent.parent / "system" / "tasks"
for _d in (str(_TASK_SUBMIT_DIR), str(_TASKS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "task_submit_tool_test", _TASK_SUBMIT_DIR / "tool.py"
)
assert _spec is not None, "Cannot load task_submit/tool.py"
assert _spec.loader is not None, "Cannot load task_submit/tool.py"
_tool_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_submit_tool_test"] = _tool_mod
_spec.loader.exec_module(_tool_mod)

TaskSubmitTool = _tool_mod.TaskSubmitTool
_normalize_description = _tool_mod._normalize_description  # noqa: SLF001
from agentos_plugin_sdk.tool_types import Tool  # noqa: E402

pytestmark = pytest.mark.unit


# ─────────────────────────── 工具函数 ───────────────────────────


def _build_inputs(inherit_from: str = "", user_id: str = "") -> dict:
    """构造一组用于 task_submit.execute() 的最小输入。

    goal 字段已按工具契约平铺为 goal_title/goal_description（均必填）。
    继承参数面（2026-10-20 用户裁定《继承机制简化方案》）：inherit_from 单
    参数 = 源任务 ID；inherit_mode 已撤下。
    """
    inputs = {
        "goal_title": "继承任务测试",
        "goal_description": "继承任务测试描述",
        "target_type": "agent",
        "target_id": "general_agent",
        "task_scope": "non_container",
        "acceptance_criteria": {"file_check": {"input_params": {"path": "src/foo.py"}}},
        "parent_agent_level": 1,
        # 提交者会话坐标（生产由 param_inject 注入；完备性闸门按 state 键计必需）
        "session_id": "sess-1",
        **({"user_id": user_id} if user_id else {}),
    }
    if inherit_from:
        inputs["inherit_from"] = inherit_from
    return inputs


async def _fake_agent_base_config(self, target_id: str):
    """完备性左值桩：agent 基座身份字段齐备（契约 (config, fail) 二元组）。"""
    return (
        {
            "level": "L3",
            "is_active": True,
            "tool_ids": ["file_write"],
            "system_prompt": "persona-stub",
        },
        None,
    )


def _patch_infrastructure():
    """构造 mock 后的 patch 上下文管理器列表，屏蔽基础设施副作用。

    GAP-1 后提交经统一出生协议派发（本文件用例未注入 chat sender → 派发段
    返回 warning → DISPATCH_FAILED 失败信封，可作「解析层已通过」的哨兵）；
    任务域读写全走 state，无 YAML 服务通道可 mock。
    """
    # mock task_worker
    mock_task_worker = MagicMock()
    mock_task_worker.submit_task.return_value = True

    # mock service provider
    mock_service_provider = MagicMock()
    mock_service_provider.get.return_value = mock_task_worker

    patches = [
        patch.object(
            TaskSubmitTool,
            "_validate_target_agent",
            return_value=(True, "", ""),
        ),
        patch.object(
            TaskSubmitTool,
            "_check_dependencies_exist",
            return_value=[],
        ),
        # 完备性左值的 agent 基座（身份性字段源）：配置单源 = 注入钩子，
        # 本文件不接真钩子——桩成身份字段齐备的最小配置
        patch.object(
            TaskSubmitTool,
            "_get_agent_base_config",
            new=_fake_agent_base_config,
        ),
        # patch 模块级服务提供者获取点 _get_service_provider
        patch.object(_tool_mod, "_get_service_provider", return_value=mock_service_provider),
        # 屏蔽 ws 广播副作用
        patch.dict(
            sys.modules,
            {
                "ws_handler": MagicMock(ws_interaction_notifier=MagicMock(send_to_user=MagicMock())),
            },
        ),
    ]
    return patches, mock_service_provider


# ─────────────────────────── Schema 验证 ───────────────────────────


def test_inherit_from_schema_only():
    """Schema 层：inherit_from 单参数（源任务 ID），inherit_mode 已撤下。

    2026-10-20 用户裁定《继承机制简化方案》：pipe 是空壳、上下文重新开始即
    重新开始 → inherit_mode 从参数面撤下；inherit_from 只承载源任务 ID，
    填写即接续该任务的工作空间（整份复制其 worktree/分支/基座坐标）。
    """
    tool_def: Tool = TaskSubmitTool.get_tool_definition()
    props = tool_def.input_schema["properties"]

    assert "inherit_from" in props, f"缺少平铺的 inherit_from 字段，实际={list(props)}"
    assert "inherit_mode" not in props, f"inherit_mode 已撤下，实际={list(props)}"
    assert props["inherit_from"].get("type") == "string"
    desc = props["inherit_from"]["description"]
    assert "源任务 ID" in desc
    assert "接续" in desc


def test_workspace_description_points_to_inherit_from():
    """workspace 参数说明补一句：接续走 inherit_from（语义两义澄清）。"""
    props = TaskSubmitTool.get_tool_definition().input_schema["properties"]
    assert "inherit_from" in props["workspace"]["description"]


# ─────────────────────────── 解析层验证 ───────────────────────────


def _run_execute_capture_error_code(inputs: dict) -> str:
    """运行 execute 并返回错误码。"""
    tool = TaskSubmitTool()
    patches, _ = _patch_infrastructure()

    for p in patches:
        p.start()
    try:
        result = asyncio.run(tool.execute(inputs))
    finally:
        for p in patches:
            p.stop()
    return result.error_code or ""


def test_inherit_without_source_is_plain_task():
    """未填 inherit_from → 普通新任务（不走继承解析，不报错）。"""
    error_code = _run_execute_capture_error_code(_build_inputs())

    assert not error_code.startswith("INHERIT_"), f"未填继承不该走继承闸，实际={error_code}"


def test_inherit_source_missing_rejected():
    """inherit_from 查无源任务 → 显式报错（不静默当普通任务提交）。

    旧实现对继承源缺失只降级为「对话历史为空」——静默降级会让人以为继承了
    其实没继承。
    """
    with patch.object(_tool_mod, "_state_reader", lambda: []):
        error_code = _run_execute_capture_error_code(_build_inputs(inherit_from="src-task-001"))

    assert error_code == "INHERIT_SOURCE_NOT_FOUND", f"实际错误码={error_code}"


def test_inherit_retired_mode_ignored():
    """inherit_mode 已撤下：旧调用方仍传该键 → 忽略（不报错、不改变继承语义）。

    撤下的参数按未知参数忽略处理，避免打断在跑的面板续写路径；新语义
    （inherit_from 单参数 = 接续工作空间）已覆盖其唯一有效意图。
    """
    inputs = _build_inputs(inherit_from="src-task-001")
    inputs["inherit_mode"] = ["pipe", "workspace"]
    with patch.object(_tool_mod, "_state_reader", lambda: []):
        error_code = _run_execute_capture_error_code(inputs)

    assert error_code != "INVALID_INHERIT_MODE", f"撤下参数不应报模式非法，实际={error_code}"
    assert error_code != "INVALID_INHERIT_PARAMS", f"撤下参数不应报参数非法，实际={error_code}"

# ─────────────────────────── description 归一化验证 ───────────────────────────


def test_normalize_description_list_to_string():
    """LLM 返回 list 形式的 description 应被归一化为 str。

    回归场景：LLM 偶尔把多行文本写成数组（如 ['在当前执行环境...', '']），
    若不归一化会静默持久化进 YAML，最终在 API 层 TaskResponse.description
    （pydantic 强制 str）校验失败导致 500。
    """
    result = _normalize_description(["在当前执行环境", ""])
    assert isinstance(result, str)
    assert result == "在当前执行环境\n", f"list 应按换行连接，实际={result!r}"


def test_normalize_description_string_passthrough():
    """正常 str 输入应原样返回。"""
    assert _normalize_description("正常描述") == "正常描述"


def test_normalize_description_empty_string():
    """空串原样返回。"""
    assert _normalize_description("") == ""


def test_normalize_description_none_to_empty():
    """None 应归一化为空串。"""
    assert _normalize_description(None) == ""


def test_normalize_description_list_with_multiple_items():
    """多元素 list 用换行连接。"""
    result = _normalize_description(["第一行", "第二行", "第三行"])
    assert result == "第一行\n第二行\n第三行"


def test_normalize_description_tuple():
    """tuple 与 list 同等处理。"""
    result = _normalize_description(("第一行", "第二行"))
    assert result == "第一行\n第二行"


def test_normalize_description_non_string_scalar():
    """非 str 标量（int/dict）转为字符串，避免 len() 校验失效。"""
    assert _normalize_description(42) == "42"
    assert _normalize_description(True) == "True"


# ─────────────────── 0.2 state 传递：提交者身份写入 ───────────────────


def test_submit_writes_submitted_by_user_id_into_initial_state():
    """创建子任务时把提交者写进初始 state 的 task.submitted_by。

    0.2 任务信息走 state 不落文件：内核 task_completed/task_failed 事件
    从 final_state 的 task.submitted_by 带出 user_id，触发器注入器据此调
    chat.send_message 才有身份——曾硬编码空串被内核 -32603 拒绝（2026-08-17
    子任务结果回传断点）。
    """
    calls: list[dict] = []

    async def fake_sender(params: dict) -> dict:
        calls.append(dict(params))
        return {"pipeline_id": "child_pipe_001"}

    tool = TaskSubmitTool()
    patches, _ = _patch_infrastructure()
    for p in patches:
        p.start()

    async def _fake_preparer(*, pipeline_id, state, execution_context):
        # 契约：返回 state 更新须含非空 workspace 坐标
        return {
            "workspace": str(pipeline_id) + "-ws",
            "task.ws_meta": {"mode": "shared", "path": str(pipeline_id) + "-ws"},
        }

    _tool_mod.set_workspace_preparer(_fake_preparer)
    try:
        with patch.object(_tool_mod, "_get_chat_sender", return_value=fake_sender):
            result = asyncio.run(
                tool.execute(_build_inputs(user_id="u_admin"))
            )
    finally:
        _tool_mod._workspace_preparer = None
        for p in patches:
            p.stop()

    assert result.error_code in (None, ""), f"应派发成功，实际 error_code={result.error_code}"
    # 2026-08-22 短化定案（8db4c6b16）：LLM 工具面回传 12 位短 id
    assert result.output.get("pipeline_id") == "child_pipe_001"[:12]
    assert calls, "出生协议应有 chat.send_message 调用"
    # 出生协议三阶段（登记/身份/派发）：各阶段 user_id 都是提交者
    assert all(c.get("user_id") == "u_admin" for c in calls), (
        "chat.send_message 各阶段 user_id 应为提交者"
    )
    birth_call = next(c for c in calls if c.get("create") and c.get("state"))
    assert (
        birth_call["state"].get("task.submitted_by") == "u_admin"
    ), "初始 state 必须写 task.submitted_by（内核事件带出 user_id 的依据）"


def test_submit_without_user_id_fails_loud_no_synthetic_identity():
    """1017 事故同刀（ADR 2026-10-17-reverse-call-user-identity-injection）：

    无 user_id 形参 → 出生硬校验**响亮失败**（TaskBirthError「出生缺
    user_id」），禁止回落合成 "task_system" 静默派发。生产路径 tool_args_inject
    恒注入调用者真实 user_id，本守卫只在身份缺失时把失败送到提交方面前。
    """
    calls: list[dict] = []

    async def fake_sender(params: dict) -> dict:
        calls.append(dict(params))
        return {"pipeline_id": "child_pipe_002"}

    tool = TaskSubmitTool()
    patches, _ = _patch_infrastructure()
    for p in patches:
        p.start()
    try:
        with patch.object(_tool_mod, "_get_chat_sender", return_value=fake_sender):
            result = asyncio.run(tool.execute(_build_inputs()))
    finally:
        for p in patches:
            p.stop()

    assert not result.success, "缺真实身份必须响亮失败（禁止合成用户静默派发）"
    assert "出生缺 user_id" in (result.error or ""), f"实际 error={result.error}"
    assert all(
        "task_system" not in str(c.get("user_id", "")) for c in calls
    ), "任何出站调用不得携带合成用户"


# ─────────────── 层级闸门零兜底（兜底反模式审查 P1/P19，2026-08-20） ───────────────


class TestValidateTargetAgentLevelGate:
    """P1：level 缺失/非法必须显式拒绝（0 会让两道层级闸门全短路）。

    P19：agent yaml 损坏与"Agent 不存在"分开归因，不再误导用户检查 target_id。
    """

    def test_registry_invalid_level_rejected(self) -> None:
        """registry 路：小写 l2 等非法值 → 校验失败（非静默放行）。"""

        async def fake_lookup(agent_id: str):
            return {"level": "l2", "is_active": True}

        with patch.object(_tool_mod, "_agent_registry_lookup", fake_lookup):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("bad_level_agent", 1)
            )
        assert ok is False
        assert code == "TARGET_AGENT_LEVEL_MISSING"
        assert "bad_level_agent" in msg
        assert "l2" in msg

    def test_registry_missing_level_rejected(self) -> None:
        """registry 路：level 缺失（空串）→ 校验失败。"""

        async def fake_lookup(agent_id: str):
            return {"level": "", "is_active": True}

        with patch.object(_tool_mod, "_agent_registry_lookup", fake_lookup):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("no_level_agent", 1)
            )
        assert ok is False
        assert code == "TARGET_AGENT_LEVEL_MISSING"

    def test_registry_unknown_level_value_rejected(self) -> None:
        """registry 路：未知名单值（L9）→ 校验失败。"""

        async def fake_lookup(agent_id: str):
            return {"level": "L9", "is_active": True}

        with patch.object(_tool_mod, "_agent_registry_lookup", fake_lookup):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("weird_agent", 1)
            )
        assert ok is False
        assert code == "TARGET_AGENT_LEVEL_MISSING"
        assert "L9" in msg

    def test_registry_unavailable_fails_closed(self) -> None:
        """registry 服务故障（抛异常）→ AGENT_CONFIG_UNAVAILABLE，不冒充"不存在"。"""

        async def boom(agent_id: str):
            raise RuntimeError("agent_manager 服务不可用")

        with patch.object(_tool_mod, "_agent_registry_lookup", boom):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("any_agent", 1)
            )
        assert ok is False
        assert code == "AGENT_CONFIG_UNAVAILABLE"
        assert "agent_manager" in msg

    def test_registry_corrupt_config_delegates_not_found_to_manager(self) -> None:
        """配置损坏（agent_manager 按 not-found 同形应答，读失败留痕归它）→
        task_submit 如实报不存在——损坏归因是 agent_manager 的职责面。"""

        async def miss(agent_id: str):
            return None

        with patch.object(_tool_mod, "_agent_registry_lookup", miss):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("broken_agent", 1)
            )
        assert ok is False
        assert code == "TARGET_AGENT_NOT_FOUND"

    def test_registry_valid_l2_passes(self) -> None:
        """合法 L2 + parent L1 → 放行（回归：正常路径不受影响）。"""

        async def fake_lookup(agent_id: str):
            return {"level": "L2", "is_active": True}

        with patch.object(_tool_mod, "_agent_registry_lookup", fake_lookup):
            ok, msg, code = asyncio.run(
                TaskSubmitTool()._validate_target_agent("l2_agent", 1)
            )
        assert (ok, msg, code) == (True, "", "")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── 参数削减（2026-10-19）：goal 单字符串主形态——title=首行截断、description=全文 ──


def test_goal_string_derives_title_from_first_line_and_full_description():
    """goal 一段话（多行）：title=首行，description=全文。"""
    tool = TaskSubmitTool()
    raw = "优化检索性能\n\n完整背景：当前全文检索在 10 万行规模下超时。\n已尝试索引重建无果。"
    goal = tool._parse_goal_input({"goal": raw})
    assert goal["title"] == "优化检索性能"
    assert goal["description"] == raw


def test_goal_string_long_first_line_truncated_with_ellipsis():
    tool = TaskSubmitTool()
    long_line = "非" * 80
    goal = tool._parse_goal_input({"goal": long_line + "\n正文"})
    assert goal["title"] == "非" * 60 + "…"
    assert goal["description"].startswith("非" * 80)


def test_declaration_goal_is_primary_required():
    """声明契约：required 收缩为 [goal, target_id]；goal_title/description 降为可选细控。"""
    schema = TaskSubmitTool.get_tool_definition().input_schema
    assert schema["required"] == ["goal", "target_id"]
    assert schema["properties"]["goal"]["type"] == "string"
    assert "goal_title" in schema["properties"]
    assert "target_type" not in schema["properties"]
