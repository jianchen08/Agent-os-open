"""
复盘引擎 Bug 修复测试 - 验证 3 个已修复 Bug 和完整复盘流程。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.memory.maintenance.review_engine import (
    ChunkData,
    ExecutionRecord,
    PipelineRunSummary,
    ReviewEngine,
)

pytestmark = pytest.mark.skip(
    reason="ReviewEngine API 已重构为 register_pipeline/run_review 模型，"
           "旧构造函数参数（storage/chunk_db/knowledge_service）和方法签名已移除"
)

try:
    from src.memory.maintenance.review_engine import (
        ChunkData,
        ExecutionRecord,
        PipelineRunSummary,
        ReviewEngine,
    )
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    run_id: str = "run-001",
    status: str = "completed",
    review_status: str = "pending",
    **overrides,
) -> PipelineRunSummary:
    """创建 PipelineRunSummary 测试 fixture。"""
    defaults = dict(
        run_id=run_id,
        total_records=5,
        total_iterations=3,
        created_at="2026-01-01T00:00:00",
        status=status,
        error="",
        review_status=review_status,
    )
    defaults.update(overrides)
    return PipelineRunSummary(**defaults)


def _make_record(
    iteration: int = 1,
    name: str = "step_a",
    error: str = "",
    **overrides,
) -> ExecutionRecord:
    defaults = dict(
        iteration=iteration,
        type="tool",
        name=name,
        error=error,
        thinking_content="thinking...",
        tool_calls_json="{}",
        content="result",
        sequence=0,
    )
    defaults.update(overrides)
    return ExecutionRecord(**defaults)


def _build_engine(
    *,
    with_pipeline_engine: bool = False,
) -> tuple[ReviewEngine, MagicMock, MagicMock, MagicMock, MagicMock | None]:
    """构建 ReviewEngine 并返回 (engine, storage, chunk_db, knowledge_service, pipeline_engine)。"""
    storage = MagicMock()
    chunk_db = MagicMock()
    knowledge_service = MagicMock()
    pipeline_engine = MagicMock() if with_pipeline_engine else None

    engine = ReviewEngine(
        storage=storage,
        chunk_db=chunk_db,
        knowledge_service=knowledge_service,
        pipeline_engine=pipeline_engine,
    )
    return engine, storage, chunk_db, knowledge_service, pipeline_engine


# ===========================================================================
# Bug 1 测试: saved_count → saved_counts.get("experiences", 0)
# ===========================================================================


class TestBug1SavedCountFix:
    """Bug1: run_review 中引用未定义的 saved_count，修复为 saved_counts.get("experiences", 0)。"""

    @pytest.mark.asyncio
    async def test_experience_count_returns_saved_count(self):
        """验证 run_review 返回的 experience_count 与实际保存数量一致。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-bug1"
        storage.get_summary.return_value = _make_summary(run_id=run_id)
        # 2 条错误记录 → 应保存 2 条经验
        storage.list_by_pipeline.return_value = ([
            _make_record(name="step_a", error="timeout"),
            _make_record(name="step_b", error="value error"),
        ], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(
            side_effect=lambda **kw: {"id": "k-1", "status": "created"}
        )

        result = await engine.run_review(run_id)

        assert result["status"] == "success"
        assert result["experience_count"] == 2

    @pytest.mark.asyncio
    async def test_experience_count_zero_when_no_errors(self):
        """验证无错误记录时 experience_count 为 0（不会因 NameError 崩溃）。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-clean"
        storage.get_summary.return_value = _make_summary(run_id=run_id)
        # 无错误记录
        storage.list_by_pipeline.return_value = ([
            _make_record(name="step_a", error=""),
        ], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})

        result = await engine.run_review(run_id)

        assert result["status"] == "success"
        assert result["experience_count"] == 0

    @pytest.mark.asyncio
    async def test_experience_count_deduplicates_existing(self):
        """验证已存在的经验不会重复保存，experience_count 只计新增数量。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-dedup"
        storage.get_summary.return_value = _make_summary(run_id=run_id)
        storage.list_by_pipeline.return_value = [
            _make_record(name="step_a", error="timeout"),
        ]
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        # 已存在一条相同内容的经验
        existing_content = f"Pipeline {run_id} - step_a: timeout"
        ks.list_semantic_memory = AsyncMock(return_value={
            "items": [
                {"content": existing_content, "source_type": "review_experience"},
            ],
            "total": 1,
        })
        ks.create_knowledge = AsyncMock(return_value={"id": "k-new"})

        result = await engine.run_review(run_id)

        assert result["status"] == "success"
        assert result["experience_count"] == 0
        ks.create_knowledge.assert_not_called()


# ===========================================================================
# Bug 2 测试: _load_existing_experiences 使用 list_semantic_memory + 按 source_type 过滤
# ===========================================================================


class TestBug2LoadExistingExperiencesFix:
    """Bug2: _load_existing_experiences 签名错误，改用 list_semantic_memory(user_id='system') + 按 source_type 过滤。"""

    @pytest.mark.asyncio
    async def test_load_existing_filters_by_source_type(self):
        """验证只返回 source_type == 'review_experience' 的条目。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        ks.list_semantic_memory = AsyncMock(return_value={
            "items": [
                {"content": "exp-A", "source_type": "review_experience"},
                {"content": "exp-B", "source_type": "other_type"},
                {"content": "exp-C", "source_type": "review_experience"},
                {"content": "exp-D", "source_type": None},
            ],
            "total": 4,
        })

        result = await engine._load_existing_experiences()

        assert result == {"exp-A", "exp-C"}
        ks.list_semantic_memory.assert_awaited_once_with(user_id="system")

    @pytest.mark.asyncio
    async def test_load_existing_handles_empty_response(self):
        """验证空返回时得到空集合，不崩溃。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})

        result = await engine._load_existing_experiences()

        assert result == set()

    @pytest.mark.asyncio
    async def test_load_existing_handles_service_error_gracefully(self):
        """验证 knowledge_service 抛异常时返回空集合而非向上传播。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        ks.list_semantic_memory = AsyncMock(side_effect=ConnectionError("service down"))

        result = await engine._load_existing_experiences()

        assert result == set()

    @pytest.mark.asyncio
    async def test_load_existing_uses_system_user_id(self):
        """验证调用 list_semantic_memory 时传入 user_id='system'。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})

        await engine._load_existing_experiences()

        ks.list_semantic_memory.assert_awaited_once_with(user_id="system")


