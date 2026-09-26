# @feature: FP-GATE-META 元门禁 gate-wiring | @ci: gate-wiring
"""元门禁 V1–V8 校验函数回归 + 删值实验六件 + 活体冒烟。

删值实验约定（执行方案批次 M）：故意制造违规形态必须红；活体 = 真实仓库
八项全绿（gate-wiring 在 ci.yml 的接线被删除时，本文件活体用例与 CI job
双双变红——自举性质的测试面落点）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_gate_wiring.py"
_spec = importlib.util.spec_from_file_location("check_gate_wiring", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_gate_wiring", _mod)
_spec.loader.exec_module(_mod)

v1_wiring = _mod.v1_wiring
v2_reverse = _mod.v2_reverse
v3_mode = _mod.v3_mode
v4_observation = _mod.v4_observation
v5_fast_budget = _mod.v5_fast_budget
v6_files = _mod.v6_files
v7_orphan_baseline = _mod.v7_orphan_baseline
v8_checklist = _mod.v8_checklist
parse_checklist = _mod.parse_checklist
_ledger_files = _mod._ledger_files


def _gate(gid: str, wiring: str = "ci", fast: bool = False, est: int | None = None,
          reason: str = "", command: str = "") -> dict:
    return {
        "id": gid, "label": gid, "domain": "cross", "fast": fast,
        "wiring": wiring, "wiring_reason": reason, "allow_failure": wiring == "observation",
        "est_seconds": est, "needs": [], "command": command,
    }


def _green_world() -> tuple[list[dict], set[str], set[str], list[str], dict[str, str], list[dict], dict[str, int]]:
    """最小但全绿的世界：账本由 gate 命令真实消费（V7 一跳），清单合法。"""
    gates = [
        _gate("g-a", command="python scripts/check_a.py --baseline .github/x-baseline.txt"),
        _gate("g-b", wiring="local", reason="过渡", command="python scripts/check_b.py"),
    ]
    referenced = {"g-a", "g-b"}
    jobs = {"ci-job"}
    mode_calls: list[str] = []
    wf_texts: dict[str, str] = {}
    rows = [
        {"capability": "cap-x", "ids": ["g-a"], "status": "covered", "note": ""},
        {"capability": "cap-y", "ids": [], "status": "exempt", "note": "行业可选"},
    ]
    baseline = {"observation_max": 1, "missing_max": 0}
    return gates, referenced, jobs, mode_calls, wf_texts, rows, baseline


def test_green_world_all_checks_pass():
    gates, referenced, jobs, mode_calls, wf_texts, rows, baseline = _green_world()
    tracked = [".github/x-baseline.txt"]
    assert v1_wiring(gates, referenced) == []
    assert v2_reverse(gates, referenced) == []
    assert v3_mode(mode_calls) == []
    assert v4_observation(gates, baseline) == []
    assert v6_files(tracked) == []
    assert v7_orphan_baseline(gates, tracked, wf_texts) == []
    assert v8_checklist(gates, referenced, jobs, rows, baseline) == []


# ── 删值实验 ①② ：接线双向 ──────────────────────────────────────────
def test_v1_red_when_ci_gate_unreferenced():
    gates = [_gate("lonely")]
    assert any("死门禁" in v for v in v1_wiring(gates, set()))


def test_v1_red_when_exempt_gate_has_no_reason():
    gates = [_gate("lazy", wiring="observation")]
    out = v1_wiring(gates, {"lazy"})
    assert any("缺 wiring_reason" in v for v in out)


def test_v2_red_on_unknown_referenced_id():
    gates = [_gate("real")]
    out = v2_reverse(gates, {"real", "kernel-fomr"})
    assert out
    assert "kernel-fomr" in out[0]


# ── 删值实验 ③：调用形态 ────────────────────────────────────────────
def test_v3_red_on_mode_invocation():
    out = v3_mode(["ci.yml: python scripts/run_gates.py --mode all"])
    assert out
    assert "--mode" in out[0]


# ── 删值实验（V4/V5）：执法力度 ─────────────────────────────────────
def test_v4_red_when_observation_over_baseline():
    gates = [_gate("o1", wiring="observation", reason="x"), _gate("o2", wiring="observation", reason="y")]
    out = v4_observation(gates, {"observation_max": 1})
    assert out
    assert "2 > 基线 1" in out[0]
    assert v4_observation(gates[:1], {"observation_max": 1}) == []


def test_v5_red_on_undeclared_est_and_over_budget():
    gates = [_gate("f1", fast=True, est=None)]
    out = v5_fast_budget(gates)
    assert any("未声明 est_seconds" in v for v in out)
    gates2 = [_gate("f1", fast=True, est=80), _gate("f2", fast=True, est=20)]
    out2 = v5_fast_budget(gates2)
    assert any("100s > 预算 90s" in v for v in out2)


# ── 删值实验 ⑤：账本位置 ────────────────────────────────────────────
def test_v6_red_on_misplaced_ledger_and_bak():
    tracked = ["scripts/new_ledger-baseline.txt", ".github/ok-baseline.txt", "junk/old.py.bak"]
    out = v6_files(tracked)
    assert any("new_ledger-baseline.txt" in v for v in out)
    assert any(".bak" in v for v in out)
    assert not any("ok-baseline" in v for v in out)
    # 检查器/测试等同名 .py/.md 不算账本（首跑误报教训）
    assert _ledger_files(["scripts/check_x_baseline.py", "docs/lint-baseline.md"]) == []


def test_v7_red_on_orphan_ledger():
    gates = [_gate("g-a", command="python scripts/check_a.py")]
    tracked = [".github/orphan-baseline.txt", ".github/consumed-baseline.txt", "scripts/check_a.py"]
    out = v7_orphan_baseline(gates, tracked, {})
    joined = "\n".join(out)
    assert "orphan-baseline.txt" in joined
    # consumed-baseline 由 gate 引用的检查器源码消费（两跳链）——此处源码不存在
    # 文件读取按 OSError 跳过，故它也在孤儿名单中；两跳正向由活体用例覆盖。
    assert "consumed-baseline.txt" in joined


# ── 删值实验 ⑥：V8 能力清单 ────────────────────────────────────────
def test_v8_red_on_broken_covered_row_and_missing_regression():
    gates = [_gate("real"), _gate("unwired")]
    rows = [
        {"capability": "ok", "ids": ["real"], "status": "covered", "note": ""},
        {"capability": "ghost", "ids": ["no-such-gate"], "status": "covered", "note": ""},
        {"capability": "notwired", "ids": ["unwired"], "status": "covered", "note": ""},
        {"capability": "bad", "ids": [], "status": "exempt", "note": ""},
        {"capability": "cap-x", "ids": [], "status": "missing", "note": ""},
        {"capability": "cap-x", "ids": [], "status": "missing", "note": ""},
    ]
    out = v8_checklist(gates, {"real"}, set(), rows, {"missing_max": 1})
    joined = "\n".join(out)
    assert "no-such-gate" in joined
    assert "未接线" in joined
    assert "exempt 行无理由" in joined
    assert "2 > 基线 1" in joined
    assert "状态非法" not in joined


def test_v8_red_on_ci_job_reference_missing():
    gates = [_gate("g-a")]
    rows = [{"capability": "inline-cap", "ids": ["ci:ghost-job"], "status": "covered", "note": ""}]
    out = v8_checklist(gates, {"g-a"}, set(), rows, {"missing_max": 0})
    assert out
    assert "ghost-job" in out[0]


def test_parse_checklist_tolerates_comments_and_malformed():
    rows = parse_checklist()
    assert rows, "真实清单非空"
    assert all(r["status"] in ("covered", "missing", "exempt") for r in rows)


# ── 活体：真实仓库八项全绿（自举——删 ci.yml 接线此处即红）──────────
def test_live_repo_passes_meta_gate():
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_SCRIPT.parents[2]),
        check=False,
    )
    assert proc.returncode == 0, f"活体元门禁红：\n{proc.stdout}\n{proc.stderr}"
