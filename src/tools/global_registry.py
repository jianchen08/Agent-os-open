"""
全局工具注册表

暴露接口：
- get_global_tool_registry_sync() -> ToolRegistry：get_global_tool_registry_sync功能
- is_initialized() -> bool：is_initialized功能
- create_tool_registry(sync_service: Optional, lazy_load: bool, load_builtin_tools: bool) -> ToolRegistry：create_tool_registry功能
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from tools.registry import ToolRegistry
from tools.types import ToolLevel

logger = logging.getLogger(__name__)

# 全局工具注册表（单例）
_global_tool_registry: ToolRegistry | None = None
_registry_lock = asyncio.Lock()
_initialized = False


def get_global_tool_registry_sync() -> ToolRegistry:
    """同步获取全局工具注册表（向后兼容）"""
    global _global_tool_registry  # noqa: PLW0603

    if _global_tool_registry is None:
        logger.warning(
            "[GlobalRegistry] 同步访问全局注册表，但尚未初始化。"
            "将创建空注册表。建议使用 await get_global_tool_registry()"
        )
        _global_tool_registry = ToolRegistry()

    return _global_tool_registry


async def get_global_tool_registry(
    force_reload: bool = False,
    session: Any | None = None,
) -> ToolRegistry:
    """获取全局工具注册表（懒加载）"""
    global _global_tool_registry, _initialized  # noqa: PLW0603

    if _global_tool_registry is None or force_reload:
        async with _registry_lock:
            # 双重检查锁定
            if _global_tool_registry is None or force_reload:
                logger.info("[GlobalRegistry] 初始化全局工具注册表（只注册核心工具）...")

                from tools.builtin import register_core_tools  # noqa: PLC0415
                from tools.loader import init_dynamic_tool_loader  # noqa: PLC0415

                _global_tool_registry = ToolRegistry()

                # 只注册核心系统工具（5-10个）
                # 如果提供了 session，也会注册需要 session 的核心工具
                registered_tools = register_core_tools(
                    registry=_global_tool_registry,
                    session=session,
                )

                # 初始化动态加载器（其他工具按需加载）
                init_dynamic_tool_loader(_global_tool_registry)

                _initialized = True

                logger.info(
                    "[GlobalRegistry] 全局工具注册表初始化完成 | 核心工具数=%d | 其他工具将按需动态加载",
                    len(registered_tools),
                )

    return _global_tool_registry


def is_initialized() -> bool:
    """检查全局工具注册表是否已初始化"""
    return _initialized


async def reload_global_registry() -> ToolRegistry:
    """重新加载全局工具注册表"""
    logger.info("[GlobalRegistry] 重新加载全局工具注册表...")
    return await get_global_tool_registry(force_reload=True)


# =============================================================================
# 工厂函数（从 registry_factory.py 合并）
# =============================================================================


def create_tool_registry(
    sync_service: Optional = None,
    lazy_load: bool = True,
    load_builtin_tools: bool = True,
) -> ToolRegistry:
    """创建 ToolRegistry 实例并正确初始化"""
    registry = ToolRegistry(sync_service=sync_service, lazy_load=lazy_load)

    if load_builtin_tools:
        # 同步初始化核心系统工具
        _sync_initialize_builtin_tools(registry)

    logger.info(f"ToolRegistry 已创建并初始化 (lazy_load={lazy_load})")
    return registry


def _sync_initialize_builtin_tools(registry: ToolRegistry) -> None:
    """同步初始化核心系统工具到注册表"""
    core_tools = []
    try:
        from tools.builtin import get_all_builtin_tools  # noqa: PLC0415

        # 获取所有工具实例，筛选系统级别的核心工具
        all_tools = get_all_builtin_tools()
        for tool_instance in all_tools:
            tool_def = tool_instance.get_tool_definition()
            # 仅注册系统级别的工具（level = SYSTEM）
            if tool_def.level == ToolLevel.SYSTEM:
                name = registry.register_with_handler(
                    tool=tool_def,
                    handler=tool_instance.execute,
                )
                core_tools.append(name)

        logger.info(f"成功加载 {len(core_tools)} 个核心系统工具: {core_tools}")

    except Exception as e:
        logger.warning(f"核心工具加载失败: {e}", exc_info=True)

    # 记录最终状态
    total_tools = registry.count()
    logger.info(f"ToolRegistry 初始化完成，{total_tools} 个核心工具可用 (其他工具将按需加载)")


async def initialize_tools_async(
    registry: ToolRegistry,
    session: Any | None = None,
    evaluator_callback: Callable | None = None,
) -> None:
    """异步初始化工具到注册表"""
    # 1. 加载内置工具（主要工具来源）
    try:
        from tools.builtin import register_all_builtin_tools  # noqa: PLC0415

        # 传递 session，确保 task_submit 等工具能正确注册
        builtin_names = register_all_builtin_tools(
            registry=registry,
            session=session,
            evaluator_callback=evaluator_callback,
        )
        logger.info(f"成功加载 {len(builtin_names)} 个内置工具: {builtin_names}")

    except Exception as e:
        logger.warning(f"内置工具加载失败: {e}")

    # 2. 加载 MCP 工具（自动扫描 mcp-servers/ 目录）
    try:
        from tools.mcp_loader import MCPToolLoader  # noqa: PLC0415

        loader = MCPToolLoader()
        mcp_count = 0

        # 2.1 自动扫描 mcp-servers/*/mcp.json
        mcp_servers_dir = Path("mcp-servers")
        if mcp_servers_dir.exists():
            mcp_tools = await loader.load_from_directory(mcp_servers_dir, include_disabled=False)
            for tool in mcp_tools:
                try:
                    registry.register(tool, overwrite=False)
                    mcp_count += 1
                except Exception:
                    pass

        # 2.2 兼容：如果存在全局配置文件也加载
        for config_path in [Path("config/mcp.json"), Path(".mcp.json")]:
            if config_path.exists():
                try:
                    mcp_tools = await loader.load_from_config(config_path, include_disabled=False)
                    for tool in mcp_tools:
                        try:
                            registry.register(tool, overwrite=False)
                            mcp_count += 1
                        except Exception:
                            pass
                except Exception:
                    pass

        if mcp_count > 0:
            logger.info(f"成功加载 {mcp_count} 个 MCP 工具")

    except Exception as e:
        logger.debug(f"MCP 工具加载失败（可选功能）: {e}")

    # 记录最终状态
    total_tools = registry.count()
    logger.info(f"ToolRegistry 初始化完成，共 {total_tools} 个工具可用")
