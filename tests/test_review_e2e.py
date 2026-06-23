"""复盘系统端到端集成测试。

用真实的 ExecutionRecordStorage（临时目录 + YAML 持久化）+ 真实的 MemoryMaintenanceService
+ 内存 KnowledgeService，验证"触发 → 拿真实管道记录 → 复盘 → 产出经验成果"整条链路。

这组测试存在意义：之前 ReviewEngine 有两个真实缺陷导致真实数据复盘产不出任何经验：
1. _run_review_full 只认 status=="completed"，但 track 插件写 success/failed
2. 只看 record.error，真实数据里该字段全空，错误只在 summary.error
修复后此文件锁定端到端行为，防止回归。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
    PipelineRunSummary,
)
from src.memory.maintenance.review_engine import ReviewEngine
from src.memory.maintenance.service import MemoryMaintenanceService


class _InMemoryKnowledgeService:
    """内存版 KnowledgeService，模拟真实知识库落盘。

    复刻真实 KnowledgeService 的 create_knowledge / list_semantic_memory 契约，
    让端到端测试不依赖真实数据库，但验证经验真实被"写入"。
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._next_id = 1

    async def create_knowledge(
        self,
        user_id: str,
        content: str,
        source_type: str,
        extra_data: dict | None = None,
    ) -> dict:
        item = {
            "id": f"k-{self._next_id}",
            "user_id": user_id,
            "content": content,
            "source_type": source_type,
            "extra_data": extra_data or {},
        }
        self._items.append(item)
        self._next_id += 1
        return {"id": item["id"], "status": "created"}

    async def list_semantic_memory(self, user_id: str) -> dict:
        items = [i for i in self._items if i["user_id"] == user_id]
        return {"items": items, "total": len(items)}


def _build_real_storage(tmpdir: Path) -> ExecutionRecordStorage:
    """构造真实 ExecutionRecordStorage，数据落盘到临时目录。"""
    return ExecutionRecordStorage(data_dir=str(tmpdir))


def _seed_pipeline(
    storage: ExecutionRecordStorage,
    run_id: str,
    *,
    status: str,
    summary_error: str | None = None,
    records: list[ExecutionRecordData] | None = None,
) -> None:
    """往真实 storage 写入一条管道（summary + records），模拟 track 插件产出。"""
    summary = PipelineRunSummary(
        run_id=run_id,
        status=status,
        error=summary_error,
        review_status="pending",
    )
    storage.save_summary(summary)
    for rec in records or []:
        storage.save(rec)


# ===========================================================================
# 场景1：failed 状态 + summary.error + record.error 全空（真实数据最常见）
# 这是修复的核心验证点：原实现会返回 experience_count=0
# ===========================================================================


