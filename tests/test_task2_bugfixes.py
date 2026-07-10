"""
灵汐系统 5 个 Bug 修复回归测试。

对应 REQ:
  REQ-1: RBAC 权限 is_role_higher_or_equal 缺失
  REQ-2: 容器任务创建后状态矛盾（pending 死锁）
  REQ-3: 触发器时区不匹配导致 TypeError
  REQ-4: 事件触发器 evaluate_event 无人调用
  REQ-5: Memory retrieve 返回空结果

每个测试类验证对应 Bug 的根因被修复，而非仅验证症状消失。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
# REQ-1: RBAC is_role_higher_or_equal
# ═══════════════════════════════════════════════════════════════════


class TestRBACIsRoleHigherOrEqual:
    """回归：验证 RBACManager.is_role_higher_or_equal 方法存在且正确。"""

    def test_method_exists(self) -> None:
        """is_role_higher_or_equal 方法应该存在于 RBACManager 上。"""
        from src.auth.rbac import RBACManager

        mgr = RBACManager()
        assert hasattr(mgr, "is_role_higher_or_equal"), (
            "根因未修复: RBACManager 缺少 is_role_higher_or_equal 方法"
        )

    def test_same_role_returns_true(self) -> None:
        """相同角色应返回 True。"""
        from src.auth.rbac import RBACManager, Role

        mgr = RBACManager()
        assert mgr.is_role_higher_or_equal(Role.ADMIN, Role.ADMIN) is True

    def test_higher_role_returns_true(self) -> None:
        """高权限角色对低权限角色应返回 True。"""
        from src.auth.rbac import RBACManager, Role

        mgr = RBACManager()
        assert mgr.is_role_higher_or_equal(Role.SUPER_ADMIN, Role.ADMIN) is True
        assert mgr.is_role_higher_or_equal(Role.ADMIN, Role.USER) is True
        assert mgr.is_role_higher_or_equal(Role.USER, Role.GUEST) is True

    def test_lower_role_returns_false(self) -> None:
        """低权限角色对高权限角色应返回 False。"""
        from src.auth.rbac import RBACManager, Role

        mgr = RBACManager()
        assert mgr.is_role_higher_or_equal(Role.GUEST, Role.USER) is False
        assert mgr.is_role_higher_or_equal(Role.USER, Role.ADMIN) is False

    def test_string_role_input(self) -> None:
        """字符串角色输入应正常工作。"""
        from src.auth.rbac import RBACManager

        mgr = RBACManager()
        assert mgr.is_role_higher_or_equal("admin", "user") is True
        assert mgr.is_role_higher_or_equal("guest", "admin") is False

    def test_super_admin_inherits_all(self) -> None:
        """SUPER_ADMIN 应高于所有角色。"""
        from src.auth.rbac import RBACManager, Role

        mgr = RBACManager()
        for role in [Role.ADMIN, Role.USER, Role.GUEST]:
            assert mgr.is_role_higher_or_equal(Role.SUPER_ADMIN, role) is True


# ═══════════════════════════════════════════════════════════════════
# REQ-2: 容器任务创建后自动 running
# ═══════════════════════════════════════════════════════════════════


class TestContainerTaskAutoRunning:
    """回归：验证容器任务创建后自动进入 running 状态。"""

    @staticmethod
    def _make_service() -> "TaskService":
        """创建一个跳过存储初始化的 TaskService，手动注入 mock storage。"""
        from tasks.service import TaskService

        # task_id 非 None 时不会初始化 _storage
        svc = TaskService(task_id="__test__")
        svc._storage = MagicMock()
        return svc

    @pytest.mark.asyncio
    async def test_container_task_auto_running(self) -> None:
        """metadata.task_scope=container 的任务创建后应自动变为 running。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task(
            title="容器任务",
            description="测试容器任务",
            metadata={"task_scope": "container"},
        )
        assert task.status == TaskStatus.RUNNING, (
            f"根因未修复: 容器任务创建后状态为 {task.status}，期望 RUNNING"
        )

    @pytest.mark.asyncio
    async def test_normal_task_stays_pending(self) -> None:
        """普通任务创建后应保持 pending 状态。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task(
            title="普通任务",
            description="测试普通任务",
            metadata={},
        )
        assert task.status == TaskStatus.PENDING, (
            f"回归破坏: 普通任务创建后状态为 {task.status}，期望 PENDING"
        )

    @pytest.mark.asyncio
    async def test_container_task_without_metadata_stays_pending(self) -> None:
        """无 metadata 的任务应保持 pending 状态。"""
        from tasks.types import TaskStatus

        svc = self._make_service()
        task = await svc.create_task(
            title="无元数据任务",
            description="测试",
        )
        assert task.status == TaskStatus.PENDING


# ═══════════════════════════════════════════════════════════════════
# REQ-3: 触发器时区归一化
# ═══════════════════════════════════════════════════════════════════


class TestTriggerTimezoneNormalization:
    """回归：验证定时触发器的时区归一化。"""

    def test_normalize_datetime_naive_input(self) -> None:
        """naive datetime 应被视为 UTC。"""
        from triggers.manager import TriggerManager

        naive = datetime.datetime(2026, 1, 1, 12, 0, 0)
        result = TriggerManager._normalize_datetime(naive)
        assert result.tzinfo is not None, "归一化后应有 tzinfo"
        assert result == datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def test_normalize_datetime_aware_input(self) -> None:
        """aware datetime 应转换为 UTC。"""
        from triggers.manager import TriggerManager

        tz_plus8 = datetime.timezone(datetime.timedelta(hours=8))
        aware = datetime.datetime(2026, 1, 1, 20, 0, 0, tzinfo=tz_plus8)
        result = TriggerManager._normalize_datetime(aware)
        assert result.tzinfo is not None
        assert result.hour == 12  # 20:00+0800 → 12:00 UTC

    def test_scheduled_trigger_no_timezone_error(self) -> None:
        """定时触发器比较时不应抛出 TypeError（原 Bug 核心验证）。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        trigger = TriggerConfig(
            trigger_id="test-tz",
            name="tz test",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=datetime.datetime(2026, 1, 1, 0, 0, 0),  # naive
        )

        # aware now — 原来会抛 TypeError: can't compare offset-naive and offset-aware
        now = datetime.datetime.now(datetime.timezone.utc)

        # 不应抛异常
        result = mgr._check_scheduled_time(trigger, now)
        assert isinstance(result, bool)

    def test_scheduled_trigger_past_time_fires(self) -> None:
        """过去的时间应返回 True（触发器到期）。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        # 过去的时间（naive）
        trigger = TriggerConfig(
            trigger_id="test-past",
            name="past test",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=datetime.datetime(2020, 1, 1, 0, 0, 0),
        )
        now = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        assert mgr._check_scheduled_time(trigger, now) is True

    def test_scheduled_trigger_future_time_not_fires(self) -> None:
        """未来的时间应返回 False（触发器未到期）。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        trigger = TriggerConfig(
            trigger_id="test-future",
            name="future test",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=datetime.datetime(2099, 1, 1, 0, 0, 0),
        )
        now = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        assert mgr._check_scheduled_time(trigger, now) is False


