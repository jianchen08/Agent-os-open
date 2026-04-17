"""ExperienceConsolidatorPlugin 单元测试。

测试要点：
- task_complete=True → 触发沉淀
- task_complete=False → 跳过
- 无 episode_id → 跳过
- 沉淀成功 → state 更新 experience_consolidated=True
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.episode_service import EpisodeService
from memory.knowledge_service import KnowledgeService
from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from plugins.output.experience_consolidator import (
    ExperienceConsolidatorPlugin,
)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def plugin() -> ExperienceConsolidatorPlugin:
    """创建经验沉淀插件实例。"""
    return ExperienceConsolidatorPlugin()


@pytest.fixture
def episode_service() -> EpisodeService:
    """创建内存降级的情景记忆服务。"""
    return EpisodeService(episode_storage=None)


@pytest.fixture
def knowledge_service() -> KnowledgeService:
    """创建内存降级的知识服务。"""
    return KnowledgeService(semantic_storage=None)


def _make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(
        state=state or {},
        _services=services or {},
    )


# ============================================================
# 测试
# ============================================================


class TestExperienceConsolidatorPlugin:
    """ExperienceConsolidatorPlugin 测试。"""

    def test_name(self, plugin: ExperienceConsolidatorPlugin) -> None:
        """插件名称正确。"""
        assert plugin.name == "experience_consolidator"

    def test_priority(self, plugin: ExperienceConsolidatorPlugin) -> None:
        """优先级为 28。"""
        assert plugin.priority == 28

    def test_error_policy(self, plugin: ExperienceConsolidatorPlugin) -> None:
        """错误策略为 SKIP。"""
        assert plugin.error_policy == ErrorPolicy.SKIP

    @pytest.mark.asyncio
    async def test_task_complete_triggers_consolidation(
        self,
        plugin: ExperienceConsolidatorPlugin,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """task_complete=True → 触发沉淀。"""
        # 创建已完成的 Episode
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="分析数据",
            execution_summary="分析完成",
            final_score=85.0,
        )

        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: True,
                "episode_id": episode_dict["id"],
                "user_id": "user1",
            },
            services={
                "episode_service": episode_service,
                "knowledge_service": knowledge_service,
            },
        )

        result = await plugin.execute(ctx)

        assert result.state_updates.get("experience_consolidated") is True
        assert result.state_updates.get("knowledge_id", "") != ""

    @pytest.mark.asyncio
    async def test_execution_status_completed_triggers_consolidation(
        self,
        plugin: ExperienceConsolidatorPlugin,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """execution_status="completed" → 触发沉淀。"""
        episode_dict = await episode_service.create_episode(
            user_id="user1",
            intent_text="分析数据",
            execution_summary="分析完成",
            final_score=90.0,
        )

        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: False,
                StateKeys.EXECUTION_STATUS: "completed",
                "episode_id": episode_dict["id"],
                "user_id": "user1",
            },
            services={
                "episode_service": episode_service,
                "knowledge_service": knowledge_service,
            },
        )

        result = await plugin.execute(ctx)

        assert result.state_updates.get("experience_consolidated") is True

    @pytest.mark.asyncio
    async def test_task_not_complete_skips(
        self,
        plugin: ExperienceConsolidatorPlugin,
    ) -> None:
        """task_complete=False 且 execution_status≠"completed" → 跳过。"""
        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: False,
                StateKeys.EXECUTION_STATUS: "running",
                "episode_id": "some-id",
                "user_id": "user1",
            },
        )

        result = await plugin.execute(ctx)

        # 无 state_updates 表示跳过
        assert not result.state_updates

    @pytest.mark.asyncio
    async def test_no_episode_id_skips(
        self,
        plugin: ExperienceConsolidatorPlugin,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """无 episode_id → 跳过。"""
        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: True,
                # 没有 episode_id
                "user_id": "user1",
            },
            services={
                "episode_service": episode_service,
                "knowledge_service": knowledge_service,
            },
        )

        result = await plugin.execute(ctx)

        assert not result.state_updates

    @pytest.mark.asyncio
    async def test_consolidation_failure_still_returns_result(
        self,
        plugin: ExperienceConsolidatorPlugin,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """沉淀失败时返回 experience_consolidated=False。"""
        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: True,
                "episode_id": "non-existent-id",
                "user_id": "user1",
            },
            services={
                "episode_service": episode_service,
                "knowledge_service": knowledge_service,
            },
        )

        result = await plugin.execute(ctx)

        assert result.state_updates.get("experience_consolidated") is False

    @pytest.mark.asyncio
    async def test_no_services_skips(
        self,
        plugin: ExperienceConsolidatorPlugin,
    ) -> None:
        """服务不可用时静默跳过。"""
        ctx = _make_ctx(
            state={
                StateKeys.TASK_COMPLETE: True,
                "episode_id": "some-id",
                "user_id": "user1",
            },
            services={},  # 无服务
        )

        result = await plugin.execute(ctx)

        # 无服务时跳过，不报错
        assert not result.state_updates
