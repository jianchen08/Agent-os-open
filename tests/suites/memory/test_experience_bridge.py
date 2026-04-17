"""ExperienceBridge 单元测试。

测试要点：
- Episode 存在且已完成 → consolidate_to_knowledge 成功，返回 knowledge_id
- Episode 不存在 → 失败，error 非空
- Episode 未完成（无 execution_summary） → 失败（除非 force=True）
- batch_consolidate 只处理 min_score 以上的 Episode
- Knowledge 的 source_type = "experience"
"""

from __future__ import annotations

import pytest

from memory.episode_service import EpisodeService
from memory.experience_bridge import ExperienceBridge, KnowledgeResult
from memory.knowledge_service import KnowledgeService


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def episode_service() -> EpisodeService:
    """创建内存降级的情景记忆服务。"""
    return EpisodeService(episode_storage=None)


@pytest.fixture
def knowledge_service() -> KnowledgeService:
    """创建内存降级的知识服务。"""
    return KnowledgeService(semantic_storage=None)


@pytest.fixture
def bridge(
    episode_service: EpisodeService,
    knowledge_service: KnowledgeService,
) -> ExperienceBridge:
    """创建经验桥梁实例。"""
    return ExperienceBridge(
        episode_service=episode_service,
        knowledge_service=knowledge_service,
    )


# ============================================================
# consolidate_to_knowledge 测试
# ============================================================


class TestConsolidateToKnowledge:
    """consolidate_to_knowledge 方法测试。"""

    @pytest.mark.asyncio
    async def test_completed_episode_consolidates_successfully(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """已完成的 Episode → 沉淀成功，返回 knowledge_id。"""
        # 创建已完成的 Episode
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析销售数据",
            execution_summary="成功分析了 Q1 销售数据，发现增长趋势",
            final_score=85.0,
            tags=["数据分析"],
        )
        episode_id = episode_dict["id"]

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_id,
            user_id="user1",
        )

        assert result.success is True
        assert result.knowledge_id != ""
        assert result.episode_id == episode_id
        assert result.error is None

    @pytest.mark.asyncio
    async def test_episode_not_exists(
        self,
        bridge: ExperienceBridge,
    ) -> None:
        """Episode 不存在 → 失败，error 非空。"""
        result = await bridge.consolidate_to_knowledge(
            episode_id="non-existent-id",
            user_id="user1",
        )

        assert result.success is False
        assert result.error is not None
        assert "不存在" in result.error
        assert result.knowledge_id == ""

    @pytest.mark.asyncio
    async def test_episode_without_summary_fails(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """Episode 无 execution_summary → 沉淀失败。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析数据",
            # 不提供 execution_summary
            final_score=80.0,
        )
        episode_id = episode_dict["id"]

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_id,
            user_id="user1",
        )

        assert result.success is False
        assert result.error is not None
        assert "execution_summary" in result.error

    @pytest.mark.asyncio
    async def test_episode_with_zero_score_fails(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """Episode final_score=0 → 沉淀失败。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析数据",
            execution_summary="执行失败",
            final_score=0,
        )
        episode_id = episode_dict["id"]

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_id,
            user_id="user1",
        )

        assert result.success is False
        assert result.error is not None
        assert "评分不足" in result.error

    @pytest.mark.asyncio
    async def test_episode_with_none_score_fails(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """Episode final_score=None → 沉淀失败。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析数据",
            execution_summary="执行了但没评分",
            # final_score=None（默认）
        )
        episode_id = episode_dict["id"]

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_id,
            user_id="user1",
        )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_force_bypasses_completion_check(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """force=True 跳过完成状态检查。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析数据",
            # 不提供 execution_summary 和 final_score
        )
        episode_id = episode_dict["id"]

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_id,
            user_id="user1",
            force=True,
        )

        assert result.success is True
        assert result.knowledge_id != ""

    @pytest.mark.asyncio
    async def test_knowledge_source_type_is_experience(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """沉淀后的 Knowledge source_type = "experience"。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析销售数据",
            execution_summary="成功分析了销售数据",
            final_score=90.0,
        )

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_dict["id"],
            user_id="user1",
        )

        assert result.success is True

        # 验证 Knowledge 的 source_type
        knowledge_list = await knowledge_service.list_semantic_memory("user1")
        items = knowledge_list.get("items", [])
        assert len(items) >= 1
        # 找到刚创建的知识
        found = any(
            item.get("source_type") == "experience" and item.get("id") == result.knowledge_id
            for item in items
        )
        assert found, "未找到 source_type='experience' 的知识记录"

    @pytest.mark.asyncio
    async def test_wrong_user_cannot_consolidate(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """不同用户无法沉淀其他用户的 Episode。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="帮我分析数据",
            execution_summary="分析完成",
            final_score=80.0,
        )

        result = await bridge.consolidate_to_knowledge(
            episode_id=episode_dict["id"],
            user_id="user2",  # 不同的用户
        )

        assert result.success is False
        assert "不存在" in (result.error or "")


