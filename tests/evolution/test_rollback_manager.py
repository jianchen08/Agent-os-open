"""回滚管理器模块测试。

覆盖 RollbackManager 的核心功能：
- create_checkpoint: 创建检查点
- rollback: 回滚到检查点
- list_checkpoints: 列出检查点
- plugin_states 是字典而非列表（SF-01 修复验证）
- 持久化和清理
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evolution.rollback_manager import Checkpoint, RollbackManager


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def rollback_manager(tmp_path: Path) -> RollbackManager:
    """回滚管理器实例（带持久化目录）。"""
    return RollbackManager(
        hot_loader=None,
        storage_dir=str(tmp_path / "checkpoints"),
    )


@pytest.fixture
def mock_loader() -> MagicMock:
    """模拟热加载器。"""
    loader = MagicMock()
    loader.get_loaded_plugins.return_value = []
    loader.unload_plugin.return_value = True
    return loader


# =========================================================================
# Checkpoint 数据类测试
# =========================================================================


class TestCheckpoint:
    """Checkpoint 数据类测试。"""

    def test_checkpoint_creation(self) -> None:
        """检查点创建。"""
        cp = Checkpoint(
            checkpoint_id="cp_test",
            description="test checkpoint",
        )
        assert cp.checkpoint_id == "cp_test"
        assert cp.description == "test checkpoint"
        assert cp.loaded_plugins == []
        assert cp.plugin_states == {}

    def test_checkpoint_with_plugins(self) -> None:
        """带插件信息的检查点。"""
        cp = Checkpoint(
            checkpoint_id="cp_plugins",
            description="with plugins",
            loaded_plugins=["plugin_a", "plugin_b"],
            plugin_states={"plugin_a": {"status": "loaded"}},
        )
        assert len(cp.loaded_plugins) == 2
        assert cp.plugin_states["plugin_a"]["status"] == "loaded"


# =========================================================================
# create_checkpoint 测试
# =========================================================================


class TestCreateCheckpoint:
    """创建检查点测试。"""

    def test_create_checkpoint(self, rollback_manager: RollbackManager) -> None:
        """创建检查点。"""
        cp_id = rollback_manager.create_checkpoint("test checkpoint")
        assert cp_id.startswith("cp_")
        assert len(cp_id) > 3

    def test_create_checkpoint_with_loader(self, mock_loader: MagicMock) -> None:
        """使用 loader 创建检查点记录已加载插件。"""
        mock_loader.get_loaded_plugins.return_value = ["plugin_a", "plugin_b"]

        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("with plugins", hot_loader=mock_loader)

        cp = manager.get_checkpoint(cp_id)
        assert cp is not None
        assert "plugin_a" in cp.loaded_plugins
        assert "plugin_b" in cp.loaded_plugins

    def test_create_checkpoint_unique_ids(self, rollback_manager: RollbackManager) -> None:
        """每次创建的检查点 ID 唯一。"""
        ids = set()
        for i in range(10):
            ids.add(rollback_manager.create_checkpoint(f"cp_{i}"))
        assert len(ids) == 10

    def test_plugin_states_is_dict(self, mock_loader: MagicMock) -> None:
        """plugin_states 是字典而非列表（SF-01修复验证）。"""
        mock_loader.get_loaded_plugins.return_value = ["p1", "p2"]

        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("sf01 test", hot_loader=mock_loader)

        cp = manager.get_checkpoint(cp_id)
        assert cp is not None
        assert isinstance(cp.plugin_states, dict), (
            "plugin_states 应为 dict 类型，而非 list（SF-01 修复）"
        )
        assert "p1" in cp.plugin_states
        assert cp.plugin_states["p1"]["status"] == "loaded"


# =========================================================================
# rollback 测试
# =========================================================================


class TestRollback:
    """回滚测试。"""

    def test_rollback_to_checkpoint(self, mock_loader: MagicMock) -> None:
        """回滚到检查点。"""
        mock_loader.get_loaded_plugins.return_value = ["old_plugin"]
        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("before", hot_loader=mock_loader)

        # 模拟新增插件
        mock_loader.get_loaded_plugins.return_value = ["old_plugin", "new_plugin"]

        result = manager.rollback(cp_id, hot_loader=mock_loader)
        assert result is True
        mock_loader.unload_plugin.assert_called_once_with("new_plugin")

    def test_rollback_nonexistent_checkpoint(self, rollback_manager: RollbackManager) -> None:
        """回滚到不存在的检查点返回 False。"""
        result = rollback_manager.rollback("nonexistent")
        assert result is False

    def test_rollback_no_loader(self) -> None:
        """无热加载器时回滚返回 False。"""
        manager = RollbackManager(hot_loader=None)
        cp_id = manager.create_checkpoint("test")
        result = manager.rollback(cp_id)
        assert result is False

    def test_rollback_multiple_new_plugins(self, mock_loader: MagicMock) -> None:
        """回滚卸载多个新增插件。"""
        mock_loader.get_loaded_plugins.return_value = ["p1"]
        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("multi", hot_loader=mock_loader)

        mock_loader.get_loaded_plugins.return_value = ["p1", "p2", "p3"]

        result = manager.rollback(cp_id, hot_loader=mock_loader)
        assert result is True
        assert mock_loader.unload_plugin.call_count == 2

    def test_rollback_partial_failure(self, mock_loader: MagicMock) -> None:
        """部分卸载失败时返回 True（只要有部分成功）。"""
        mock_loader.get_loaded_plugins.return_value = ["p1"]
        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("partial", hot_loader=mock_loader)

        mock_loader.get_loaded_plugins.return_value = ["p1", "p2", "p3"]
        # 第二个插件卸载失败
        mock_loader.unload_plugin.side_effect = [True, RuntimeError("fail")]

        result = manager.rollback(cp_id, hot_loader=mock_loader)
        assert result is True  # 部分成功

    def test_rollback_all_failure(self, mock_loader: MagicMock) -> None:
        """全部卸载失败时返回 False。"""
        mock_loader.get_loaded_plugins.return_value = ["p1"]
        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("all_fail", hot_loader=mock_loader)

        mock_loader.get_loaded_plugins.return_value = ["p1", "p2"]
        mock_loader.unload_plugin.side_effect = RuntimeError("fail")

        result = manager.rollback(cp_id, hot_loader=mock_loader)
        assert result is False

    def test_rollback_no_new_plugins(self, mock_loader: MagicMock) -> None:
        """无新增插件时回滚返回 True。"""
        mock_loader.get_loaded_plugins.return_value = ["p1"]
        manager = RollbackManager(hot_loader=mock_loader)
        cp_id = manager.create_checkpoint("no_new", hot_loader=mock_loader)

        # 插件列表没变化
        result = manager.rollback(cp_id, hot_loader=mock_loader)
        assert result is True
        mock_loader.unload_plugin.assert_not_called()


# =========================================================================
# list_checkpoints 测试
# =========================================================================


class TestListCheckpoints:
    """列出检查点测试。"""

    def test_list_checkpoints_empty(self, rollback_manager: RollbackManager) -> None:
        """初始无检查点。"""
        checkpoints = rollback_manager.list_checkpoints()
        assert checkpoints == []

    def test_list_checkpoints(self, rollback_manager: RollbackManager) -> None:
        """列出检查点。"""
        rollback_manager.create_checkpoint("cp1")
        rollback_manager.create_checkpoint("cp2")

        checkpoints = rollback_manager.list_checkpoints()
        assert len(checkpoints) == 2

    def test_list_checkpoints_sorted_by_time_desc(
        self, rollback_manager: RollbackManager,
    ) -> None:
        """检查点按时间倒序排列。"""
        ids = []
        for i in range(3):
            ids.append(rollback_manager.create_checkpoint(f"cp_{i}"))

        checkpoints = rollback_manager.list_checkpoints()
        assert len(checkpoints) == 3
        # 最新的在前
        assert checkpoints[0]["checkpoint_id"] == ids[2]

    def test_list_checkpoints_structure(self, rollback_manager: RollbackManager) -> None:
        """检查点信息结构正确。"""
        rollback_manager.create_checkpoint("struct test")

        checkpoints = rollback_manager.list_checkpoints()
        cp = checkpoints[0]
        assert "checkpoint_id" in cp
        assert "description" in cp
        assert "timestamp" in cp
        assert "loaded_plugins_count" in cp
        assert "loaded_plugins" in cp


# =========================================================================
# get_checkpoint 测试
# =========================================================================


class TestGetCheckpoint:
    """获取检查点测试。"""

    def test_get_checkpoint(self, rollback_manager: RollbackManager) -> None:
        """获取已创建的检查点。"""
        cp_id = rollback_manager.create_checkpoint("test cp")
        cp = rollback_manager.get_checkpoint(cp_id)

        assert cp is not None
        assert cp.checkpoint_id == cp_id
        assert cp.description == "test cp"

    def test_get_nonexistent_checkpoint(self, rollback_manager: RollbackManager) -> None:
        """获取不存在的检查点返回 None。"""
        result = rollback_manager.get_checkpoint("nonexistent")
        assert result is None


# =========================================================================
# 持久化与清理测试
# =========================================================================


class TestPersistenceAndCleanup:
    """持久化和清理测试。"""

    def test_persistence(self, tmp_path: Path) -> None:
        """检查点持久化到文件并可重新加载。"""
        storage_dir = str(tmp_path / "cp_store")

        # 创建
        manager1 = RollbackManager(storage_dir=storage_dir)
        cp_id = manager1.create_checkpoint("persistent cp")

        # 重新加载
        manager2 = RollbackManager(storage_dir=storage_dir)
        cp = manager2.get_checkpoint(cp_id)

        assert cp is not None
        assert cp.description == "persistent cp"

    def test_persistence_file_created(self, tmp_path: Path) -> None:
        """持久化文件被创建。"""
        storage_dir = tmp_path / "cp_files"
        manager = RollbackManager(storage_dir=str(storage_dir))
        cp_id = manager.create_checkpoint("file test")

        cp_file = storage_dir / f"{cp_id}.json"
        assert cp_file.exists()

        data = json.loads(cp_file.read_text(encoding="utf-8"))
        assert data["checkpoint_id"] == cp_id

    def test_max_checkpoints_cleanup(self) -> None:
        """检查点数量超限时自动清理。"""
        manager = RollbackManager(max_checkpoints=3)

        for i in range(5):
            manager.create_checkpoint(f"cp_{i}")

        checkpoints = manager.list_checkpoints()
        assert len(checkpoints) <= 3

    def test_cleanup_keeps_latest(self) -> None:
        """清理时保留最新的检查点。"""
        manager = RollbackManager(max_checkpoints=2)

        ids = []
        for i in range(4):
            ids.append(manager.create_checkpoint(f"cp_{i}"))

        checkpoints = manager.list_checkpoints()
        stored_ids = {cp["checkpoint_id"] for cp in checkpoints}
        # 最新的两个应保留
        assert ids[2] in stored_ids
        assert ids[3] in stored_ids
        # 最旧的应被清理
        assert ids[0] not in stored_ids

    def test_no_storage_dir_no_persistence(self) -> None:
        """无 storage_dir 时不持久化。"""
        manager = RollbackManager(storage_dir=None)
        cp_id = manager.create_checkpoint("no persist")

        # 获取检查点应该成功（内存中有）
        assert manager.get_checkpoint(cp_id) is not None
