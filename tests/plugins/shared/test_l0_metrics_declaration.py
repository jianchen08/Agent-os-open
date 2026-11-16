# @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: python-coverage
"""L0 指标声明面防漂移测试（ADR 2026-10-27 步骤 2：L0 指标化观察模式）。

意图：mode 声明面 `ci:` 段引用的指标 id 与 evaluation_metrics.yaml 定义之间
不允许人工维护漂移——引用 id 必须真实可执行（定义在位），观察模式语义
（gate_mode=false）必须有显式声明锚，matches_schema 枚举必须与执行器能力
同步声明，antipattern_scan 观察期参考命令必须在位（试点任务可定制但缺省
形态不许缺席）。

本文件测声明一致性（配置即契约），不测执行语义——执行语义见
plugins/shared/task_evaluate/test_l0_executor.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_METRICS_YAML = _REPO_ROOT / "config" / "plugins" / "evaluation" / "evaluation_metrics.yaml"
_MODE_YAML = _REPO_ROOT / "plugins" / "shared" / "mode_coding" / "mode.yaml"

L0_METRIC_IDS = ["artifact_schema_check", "antipattern_scan", "scope_consistency"]


def _load_metrics() -> dict[str, dict]:
    """evaluation_metrics.yaml 解析为 name → 定义。"""
    data = yaml.safe_load(_METRICS_YAML.read_text(encoding="utf-8")) or {}
    return {
        m["name"]: m
        for m in data.get("metrics", [])
        if isinstance(m, dict) and m.get("name")
    }


def _load_mode_ci() -> dict:
    """mode_coding/mode.yaml 的 ci: 段。"""
    data = yaml.safe_load(_MODE_YAML.read_text(encoding="utf-8")) or {}
    return data.get("ci") or {}


class TestL0MetricsDeclaration:
    def test_all_l0_metrics_defined_as_tool_type(self) -> None:
        # 三条 L0 指标必须定义且为 tool 型——观察模式误报率数据来自真实本地执行
        metrics = _load_metrics()
        for mid in L0_METRIC_IDS:
            assert mid in metrics, f"L0 指标 {mid} 未定义于 evaluation_metrics.yaml"
            assert metrics[mid].get("evaluator_type") == "tool", (
                f"L0 指标 {mid} 必须 tool 型（本地可执行）"
            )

    def test_artifact_schema_check_declares_matches_schema(self) -> None:
        # 声明面 check 枚举必须含 matches_schema（与执行器能力同步，防声明/执行漂移）
        metrics = _load_metrics()
        props = (metrics["artifact_schema_check"].get("input_schema") or {}).get("properties") or {}
        enum = (props.get("check") or {}).get("enum") or []
        assert "matches_schema" in enum, (
            "artifact_schema_check 的 check 枚举缺 matches_schema（执行器已支持该检查类型）"
        )

    def test_antipattern_scan_has_reference_command(self) -> None:
        # 观察期参考命令必须在位（default_config.command）；试点任务可按语境定制
        metrics = _load_metrics()
        cmd = (
            (metrics["antipattern_scan"].get("default_config") or {}).get("command") or ""
        ).strip()
        assert cmd, "antipattern_scan 缺观察期参考扫描命令（default_config.command）"

    def test_mode_ci_refs_all_defined(self) -> None:
        # ci.base 引用的每个 id 必须真实定义（防悬空引用漂移）
        metrics = _load_metrics()
        refs = _load_mode_ci().get("base") or []
        assert refs, "mode_coding ci.base 为空（评估清单未启用）"
        for mid in refs:
            assert mid in metrics, f"ci.base 引用 {mid} 未定义于 evaluation_metrics.yaml"

    def test_mode_ci_includes_all_l0_metrics(self) -> None:
        # 启用完整性：三条 L0 全部进 ci.base（ADR 口径：三条 L0 指标观察模式启用中）
        refs = set(_load_mode_ci().get("base") or [])
        missing = set(L0_METRIC_IDS) - refs
        assert not missing, f"ci.base 缺 L0 指标: {sorted(missing)}"

    def test_mode_ci_observation_mode(self) -> None:
        # 观察模式语义锚：gate_mode 必须显式 False——缺省/缺失将被视为未启用观察模式
        assert _load_mode_ci().get("gate_mode") is False, (
            "ci.gate_mode 必须显式 false（观察模式：只记录不阻塞，收误报率）"
        )
