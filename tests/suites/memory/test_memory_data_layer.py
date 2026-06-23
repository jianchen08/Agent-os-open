"""数据层模块测试 — 记忆/复盘/工作空间/多通道。

覆盖需求文档中缺失的 AC：
- AC-MEM-05: 知识库导入后检索命中
- AC-REV-01: 自动复盘触发（阈值判断）
- AC-REV-07: 清理按决策表执行
- AC-REV-08: 双源标记一致性
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.service import MemoryService
from memory.types import Episode, Knowledge, MemoryType


# ============================================================
# AC-MEM-05: 知识库导入后检索命中
# ============================================================


class TestKnowledgeImportRetrieve:
    """验证知识库导入后能被检索命中。"""

    @pytest.mark.asyncio
    async def test_import_text_then_retrieve_keyword(self, tmp_path):
        """导入文本知识后，keyword 检索应命中。

        AC-MEM-05: 知识库导入后检索命中。
        """
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
        )

        # 导入知识
        kn = Knowledge(
            user_id="u1",
            content="FastAPI 是一个现代化的 Python Web 框架，支持异步请求处理",
            source_type="manual",
            extra_data={"tags": ["fastapi", "python"]},
        )
        await svc.store_knowledge(kn)

        # keyword 检索应命中
        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="FastAPI",
            top_k=5,
        )
        assert len(results) >= 1, "导入后 keyword 检索应命中"
        assert "FastAPI" in results[0].content

    @pytest.mark.asyncio
    async def test_import_text_then_retrieve_vector_fallback(self, tmp_path):
        """导入知识后，vector 检索在禁用时 fallback 到 keyword 也应命中。

        AC-MEM-05 + AC-MEM-02 关联验证。
        """
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
            config={"vector_search": {"enabled": False, "fallback_to_keyword": True}},
        )

        kn = Knowledge(
            user_id="u1",
            content="Docker 容器化部署最佳实践",
            source_type="manual",
            extra_data={"tags": ["docker", "devops"]},
        )
        await svc.store_knowledge(kn)

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="vector",
            query="Docker",
            top_k=5,
        )
        assert len(results) >= 1, "vector fallback keyword 应命中"

    @pytest.mark.asyncio
    async def test_import_text_then_retrieve_tagwave_fallback(self, tmp_path):
        """导入知识后，tagwave 检索 fallback 到 keyword 也应命中。

        AC-MEM-05 + AC-MEM-04 关联验证。
        """
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
            config={"vector_search": {"enabled": False, "fallback_to_keyword": True}},
        )

        kn = Knowledge(
            user_id="u1",
            content="Redis 缓存穿透解决方案：布隆过滤器",
            source_type="manual",
            extra_data={"tags": ["redis", "cache"]},
        )
        await svc.store_knowledge(kn)

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="tagwave",
            query="Redis",
            top_k=5,
        )
        assert len(results) >= 1, "tagwave fallback keyword 应命中"


# ============================================================
# AC-MEM-05 补充: 三种注入方式完整性验证
# ============================================================


class TestInjectTypes:
    """验证 FULL / RETRIEVAL / SUMMARY 三种注入方式。"""

    @pytest.mark.asyncio
    async def test_full_inject_returns_all_matching(self):
        """FULL 注入方式应委托给检索器返回所有匹配的知识。

        FULL 模式语义：将知识库完整注入上下文（无 query 过滤），
        委托给检索器执行。使用 mock 检索器验证委派正确。
        """
        from unittest.mock import AsyncMock
        from memory.types import SearchResult, MemoryType

        r = AsyncMock()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="知识1", score=1.0, memory_type=MemoryType.SEMANTIC),
            SearchResult(id="2", content="知识2", score=0.9, memory_type=MemoryType.SEMANTIC),
        ])
        svc = MemoryService(retrievers={"keyword": r})

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="full",
        )
        assert len(results) == 2
        r.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_retrieval_inject_with_query(self, tmp_path):
        """RETRIEVAL 注入方式需要 query 参数。"""
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
        )

        kn = Knowledge(
            user_id="u1",
            content="Python 类型注解是 PEP 484 引入的",
            source_type="manual",
        )
        await svc.store_knowledge(kn)

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="Python 类型注解",
        )
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_summary_inject_returns_results(self, tmp_path):
        """SUMMARY 注入方式应返回精简的结果。"""
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
        )

        kn = Knowledge(
            user_id="u1",
            content="数据库连接池的配置参数包括 max_pool_size 和 timeout",
            source_type="manual",
        )
        await svc.store_knowledge(kn)

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="summary",
            query="数据库",
        )
        assert len(results) >= 1


# ============================================================
# AC-MEM-05 补充: 会话级情景记忆自动记录
# ============================================================


class TestEpisodeAutoRecord:
    """验证会话级情景记忆自动记录（F-MEM-05）。"""

    @pytest.mark.asyncio
    async def test_store_episode_persists_across_sessions(self, tmp_path):
        """情景记忆存储后，后续可读取。"""
        from memory.storage.json_store import JsonMemoryStore

        store = JsonMemoryStore(data_dir=str(tmp_path / "memory"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
        )

        ep = Episode(
            user_id="u1",
            intent_text="帮用户重构认证模块",
            execution_summary="成功重构了 JWT 认证逻辑",
            tags=["auth", "refactor"],
        )
        eid = await svc.store_episode(ep)

        # 重新读取
        result = await svc.get_episode(eid, "u1")
        assert result is not None
        assert result["intent_text"] == "帮用户重构认证模块"
        assert result["execution_summary"] == "成功重构了 JWT 认证逻辑"

    @pytest.mark.asyncio
    async def test_store_episode_in_memory_no_loss(self):
        """无后端时情景记忆也不丢失（内存降级）。"""
        svc = MemoryService()

        ep = Episode(user_id="u1", intent_text="测试内存降级")
        eid = await svc.store_episode(ep)

        result = await svc.get_episode(eid, "u1")
        assert result is not None
        assert result["intent_text"] == "测试内存降级"


# ============================================================
# AC-REV-01: 自动复盘触发（阈值判断）
# ============================================================


class TestReviewAutoTrigger:
    """验证自动复盘触发逻辑。"""

    def test_should_trigger_when_pending_exists(self):
        """存在 pending 管道时应触发复盘。

        AC-REV-01: 自动复盘触发。
        """
        from src.memory.maintenance.service import MemoryMaintenanceService

        service = MemoryMaintenanceService(
            storage=MagicMock(),
            chunk_db=None,
            knowledge_service=MagicMock(),
        )

        # Mock review engine 返回 pending 列表
        mock_engine = MagicMock()
        mock_engine.get_pending_pipelines.return_value = ["run-1", "run-2"]
        service._review_engine = mock_engine

        assert service.should_trigger_review() is True

    def test_should_not_trigger_when_no_pending(self):
        """无 pending 管道且未超过间隔时不应触发。"""
        from src.memory.maintenance.service import MemoryMaintenanceService

        service = MemoryMaintenanceService(
            storage=MagicMock(),
            chunk_db=None,
            knowledge_service=MagicMock(),
        )

        mock_engine = MagicMock()
        mock_engine.get_pending_pipelines.return_value = []
        service._review_engine = mock_engine
        service._last_review_time = datetime.now(UTC)

        assert service.should_trigger_review() is False

    def test_should_trigger_when_interval_exceeded(self):
        """超过 max_interval 时应触发复盘（即使无 pending）。"""
        from src.memory.maintenance.service import MemoryMaintenanceService

        service = MemoryMaintenanceService(
            storage=MagicMock(),
            chunk_db=None,
            knowledge_service=MagicMock(),
        )

        mock_engine = MagicMock()
        mock_engine.get_pending_pipelines.return_value = []
        service._review_engine = mock_engine

        # 模拟上次复盘时间是很久以前，且 max_interval 设为很小值
        service._stats["last_review_at"] = (
            datetime.now(UTC) - timedelta(days=30)
        ).isoformat()
        service._config.review_max_interval = 60  # 60 秒

        assert service.should_trigger_review() is True


# ============================================================
# AC-REV-07: 清理按决策表执行
# ============================================================


class TestCleanupDecisionTable:
    """验证清理决策表正确执行。

    清理决策表：
                        年龄短(<7天)   年龄中(7-30天)   年龄老(>30天)
    COMPLETED   │   不清理   │   看容量     │     清理       │
    PENDING     │   不清理   │   不清理     │  触发复盘+清理 │
    """

    @pytest.mark.asyncio
    async def test_completed_old_pipeline_gets_cleaned(self):
        """已复盘（completed）+ 老管道（>30天）→ 应清理 L0+L1。

        AC-REV-07 修复验证：review_status="reviewed" 改为 "completed"。
        """
        from src.infrastructure.execution_record_storage import (
            ExecutionRecordData,
            ExecutionRecordStorage,
            PipelineRunSummary,
        )
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        # 模拟一个已复盘+老管道
        old_time = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        summary = MagicMock()
        summary.run_id = "run-old-reviewed"
        summary.created_at = old_time
        storage.list_all_summaries.return_value = [summary]

        # _get_review_status 返回 completed
        storage.get_summary.return_value = MagicMock(review_status="completed")
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])

        # Mock _get_capacity_pressure
        engine._get_capacity_pressure = MagicMock(return_value=0.1)

        # Mock _delete_pipeline_data
        engine._delete_pipeline_data = AsyncMock(return_value={
            "l0_deleted": 5, "l1_deleted": 2, "episodes_deleted": 0,
        })

        result = await engine.cleanup_by_age_and_capacity()

        assert result["status"] == "success"
        engine._delete_pipeline_data.assert_called_once()
        call_kwargs = engine._delete_pipeline_data.call_args
        assert call_kwargs[1]["delete_l1"] is True  # 老管道同时删 L0+L1

    @pytest.mark.asyncio
    async def test_completed_young_pipeline_not_cleaned(self):
        """已复盘 + 新管道（<7天）→ 不清理。"""
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        young_time = datetime.now(UTC).isoformat()
        summary = MagicMock()
        summary.run_id = "run-young"
        summary.created_at = young_time
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = MagicMock(review_status="completed")
        engine._get_capacity_pressure = MagicMock(return_value=0.1)
        engine._delete_pipeline_data = AsyncMock()

        result = await engine.cleanup_by_age_and_capacity()

        engine._delete_pipeline_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_old_pipeline_triggers_review_then_clean(self):
        """未复盘 + 老管道（>30天）→ 先触发复盘再清理。"""
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        old_time = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        summary = MagicMock()
        summary.run_id = "run-old-pending"
        summary.created_at = old_time
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = MagicMock(review_status="pending")
        engine._get_capacity_pressure = MagicMock(return_value=0.1)

        mock_review_engine = MagicMock()
        mock_review_engine._review_single_pipeline = AsyncMock()

        engine._delete_pipeline_data = AsyncMock(return_value={
            "l0_deleted": 1, "l1_deleted": 0, "episodes_deleted": 0,
        })

        result = await engine.cleanup_by_age_and_capacity(review_engine=mock_review_engine)

        mock_review_engine._review_single_pipeline.assert_called_once_with("run-old-pending")
        engine._delete_pipeline_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_pending_young_pipeline_not_cleaned(self):
        """未复盘 + 新管道（<7天）→ 不清理。"""
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        young_time = datetime.now(UTC).isoformat()
        summary = MagicMock()
        summary.run_id = "run-pending-young"
        summary.created_at = young_time
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = MagicMock(review_status="pending")
        engine._get_capacity_pressure = MagicMock(return_value=0.1)
        engine._delete_pipeline_data = AsyncMock()

        await engine.cleanup_by_age_and_capacity()

        engine._delete_pipeline_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_medium_age_with_capacity_pressure(self):
        """已复盘 + 中等年龄（7-30天）+ 容量紧张 → 删 L0。"""
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        medium_time = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        summary = MagicMock()
        summary.run_id = "run-medium"
        summary.created_at = medium_time
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = MagicMock(review_status="completed")
        engine._get_capacity_pressure = MagicMock(return_value=0.9)  # 容量紧张

        engine._delete_pipeline_data = AsyncMock(return_value={
            "l0_deleted": 1, "l1_deleted": 0, "episodes_deleted": 0,
        })

        result = await engine.cleanup_by_age_and_capacity()

        engine._delete_pipeline_data.assert_called_once()
        call_kwargs = engine._delete_pipeline_data.call_args
        assert call_kwargs[1]["delete_l1"] is False  # 只删 L0，不删 L1


# ============================================================
# AC-REV-08: 双源标记一致性
# ============================================================


class TestDualSourceMarking:
    """验证 PipelineRunSummary 和 ChunkMetadata 的 review_status 一致性。"""

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_both_sources(self):
        """复盘后 PipelineRunSummary 和 ChunkMetadata 都标记为 completed。

        AC-REV-08: 双源标记一致性。
        """
        from src.memory.maintenance.review_engine import (
            ChunkData,
            ReviewEngine,
        )

        storage = MagicMock()
        chunk_db = MagicMock()
        knowledge_service = MagicMock()

        engine = ReviewEngine(
            storage=storage,
            chunk_db=chunk_db,
            knowledge_service=knowledge_service,
        )

        run_id = "run-dual"
        chunk = ChunkData(
            chunk_id="c1",
            pipeline_id=run_id,
            layer="summary",
            content="data",
            extra_data={"reviewed": False},
        )
        chunk_db.find_by_pipeline = AsyncMock(return_value=[chunk])

        await engine._mark_pipeline_reviewed(run_id)

        # 验证 PipelineRunSummary 被更新
        storage.update_summary.assert_called_with(run_id, {"review_status": "completed"})

        # 验证 ChunkMetadata 被更新
        assert chunk.extra_data["reviewed"] is True
        chunk_db.save_chunk.assert_called_once_with(chunk)

    @pytest.mark.asyncio
    async def test_dual_source_read_consistency(self):
        """双源读取时 L0 summary 优先，L0 不存在则读 L1。"""
        from src.memory.maintenance.cleanup_engine import CleanupEngine

        storage = MagicMock()
        chunk_db = AsyncMock()
        config = MagicMock()
        config.cleanup_min_age_days = 30
        config.cleanup_early_age_days = 7
        config.cleanup_capacity_threshold = 0.8

        engine = CleanupEngine(
            storage=storage,
            chunk_db=chunk_db,
            memory_service=None,
            config=config,
        )

        # Case 1: L0 summary 存在，读 L0
        storage.get_summary.return_value = MagicMock(review_status="completed")
        status = await engine._get_review_status("run-1")
        assert status == "completed"

        # Case 2: L0 不存在，读 L1 chunk
        storage.get_summary.return_value = None
        chunk = MagicMock()
        chunk.extra_data = {"review_status": "completed"}
        chunk_db.find_by_pipeline = AsyncMock(return_value=[chunk])
        status = await engine._get_review_status("run-2")
        assert status == "completed"

        # Case 3: L0 和 L1 都不存在
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        status = await engine._get_review_status("run-3")
        assert status == "deleted"

    @pytest.mark.asyncio
    async def test_chunk_error_does_not_block_summary_update(self):
        """chunk 更新失败不影响 summary 的 review_status 更新。"""
        from src.memory.maintenance.review_engine import (
            ReviewEngine,
        )

        storage = MagicMock()
        chunk_db = MagicMock()
        knowledge_service = MagicMock()

        engine = ReviewEngine(
            storage=storage,
            chunk_db=chunk_db,
            knowledge_service=knowledge_service,
        )

        chunk_db.find_by_pipeline = AsyncMock(side_effect=RuntimeError("disk error"))

        await engine._mark_pipeline_reviewed("run-err")

        storage.update_summary.assert_called_once_with(
            "run-err", {"review_status": "completed"}
        )