# ═══════════════════════════════════════════════════════════════════
# REQ-4: 事件触发器桥接
# ═══════════════════════════════════════════════════════════════════


class TestEventTriggerBridge:
    """回归：验证事件触发器的桥接方法存在且可用。"""

    def test_on_system_event_method_exists(self) -> None:
        """on_system_event 方法应存在。"""
        from triggers.manager import TriggerManager

        mgr = TriggerManager()
        assert hasattr(mgr, "on_system_event"), (
            "根因未修复: TriggerManager 缺少 on_system_event 方法"
        )

    def test_subscribe_to_event_bus_method_exists(self) -> None:
        """subscribe_to_event_bus 方法应存在。"""
        from triggers.manager import TriggerManager

        mgr = TriggerManager()
        assert hasattr(mgr, "subscribe_to_event_bus"), (
            "根因未修复: TriggerManager 缺少 subscribe_to_event_bus 方法"
        )

    @pytest.mark.asyncio
    async def test_on_system_event_calls_evaluate_event(self) -> None:
        """on_system_event 应调用 evaluate_event 并返回结果。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        # 注册一个事件触发器
        trigger = TriggerConfig(
            trigger_id="evt-1",
            name="task completed trigger",
            trigger_type=TriggerType.EVENT,
            event_name="task_completed",
        )
        mgr.register(trigger)

        result = await mgr.on_system_event("task_completed", {"task_id": "abc"})
        assert "evt-1" in result, (
            f"on_system_event 应返回被触发的 trigger_id 列表，实际: {result}"
        )

    @pytest.mark.asyncio
    async def test_on_system_event_no_match(self) -> None:
        """不匹配的事件名不应触发任何触发器。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        trigger = TriggerConfig(
            trigger_id="evt-2",
            name="task failed trigger",
            trigger_type=TriggerType.EVENT,
            event_name="task_failed",
        )
        mgr.register(trigger)

        result = await mgr.on_system_event("task_completed", {"task_id": "abc"})
        assert result == []

    def test_subscribe_to_event_bus_calls_subscribe(self) -> None:
        """subscribe_to_event_bus 应调用 event_bus.subscribe。"""
        from triggers.manager import TriggerManager

        mgr = TriggerManager()
        mock_bus = MagicMock()
        mock_bus.subscribe = MagicMock()

        mgr.subscribe_to_event_bus(mock_bus)
        mock_bus.subscribe.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# REQ-5: Memory retrieve 内置 keyword 检索器
