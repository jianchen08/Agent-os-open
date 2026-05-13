"""插件解析器。

从 PipelineEngine 中迁出的插件解析相关逻辑，负责：
1. 根据 Agent 配置构建最终插件列表
2. 将 Agent 配置的插件覆盖合并到 PluginRegistry
3. 将 Agent 配置中的 model_name/model_tier 覆盖到 llm_call 核心插件
4. 从 llm.yaml defaults.tiers 解析 tier 为 model_id
5. 检查插件名称是否匹配禁用列表

所有函数均为模块级公开函数，不依赖 PipelineEngine 实例，
通过参数显式传入所需依赖（plugin_registry、services 等）。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IPlugin

logger = logging.getLogger(__name__)


def build_plugin_list(
    plugin_registry: Any,
    agent_config: Any | None,
) -> list[IPlugin]:
    """根据 Agent 配置构建最终插件列表。

    配置合并逻辑：
    1. 从 PluginRegistry 获取所有已注册插件（Pipeline 默认）
    2. 移除 agent_config.plugins.disabled 中声明的插件
    3. 添加 agent_config.plugins.enabled 中声明的非默认插件

    Args:
        plugin_registry: PluginRegistry 实例
        agent_config: Agent 配置实例

    Returns:
        最终生效的插件列表
    """
    result: list[IPlugin] = []

    for plugin in plugin_registry._plugins.values():
        result.append(plugin)

    if not agent_config or not hasattr(agent_config, "plugins"):
        return result

    plugins_config = agent_config.plugins

    if hasattr(plugins_config, "disabled") and plugins_config.disabled:
        result = [
            p
            for p in result
            if not matches_disabled(p.name, plugins_config.disabled)
        ]

    if hasattr(plugins_config, "enabled") and plugins_config.enabled:
        for name, config in plugins_config.enabled.items():
            existing = plugin_registry.get(name)
            if existing is not None:
                if isinstance(config, dict) and hasattr(existing, "_config"):
                    merged_config = {**existing._config, **config}
                    try:
                        new_plugin = type(existing)(config=merged_config)
                        # 在 result 列表中替换旧引用
                        for idx, p in enumerate(result):
                            if p is existing:
                                result[idx] = new_plugin
                                break
                    except Exception:
                        pass
                continue
            logger.info(
                "Agent enables non-default plugin: %s (config=%s)",
                name,
                config,
            )

    return result


def apply_agent_plugin_configs(
    plugin_registry: Any,
    agent_config: Any | None,
) -> None:
    """将 Agent 配置的插件覆盖直接合并到 plugin_registry。

    遍历 agent_config.plugins.enabled，将配置合并到 registry 中
    已有的同名插件实例上（原地替换），使后续 _run_loop 通过
    registry.get() 拿到的就是合并后的插件。

    Args:
        plugin_registry: PluginRegistry 实例
        agent_config: Agent 配置实例
    """
    if not agent_config:
        return

    # 自动注入 agent_level 到 context_build 插件
    # BUG-FIX-fix_20260513_agent_level_wrong:
    # 问题根因: ContextBuildPlugin 的 agent_level 默认为 "L1"，
    #           但 Agent YAML 配置中未通过 plugins.enabled 覆盖该值，
    #           导致所有 Agent 的管道 state 中 AGENT_LEVEL 始终为 "L1"，
    #           task_submit 计算 child_level = min(1+1, 3) = 2，永远是 L2。
    # 修复方案: 在此自动将 agent_config.level 注入到 context_build 插件配置中。
    if hasattr(agent_config, "level") and agent_config.level:
        cb_plugin = plugin_registry.get("context_build")
        if cb_plugin and hasattr(cb_plugin, "_config"):
            level_value = agent_config.level.value if hasattr(agent_config.level, "value") else str(agent_config.level)
            if cb_plugin._config.get("agent_level") != level_value:
                merged_config = {**cb_plugin._config, "agent_level": level_value}
                try:
                    new_plugin = type(cb_plugin)(config=merged_config)
                    plugin_registry._plugins["context_build"] = new_plugin
                    for core_key, pname in list(plugin_registry._core_plugins.items()):
                        if pname == "context_build":
                            plugin_registry._core_plugins[core_key] = "context_build"
                    logger.debug(
                        "Agent plugin config auto-injected: context_build.agent_level=%s",
                        level_value,
                    )
                except Exception:
                    logger.debug("Agent plugin config auto-inject failed for context_build")

    if not hasattr(agent_config, "plugins"):
        return

    plugins_config = agent_config.plugins
    if not hasattr(plugins_config, "enabled") or not plugins_config.enabled:
        return

    for name, override in plugins_config.enabled.items():
        if not isinstance(override, dict):
            continue
        existing = plugin_registry.get(name)
        if existing is None:
            continue
        if not hasattr(existing, "_config"):
            continue
        merged_config = {**existing._config, **override}
        try:
            new_plugin = type(existing)(config=merged_config)
            plugin_registry._plugins[name] = new_plugin
            # 同步 core_plugins 映射
            for core_key, pname in list(
                plugin_registry._core_plugins.items()
            ):
                if pname == name:
                    plugin_registry._core_plugins[core_key] = name
            logger.debug(
                "Agent plugin config merged: %s + %s -> %s",
                name,
                list(override.keys()),
                list(merged_config.keys()),
            )
        except Exception:
            logger.debug(
                "Agent plugin config merge failed for %s",
                name,
            )


def apply_agent_model_override(
    plugin_registry: Any,
    agent_config: Any | None,
    services: dict[str, Any],
) -> None:
    """将 Agent 配置中的 model_name/model_tier 覆盖到 llm_call 核心插件。

    优先级：model_name > model_tier > defaults.chat
    - model_name: 直接使用指定的模型标识
    - model_tier: 从 llm.yaml defaults.tiers 解析为 model_name
    - Router 模式：直接切换路由别名，不重建插件
    - 直连模式：从 llm.yaml 加载完整配置重建插件

    Args:
        plugin_registry: PluginRegistry 实例
        agent_config: Agent 配置实例
        services: 服务字典（包含 model_loader 等）
    """
    if not agent_config or not hasattr(agent_config, "model_name"):
        return

    model_id = agent_config.model_name

    # model_tier 解析：当 model_name 未指定时，从 tiers 映射
    if not model_id and hasattr(agent_config, "model_tier") and agent_config.model_tier:
        model_id = resolve_tier(agent_config.model_tier, services)
        if model_id:
            logger.info(
                "[apply_agent_model_override] model_tier=%s -> model_name=%s",
                agent_config.model_tier,
                model_id,
            )

    if not model_id:
        return

    llm_call = plugin_registry.get_core("llm_call")
    if llm_call is None:
        return

    # Router 模式：只需切换路由别名，不重建插件
    if getattr(llm_call, "_use_router", False):
        llm_call._model = model_id
        # 更新 context_window（从 model_loader 读取）
        model_loader = services.get("model_loader") if services else None
        if model_loader:
            conf = model_loader.get_model_config(model_id)
            if conf:
                llm_call._provider = conf.get("provider", llm_call._provider)
                llm_call._context_window = conf.get("context_window")
        logger.info(
            "[apply_agent_model_override] Router 模式切换模型: %s (provider=%s, context_window=%s)",
            model_id,
            llm_call._provider,
            llm_call._context_window,
        )
        return

    # 直连模式：重建插件（原有逻辑）
    model_loader = None
    if services:
        model_loader = services.get("model_loader")

    if model_loader is None:
        try:
            from config.models import get_model_config_loader

            model_loader = get_model_config_loader()
        except Exception:
            logger.warning(
                "[apply_agent_model_override] ModelConfigLoader 不可用，跳过模型覆盖"
            )
            return

    llm_conf = model_loader.get_llm_core_config(model_id)
    if not llm_conf:
        logger.warning(
            "[apply_agent_model_override] 模型 %r 未在 llm.yaml 中找到配置，跳过覆盖",
            model_id,
        )
        return

    # 以现有 llm_call 配置为基础，用模型配置覆盖
    # 这样 provider/api_base/api_key 等字段跟随模型配置
    if hasattr(llm_call, "_config") and isinstance(llm_call._config, dict):
        merged_config = dict(llm_call._config)
        merged_config.update(llm_conf)
    else:
        merged_config = dict(llm_conf)
    merged_config["model_name"] = llm_conf.get("model_name", model_id)

    try:
        # 复用已有的 KeyPoolAdapter（按 key 粒度并发控制）
        existing_adapter = getattr(llm_call, "_adapter", None)
        if existing_adapter is not None:
            new_plugin = type(llm_call)(config=merged_config, adapter=existing_adapter)
        else:
            new_plugin = type(llm_call)(config=merged_config)
        plugin_name = getattr(llm_call, "name", "LLMCorePlugin")
        plugin_registry._core_plugins["llm_call"] = new_plugin
        plugin_registry._plugins[plugin_name] = new_plugin
        logger.info(
            "[apply_agent_model_override] Agent %s 使用模型: %s (provider=%s, context_window=%s)",
            getattr(agent_config, "config_id", "?"),
            merged_config.get("model_name"),
            merged_config.get("provider"),
            merged_config.get("context_window"),
        )
    except Exception as exc:
        logger.warning(
            "[apply_agent_model_override] 重建 llm_call 插件失败: %s",
            exc,
        )


def resolve_tier(tier: str, services: dict[str, Any]) -> str:
    """从 llm.yaml defaults.tiers 解析 tier 为 model_id。

    Args:
        tier: 分级标识（large/medium/small）
        services: 服务字典（包含 model_loader 等）

    Returns:
        对应的模型标识字符串，未找到返回空字符串
    """
    model_loader = services.get("model_loader") if services else None
    if model_loader is None:
        try:
            from config.models import get_model_config_loader

            model_loader = get_model_config_loader()
        except Exception:
            logger.warning("[resolve_tier] ModelConfigLoader 不可用")
            return ""

    llm_data = model_loader._load_llm_data()
    tiers = llm_data.get("defaults", {}).get("tiers", {})
    model_id = tiers.get(tier, "")
    if not model_id:
        logger.warning(
            "[resolve_tier] tier=%r 未在 llm.yaml defaults.tiers 中定义",
            tier,
        )
    return model_id


def matches_disabled(plugin_name: str, disabled_names: list[str]) -> bool:
    """检查插件名称是否匹配禁用列表。

    支持精确匹配和前缀匹配：
    - 'isolation_guard' 精确匹配 'isolation_guard'
    - 'isolation_guard' 前缀匹配 'isolation'（以 _ 分隔）

    Args:
        plugin_name: 插件完整名称
        disabled_names: 禁用名称列表

    Returns:
        是否匹配禁用列表
    """
    for disabled in disabled_names:
        if plugin_name == disabled:
            return True
        if plugin_name.startswith(disabled + "_"):
            return True
    return False
