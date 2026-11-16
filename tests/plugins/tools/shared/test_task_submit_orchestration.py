# @feature: FP-MIGR 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P2 编排运行面 | @ci: python-coverage
"""task_submit 编排解析 + 完备性闸门行为测试（P2：两级解析/H3/审计键）。

断输入→输出与结构化错误形状；解析域逻辑（两级链）单测见
plugins/shared/tasks/test_orchestration.py，本文件验证工具侧接线：

1. 旧调用兼容：不带 orchestration_key 的提交走② autonomous，行为不变
   （真实仓库编排发现，autonomous 派发成功 + 出生 state 记编排键）；
2. ① 显式键：命中即用；未命中 fail-closed（ORCHESTRATION_KEY_NOT_FOUND，
   含可用编排提示，不实例化——sender 零调用）；
3. mode 键不参与编排选择（D6：模式控制经 execution_context.mode 透传给
   消费面）——带 mode 的提交仍兜底 autonomous 且 mode 原样落
   execution_context；
4. 完备性（§3.5 派发期门口拒派）：agent 基座缺身份性字段 / target_type
   非 agent（bypass schema 直调）→ INCOMPLETE_INITIAL_INPUTS 结构化拒绝
   （{missing_fields, orchestration_key, suggestion}，不实例化）；
   target_type 常量内化（LLM 面无此参数）→ 缺省归一 agent，agent.id 计入
   左值正常放行。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "tasks"

for _d in (str(_TASKS_DIR), str(_TS_DIR)):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import orchestration as orchestration_mod  # noqa: E402 — tasks 域平铺模块

_TESTS_ROOT = _REPO_ROOT / "tests"
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
from _task_birth_workspace import inject_preparer, reset_preparer  # noqa: E402


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_orchestration_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TS_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


class _FakeSender:
    """chat.send_message 替身：出生各段各回同一 pipeline_id。"""

    def __init__(self, pipeline_id: str = "abcdef123456") -> None:
        self.calls: list[dict[str, Any]] = []
        self.pipeline_id = pipeline_id

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        return {"pipeline_id": self.pipeline_id}


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal_title": "写章节",
        "goal_description": "按大纲完成第三章",
        "target_type": "agent",
        "target_id": "code_writer",
        "parent_agent_level": 1,
        "user_id": "user-1",
        # 提交者会话坐标（生产由 param_inject 注入；完备性闸门按 state 键计必需）
        "session_id": "sess-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """工具实例 + target 校验替身 + 真实形态 agent 基座（身份字段齐备）。"""
    tool = mod.TaskSubmitTool()

    async def _ok(t: Any, level: Any) -> tuple[bool, str, str]:
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]

    async def _base_config(target_id: str) -> tuple[dict[str, Any] | None, None]:
        return (
            {
                "level": "L3",
                "is_active": True,
                "tool_ids": ["file_write"],
                "system_prompt": "persona-stub",
            },
            None,
        )

    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    return tool


def _fake_defs(**raws: dict[str, Any]) -> dict[str, Any]:
    return {
        key: orchestration_mod.OrchestrationDefinition(
            key=key, path=f"<{key}>", raw=raw
        )
        for key, raw in raws.items()
    }


@pytest.fixture(autouse=True)
def _restore_module_injections(tmp_path_factory, monkeypatch):
    """模块级注入点代际还原（sender / registry lookup / orchestration 补丁面）。

    用户根钉到空 tmp：mode 键磁盘回退的出厂命中断言不得依赖机器真实播种副本。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path_factory.mktemp("user-root")))
    yield
    mod = sys.modules.get("task_submit_tool_orchestration_test")
    if mod is not None:
        mod._chat_sender = None
        mod._agent_registry_lookup = None


async def _run_submit(mod: Any, inputs: dict[str, Any]) -> Any:
    sender = _FakeSender()
    mod.set_chat_sender(sender)
    inject_preparer(mod)
    tool = _make_tool(mod)
    try:
        result = await tool.execute(inputs)
    finally:
        mod._chat_sender = None
        reset_preparer(mod)
    return result, sender


