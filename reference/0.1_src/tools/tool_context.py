"""工具上下文桥接层 — 解耦 tools/builtin/ 与 pipeline/、infrastructure/ 的直接依赖。

所有 tools/builtin/ 下的工具文件通过本模块间接访问管道类型和服务，
不再直接 ``from pipeline import`` 或 ``from infrastructure import``。

本模块位于 tools/ 层（非 builtin/），允许导入 pipeline 和 infrastructure。

暴露接口：
- PipelineMessage, MessageType: 管道消息类型（dataclass / enum）
- emit: 管道消息发送函数
- HotSwapManager: 热替换管理器
- PluginRegistry: 插件注册表
- RollbackManager: 回滚管理器
- get_engine_registry: 引擎注册表获取函数
- PipelineConfig, PipelineConfigStore: 管道配置类型
- PipelineEngine: 管道引擎
- get_service: 统一服务获取入口（替代直接 import infrastructure.service_provider）
"""

from __future__ import annotations

from typing import Any

# ── 管道类型 re-export ──────────────────────────────────
# 集中导入，tools/builtin/ 不再直接 from pipeline import
from pipeline.config_store import PipelineConfig, PipelineConfigStore
from pipeline.engine import PipelineEngine
from pipeline.hot_swap import HotSwapManager
from pipeline.message_bus import emit
from pipeline.message_types import MessageType, PipelineMessage
from pipeline.registry import PluginRegistry, get_engine_registry
from pipeline.rollback import RollbackManager


def get_service(name: str) -> Any:
    """统一服务获取入口。

    DEBT: 工具通过此函数获取基础设施服务，避免散点 import。ceiling: 仍委托 ServiceProvider。
    upgrade: 迁移到构造函数注入后，此函数可移除。

    Args:
        name: 服务名称（如 "task_worker"、"pipeline_factory"）

    Returns:
        服务实例，找不到返回 None
    """
    from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

    return get_service_provider().get(name)


__all__ = [
    "HotSwapManager",
    "MessageType",
    "PipelineConfig",
    "PipelineConfigStore",
    "PipelineEngine",
    "PipelineMessage",
    "PluginRegistry",
    "RollbackManager",
    "emit",
    "get_engine_registry",
    "get_service",
]