class TestReviewProducesExperienceFromSummaryError:
    """验证：错误记在 summary.error 时，复盘能产出经验（真实数据场景）。"""

    async def test_failed_pipeline_with_summary_error_produces_experience(self, tmp_path):
        """failed + summary.error 非空 + 无记录级错误 → 产出 1 条经验。

        真实数据样本：run_id=3bf4660f59d2, status=failed,
        error='litellm.AuthenticationError: ... ZAI_API_KEY 未设置'
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="run-auth-fail",
            status="failed",
            summary_error="litellm.AuthenticationError: ZAI_API_KEY 未设置",
            records=[
                ExecutionRecordData(
                    record_id="r1",
                    pipeline_run_id="run-auth-fail",
                    type="ai",
                    error=None,  # 真实数据：记录级 error 全空
                    content="尝试调用 LLM",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        result = await service.trigger_review_now(force=False)

        assert result["pending_count"] == 1
        assert result["pipelines_reviewed"] == 1
        assert result["experiences_saved"] == 1
        # 验证经验真实写入知识库，内容包含 summary.error
        assert len(ks._items) == 1
        assert "AuthenticationError" in ks._items[0]["content"]
        assert ks._items[0]["source_type"] == "review_experience"
        # 验证 review_status 被标记为 completed
        assert storage.get_summary("run-auth-fail").review_status == "completed"


# ===========================================================================
# 场景2：success 状态 + summary.error（部分真实数据）
# 验证 success 不再被错误拒绝（原 bug：只认 completed）
# ===========================================================================


class TestReviewAcceptsSuccessStatus:
    """验证：success 状态的 pipeline 不再被拒。"""

    async def test_success_pipeline_with_error_is_reviewed(self, tmp_path):
        """success + summary.error → 正常复盘并产出经验。"""
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="run-success-with-err",
            status="success",
            summary_error="部分工具调用超时",
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        result = await service.trigger_review_now(force=False)

        assert result["pipelines_reviewed"] == 1
        assert result["experiences_saved"] == 1


# ===========================================================================
# 场景3：success + 无任何错误 → 不产经验但正常标记已复盘
# 验证大多数 success pipeline 的处理（不产出但推进状态）
# ===========================================================================


class TestReviewNoErrorProducesNoExperience:
    """验证：无错误的 pipeline 不产经验，但正常标记已复盘。"""

    async def test_clean_success_pipeline_reviewed_without_experience(self, tmp_path):
        """success + 无 error → experience_count=0，review_status=completed。"""
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="run-clean",
            status="success",
            summary_error=None,
            records=[
                ExecutionRecordData(
                    record_id="r1",
                    pipeline_run_id="run-clean",
                    type="ai",
                    error=None,
                    content="正常执行",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        result = await service.trigger_review_now(force=False)

        assert result["pipelines_reviewed"] == 1
        assert result["experiences_saved"] == 0
        assert len(ks._items) == 0
        assert storage.get_summary("run-clean").review_status == "completed"


# ===========================================================================
# 场景4：批量复盘多个 pending（真实数据 196 个 pending 的缩影）
# ===========================================================================


class TestReviewBatchFromRealisticStorage:
    """验证：批量处理多个 pending pipeline，混合错误来源。"""

    async def test_mixed_batch_produces_correct_experience_count(self, tmp_path):
        """3 个 pending（1 failed+summary.err / 1 success+record.err / 1 clean）→ 产 2 条经验。"""
        storage = _build_real_storage(tmp_path)

        # pipeline A: failed，错误在 summary
        _seed_pipeline(
            storage, run_id="batch-a", status="failed",
            summary_error="连接数据库失败",
        )
        # pipeline B: success，但某条 record 有错误
        _seed_pipeline(
            storage, run_id="batch-b", status="success",
            summary_error=None,
            records=[
                ExecutionRecordData(
                    record_id="r-b1", pipeline_run_id="batch-b",
                    type="tool", name="fetch_data", error="timeout 30s",
                ),
            ],
        )
        # pipeline C: 干净的 success
        _seed_pipeline(
            storage, run_id="batch-c", status="success",
            summary_error=None,
        )

        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        result = await service.trigger_review_now(force=False)

        assert result["pending_count"] == 3
        assert result["pipelines_reviewed"] == 3
        # A 产 1 条（summary.error 兜底）+ B 产 1 条（record.error）+ C 产 0 条
        assert result["experiences_saved"] == 2
        assert len(ks._items) == 2
        # 三个都标记已复盘
        for rid in ("batch-a", "batch-b", "batch-c"):
            assert storage.get_summary(rid).review_status == "completed"


# ===========================================================================
# 场景5：去重——同一错误重复复盘不重复写入
# ===========================================================================


class TestReviewDeduplication:
    """验证：已复盘的经验不重复写入（_load_existing_experiences 去重）。"""

    async def test_repeated_review_does_not_duplicate(self, tmp_path):
        """同一 pipeline 重新 pending 后再复盘，相同 error 不重复写入。

        模拟场景：复盘后某种原因 review_status 又变回 pending（如手动重置），
        再次复盘不应产出重复经验。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage, run_id="run-dedup", status="failed",
            summary_error="重复出现的错误",
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        # 第一次复盘
        result1 = await service.trigger_review_now(force=False)
        assert result1["experiences_saved"] == 1
        assert len(ks._items) == 1

        # 手动重置 review_status 模拟"再次 pending"
        storage.update_summary("run-dedup", {"review_status": "pending"})

        # 第二次复盘——相同 error 应被去重
        result2 = await service.trigger_review_now(force=False)
        assert result2["experiences_saved"] == 0
        assert len(ks._items) == 1  # 没有新增


# ===========================================================================
# 场景6：经验产出带任务描述/规模/时间（可读性验证）
# 这是本次改进的核心：让经验不再是 "Pipeline {hash} - {error}" 的晦涩格式
# ===========================================================================


