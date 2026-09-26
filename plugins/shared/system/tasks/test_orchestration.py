# @feature: FP-0.2.〇 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P2 编排运行面 | @ci: python-coverage
"""orchestration.py 行为测试（编排键三级解析 / 派生输入集 / 完备性校验）。

断输入→输出与结构化错误形状，不钉内部实现；关键路径走真实依赖
（真实 config/pipelines/autonomous.yaml 与真实管道 manifest 声明）。

覆盖契约（docs/working/模式体系落地设计_20260915.md §3.3/§3.5/§4.5/§十一）：
- 发现：系统键（stem）/ 模式键（mode_X/stem）/ 用户根覆盖（用户赢）/
  坏 yaml 跳过留痕；
- 三级解析：① 显式键命中（系统键、模式键两形态）与未命中 fail-closed
  （不落③，含可用编排提示）；② mode 限定候选（单候选选定 / task_kinds
  匹配优先 / 意图不明落③ / mode 无编排落③——约束非门槛）；③ 兜底
  autonomous；
- 派生输入集 = 编排引用 step 集合并集其 required_state_inputs；完备性
  校验过/不过（区分度输入：全齐 / 缺一 / 缺多）；
- H3 两类错误结构：ORCHESTRATION_KEY_NOT_FOUND /
  INCOMPLETE_INITIAL_INPUTS 均带可编程结构化字段。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLUGIN_DIR.parents[3]

_EVICT_NAMES = ("orchestration", "user_space")


@pytest.fixture(autouse=True)
def _isolate_orchestration_modules():
    """裸名逐出 + 代际还原（同 test_service_crud.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, Any] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


def _write_pipeline(directory: Path, stem: str, raw: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


_AUTONOMOUS_RAW: dict[str, Any] = {
    "name": "autonomous",
    "loop_bodies": [
        {
            "id": "main",
            "steps": [
                {
                    "id": "prepare",
                    "steps": [
                        "pipeline_context_build",
                        {"name": "pipeline_context_window_guard", "when": "x > 1"},
                    ],
                }
            ],
        }
    ],
    "initial_state": {"core_plugin": "pipeline_llm_core"},
}


# ── 发现 ──


class TestDiscover:
    def test_system_and_mode_keys_and_user_wins(self, tmp_path: Path) -> None:
        """系统键=stem、模式键=mode_X/stem；用户根同键覆盖 factory（用户赢）。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "autonomous", _AUTONOMOUS_RAW)
        factory_modes = tmp_path / "modes"
        _write_pipeline(factory_modes / "mode_writing" / "pipelines", "chapter", {})
        user_modes = tmp_path / "user_modes"
        _write_pipeline(
            user_modes / "mode_writing" / "pipelines",
            "chapter",
            {"name": "user-override"},
        )
        _write_pipeline(user_modes / "mode_research" / "pipelines", "report", {})

        got = discover_orchestrations(
            pipelines_dir=pipelines,
            modes_root=factory_modes,
            user_modes_root=user_modes,
        )
        assert set(got) == {"autonomous", "mode_writing/chapter", "mode_research/report"}
        # 用户副本覆盖 factory（同 id 用户赢）
        assert got["mode_writing/chapter"].raw == {"name": "user-override"}
        assert got["autonomous"].key == "autonomous"

    def test_corrupt_yaml_skipped_with_rest_intact(self, tmp_path: Path) -> None:
        """坏 yaml 跳过（留 warning），其余编排仍可发现——发现面聚合降级口径。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "good", _AUTONOMOUS_RAW)
        pipelines.mkdir(parents=True, exist_ok=True)
        (pipelines / "broken.yaml").write_text("loop_bodies: [unclosed", encoding="utf-8")

        got = discover_orchestrations(
            pipelines_dir=pipelines, modes_root=tmp_path / "absent",
            user_modes_root=tmp_path / "user_absent"  # 显式置空：禁读宿主/仓库真实用户空间（机器状态敏感）
        )
        assert set(got) == {"good"}

    def test_non_mapping_yaml_skipped(self, tmp_path: Path) -> None:
        """yaml 可解析但非映射（列表等）→ 跳过，不产生非法定义。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "good", _AUTONOMOUS_RAW)
        (pipelines / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")

        got = discover_orchestrations(
            pipelines_dir=pipelines, modes_root=tmp_path / "absent",
            user_modes_root=tmp_path / "user_absent"  # 显式置空：禁读宿主/仓库真实用户空间（机器状态敏感）
        )
        assert set(got) == {"good"}

    def test_task_kinds_header_parsed(self, tmp_path: Path) -> None:
        """文件头 task_kinds 标注进定义（非字符串项过滤）。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(
            pipelines,
            "kinds",
            {"task_kinds": ["review", "translation", 42, None]},
        )
        got = discover_orchestrations(
            pipelines_dir=pipelines, modes_root=tmp_path / "absent",
            user_modes_root=tmp_path / "user_absent"  # 显式置空：禁读宿主/仓库真实用户空间（机器状态敏感）
        )
        assert got["kinds"].task_kinds == ("review", "translation")


