"""补充场景验证脚本 - 错误输入 + 边界/异常"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

from src.memory.maintenance.review_engine import (
    ReviewEngine,
    PipelineRunSummary,
    ExecutionRecord,
    ChunkData,
)

print("=" * 70)
print("补充场景验证")
print("=" * 70)

# ============================================================================
# 补充场景 1: 错误输入 - 不存在的 pipeline 和未完成的 pipeline
# ============================================================================
print("\n--- 补充场景 1: 错误输入场景 ---")

storage = MagicMock()
chunk_db = MagicMock()
knowledge_service = MagicMock()
engine = ReviewEngine(
    storage=storage,
    chunk_db=chunk_db,
    knowledge_service=knowledge_service,
    pipeline_engine=None,
)

# 1a: 不存在的 pipeline
storage.get_summary.return_value = None
result = asyncio.run(engine.run_review("nonexistent-run"))
assert result["status"] == "error", f"Expected error, got: {result}"
assert "not found" in result["message"], f"Expected 'not found', got: {result['message']}"
print("  [OK] 1a. 不存在的 pipeline 返回 error + not found")

# 1b: 未完成的 pipeline
storage.get_summary.return_value = PipelineRunSummary(
    run_id="run-running", status="running", review_status="pending",
    total_records=0, total_iterations=0, created_at="2026-01-01T00:00:00", error=""
)
result = asyncio.run(engine.run_review("run-running"))
assert result["status"] == "error", f"Expected error, got: {result}"
assert "not completed" in result["message"], f"Expected 'not completed', got: {result['message']}"
print("  [OK] 1b. 未完成的 pipeline 返回 error + not completed")

# 1c: 空 pipeline ID
storage.get_summary.return_value = None
result = asyncio.run(engine.run_review(""))
assert result["status"] == "error", f"Expected error for empty id, got: {result}"
print("  [OK] 1c. 空 pipeline ID 返回 error")

scenario1_ok = True

# ============================================================================
# 补充场景 2: 边界/异常 - Knowledge 服务异常容错 + 全量去重
# ============================================================================
print("\n--- 补充场景 2: 边界/异常场景 ---")

# 2a: Knowledge 服务 list_semantic_memory 异常时容错
storage2 = MagicMock()
chunk_db2 = MagicMock()
ks2 = MagicMock()
engine2 = ReviewEngine(
    storage=storage2, chunk_db=chunk_db2,
    knowledge_service=ks2, pipeline_engine=None,
)

run_id = "run-resilient"
storage2.get_summary.return_value = PipelineRunSummary(
    run_id=run_id, status="completed", review_status="pending",
    total_records=2, total_iterations=1, created_at="2026-01-01T00:00:00", error=""
)
storage2.list_by_pipeline.return_value = [
    ExecutionRecord(
        iteration=1, type="tool", name="step_a", error="test error",
        thinking_content="", tool_calls_json="{}", content="", sequence=0,
    ),
]

# list_semantic_memory 抛异常，create_knowledge 也抛异常
ks2.list_semantic_memory = AsyncMock(side_effect=ConnectionError("service down"))
ks2.create_knowledge = AsyncMock(side_effect=RuntimeError("write failed"))
chunk_db2.find_by_pipeline = AsyncMock(return_value=[])

result = asyncio.run(engine2.run_review(run_id))
assert result["status"] == "success", f"Expected success even with errors, got: {result}"
assert result["experience_count"] == 0, f"Expected 0 experiences when create fails, got: {result['experience_count']}"
# 标记仍然完成
update_calls = storage2.update_summary.call_args_list
assert update_calls[-1][0] == (run_id, {"review_status": "completed"}), "Final status not completed"
print("  [OK] 2a. Knowledge 服务异常时复盘不崩溃，仍然标记完成")

# 2b: 全量已有经验 - 所有错误记录都已存在，不重复保存
storage3 = MagicMock()
chunk_db3 = MagicMock()
ks3 = MagicMock()
engine3 = ReviewEngine(
    storage=storage3, chunk_db=chunk_db3,
    knowledge_service=ks3, pipeline_engine=None,
)

run_id = "run-dedup-all"
storage3.get_summary.return_value = PipelineRunSummary(
    run_id=run_id, status="completed", review_status="pending",
    total_records=2, total_iterations=1, created_at="2026-01-01T00:00:00", error=""
)
storage3.list_by_pipeline.return_value = [
    ExecutionRecord(iteration=1, type="tool", name="step_x", error="known error A",
                    thinking_content="", tool_calls_json="{}", content="", sequence=0),
    ExecutionRecord(iteration=2, type="tool", name="step_y", error="known error B",
                    thinking_content="", tool_calls_json="{}", content="", sequence=1),
]

# 已有经验完全匹配
ks3.list_semantic_memory = AsyncMock(return_value={
    "items": [
        {"content": "Pipeline run-dedup-all - step_x: known error A", "source_type": "review_experience"},
        {"content": "Pipeline run-dedup-all - step_y: known error B", "source_type": "review_experience"},
    ],
    "total": 2,
})
ks3.create_knowledge = AsyncMock(return_value={"id": "k-new"})
chunk_db3.find_by_pipeline = AsyncMock(return_value=[])

result = asyncio.run(engine3.run_review(run_id))
assert result["status"] == "success", f"Expected success, got: {result}"
assert result["experience_count"] == 0, f"Expected 0 new experiences (all deduped), got: {result['experience_count']}"
ks3.create_knowledge.assert_not_called()
print("  [OK] 2b. 全量去重 - 所有经验已存在，不创建新记录")

# 2c: 无 pending 管道时 get_pending_pipelines 返回空
storage3.list_all_summaries.return_value = [
    PipelineRunSummary(
        run_id="r1", status="completed", review_status="completed",
        total_records=1, total_iterations=1, created_at="2026-01-01T00:00:00", error=""
    ),
]
pending = engine3.get_pending_pipelines()
assert len(pending) == 0, f"Expected empty list, got {len(pending)}"
print("  [OK] 2c. 无 pending 管道时返回空列表")

scenario2_ok = True

# ============================================================================
# 汇总
# ============================================================================
print("\n" + "=" * 70)
all_ok = scenario1_ok and scenario2_ok
status_text = "2/2 通过" if all_ok else "FAIL"
print(f"补充场景结果: 全部通过 ({status_text})")
print("=" * 70)
sys.exit(0 if all_ok else 1)