# ═══════════════════════════════════════════════════════════════════


class TestMemoryDefaultRetriever:
    """回归：验证 MemoryService 自动注册内置 keyword 检索器。"""

    def test_keyword_retriever_auto_registered(self) -> None:
        """构造后 _retrievers 应自动包含 keyword 检索器。"""
        from memory.service import MemoryService

        svc = MemoryService()
        assert "keyword" in svc._retrievers, (
            "根因未修复: MemoryService 构造后 _retrievers 缺少 keyword"
        )

    def test_keyword_retriever_not_overwrite_explicit(self) -> None:
        """显式传入的 retrievers 不应被覆盖。"""
        from memory.service import MemoryService
        from memory.ports import IRetriever

        class DummyRetriever(IRetriever):
            async def retrieve(self, query, user_id=None, top_k=5, memory_type="semantic", filters=None):
                return []

        custom = DummyRetriever()
        svc = MemoryService(retrievers={"keyword": custom})
        # 显式传入的应保留
        assert svc._retrievers["keyword"] is custom

    def test_external_retrievers_preserved(self) -> None:
        """显式传入的 vector 检索器应保留。"""
        from memory.service import MemoryService
        from memory.ports import IRetriever

        class DummyRetriever(IRetriever):
            async def retrieve(self, query, user_id=None, top_k=5, memory_type="semantic", filters=None):
                return []

        vector = DummyRetriever()
        svc = MemoryService(retrievers={"vector": vector})
        assert "vector" in svc._retrievers
        assert svc._retrievers["vector"] is vector

    def test_ensure_default_retrievers_idempotent(self) -> None:
        """多次调用 _ensure_default_retrievers 不应重复添加。"""
        from memory.service import MemoryService

        svc = MemoryService()
        count_before = len(svc._retrievers)
        svc._ensure_default_retrievers()
        count_after = len(svc._retrievers)
        assert count_before == count_after, (
            f"重复调用不应增加检索器数量: {count_before} → {count_after}"
        )

    @pytest.mark.asyncio
    async def test_retrieve_keyword_returns_results(self) -> None:
        """keyword 检索器在匹配内容时应返回非空结果。"""
        from memory.service import MemoryService

        svc = MemoryService()

        # 模拟 episode_service 返回包含匹配关键词的内容
        mock_episode_svc = MagicMock()
        mock_episode_svc.list_episodes = AsyncMock(return_value={
            "items": [
                {
                    "id": "ep-1",
                    "intent_text": "测试关键词搜索功能",
                    "execution_summary": "验证关键词检索器工作正常",
                },
            ],
        })

        # 替换内部的 episode_service（同时更新已注册的 keyword 检索器引用）
        svc._episode_service = mock_episode_svc
        svc._retrievers["keyword"]._episode_service = mock_episode_svc

        results = await svc._retrievers["keyword"].retrieve(
            query="关键词",
            top_k=5,
            memory_type="all",
        )
        assert len(results) > 0, "keyword 检索器应返回匹配的结果"
        assert results[0].id == "ep-1"
