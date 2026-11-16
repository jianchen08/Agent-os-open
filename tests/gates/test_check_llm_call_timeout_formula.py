# @feature: FP-GATE L3 复合界公式化校验（ADR 2026-11-05-timeout-taxonomy-and-merge） | @ci: python-coverage
"""check_llm_call_timeout_formula.py 单测（设计真值
docs/working/超时体系分类设计_20261105.md §4.2 L3 / §5.3）。

行为契约（断输入→输出，不钉实现）：
- 公式数学：跳数 = 1 主尝试 + 兜底链长；required = 跳数 × max(B1,B2,B3)
  + 跳数 × B5 + 余量；多档链取最大需求档；
- 违规面：声明 < required → 违规逐条可辨（含公式分解）；
  缺声明（M2 单声明键缺席）→ 违规；
- M2 倒挂守卫：mcp.request_timeout_secs < call_timeout_seconds → 违规
  （llm_core 900<1800 历史倒挂的回归面）；单声明形态（键缺席）绿；
- 负控制（防恒绿）：真实仓文件子进程实跑 exit 0（现配置
  call_timeout_seconds=3780 ≥ 2×1800+2×60+60=3780 边界合规基线）；
  真实文件改坏（tmp 副本声明调低）→ exit 1。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = SCRIPTS / "check_llm_call_timeout_formula.py"
REAL_MANIFEST = REPO_ROOT / "plugins" / "shared" / "llm_core" / "plugin.json"
REAL_LLM_YAML = REPO_ROOT / "config" / "plugins" / "llm" / "llm.yaml"

sys.path.insert(0, str(SCRIPTS))

from check_llm_call_timeout_formula import FormulaInputs, required_call_timeout  # noqa: E402


def inputs(**overrides) -> FormulaInputs:
    """现仓形态基线输入（2 跳 × 1800 + 2 × 60 + 60 = 3780）。"""
    base = dict(
        fallback_chain={"chat": ["glm-5.3-flash"]},
        stream_idle_timeout=180.0,
        call_timeout=1800.0,
        first_token_timeout=120.0,
        slot_wait_secs=60.0,
        margin_secs=60.0,
        manifest_call_timeout_seconds=3780.0,
        manifest_request_timeout_secs=None,
    )
    base.update(overrides)
    return FormulaInputs(**base)


class TestFormulaMath:
    def test_two_hop_boundary_exact(self):
        verdict = required_call_timeout(inputs())
        assert verdict.hops == 2
        assert verdict.per_hop_bound == 1800.0
        assert verdict.required == 3780.0
        assert verdict.violations == []

    def test_below_required_reports_with_breakdown(self):
        verdict = required_call_timeout(inputs(manifest_call_timeout_seconds=1800.0))
        assert verdict.violations, "1800 < 3780 必须违规（历史 R1 止血值即此形态）"
        assert any("3780" in v for v in verdict.violations)
        assert any("1800" in v for v in verdict.violations)

    def test_missing_declaration_violates(self):
        verdict = required_call_timeout(inputs(manifest_call_timeout_seconds=None))
        assert any("缺 capabilities.call_timeout_seconds" in v for v in verdict.violations)

    def test_longer_chain_raises_required(self):
        # 链长 2 → 3 跳：required = 3×1800+3×60+60 = 5580；声明 3780 不够即红
        # ——「改兜底链长度门禁提醒同步重算 B8」的执法形态。
        verdict = required_call_timeout(
            inputs(
                fallback_chain={"chat": ["a", "b"]},
                manifest_call_timeout_seconds=3780.0,
            )
        )
        assert verdict.hops == 3
        assert verdict.required == 5640.0
        assert verdict.violations

    def test_multi_tier_takes_max_demand(self):
        verdict = required_call_timeout(
            inputs(
                fallback_chain={"chat": [], "large": ["x", "y"]},
                manifest_call_timeout_seconds=3780.0,
            )
        )
        assert verdict.hops == 3
        assert verdict.violations

    def test_no_chain_is_single_hop(self):
        verdict = required_call_timeout(
            inputs(fallback_chain={}, manifest_call_timeout_seconds=1800.0)
        )
        assert verdict.hops == 1
        assert verdict.required == 1920.0
        assert verdict.violations

    def test_inversion_guard(self):
        verdict = required_call_timeout(inputs(manifest_request_timeout_secs=900.0))
        assert any("倒挂" in v for v in verdict.violations)

    def test_inversion_guard_green_when_transport_covers(self):
        verdict = required_call_timeout(
            inputs(manifest_request_timeout_secs=3780.0)
        )
        assert verdict.violations == []


class TestRealRepoFiles:
    """负控制：真实仓文件子进程实跑（防恒绿）。"""

    def test_real_config_passes(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, f"现仓配置必须合规: {proc.stdout}\n{proc.stderr}"

    def test_degraded_manifest_copy_fails(self, tmp_path: Path):
        """真实 llm.yaml + 声明调低的 manifest 副本 → exit 1。"""
        manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
        manifest["capabilities"]["call_timeout_seconds"] = 1800
        degraded = tmp_path / "plugin.json"
        degraded.write_text(json.dumps(manifest), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--manifest",
                str(degraded),
                "--llm-yaml",
                str(REAL_LLM_YAML),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 1
        assert "L3 公式违规" in proc.stderr

    @pytest.mark.parametrize("arg", ["--help"])
    def test_cli_surface(self, arg: str):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), arg],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0
