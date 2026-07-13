"""配置版本与回滚管理器。

管理配置的版本快照，支持在配置更新失败时自动回滚。
提供 save_version / list_versions / update_with_rollback / rollback_to_version 等操作。
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pipeline.config_store import PipelineConfigStore

logger = logging.getLogger(__name__)


@dataclass
class ConfigVersion:
    """配置版本快照。

    保存配置的某个版本数据，供回滚使用。

    Attributes:
        version_id: 版本唯一标识
        config_id: 配置 ID
        config_data: 配置数据快照
        timestamp: 快照时间戳
        description: 版本描述
    """

    version_id: str
    config_id: str
    config_data: dict[str, Any]
    timestamp: float = field(default_factory=time.monotonic)
    description: str = ""


@dataclass
class RollbackResult:
    """回滚结果。

    Attributes:
        success: 操作是否成功
        version_id: 相关版本 ID
        rolled_back: 是否已执行回滚
        error: 错误信息，成功时为 None
    """

    success: bool = False
    version_id: str = ""
    rolled_back: bool = False
    error: str | None = None


class RollbackManager:
    """配置版本与回滚管理器。

    管理配置的版本快照，支持在配置更新失败时自动回滚。

    流程：
    1. 更新前保存版本快照
    2. 执行配置更新
    3. 验证更新后的配置
    4. 验证失败 → 回滚到上一版本

    Attributes:
        _config_store: 管道配置存储实例（可选）
        _max_versions: 每个配置 ID 保留的最大版本数
        _versions: version_id → ConfigVersion 的映射
        _config_versions: config_id → version_id 列表的映射（按时间排序）
    """

    def __init__(
        self,
        config_store: PipelineConfigStore | None = None,
        max_versions: int = 10,
    ) -> None:
        """初始化回滚管理器。

        Args:
            config_store: 管道配置存储实例，提供实际的配置更新能力
            max_versions: 每个配置 ID 保留的最大版本数
        """
        self._config_store = config_store
        self._max_versions = max_versions
        self._versions: dict[str, ConfigVersion] = {}
        self._config_versions: dict[str, list[str]] = {}

    def save_version(
        self,
        config_id: str,
        config_data: dict[str, Any],
        *,
        description: str = "",
    ) -> ConfigVersion:
        """保存配置版本快照。

        创建版本快照并存入版本历史，超过 max_versions 时自动清理最旧的版本。

        Args:
            config_id: 配置 ID
            config_data: 配置数据
            description: 版本描述

        Returns:
            保存的版本快照
        """
        version_id = str(uuid.uuid4())[:8]
        version = ConfigVersion(
            version_id=version_id,
            config_id=config_id,
            config_data=copy.deepcopy(config_data),
            description=description,
        )

        self._versions[version_id] = version

        if config_id not in self._config_versions:
            self._config_versions[config_id] = []
        self._config_versions[config_id].append(version_id)

        # 清理超限的旧版本
        self._cleanup_old_versions(config_id)

        logger.info(
            "Config version saved: config_id='%s', version_id='%s', description='%s'",
            config_id,
            version_id,
            description,
        )
        return version

    def get_version(self, version_id: str) -> ConfigVersion | None:
        """获取指定版本快照。

        Args:
            version_id: 版本 ID

        Returns:
            版本快照实例，不存在时返回 None
        """
        return self._versions.get(version_id)

    def list_versions(self, config_id: str) -> list[ConfigVersion]:
        """列出指定配置的所有版本。

        按保存时间从旧到新排序。

        Args:
            config_id: 配置 ID

        Returns:
            版本快照列表
        """
        version_ids = self._config_versions.get(config_id, [])
        versions: list[ConfigVersion] = []
        for vid in version_ids:
            version = self._versions.get(vid)
            if version is not None:
                versions.append(version)
        return versions

    def get_latest_version(self, config_id: str) -> ConfigVersion | None:
        """获取指定配置的最新版本。

        Args:
            config_id: 配置 ID

        Returns:
            最新版本快照，不存在时返回 None
        """
        version_ids = self._config_versions.get(config_id, [])
        if not version_ids:
            return None
        latest_id = version_ids[-1]
        return self._versions.get(latest_id)

    async def update_with_rollback(
        self,
        config_id: str,
        new_config_data: dict[str, Any],
        *,
        validator: Callable[[dict[str, Any]], bool] | None = None,
        description: str = "",
    ) -> RollbackResult:
        """带回滚的配置更新。

        流程：
        1. 保存当前版本快照
        2. 执行更新
        3. 验证（如果有 validator）
        4. 验证失败 → 自动回滚

        Args:
            config_id: 配置 ID
            new_config_data: 新配置数据
            validator: 验证函数，返回 True 表示配置有效
            description: 版本描述

        Returns:
            RollbackResult 包含操作结果和回滚状态
        """
        # 1. 保存当前版本快照
        previous_version = self.get_latest_version(config_id)
        previous_data: dict[str, Any] | None = None
        if previous_version is not None:
            previous_data = previous_version.config_data.copy()

        # 保存新版本快照
        new_version = self.save_version(
            config_id,
            new_config_data,
            description=description or "update_with_rollback",
        )

        # 2. 执行更新（如果有 config_store）
        if self._config_store is not None:
            try:
                self._apply_config_to_store(config_id, new_config_data)
            except Exception as exc:
                # 更新失败 → 回滚
                logger.error(
                    "Config update failed, rolling back: config_id='%s', error=%s",
                    config_id,
                    exc,
                )
                rolled_back = await self._rollback_config(
                    config_id,
                    previous_data,
                    new_version.version_id,
                )
                return RollbackResult(
                    success=False,
                    version_id=new_version.version_id,
                    rolled_back=rolled_back,
                    error=f"配置更新失败: {exc}",
                )

        # 3. 验证
        if validator is not None:
            try:
                is_valid = validator(new_config_data)
            except Exception as exc:
                is_valid = False
                logger.warning(
                    "Validator raised exception: config_id='%s', error=%s",
                    config_id,
                    exc,
                )

            if not is_valid:
                # 验证失败 → 回滚
                logger.warning(
                    "Config validation failed, rolling back: config_id='%s'",
                    config_id,
                )
                rolled_back = await self._rollback_config(
                    config_id,
                    previous_data,
                    new_version.version_id,
                )
                return RollbackResult(
                    success=False,
                    version_id=new_version.version_id,
                    rolled_back=rolled_back,
                    error="配置验证失败",
                )

        logger.info(
            "Config update succeeded: config_id='%s', version_id='%s'",
            config_id,
            new_version.version_id,
        )
        return RollbackResult(
            success=True,
            version_id=new_version.version_id,
        )

    async def rollback_to_version(self, version_id: str) -> bool:
        """回滚到指定版本。

        从版本快照中恢复配置数据，并更新到 config_store。

        Args:
            version_id: 目标版本 ID

        Returns:
            是否回滚成功
        """
        version = self._versions.get(version_id)
        if version is None:
            logger.warning("Rollback failed: version_id '%s' not found", version_id)
            return False

        try:
            if self._config_store is not None:
                self._apply_config_to_store(version.config_id, version.config_data)

            logger.info(
                "Rollback to version succeeded: version_id='%s', config_id='%s'",
                version_id,
                version.config_id,
            )
            return True

        except Exception as exc:
            logger.error("Rollback to version failed: %s", exc)
            return False

    def _apply_config_to_store(self, config_id: str, config_data: dict[str, Any]) -> None:
        """将配置数据应用到 config_store。

        Args:
            config_id: 配置 ID
            config_data: 配置数据

        Raises:
            ValueError: config_store 未配置时抛出
        """
        if self._config_store is None:
            raise ValueError("config_store 未配置，无法应用配置")

        # 构建 PipelineConfig 实例
        from pipeline.config_store import PipelineConfig  # noqa: PLC0415

        # 如果 config_data 已经是完整的配置，尝试直接构建 PipelineConfig
        if isinstance(config_data, dict):
            # 尝试从 config_data 构建 PipelineConfig
            if "pipeline_id" in config_data and "name" in config_data:
                # 已经是 PipelineConfig 格式的数据
                config = PipelineConfig(
                    pipeline_id=config_data.get("pipeline_id", config_id),
                    name=config_data.get("name", config_id),
                    input_routes=config_data.get("input_routes", []),
                    output_routes=config_data.get("output_routes", []),
                    plugins=config_data.get("plugins", []),
                    core_plugins=config_data.get("core_plugins", {}),
                    max_iterations=config_data.get("max_iterations", 100),
                )
            else:
                # 通用格式，使用 config_id 作为标识
                config = PipelineConfig(
                    pipeline_id=config_id,
                    name=config_id,
                )
            self._config_store.register(config_id, config)
        else:
            # 直接注册
            self._config_store.register(config_id, config_data)

    async def _rollback_config(
        self,
        config_id: str,
        previous_data: dict[str, Any] | None,
        failed_version_id: str,
    ) -> bool:
        """执行配置回滚。

        恢复到 previous_data，并清理失败版本。

        Args:
            config_id: 配置 ID
            previous_data: 之前的配置数据
            failed_version_id: 失败的版本 ID

        Returns:
            是否回滚成功
        """
        if previous_data is None:
            # 没有之前的版本，仅移除失败版本
            self._remove_version(failed_version_id)
            logger.info(
                "Rollback: no previous version, removed failed version '%s'",
                failed_version_id,
            )
            return True

        try:
            if self._config_store is not None:
                self._apply_config_to_store(config_id, previous_data)

            # 清理失败版本
            self._remove_version(failed_version_id)

            logger.info(
                "Rollback succeeded: config_id='%s', restored to previous version",
                config_id,
            )
            return True

        except Exception as exc:
            logger.error("Rollback failed: %s", exc)
            return False

    def _remove_version(self, version_id: str) -> None:
        """移除指定版本。

        Args:
            version_id: 版本 ID
        """
        version = self._versions.pop(version_id, None)
        if version is not None:
            config_id = version.config_id
            version_ids = self._config_versions.get(config_id, [])
            if version_id in version_ids:
                version_ids.remove(version_id)
            if not version_ids:
                del self._config_versions[config_id]

    def _cleanup_old_versions(self, config_id: str) -> None:
        """清理超限的旧版本。

        当某个 config_id 的版本数超过 max_versions 时，
        移除最旧的版本。

        Args:
            config_id: 配置 ID
        """
        version_ids = self._config_versions.get(config_id, [])
        while len(version_ids) > self._max_versions:
            oldest_id = version_ids.pop(0)
            self._versions.pop(oldest_id, None)
            logger.debug(
                "Cleaned up old version: config_id='%s', version_id='%s'",
                config_id,
                oldest_id,
            )
