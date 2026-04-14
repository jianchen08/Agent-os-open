"""配置版本与回滚管理器单元测试。

测试 RollbackManager 的版本保存、列表、更新回滚等功能。
"""

from __future__ import annotations

import pytest

from pipeline.config_store import PipelineConfig, PipelineConfigStore
from pipeline.rollback import ConfigVersion, RollbackManager, RollbackResult


# --- 测试用例 ---


class TestVersionSave:
    """版本保存测试。"""

    def test_save_version_returns_config_version(self) -> None:
        """save_version → 返回 ConfigVersion 实例。"""
        manager = RollbackManager()
        version = manager.save_version(
            "test_config",
            {"name": "test", "value": 1},
            description="初始版本",
        )

        assert isinstance(version, ConfigVersion)
        assert version.config_id == "test_config"
        assert version.config_data == {"name": "test", "value": 1}
        assert version.description == "初始版本"
        assert version.version_id != ""

    def test_save_version_copies_data(self) -> None:
        """save_version → 深拷贝配置数据。"""
        manager = RollbackManager()
        data = {"name": "test", "items": [1, 2, 3]}
        version = manager.save_version("test_config", data)

        # 修改原始数据不影响快照
        data["items"].append(4)
        assert version.config_data["items"] == [1, 2, 3]


class TestVersionList:
    """版本列表测试。"""

    def test_list_versions_returns_saved(self) -> None:
        """save_version → list_versions 返回该版本。"""
        manager = RollbackManager()
        manager.save_version("config_a", {"v": 1}, description="v1")
        manager.save_version("config_a", {"v": 2}, description="v2")

        versions = manager.list_versions("config_a")
        assert len(versions) == 2
        assert versions[0].config_data == {"v": 1}
        assert versions[1].config_data == {"v": 2}

    def test_list_versions_separate_config_ids(self) -> None:
        """不同 config_id 的版本互不干扰。"""
        manager = RollbackManager()
        manager.save_version("config_a", {"v": 1})
        manager.save_version("config_b", {"v": 2})

        versions_a = manager.list_versions("config_a")
        versions_b = manager.list_versions("config_b")
        assert len(versions_a) == 1
        assert len(versions_b) == 1

    def test_list_versions_empty(self) -> None:
        """不存在的 config_id → 空列表。"""
        manager = RollbackManager()
        versions = manager.list_versions("nonexistent")
        assert versions == []


class TestGetLatestVersion:
    """获取最新版本测试。"""

    def test_get_latest_version(self) -> None:
        """get_latest_version → 返回最新保存的版本。"""
        manager = RollbackManager()
        manager.save_version("config_a", {"v": 1}, description="v1")
        manager.save_version("config_a", {"v": 2}, description="v2")
        manager.save_version("config_a", {"v": 3}, description="v3")

        latest = manager.get_latest_version("config_a")
        assert latest is not None
        assert latest.config_data == {"v": 3}
        assert latest.description == "v3"

    def test_get_latest_version_none(self) -> None:
        """不存在的 config_id → None。"""
        manager = RollbackManager()
        latest = manager.get_latest_version("nonexistent")
        assert latest is None


class TestGetVersion:
    """获取指定版本测试。"""

    def test_get_version_by_id(self) -> None:
        """get_version → 按 version_id 获取版本。"""
        manager = RollbackManager()
        version = manager.save_version("config_a", {"v": 1})

        found = manager.get_version(version.version_id)
        assert found is version

    def test_get_version_not_found(self) -> None:
        """不存在的 version_id → None。"""
        manager = RollbackManager()
        found = manager.get_version("nonexistent")
        assert found is None


