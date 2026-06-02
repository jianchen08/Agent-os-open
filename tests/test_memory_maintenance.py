"""记忆维护机制单元测试。

覆盖三个核心功能：
1. 过期清理机制（TTL）
2. 容量限制机制（LRU + 重要性权重）
3. 重要性衰减机制（指数/线性衰减）

测试策略：
- 使用 Mock 隔离外部依赖（memory_service、存储后端）
- 每个 AC 至少一个正向测试 + 边界测试
- 验证状态变化和副作用
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(
    reason="MaintenanceConfig 和 MemoryMaintenanceService 已完全重构为复盘驱动模式，"
           "旧API（cleanup_ttl_expired/evict_by_capacity/decay_importance 等）已移除"
)

try:
    from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService
    from memory.types import Episode, Knowledge
except ImportError:
    # 旧 API 已重构移除，整个模块被 skip，此处仅防止 collection 阶段报错
    MaintenanceConfig = None  # type: ignore[misc,assignment]
    MemoryMaintenanceService = None  # type: ignore[misc,assignment]
    Episode = None  # type: ignore[misc,assignment]
    Knowledge = None  # type: ignore[misc,assignment]


# ============================================================
# Helpers
# ============================================================


def make_episode(
    id: str = "test-ep",
    intent_text: str = "test",
    created_at: datetime | None = None,
    extra_data: dict[str, Any] | None = None,
) -> Episode:
    """创建带 extra_data 的情景记忆。"""
    ep = Episode(
        id=id,
        intent_text=intent_text,
        created_at=created_at or datetime.now(UTC),
    )
    if extra_data is not None:
        ep.extra_data = extra_data
    return ep


def make_knowledge(
    id: str = "test-kn",
    content: str = "test content",
    created_at: datetime | None = None,
    extra_data: dict[str, Any] | None = None,
) -> Knowledge:
    """创建带 extra_data 的语义记忆。"""
    kn = Knowledge(
        id=id,
        content=content,
        created_at=created_at or datetime.now(UTC),
    )
    if extra_data is not None:
        kn.extra_data = extra_data
    return kn


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_episode_service() -> MagicMock:
    """创建 Mock 情景记忆服务。"""
    service = MagicMock()
    service._storage = None  # 使用内存模式
    service._in_memory: dict[str, Episode] = {}
    return service


@pytest.fixture
def mock_knowledge_service() -> MagicMock:
    """创建 Mock 知识服务。"""
    service = MagicMock()
    service._storage = None  # 使用内存模式
    service._in_memory: dict[str, Knowledge] = {}
    return service


@pytest.fixture
def mock_memory_service(
    mock_episode_service: MagicMock,
    mock_knowledge_service: MagicMock,
) -> MagicMock:
    """创建 Mock 记忆服务门面。"""
    service = MagicMock()
    service._episode_service = mock_episode_service
    service._knowledge_service = mock_knowledge_service
    service._embedding_service = None
    service._vector_retriever = None
    service._tag_service = None
    return service


@pytest.fixture
def default_config() -> MaintenanceConfig:
    """创建默认维护配置。"""
    return MaintenanceConfig()


@pytest.fixture
def maintenance_service(
    mock_memory_service: MagicMock,
    default_config: MaintenanceConfig,
) -> MemoryMaintenanceService:
    """创建维护服务实例。"""
    return MemoryMaintenanceService(
        memory_service=mock_memory_service,
        config=default_config,
    )


# ============================================================
# 1. MaintenanceConfig 测试
# ============================================================


class TestMaintenanceConfig:
    """维护配置类的测试。"""

    def test_default_values_are_valid(self) -> None:
        """默认配置值应该是合理有效的。"""
        config = MaintenanceConfig()
        assert config.ttl_enabled is True
        assert config.default_ttl_seconds > 0
        assert config.capacity_limit > 0
        assert config.decay_enabled is True
        assert config.decay_half_life_seconds > 0
        assert 0.0 < config.lru_weight <= 1.0
        assert 0.0 < config.importance_weight <= 1.0

    def test_custom_values_override_defaults(self) -> None:
        """自定义值应该覆盖默认值。"""
        config = MaintenanceConfig(
            ttl_enabled=False,
            default_ttl_seconds=3600,
            capacity_limit=500,
            decay_type="linear",
            decay_rate=0.01,
        )
        assert config.ttl_enabled is False
        assert config.default_ttl_seconds == 3600
        assert config.capacity_limit == 500
        assert config.decay_type == "linear"
        assert config.decay_rate == 0.01

    def test_lru_and_importance_weights_sum_to_one(self) -> None:
        """LRU 权重和重要性权重之和应该为 1。"""
        config = MaintenanceConfig()
        assert abs(config.lru_weight + config.importance_weight - 1.0) < 1e-9

    def test_from_dict_merges_with_defaults(self) -> None:
        """from_dict 应该将字典值合并到默认配置。"""
        config = MaintenanceConfig.from_dict({"capacity_limit": 200})
        assert config.capacity_limit == 200
        assert config.ttl_enabled is True  # 未提供的值保持默认

    def test_from_dict_empty_returns_defaults(self) -> None:
        """空字典应返回完全默认的配置。"""
        config = MaintenanceConfig.from_dict({})
        default = MaintenanceConfig()
        assert config.capacity_limit == default.capacity_limit
        assert config.decay_type == default.decay_type


# ============================================================
# 2. 过期清理机制（TTL）测试
# ============================================================


class TestTTLExpirationCleanup:
    """TTL 过期清理机制测试。"""

    def test_cleanup_ttl_expired_removes_expired_episodes(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """应该删除 TTL 已过期的情景记忆。"""
        now = datetime.now(UTC)
        expired_time = now - timedelta(seconds=7200)
        recent_time = now - timedelta(seconds=100)

        expired_ep = make_episode(
            id="expired-1",
            intent_text="expired",
            created_at=expired_time,
            extra_data={"ttl_seconds": 3600},  # 1小时TTL，已过期
        )
        recent_ep = make_episode(
            id="recent-1",
            intent_text="recent",
            created_at=recent_time,
            extra_data={"ttl_seconds": 3600},
        )

        mock_episode_service._in_memory = {
            "expired-1": expired_ep,
            "recent-1": recent_ep,
        }

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert "expired-1" not in mock_episode_service._in_memory
        assert "recent-1" in mock_episode_service._in_memory
        assert result["cleaned_count"] >= 1

    def test_cleanup_ttl_expired_removes_expired_knowledge(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_knowledge_service: MagicMock,
    ) -> None:
        """应该删除 TTL 已过期的语义记忆。"""
        now = datetime.now(UTC)
        expired_time = now - timedelta(seconds=7200)

        expired_kn = make_knowledge(
            id="kn-expired-1",
            content="expired knowledge",
            created_at=expired_time,
            extra_data={"ttl_seconds": 3600},
        )

        mock_knowledge_service._in_memory = {
            "kn-expired-1": expired_kn,
        }

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert "kn-expired-1" not in mock_knowledge_service._in_memory
        assert result["cleaned_count"] >= 1

    def test_cleanup_ttl_uses_default_ttl_when_not_set(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """当记忆没有设置 TTL 时，应使用默认 TTL。"""
        now = datetime.now(UTC)
        very_old_time = now - timedelta(days=400)  # 超过默认 TTL (365天)

        old_ep = make_episode(
            id="old-1",
            intent_text="old",
            created_at=very_old_time,
            # 没有 extra_data
        )

        mock_episode_service._in_memory = {"old-1": old_ep}

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert "old-1" not in mock_episode_service._in_memory

    def test_cleanup_ttl_skips_when_disabled(
        self,
        mock_memory_service: MagicMock,
    ) -> None:
        """TTL 清理禁用时应跳过。"""
        config = MaintenanceConfig(ttl_enabled=False)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = service.cleanup_ttl_expired()

        assert result["status"] == "skipped"
        assert "disabled" in result.get("reason", "").lower()

    def test_cleanup_ttl_no_expired_returns_zero(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
        mock_knowledge_service: MagicMock,
    ) -> None:
        """没有过期记忆时应返回 cleaned_count=0。"""
        now = datetime.now(UTC)
        recent_ep = make_episode(
            id="recent-1",
            intent_text="recent",
            created_at=now,
        )
        mock_episode_service._in_memory = {"recent-1": recent_ep}

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert result["cleaned_count"] == 0
        assert result["status"] == "success"

    def test_cleanup_ttl_with_zero_ttl_never_expires(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """TTL 设为 0 表示永不过期。"""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=999)

        never_expire_ep = make_episode(
            id="never-expire",
            intent_text="should not expire",
            created_at=old_time,
            extra_data={"ttl_seconds": 0},
        )

        mock_episode_service._in_memory = {
            "never-expire": never_expire_ep,
        }

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert "never-expire" in mock_episode_service._in_memory
        assert result["cleaned_count"] == 0

    def test_cleanup_ttl_with_storage_backend(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """使用存储后端时应通过 storage API 删除。"""
        now = datetime.now(UTC)
        expired_time = now - timedelta(seconds=7200)

        expired_ep = make_episode(
            id="expired-storage-1",
            intent_text="expired",
            created_at=expired_time,
            extra_data={"ttl_seconds": 3600},
        )

        # 启用存储后端模式 - 同步 mock
        mock_storage = MagicMock()
        mock_storage.find_by_user = MagicMock(return_value=[expired_ep])
        mock_storage.delete = MagicMock(return_value=True)
        mock_episode_service._storage = mock_storage
        mock_episode_service._in_memory = {}

        result = maintenance_service.cleanup_ttl_expired(now=now)

        assert result["cleaned_count"] >= 1
        mock_storage.delete.assert_called()


# ============================================================
# 3. 容量限制机制（LRU + 重要性权重）测试
# ============================================================


class TestCapacityEviction:
    """容量限制淘汰机制测试。"""

    def test_evict_when_over_capacity(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """记忆超过容量限制时应淘汰低价值记忆。"""
        config = MaintenanceConfig(capacity_limit=3)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        # 创建 5 条记忆，容量限制为 3，应淘汰 2 条
        for i in range(5):
            ep = make_episode(
                id=f"ep-{i}",
                intent_text=f"episode {i}",
                created_at=now - timedelta(seconds=i * 10),
                extra_data={
                    "importance": 0.1 * i,  # 0.0, 0.1, 0.2, 0.3, 0.4
                    "last_accessed_at": (
                        now - timedelta(seconds=(4 - i) * 100)
                    ).isoformat(),
                },
            )
            mock_episode_service._in_memory[f"ep-{i}"] = ep

        result = service.evict_by_capacity(now=now)

        # 容量为 3，原有 5 条，应淘汰 2 条
        assert result["evicted_count"] == 2
        assert result["status"] == "success"

    def test_evict_prefers_low_importance_and_old(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """淘汰时应优先淘汰低重要性 + 久未访问的记忆。"""
        config = MaintenanceConfig(
            capacity_limit=2,
            lru_weight=0.5,
            importance_weight=0.5,
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        # 高重要性 + 最近访问（应保留）
        high_value = make_episode(
            id="high-value",
            intent_text="important",
            created_at=now - timedelta(hours=1),
            extra_data={
                "importance": 0.9,
                "last_accessed_at": now.isoformat(),
            },
        )
        # 中等价值
        mid_value = make_episode(
            id="mid-value",
            intent_text="medium",
            created_at=now - timedelta(hours=2),
            extra_data={
                "importance": 0.5,
                "last_accessed_at": (now - timedelta(hours=1)).isoformat(),
            },
        )
        # 低重要性 + 久未访问（应淘汰）
        low_value = make_episode(
            id="low-value",
            intent_text="not important",
            created_at=now - timedelta(hours=10),
            extra_data={
                "importance": 0.1,
                "last_accessed_at": (now - timedelta(hours=9)).isoformat(),
            },
        )

        mock_episode_service._in_memory = {
            "high-value": high_value,
            "mid-value": mid_value,
            "low-value": low_value,
        }

        result = service.evict_by_capacity(now=now)

        assert result["evicted_count"] == 1
        # 低价值记忆应被淘汰
        assert "low-value" not in mock_episode_service._in_memory
        # 高价值记忆应保留
        assert "high-value" in mock_episode_service._in_memory

    def test_evict_no_eviction_when_under_capacity(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """记忆未超过容量限制时不应淘汰。"""
        now = datetime.now(UTC)
        ep = make_episode(id="ep-1", intent_text="test", created_at=now)
        mock_episode_service._in_memory = {"ep-1": ep}
        # 默认容量限制远大于 1

        result = maintenance_service.evict_by_capacity(now=now)

        assert result["evicted_count"] == 0
        assert result["status"] == "success"

    def test_evict_at_exact_capacity_does_nothing(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """记忆数量恰好等于容量限制时不淘汰。"""
        config = MaintenanceConfig(capacity_limit=2)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        ep1 = make_episode(id="ep-1", intent_text="a", created_at=now)
        ep2 = make_episode(id="ep-2", intent_text="b", created_at=now)
        mock_episode_service._in_memory = {"ep-1": ep1, "ep-2": ep2}

        result = service.evict_by_capacity(now=now)

        assert result["evicted_count"] == 0

    def test_evict_memories_without_importance_use_default(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """没有设置 importance 的记忆应使用默认值（中等）。"""
        config = MaintenanceConfig(capacity_limit=2)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        # 都没有 importance，应该按 LRU 淘汰最旧的
        oldest = make_episode(
            id="oldest",
            intent_text="oldest",
            created_at=now - timedelta(hours=5),
        )
        mid = make_episode(
            id="mid",
            intent_text="mid",
            created_at=now - timedelta(hours=3),
        )
        newest = make_episode(
            id="newest",
            intent_text="newest",
            created_at=now - timedelta(hours=1),
        )

        mock_episode_service._in_memory = {
            "oldest": oldest,
            "mid": mid,
            "newest": newest,
        }

        result = service.evict_by_capacity(now=now)

        assert result["evicted_count"] == 1
        # 最旧且无 last_accessed_at 的应被淘汰
        assert "oldest" not in mock_episode_service._in_memory

    def test_evict_with_storage_backend(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """使用存储后端时应通过 storage API 统计和删除。"""
        now = datetime.now(UTC)
        mock_storage = MagicMock()
        mock_storage.count_by_user = MagicMock(return_value=1000)

        episodes = [
            make_episode(
                id=f"ep-{i}",
                intent_text=f"episode {i}",
                created_at=now - timedelta(seconds=i),
                extra_data={"importance": 0.1},
            )
            for i in range(10)
        ]
        mock_storage.find_by_user = MagicMock(return_value=episodes)
        mock_storage.delete = MagicMock(return_value=True)

        mock_episode_service._storage = mock_storage
        mock_episode_service._in_memory = {}

        # 设置较小的容量
        config = MaintenanceConfig(capacity_limit=5)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = service.evict_by_capacity(now=now)

        assert result["evicted_count"] > 0
        assert result["status"] == "success"


# ============================================================
# 4. 重要性衰减机制测试
# ============================================================


class TestImportanceDecay:
    """重要性衰减机制测试。"""

    def test_decay_reduces_importance_over_time(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """随时间推移，记忆重要性应降低。"""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=30)

        ep = make_episode(
            id="decay-test",
            intent_text="test decay",
            created_at=old_time,
            extra_data={"importance": 1.0},
        )
        mock_episode_service._in_memory = {"decay-test": ep}

        result = maintenance_service.decay_importance(now=now)

        assert result["decayed_count"] >= 1
        decayed_importance = mock_episode_service._in_memory[
            "decay-test"
        ].extra_data.get("importance", 1.0)
        assert decayed_importance < 1.0

    def test_decay_exponential_follows_formula(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """指数衰减应遵循公式：importance * (0.5 ^ (elapsed / half_life))。"""
        half_life = 86400  # 1 天
        config = MaintenanceConfig(
            decay_type="exponential",
            decay_half_life_seconds=half_life,
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        # 恰好经过一个半衰期
        created_time = now - timedelta(seconds=half_life)

        ep = make_episode(
            id="half-life-test",
            intent_text="test",
            created_at=created_time,
            extra_data={"importance": 1.0},
        )
        mock_episode_service._in_memory = {"half-life-test": ep}

        service.decay_importance(now=now)

        decayed = mock_episode_service._in_memory[
            "half-life-test"
        ].extra_data["importance"]
        # 经过一个半衰期，重要性应约为 0.5
        assert abs(decayed - 0.5) < 0.05

    def test_decay_linear_reduces_proportionally(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """线性衰减应按固定速率降低。"""
        config = MaintenanceConfig(
            decay_type="linear",
            decay_rate=0.01,  # 每秒衰减 0.01
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        # 经过 50 秒
        created_time = now - timedelta(seconds=50)

        ep = make_episode(
            id="linear-test",
            intent_text="test",
            created_at=created_time,
            extra_data={"importance": 1.0},
        )
        mock_episode_service._in_memory = {"linear-test": ep}

        service.decay_importance(now=now)

        decayed = mock_episode_service._in_memory[
            "linear-test"
        ].extra_data["importance"]
        # 线性衰减：1.0 - 0.01 * 50 = 0.5
        assert abs(decayed - 0.5) < 0.05

    def test_decay_importance_never_goes_below_zero(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
    ) -> None:
        """衰减后重要性不应低于 0。"""
        config = MaintenanceConfig(
            decay_type="linear",
            decay_rate=1.0,  # 极大衰减率
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        now = datetime.now(UTC)
        very_old = now - timedelta(days=365)

        ep = make_episode(
            id="floor-test",
            intent_text="test",
            created_at=very_old,
            extra_data={"importance": 0.5},
        )
        mock_episode_service._in_memory = {"floor-test": ep}

        service.decay_importance(now=now)

        decayed = mock_episode_service._in_memory[
            "floor-test"
        ].extra_data["importance"]
        assert decayed >= 0.0

    def test_decay_skipped_when_disabled(
        self,
        mock_memory_service: MagicMock,
    ) -> None:
        """衰减禁用时应跳过。"""
        config = MaintenanceConfig(decay_enabled=False)
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = service.decay_importance()

        assert result["status"] == "skipped"

    def test_decay_recent_memories_change_minimally(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """最近创建的记忆重要性衰减应很少。"""
        now = datetime.now(UTC)
        recent_time = now - timedelta(seconds=10)

        ep = make_episode(
            id="recent-decay",
            intent_text="recent",
            created_at=recent_time,
            extra_data={"importance": 1.0},
        )
        mock_episode_service._in_memory = {"recent-decay": ep}

        maintenance_service.decay_importance(now=now)

        decayed = mock_episode_service._in_memory[
            "recent-decay"
        ].extra_data["importance"]
        # 10 秒衰减应该很少（默认半衰期 7 天）
        assert decayed > 0.99

    def test_decay_applies_to_both_episodes_and_knowledge(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
        mock_knowledge_service: MagicMock,
    ) -> None:
        """衰减应同时应用于情景记忆和语义记忆。"""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=10)

        ep = make_episode(
            id="ep-decay",
            intent_text="episode",
            created_at=old_time,
            extra_data={"importance": 1.0},
        )
        kn = make_knowledge(
            id="kn-decay",
            content="knowledge",
            created_at=old_time,
            extra_data={"importance": 1.0},
        )

        mock_episode_service._in_memory = {"ep-decay": ep}
        mock_knowledge_service._in_memory = {"kn-decay": kn}

        result = maintenance_service.decay_importance(now=now)

        assert result["decayed_count"] >= 2

    def test_decay_without_existing_importance_uses_default(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_episode_service: MagicMock,
    ) -> None:
        """没有 importance 字段的记忆应使用默认初始值。"""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=10)

        ep = make_episode(
            id="no-importance",
            intent_text="no importance field",
            created_at=old_time,
            # 没有 extra_data
        )
        mock_episode_service._in_memory = {"no-importance": ep}

        result = maintenance_service.decay_importance(now=now)

        # 应该正常处理，不抛异常
        assert result["status"] == "success"


# ============================================================
# 5. 统一维护接口 run_maintenance 测试
# ============================================================


class TestRunMaintenance:
    """统一维护接口测试。"""

    @pytest.mark.asyncio
    async def test_run_maintenance_executes_all_tasks(
        self,
        mock_memory_service: MagicMock,
        mock_episode_service: MagicMock,
        mock_knowledge_service: MagicMock,
    ) -> None:
        """run_maintenance 应按顺序执行全部维护操作。"""
        config = MaintenanceConfig(
            ttl_enabled=True,
            capacity_limit=10000,
            decay_enabled=True,
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = await service.run_maintenance()

        assert result["status"] == "completed"
        assert "tasks" in result
        # 应包含原有的和新加的维护任务
        assert "cleanup_expired" in result["tasks"]
        assert "cleanup_ttl_expired" in result["tasks"]
        assert "evict_by_capacity" in result["tasks"]
        assert "decay_importance" in result["tasks"]

    @pytest.mark.asyncio
    async def test_run_maintenance_single_failure_continues(
        self,
        maintenance_service: MemoryMaintenanceService,
        mock_memory_service: MagicMock,
    ) -> None:
        """单个维护任务失败不应阻止后续任务。"""
        # 让 merge_similar 抛异常
        mock_knowledge = mock_memory_service._knowledge_service
        mock_knowledge._get_all_knowledge = AsyncMock(
            side_effect=RuntimeError("test error")
        )

        result = await maintenance_service.run_maintenance()

        # 应该仍然完成
        assert result["status"] == "completed"
        # 其他任务应该正常运行
        assert "cleanup_expired" in result["tasks"]

    @pytest.mark.asyncio
    async def test_run_maintenance_updates_stats(
        self,
        maintenance_service: MemoryMaintenanceService,
    ) -> None:
        """run_maintenance 应更新维护统计信息。"""
        await maintenance_service.run_maintenance()

        stats = maintenance_service.get_stats()
        assert stats["cleanup_count"] >= 1

    @pytest.mark.asyncio
    async def test_run_maintenance_respects_config_flags(
        self,
        mock_memory_service: MagicMock,
    ) -> None:
        """run_maintenance 应尊重配置中的启用/禁用标志。"""
        config = MaintenanceConfig(
            ttl_enabled=False,
            decay_enabled=False,
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = await service.run_maintenance()

        ttl_result = result["tasks"]["cleanup_ttl_expired"]
        decay_result = result["tasks"]["decay_importance"]
        assert ttl_result["status"] == "skipped"
        assert decay_result["status"] == "skipped"


# ============================================================
# 6. 衰减计算辅助函数测试
# ============================================================


class TestDecayCalculation:
    """衰减计算辅助函数测试。"""

    def test_calculate_eviction_score_combines_lru_and_importance(
        self,
        maintenance_service: MemoryMaintenanceService,
    ) -> None:
        """淘汰评分应综合 LRU 和重要性。"""
        now = datetime.now(UTC)

        score = maintenance_service._calculate_eviction_score(
            created_at=now - timedelta(hours=1),
            last_accessed_at=now.isoformat(),
            importance=0.8,
            now=now,
        )

        # 高重要性 + 最近访问 → 高分（不易被淘汰）
        assert score > 0.0

    def test_calculate_eviction_score_low_values(
        self,
        maintenance_service: MemoryMaintenanceService,
    ) -> None:
        """低重要性 + 久未访问应得低分（容易被淘汰）。"""
        now = datetime.now(UTC)

        score = maintenance_service._calculate_eviction_score(
            created_at=now - timedelta(days=30),
            last_accessed_at=(now - timedelta(days=29)).isoformat(),
            importance=0.1,
            now=now,
        )

        # 应该是一个较低的分数
        assert score >= 0.0
        assert score < 0.5

    def test_exponential_decay_formula(
        self,
        maintenance_service: MemoryMaintenanceService,
    ) -> None:
        """指数衰减公式应正确计算。"""
        half_life = maintenance_service._config.decay_half_life_seconds
        elapsed = half_life  # 恰好一个半衰期

        result = maintenance_service._apply_decay(
            importance=1.0,
            elapsed_seconds=elapsed,
        )

        assert abs(result - 0.5) < 0.01

    def test_linear_decay_formula(
        self,
        mock_memory_service: MagicMock,
    ) -> None:
        """线性衰减公式应正确计算。"""
        config = MaintenanceConfig(
            decay_type="linear",
            decay_rate=0.001,
        )
        service = MemoryMaintenanceService(
            memory_service=mock_memory_service,
            config=config,
        )

        result = service._apply_decay(
            importance=1.0,
            elapsed_seconds=500,
        )

        expected = max(0.0, 1.0 - 0.001 * 500)
        assert abs(result - expected) < 0.001

    def test_decay_floor_at_zero(
        self,
        maintenance_service: MemoryMaintenanceService,
    ) -> None:
        """衰减结果不应低于 0。"""
        result = maintenance_service._apply_decay(
            importance=0.001,
            elapsed_seconds=999999999,
        )

        assert result >= 0.0