# ============================================================
# batch_consolidate 测试
# ============================================================


class TestBatchConsolidate:
    """batch_consolidate 方法测试。"""

    @pytest.mark.asyncio
    async def test_batch_only_processes_above_min_score(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """batch_consolidate 只处理 min_score 以上的 Episode。"""
        # 高分 Episode
        await episode_service.create_episode(
            user_id="user1",
            intent_text="高分任务",
            execution_summary="高分执行",
            final_score=90.0,
        )
        # 低分 Episode
        await episode_service.create_episode(
            user_id="user1",
            intent_text="低分任务",
            execution_summary="低分执行",
            final_score=30.0,
        )

        results = await bridge.batch_consolidate(
            user_id="user1",
            min_score=60.0,
        )

        # 只有 90 分的被沉淀
        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_batch_respects_limit(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """batch_consolidate 限制处理数量。"""
        for i in range(5):
            await episode_service.create_episode(
                user_id="user1",
                intent_text=f"任务 {i}",
                execution_summary=f"执行 {i}",
                final_score=80.0 + i,
            )

        results = await bridge.batch_consolidate(
            user_id="user1",
            min_score=60.0,
            limit=2,
        )

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_batch_skips_consolidated_episodes(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """batch_consolidate 跳过已沉淀的 Episode。"""
        # 先创建并沉淀一个
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="任务 1",
            execution_summary="执行 1",
            final_score=90.0,
        )
        await bridge.consolidate_to_knowledge(
            episode_id=episode_dict["id"],
            user_id="user1",
        )

        # 批量沉淀时应该跳过已沉淀的
        results = await bridge.batch_consolidate(
            user_id="user1",
            min_score=60.0,
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_batch_no_eligible_episodes(
        self,
        bridge: ExperienceBridge,
        episode_service: EpisodeService,
    ) -> None:
        """没有符合条件的 Episode 时返回空列表。"""
        await episode_service.create_episode(
            user_id="user1",
            intent_text="低分任务",
            execution_summary="执行",
            final_score=20.0,
        )

        results = await bridge.batch_consolidate(
            user_id="user1",
            min_score=60.0,
        )

        assert results == []


# ============================================================
# _generate_knowledge_content 测试
# ============================================================


class TestGenerateKnowledgeContent:
    """_generate_knowledge_content 静态方法测试。"""

    def test_full_episode(self) -> None:
        """包含所有字段的 Episode 生成完整内容。"""
        episode = {
            "intent_text": "分析数据",
            "execution_summary": "分析完成",
            "plan_dag": {"step1": "load", "step2": "analyze"},
            "final_score": 90.0,
        }
        content = ExperienceBridge._generate_knowledge_content(episode)
        assert "意图: 分析数据" in content
        assert "执行摘要: 分析完成" in content
        assert "执行计划:" in content
        assert "评分: 90.0" in content

    def test_minimal_episode(self) -> None:
        """只有部分字段的 Episode。"""
        episode = {"intent_text": "分析数据"}
        content = ExperienceBridge._generate_knowledge_content(episode)
        assert "意图: 分析数据" in content
        assert "执行摘要" not in content

    def test_empty_episode(self) -> None:
        """空 Episode 生成空内容。"""
        episode: dict = {}
        content = ExperienceBridge._generate_knowledge_content(episode)
        assert content == ""