class TestExperienceContentReadability:
    """验证：经验产出包含任务描述、执行规模、时间，让人一眼看懂是什么任务。"""

    async def test_experience_includes_task_description(self, tmp_path):
        """经验内容必须包含首条 user 消息作为任务描述。"""
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="run-with-task",
            status="failed",
            summary_error="litellm.AuthenticationError: ZAI_API_KEY 未设置",
            records=[
                ExecutionRecordData(
                    record_id="r1",
                    pipeline_run_id="run-with-task",
                    type="user",
                    role="user",
                    content="你好，请用一句话介绍一下你自己",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        await service.trigger_review_now(force=False)

        assert len(ks._items) == 1
        content = ks._items[0]["content"]
        # 任务描述必须出现在经验里
        assert "你好，请用一句话介绍一下你自己" in content, (
            f"经验应包含任务描述，实际: {content!r}"
        )
        # pipeline=run_id 保留在末尾便于追溯
        assert "pipeline=run-with-task" in content

    async def test_experience_includes_status_and_error(self, tmp_path):
        """经验内容必须包含状态和错误信息。"""
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="run-err-info",
            status="failed",
            summary_error="数据库连接超时",
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        await service.trigger_review_now(force=False)

        content = ks._items[0]["content"]
        assert "[failed" in content or "failed" in content
        assert "数据库连接超时" in content

    async def test_experience_format_readable_not_just_hash(self, tmp_path):
        """经验格式应可读，不再是 "Pipeline {hash} - {error}" 晦涩格式。

        防回归：早期格式是 'Pipeline 3bf4660f59d2 - failed: xxx'，
        hash 看不出是什么任务。改进后应带 [状态] 头部 + 任务描述。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage,
            run_id="abc123def456",
            status="failed",
            summary_error="连接错误",
            records=[
                ExecutionRecordData(
                    record_id="r1",
                    pipeline_run_id="abc123def456",
                    type="user",
                    role="user",
                    content="测试评估指标",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        await service.trigger_review_now(force=False)

        content = ks._items[0]["content"]
        # 不应以 "Pipeline {hash} -" 开头（旧格式）
        assert not content.startswith("Pipeline abc123def456 -"), (
            "不应是旧的晦涩格式 'Pipeline {hash} -'"
        )
        # 应包含任务描述
        assert "测试评估指标" in content


# ===========================================================================
# 场景7：agent 身份注入（task_lookup 回调）
# 真实数据约 58% 的管道能反查到 agent，34% 是纯对话管道没有 task。
# 三档完整度：有 agent > 有任务描述 > 只有状态错误。
# ===========================================================================


class TestAgentIdentityInExperience:
    """验证：task_lookup 回调能把目标 agent 带进经验产出。"""

    async def test_experience_includes_agent_when_task_lookup_returns_it(self, tmp_path):
        """task_lookup 返回 agent → 经验头部带 agent 身份。

        模拟真实场景：pipeline_run_id 通过 root_map 反查到 task，
        task.metadata.target_id = 'solution_planning_agent'。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage, run_id="run-with-agent", status="failed",
            summary_error="鉴权失败",
        )
        ks = _InMemoryKnowledgeService()

        # task_lookup 回调：返回真实 task 数据形态
        def task_lookup(pipeline_run_id):
            if pipeline_run_id == "run-with-agent":
                return {"agent": "solution_planning_agent", "title": "设计多设备兼容方案"}
            return None

        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
            task_lookup=task_lookup,
        )

        await service.trigger_review_now(force=False)

        assert len(ks._items) == 1
        content = ks._items[0]["content"]
        # agent 身份必须在头部
        assert "solution_planning_agent" in content, (
            f"经验应包含目标 agent，实际: {content!r}"
        )
        # task_lookup 返回的 title 应作为任务描述（比 user 消息更规范）
        assert "设计多设备兼容方案" in content

    async def test_experience_falls_back_when_task_lookup_returns_none(self, tmp_path):
        """task_lookup 返回 None（纯对话管道）→ 用 user 消息兜底，无 agent 但不崩。

        真实数据 34% 的管道是这种情形：没有对应 task，直接对话产生。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage, run_id="run-no-task", status="failed",
            summary_error="超时",
            records=[
                ExecutionRecordData(
                    record_id="r1", pipeline_run_id="run-no-task",
                    type="user", role="user",
                    content="帮我查一下天气",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()

        # task_lookup 永远返回 None（纯对话管道）
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
            task_lookup=lambda rid: None,
        )

        await service.trigger_review_now(force=False)

        content = ks._items[0]["content"]
        # 无 agent，但有任务描述兜底
        assert "帮我查一下天气" in content
        # 不应出现 "agent:" 之类残留
        assert content.startswith("[failed")

    async def test_experience_works_without_task_lookup(self, tmp_path):
        """完全不传 task_lookup → 行为与之前一致，用 user 消息兜底。

        保证向后兼容：老代码不传 task_lookup 也能正常产出经验。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage, run_id="run-legacy", status="failed",
            summary_error="连接错误",
            records=[
                ExecutionRecordData(
                    record_id="r1", pipeline_run_id="run-legacy",
                    type="user", role="user",
                    content="测试任务",
                ),
            ],
        )
        ks = _InMemoryKnowledgeService()
        # 不传 task_lookup
        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
        )

        await service.trigger_review_now(force=False)

        content = ks._items[0]["content"]
        assert "连接错误" in content
        assert "测试任务" in content

    async def test_task_lookup_exception_does_not_break_review(self, tmp_path):
        """task_lookup 抛异常 → 复盘不崩，降级为无 agent。

        回调可能因 task 文件损坏、task_service 未就绪等原因失败，
        ReviewEngine 应吞掉异常继续产出经验（带任务描述兜底）。
        """
        storage = _build_real_storage(tmp_path)
        _seed_pipeline(
            storage, run_id="run-bad-lookup", status="failed",
            summary_error="真实错误",
        )
        ks = _InMemoryKnowledgeService()

        def bad_lookup(pipeline_run_id):
            raise RuntimeError("task_service 不可用")

        service = MemoryMaintenanceService(
            storage=storage, chunk_db=None, knowledge_service=ks,
            task_lookup=bad_lookup,
        )

        # 不应抛异常
        await service.trigger_review_now(force=False)

        assert len(ks._items) == 1
        assert "真实错误" in ks._items[0]["content"]


