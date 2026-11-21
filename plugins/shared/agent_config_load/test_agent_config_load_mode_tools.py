# @feature: FP-0.2.二 mode_personal M1 工具并入 | @ci: python-coverage
"""agent_config_load 模式声明工具并入单测（ADR 2026-10-12-personal-mode-package-
form-and-boundary，审查报告 §3 落点六要点）。

口径：①并入源 = 包 plugin.json capabilities.tools[].name（工具 id 形态）
②判据 = 非任务管道（无 lineage.*）③守卫 A = 仅显式 tool_ids list 分支并入
④启用判定 = tool-surface 探针活注册表交集 ⑤审计 = tool_ids.mode_source
⑥合并 = 去重保序追加。断行为（state_updates 可观察面），探针 caller 为外部
依赖允许注入 fake；模式包声明面走真实磁盘——主路径直接消费出厂根真实
mode_personal 包（关键路径真依赖），需出厂根隔离的用例 monkeypatch
mode_keys._SHARED_ROOT（文件系统布局=外部依赖，测试缝隙合法）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import mode_keys  # noqa: E402
from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

# 出厂根真实包（真依赖）：mode_personal 声明 pim 单工具
MODE_PKG_ID = "mode_personal"
MODE_TOOL_NAME = "pim"


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_mode_tool_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


def _seed_user_package(
    user_root: Path, pkg_id: str, tool_name: str, *, inject_tools: bool = True
) -> Path:
    """用户层注入假模式包（AGENTOS_USER_PLUGINS_DIR 声明面真实磁盘）。"""
    pkg = user_root / "plugins" / "modes" / f"mode_{pkg_id.removeprefix('mode_')}"
    pkg.mkdir(parents=True)
    (pkg / "plugin.json").write_text(json.dumps({
        "id": pkg_id,
        "capabilities": {"tools": [{"name": tool_name, "description": "fake"}]},
    }), encoding="utf-8")
    if inject_tools:
        (pkg / "mode.yaml").write_text(
            f"mode: {pkg_id.removeprefix('mode_')}\nname: 假包\ninject_tools: true\n",
            encoding="utf-8",
        )
    return pkg


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的用户配置层（出厂根保持真实——主路径消费真实 mode_personal）。"""
    user_root = tmp_path / "user-root"
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(user_root / "plugins"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    return user_root


def _seed_external_tool_package(
    user_root: Path, mode: str, tool_ids: Any
) -> Path:
    """用户层假模式包：mode.yaml 只声明 inject_tool_ids（外部工具注入面）。"""
    pkg = user_root / "plugins" / "modes" / f"mode_{mode}"
    pkg.mkdir(parents=True)
    (pkg / "plugin.json").write_text(
        json.dumps({"id": f"mode_{mode}", "capabilities": {"tools": []}}),
        encoding="utf-8",
    )
    if isinstance(tool_ids, str):
        body = f"mode: {mode}\nname: 假包\ninject_tool_ids: {tool_ids}\n"
    else:
        entries = "".join(f"  - {json.dumps(item)}\n" for item in tool_ids)
        body = f"mode: {mode}\nname: 假包\ninject_tool_ids:\n{entries}"
    (pkg / "mode.yaml").write_text(body, encoding="utf-8")
    return pkg


def _isolated_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """出厂模式根隔离（零声明用例：真实仓 mode_personal 不得泄漏进断言）。"""
    empty_modes = tmp_path / "factory-modes"
    empty_modes.mkdir()
    monkeypatch.setattr(mode_keys, "_SHARED_ROOT", tmp_path)
    empty_modes.parent.joinpath("modes")  # no-op（_SHARED_ROOT/modes 由函数内拼接）
    (tmp_path / "modes").mkdir(exist_ok=True)


def _write_agent_yaml(env: Path, name: str, tool_ids_line: str) -> None:
    """写 factory agent yaml（AGENTOS_CONFIG_ROOT 隔离）。"""
    agents = env.parent / "factory" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.yaml").write_text(
        f"display_name: 执行者\n{tool_ids_line}\n", encoding="utf-8"
    )


@pytest.fixture
def factory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agents = tmp_path / "factory" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "factory"))
    return agents


def _caller_with(live: set[str], calls: list[tuple[str, dict]] | None = None) -> Any:
    async def caller(method: str, params: dict, timeout: float | None = None) -> dict:
        if calls is not None:
            calls.append((method, params))
        schemas = [{"function": {"name": name}} for name in sorted(live)]
        return {"schemas": schemas, "contracts": {}}

    return caller


def _plugin_with(caller: Any) -> Any:
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_capability_caller(caller)
    return plugin


