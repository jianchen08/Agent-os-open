# @feature: FP-0.2.二 模式体系 P2 context_build 物料档注入 | @ci: python-coverage
"""context_build 模式物料档注入行为契约（模式体系 P2，设计稿 §3.3②/§4.1/§4.2）。

锁六件事：
1. **带 mode（main 路径）**：模式段注入 system_prompt（编排清单键
   mode_X/<stem> + task_kinds、调度链/执行者池、口径段），tool_ids 按
   material_scope.tool_ids 收窄（结果 ⊆ 基线，只收窄不扩权）；
2. **不带 mode / 非法 mode**：零注入，profile 服务零调用（行为不变）；
3. **服务失败降级**：mode.get_profile 抛错/通道未接线 → 不注入不阻断，
   管道其余产出照常；
4. **专属 agent 不注入**（§4.2 档2/档3，Wave2/P3 分支留接口）；
5. **不扩权边界**：模式未细化 tool_ids 或基线缺失 = 不收窄；交集为空 =
   显式空表；
6. **包目录取数**：pipelines/*.yaml 解析（坏文件跳过）、rules/*.md 拼接、
   双根解析（用户副本优先 → 出厂种子回落 → 缺失 None）；
7. **state 顶层 mode 键回写**（BUG-34）：解析成功即回写（值 = resolve_mode
   非空结果），同管道重跑覆盖更新；解析不出/自动模式/形态非法不写键（前端
   无键零动作）；物料注入降级不回滚回写。

mode 键区分度输入 ≥2：utdemo（全物料模式）与 utplain（裸模式）两组 profile
+ 包目录内容互异，注入结果随之不同。

[来源: docs/working/模式体系落地设计_20260915.md；服务通道先例
plugins/shared/system/eval_harness/server.py::_fetch_mode_profile]
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "context_build")
import mode_material  # noqa: E402
import plugin as context_build_mod  # noqa: E402


def _ctx(state: dict[str, Any]):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


class FakeProfileService:
    """mode.get_profile 假服务（外部依赖 mock）：记录调用，可编程信封/异常。"""

    def __init__(
        self,
        profiles: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
        envelope: bool = False,
    ) -> None:
        self.profiles = profiles or {}
        self.error = error
        self.envelope = envelope
        self.calls: list[str] = []

    async def __call__(self, mode: str) -> Any:
        self.calls.append(mode)
        if self.error is not None:
            raise self.error
        profile = self.profiles[mode]
        return {"data": profile} if self.envelope else profile


def _run(service: Any, config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    cb = context_build_mod.ContextBuildPlugin(config=config, profile_fetcher=service)
    return asyncio.run(cb.execute(_ctx(state))).state_updates


def _write_main_agent(agents_dir: Path, tool_ids: list[str] | None = None) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    text = "display_name: 灵汐\nagent_type: main\nsystem_prompt: 你是主agent基线提示词\n"
    if tool_ids is not None:
        text += "tool_ids:\n" + "".join(f"  - {t}\n" for t in tool_ids)
    (agents_dir / "agentos.yaml").write_text(text, encoding="utf-8")


def _write_mode_package(
    modes_dir: Path,
    mode: str,
    *,
    pipelines: dict[str, str] | None = None,
    rules: dict[str, str] | None = None,
) -> Path:
    """造一个用户副本模式包（plugin.json 为包标记；pipelines/rules 按需）。"""
    pkg = modes_dir / f"mode_{mode}"
    (pkg / "pipelines").mkdir(parents=True, exist_ok=True)
    (pkg / "rules").mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text('{"id": "mode_x"}', encoding="utf-8")
    for stem, text in (pipelines or {}).items():
        (pkg / "pipelines" / f"{stem}.yaml").write_text(text, encoding="utf-8")
    for name, text in (rules or {}).items():
        (pkg / "rules" / name).write_text(text, encoding="utf-8")
    return pkg


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离三根：用户根/配置根钉 tmp；返回 agents 与 modes 目录坐标。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    return {
        "root": tmp_path,
        "agents": tmp_path / "agents",
        "modes": tmp_path / "plugins" / "modes",
    }


_UTDEMO_PROFILE: dict[str, Any] = {
    "mode": "utdemo",
    "name": "演示模式",
    "chain": {
        "entry": "main",
        "expected_path": ["main", "orchestrator/demo_orchestrator"],
        "executor_pool": ["executor/demo", "executor/general_agent"],
    },
    "material_scope": {"tool_ids": ["file_read", "file_write", "task_submit"]},
}

_UTPLAIN_PROFILE: dict[str, Any] = {
    "mode": "utplain",
    "name": "裸模式",
    "chain": {"entry": "main", "expected_path": ["main"]},
}


class TestModeInjectionMainPath:
    def test_full_material_injects_and_narrows(self, env) -> None:
        """带 mode + 全物料包：模式段三件齐活，tool_ids 收窄为基线∩模式（保基线序）。"""
        _write_main_agent(env["agents"], tool_ids=["file_read", "web_search", "task_submit", "file_write"])
        _write_mode_package(
            env["modes"],
            "utdemo",
            pipelines={"fix_flow": "task_kinds: [缺陷修复, 测试修复]\nloop_bodies: []\n"},
            rules={"口径.md": "演示口径：一切输出带验证证据。"},
        )
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE}, envelope=True)

        updates = _run(service, {}, {"execution_context": {"mode": "utdemo"}})

        prompt = updates["context.system_prompt"]
        assert "## 模式物料（mode=utdemo）" in prompt
        assert "你是主agent基线提示词" in prompt, "基线提示词必须保留（追加非替换）"
        assert "`mode_utdemo/fix_flow`" in prompt, "编排清单用约定键 mode_X/<stem>"
        assert "缺陷修复" in prompt, "文件头 task_kinds 进路由指引"
        assert "演示口径：一切输出带验证证据。" in prompt, "包内 rules 口径段注入"
        assert "main → orchestrator/demo_orchestrator" in prompt, "调度链（偏好非强制）"
        assert "executor/demo" in prompt, "执行者池进路由指引"
        assert "模式工具面收窄" in prompt
        baseline = ["file_read", "web_search", "task_submit", "file_write"]
        narrowed = updates["tool_ids"]
        assert narrowed == ["file_read", "task_submit", "file_write"], (
            "收窄 = 基线 ∩ 模式声明，保基线序"
        )
        mode_ids = set(_UTDEMO_PROFILE["material_scope"]["tool_ids"])
        # 性质断言：收窄结果是基线与模式声明的公共子集（只收窄不扩权）
        assert set(narrowed) <= set(baseline), "收窄结果 ⊆ 基线"
        assert set(narrowed) <= mode_ids, "收窄结果 ⊆ 模式声明"
        assert service.calls == ["utdemo"]

    def test_bare_mode_placeholders_and_no_narrowing(self, env) -> None:
        """裸模式（无包目录 + 未细化 tool_ids）：占位注明 + 基线工具面不动。"""
        _write_main_agent(env["agents"], tool_ids=["file_read", "web_search"])
        service = FakeProfileService({"utplain": _UTPLAIN_PROFILE})

        updates = _run(service, {}, {"execution_context": {"mode": "utplain"}})

        prompt = updates["context.system_prompt"]
        assert "## 模式物料（mode=utplain）" in prompt
        assert "暂未携带专属编排" in prompt, "无包目录 = 编排清单空，注明走共享编排"
        assert "暂未携带口径规则" in prompt, "无 rules = 口径段占位注明"
        assert "未细化 tool_ids" in prompt, "material_scope 未细化 = 如实注明不收窄"
        assert updates["tool_ids"] == ["file_read", "web_search"], "工具面维持基线"


class TestZeroInjectionAndDegradation:
    def test_no_mode_key_is_zero_injection(self, env) -> None:
        """无 mode 键 = 零注入：提示词/工具面与既有行为一致，服务零调用。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {}, {"session_id": "s1"})

        assert "模式物料" not in updates["context.system_prompt"]
        assert updates["tool_ids"] == ["file_read"]
        assert service.calls == []

    def test_malformed_mode_key_is_zero_injection(self, env) -> None:
        """非法 mode 形态（大写/符号/非字符串/非 dict 上下文）一律不注入不取数。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})
        for ec in [{"mode": "Coding!"}, {"mode": 42}, "not-a-dict", {"nomode": 1}]:
            updates = _run(service, {}, {"execution_context": ec})
            assert "模式物料" not in updates["context.system_prompt"], f"ec={ec!r}"
            assert updates["tool_ids"] == ["file_read"], f"ec={ec!r}"
        assert service.calls == []

    def test_profile_service_failure_degrades_without_blocking(self, env) -> None:
        """mode.get_profile 抛错 = 降级不注入：管道其余产出照常（不阻断）。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        service = FakeProfileService(error=RuntimeError("mode sidecar down"))

        updates = _run(
            service, {}, {"execution_context": {"mode": "utdemo"}, "session_id": "s2"}
        )

        assert "模式物料" not in updates["context.system_prompt"]
        assert updates["tool_ids"] == ["file_read"]
        assert updates["context.agent_name"] == "灵汐", "管道其余产出不受影响"

    def test_unwired_fetcher_degrades(self, env) -> None:
        """取数通道未接线（构造未注入 fetcher）= 同样降级不注入。"""
        _write_main_agent(env["agents"])
        cb = context_build_mod.ContextBuildPlugin(config={})
        updates = asyncio.run(
            cb.execute(_ctx({"execution_context": {"mode": "utdemo"}}))
        ).state_updates
        assert "模式物料" not in updates["context.system_prompt"]

    def test_specialist_agent_not_injected(self, env) -> None:
        """§4.2 档2/档3：绑定专属 agent（agent_type≠main）不注入、不取数。"""
        agents = env["agents"]
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "specialist.yaml").write_text(
            "display_name: 专属执行者\nagent_type: specialized\n", encoding="utf-8"
        )
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(
            service,
            {},
            {"execution_context": {"mode": "utdemo"}, "agent.id": "specialist"},
        )

        assert "模式物料" not in updates["context.system_prompt"]
        assert service.calls == []


