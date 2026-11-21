# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""learning_curve CLI 测试：campaign 聚合与离线 AUC。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import learning_curve as curve  # noqa: E402


def _episode(episode_id: str, action: str, score: int, value: float) -> dict:
    return {
        "episode_id": episode_id,
        "steps": [{
            "step": 0,
            "state": {"repr": "goal=same|step=0|last=x|seq=1"},
            "action": {"normalized": action},
            "credit": {"review_score": score, "discounted_return": value},
        }],
    }


def test_report_aggregates_avg_final_delta_and_slope(tmp_path):
    campaign_dir = tmp_path / "campaigns" / "c1"
    campaign_dir.mkdir(parents=True)
    payload = {
        "campaign_id": "c1",
        "suite": {"id": "demo", "version": 3},
        "protocol": {"type": "sequential_trials", "L": 3, "arms": ["static", "mem"]},
        "results": [
            {"arm": "static", "case_id": "case_1", "trial": 1, "passed": False,
             "tokens": 10, "cost_usd": 0.01},
            {"arm": "static", "case_id": "case_1", "trial": 2, "passed": False,
             "tokens": 10, "cost_usd": 0.01},
            {"arm": "static", "case_id": "case_1", "trial": 3, "passed": True,
             "tokens": 10, "cost_usd": 0.01},
            {"arm": "mem", "case_id": "case_1", "trial": 1, "passed": True,
             "tokens": 10, "cost_usd": 0.01},
            {"arm": "mem", "case_id": "case_1", "trial": 2, "passed": True,
             "tokens": 10, "cost_usd": 0.01},
            {"arm": "mem", "case_id": "case_1", "trial": 3, "passed": True,
             "tokens": 10, "cost_usd": 0.01},
        ],
    }
    (campaign_dir / "campaign.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    code = curve.main(["report", "--root", str(tmp_path), "--run", "c1"])
    assert code == 0
    report = json.loads((campaign_dir / "report.json").read_text(encoding="utf-8"))
    arms = {arm["name"]: arm["aggregate"] for arm in report["arms"]}
    assert arms["static"]["avg"] == 0.3333
    assert arms["static"]["final"] == 1.0
    assert arms["static"]["delta"] == 0.6667
    assert arms["static"]["slope"] == 0.5
    assert arms["mem"]["avg"] == 1.0
    assert arms["mem"]["cost_per_success"] == 0.01


def test_offline_auc_reads_latest_run(tmp_path):
    run_dir = tmp_path / "datasets" / "demo" / "r1"
    run_dir.mkdir(parents=True)
    episodes = [
        _episode("e1", "good()", 3, 3.0),
        _episode("e2", "good()", 3, 3.0),
        _episode("e3", "bad()", -2, -2.0),
        _episode("e4", "bad()", -2, -2.0),
    ]
    (run_dir / "episodes.jsonl").write_text(
        "\n".join(json.dumps(ep, ensure_ascii=False) for ep in episodes) + "\n",
        encoding="utf-8")
    (tmp_path / "datasets" / "demo" / "latest").write_text("r1\n", encoding="utf-8")

    code = curve.main(["offline", "--root", str(tmp_path), "--suite", "demo",
                       "--min-auc", "0.9"])
    assert code == 0
    result = json.loads((run_dir / "offline_auc.json").read_text(encoding="utf-8"))
    assert result["auc"] == 1.0
