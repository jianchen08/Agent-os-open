# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""experience_build CLI 测试：从导出 bundle 构建经验集并过质量闸。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import experience_build as build  # noqa: E402


def _bundle(pipeline_id: str, goal: str, status: str, review_id: str, score: int) -> dict:
    return {
        "task": {
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
        },
        "traces": [{
            "seq": 1,
            "patch_data": {
                "_executed_tool_calls": [{
                    "name": "file_write",
                    "arguments": '{"path": "/tmp/ws/note.txt", "content": "hi"}',
                }],
                "tool_results": {"items": [{"tool_name": "file_write", "chars": 2}]},
            },
        }],
        "review": {
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
            }],
        },
    }


def test_build_from_json_writes_episodes_and_passes_gates(tmp_path):
    bundle_path = tmp_path / "bundles.json"
    bundle_path.write_text(json.dumps([
        _bundle("p1", "goal one", "completed", "r1", 3),
        _bundle("p2", "goal two", "failed", "r2", -2),
    ], ensure_ascii=False), encoding="utf-8")

    code = build.main([
        "build", "--root", str(tmp_path), "--suite", "demo",
        "--from-json", str(bundle_path), "--run-id", "r1",
    ])
    assert code == 0, "质量闸应全绿"

    run_dir = tmp_path / "datasets" / "demo" / "r1"
    episodes = [json.loads(line) for line in
                (run_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
    assert len(episodes) == 2
    assert episodes[0]["review"]["review_id"] == "r1"
    assert episodes[0]["returns"]["G"] == [3.0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality_gates"]["all_passed"] is True
    assert (tmp_path / "datasets" / "demo" / "latest").read_text(
        encoding="utf-8").strip() == "r1"


def test_build_without_reviews_fails_coverage_gate(tmp_path):
    bundle = _bundle("p3", "goal three", "completed", "r3", 3)
    bundle["review"] = None
    bundle_path = tmp_path / "bundles.json"
    bundle_path.write_text(json.dumps([bundle], ensure_ascii=False), encoding="utf-8")

    code = build.main([
        "build", "--root", str(tmp_path), "--suite", "demo",
        "--from-json", str(bundle_path), "--run-id", "r2",
    ])
    assert code == 2
    manifest = json.loads((tmp_path / "datasets" / "demo" / "r2" /
                           "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality_gates"]["review_coverage"] == 0.0
    assert manifest["quality_gates"]["all_passed"] is False
