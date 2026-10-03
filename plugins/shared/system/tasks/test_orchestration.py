# @feature: FP-0.2.〇 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P2 编排运行面 | @ci: python-coverage
"""orchestration.py 行为测试（编排键两级解析 / 派生输入集 / 完备性校验）。

断输入→输出与结构化错误形状，不钉内部实现；关键路径走真实依赖
（真实 config/pipelines/autonomous.yaml 与真实管道 manifest 声明）。

覆盖契约（docs/working/模式包工作模式设计_20260928.md D2/一管一配置）：
- 发现：唯一来源 config/pipelines 登记处（系统键 = stem）/ 坏 yaml 跳过
  留痕；包内 pipelines/ 编排清单退役不扫描（回归）；
- 两级解析：① 显式键命中与未命中 fail-closed（不落②，含可用编排提示）；
  ② 兜底 autonomous；mode 键不再限定编排候选（回归，D6 模式控制与管道
  选择解耦）；
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

_EVICT_NAMES = ("orchestration",)


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
                        "pipeline_agent_config_load",
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
    def test_registry_keys_are_stems(self, tmp_path: Path) -> None:
        """系统键 = 登记文件名 stem（一管一配置，config/pipelines 唯一来源）。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "autonomous", _AUTONOMOUS_RAW)
        _write_pipeline(pipelines, "roleplay", {"name": "roleplay"})

        got = discover_orchestrations(pipelines_dir=pipelines)
        assert set(got) == {"autonomous", "roleplay"}
        assert got["autonomous"].key == "autonomous"

    def test_retired_mode_package_pipelines_not_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：包内 pipelines/ 编排清单已退役（D2）——目录存在也不入候选。

        钉死缺省根（repo 根 → tmp）：登记处照常发现，历史退役形态（出厂
        模式包内 pipelines/ 目录）不扫描——重新引入该扫描源即红。
        """
        import orchestration
        from orchestration import discover_orchestrations

        _write_pipeline(
            tmp_path / "config" / "pipelines", "autonomous", _AUTONOMOUS_RAW
        )
        # 历史退役形态：包内编排清单目录（同名异物教训源）
        _write_pipeline(
            tmp_path / "plugins" / "shared" / "modes" / "mode_writing" / "pipelines",
            "chapter",
            {"name": "chapter"},
        )
        monkeypatch.setattr(orchestration, "_repo_root", lambda: tmp_path)

        got = discover_orchestrations()
        assert set(got) == {"autonomous"}, "包内 pipelines/ 不再是扫描源"

    def test_corrupt_yaml_skipped_with_rest_intact(self, tmp_path: Path) -> None:
        """坏 yaml 跳过（留 warning），其余编排仍可发现——发现面聚合降级口径。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "good", _AUTONOMOUS_RAW)
        pipelines.mkdir(parents=True, exist_ok=True)
        (pipelines / "broken.yaml").write_text("loop_bodies: [unclosed", encoding="utf-8")

        got = discover_orchestrations(pipelines_dir=pipelines)
        assert set(got) == {"good"}

    def test_non_mapping_yaml_skipped(self, tmp_path: Path) -> None:
        """yaml 可解析但非映射（列表等）→ 跳过，不产生非法定义。"""
        from orchestration import discover_orchestrations

        pipelines = tmp_path / "pipelines"
        _write_pipeline(pipelines, "good", _AUTONOMOUS_RAW)
        (pipelines / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")

        got = discover_orchestrations(pipelines_dir=pipelines)
        assert set(got) == {"good"}


# ── 两级解析 ──


def _definitions(**raws: dict[str, Any]) -> dict[str, Any]:
    from orchestration import OrchestrationDefinition

    return {
        key: OrchestrationDefinition(key=key, path=f"<{key}>", raw=raw)
        for key, raw in raws.items()
    }


class TestResolveExplicit:
    def test_explicit_hit_registry_keys(self) -> None:
        """① 显式键命中：登记名（stem）直接取用。"""
        from orchestration import resolve_orchestration

        defs = _definitions(autonomous={"name": "auto"}, roleplay={"name": "rp"})
        assert resolve_orchestration(
            explicit_key="autonomous", orchestrations=defs
        ).key == "autonomous"
        assert resolve_orchestration(
            explicit_key="roleplay", orchestrations=defs
        ).key == "roleplay"

    def test_explicit_miss_fail_closed_no_fallback(self) -> None:
        """① 显式键未命中 = fail-closed 结构化报错，禁止静默落②（H3①）。"""
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


class TestModeKeyNoLongerLimits:
    """回归：mode 键不再限定编排候选（包内编排清单退役，D2/D6——模式控制
    经 execution_context.mode 透传给消费面，与管道选择解耦）。"""

    def test_mode_scoped_keys_present_still_falls_to_autonomous(self) -> None:
        """候选集含 mode_ 前缀键（历史形态）且无显式键 → 恒落 autonomous，
        不再按 mode 推断路由。"""
        from orchestration import AUTONOMOUS_ORCHESTRATION_KEY, resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        assert resolve_orchestration(orchestrations=defs).key == AUTONOMOUS_ORCHESTRATION_KEY

    def test_single_mode_scoped_candidate_not_auto_selected(self) -> None:
        """唯一 mode_ 前缀候选也不再自动选定（旧②路单候选语义随 mode 限定退役）。"""
        from orchestration import AUTONOMOUS_ORCHESTRATION_KEY, resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        got = resolve_orchestration(orchestrations=defs)
        assert got.key == AUTONOMOUS_ORCHESTRATION_KEY

    def test_mode_scoped_key_still_usable_when_explicit(self) -> None:
        """显式给到 mode_ 前缀键仍按显式路径命中（解析面对键形不设枚举）。"""
        from orchestration import resolve_orchestration

        defs = _definitions(
            autonomous={"name": "auto"},
            **{"mode_writing/chapter": {"name": "chapter"}},
        )
        assert resolve_orchestration(
            explicit_key="mode_writing/chapter", orchestrations=defs
        ).key == "mode_writing/chapter"


class TestResolveFallback:
    def test_no_key_goes_autonomous(self) -> None:
        """② 无显式键（旧调用形态）→ autonomous，行为不变。"""
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

        defs = _definitions(roleplay={"name": "rp"})
        with pytest.raises(OrchestrationKeyNotFoundError):
            resolve_orchestration(orchestrations=defs)


# ── step 引用抽取 / 派生输入集 / 完备性 ──


class TestStepRefsAndCompleteness:
    def test_real_autonomous_refs_and_real_declarations(self) -> None:
        """真依赖：真实 autonomous.yaml 引用抽取 + 真实管道 manifest 声明索引。

        锁首批 H2 声明（agent_config_load/tool_schema/prompt_build/llm_core）与
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
        assert "pipeline_agent_config_load" in refs
        assert "pipeline_context_window_guard" in refs  # 条件步骤 name 形态
        assert "pipeline_llm_core" in refs  # initial_state + post 路由 set
        assert "pipeline_tool_core" in refs

        step_required = load_step_required_inputs()
        assert step_required["pipeline_agent_config_load"] == ["agent.id"]
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
            "pipeline_agent_config_load": ["agent.id", "tool_ids"],
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

    def test_loop_local_keys_exempt_from_derived_inputs(self) -> None:
        """回归：loop_config.as 声明的迭代键不计入派生输入集。

        并行 for-each（ADR 2026-10-02-loopconfig-parallel-foreach）下引擎每次
        迭代注入 as 键（如 tool_core 的 current_call）；完备性校验若把它当
        初始输入要求，任务派发被 400 永久拒绝（e2e_02 test_13/14 + 任务矩阵
        6 用例实测事故源）。同一 step 在循环外（无 loop_config.as）仍正常
        计入——豁免只对声明体内键。autonomous.yaml 真实形态 = loop_config
        内嵌在带 steps 的 step 字典上（tool_exec 层级）。
        """
        from orchestration import (
            OrchestrationDefinition,
            derived_input_set,
        )

        step_required = {"pipeline_tool_core": ["current_call", "raw_tool_calls"]}
        in_loop = OrchestrationDefinition(
            key="x",
            path="<x>",
            raw={
                "loop_bodies": [
                    {
                        "id": "main",
                        "steps": [
                            {
                                "id": "tool_exec",
                                "loop_config": {"as": "current_call"},
                                "steps": ["pipeline_tool_core"],
                            }
                        ],
                    }
                ]
            },
        )
        # current_call 由迭代注入豁免；raw_tool_calls 非 loop 局部键仍要求。
        assert derived_input_set(in_loop, step_required) == frozenset(
            {"raw_tool_calls"}
        )

        outside = OrchestrationDefinition(
            key="y", path="<y>", raw={"loop_bodies": [{"steps": ["pipeline_tool_core"]}]}
        )
        assert derived_input_set(outside, step_required) == frozenset(
            {"current_call", "raw_tool_calls"}
        )

    def test_nested_loop_locals_inherited(self) -> None:
        """嵌套循环的 as 键向内层继承（外层迭代键在内层 step 同样豁免），
        body 级 loop_config.as（装载器形态）与 step 级（yaml 原文形态）皆支持。"""
        from orchestration import (
            OrchestrationDefinition,
            derived_input_set,
        )

        nested_step_level = OrchestrationDefinition(
            key="x",
            path="<x>",
            raw={
                "loop_bodies": [
                    {
                        "id": "outer",
                        "steps": [
                            {
                                "id": "outer_loop",
                                "loop_config": {"as": "current_call"},
                                "steps": [
                                    {
                                        "id": "inner_loop",
                                        "loop_config": {"as": "iteration_item"},
                                        "steps": ["pipeline_tool_core"],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )
        assert derived_input_set(
            nested_step_level,
            {"pipeline_tool_core": ["current_call", "iteration_item"]},
        ) == frozenset()

        body_level = OrchestrationDefinition(
            key="y",
            path="<y>",
            raw={
                "loop_bodies": [
                    {
                        "id": "outer",
                        "loop_config": {"as": "current_call"},
                        "loop_bodies": [
                            {
                                "id": "inner",
                                "loop_config": {"as": "iteration_item"},
                                "steps": ["pipeline_tool_core"],
                            }
                        ],
                    }
                ]
            },
        )
        assert derived_input_set(
            body_level, {"pipeline_tool_core": ["current_call", "iteration_item"]}
        ) == frozenset()


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