# ── 旧调用兼容（② 兜底）──


class TestLegacyCalls:
    async def test_old_call_resolves_autonomous_and_records_key(self) -> None:
        """无编排键的旧调用走② autonomous：派发成功 + 出生 state 记键。"""
        mod = _load_module()
        result, sender = await _run_submit(mod, _base_inputs())
        assert result.success, result.error
        assert len(sender.calls) == 4, "出生四段（登记/身份/工作空间/派发，ADR 2026-10-17 增工作空间段）"
        birth_state = sender.calls[0]["state"]
        assert birth_state["task.orchestration"] == "autonomous"
        assert result.output["pipeline_id"] == "abcdef123456"

    async def test_old_call_agent_base_missing_fields_rejected(self) -> None:
        """旧调用 ≠ 免检：agent 基座缺身份性字段仍被完备性闸门拒派
        （fail-closed 提升到编排级，§3.5；错误是结构化值，M2 回流）。"""
        mod = _load_module()

        async def _thin_base(
            target_id: str,
        ) -> tuple[dict[str, Any] | None, None]:
            return {"level": "L3", "is_active": True}, None

        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        tool._get_agent_base_config = _thin_base  # type: ignore[method-assign]
        try:
            result = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert not result.success
        assert result.error_code == "INCOMPLETE_INITIAL_INPUTS"
        fields = result.metadata
        assert fields["orchestration_key"] == "autonomous"
        assert fields["missing_fields"] == ["context.system_prompt", "tool_ids"]
        assert fields["suggestion"]
        assert "context.system_prompt" in result.error
        assert sender.calls == [], "门口拒派不实例化（出生四段零调用）"


# ── ① 显式编排键 ──