class TestInjectionHappyPath:
    def test_non_task_pipeline_gets_mode_tools(
        self, env: Path, factory_root: Path
    ) -> None:
        """①非任务管道（无 lineage.*）×真实 mode_personal 声明×探针命中 →
        并入 pim + 审计键；探针以工具 id 形态查询（非插件 id）。"""
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}, calls))
        _write_agent_yaml(env, "agentos", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == ["file_read", MODE_TOOL_NAME]
        assert res.state_updates["tool_ids.mode_source"] == {MODE_TOOL_NAME: MODE_PKG_ID}
        assert calls and calls[0][0] == "schemas"
        assert calls[0][1] == {"tool_ids": [MODE_TOOL_NAME]}

    def test_merge_dedupes_preserving_order(
        self, env: Path, factory_root: Path
    ) -> None:
        """⑥去重保序追加：原清单顺序不动、重复 id 不重、新 id 追加在尾。"""
        _seed_user_package(env, "mode_extra", "zread_extra")  # 第二包新工具
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(
            _caller_with({MODE_TOOL_NAME, "zread_extra", "file_read"}, calls)
        )
        _write_agent_yaml(env, "agentos", "tool_ids: [zread_extra, file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == [
            "zread_extra", "file_read", MODE_TOOL_NAME,
        ]
        assert res.state_updates["tool_ids.mode_source"] == {
            MODE_TOOL_NAME: MODE_PKG_ID,
            "zread_extra": "mode_extra",
        }


class TestGuards:
    def test_task_pipeline_never_injected(
        self, env: Path, factory_root: Path
    ) -> None:
        """②任务管道（带 lineage.*）结构性不可见——零探针零并入零审计键。"""
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}, calls))
        _write_agent_yaml(env, "executor", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({
            "agent.id": "executor",
            "lineage.parent_pipeline_id": "pipe-parent",
            "task.id": "pipe-child",
        })))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates
        assert calls == []  # 任务管道零探针（判据先行，省一次 capability 调用）

    def test_inherit_form_never_injected(
        self, env: Path, factory_root: Path
    ) -> None:
        """③守卫 A：tool_ids: inherit 不写键不并入（凭空写入 = 白名单语义反转）。"""
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}))
        _write_agent_yaml(env, "inheritor", "tool_ids: inherit")

        res = _run(plugin.execute(_ctx({"agent.id": "inheritor"})))

        assert "tool_ids" not in res.state_updates
        assert "tool_ids.mode_source" not in res.state_updates

    def test_undeclared_form_never_injected(
        self, env: Path, factory_root: Path
    ) -> None:
        """③守卫 A：未声明 tool_ids 同样不并入（键不写）。"""
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}))
        _write_agent_yaml(env, "bare", "display_name: 裸")

        res = _run(plugin.execute(_ctx({"agent.id": "bare"})))

        assert "tool_ids" not in res.state_updates
        assert "tool_ids.mode_source" not in res.state_updates

    def test_explicit_empty_list_still_additive(
        self, env: Path, factory_root: Path
    ) -> None:
        """显式空表 = agent 声明零 agent 工具（list 形态），模式工具仍按加行制并入。"""
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}))
        _write_agent_yaml(env, "zero", "tool_ids: []")

        res = _run(plugin.execute(_ctx({"agent.id": "zero"})))

        assert res.state_updates["tool_ids"] == [MODE_TOOL_NAME]

    def test_disabled_package_not_injected(
        self, env: Path, factory_root: Path
    ) -> None:
        """④启用判定：包声明了但探针（活注册表）不命中 = 不并入（禁用/G2 遮挡/
        未注册三态同解）。"""
        plugin = _plugin_with(_caller_with(set()))  # 注册表无 pim（包被禁用）
        _write_agent_yaml(env, "agentos", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates

    def test_no_mode_tool_declarations_zero_probe(
        self, env: Path, factory_root: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无包声明工具（六模式 tools:[] 现状）= 零探针零成本，行为与旧版一致。"""
        _isolated_factory(monkeypatch, tmp_path)
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(_caller_with(set(), calls))
        _write_agent_yaml(env, "agentos", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert calls == []


class TestProbeDegradation:
    def test_caller_unwired_skips_injection(
        self, env: Path, factory_root: Path
    ) -> None:
        """④探针通道未接线 = 本轮不并入（降级安全方向，权限面不放宽）。"""
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})  # 不注入 caller
        _write_agent_yaml(env, "agentos", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates

    def test_caller_error_skips_injection(
        self, env: Path, factory_root: Path
    ) -> None:
        """④探针调用失败 = 本轮不并入（错误是值：降级 + warning 留痕）。"""

        async def broken_caller(
            method: str, params: dict, timeout: float | None = None
        ) -> dict:
            raise RuntimeError("tool-surface unreachable")

        plugin = _plugin_with(broken_caller)
        _write_agent_yaml(env, "agentos", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({"agent.id": "agentos"})))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates


class TestDeclarationFace:
    def test_mode_source_exported_in_manifest(self) -> None:
        """⑤审计键同刀进 export_fields（漏声明 = 审计面出口断链，审查 R9）。"""
        with open(_DIR / "plugin.json", encoding="utf-8") as fh:
            manifest = json.load(fh)
        assert "tool_ids.mode_source" in manifest["export_fields"]

    def test_aggregation_shape_dual_root_and_opt_in(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """①聚合面：出厂真实包（mode_personal 显式 inject_tools）贡献
        pim→mode_personal；未声明并入的包零贡献（守卫 C，mode_evolution 注册式
        工具同形态）；双根同 id 用户副本整包赢；非法工具名零贡献。"""
        _seed_user_package(env, "mode_personal", "user_override_tool")  # 同 id 用户副本
        pkg_bad = env / "plugins" / "modes" / "mode_badname"
        pkg_bad.mkdir(parents=True)
        (pkg_bad / "plugin.json").write_text(json.dumps({
            "id": "mode_badname",
            "capabilities": {"tools": [{"name": "有 空格"}]},
        }), encoding="utf-8")
        (pkg_bad / "mode.yaml").write_text(
            "mode: badname\nname: 坏名\ninject_tools: true\n", encoding="utf-8"
        )
        declared = mode_keys.collect_mode_tool_ids()
        assert declared["user_override_tool"] == "mode_personal"  # 用户副本整包赢
        assert MODE_TOOL_NAME not in declared  # 出厂同 id 包不参与（双根同 id 用户赢）
        assert "有 空格" not in declared  # 非法形态条目零贡献
        # 守卫 C：mode_evolution 声明八件工具但未声明 inject_tools → 全部零贡献
        for evolution_tool in ("eval_view_suite", "proposal_apply", "eval.screen"):
            assert evolution_tool not in declared, evolution_tool

    def test_opt_in_gate_packages_without_declaration(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③守卫 C（包级）：plugin.json 有 tools 但 mode.yaml 未声明 inject_tools
        = 零贡献（注册式工具包缺省不入面）。"""
        _isolated_factory(monkeypatch, tmp_path)
        _seed_user_package(env, "silent", "silent_tool", inject_tools=False)
        declared = mode_keys.collect_mode_tool_ids()
        assert "silent_tool" not in declared

    def test_generalization_alternative_yaml_forms(
        self, env: Path, factory_root: Path
    ) -> None:
        """泛化性自检（并入判据，训练集外 2 组）：不同 agent yaml 形态同契约——
        config_id 匹配形态与嵌套目录形态都按同一三态判据装配。"""
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}))
        agents = factory_root
        (agents / "nested").mkdir()
        (agents / "nested" / "writer.yaml").write_text(
            "display_name: 嵌套写手\ntool_ids: [file_read]\n", encoding="utf-8"
        )
        (agents / "cfg.yaml").write_text(
            "display_name: 别名\ntool_ids: [file_read]\nconfig_id: cfg_alias\n",
            encoding="utf-8",
        )

        nested = _run(plugin.execute(_ctx({"agent.id": "nested/writer"})))
        aliased = _run(plugin.execute(_ctx({"agent.id": "cfg_alias"})))

        assert nested.state_updates["tool_ids"] == ["file_read", MODE_TOOL_NAME]
        assert aliased.state_updates["tool_ids"] == ["file_read", MODE_TOOL_NAME]
        assert aliased.state_updates["tool_ids.mode_source"] == {
            MODE_TOOL_NAME: MODE_PKG_ID,
        }


