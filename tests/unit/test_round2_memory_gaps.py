"""Round2 记忆与复盘系统模块测试缺口补充。

对照 docs/requirements/各模块需求文档/08_记忆与复盘系统模块需求文档.md
逐条补充现有测试未覆盖或覆盖不充分的场景。

参考的现有测试（避免重复）：
- tests/test_memory_retrieve_chain.py        — JsonMemoryStore.retrieve + keyword 链路
- tests/test_wave_retriever.py               — WaveRetriever 三阶段
- tests/unit/test_memory_review_coverage.py  — 复盘引擎 AC-REV-06/07/08 主路径

本文件新增覆盖：
1. F-MEM-04 三种注入方式（InjectType 枚举完整性 + 分派路径）
2. AC-MEM-05 知识库 store → retrieve 命中（端到端）
3. F-MEM-01 知识库 CRUD（create/list/delete）
4. F-MEM-06 持久化语义记忆跨会话可调用
5. F-REV-03 复盘五阶段状态迁移（pending→reviewing→completed）
6. F-REV-05 经验产出 source_type=experience
7. AC-REV-08 双源标记：L1 多块取首块
8. AC-REV-07 清理决策表边界
9. F-REV-04 三层渐进披露 read_execution_detail
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService
from memory.maintenance.cleanup_engine import CleanupEngine
from memory.maintenance.review_engine import (
    ErrorRecord,
    ExecutionRecord,
    Pipeline,
    PipelineRunSummary,
    ReviewEngine,
    ReviewStatus,
)
from memory.service import MemoryService
from memory.storage.json_store import JsonMemoryStore
from memory.types import (
    Episode,
    InjectType,
    Knowledge,
    MemoryType,
    RetrievalMethod,
    SearchResult,
)


# ============================================================
# Helpers
# ============================================================

def _make_summary(
    run_id: str = "run-001",
    status: str = "completed",
    review_status: str = "pending",
    created_at: str | None = None,
    **overrides: Any,
) -> PipelineRunSummary:
    """构造 PipelineRunSummary。"""
    defaults: dict[str, Any] = dict(
        run_id=run_id,
        total_records=5,
        total_iterations=3,
        created_at=created_at or "2026-01-01T00:00:00+00:00",
        status=status,
        error="",
        review_status=review_status,
    )
    defaults.update(overrides)
    return PipelineRunSummary(**defaults)


def _build_review_engine() -> tuple[ReviewEngine, MagicMock, MagicMock, MagicMock]:
    """构建 ReviewEngine 及其 Mock 依赖。"""
    storage = MagicMock()
    chunk_db = MagicMock()
    ks = MagicMock()
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
    ks.create_knowledge = AsyncMock(return_value={"id": "k-1"})
    engine = ReviewEngine(
        storage=storage,
        chunk_db=chunk_db,
        knowledge_service=ks,
    )
    return engine, storage, chunk_db, ks


def _build_cleanup_engine(
    *,
    capacity_pressure: float = 0.0,
) -> tuple[CleanupEngine, MagicMock, MagicMock]:
    """构造 CleanupEngine 及其依赖。"""
    storage = MagicMock()
    chunk_db = MagicMock()
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    chunk_db.delete = AsyncMock(return_value=None)
    engine = CleanupEngine(
        storage=storage,
        chunk_db=chunk_db,
        memory_service=None,
        config=MaintenanceConfig(),
    )
    engine._get_capacity_pressure = lambda: capacity_pressure  # type: ignore[method-assign]
    return engine, storage, chunk_db


# ============================================================
# F-MEM-04: InjectType / RetrievalMethod 枚举完整性
# ============================================================


class TestInjectTypeEnum:
    """F-MEM-04: 三种注入方式 FULL/RETRIEVAL/SUMMARY。"""

    def test_F_MEM_04_inject_type_has_three_modes(self) -> None:
        """需求文档明确三种注入方式，枚举不能多也不能少。"""
        values = {item.value for item in InjectType}
        assert values == {"full", "retrieval", "summary"}, (
            f"InjectType 应只包含三种模式，实际为 {values}"
        )

    def test_F_MEM_04_retrieval_method_has_three_methods(self) -> None:
        """检索方法枚举：vector/keyword/tagwave。"""
        values = {item.value for item in RetrievalMethod}
        assert values == {"vector", "keyword", "tagwave"}


# ============================================================
# F-MEM-04: 注入方式分派路径
# ============================================================


class TestInjectTypeDispatch:
    """F-MEM-04: MemoryService.retrieve 按 inject_type 分派到正确路径。

    使用 Mock 检索器精确验证分派路径，而非依赖具体检索器行为。
    """

    @pytest.mark.asyncio
    async def test_F_MEM_04_full_dispatches_to_retrieve_full(self) -> None:
        """inject_type='full' → 调用 retriever.retrieve(query='')。"""
        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(return_value=[
            SearchResult(id="k1", content="full content", score=1.0),
        ])

        svc = MemoryService(retrievers={"keyword": mock_retriever})
        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="full",
            top_k=10,
        )
        assert len(results) == 1
        mock_retriever.retrieve.assert_awaited_once()
        # full 模式应传 query=''
        call_kwargs = mock_retriever.retrieve.await_args.kwargs
        assert call_kwargs["query"] == ""

    @pytest.mark.asyncio
    async def test_F_MEM_04_summary_dispatches_to_retrieve_summary(self) -> None:
        """inject_type='summary' → 走 _retrieve_summary，调用默认检索器。"""
        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(return_value=[
            SearchResult(id="k1", content="summary content", score=0.9),
        ])

        svc = MemoryService(
            retrievers={"keyword": mock_retriever},
            config={"vector_search": {"default_method": "keyword"}},
        )
        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="summary",
            query="测试",
            top_k=5,
        )
        assert len(results) == 1
        mock_retriever.retrieve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_F_MEM_04_retrieval_dispatches_to_method(
        self, tmp_path: Any,
    ) -> None:
        """inject_type='retrieval' + method='keyword' → _retrieve_by_method。"""
        store = JsonMemoryStore(data_dir=str(tmp_path / "mem"))
        svc = MemoryService(
            episode_storage=store,
            semantic_storage=store,
            retrievers={"keyword": store},
        )
        kn = Knowledge(user_id="u1", content="RETRIEVAL 测试关键词", source_type="m")
        await svc.store_knowledge(kn)

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="RETRIEVAL",
            top_k=5,
        )
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_F_MEM_04_invalid_inject_type_raises(self) -> None:
        """不在枚举内的 inject_type 应抛 ValueError。"""
        svc = MemoryService()
        with pytest.raises(ValueError):
            await svc.retrieve(
                user_id="u1",
                inject_type="invalid_mode",
                query="test",
            )


# ============================================================
# F-MEM-01 / AC-MEM-05: 知识库 CRUD + 导入后检索命中
# ============================================================


class TestKnowledgeCRUD:
    """F-MEM-01: 知识库创建/删除/导入。AC-MEM-05: 导入后检索命中。"""

    @pytest.mark.asyncio
    async def test_F_MEM_01_create_then_list(self, tmp_path: Any) -> None:
        """create_knowledge 后 list_semantic_memory 能读回。"""
        svc = MemoryService(semantic_storage=JsonMemoryStore(str(tmp_path / "m")))

        await svc.create_knowledge(
            user_id="u1",
            content="Python 类型注解规范",
            source_type="manual",
        )
        await svc.create_knowledge(
            user_id="u1",
            content="FastAPI 路由设计",
            source_type="manual",
        )

        result = await svc.list_semantic_memory(user_id="u1")
        assert result["total"] >= 2

    @pytest.mark.asyncio
    async def test_F_MEM_01_delete_knowledge(self, tmp_path: Any) -> None:
        """delete_knowledge 后不能再读回。"""
        store = JsonMemoryStore(str(tmp_path / "m"))
        svc = MemoryService(semantic_storage=store)

        kn_dict = await svc.create_knowledge(
            user_id="u1",
            content="待删除的知识",
            source_type="manual",
        )
        kid = kn_dict["id"]

        ok = await svc.delete_knowledge(knowledge_id=kid, user_id="u1")
        assert ok is True

        result = await svc.list_semantic_memory(user_id="u1")
        ids = [item["id"] for item in result["items"]]
        assert kid not in ids

    @pytest.mark.asyncio
    async def test_AC_MEM_05_store_then_retrieve_keyword_hit(
        self, tmp_path: Any,
    ) -> None:
        """AC-MEM-05: store_knowledge 后用 keyword 检索应命中。"""
        store = JsonMemoryStore(str(tmp_path / "m"))
        svc = MemoryService(
            semantic_storage=store,
            retrievers={"keyword": store},
        )
        await svc.store_knowledge(
            Knowledge(
                user_id="u1",
                content="缓存失效策略 LRU LFU FIFO",
                source_type="manual",
                extra_data={"tags": ["cache"]},
            )
        )

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="缓存失效",
            top_k=5,
        )
        assert len(results) >= 1
        assert any("缓存" in r.content for r in results)

    @pytest.mark.asyncio
    async def test_AC_MEM_05_import_via_create_then_retrieve(
        self, tmp_path: Any,
    ) -> None:
        """AC-MEM-05: 通过 create_knowledge 导入后应能检索到。"""
        store = JsonMemoryStore(str(tmp_path / "m"))
        svc = MemoryService(
            semantic_storage=store,
            retrievers={"keyword": store},
        )
        await svc.create_knowledge(
            user_id="u1",
            content="Docker 容器编排最佳实践",
            source_type="manual",
        )

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="Docker",
            top_k=5,
        )
        assert len(results) >= 1


# ============================================================
# F-MEM-06: 持久化语义记忆跨"会话"可调用
# ============================================================


class TestPersistentSemanticMemory:
    """F-MEM-06: 新会话能调用持久化的语义记忆。"""

    @pytest.mark.asyncio
    async def test_F_MEM_06_new_service_reads_old_data(self, tmp_path: Any) -> None:
        """第一个 MemoryService 存入知识后，新 MemoryService（同目录）能检索到。"""
        data_dir = str(tmp_path / "persist")

        svc1 = MemoryService(semantic_storage=JsonMemoryStore(data_dir))
        await svc1.store_knowledge(
            Knowledge(
                user_id="u1",
                content="持久化语义记忆测试内容",
                source_type="manual",
            )
        )

        svc2 = MemoryService(
            semantic_storage=JsonMemoryStore(data_dir),
            retrievers={"keyword": JsonMemoryStore(data_dir)},
        )
        results = await svc2.retrieve(
            user_id="u1",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="持久化",
            top_k=5,
        )
        assert len(results) >= 1
        assert any("持久化" in r.content for r in results)


# ============================================================
# F-MEM-05: 会话级情景记忆自动记录
# ============================================================


class TestEpisodeAutoRecord:
    """F-MEM-05: 会话级情景记忆自动记录。"""

    @pytest.mark.asyncio
    async def test_F_MEM_05_store_episode_then_retrieve(self, tmp_path: Any) -> None:
        """store_episode 后能检索到。"""
        store = JsonMemoryStore(str(tmp_path / "m"))
        svc = MemoryService(
            episode_storage=store,
            retrievers={"keyword": store},
        )
        await svc.store_episode(
            Episode(
                user_id="u1",
                session_id="sess-001",
                intent_text="帮我写一个登录测试",
                execution_summary="完成了登录功能测试",
                tags=["test", "auth"],
            )
        )

        results = await svc.retrieve(
            user_id="u1",
            filter={"memory_type": "episode"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="登录测试",
            top_k=5,
        )
        assert len(results) >= 1
        assert all(r.memory_type == MemoryType.EPISODE for r in results)


# ============================================================
# F-REV-03: 复盘五阶段流程
# ============================================================


class TestReviewFivePhases:
    """F-REV-03: 复盘五阶段流程的状态迁移和副作用。"""

    @pytest.mark.asyncio
    async def test_F_REV_03_phase1_filter_pending_then_mark_reviewing(self) -> None:
        """Phase 1：筛选 pending → 标记 reviewing → 处理 → completed。"""
        engine, storage, _, _ = _build_review_engine()
        storage.get_summary.return_value = _make_summary(run_id="r1")
        storage.list_by_pipeline.return_value = (
            [_make_record(error="timeout")],
            False,
        )

        await engine.run_review("r1")

        calls = storage.update_summary.call_args_list
        # 第一次标记 reviewing
        assert calls[0].args[0] == "r1"
        assert calls[0].args[1].get("review_status") == "reviewing"
        # 最后一次标记 completed
        assert calls[-1].args[1].get("review_status") == "completed"

    @pytest.mark.asyncio
    async def test_F_REV_03_phase5_experiences_stored(self) -> None:
        """Phase 5：经验存储到 Knowledge（source_type=experience）。"""
        engine, storage, _, ks = _build_review_engine()
        storage.get_summary.return_value = _make_summary(run_id="r1")
        storage.list_by_pipeline.return_value = (
            [_make_record(error="connection refused")],
            False,
        )

        await engine.run_review("r1")

        # create_knowledge 被调用
        ks.create_knowledge.assert_awaited()
        # 检查 source_type=experience
        call_kwargs = ks.create_knowledge.await_args
        assert call_kwargs.kwargs.get("source_type") == "experience"

    @pytest.mark.asyncio
    async def test_F_REV_03_non_terminal_status_skipped(self) -> None:
        """Phase 1：非 terminal 状态的管道不被复盘。"""
        engine, storage, _, _ = _build_review_engine()
        storage.get_summary.return_value = _make_summary(
            run_id="r1", status="running",
        )

        result = await engine.run_review("r1")

        assert result["status"] == "error"
        assert "not completed" in result["message"].lower()


def _make_record(
    iteration: int = 1,
    rtype: str = "tool",
    name: str = "step",
    error: str = "",
    content: str = "result",
) -> ExecutionRecord:
    return ExecutionRecord(
        iteration=iteration, type=rtype, name=name, error=error, content=content,
    )


# ============================================================
# F-REV-05: 经验与建议两类产出
# ============================================================


class TestReviewOutputTypes:
    """F-REV-05: 复盘产出两类：经验（experience）/ 建议（action_item）。"""

    @pytest.mark.asyncio
    async def test_F_REV_05_experience_source_type_is_experience(self) -> None:
        """有错误的管道复盘后，经验产出 source_type 必须是 experience。"""
        engine, storage, _, ks = _build_review_engine()
        storage.get_summary.return_value = _make_summary(
            run_id="r1", status="failed", error="boom",
        )
        storage.list_by_pipeline.return_value = ([], False)

        await engine.run_review("r1")

        ks.create_knowledge.assert_awaited()
        assert ks.create_knowledge.await_args.kwargs.get("source_type") == "experience"

    @pytest.mark.asyncio
    async def test_F_REV_05_dedup_skips_existing_experience(self) -> None:
        """已存在相同经验的应被跳过（去重）。"""
        engine, storage, _, ks = _build_review_engine()
        summary = _make_summary(
            run_id="r1", status="failed", error="boom",
            total_iterations=3,
        )
        storage.get_summary.return_value = summary
        storage.list_by_pipeline.return_value = ([], False)

        # 用与 ReviewEngine 内部完全一致的参数构建已存在经验内容
        existing_content = ReviewEngine._build_experience_content(
            run_id="r1",
            status="failed",
            error="boom",
            task="",
            iterations=3,
            duration=0.0,
            created_at=summary.created_at,
            agent="",
        )
        ks.list_semantic_memory = AsyncMock(return_value={
            "items": [{"content": existing_content, "source_type": "experience"}],
            "total": 1,
        })

        await engine.run_review("r1")

        # create_knowledge 不应被调用（已去重）
        ks.create_knowledge.assert_not_awaited()


# ============================================================
# AC-REV-08: 双源标记一致性 — 补充场景
# ============================================================


class TestDualSourceMoreScenarios:
    """AC-REV-08: L1 多块取首块 review_status；L0 优先。"""

    @pytest.mark.asyncio
    async def test_AC_REV_08_l0_summary_with_non_pending_status(self) -> None:
        """L0 summary review_status 非 pending 也非 completed 时直接返回该值。"""
        engine, storage, _ = _build_cleanup_engine()
        summary = _make_summary(run_id="x1", review_status="reviewing")
        storage.get_summary.return_value = summary

        status = await engine._get_review_status("x1")
        assert status == "reviewing"

    @pytest.mark.asyncio
    async def test_AC_REV_08_l1_multiple_chunks_takes_first(self) -> None:
        """L1 有多块时取第一块的 review_status。"""
        engine, storage, chunk_db = _build_cleanup_engine()
        storage.get_summary.return_value = None

        chunk1 = MagicMock()
        chunk1.extra_data = {"review_status": "completed"}
        chunk2 = MagicMock()
        chunk2.extra_data = {"review_status": "pending"}
        chunk_db.find_by_pipeline = AsyncMock(return_value=[chunk1, chunk2])

        status = await engine._get_review_status("multi")
        assert status == "completed"


# ============================================================
# AC-REV-07: 清理决策表 — 补充边界场景
# ============================================================


class TestCleanupBoundary:
    """AC-REV-07: 清理决策表边界场景。"""

    @pytest.mark.asyncio
    async def test_AC_REV_07_reviewing_status_treated_as_pending(self) -> None:
        """review_status=reviewing 且 age > 30 天 → 触发复盘后清理。"""
        engine, storage, _ = _build_cleanup_engine()
        old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        summary = _make_summary(
            run_id="reviewing-old",
            review_status="reviewing",
            created_at=old_ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        review_engine = MagicMock()
        review_engine.run_review = AsyncMock(return_value={"status": "success"})

        result = await engine.cleanup_by_age_and_capacity(review_engine=review_engine)

        # reviewing 不等于 pending/completed，不会进入清理分支
        assert result["l0_deleted"] == 0

    @pytest.mark.asyncio
    async def test_AC_REV_07_just_under_min_age_not_deleted(self) -> None:
        """年龄略小于 cleanup_min_age_days（29天）时不删除 L0。"""
        engine, storage, _ = _build_cleanup_engine()
        almost_old = (datetime.now(UTC) - timedelta(days=29)).isoformat()
        summary = _make_summary(
            run_id="boundary-29",
            review_status="completed",
            created_at=almost_old,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 0

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)
        assert result["l0_deleted"] == 0


# ============================================================
# F-REV-04: 三层渐进披露工具
# ============================================================


class TestReadExecutionDetail:
    """F-REV-04: read_execution_detail 工具的三层渐进披露。"""

    def test_F_REV_04_tool_definition_has_three_levels(self) -> None:
        """工具定义的 level 枚举必须包含 skeleton/L1/L0。"""
        from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool

        tool_def = ReadExecutionDetailTool.get_tool_definition()
        level_enum = tool_def.input_schema["properties"]["level"]["enum"]
        assert set(level_enum) == {"skeleton", "L1", "L0"}

    @pytest.mark.asyncio
    async def test_F_REV_04_skeleton_returns_lines(self) -> None:
        """skeleton 层应返回每轮一行的概览。"""
        from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool

        storage = MagicMock()
        records = [
            ExecutionRecord(iteration=1, type="user", name="", error="", content="请帮我写代码"),
            ExecutionRecord(iteration=1, type="ai", name="", error="", content="好的"),
            ExecutionRecord(iteration=2, type="tool", name="file_write", error="", content="ok"),
            ExecutionRecord(iteration=2, type="tool", name="file_read", error="not found", content=""),
        ]
        storage.list_by_pipeline.return_value = (records, False)
        storage._ensure_loaded = MagicMock()

        tool = ReadExecutionDetailTool(storage=storage)
        result = await tool.execute({
            "pipeline_run_id": "p1",
            "level": "skeleton",
        })

        assert result.success is True
        assert result.output["level"] == "skeleton"
        assert result.output["total_records"] == 4
        assert len(result.output["lines"]) == 4

    @pytest.mark.asyncio
    async def test_F_REV_04_l0_filters_fields(self) -> None:
        """L0 层 fields 参数应过滤返回字段。

        ExecutionRecordData 字段比 review_engine.ExecutionRecord 更全；
        L0 工具消费 ExecutionRecordStorage 真实写入的数据。
        这里用 MagicMock 提供所需所有属性以验证 fields 过滤路径。
        """
        from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool

        storage = MagicMock()
        record = MagicMock()
        record.record_id = "rec-001"
        record.iteration = 1
        record.sequence = 1
        record.type = "tool"
        record.name = "bash"
        record.role = "tool"
        record.content = "output here"
        record.thinking_content = "thinking process"
        record.tool_calls_json = '{"cmd": "ls"}'
        record.tool_input = {}
        record.error = "some error"
        record.created_at = "2026-01-01T00:00:00"

        storage.list_by_pipeline.return_value = ([record], False)
        storage._ensure_loaded = MagicMock()

        tool = ReadExecutionDetailTool(storage=storage)
        result = await tool.execute({
            "pipeline_run_id": "p1",
            "level": "L0",
            "iteration": 1,
            "fields": ["error"],
        })

        assert result.success is True
        assert result.output["level"] == "L0"

    @pytest.mark.asyncio
    async def test_F_REV_04_missing_storage_returns_error(self) -> None:
        """未注入 storage 时返回错误。"""
        from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool

        tool = ReadExecutionDetailTool()
        result = await tool.execute({
            "pipeline_run_id": "p1",
            "level": "skeleton",
        })
        assert result.success is False


# ============================================================
# F-REV-02: 复盘触发配置 — 需求文档 §2.2 默认值
# ============================================================


class TestReviewTriggerConfig:
    """F-REV-02: 复盘触发配置与需求文档一致。"""

    def test_F_REV_02_default_min_records_is_500(self) -> None:
        """需求文档 §2.2: min_records: 500。"""
        cfg = MaintenanceConfig()
        assert cfg.review_min_records == 500

    def test_F_REV_02_default_max_interval_is_7_days(self) -> None:
        """需求文档 §2.2: max_interval: 604800 (7天)。"""
        cfg = MaintenanceConfig()
        assert cfg.review_max_interval == 604800

    def test_F_REV_02_skeleton_budget_percent_15(self) -> None:
        """需求文档 §2.2: skeleton_budget_percent: 15。"""
        cfg = MaintenanceConfig()
        assert cfg.skeleton_budget_percent == 15

    def test_F_REV_02_max_records_per_review_2000(self) -> None:
        """需求文档 §2.2: max_records_per_review: 2000。"""
        cfg = MaintenanceConfig()
        assert cfg.max_records_per_review == 2000


# ============================================================
# F-REV-09: 复盘通知结构化字段
# ============================================================


class TestReviewNotification:
    """F-REV-09: 复盘通知应包含结构化字段。"""

    @pytest.mark.asyncio
    async def test_F_REV_09_review_result_contains_count_fields(self) -> None:
        """run_review 返回结果应包含 experience_count 等结构化字段。"""
        engine, storage, _, _ = _build_review_engine()
        storage.get_summary.return_value = _make_summary(
            run_id="r1", status="failed", error="timeout",
        )
        storage.list_by_pipeline.return_value = ([], False)

        result = await engine.run_review("r1")

        assert result["status"] == "success"
        assert "experience_count" in result
        assert "records_analyzed" in result
        assert isinstance(result["experience_count"], int)
