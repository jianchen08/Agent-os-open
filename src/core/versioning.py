"""
统一版本管理模块

提供工具、Agent、工作流的统一版本控制基础类
支持版本记录、回滚、历史查询等功能
"""

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field


class VersionType(str, Enum):
    """版本类型"""

    TOOL = "tool"
    AGENT = "agent"
    WORKFLOW = "workflow"


class VersionStatus(str, Enum):
    """版本状态"""

    DRAFT = "draft"  # 草稿
    ACTIVE = "active"  # 活跃
    DEPRECATED = "deprecated"  # 弃用
    ROLLED_BACK = "rolled_back"  # 已回滚


class VersionMetadata(BaseModel):
    """版本元数据"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version_type: VersionType
    version_number: str  # 语义化版本号，如 "1.0.0"
    status: VersionStatus
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str | None = None  # 创建者（Agent 或用户）
    description: str | None = None  # 版本描述
    changelog: str | None = None  # 变更日志
    tags: list[str] = Field(default_factory=list)  # 标签，如 ["stable", "tested"]
    metrics: dict[str, Any] = Field(
        default_factory=dict
    )  # 性能指标，如 {"success_rate": 0.95}


T = TypeVar("T")


class VersionedEntity(BaseModel, Generic[T]):
    """版本化实体"""

    metadata: VersionMetadata
    data: T  # 实体数据（工具定义、Agent 配置、工作流定义等）


class VersionHistory(BaseModel):
    """版本历史"""

    entity_id: str  # 实体 ID
    entity_name: str  # 实体名称
    version_type: VersionType
    versions: list[VersionMetadata] = Field(default_factory=list)
    current_version: str | None = None

    def add_version(self, metadata: VersionMetadata) -> None:
        """添加新版本"""
        self.versions.append(metadata)
        # 按创建时间排序
        self.versions.sort(key=lambda v: v.created_at, reverse=True)

    def get_version(self, version_number: str) -> VersionMetadata | None:
        """获取指定版本"""
        for v in self.versions:
            if v.version_number == version_number:
                return v
        return None

    def get_latest_version(self) -> VersionMetadata | None:
        """获取最新版本"""
        return self.versions[0] if self.versions else None

    def get_active_version(self) -> VersionMetadata | None:
        """获取活跃版本"""
        for v in self.versions:
            if v.status == VersionStatus.ACTIVE:
                return v
        return None


class VersionManager:
    """
    版本管理器

    提供统一的版本管理接口，支持工具、Agent、工作流的版本控制
    """

    def __init__(self):
        self._histories: dict[str, VersionHistory] = {}  # entity_id -> VersionHistory
        self._storage: dict[str, Any] = {}  # (entity_id, version_number) -> data

    def register_entity(
        self,
        entity_id: str,
        entity_name: str,
        version_type: VersionType,
    ) -> VersionHistory:
        """注册新实体"""
        if entity_id in self._histories:
            return self._histories[entity_id]

        history = VersionHistory(
            entity_id=entity_id,
            entity_name=entity_name,
            version_type=version_type,
        )
        self._histories[entity_id] = history
        return history

    def create_version(
        self,
        entity_id: str,
        data: Any,
        version_number: str,
        created_by: str | None = None,
        description: str | None = None,
        changelog: str | None = None,
        tags: list[str] = None,
        metrics: dict[str, Any] = None,
        status: VersionStatus = VersionStatus.ACTIVE,
    ) -> VersionMetadata:
        """
        创建新版本

        Args:
            entity_id: 实体 ID
            data: 实体数据
            version_number: 版本号
            created_by: 创建者
            description: 版本描述
            changelog: 变更日志
            tags: 标签
            metrics: 性能指标
            status: 版本状态

        Returns:
            版本元数据
        """
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]

        # 检查版本号是否已存在
        if history.get_version(version_number):
            raise ValueError(f"版本 {version_number} 已存在")

        # 创建版本元数据
        metadata = VersionMetadata(
            version_type=history.version_type,
            version_number=version_number,
            status=status,
            created_by=created_by,
            description=description,
            changelog=changelog,
            tags=tags or [],
            metrics=metrics or {},
        )

        # 存储数据
        storage_key = (entity_id, version_number)
        self._storage[storage_key] = data

        # 更新历史
        history.add_version(metadata)
        if status == VersionStatus.ACTIVE:
            history.current_version = version_number

        return metadata

    def get_version(self, entity_id: str, version_number: str) -> Any | None:
        """获取指定版本的数据"""
        storage_key = (entity_id, version_number)
        return self._storage.get(storage_key)

    def get_current_version(self, entity_id: str) -> Any | None:
        """获取当前版本的数据"""
        if entity_id not in self._histories:
            return None

        history = self._histories[entity_id]
        if not history.current_version:
            return None

        return self.get_version(entity_id, history.current_version)

    def get_version_info(
        self, entity_id: str, version_number: str
    ) -> VersionMetadata | None:
        """获取版本信息"""
        if entity_id not in self._histories:
            return None

        history = self._histories[entity_id]
        return history.get_version(version_number)

    def list_versions(self, entity_id: str) -> list[VersionMetadata]:
        """列出所有版本"""
        if entity_id not in self._histories:
            return []

        history = self._histories[entity_id]
        return history.versions

    def rollback(
        self,
        entity_id: str,
        target_version: str,
        rollback_by: str | None = None,
    ) -> VersionMetadata:
        """
        回滚到指定版本

        Args:
            entity_id: 实体 ID
            target_version: 目标版本号
            rollback_by: 回滚操作者

        Returns:
            新的版本元数据（回滚本身也算一个新版本）
        """
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]

        # 获取目标版本数据
        target_data = self.get_version(entity_id, target_version)
        if target_data is None:
            raise ValueError(f"版本 {target_version} 不存在")

        # 获取目标版本信息
        target_info = history.get_version(target_version)
        if not target_info:
            raise ValueError(f"版本 {target_version} 不存在")

        # 标记当前版本为已回滚
        if history.current_version:
            current_info = history.get_version(history.current_version)
            if current_info:
                current_info.status = VersionStatus.ROLLED_BACK

        # 生成新版本号（基于目标版本）
        # 例如：1.2.0 -> 1.2.1（回滚）
        base_version = target_info.version_number
        new_version = self._increment_patch_version(base_version)

        # 创建回滚版本
        metadata = self.create_version(
            entity_id=entity_id,
            data=target_data,
            version_number=new_version,
            created_by=rollback_by,
            description=f"回滚到版本 {target_version}",
            changelog=f"从 {history.current_version} 回滚到 {target_version}",
            tags=target_info.tags + ["rollback"],
            metrics=target_info.metrics,
        )

        return metadata

    def deprecate_version(
        self,
        entity_id: str,
        version_number: str,
    ) -> None:
        """弃用指定版本"""
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]
        version_info = history.get_version(version_number)

        if not version_info:
            raise ValueError(f"版本 {version_number} 不存在")

        version_info.status = VersionStatus.DEPRECATED

    def activate_version(
        self,
        entity_id: str,
        version_number: str,
    ) -> None:
        """激活指定版本"""
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]
        version_info = history.get_version(version_number)

        if not version_info:
            raise ValueError(f"版本 {version_number} 不存在")

        # 标记当前活跃版本为弃用
        if history.current_version and history.current_version != version_number:
            current_info = history.get_version(history.current_version)
            if current_info:
                current_info.status = VersionStatus.DEPRECATED

        version_info.status = VersionStatus.ACTIVE
        history.current_version = version_number

    def update_metrics(
        self,
        entity_id: str,
        version_number: str,
        metrics: dict[str, Any],
    ) -> None:
        """更新版本指标"""
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]
        version_info = history.get_version(version_number)

        if not version_info:
            raise ValueError(f"版本 {version_number} 不存在")

        version_info.metrics.update(metrics)

    def get_history(self, entity_id: str) -> VersionHistory | None:
        """获取完整版本历史"""
        return self._histories.get(entity_id)

    def compare_versions(
        self,
        entity_id: str,
        version1: str,
        version2: str,
    ) -> dict[str, Any]:
        """
        比较两个版本

        Returns:
            包含差异的字典
        """
        info1 = self.get_version_info(entity_id, version1)
        info2 = self.get_version_info(entity_id, version2)

        if not info1 or not info2:
            raise ValueError("版本不存在")

        data1 = self.get_version(entity_id, version1)
        data2 = self.get_version(entity_id, version2)

        return {
            "version1": version1,
            "version2": version2,
            "metadata_diff": {
                "status": (info1.status, info2.status),
                "tags": (info1.tags, info2.tags),
                "metrics": (info1.metrics, info2.metrics),
            },
            "data_diff": self._diff_data(data1, data2),
        }

    def _increment_patch_version(self, version: str) -> str:
        """递增补丁版本号"""
        try:
            parts = version.split(".")
            if len(parts) != 3:
                # 如果不是标准版本号，追加后缀
                return f"{version}-rollback"

            major, minor, patch = parts
            new_patch = int(patch) + 1
            return f"{major}.{minor}.{new_patch}"
        except ValueError:
            # 如果解析失败，追加后缀
            return f"{version}-rollback"

    def _diff_data(self, data1: Any, data2: Any) -> dict[str, Any]:
        """比较数据差异"""
        if isinstance(data1, dict) and isinstance(data2, dict):
            diff = {}
            all_keys = set(data1.keys()) | set(data2.keys())
            for key in all_keys:
                if key not in data1:
                    diff[key] = ("__missing__", data2[key])
                elif key not in data2:
                    diff[key] = (data1[key], "__missing__")
                elif data1[key] != data2[key]:
                    diff[key] = (data1[key], data2[key])
            return diff
        elif data1 != data2:
            return {"value": (data1, data2)}
        else:
            return {}

    def export_history(self, entity_id: str) -> str:
        """导出版本历史为 JSON"""
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]

        # 导出历史和关联的数据
        export_data = {
            "history": history.dict(),
            "versions": {},
        }

        for version_info in history.versions:
            storage_key = (entity_id, version_info.version_number)
            if storage_key in self._storage:
                # 序列化数据
                data = self._storage[storage_key]
                if hasattr(data, "dict"):
                    data = data.dict()
                elif isinstance(data, dict):
                    data = data
                else:
                    data = str(data)

                export_data["versions"][version_info.version_number] = data

        return json.dumps(export_data, ensure_ascii=False, indent=2, default=str)

    def import_history(self, entity_id: str, json_data: str) -> None:
        """从 JSON 导入版本历史"""
        try:
            import_data = json.loads(json_data)
        except json.JSONDecodeError as e:
            raise ValueError(f"无效的 JSON 数据: {e}")

        # 导入历史
        history_dict = import_data.get("history", {})
        if entity_id in self._histories:
            # 更新现有历史
            history = self._histories[entity_id]
        else:
            # 创建新历史
            history = VersionHistory(**history_dict)
            self._histories[entity_id] = history

        # 导入版本数据
        versions_data = import_data.get("versions", {})
        for version_number, data in versions_data.items():
            storage_key = (entity_id, version_number)
            self._storage[storage_key] = data

    def cleanup_old_versions(
        self,
        entity_id: str,
        keep_count: int = 5,
        keep_active: bool = True,
    ) -> list[str]:
        """
        清理旧版本

        Args:
            entity_id: 实体 ID
            keep_count: 保留版本数量
            keep_active: 是否额外保留活跃版本（除了 keep_count 之外）

        Returns:
            被删除的版本号列表

        Note:
            - 保留前 keep_count 个版本（最新的）
            - 如果 keep_active=True，额外保留所有 ACTIVE 状态的版本
            - 如果所有版本都是 ACTIVE，则只保留最新的 keep_count 个
        """
        if entity_id not in self._histories:
            raise ValueError(f"实体 {entity_id} 未注册")

        history = self._histories[entity_id]
        deleted_versions = []

        # 版本已按创建时间倒序排列（最新的在前）

        # 特殊情况：如果所有版本都是 ACTIVE，只保留最新的 keep_count 个
        all_active = all(v.status == VersionStatus.ACTIVE for v in history.versions)
        if all_active:
            # 保留前 keep_count 个，删除其余的
            for i, version_info in enumerate(history.versions):
                if i >= keep_count:
                    deleted_versions.append(version_info.version_number)
        else:
            # 正常情况：保留前 keep_count 个 + 所有 ACTIVE 版本
            indices_to_keep = set()

            for i, version_info in enumerate(history.versions):
                # 保留前 keep_count 个版本
                if i < keep_count:
                    indices_to_keep.add(i)
                # 额外保留活跃版本
                elif keep_active and version_info.status == VersionStatus.ACTIVE:
                    indices_to_keep.add(i)

            # 找出要删除的版本
            for i, version_info in enumerate(history.versions):
                if i not in indices_to_keep:
                    deleted_versions.append(version_info.version_number)

        # 执行删除
        for version_number in deleted_versions:
            storage_key = (entity_id, version_number)
            if storage_key in self._storage:
                del self._storage[storage_key]

        # 从历史中移除已删除的版本
        history.versions = [
            v for v in history.versions if v.version_number not in deleted_versions
        ]

        return deleted_versions