class TestTaskModeTagInjection:
    """任务模式标签注入（ADR 2026-11-11 D2）：激活模式的 inject_tool_ids 在
    **任务管道**也并入；模式包 capabilities.tools 的 lineage 守卫不变。"""

    def test_task_pipeline_injects_external_tool_of_active_mode(
        self, env: Path, factory_root: Path
    ) -> None:
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(_caller_with({"plugin_verify"}, calls))
        _write_agent_yaml(env, "executor", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({
            "agent.id": "executor",
            "lineage.parent_pipeline_id": "pipe-parent",
            "task.id": "pipe-child",
            "execution_context": {"mode": ["evolution"]},
        })))

        assert res.state_updates["tool_ids"] == ["file_read", "plugin_verify"]
        assert res.state_updates["tool_ids.mode_source"] == {
            "plugin_verify": "mode_evolution",
        }
        assert calls == [("schemas", {"tool_ids": ["plugin_verify"]})]

    def test_task_pipeline_capabilities_guard_still_holds(
        self, env: Path, factory_root: Path
    ) -> None:
        """任务管道仍不并入模式包 capabilities.tools（pim 零探针零并入）。"""
        calls: list[tuple[str, dict]] = []
        plugin = _plugin_with(_caller_with({MODE_TOOL_NAME}, calls))
        _write_agent_yaml(env, "executor", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({
            "agent.id": "executor",
            "lineage.parent_pipeline_id": "pipe-parent",
            "task.id": "pipe-child",
            "execution_context": {"mode": ["personal"]},
        })))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates
        assert calls == []

    def test_multitag_primary_is_last_tag(
        self, env: Path, factory_root: Path
    ) -> None:
        """标签集主标签 = 末元素（最新激活优先），按主标签包取 inject_tool_ids。"""
        _seed_external_tool_package(env, "zeta", ["zeta_tool"])
        plugin = _plugin_with(_caller_with({"zeta_tool"}))
        _write_agent_yaml(env, "executor", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({
            "agent.id": "executor",
            "lineage.parent_pipeline_id": "pipe-parent",
            "execution_context": {"mode": ["evolution", "zeta"]},
        })))

        assert res.state_updates["tool_ids"] == ["file_read", "zeta_tool"]
        assert res.state_updates["tool_ids.mode_source"] == {"zeta_tool": "mode_zeta"}

    def test_live_registry_miss_skips_task_injection(
        self, env: Path, factory_root: Path
    ) -> None:
        """活注册表不命中（插件未装载/G2 遮挡）→ 不并入，权限面不放宽。"""
        plugin = _plugin_with(_caller_with(set()))
        _write_agent_yaml(env, "executor", "tool_ids: [file_read]")

        res = _run(plugin.execute(_ctx({
            "agent.id": "executor",
            "lineage.parent_pipeline_id": "pipe-parent",
            "execution_context": {"mode": ["evolution"]},
        })))

        assert res.state_updates["tool_ids"] == ["file_read"]
        assert "tool_ids.mode_source" not in res.state_updates