class TestExplicitKey:
    async def test_explicit_autonomous_key_dispatches(self) -> None:
        """① 显式系统键命中即用（与兜底同定义，但走显式命中路径）。"""
        mod = _load_module()
        result, sender = await _run_submit(
            mod, _base_inputs(orchestration_key="autonomous")
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "autonomous"

    async def test_explicit_unknown_key_fails_closed_without_fallback(self) -> None:
        """① 显式键未命中 = fail-closed 结构化报错（含可用编排），不落②不实例化。"""
        mod = _load_module()
        result, sender = await _run_submit(
            mod, _base_inputs(orchestration_key="mode_writing/nope")
        )
        assert not result.success
        assert result.error_code == "ORCHESTRATION_KEY_NOT_FOUND"
        assert result.metadata["orchestration_key"] == "mode_writing/nope"
        assert "autonomous" in result.metadata["available_orchestrations"]
        assert "autonomous" in result.error
        assert sender.calls == []


# ── mode 键与编排选择解耦（D6：模式控制经 execution_context.mode 透传）──


class TestModeKeyDecoupled:
    async def test_mode_does_not_limit_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：带 mode 的提交不再按 mode 选编排——即便候选集里有该模式
        前缀的编排键，仍兜底 autonomous（mode 限定候选路径已随包内编排
        清单退役，D2/D6）。"""
        mod = _load_module()
        patched = _fake_defs(
            autonomous={},
            **{"mode_writing/chapter": {}},
        )
        monkeypatch.setattr(
            orchestration_mod,
            "discover_orchestrations",
            lambda *_args, **_kwargs: patched,
        )
        result, sender = await _run_submit(mod, _base_inputs(mode="writing"))
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "autonomous"

    async def test_mode_passes_through_to_execution_context(self) -> None:
        """mode 透传不受编排解耦影响：出生 execution_context 带 mode（子任务
        受该模式控制，D6）；空白视同未声明。"""
        mod = _load_module()
        result, sender = await _run_submit(mod, _base_inputs(mode="writing"))
        assert result.success, result.error
        dispatch = sender.calls[3]
        assert dispatch["execution_context"]["mode"] == "writing"

        result_blank, sender_blank = await _run_submit(mod, _base_inputs(mode="  "))
        assert result_blank.success, result_blank.error
        assert "mode" not in sender_blank.calls[3]["execution_context"]


# ── 完备性闸门（派发期门口拒派）──


class TestAgentBaseConfig:
    """_get_agent_base_config：注入钩子单源（无磁盘回退，解析权归 agent_manager）。"""

    async def test_registry_failure_fails_closed(self) -> None:
        """registry 查询抛错（服务故障）→ 显式失败信封（不冒充"缺字段"拒派）。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()

        async def _boom(target_id: str) -> dict[str, Any] | None:
            raise RuntimeError("agent_manager 服务不可用")

        mod.set_agent_registry_lookup(_boom)
        config, fail = await tool._get_agent_base_config("mode_coding/code_writer")
        assert config is None
        assert fail is not None
        assert fail.error_code == "AGENT_CONFIG_UNAVAILABLE"
        assert "agent_manager" in fail.error

    async def test_registry_hit_dict_returned_as_is(self) -> None:
        """registry 命中 dict → 原样返回（完备性身份字段同源解析）。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()
        base = {"level": "L3", "tool_ids": ["file_write"]}

        async def _hit(target_id: str) -> dict[str, Any] | None:
            assert target_id == "code_writer"
            return dict(base)

        mod.set_agent_registry_lookup(_hit)
        config, fail = await tool._get_agent_base_config("code_writer")
        assert fail is None
        assert config == base

    async def test_registry_miss_returns_none_without_fail(self) -> None:
        """registry 未命中（未注册形态）→ (None, None)（左值宁缺勿假）。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()

        async def _miss(target_id: str) -> dict[str, Any] | None:
            return None

        mod.set_agent_registry_lookup(_miss)
        config, fail = await tool._get_agent_base_config("no_such_agent_p2")
        assert config is None
        assert fail is None

    async def test_hook_missing_degrades_to_none(self) -> None:
        """钩子未注入（sidecar 接线缺失）→ (None, None) 文档化降级（左值宁缺勿假）。"""
        mod = _load_module()
        mod._agent_registry_lookup = None
        tool = mod.TaskSubmitTool()
        config, fail = await tool._get_agent_base_config("any_agent_p2")
        assert config is None
        assert fail is None


# ── 完备性闸门（派发期门口拒派）──


class TestCompleteness:
    async def test_target_type_constant_internalized_dispatches(self) -> None:
        """LLM 面无 target_type（常量内化）→ 缺省归一 agent：agent.id 计入
        完备性左值，派发放行（身份随 target_id 出生协议写全）。
        回归锚：归一值曾不回写 inputs，左值恒缺 agent.id，所有 LLM 派发被
        门口拒死（装机实测 2026-10-19 三连拒）。"""
        mod = _load_module()
        inputs = _base_inputs()
        inputs.pop("target_type")
        result, sender = await _run_submit(mod, inputs)
        assert result.success, result.error
        assert len(sender.calls) == 4

    async def test_non_agent_target_lacks_identity_rejected(self) -> None:
        """target_type 非 agent（bypass schema 直调）→ 无 agent.id 身份 →
        初始输入集不虚报，完备性拒绝（身份不明不派发）。"""
        mod = _load_module()
        result, sender = await _run_submit(
            mod, _base_inputs(target_type="widget", target_id="widget-1")
        )
        assert not result.success
        assert result.error_code == "INCOMPLETE_INITIAL_INPUTS"
        assert "agent.id" in result.metadata["missing_fields"]
        assert sender.calls == []

    async def test_passing_inputs_superset_dispatches(self) -> None:
        """真依赖过闸：真实仓库 autonomous 派生集（首批 H2 声明并集）⊆
        完整 agent 基座 + 提交参数的初始输入集 → 放行。"""
        mod = _load_module()
        result, sender = await _run_submit(mod, _base_inputs())
        assert result.success, result.error
        assert len(sender.calls) == 4