# ── 三级解析 ──


def _definitions(**raws: dict[str, Any]) -> dict[str, Any]:
    from orchestration import OrchestrationDefinition

    return {
        key: OrchestrationDefinition(
            key=key, path=f"<{key}>", raw=raw, task_kinds=tuple(raw.get("task_kinds", ()))
        )
        for key, raw in raws.items()
    }


class TestResolveExplicit:
    def test_explicit_hit_system_and_mode_keys(self) -> None:
        """① 显式键命中两形态：系统裸键与 mode_X/<编排名> 全限定键。"""
        from orchestration import resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        assert resolve_orchestration(
            explicit_key="autonomous", orchestrations=defs
        ).key == "autonomous"
        assert resolve_orchestration(
            explicit_key="mode_writing/chapter", orchestrations=defs
        ).key == "mode_writing/chapter"

    def test_explicit_miss_fail_closed_no_fallback(self) -> None:
        """① 显式键未命中 = fail-closed 结构化报错，禁止静默落③（H3①）。"""
        from orchestration import (
            OrchestrationKeyNotFoundError,
            resolve_orchestration,
        )

        defs = _definitions(autonomous={"name": "auto"})
        with pytest.raises(OrchestrationKeyNotFoundError) as exc_info:
            resolve_orchestration(
                explicit_key="mode_writing/chapter", orchestrations=defs
            )
        err = exc_info.value
        assert err.error_code == "ORCHESTRATION_KEY_NOT_FOUND"
        assert err.fields["orchestration_key"] == "mode_writing/chapter"
        # 可用编排提示在错误里（键拼错/插件禁用可自诊）
        assert err.fields["available_orchestrations"] == ["autonomous"]
        assert "autonomous" in str(err)


class TestResolveMode:
    def test_single_candidate_selected_without_kind(self) -> None:
        """② mode 包单编排：无 task_kind 也选定（唯一候选=意图明确）。"""
        from orchestration import resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        got = resolve_orchestration(mode_key="mode_writing", orchestrations=defs)
        assert got.key == "mode_writing/chapter"

    def test_task_kinds_match_wins_over_candidates(self) -> None:
        """② 多候选：task_kinds 与任务 kind 唯一命中者优先。"""
        from orchestration import resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{
                "mode_writing/chapter": {"task_kinds": ["chapter"]},
                "mode_writing/review": {"task_kinds": ["review"]},
            },
        )
        got = resolve_orchestration(
            mode_key="mode_writing", task_kind="review", orchestrations=defs
        )
        assert got.key == "mode_writing/review"

    def test_ambiguous_candidates_fall_to_autonomous(self) -> None:
        """② 多候选且无 task_kinds 可区分 = 意图不明 → 落③（H4：③只接意图不明）。"""
        from orchestration import AUTONOMOUS_ORCHESTRATION_KEY, resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{
                "mode_writing/chapter": {},
                "mode_writing/review": {},
            },
        )
        got = resolve_orchestration(mode_key="mode_writing", orchestrations=defs)
        assert got.key == AUTONOMOUS_ORCHESTRATION_KEY

    def test_mode_without_pipelines_falls_to_autonomous(self) -> None:
        """② mode 键无自有编排 → 落③：mode 是约束非门槛（H4），共享默认仍有效。"""
        from orchestration import AUTONOMOUS_ORCHESTRATION_KEY, resolve_orchestration

        defs = _definitions(autonomous={"name": "auto"})
        got = resolve_orchestration(mode_key="mode_coding", orchestrations=defs)
        assert got.key == AUTONOMOUS_ORCHESTRATION_KEY


