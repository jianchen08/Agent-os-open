"""回滚管理器模块。

管理进化过程中的检查点和回滚操作，整合已有的回滚设施经验。
参考 src/pipeline/rollback.py 的设计模式。

暴露接口：
- create_checkpoint(description) -> str
- rollback(checkpoint_id) -> bool
- list_checkpoints() -> list[dict]
- RollbackManager: 回滚管理器类
"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """进化检查点。

    记录某个时刻的完整状态快照，用于回滚。

    Attributes:
        checkpoint_id: 检查点唯一标识
        description: 检查点描述
        timestamp: 创建时间戳
        loaded_plugins: 当时的已加载插件列表
        plugin_states: 当时的插件状态快照
    """

    checkpoint_id: str
    description: str
    timestamp: float = field(default_factory=time.monotonic)
    loaded_plugins: list[str] = field(default_factory=list)
    plugin_states: dict[str, Any] = field(default_factory=dict)


class RollbackManager:
    """进化回滚管理器。

    管理进化过程中的检查点和回滚操作：
    - 创建检查点（记录当前加载的插件列表和状态）
    - 回滚到指定检查点（卸载新增的插件）
    - 列出所有检查点

    整合 src/pipeline/rollback.py 的设计经验：
    - 版本快照模式
    - 超限自动清理
    - 失败自动恢复

    Attributes:
        _hot_loader: 热加载器实例（用于卸载插件）
        _checkpoints: 检查点存储 {checkpoint_id: Checkpoint}
        _max_checkpoints: 最大保留检查点数
    """

    def __init__(
        self,
        hot_loader: Any | None = None,
        max_checkpoints: int = 10,
        storage_dir: str | None = None,
    ) -> None:
        """初始化回滚管理器。

        Args:
            hot_loader: 热加载器实例（需实现 unload_plugin 方法）
            max_checkpoints: 最大保留检查点数
            storage_dir: 检查点持久化目录
        """
        self._hot_loader = hot_loader
        self._max_checkpoints = max_checkpoints
        self._storage_dir = Path(storage_dir) if storage_dir else None
        self._checkpoints: dict[str, Checkpoint] = {}

        # 加载持久化的检查点
        if self._storage_dir:
            self._load_checkpoints()

    def create_checkpoint(
        self,
        description: str = "",
        hot_loader: Any | None = None,
    ) -> str:
        """创建检查点。

        记录当前加载的插件列表和状态，用于后续回滚。

        Args:
            description: 检查点描述
            hot_loader: 热加载器实例（覆盖构造时的实例）

        Returns:
            检查点 ID
        """
        loader = hot_loader or self._hot_loader

        # 获取当前加载的插件列表
        loaded_plugins: list[str] = []
        if loader is not None and hasattr(loader, "get_loaded_plugins"):
            loaded_plugins = loader.get_loaded_plugins()

        # 创建检查点
        checkpoint_id = f"cp_{uuid.uuid4().hex[:8]}"
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            description=description,
            timestamp=time.monotonic(),
            loaded_plugins=list(loaded_plugins),
            plugin_states={name: {"status": "loaded"} for name in loaded_plugins},
        )

        self._checkpoints[checkpoint_id] = checkpoint

        # 清理超限的旧检查点
        self._cleanup_old_checkpoints()

        # 持久化
        self._persist_checkpoint(checkpoint)

        logger.info(
            "[RollbackManager] 创建检查点: id='%s', plugins=%d, desc='%s'",
            checkpoint_id,
            len(loaded_plugins),
            description,
        )
        return checkpoint_id

    def rollback(
        self,
        checkpoint_id: str,
        hot_loader: Any | None = None,
    ) -> bool:
        """回滚到指定检查点。

        卸载检查点之后新增的所有插件。

        Args:
            checkpoint_id: 目标检查点 ID
            hot_loader: 热加载器实例（覆盖构造时的实例）

        Returns:
            是否回滚成功
        """
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            logger.warning(
                "[RollbackManager] 检查点不存在: id='%s'", checkpoint_id
            )
            return False

        loader = hot_loader or self._hot_loader
        if loader is None:
            logger.warning("[RollbackManager] 无热加载器，无法执行回滚")
            return False

        logger.info(
            "[RollbackManager] 开始回滚: target='%s', desc='%s'",
            checkpoint_id,
            checkpoint.description,
        )

        try:
            # 获取当前加载的插件
            current_plugins: list[str] = []
            if hasattr(loader, "get_loaded_plugins"):
                current_plugins = loader.get_loaded_plugins()

            # 计算需要卸载的插件（当前有但检查点时没有的）
            original_plugins = set(checkpoint.loaded_plugins)
            plugins_to_unload = [
                p for p in current_plugins if p not in original_plugins
            ]

            # 卸载新增插件
            unload_errors: list[str] = []
            for plugin_name in plugins_to_unload:
                try:
                    if hasattr(loader, "unload_plugin"):
                        success = loader.unload_plugin(plugin_name)
                        if not success:
                            unload_errors.append(plugin_name)
                except Exception as exc:
                    logger.warning(
                        "[RollbackManager] 卸载插件失败: name='%s', error=%s",
                        plugin_name,
                        exc,
                    )
                    unload_errors.append(plugin_name)

            if unload_errors:
                logger.warning(
                    "[RollbackManager] 回滚部分失败: unloaded=%d, failed=%d",
                    len(plugins_to_unload) - len(unload_errors),
                    len(unload_errors),
                )
                return len(unload_errors) < len(plugins_to_unload)

            logger.info(
                "[RollbackManager] 回滚成功: target='%s', unloaded=%d",
                checkpoint_id,
                len(plugins_to_unload),
            )
            return True

        except Exception as exc:
            logger.error(
                "[RollbackManager] 回滚异常: target='%s', error=%s",
                checkpoint_id,
                exc,
            )
            return False

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有检查点。

        Returns:
            检查点信息列表（按时间倒序）
        """
        checkpoints = sorted(
            self._checkpoints.values(),
            key=lambda c: c.timestamp,
            reverse=True,
        )

        return [
            {
                "checkpoint_id": cp.checkpoint_id,
                "description": cp.description,
                "timestamp": cp.timestamp,
                "loaded_plugins_count": len(cp.loaded_plugins),
                "loaded_plugins": cp.loaded_plugins,
            }
            for cp in checkpoints
        ]

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """获取指定检查点。

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            检查点实例，不存在返回 None
        """
        return self._checkpoints.get(checkpoint_id)

    # -- 内部方法 --------------------------------------------------------

    def _cleanup_old_checkpoints(self) -> None:
        """清理超限的旧检查点。"""
        if len(self._checkpoints) <= self._max_checkpoints:
            return

        # 按时间排序，移除最旧的
        sorted_ids = sorted(
            self._checkpoints.keys(),
            key=lambda cid: self._checkpoints[cid].timestamp,
        )

        while len(self._checkpoints) > self._max_checkpoints:
            oldest_id = sorted_ids.pop(0)
            self._checkpoints.pop(oldest_id, None)

            # 删除持久化文件
            if self._storage_dir:
                file_path = self._storage_dir / f"{oldest_id}.json"
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass

            logger.debug(
                "[RollbackManager] 清理旧检查点: id='%s'", oldest_id
            )

    def _persist_checkpoint(self, checkpoint: Checkpoint) -> None:
        """持久化检查点到文件。

        Args:
            checkpoint: 检查点实例
        """
        if self._storage_dir is None:
            return

        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            file_path = self._storage_dir / f"{checkpoint.checkpoint_id}.json"
            data = {
                "checkpoint_id": checkpoint.checkpoint_id,
                "description": checkpoint.description,
                "timestamp": checkpoint.timestamp,
                "loaded_plugins": checkpoint.loaded_plugins,
            }
            file_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(
                "[RollbackManager] 持久化检查点失败: %s", exc
            )

    def _load_checkpoints(self) -> None:
        """从文件加载检查点。"""
        if self._storage_dir is None or not self._storage_dir.exists():
            return

        try:
            for file_path in self._storage_dir.glob("cp_*.json"):
                try:
                    data = json.loads(
                        file_path.read_text(encoding="utf-8")
                    )
                    checkpoint = Checkpoint(
                        checkpoint_id=data["checkpoint_id"],
                        description=data.get("description", ""),
                        timestamp=data.get("timestamp", 0),
                        loaded_plugins=data.get("loaded_plugins", []),
                    )
                    self._checkpoints[checkpoint.checkpoint_id] = checkpoint
                except Exception:
                    continue
        except Exception as exc:
            logger.warning(
                "[RollbackManager] 加载检查点失败: %s", exc
            )
