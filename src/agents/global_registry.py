"""全局 Agent 注册表单例。

对齐 tools/global_registry.py 的模式：模块级全局实例 + 异步锁 + 双重检查。

暴露接口：
- get_global_agent_registry(force_reload) -> AgentRegistry：异步主路径
- get_global_agent_registry_sync() -> AgentRegistry：同步兜底
- reload_global_registry() -> AgentRegistry：热重载（ConfigCenter 回调用）
- is_initialized() -> bool：状态查询
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agents.registry import AgentRegistry

logger = logging.getLogger(__name__)

# 全局 Agent 注册表（单例）
_global_agent_registry: AgentRegistry | None = None
_registry_lock = asyncio.Lock()
_initialized = False

# 默认 agent 配置目录
_DEFAULT_CONFIG_DIR = Path("config/agents")


def get_global_agent_registry_sync() -> AgentRegistry:
    """同步获取全局 Agent 注册表。

    未初始化时创建实例并尝试从 config/agents 加载。已初始化则直接返回。

    适合无法 await 的场景（如工具内部、同步代码路径）。
    若需要确保配置完整加载，优先使用 await get_global_agent_registry()。

    Returns:
        全局唯一的 AgentRegistry 实例。
    """
    global _global_agent_registry  # noqa: PLW0603
    if _global_agent_registry is None:
        _global_agent_registry = AgentRegistry()
        if _DEFAULT_CONFIG_DIR.exists():
            try:
                count = _global_agent_registry.load_directory(_DEFAULT_CONFIG_DIR)
                logger.info(
                    "[GlobalAgentRegistry] 同步加载 %d 个 agent 配置",
                    count,
                )
            except Exception as exc:
                logger.warning(
                    "[GlobalAgentRegistry] 同步加载失败: %s",
                    exc,
                )
    return _global_agent_registry


async def get_global_agent_registry(
    force_reload: bool = False,
) -> AgentRegistry:
    """异步获取全局 Agent 注册表（懒加载 + 双重检查锁）。

    首次调用从 config/agents 加载所有 agent 配置。force_reload=True 时
    重新创建实例并加载（热重载用）。

    Args:
        force_reload: 是否强制重新加载。

    Returns:
        全局唯一的 AgentRegistry 实例。
    """
    global _global_agent_registry, _initialized  # noqa: PLW0603

    if _global_agent_registry is None or force_reload:
        async with _registry_lock:
            # 双重检查锁定
            if _global_agent_registry is None or force_reload:
                logger.info("[GlobalAgentRegistry] 初始化全局 Agent 注册表...")
                _global_agent_registry = AgentRegistry()

                if _DEFAULT_CONFIG_DIR.exists():
                    try:
                        count = _global_agent_registry.load_directory(
                            _DEFAULT_CONFIG_DIR,
                        )
                        logger.info(
                            "[GlobalAgentRegistry] 加载了 %d 个 agent 配置",
                            count,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[GlobalAgentRegistry] 加载失败: %s",
                            exc,
                        )

                _initialized = True

    return _global_agent_registry


async def reload_global_registry() -> AgentRegistry:
    """重新加载全局 Agent 注册表（热重载入口）。

    Returns:
        重新加载后的 AgentRegistry 实例。
    """
    logger.info("[GlobalAgentRegistry] 重新加载全局 Agent 注册表...")
    return await get_global_agent_registry(force_reload=True)


def is_initialized() -> bool:
    """检查全局 Agent 注册表是否已初始化。

    Returns:
        已初始化返回 True。
    """
    return _initialized