class TestModeKeysHelpers:
    """mode_keys 模式标签/外部工具声明解析的形态闸（单点语义）。"""

    def test_primary_mode_tag_forms(self) -> None:
        assert mode_keys.primary_mode_tag(["coding", " evolution "]) == "evolution"
        assert mode_keys.primary_mode_tag(" coding ") == "coding"
        assert mode_keys.primary_mode_tag(["", "  "]) == ""
        assert mode_keys.primary_mode_tag(None) == ""
        assert mode_keys.primary_mode_tag({"mode": "x"}) == ""

    def test_resolve_active_mode_priority_and_package_gate(self) -> None:
        assert mode_keys.resolve_active_mode(
            {"execution_context": {"mode": ["evolution"]}}
        ) == "evolution"
        assert mode_keys.resolve_active_mode({"mode": "coding"}) == "coding"
        assert mode_keys.resolve_active_mode(
            {"agent.id": "mode_research/research_agent"}
        ) == "research"
        # 包不存在 → 不派生（防凭空键伪造）
        assert mode_keys.resolve_active_mode({"agent.id": "mode_nope/x"}) == ""
        assert mode_keys.resolve_active_mode({"agent.id": "plain_agent"}) == ""

    def test_collect_inject_tool_ids_shape_guard(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolated_factory(monkeypatch, tmp_path)
        assert mode_keys.collect_mode_inject_tool_ids("../evil") == {}
        assert mode_keys.collect_mode_inject_tool_ids("") == {}
        assert mode_keys.collect_mode_inject_tool_ids("missing") == {}

        # 未声明 / 非列表 = 零贡献
        _seed_external_tool_package(env, "scalar", "single_tool")
        assert mode_keys.collect_mode_inject_tool_ids("scalar") == {}

        # 合法条目收、非法条目跳过
        _seed_external_tool_package(env, "mixed", ["ok_tool", "bad name", 3])
        assert mode_keys.collect_mode_inject_tool_ids("mixed") == {
            "ok_tool": "mode_mixed",
        }

        # mode.yaml 读失败（目录占位）→ 零贡献
        broken = env / "plugins" / "modes" / "mode_broken"
        broken.mkdir(parents=True)
        (broken / "plugin.json").write_text(
            json.dumps({"id": "mode_broken"}), encoding="utf-8"
        )
        (broken / "mode.yaml").mkdir()
        assert mode_keys.collect_mode_inject_tool_ids("broken") == {}
