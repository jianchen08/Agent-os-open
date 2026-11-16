# @feature: FP-GATE-META 规范执法档位注册表 | @ci: python-coverage
"""规范执法档位检查器回归（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 4）。

"规范都变成 CI"的制度化：每条规范立项必须标三档执法位之一
（mechanical 机械闸 / metric 评估指标 / review 审查清单），无档位不入库。
注册表（enforcement_tiers.yaml）+ 基线棘轮（存量触碰即补）+ 防悬空引用
（gate id / 指标 id / 清单路径必须真实存在——CI 也是 CI 的被检对象）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_rule_enforcement_tier.py"
_spec = importlib.util.spec_from_file_location("check_rule_enforcement_tier", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_rule_enforcement_tier", _mod)
_spec.loader.exec_module(_mod)

validate_registry = _mod.validate_registry
check_coverage = _mod.check_coverage
load_metric_ids = _mod.load_metric_ids

_ROOT = Path(__file__).resolve().parents[2]


def _rule(path: str = "config/rules/a.md", tier: str = "review", **kw: object) -> dict:
    d: dict = {"path": path, "tier": tier}
    d.update(kw)
    return d


class TestValidateRegistry:
    """注册表校验：档位合法性 + 三档语义 + 防悬空引用。"""

    def test_valid_review_entry_passes(self) -> None:
        assert validate_registry([_rule(tier="review")], {"g"}, {"m"}, _ROOT) == []

    def test_valid_mechanical_and_mixed_pass(self) -> None:
        entries = [
            _rule(path="config/rules/m.md", tier="mechanical", gates=["graph-check"]),
            _rule(
                path="config/rules/x.md",
                tier="mixed",
                gates=["graph-check"],
                metrics=["antipattern_scan"],
            ),
        ]
        assert validate_registry(entries, {"graph-check"}, {"antipattern_scan"}, _ROOT) == []

    def test_unknown_tier_fails(self) -> None:
        errs = validate_registry([_rule(tier="hope")], set(), set(), _ROOT)
        assert errs and "hope" in errs[0]

    def test_mechanical_requires_gates(self) -> None:
        errs = validate_registry([_rule(tier="mechanical")], set(), set(), _ROOT)
        assert errs and "gates" in errs[0]

    def test_metric_requires_metrics(self) -> None:
        errs = validate_registry([_rule(tier="metric")], set(), set(), _ROOT)
        assert errs and "metrics" in errs[0]

    def test_review_with_gates_is_contradiction(self) -> None:
        errs = validate_registry(
            [_rule(tier="review", gates=["graph-check"])], {"graph-check"}, set(), _ROOT
        )
        assert errs  # review 档却声明机械面 = 声明矛盾

    def test_dangling_gate_id_fails(self) -> None:
        errs = validate_registry(
            [_rule(tier="mechanical", gates=["no-such-gate"])], {"real-gate"}, set(), _ROOT
        )
        assert errs and "no-such-gate" in errs[0]

    def test_dangling_metric_id_fails(self) -> None:
        errs = validate_registry(
            [_rule(tier="metric", metrics=["ghost_metric"])], set(), {"real_metric"}, _ROOT
        )
        assert errs and "ghost_metric" in errs[0]

    def test_missing_review_doc_fails(self) -> None:
        errs = validate_registry(
            [_rule(tier="review", review_docs=["config/rules/不存在.md"])], set(), set(), _ROOT
        )
        assert errs and "不存在.md" in errs[0]

    def test_duplicate_path_fails(self) -> None:
        errs = validate_registry(
            [_rule(), _rule(tier="review")], set(), set(), _ROOT
        )
        assert errs  # 同一路径重复登记


class TestCheckCoverage:
    """覆盖校验：规则目录每文件必须在注册表或基线；基线棘轮（已登记须移出）。"""

    def _mk_rules(self, tmp_path: Path, names: list[str]) -> Path:
        d = tmp_path / "rules"
        d.mkdir()
        for n in names:
            (d / n).write_text("# rule\n", encoding="utf-8")
        return d

    def test_all_registered_passes(self, tmp_path: Path) -> None:
        d = self._mk_rules(tmp_path, ["a.md", "b.md"])
        assert check_coverage(d, {"a.md", "b.md"}, set()) == []

    def test_unregistered_file_not_in_baseline_fails(self, tmp_path: Path) -> None:
        d = self._mk_rules(tmp_path, ["a.md", "new.md"])
        errs = check_coverage(d, {"a.md"}, {"a.md"})
        assert any("new.md" in e for e in errs)

    def test_registered_path_left_in_baseline_fails(self, tmp_path: Path) -> None:
        """棘轮方向：已登记的路径必须从基线移除——基线只减不增。"""
        d = self._mk_rules(tmp_path, ["a.md"])
        errs = check_coverage(d, {"a.md"}, {"a.md", "gone.md"})
        assert any("gone.md" in e for e in errs)


class TestLoadMetricIds:
    def test_extracts_top_level_metric_keys(self) -> None:
        text = (
            "metrics:\n"
            "  artifact_schema_check:\n"
            "    gate_mode: false\n"
            "  antipattern_scan:\n"
            "    gate_mode: false\n"
        )
        assert load_metric_ids(text) == {"artifact_schema_check", "antipattern_scan"}

    def test_live_metrics_file_contains_l0_three(self) -> None:
        """活体：真指标文件含 L0 三指标（真实链路兜底，防解析与真文件脱节）。"""
        text = (_ROOT / "config/plugins/evaluation/evaluation_metrics.yaml").read_text(
            encoding="utf-8"
        )
        ids = load_metric_ids(text)
        assert {"artifact_schema_check", "antipattern_scan", "scope_consistency"}.issubset(ids)


class TestCLI:
    """CLI 集成：tmp 注入面全绿路径 + 悬空引用红 + 活体（主仓默认参数）。"""

    def _setup_tmp(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "my_rule.md").write_text("# r\n", encoding="utf-8")
        registry = tmp_path / "tiers.yaml"
        registry.write_text(
            "rules:\n"
            "  - path: my_rule.md\n"
            "    tier: mechanical\n"
            "    gates: [graph-check]\n"
            "    note: 示例\n",
            encoding="utf-8",
        )
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("", encoding="utf-8")
        metrics = tmp_path / "metrics.yaml"
        metrics.write_text("metrics:\n  antipattern_scan:\n    gate_mode: false\n", encoding="utf-8")
        return rules, registry, baseline, metrics

    def test_green_path_exit_zero(self, tmp_path: Path) -> None:
        rules, registry, baseline, metrics = self._setup_tmp(tmp_path)
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--rules-dir",
                str(rules),
                "--registry",
                str(registry),
                "--baseline",
                str(baseline),
                "--metrics-file",
                str(metrics),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr

    def test_dangling_gate_ref_fails(self, tmp_path: Path) -> None:
        rules, registry, baseline, metrics = self._setup_tmp(tmp_path)
        registry.write_text(
            "rules:\n"
            "  - path: my_rule.md\n"
            "    tier: mechanical\n"
            "    gates: [no-such-gate-xyz]\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--rules-dir",
                str(rules),
                "--registry",
                str(registry),
                "--baseline",
                str(baseline),
                "--metrics-file",
                str(metrics),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "no-such-gate-xyz" in proc.stderr + proc.stdout

    def test_unregistered_new_rule_fails(self, tmp_path: Path) -> None:
        rules, registry, baseline, metrics = self._setup_tmp(tmp_path)
        (rules / "late_addition.md").write_text("# r\n", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--rules-dir",
                str(rules),
                "--registry",
                str(registry),
                "--baseline",
                str(baseline),
                "--metrics-file",
                str(metrics),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "late_addition.md" in proc.stderr + proc.stdout

    def test_live_repo_passes(self) -> None:
        """活体：主仓默认参数全绿（本刀落地后的自举验证——制度必须管住自己）。"""
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