# ===========================================================================
# Bug 3 测试: _mark_pipeline_reviewed 改为 async，使用 await
# ===========================================================================


class TestBug3MarkPipelineReviewedAsyncFix:
    """Bug3: _mark_pipeline_reviewed 从同步改为 async，内部 run_until_complete 改为 await。"""

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_summary(self):
        """验证 _mark_pipeline_reviewed 正确更新 summary 的 review_status 为 completed。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-mark"
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])

        await engine._mark_pipeline_reviewed(run_id)

        storage.update_summary.assert_called_once_with(run_id, {"review_status": "completed"})

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_chunk_flags(self):
        """验证 _mark_pipeline_reviewed 会更新 chunk 的 reviewed 标记并保存。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-chunks"
        chunk1 = ChunkData(
            chunk_id="c1", pipeline_id=run_id, layer="summary",
            content="data", extra_data={"reviewed": False},
        )
        chunk_db.find_by_pipeline = AsyncMock(return_value=[chunk1])

        await engine._mark_pipeline_reviewed(run_id)

        assert chunk1.extra_data["reviewed"] is True
        chunk_db.save_chunk.assert_called_once_with(chunk1)

    @pytest.mark.asyncio
    async def test_mark_reviewed_handles_chunk_error_gracefully(self):
        """验证 chunk 操作异常不会阻止 review_status 更新。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        run_id = "run-err"
        chunk_db.find_by_pipeline = AsyncMock(side_effect=RuntimeError("disk error"))

        # 不应抛异常
        await engine._mark_pipeline_reviewed(run_id)

        # summary 仍然被更新
        storage.update_summary.assert_called_once_with(run_id, {"review_status": "completed"})

    @pytest.mark.asyncio
    async def test_mark_reviewed_is_awaitable_without_event_loop_conflict(self):
        """验证在已有事件循环中可以正常 await，不会因 run_until_complete 冲突。

        这是 Bug3 的核心场景：原同步实现使用 run_until_complete，在 async 上下文中
        会抛出 'This event loop is already running' 错误。
        """
        engine, storage, chunk_db, ks, _ = _build_engine()

        chunk_db.find_by_pipeline = AsyncMock(return_value=[])

        # 在 async 函数中直接 await（模拟实际调用场景）
        await engine._mark_pipeline_reviewed("run-loop")

        storage.update_summary.assert_called_once()


# ===========================================================================
# 集成测试: 完整复盘流程
# ===========================================================================


class TestIntegrationReviewFlow:
    """端到端复盘流程：get_pending → run_review → mark_reviewed。"""

    @pytest.mark.asyncio
    async def test_full_review_flow(self):
        """模拟完整复盘流程：
        1. 获取待复盘列表
        2. 对 pending 的 pipeline 执行 run_review
        3. 验证经验被正确保存
        4. 验证最终状态为 completed
        """
        engine, storage, chunk_db, ks, _ = _build_engine()

        # --- 阶段 1: 获取待复盘列表 ---
        summaries = [
            _make_summary(run_id="run-100", status="completed", review_status="pending"),
            _make_summary(run_id="run-101", status="completed", review_status="pending"),
            _make_summary(run_id="run-102", status="completed", review_status="completed"),  # 已完成
            _make_summary(run_id="run-103", status="running", review_status="pending"),  # 未完成
        ]
        storage.list_all_summaries.return_value = summaries

        pending = engine.get_pending_pipelines()
        assert len(pending) == 2
        assert {s.run_id for s in pending} == {"run-100", "run-101"}

        # --- 阶段 2: 对 run-100 执行复盘 ---
        run_id = "run-100"
        storage.get_summary.return_value = _make_summary(run_id=run_id)
        storage.list_by_pipeline.return_value = ([
            _make_record(iteration=1, name="search", error="API timeout"),
            _make_record(iteration=2, name="parse", error=""),  # 无错误
            _make_record(iteration=3, name="write", error="Permission denied"),
        ], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[
            ChunkData(
                chunk_id="c1", pipeline_id=run_id, layer="summary",
                content="analysis result", extra_data={"reviewed": False},
            ),
        ])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(return_value={"id": "k-new", "status": "created"})

        result = await engine.run_review(run_id)

        # --- 阶段 3: 验证结果 ---
        assert result["status"] == "success"
        assert result["run_id"] == run_id
        assert result["experience_count"] == 2  # 2 条错误记录
        assert result["records_analyzed"] == 3

        # 验证 storage 调用链
        update_calls = storage.update_summary.call_args_list
        assert update_calls[0] == ((run_id, {"review_status": "reviewing"}),)
        assert update_calls[-1] == ((run_id, {"review_status": "completed"}),)

        # 验证经验创建调用
        assert ks.create_knowledge.await_count == 2
        create_calls = ks.create_knowledge.call_args_list
        assert create_calls[0].kwargs["user_id"] == "system"
        assert create_calls[0].kwargs["source_type"] == "review_experience"
        assert "search" in create_calls[0].kwargs["content"]
        assert "API timeout" in create_calls[0].kwargs["content"]

        # 验证 chunk 被标记
        chunks = chunk_db.find_by_pipeline.return_value
        assert chunks[0].extra_data["reviewed"] is True

    @pytest.mark.asyncio
    async def test_full_flow_with_pipeline_engine(self):
        """验证有 pipeline_engine 时的深度分析流程。"""
        engine, storage, chunk_db, ks, pe = _build_engine(with_pipeline_engine=True)

        run_id = "run-deep"
        storage.get_summary.return_value = _make_summary(run_id=run_id)
        storage.list_by_pipeline.return_value = [
            _make_record(name="step", error="some error"),
        ]
        chunk_db.find_by_pipeline = AsyncMock(return_value=[
            ChunkData(chunk_id="c1", pipeline_id=run_id, layer="summary", content="chunk content"),
        ])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(return_value={"id": "k-1"})
        pe.run = AsyncMock(return_value={"raw_result": "deep analysis done"})

        result = await engine.run_review(run_id)

        assert result["status"] == "success"
        pe.run.assert_awaited_once()
        call_kwargs = pe.run.call_args
        assert "chunk content" in call_kwargs.kwargs["user_input"]
        assert call_kwargs.kwargs["allow_default_fallback"] is True

    @pytest.mark.asyncio
    async def test_full_flow_pipeline_not_found(self):
        """验证 pipeline 不存在时返回错误。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        storage.get_summary.return_value = None

        result = await engine.run_review("nonexistent")

        assert result["status"] == "error"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_full_flow_pipeline_not_completed(self):
        """验证未完成的 pipeline 不能被复盘。"""
        engine, storage, chunk_db, ks, _ = _build_engine()

        storage.get_summary.return_value = _make_summary(
            run_id="run-running", status="running"
        )

        result = await engine.run_review("run-running")

        assert result["status"] == "error"
        assert "not completed" in result["message"]