class TestUpdateWithRollback:
    """带回滚的配置更新测试。"""

    @pytest.mark.asyncio
    async def test_update_validation_passes(self) -> None:
        """update_with_rollback 验证通过 → 成功更新。"""
        manager = RollbackManager()
        result = await manager.update_with_rollback(
            "config_a",
            {"name": "test", "value": 1},
            validator=lambda d: True,
            description="v1",
        )

        assert result.success is True
        assert result.rolled_back is False
        assert result.error is None

    @pytest.mark.asyncio
    async def test_update_validation_fails_rollback(self) -> None:
        """update_with_rollback 验证失败 → 自动回滚。"""
        config_store = PipelineConfigStore()
        manager = RollbackManager(config_store=config_store)

        # 先注册一个初始配置
        initial_config = PipelineConfig(
            pipeline_id="config_a",
            name="Config A",
        )
        config_store.register("config_a", initial_config)

        # 保存初始版本
        manager.save_version(
            "config_a",
            {"pipeline_id": "config_a", "name": "Config A"},
            description="initial",
        )

        # 更新并验证失败
        result = await manager.update_with_rollback(
            "config_a",
            {"pipeline_id": "config_a", "name": "Bad Config"},
            validator=lambda d: False,
            description="bad_update",
        )

        assert result.success is False
        assert result.rolled_back is True
        assert "验证失败" in result.error

    @pytest.mark.asyncio
    async def test_update_without_validator(self) -> None:
        """update_with_rollback 无 validator → 直接成功。"""
        manager = RollbackManager()
        result = await manager.update_with_rollback(
            "config_a",
            {"name": "test"},
            description="no_validator",
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_update_validator_raises_exception(self) -> None:
        """validator 抛异常 → 视为验证失败 → 回滚。"""
        config_store = PipelineConfigStore()
        manager = RollbackManager(config_store=config_store)

        initial_config = PipelineConfig(
            pipeline_id="config_a",
            name="Config A",
        )
        config_store.register("config_a", initial_config)
        manager.save_version(
            "config_a",
            {"pipeline_id": "config_a", "name": "Config A"},
            description="initial",
        )

        def bad_validator(d: dict) -> bool:
            raise ValueError("validator crashed")

        result = await manager.update_with_rollback(
            "config_a",
            {"pipeline_id": "config_a", "name": "Bad"},
            validator=bad_validator,
        )

        assert result.success is False
        assert result.rolled_back is True


class TestRollbackToVersion:
    """回滚到指定版本测试。"""

    @pytest.mark.asyncio
    async def test_rollback_to_version(self) -> None:
        """rollback_to_version → 恢复到指定版本。"""
        config_store = PipelineConfigStore()
        manager = RollbackManager(config_store=config_store)

        # 保存两个版本
        v1 = manager.save_version(
            "config_a",
            {"pipeline_id": "config_a", "name": "Version 1"},
            description="v1",
        )
        v2 = manager.save_version(
            "config_a",
            {"pipeline_id": "config_a", "name": "Version 2"},
            description="v2",
        )

        # 注册最新版本到 store
        config_store.register(
            "config_a",
            PipelineConfig(pipeline_id="config_a", name="Version 2"),
        )

        # 回滚到 v1
        success = await manager.rollback_to_version(v1.version_id)
        assert success is True

        # config_store 中应该是 v1 的数据
        config = config_store.get("config_a")
        assert config is not None
        assert config.name == "Version 1"

    @pytest.mark.asyncio
    async def test_rollback_to_nonexistent_version(self) -> None:
        """回滚到不存在的版本 → False。"""
        manager = RollbackManager()
        success = await manager.rollback_to_version("nonexistent")
        assert success is False

    @pytest.mark.asyncio
    async def test_rollback_without_config_store(self) -> None:
        """无 config_store 时 rollback_to_version → True（仅版本数据恢复）。"""
        manager = RollbackManager(config_store=None)
        v1 = manager.save_version("config_a", {"name": "v1"})

        success = await manager.rollback_to_version(v1.version_id)
        assert success is True


class TestMaxVersions:
    """版本数限制测试。"""

    def test_max_versions_limits_history(self) -> None:
        """max_versions 限制 → 旧版本被清理。"""
        manager = RollbackManager(max_versions=3)

        versions: list[ConfigVersion] = []
        for i in range(5):
            v = manager.save_version("config_a", {"v": i}, description=f"v{i}")
            versions.append(v)

        # 只应保留最后 3 个版本
        all_versions = manager.list_versions("config_a")
        assert len(all_versions) == 3
        # 保留的应该是 v2, v3, v4
        assert all_versions[0].config_data == {"v": 2}
        assert all_versions[1].config_data == {"v": 3}
        assert all_versions[2].config_data == {"v": 4}

    def test_max_versions_cleanup_removes_from_index(self) -> None:
        """清理旧版本时，version_id 从索引中移除。"""
        manager = RollbackManager(max_versions=2)

        v1 = manager.save_version("config_a", {"v": 1}, description="v1")
        manager.save_version("config_a", {"v": 2}, description="v2")
        manager.save_version("config_a", {"v": 3}, description="v3")

        # v1 应该被清理
        assert manager.get_version(v1.version_id) is None

    def test_max_versions_default(self) -> None:
        """默认 max_versions=10。"""
        manager = RollbackManager()

        for i in range(12):
            manager.save_version("config_a", {"v": i})

        all_versions = manager.list_versions("config_a")
        assert len(all_versions) == 10


class TestUpdateWithRollbackNoStore:
    """无 config_store 时的 update_with_rollback 测试。"""

    @pytest.mark.asyncio
    async def test_update_no_store_validation_passes(self) -> None:
        """无 config_store 时验证通过 → 成功。"""
        manager = RollbackManager(config_store=None)
        result = await manager.update_with_rollback(
            "config_a",
            {"name": "test"},
            validator=lambda d: True,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_update_no_store_validation_fails(self) -> None:
        """无 config_store 时验证失败 → 回滚（仅清理失败版本）。"""
        manager = RollbackManager(config_store=None)
        manager.save_version("config_a", {"name": "original"}, description="initial")

        result = await manager.update_with_rollback(
            "config_a",
            {"name": "bad"},
            validator=lambda d: False,
        )

        assert result.success is False
        assert result.rolled_back is True
