# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""experience_lib 纯函数测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import experience_lib as lib  # noqa: E402


def _task(pipeline_id: str = "p1", goal: str = "task one", status: str = "completed") -> dict:
    return {
        "pipeline_id": pipeline_id,
        "goal": goal,
        "description": goal,
        "status": status,
        "acceptance_criteria": {"file_check": {"path": "note.txt"}},
        "score": 100.0 if status == "completed" else 0.0,
        "model": "test/model",
        "agent_id": "general_agent",
        "ended_at": "2026-11-13T00:00:00+00:00",
        "ws_path": "/tmp/ws",
    }


def _traces(seq: int = 1) -> list[dict]:
    return [{
        "seq": seq,
        "patch_data": {
            "_executed_tool_calls": [{
                "name": "file_write",
                "arguments": '{"path": "/tmp/ws/note.txt", "content": "hi"}',
            }],
            "tool_results": {"items": [{"tool_name": "file_write", "chars": 2}]},
        },
    }]


def _review(review_id: str, score: int) -> dict:
    return {
        "review_id": review_id,
        "status": "completed",
        "report_ref": f"ws:docs/working/reviews/{review_id}.json",
        "overall_status": "success" if score > 0 else "failed",
        "credit_policy": "review_credit_v1",
        "lessons": ["lesson"],
        "step_credits": [{
            "step_ref": "trace:1:0",
            "score": score,
            "dimension": "instruction_compliance",
            "confidence": "certain",
            "evidence": "iteration:1",
            "root_cause": None,
        }],
    }


def test_normalize_action_strips_workspace_and_volatile_keys():
    action = lib.normalize_action(
        raw='{"path": "/tmp/ws/src/a.py", "timestamp": "x", "n": 1}',
        tool="file_read",
        workspace="/tmp/ws",
    )
    assert action["tool"] == "file_read"
    assert action["args"]["path"] == "src/a.py"
    assert "timestamp" not in action["args"]
    assert action["normalized"].startswith("file_read(")


def test_discounted_returns_marks_incomplete_window_none():
    assert lib.discounted_returns([3, -2], gamma=0.5) == [2.0, -2.0]
    assert lib.discounted_returns([3, None], gamma=0.5) == [None, None]


def test_build_episode_aligns_step_credits_and_returns():
    episode = lib.build_episode(_task(), _traces(), _review("review_1", 3),
                                suite="demo", split="train", gamma=0.5)
    assert episode is not None
    assert episode["steps"][0]["credit"]["review_score"] == 3
    assert episode["steps"][0]["credit"]["review_id"] == "review_1"
    assert episode["returns"]["G"] == [3.0]
    assert episode["review"]["review_id"] == "review_1"
    assert lib.validate_episode(episode) == []


def test_quality_gates_pass_and_leakage_fail():
    success = lib.build_episode(_task("p1", "goal one", "completed"), _traces(),
                                _review("r1", 3), suite="demo", split="train")
    failed = lib.build_episode(_task("p2", "goal two", "failed"), _traces(),
                               _review("r2", -2), suite="demo", split="train")
    episodes = [success, failed]

    gates = lib.quality_gates(episodes)
    assert gates["schema_valid"] is True
    assert gates["review_coverage"] == 1.0
    assert gates["calibration"] == 1.0
    assert gates["all_passed"] is True

    source = episodes[0]["source"]
    source["case_id"] = "case_x"
    leaked = lib.quality_gates(episodes, heldout_case_ids={"case_x"})
    assert leaked["leakage"] == 1
    assert leaked["all_passed"] is False


def test_leave_one_out_auc_separates_good_and_bad_actions():
    def episode(episode_id: str, action: str, score: int, value: float) -> dict:
        return {
            "episode_id": episode_id,
            "steps": [{
                "step": 0,
                "state": {"repr": "goal=same|step=0|last=x|seq=1"},
                "action": {"normalized": action},
                "credit": {"review_score": score, "discounted_return": value},
            }],
        }

    episodes = [
        episode("e1", "good()", 3, 3.0),
        episode("e2", "good()", 3, 3.0),
        episode("e3", "bad()", -2, -2.0),
        episode("e4", "bad()", -2, -2.0),
    ]
    result = lib.leave_one_out_auc(episodes, k=3)
    assert result["auc"] == 1.0
    assert result["positives"] == 2
    assert result["negatives"] == 2
