"""基础设施服务获取的统一入口。

所有模块通过此公共接口获取 ExecutionRecordStorage 等基础设施服务，
避免在各文件中重复定义 _get_execution_record_storage()。

公共接口：
- get_execution_record_storage() -> Any: 获取全局 ExecutionRecordStorage 实例
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["get_execution_record_storage"]


def get_execution_record_storage() -> Any:
    """从 ServiceProvider 获取全局 ExecutionRecordStorage 实例。

    当 ServiceProvider 中未注册时，使用 get_or_create 懒加载创建。

    Returns:
        ExecutionRecordStorage 实例，服务不可用返回 None
    """
    try:
        from infrastructure.execution_record_storage import ExecutionRecordStorage  # noqa: PLC0415
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        storage = provider.get("execution_record_storage")
        if storage is not None:
            return storage
        return provider.get_or_create(
            "execution_record_storage",
            lambda: ExecutionRecordStorage(
                data_dir=str(Path(__file__).resolve().parent.parent.parent / "data" / "pipelines"),
            ),
        )
    except Exception as exc:
        logger.warning(
            "get_execution_record_storage: 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None
