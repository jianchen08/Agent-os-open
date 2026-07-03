"""
YAML 配置同步基类

提供 YAML 配置文件到数据库同步的通用功能。
核心原则：YAML 文件是配置的唯一来源，数据库用于运行时读取。
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class YamlConfigSyncService(ABC):
    """
    YAML 配置同步服务基类

    子类需要实现：
    - _get_default_config_dir(): 返回默认配置目录
    - _get_config_id_field(): 返回配置 ID 字段名
    - _get_entity_class(): 返回数据库实体类
    - _get_entity_id_field(): 返回实体 ID 字段名
    - _get_checksum_from_entity(): 从实体中获取校验和
    - _prepare_entity_data(): 准备实体数据
    - _get_log_prefix(): 返回日志前缀
    """

    def __init__(self, config_dir: Path | None = None):
        """
        初始化同步服务

        Args:
            config_dir: 配置目录，默认为子类指定的默认目录
        """
        self.config_dir = config_dir or self._get_default_config_dir()

    @abstractmethod
    def _get_default_config_dir(self) -> Path:
        """
        获取默认配置目录

        Returns:
            配置目录路径
        """
        pass

    @abstractmethod
    def _get_config_id_field(self) -> str:
        """
        获取配置 ID 字段名

        Returns:
            配置文件中标识配置的字段名
        """
        pass

    @abstractmethod
    def _get_entity_class(self) -> type:
        """
        获取数据库实体类

        Returns:
            SQLAlchemy 实体类
        """
        pass

    @abstractmethod
    def _get_entity_id_field(self) -> str:
        """
        获取实体 ID 字段名

        Returns:
            数据库实体中标识配置的字段名
        """
        pass

    @abstractmethod
    def _get_checksum_from_entity(self, entity: Any) -> str | None:
        """
        从实体中获取校验和

        Args:
            entity: 数据库实体实例

        Returns:
            校验和字符串，如果不存在返回 None
        """
        pass

    @abstractmethod
    def _prepare_entity_data(self, data: dict, checksum: str) -> dict:
        """
        准备实体数据（转换为数据库格式）

        Args:
            data: YAML 数据
            checksum: 校验和

        Returns:
            数据库字段字典
        """
        pass

    @abstractmethod
    def _get_log_prefix(self) -> str:
        """
        获取日志前缀

        Returns:
            用于日志标识的前缀字符串
        """
        pass

    def _calculate_checksum(self, data: dict) -> str:
        """
        计算配置的校验和

        Args:
            data: 配置数据

        Returns:
            MD5 校验和
        """
        content = str(sorted(data.items()))
        return hashlib.md5(content.encode()).hexdigest()

    def _scan_yaml_files(self) -> list[Path]:
        """
        递归扫描所有 YAML 配置文件

        Returns:
            YAML 文件路径列表
        """
        yaml_files = []
        if not self.config_dir.exists():
            return yaml_files

        for pattern in ["**/*.yaml", "**/*.yml"]:
            yaml_files.extend(self.config_dir.glob(pattern))

        return yaml_files

    def _should_skip_file(self, yaml_file: Path) -> bool:
        """
        判断是否应该跳过某个文件

        Args:
            yaml_file: YAML 文件路径

        Returns:
            是否应该跳过
        """
        return yaml_file.name.startswith("_") or "README" in yaml_file.name

    async def sync_all(
        self,
        session: AsyncSession,
        force: bool = False,
    ) -> dict[str, int]:
        """
        同步所有配置

        Args:
            session: 数据库会话
            force: 是否强制同步

        Returns:
            同步统计 {created, updated, skipped, failed}
        """
        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

        yaml_files = self._scan_yaml_files()
        log_prefix = self._get_log_prefix()
        logger.info(f"[{log_prefix}同步] 发现 {len(yaml_files)} 个配置文件")

        for yaml_file in yaml_files:
            if self._should_skip_file(yaml_file):
                continue

            try:
                result = await self.sync_one(session, yaml_file, force)
                stats[result] += 1
            except Exception as e:
                logger.error(f"[{log_prefix}同步] 同步失败: {yaml_file}, 错误: {e}")
                stats["failed"] += 1

        await session.commit()
        logger.info(f"[{log_prefix}同步] 完成: {stats}")
        return stats

    async def sync_one(
        self,
        session: AsyncSession,
        yaml_file: Path,
        force: bool = False,
    ) -> str:
        """
        同步单个配置

        Args:
            session: 数据库会话
            yaml_file: YAML 文件路径
            force: 是否强制同步

        Returns:
            操作类型: created | updated | skipped
        """
        log_prefix = self._get_log_prefix()
        config_id_field = self._get_config_id_field()
        entity_class = self._get_entity_class()
        entity_id_field = self._get_entity_id_field()

        with open(yaml_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or config_id_field not in data:
            logger.warning(f"[{log_prefix}同步] 无效配置: {yaml_file}")
            return "skipped"

        config_id = data[config_id_field]
        checksum = self._calculate_checksum(data)

        # 查询数据库
        entity_id_column = getattr(entity_class, entity_id_field)
        result = await session.execute(select(entity_class).where(entity_id_column == config_id))
        entity = result.scalar_one_or_none()

        # 检查是否需要更新
        if entity and not force:
            db_checksum = self._get_checksum_from_entity(entity)
            if db_checksum == checksum:
                logger.debug(f"[{log_prefix}同步] 跳过（未变更）: {config_id}")
                return "skipped"

        # 准备数据
        entity_data = self._prepare_entity_data(data, checksum)

        if entity:
            # 更新
            for key, value in entity_data.items():
                setattr(entity, key, value)
            logger.info(f"[{log_prefix}同步] 更新: {config_id}")
            return "updated"
        # 创建
        new_entity = entity_class(**entity_data)
        session.add(new_entity)
        logger.info(f"[{log_prefix}同步] 创建: {config_id}")
        return "created"
