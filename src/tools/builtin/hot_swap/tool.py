"""热替换与回滚工具 — 让 Agent 可以安全地替换插件和回滚配置。

支持操作：
- swap_plugin: 热替换管道中的插件（自动快照+健康检查+失败回滚）
- rollback_plugin: 回滚到上一个插件版本
- save_config_version: 保存配置版本快照
- rollback_config: 回滚配置到指定版本

暴露接口：
- hot_swap_schema: 工具参数 JSON Schema
- hot_swap_func: 工具执行函数
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 工具参数 Schema（OpenAI Function Calling 格式）
hot_swap_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "swap_plugin",
                "rollback_plugin",
                "save_config_version",
                "rollback_config",
                "list_versions",
            ],
            "description": "操作类型",
        },
        # swap_plugin 参数
        "plugin_name": {
            "type": "string",
            "description": "要替换的插件名称（swap_plugin 操作必填）",
        },
        "new_plugin_class": {
            "type": "string",
            "description": "新插件的完整类路径，如 'agent_os.plugins.input.my_plugin.MyPlugin'（swap_plugin 操作必填）",
        },
        "health_check": {
            "type": "boolean",
            "description": "是否执行健康检查（默认 true）",
            "default": True,
        },
        # rollback_plugin 参数
        "swap_id": {
            "type": "string",
            "description": "替换操作 ID（rollback_plugin 操作必填）",
        },
        # save_config_version / rollback_config 参数
        "config_id": {
            "type": "string",
            "description": "配置 ID（save_config_version / rollback_config / list_versions 操作必填）",
        },
        "config_data": {
            "type": "object",
            "description": "配置数据（save_config_version 操作必填）",
        },
        "description": {
            "type": "string",
            "description": "版本描述（save_config_version 操作可选）",
        },
        "version_id": {
            "type": "string",
            "description": "目标版本 ID（rollback_config 操作必填）",
        },
        "validator": {
            "type": "string",
            "description": "验证函数的完整路径（可选，用于 rollback_config 时验证）",
        },
    },
    "required": ["action"],
}

HOT_SWAP_DESCRIPTION = (
    "热替换与回滚工具。支持插件热替换（自动快照+健康检查+失败回滚）、"
    "插件回滚、配置版本管理和配置回滚。替换失败时自动恢复原状。"
)


def hot_swap_func(params: dict[str, Any]) -> dict[str, Any]:
    """执行热替换与回滚操作。

    Args:
        params: 工具参数，含 action 和对应操作的参数

    Returns:
        包含 success 和操作结果的字典
    """
    action = params.get("action")

    if not action:
        return {
            "success": False,
            "error": "必须提供 action 参数",
            "error_code": "MISSING_ACTION",
        }

    dispatchers = {
        "swap_plugin": _action_swap_plugin,
        "rollback_plugin": _action_rollback_plugin,
        "save_config_version": _action_save_config_version,
        "rollback_config": _action_rollback_config,
        "list_versions": _action_list_versions,
    }

    dispatcher = dispatchers.get(action)
    if dispatcher is None:
        return {
            "success": False,
            "error": f"不支持的操作: {action}",
            "error_code": "INVALID_ACTION",
        }

    return dispatcher(params)


def _action_swap_plugin(params: dict[str, Any]) -> dict[str, Any]:
    """热替换插件。

    通过反射加载新插件类，然后用 HotSwapManager 执行替换。

    Args:
        params: 含 plugin_name、new_plugin_class、health_check 等

    Returns:
        替换结果字典
    """
    plugin_name = params.get("plugin_name")
    new_plugin_class_path = params.get("new_plugin_class")
    health_check = params.get("health_check", True)

    if not plugin_name:
        return {
            "success": False,
            "error": "必须提供 plugin_name",
            "error_code": "MISSING_PLUGIN_NAME",
        }

    if not new_plugin_class_path:
        return {
            "success": False,
            "error": "必须提供 new_plugin_class",
            "error_code": "MISSING_NEW_PLUGIN_CLASS",
        }

    try:
        # 反射加载新插件类
        new_plugin = _load_plugin_instance(new_plugin_class_path)

        # 获取 PluginRegistry 和 HotSwapManager
        from tools.tool_context import HotSwapManager, PluginRegistry  # noqa: PLC0415

        plugin_registry = _get_service("plugin_registry")
        if plugin_registry is None:
            plugin_registry = PluginRegistry()

        manager = HotSwapManager(plugin_registry)

        # 执行替换（异步）
        loop = _get_or_create_event_loop()
        result = loop.run_until_complete(
            manager.swap_plugin(
                plugin_name,
                new_plugin,
                health_check=health_check,
            )
        )

        logger.info(
            "[hot_swap] 插件替换: name=%s, success=%s, swap_id=%s",
            plugin_name,
            result.success,
            result.swap_id,
        )

        return {
            "success": result.success,
            "swap_id": result.swap_id,
            "rolled_back": result.rolled_back,
            "error": result.error,
            "message": (
                f"插件 '{plugin_name}' 替换成功 (swap_id={result.swap_id})"
                if result.success
                else f"插件 '{plugin_name}' 替换失败: {result.error}"
            ),
        }

    except Exception as exc:
        logger.error("[hot_swap] 插件替换异常: %s", exc)
        return {
            "success": False,
            "error": f"插件替换异常: {exc}",
            "error_code": "SWAP_FAILED",
        }


def _action_rollback_plugin(params: dict[str, Any]) -> dict[str, Any]:
    """回滚插件到替换前版本。

    Args:
        params: 含 swap_id

    Returns:
        回滚结果字典
    """
    swap_id = params.get("swap_id")

    if not swap_id:
        return {
            "success": False,
            "error": "必须提供 swap_id",
            "error_code": "MISSING_SWAP_ID",
        }

    try:
        from tools.tool_context import HotSwapManager, PluginRegistry  # noqa: PLC0415

        plugin_registry = _get_service("plugin_registry")
        if plugin_registry is None:
            plugin_registry = PluginRegistry()

        manager = HotSwapManager(plugin_registry)

        loop = _get_or_create_event_loop()
        rolled_back = loop.run_until_complete(manager.rollback(swap_id))

        logger.info(
            "[hot_swap] 插件回滚: swap_id=%s, success=%s",
            swap_id,
            rolled_back,
        )

        return {
            "success": rolled_back,
            "swap_id": swap_id,
            "message": (
                f"插件回滚成功 (swap_id={swap_id})"
                if rolled_back
                else f"插件回滚失败，swap_id={swap_id} 不存在或已过期"
            ),
        }

    except Exception as exc:
        logger.error("[hot_swap] 插件回滚异常: %s", exc)
        return {
            "success": False,
            "error": f"插件回滚异常: {exc}",
            "error_code": "ROLLBACK_FAILED",
        }


def _action_save_config_version(params: dict[str, Any]) -> dict[str, Any]:
    """保存配置版本快照。

    Args:
        params: 含 config_id、config_data、description

    Returns:
        保存结果字典
    """
    config_id = params.get("config_id")
    config_data = params.get("config_data")
    description = params.get("description", "")

    if not config_id:
        return {
            "success": False,
            "error": "必须提供 config_id",
            "error_code": "MISSING_CONFIG_ID",
        }

    if not config_data:
        return {
            "success": False,
            "error": "必须提供 config_data",
            "error_code": "MISSING_CONFIG_DATA",
        }

    try:
        manager = _get_rollback_manager()
        version = manager.save_version(
            config_id,
            config_data,
            description=description,
        )

        logger.info(
            "[hot_swap] 配置版本保存: config_id=%s, version_id=%s",
            config_id,
            version.version_id,
        )

        return {
            "success": True,
            "config_id": config_id,
            "version_id": version.version_id,
            "description": description,
            "message": f"配置版本已保存 (version_id={version.version_id})",
        }

    except Exception as exc:
        logger.error("[hot_swap] 配置版本保存异常: %s", exc)
        return {
            "success": False,
            "error": f"配置版本保存失败: {exc}",
            "error_code": "SAVE_VERSION_FAILED",
        }


def _action_rollback_config(params: dict[str, Any]) -> dict[str, Any]:
    """回滚配置到指定版本。

    Args:
        params: 含 version_id

    Returns:
        回滚结果字典
    """
    version_id = params.get("version_id")

    if not version_id:
        return {
            "success": False,
            "error": "必须提供 version_id",
            "error_code": "MISSING_VERSION_ID",
        }

    try:
        manager = _get_rollback_manager()
        loop = _get_or_create_event_loop()
        success = loop.run_until_complete(manager.rollback_to_version(version_id))

        logger.info(
            "[hot_swap] 配置回滚: version_id=%s, success=%s",
            version_id,
            success,
        )

        return {
            "success": success,
            "version_id": version_id,
            "message": (f"配置已回滚到版本 {version_id}" if success else f"配置回滚失败，版本 {version_id} 不存在"),
        }

    except Exception as exc:
        logger.error("[hot_swap] 配置回滚异常: %s", exc)
        return {
            "success": False,
            "error": f"配置回滚失败: {exc}",
            "error_code": "ROLLBACK_CONFIG_FAILED",
        }


def _action_list_versions(params: dict[str, Any]) -> dict[str, Any]:
    """列出配置的所有版本。

    Args:
        params: 含 config_id

    Returns:
        版本列表字典
    """
    config_id = params.get("config_id")

    if not config_id:
        return {
            "success": False,
            "error": "必须提供 config_id",
            "error_code": "MISSING_CONFIG_ID",
        }

    try:
        manager = _get_rollback_manager()
        versions = manager.list_versions(config_id)

        version_list = [
            {
                "version_id": v.version_id,
                "config_id": v.config_id,
                "description": v.description,
                "timestamp": v.timestamp,
            }
            for v in versions
        ]

        return {
            "success": True,
            "config_id": config_id,
            "versions": version_list,
            "count": len(version_list),
            "message": f"配置 '{config_id}' 共 {len(version_list)} 个版本",
        }

    except Exception as exc:
        logger.error("[hot_swap] 版本列表获取异常: %s", exc)
        return {
            "success": False,
            "error": f"版本列表获取失败: {exc}",
            "error_code": "LIST_VERSIONS_FAILED",
        }


def _load_plugin_instance(class_path: str) -> Any:
    """通过反射加载并实例化插件类。

    Args:
        class_path: 完整类路径，如 'agent_os.plugins.input.my_plugin.MyPlugin'

    Returns:
        插件实例

    Raises:
        ImportError: 模块不存在
        AttributeError: 类不存在
    """
    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"无效的类路径: {class_path}，格式应为 'module.ClassName'")

    module_path, class_name = parts
    import importlib  # noqa: PLC0415

    module = importlib.import_module(module_path)
    plugin_class = getattr(module, class_name)
    return plugin_class()


def _get_rollback_manager() -> RollbackManager:  # noqa: F821
    """获取或创建 RollbackManager 实例（模块级单例）。

    Returns:
        RollbackManager 实例
    """
    from tools.tool_context import RollbackManager  # noqa: PLC0415

    # 尝试获取已有的 manager
    manager = _get_service("rollback_manager")
    if manager is not None:
        return manager

    # 使用模块级单例
    global _rollback_manager_instance  # noqa: PLW0603
    if _rollback_manager_instance is not None:
        return _rollback_manager_instance

    # 尝试获取 config_store
    config_store = _get_service("pipeline_config_store")
    _rollback_manager_instance = RollbackManager(config_store=config_store)
    return _rollback_manager_instance


# 模块级 RollbackManager 单例
_rollback_manager_instance: Any = None


def _get_service(service_name: str) -> Any:
    """获取已注册的服务实例。

    Args:
        service_name: 服务名称

    Returns:
        服务实例，不可用时返回 None
    """
    try:
        from channels.cli.cli_main import CLIMain  # noqa: PLC0415

        app = CLIMain.get_instance()
        if app is not None:
            return app._services.get(service_name)
    except Exception:
        pass

    return None


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """获取或创建事件循环。

    Returns:
        事件循环实例
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop
