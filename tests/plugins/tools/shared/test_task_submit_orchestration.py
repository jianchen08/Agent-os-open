# @feature: FP-MIGR 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P2 编排运行面 | @ci: python-coverage
"""task_submit 编排解析 + 完备性闸门行为测试（P2：三级解析/H3/H4/审计键）。

断输入→输出与结构化错误形状；解析域逻辑（三级链）单测见
plugins/shared/system/tasks/test_orchestration.py，本文件验证工具侧接线：

1. 旧调用兼容：不带 orchestration_key/mode 的提交走③ autonomous，行为不变
   （真实仓库编排发现，autonomous 派发成功 + 出生 state 记编排键）；
2. ① 显式键：命中即用；未命中 fail-closed（ORCHESTRATION_KEY_NOT_FOUND，
   含可用编排提示，不实例化——sender 零调用）；显式键优先于 mode；
3. ② mode 键：限定候选（task_kinds 匹配优先/唯一候选选定）；无自有编排或
   意图不明落③（H4：约束非门槛）；
4. 完备性（§3.5 派发期门口拒派）：agent 基座缺身份性字段 / target_type
   非 agent（bypass schema 直调）→ INCOMPLETE_INITIAL_INPUTS 结构化拒绝
   （{missing_fields, orchestration_key, suggestion}，不实例化）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"

for _d in (str(_TASKS_DIR), str(_TS_DIR)):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import orchestration as orchestration_mod  # noqa: E402 — tasks 域平铺模块


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
    """chat.send_message 替身：三段出生各回同一 pipeline_id。"""

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
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """工具实例 + target 校验替身 + 真实形态 agent 基座（身份字段齐备）。"""
    tool = mod.TaskSubmitTool()

    async def _ok(t: Any, level: Any) -> tuple[bool, str, str]:
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]

    async def _base_config(target_id: str) -> dict[str, Any] | None:
        return {
            "level": "L3",
            "is_active": True,
            "tool_ids": ["file_write"],
            "system_prompt": "persona-stub",
        }

    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    return tool


def _fake_defs(**raws: dict[str, Any]) -> dict[str, Any]:
    return {
        key: orchestration_mod.OrchestrationDefinition(
            key=key,
            path=f"<{key}>",
            raw=raw,
            task_kinds=tuple(raw.get("task_kinds", ())),
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
    tool = _make_tool(mod)
    try:
        result = await tool.execute(inputs)
    finally:
        mod._chat_sender = None
    return result, sender


# ── 旧调用兼容（③ 兜底）──


class TestLegacyCalls:
    async def test_old_call_resolves_autonomous_and_records_key(self) -> None:
        """无编排键/mode 的旧调用走③ autonomous：派发成功 + 出生 state 记键。"""
        mod = _load_module()
        result, sender = await _run_submit(mod, _base_inputs())
        assert result.success, result.error
        assert len(sender.calls) == 3, "出生三段（登记/身份/派发）"
        birth_state = sender.calls[0]["state"]
        assert birth_state["task.orchestration"] == "autonomous"
        assert result.output["pipeline_id"] == "abcdef123456"

    async def test_old_call_agent_base_missing_fields_rejected(self) -> None:
        """旧调用 ≠ 免检：agent 基座缺身份性字段仍被完备性闸门拒派
        （fail-closed 提升到编排级，§3.5；错误是结构化值，M2 回流）。"""
        mod = _load_module()

        async def _thin_base(target_id: str) -> dict[str, Any] | None:
            return {"level": "L3", "is_active": True}

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
        assert sender.calls == [], "门口拒派不实例化（出生三段零调用）"


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
        """① 显式键未命中 = fail-closed 结构化报错（含可用编排），不落③不实例化。"""
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

    async def test_explicit_key_takes_precedence_over_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """① 显式键优先：带 mode 限定也无视——主 agent 显式选择是最高意图。"""
        mod = _load_module()
        patched = _fake_defs(
            autonomous={},
            **{"mode_writing/chapter": {"task_kinds": ["chapter"]}},
        )
        monkeypatch.setattr(
            orchestration_mod,
            "discover_orchestrations",
            lambda *_args, **_kwargs: patched,
        )
        result, sender = await _run_submit(
            mod,
            _base_inputs(orchestration_key="autonomous", mode="mode_writing"),
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "autonomous"


# ── ② mode 键（H4：约束非门槛）──


class TestModeKey:
    async def _run_with_defs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        defs: dict[str, Any],
        **over: Any,
    ) -> Any:
        monkeypatch.setattr(
            orchestration_mod, "discover_orchestrations", lambda *_args, **_kwargs: defs
        )
        return await _run_submit(_load_module(), _base_inputs(**over))

    async def test_mode_single_candidate_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """② mode 包唯一编排即选定，出生 state 记全限定键。"""
        result, sender = await self._run_with_defs(
            monkeypatch,
            _fake_defs(
                autonomous={},
                **{"mode_writing/chapter": {"task_kinds": ["chapter"]}},
            ),
            mode="mode_writing",
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "mode_writing/chapter"

    async def test_mode_task_kind_match_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """② 多候选：task_kinds 与 task_kind 唯一命中者优先。"""
        result, sender = await self._run_with_defs(
            monkeypatch,
            _fake_defs(
                autonomous={},
                **{
                    "mode_writing/chapter": {"task_kinds": ["chapter"]},
                    "mode_writing/review": {"task_kinds": ["review"]},
                },
            ),
            mode="mode_writing",
            task_kind="review",
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "mode_writing/review"

    async def test_mode_ambiguous_falls_to_autonomous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """② 多候选且无法区分意图 → 落③（H4：③只接意图不明）。"""
        result, sender = await self._run_with_defs(
            monkeypatch,
            _fake_defs(
                autonomous={},
                **{
                    "mode_writing/chapter": {},
                    "mode_writing/review": {},
                },
            ),
            mode="mode_writing",
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "autonomous"

    async def test_mode_without_own_pipelines_falls_to_autonomous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """② mode 无自有编排 → 落③：mode 是约束非门槛，共享默认仍有效。"""
        result, sender = await self._run_with_defs(
            monkeypatch,
            _fake_defs(autonomous={}),
            mode="mode_coding",
        )
        assert result.success, result.error
        assert sender.calls[0]["state"]["task.orchestration"] == "autonomous"


# ── 完备性闸门（派发期门口拒派）──


class TestAgentBaseConfig:
    """_get_agent_base_config：registry 优先 → 磁盘回退（与 target 校验同解析序）。"""

    async def test_registry_failure_falls_back_to_disk(self) -> None:
        """registry 查询抛错（服务故障）→ 降级磁盘 yaml，不向调用方抛。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()

        async def _boom(target_id: str) -> dict[str, Any] | None:
            raise RuntimeError("agent_manager 服务不可用")

        mod.set_agent_registry_lookup(_boom)
        got = await tool._get_agent_base_config("mode_coding/code_writer")
        assert got is not None
        assert "level" in got

    async def test_registry_non_dict_falls_back_to_disk(self) -> None:
        """registry 返回非 dict（信封违约形态）→ 视为未命中，磁盘回退。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()

        async def _junk(target_id: str) -> dict[str, Any] | None:
            return "not-a-dict"  # type: ignore[return-value]

        mod.set_agent_registry_lookup(_junk)
        got = await tool._get_agent_base_config("mode_coding/code_writer")
        assert got is not None
        assert "level" in got

    async def test_registry_hit_dict_returned_as_is(self) -> None:
        """registry 命中 dict → 原样返回（不触磁盘；完备性身份字段同源解析）。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()
        base = {"level": "L3", "tool_ids": ["file_write"]}

        async def _hit(target_id: str) -> dict[str, Any] | None:
            assert target_id == "code_writer"
            return dict(base)

        mod.set_agent_registry_lookup(_hit)
        assert await tool._get_agent_base_config("code_writer") == base

    async def test_registry_miss_without_disk_hit_returns_none(self) -> None:
        """registry 未命中且磁盘无此 agent（未注册形态）→ None（左值宁缺勿假）。"""
        mod = _load_module()
        tool = mod.TaskSubmitTool()

        async def _miss(target_id: str) -> dict[str, Any] | None:
            return None

        mod.set_agent_registry_lookup(_miss)
        assert await tool._get_agent_base_config("no_such_agent_p2") is None

    def test_disk_yaml_empty_file_is_empty_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空 yaml = 空配置 dict（合法形态，按默认运行）。"""
        mod = _load_module()
        cfg_dir = tmp_path / "config" / "agents"
        cfg_dir.mkdir(parents=True)
        target = cfg_dir / "empty_agent_p2.yaml"
        target.write_text("", encoding="utf-8")
        import pathlib

        def _fake_rglob(self: Any, pattern: str) -> Any:
            if pattern == "empty_agent_p2.yaml":
                return iter([target])
            return pathlib.Path.rglob(self, pattern)

        monkeypatch.setattr(pathlib.Path, "rglob", _fake_rglob)
        config, corrupt = mod.TaskSubmitTool._load_agent_yaml_dict("empty_agent_p2")
        assert config == {}
        assert corrupt == ""

    def test_disk_yaml_non_mapping_is_corrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """yaml 解析为非映射（配置损坏形态）→ (None, 损坏路径) 与不存在区分归因。"""
        mod = _load_module()
        cfg_dir = tmp_path / "config" / "agents"
        cfg_dir.mkdir(parents=True)
        target = cfg_dir / "junk_agent_p2.yaml"
        target.write_text("- just\n- a list\n", encoding="utf-8")
        import pathlib

        def _fake_rglob(self: Any, pattern: str) -> Any:
            if pattern == "junk_agent_p2.yaml":
                return iter([target])
            return pathlib.Path.rglob(self, pattern)

        monkeypatch.setattr(pathlib.Path, "rglob", _fake_rglob)
        config, corrupt = mod.TaskSubmitTool._load_agent_yaml_dict("junk_agent_p2")
        assert config is None
        assert corrupt.endswith("junk_agent_p2.yaml")


# ── 完备性闸门（派发期门口拒派）──


class TestCompleteness:
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
        assert len(sender.calls) == 3
