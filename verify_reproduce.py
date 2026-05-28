"""
复盘模块修复后完整可用性验证 - 可复现验证脚本
============================================
验证修复了 review_engine.py 中 3 个 Bug 后的完整复盘流程。

Bug 修复记录:
- Bug1: saved_count 未定义 → 改为 saved_counts.get("experiences", 0)
- Bug2: _load_existing_experiences 签名错误 → 改用 list_semantic_memory + 按 source_type 过滤
- Bug3: _mark_pipeline_reviewed 从同步改为 async，run_until_complete 改为 await

运行方式: python3 verify_reproduce.py
前置条件: pip install pytest pytest-asyncio
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

from src.memory.maintenance.review_engine import (
    ChunkData,
    ExecutionRecord,
    PipelineRunSummary,
    ReviewEngine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_summary(
    run_id: str = "run-001",
    status: str = "completed",
    review_status: str = "pending",
) -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=run_id, status=status, review_status=review_status,
        total_records=5, total_iterations=3,
        created_at="2026-01-01T00:00:00", error="",
    )


def make_record(
    iteration: int = 1, name: str = "step", error: str = "",
) -> ExecutionRecord:
    return ExecutionRecord(
        iteration=iteration, type="tool", name=name, error=error,
        thinking_content="", tool_calls_json="{}", content="", sequence=0,
    )


def build_engine(*, with_pipeline_engine: bool = False):
    storage = MagicMock()
    chunk_db = MagicMock()
    ks = MagicMock()
    pe = MagicMock() if with_pipeline_engine else None
    engine = ReviewEngine(
        storage=storage, chunk_db=chunk_db,
        knowledge_service=ks, pipeline_engine=pe,
    )
    return engine, storage, chunk_db, ks, pe


# ---------------------------------------------------------------------------
# 用户旅程: 手动触发复盘完整流程 (6 步串联)
# ---------------------------------------------------------------------------

def test_user_journey():
    """完整用户旅程: 构建引擎 → 查询 pending → 复盘 → 验证产出 → 验证标记 → 二次触发"""
    print("\n" + "=" * 70)
    print("用户旅程: 手动触发复盘完整流程")
    print("=" * 70)

    # 步骤 1: 构建 ReviewEngine（用户实例化复盘引擎）
    print("\n--- 步骤 1: 构建 ReviewEngine ---")
    engine, storage, chunk_db, ks, _ = build_engine()
    print("  [OK] ReviewEngine 实例化成功")

    # 步骤 2: 查询 pending 管道
    print("\n--- 步骤 2: 查询 pending 管道 ---")
    storage.list_all_summaries.return_value = [
        make_summary(run_id="run-001"),
        make_summary(run_id="run-002"),
        make_summary(run_id="run-003", review_status="completed"),
        make_summary(run_id="run-004", status="running"),
    ]
    pending = engine.get_pending_pipelines()
    assert len(pending) == 2
    pending_ids = sorted(s.run_id for s in pending)
    assert pending_ids == ["run-001", "run-002"]
    print(f"  [OK] 筛选出 {len(pending)} 条 pending 管道: {pending_ids}")

    # 步骤 3: 对 pending 管道执行完整复盘（状态传递: 使用步骤2的筛选结果）
    print("\n--- 步骤 3: 对 pending 管道执行完整复盘 ---")
    target_run_id = pending_ids[0]
    storage.get_summary.return_value = make_summary(run_id=target_run_id)
    storage.list_by_pipeline.return_value = [
        make_record(iteration=1, name="search_tool", error="API timeout after 30s"),
        make_record(iteration=2, name="parse_tool"),
        make_record(iteration=3, name="write_tool", error="Permission denied: /data/output.txt"),
    ]
    chunk_db.find_by_pipeline = AsyncMock(return_value=[
        ChunkData(chunk_id="c1", pipeline_id=target_run_id, layer="summary",
                  content="analysis result", extra_data={"reviewed": False}),
    ])
    ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
    saved_experiences = []
    ks.create_knowledge = AsyncMock(
        side_effect=lambda **kw: (saved_experiences.append(kw), {"id": f"k-{len(saved_experiences)}", "status": "created"})[1]
    )

    result = asyncio.run(engine.run_review(target_run_id))
    assert result["status"] == "success"
    assert result["run_id"] == target_run_id
    assert result["experience_count"] == 2
    assert result["records_analyzed"] == 3
    print(f"  [OK] 复盘成功: experience_count={result['experience_count']}, records_analyzed={result['records_analyzed']}")

    # 步骤 4: 验证经验产出（状态传递: 使用步骤3的保存结果）
    print("\n--- 步骤 4: 验证经验产出 ---")
    assert len(saved_experiences) == 2
    assert "search_tool" in saved_experiences[0]["content"]
    assert "API timeout" in saved_experiences[0]["content"]
    assert "write_tool" in saved_experiences[1]["content"]
    for exp in saved_experiences:
        assert exp["source_type"] == "review_experience"
        assert exp["user_id"] == "system"
    print(f"  [OK] {len(saved_experiences)} 条经验正确保存到 Knowledge")

    # 步骤 5: 验证复盘标记（状态传递: 使用步骤3的 chunk 和 summary）
    print("\n--- 步骤 5: 验证复盘标记 ---")
    update_calls = storage.update_summary.call_args_list
    assert update_calls[0][0] == (target_run_id, {"review_status": "reviewing"})
    assert update_calls[-1][0] == (target_run_id, {"review_status": "completed"})
    chunk = chunk_db.find_by_pipeline.return_value[0]
    assert chunk.extra_data["reviewed"] is True
    print("  [OK] summary: pending → reviewing → completed, chunk: reviewed=True")

    # 步骤 6: 二次触发验证（状态传递: 已复盘的不再出现）
    print("\n--- 步骤 6: 二次触发验证 ---")
    storage.list_all_summaries.return_value = [
        make_summary(run_id="run-001", review_status="completed"),
        make_summary(run_id="run-002"),
    ]
    pending_after = engine.get_pending_pipelines()
    assert len(pending_after) == 1
    assert pending_after[0].run_id == "run-002"
    print("  [OK] 二次筛选: run-001 已复盘不再出现, 只剩 run-002")

    print("\n用户旅程: 6/6 步骤通过 ✅")
    return True


# ---------------------------------------------------------------------------
# 补充场景 1: 错误输入
# ---------------------------------------------------------------------------

def test_error_inputs():
    """错误输入: 不存在的 pipeline、未完成的 pipeline、空 ID"""
    print("\n--- 补充场景 1: 错误输入 ---")
    engine, storage, chunk_db, ks, _ = build_engine()

    # 不存在
    storage.get_summary.return_value = None
    result = asyncio.run(engine.run_review("nonexistent"))
    assert result["status"] == "error" and "not found" in result["message"]
    print("  [OK] 1a. 不存在的 pipeline → error + not found")

    # 未完成
    storage.get_summary.return_value = make_summary(status="running")
    result = asyncio.run(engine.run_review("run-running"))
    assert result["status"] == "error" and "not completed" in result["message"]
    print("  [OK] 1b. 未完成的 pipeline → error + not completed")

    # 空 ID
    storage.get_summary.return_value = None
    result = asyncio.run(engine.run_review(""))
    assert result["status"] == "error"
    print("  [OK] 1c. 空 pipeline ID → error")

    return True


# ---------------------------------------------------------------------------
# 补充场景 2: 边界/异常
# ---------------------------------------------------------------------------

def test_edge_cases():
    """边界/异常: Knowledge 服务异常容错、全量去重、无 pending"""
    print("\n--- 补充场景 2: 边界/异常 ---")

    # 2a: Knowledge 服务异常容错
    engine, storage, chunk_db, ks, _ = build_engine()
    run_id = "run-resilient"
    storage.get_summary.return_value = make_summary(run_id=run_id)
    storage.list_by_pipeline.return_value = [make_record(name="step_a", error="test error")]
    ks.list_semantic_memory = AsyncMock(side_effect=ConnectionError("service down"))
    ks.create_knowledge = AsyncMock(side_effect=RuntimeError("write failed"))
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    result = asyncio.run(engine.run_review(run_id))
    assert result["status"] == "success"
    assert result["experience_count"] == 0
    update_calls = storage.update_summary.call_args_list
    assert update_calls[-1][0] == (run_id, {"review_status": "completed"})
    print("  [OK] 2a. Knowledge 服务异常时不崩溃，仍标记完成")

    # 2b: 全量去重
    engine, storage, chunk_db, ks, _ = build_engine()
    run_id = "run-dedup"
    storage.get_summary.return_value = make_summary(run_id=run_id)
    storage.list_by_pipeline.return_value = [
        make_record(name="step_x", error="known error"),
    ]
    ks.list_semantic_memory = AsyncMock(return_value={
        "items": [
            {"content": f"Pipeline {run_id} - step_x: known error", "source_type": "review_experience"},
        ],
        "total": 1,
    })
    ks.create_knowledge = AsyncMock(return_value={"id": "k-new"})
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    result = asyncio.run(engine.run_review(run_id))
    assert result["experience_count"] == 0
    ks.create_knowledge.assert_not_called()
    print("  [OK] 2b. 全量去重 - 不创建重复经验")

    # 2c: 无 pending
    storage.list_all_summaries.return_value = [
        make_summary(review_status="completed"),
    ]
    assert len(engine.get_pending_pipelines()) == 0
    print("  [OK] 2c. 无 pending 管道时返回空列表")

    return True


# ---------------------------------------------------------------------------
# Bug 专项验证
# ---------------------------------------------------------------------------

def test_bug_specific():
    """专项验证 3 个修复的 Bug"""
    print("\n--- Bug 专项验证 ---")

    # Bug1: saved_count → saved_counts.get
    engine, storage, chunk_db, ks, _ = build_engine()
    storage.get_summary.return_value = make_summary()
    storage.list_by_pipeline.return_value = [make_record(error="err")]
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
    ks.create_knowledge = AsyncMock(return_value={"id": "k-1"})
    result = asyncio.run(engine.run_review("run-001"))
    assert result["experience_count"] == 1  # 不会 NameError
    print("  [OK] Bug1: saved_counts.get 正确返回经验数量")

    # Bug2: _load_existing_experiences 使用 list_semantic_memory
    engine, storage, chunk_db, ks, _ = build_engine()
    ks.list_semantic_memory = AsyncMock(return_value={
        "items": [
            {"content": "exp-A", "source_type": "review_experience"},
            {"content": "exp-B", "source_type": "other"},
        ],
        "total": 2,
    })
    result = asyncio.run(engine._load_existing_experiences())
    assert result == {"exp-A"}
    ks.list_semantic_memory.assert_awaited_once_with(user_id="system")
    print("  [OK] Bug2: _load_existing_experiences 正确过滤 source_type")

    # Bug3: _mark_pipeline_reviewed async 化
    engine, storage, chunk_db, ks, _ = build_engine()
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    asyncio.run(engine._mark_pipeline_reviewed("run-001"))
    storage.update_summary.assert_called_once_with("run-001", {"review_status": "completed"})
    print("  [OK] Bug3: _mark_pipeline_reviewed 在 async 上下文正常工作")

    return True


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("复盘模块修复后完整可用性验证")
    print("=" * 70)

    results = {}

    # 1. 单元测试基线
    print("\n>>> 运行已有单元测试 (pytest)")
    import subprocess
    proc = subprocess.run(
        ["python3", "-m", "pytest", "tests/test_review_engine_fixes.py", "-v", "--tb=short"],
        capture_output=True, text=True,
    )
    print(proc.stdout[-500:] if len(proc.stdout) > 500 else proc.stdout)
    results["unit_tests"] = proc.returncode == 0
    print(f"  单元测试: {'通过' if results['unit_tests'] else '失败'} (15 tests)")

    # 2. 用户旅程
    try:
        results["user_journey"] = test_user_journey()
    except Exception as e:
        print(f"  用户旅程失败: {e}")
        results["user_journey"] = False

    # 3. 补充场景
    try:
        results["error_inputs"] = test_error_inputs()
    except Exception as e:
        print(f"  错误输入场景失败: {e}")
        results["error_inputs"] = False

    try:
        results["edge_cases"] = test_edge_cases()
    except Exception as e:
        print(f"  边界异常场景失败: {e}")
        results["edge_cases"] = False

    # 4. Bug 专项
    try:
        results["bug_specific"] = test_bug_specific()
    except Exception as e:
        print(f"  Bug 专项验证失败: {e}")
        results["bug_specific"] = False

    # 汇总
    print("\n" + "=" * 70)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"验证汇总: {passed}/{total} 项目通过")
    for k, v in results.items():
        print(f"  - {k}: {'✅ 通过' if v else '❌ 失败'}")
    print("=" * 70)

    sys.exit(0 if all(results.values()) else 1)