class TestResolveFallback:
    def test_no_keys_goes_autonomous(self) -> None:
        """③ 无显式键无 mode 键（旧调用形态）→ autonomous，行为不变。"""
        from orchestration import resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        assert resolve_orchestration(orchestrations=defs).key == "autonomous"

    def test_missing_autonomous_is_error_not_invention(self) -> None:
        """兜底键缺失（共享默认管道缺失）= 结构化报错，不静默造兜底。"""
        from orchestration import (
            OrchestrationKeyNotFoundError,
            resolve_orchestration,
        )

        defs = _definitions(**{"mode_writing/chapter": {}})
        with pytest.raises(OrchestrationKeyNotFoundError):
            resolve_orchestration(orchestrations=defs)


# ── step 引用抽取 / 派生输入集 / 完备性 ──


class TestStepRefsAndCompleteness:
    def test_real_autonomous_refs_and_real_declarations(self) -> None:
        """真依赖：真实 autonomous.yaml 引用抽取 + 真实管道 manifest 声明索引。

        锁首批 H2 声明（context_build/tool_schema/prompt_build/llm_core）与
        动态核心键（initial_state / post 路由 set）的引用抽取。
        """
        from orchestration import (
            OrchestrationDefinition,
            derived_input_set,
            iter_step_refs,
            load_step_required_inputs,
        )

        raw = yaml.safe_load(
            (_REPO_ROOT / "config" / "pipelines" / "autonomous.yaml").read_text(
                encoding="utf-8"
            )
        )
        refs = iter_step_refs(raw)
        assert "pipeline_context_build" in refs
        assert "pipeline_context_window_guard" in refs  # 条件步骤 name 形态
        assert "pipeline_llm_core" in refs  # initial_state + post 路由 set
        assert "pipeline_tool_core" in refs

        step_required = load_step_required_inputs()
        assert step_required["pipeline_context_build"] == ["agent.id"]
        assert step_required["pipeline_tool_schema"] == ["tool_ids"]
        assert step_required["pipeline_prompt_build"] == ["context.system_prompt"]
        assert step_required["pipeline_llm_core"] == ["messages"]
        # 显式空声明入索引（与未声明区分）
        assert step_required["pipeline_model_prompt_adapter"] == []

        definition = OrchestrationDefinition(
            key="autonomous", path="<repo>", raw=raw
        )
        derived = derived_input_set(definition, step_required)
        assert {
            "agent.id",
            "tool_ids",
            "context.system_prompt",
            "messages",
        } <= derived

    def test_completeness_pass_and_fail(self) -> None:
        """完备性：过（空清单）/ 不过（排序缺失清单）；缺一与缺多两组区分度输入。"""
        from orchestration import (
            OrchestrationDefinition,
            check_completeness,
        )

        definition = OrchestrationDefinition(
            key="autonomous", path="<repo>", raw=_AUTONOMOUS_RAW
        )
        step_required = {
            "pipeline_context_build": ["agent.id", "tool_ids"],
            "pipeline_context_window_guard": [],
            "pipeline_llm_core": ["messages", "context.system_prompt"],
        }
        full = {"agent.id", "tool_ids", "messages", "context.system_prompt", "task.goal"}
        assert check_completeness(full, definition, step_required) == []
        assert check_completeness(
            full - {"tool_ids"}, definition, step_required
        ) == ["tool_ids"]
        assert check_completeness(set(), definition, step_required) == [
            "agent.id",
            "context.system_prompt",
            "messages",
            "tool_ids",
        ]

    def test_undeclared_steps_contribute_nothing(self) -> None:
        """未声明 required_state_inputs 的 step 对派生集贡献空（不臆测）。"""
        from orchestration import (
            OrchestrationDefinition,
            derived_input_set,
        )

        definition = OrchestrationDefinition(
            key="x", path="<x>", raw={"loop_bodies": [{"steps": ["pipeline_unknown"]}]}
        )
        assert derived_input_set(definition, {"pipeline_other": ["k"]}) == frozenset()