class TestNarrowingBoundaries:
    def test_no_baseline_tool_ids_never_expands(self, env) -> None:
        """基线缺 tool_ids：模式声明不套用（套用即扩权），不写 tool_ids 键。"""
        _write_main_agent(env["agents"])  # yaml 无 tool_ids
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {}, {"execution_context": {"mode": "utdemo"}})

        assert "tool_ids" not in updates, "不得把模式清单当基线写入（只收窄不扩权）"
        assert "不套用" in updates["context.system_prompt"]

    def test_disjoint_intersection_writes_explicit_empty(self, env) -> None:
        """交集为空 = 显式空表（键存在即写，下游按零工具消费）。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        profile = {
            "mode": "utdemo",
            "name": "演示模式",
            "material_scope": {"tool_ids": ["memory_search"]},
        }
        service = FakeProfileService({"utdemo": profile})

        updates = _run(service, {}, {"execution_context": {"mode": "utdemo"}})

        assert updates["tool_ids"] == []


class TestPackageMaterialSources:
    def test_find_package_dir_dual_root(self, env) -> None:
        """用户副本优先；用户缺失回落出厂种子；两侧皆无 = None。"""
        user_pkg = _write_mode_package(env["modes"], "utdemo")
        assert mode_material.find_package_dir("utdemo") == user_pkg
        factory = mode_material.find_package_dir("coding")  # 出厂种子 mode_coding
        assert factory is not None, "用户缺失应回落出厂种子"
        assert factory.name == "mode_coding"
        assert mode_material.find_package_dir("utabsent") is None

    def test_find_package_dir_user_layer_unavailable_falls_to_factory(
        self, env, monkeypatch
    ) -> None:
        """用户插件层解析失败（user_space 不可得）→ 仅出厂根，不抛出。"""
        import sys
        import types

        def _boom() -> None:
            raise RuntimeError("user layer down")

        broken = types.ModuleType("user_space")
        broken.user_plugins_dir = _boom
        monkeypatch.setitem(sys.modules, "user_space", broken)
        pkg = mode_material.find_package_dir("coding")
        assert pkg is not None, "用户层不可得应仍能回落出厂种子"
        assert pkg.name == "mode_coding"

    def test_bare_package_without_convention_dirs(self, env) -> None:
        """包存在但无 pipelines//rules/ 约定目录：清单空、口径占位、工具面按 profile。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        bare_pkg = env["modes"] / "mode_utbare"
        bare_pkg.mkdir(parents=True)  # 仅 plugin.json 包标记，无约定子目录
        (bare_pkg / "plugin.json").write_text('{"id": "mode_utbare"}', encoding="utf-8")
        service = FakeProfileService({"utbare": dict(_UTPLAIN_PROFILE, mode="utbare", name="素包模式")})

        updates = _run(service, {}, {"execution_context": {"mode": "utbare"}})

        prompt = updates["context.system_prompt"]
        assert "## 模式物料（mode=utbare）" in prompt
        assert "暂未携带专属编排" in prompt
        assert "暂未携带口径规则" in prompt
        assert updates["tool_ids"] == ["file_read"]

    def test_read_rules_skips_unreadable_file(self, env) -> None:
        """rules/ 下不可读项（目录冒名 .md）跳过不中断，可读项照常拼接。"""
        pkg = _write_mode_package(
            env["modes"], "utdemo", rules={"口径.md": "有效口径。"}
        )
        (pkg / "rules" / "broken.md").mkdir()  # 目录冒名 .md → read_text 抛 OSError
        text = mode_material.read_rules_text(pkg)
        assert text == "有效口径。"

    def test_tool_scope_non_list_tool_ids_treated_as_unrefined(self) -> None:
        """material_scope.tool_ids 形态不符（非列表）= 视同未细化，不收窄。"""
        profile = {"material_scope": {"tool_ids": "file_read"}}
        note, narrowed = mode_material.tool_surface_note(profile, ["file_read"])
        assert narrowed is None
        assert "未细化" in note

    def test_read_orchestrations_parses_and_skips_broken(self, env) -> None:
        """编排清单：键=mode_X/<stem>、task_kinds 缺省空表；坏文件跳过不中断。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            pipelines={
                "a_flow": "task_kinds: [调研, 报告]\nloop_bodies: []\n",
                "b_flow": "loop_bodies: []\n",
                "c_flow": "task_kinds: [截断\n",
            },
        )
        pkg = mode_material.find_package_dir("utdemo")
        orch = mode_material.read_orchestrations(pkg, "utdemo")
        assert [o["key"] for o in orch] == [
            "mode_utdemo/a_flow",
            "mode_utdemo/b_flow",
        ], "坏文件跳过，好文件按名序出清单"
        assert orch[0]["task_kinds"] == ["调研", "报告"]
        assert orch[1]["task_kinds"] == []

    def test_unwrap_mode_profile_envelope_shapes(self) -> None:
        """信封两形态都容忍（{"data": {...}} / 本体）；无效形态抛 ValueError。"""
        profile = {"mode": "utdemo", "name": "x"}
        assert mode_material.unwrap_mode_profile({"data": profile}) == profile
        assert mode_material.unwrap_mode_profile(profile) == profile
        with pytest.raises(ValueError, match="无效 profile"):
            mode_material.unwrap_mode_profile({"unexpected": 1})
        with pytest.raises(ValueError, match="无效 profile"):
            mode_material.unwrap_mode_profile("garbage")


class TestModeStateKeyEcho:
    """state 顶层 mode 键回写（BUG-34 生产者缺失）。

    消费端契约 = GET /api/v1/pipelines/state 摘要的 mode 出口：前端模式面板
    自动弹出（modePanelAutoOpen）与管道视图模式徽标同源取数，无键零动作。
    回写走既有通道：input 插件 state_updates 平键合并（与 tool_ids/model_tier
    同形），出口可见性由 manifest export_fields/persistent_fields 声明。
    """

    @pytest.mark.parametrize("mode", ["utdemo", "utplain"])
    def test_explicit_mode_writes_top_level_key(self, env, mode: str) -> None:
        """显式模式 → state_updates 出现顶层 mode 键，值 = resolve_mode 结果。"""
        _write_main_agent(env["agents"], tool_ids=["file_read"])
        service = FakeProfileService({mode: dict(_UTPLAIN_PROFILE, mode=mode)})
        state = {"execution_context": {"mode": mode}, "session_id": "s1"}

        updates = _run(service, {}, state)

        assert updates["mode"] == mode, f"顶层 mode 键须回写解析结果，实际: {updates.get('mode')!r}"
        # 性质断言：回写值与 resolve_mode 同源（回写即解析回声，非独立常量）
        assert updates["mode"] == mode_material.resolve_mode(state)

    def test_mode_value_is_resolved_normal_form(self, env) -> None:
        """回写值 = resolve_mode 归一形态（首尾空白剥离），非原文透传。"""
        _write_main_agent(env["agents"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE}, envelope=True)

        updates = _run(service, {}, {"execution_context": {"mode": "  utdemo  "}})

        assert updates["mode"] == "utdemo"

    @pytest.mark.parametrize(
        "state",
        [
            {"session_id": "s1"},  # 无 execution_context（自动模式：前端不带键）
            {"execution_context": {"other": 1}},  # 上下文无 mode 键
            {"execution_context": {"mode": ""}},  # 空串 = 解析不出
        ],
    )
    def test_no_mode_writes_no_key(self, env, state: dict[str, Any]) -> None:
        """自动/无模式 → 不写 mode 键（前端无键零动作，不造默认值）。"""
        _write_main_agent(env["agents"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {}, state)

        assert "mode" not in updates, f"无模式不得写 mode 键，实际: {updates.get('mode')!r}"

    def test_malformed_mode_writes_no_key(self, env) -> None:
        """形态非法（MODE_ID_RE 不放行）不出口：垃圾值不进观测面。"""
        _write_main_agent(env["agents"])
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {}, {"execution_context": {"mode": "Coding!"}})

        assert "mode" not in updates

    def test_rerun_with_new_mode_overwrites_value(self, env) -> None:
        """同管道重复运行：新解析值覆盖旧值（逐轮回写，不先写先赢）。"""
        _write_main_agent(env["agents"])
        service = FakeProfileService(
            {"utdemo": _UTDEMO_PROFILE, "utplain": _UTPLAIN_PROFILE}
        )
        state = {"execution_context": {"mode": "utdemo"}, "session_id": "s1"}
        first = _run(service, {}, state)
        assert first["mode"] == "utdemo"

        merged = {**state, **first}
        rerun_state = {**merged, "execution_context": {"mode": "utplain"}}
        second = _run(service, {}, rerun_state)

        assert second["mode"] == "utplain", "重跑须按新解析值覆盖"
        assert {**merged, **second}["mode"] == "utplain", "合并后 state.mode 为新值"

    def test_key_survives_profile_fetch_degradation(self, env) -> None:
        """物料注入降级（取数失败）不回滚回写：mode 是解析路径产物，
        面板自动弹出不得随模式包故障丢失。"""
        _write_main_agent(env["agents"])
        service = FakeProfileService(error=RuntimeError("mode sidecar down"))

        updates = _run(service, {}, {"execution_context": {"mode": "utdemo"}})

        assert updates["mode"] == "utdemo"
        assert "模式物料" not in updates["context.system_prompt"], "注入仍降级"


# ── project_roots 注入（ADR 2026-09-17 决策 4：项目范围声明）──────────


class TestProjectRootsInjection:
    def test_anchored_run_injects_project_roots(self, env, monkeypatch) -> None:
        """挂靠项目（task.parent_project_id）→ state.project_roots = [项目根]。"""
        import project_registry

        monkeypatch.setattr(
            project_registry,
            "load_project_paths",
            lambda: {"proj00000001": "D:/x/proj_a"},
        )
        updates = _run(
            FakeProfileService(),
            {"agent_level": "L3"},
            {
                "agent.id": "executor/general_agent",
                "lineage.parent_pipeline_id": "p1",
                "task.parent_project_id": "proj00000001",
            },
        )
        assert updates["project_roots"] == ["D:/x/proj_a"]

    def test_unregistered_project_degrades_without_injection(self, env, monkeypatch) -> None:
        """project_id 不在登记 → 降级不注入（warning，不阻断管道）。"""
        import project_registry

        monkeypatch.setattr(project_registry, "load_project_paths", lambda: {})
        updates = _run(
            FakeProfileService(),
            {"agent_level": "L3"},
            {
                "agent.id": "executor/general_agent",
                "lineage.parent_pipeline_id": "p1",
                "task.parent_project_id": "ghost0000001",
            },
        )
        assert "project_roots" not in updates

    def test_no_project_key_is_zero_injection(self, env) -> None:
        """无挂靠键（聊天管道/独立任务）→ 零注入。"""
        updates = _run(
            FakeProfileService(),
            {"agent_level": "L1"},
            {"agent.id": "", "user_input": "hi"},
        )
        assert "project_roots" not in updates