class TestStepDeclarationIndex:
    """load_step_required_inputs 的发现面防御分支（坏 manifest/形态漂移）。"""

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        """索引根不存在（缺省根缺失等）→ 空索引，不抛不臆测。"""
        from orchestration import load_step_required_inputs

        assert load_step_required_inputs(tmp_path / "absent") == {}

    def test_corrupt_manifest_skipped_with_rest_intact(self, tmp_path: Path) -> None:
        """坏 manifest（非法 JSON/非映射）→ 跳过留痕，其余照常入索引。"""
        from orchestration import load_step_required_inputs

        root = tmp_path / "pipeline"
        good = root / "good"
        good.mkdir(parents=True)
        (good / "plugin.json").write_text(
            '{"id":"p_good","capabilities":{"steps":[{"name":"s_good",'
            '"required_state_inputs":["k1"]}]}}',
            encoding="utf-8",
        )
        bad = root / "bad"
        bad.mkdir(parents=True)
        (bad / "plugin.json").write_text('{"id": broken', encoding="utf-8")
        nonmap = root / "nonmap"
        nonmap.mkdir(parents=True)
        (nonmap / "plugin.json").write_text("[]", encoding="utf-8")

        got = load_step_required_inputs(root)
        assert got == {"s_good": ["k1"]}

    def test_malformed_step_entries_skipped(self, tmp_path: Path) -> None:
        """steps 条目形态漂移（非映射/required 非 list/空 name）→ 逐条跳过。"""
        from orchestration import load_step_required_inputs

        root = tmp_path / "pipeline"
        d = root / "p"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(
            '{"id":"p","capabilities":{"steps":['
            '"not-a-dict",'
            '{"name":"s_nolist","required_state_inputs":"k"},'
            '{"name":"","required_state_inputs":["k"]},'
            '{"name":"s_ok","required_state_inputs":["k1","k2"]}'
            ']}}',
            encoding="utf-8",
        )
        got = load_step_required_inputs(root)
        assert got == {"s_ok": ["k1", "k2"]}

    def test_conflicting_declarations_later_scan_wins(self, tmp_path: Path) -> None:
        """同 step 名冲突声明（跨包异常形态）→ 后扫描者覆盖并留 warning 痕迹。"""
        from orchestration import load_step_required_inputs

        root = tmp_path / "pipeline"
        for sub, keys in (("a", ["k1"]), ("b", ["k2"])):
            d = root / sub
            d.mkdir(parents=True)
            manifest = {
                "id": f"p_{sub}",
                "capabilities": {
                    "steps": [
                        {
                            "name": "s_dup",
                            "required_state_inputs": [keys[0]],
                        }
                    ]
                },
            }
            (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        got = load_step_required_inputs(root)
        assert got["s_dup"] == ["k2"]

    def test_user_modes_root_import_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user_space 不可导入（裸插件环境）→ 用户模式根 None，factory 仍可用。"""
        import orchestration

        monkeypatch.setitem(sys.modules, "user_space", None)
        assert orchestration._user_modes_root() is None


class TestErrorShapes:
    def test_incomplete_error_structured_fields(self) -> None:
        """M2 结构：{missing_fields, orchestration_key, suggestion} 可编程消费。"""
        from orchestration import incomplete_error

        definition = _definitions(autonomous={})["autonomous"]
        err = incomplete_error(definition, ["tool_ids", "agent.id"])
        assert err.error_code == "INCOMPLETE_INITIAL_INPUTS"
        assert err.fields["orchestration_key"] == "autonomous"
        assert err.fields["missing_fields"] == ["tool_ids", "agent.id"]
        assert err.fields["suggestion"]
        assert "tool_ids" in err.message
