"""管道配置加载与构建。

从 YAML 文件加载管道配置，
并根据配置动态导入和实例化插件。

支持环境变量替换：
- ``${ENV_VAR}`` 格式自动替换为 ``os.environ.get("ENV_VAR", "")``
- 若环境变量不存在，回退到 ``config/models/llm.yaml`` 中 providers 节对应的 api_key
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pipeline.plugin import ICorePlugin, IInputPlugin, IOutputPlugin, IPlugin
from pipeline.plugin_types import PluginTypeSlot
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable

logger = logging.getLogger(__name__)

# 环境变量占位符模式：${VAR_NAME}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars_in_value(
    value: Any,
    model_loader: Any | None = None,
) -> Any:
    """递归替换配置值中的环境变量占位符。

    将 ``${ENV_VAR}`` 替换为 ``os.environ.get("ENV_VAR", "")``。
    若替换后为空且提供了 ModelConfigLoader，则尝试从提供商配置回退。

    Args:
        value: 待替换的值，可以是字典、列表、字符串或其他类型。
        model_loader: 可选的 ModelConfigLoader 实例，用于 api_key 回退。

    Returns:
        替换后的值。
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")

        result = _ENV_VAR_PATTERN.sub(_replace, value)
        # 若替换后为空且整个字符串就是一个占位符，尝试回退到模型配置
        if not result and _ENV_VAR_PATTERN.fullmatch(value) and model_loader is not None:
            # 从环境变量名推断提供商名称（如 MINIMAX_API_KEY → minimax）
            provider_name = _infer_provider_from_env_var(value)
            if provider_name:
                try:
                    provider_conf = model_loader.get_provider_config(provider_name)
                    if provider_conf and "api_key" in provider_conf:
                        return provider_conf["api_key"]
                except Exception:
                    pass
        return result
    if isinstance(value, dict):
        return {k: _resolve_env_vars_in_value(v, model_loader) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars_in_value(item, model_loader) for item in value]
    return value


def _infer_provider_from_env_var(env_var_str: str) -> str | None:
    """从环境变量占位符推断提供商名称。

    规则：提取 ``${...}`` 中的变量名，按 ``_`` 分割，
    取第一段转小写作为提供商名称。

    示例：
    - ``${MINIMAX_API_KEY}`` → ``minimax``
    - ``${DEEPSEEK_API_KEY}`` → ``deepseek``
    - ``${APP_ZHIPU_API_KEY}`` → ``app`` (不匹配，返回 None)

    Args:
        env_var_str: 环境变量占位符字符串，如 ``${MINIMAX_API_KEY}``。

    Returns:
        推断出的提供商名称，或 ``None``。
    """
    match = _ENV_VAR_PATTERN.fullmatch(env_var_str)
    if not match:
        return None
    var_name = match.group(1)
    # 去掉常见后缀（_API_KEY, _KEY, _API_BASE）再取提供商
    for suffix in ("_API_KEY", "_KEY", "_API_BASE", "_BASE"):
        if var_name.endswith(suffix):
            provider_part = var_name[: -len(suffix)]
            # 只取最后一段（如 APP_ZHIPU → ZHIPU）或整段
            parts = provider_part.split("_")
            # 优先用最后一段非空部分
            for part in reversed(parts):
                if part:
                    return part.lower()
    return None


@dataclass
class PipelineConfig:
    """管道配置数据类。

    Attributes:
        name: 管道名称
        input_route_table: 输入路由表
        output_route_table: 输出路由表
        plugins: 插件配置列表，每项包含 class 和可选 config
        core_plugins: 核心插件配置字典，键为 core_type
    """

    name: str
    input_route_table: InputRouteTable
    output_route_table: OutputRouteTable
    plugins: list[dict[str, Any]] = field(default_factory=list)
    core_plugins: dict[str, Any] = field(default_factory=dict)


def load_pipeline_config(
    path: str | Path,
    model_loader: Any | None = None,
) -> PipelineConfig:
    """从 YAML 文件加载管道配置。

    YAML 结构示例::

        name: main_pipeline
        input_routes:
          - name: default
            condition: ""
            target: core
            plugins: [input_validator]
            priority: 0
        output_routes:
          - route_type: next_llm
            condition: ""
            priority: 0
          - route_type: end
            condition: "task_complete == True"
            priority: 1
        plugins:
          - class: my_package.plugins.InputValidator
            config: {}
        core_plugins:
          llm_call:
            class: my_package.plugins.LLMCorePlugin
            config: {}

    环境变量替换：
    - 配置值中的 ``${ENV_VAR}`` 自动替换为环境变量值
    - 若环境变量不存在且提供了 ``model_loader``，回退到模型配置中的 api_key

    Args:
        path: YAML 配置文件路径
        model_loader: 可选的 ModelConfigLoader 实例，用于环境变量回退

    Returns:
        管道配置实例

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置格式错误
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    if not raw or "name" not in raw:
        raise ValueError("Pipeline config must contain 'name' field")

    # 环境变量替换（含模型配置回退）
    raw = _resolve_env_vars_in_value(raw, model_loader)

    # 构建输入路由表
    input_entries: list[InputRouteEntry] = []
    for entry_data in raw.get("input_routes", []):
        input_entries.append(
            InputRouteEntry(
                name=entry_data["name"],
                condition=entry_data.get("condition", ""),
                target=entry_data.get("target", "core"),
                plugins=entry_data.get("plugins", []),
                priority=entry_data.get("priority", 0),
            )
        )
    input_route_table = InputRouteTable(input_entries)

    # 构建输出路由表
    output_entries: list[OutputRouteEntry] = []
    for entry_data in raw.get("output_routes", []):
        output_entries.append(
            OutputRouteEntry(
                name=entry_data.get("name", ""),
                route_type=entry_data.get("route_type", ""),
                condition=entry_data.get("condition", ""),
                priority=entry_data.get("priority", 0),
                target_core=entry_data.get("target_core"),
                plugins=entry_data.get("plugins", []),
            )
        )
    output_route_table = OutputRouteTable(output_entries)

    return PipelineConfig(
        name=raw["name"],
        input_route_table=input_route_table,
        output_route_table=output_route_table,
        plugins=raw.get("plugins", []),
        core_plugins=raw.get("core_plugins", {}),
    )


_ALLOWED_PREFIXES = ("plugins.", "pipeline.", "agents.", "tools.")


def _import_class(dotted_path: str) -> type:
    """动态导入类。

    Args:
        dotted_path: 类的完整路径，如 my_package.modules.MyClass

    Returns:
        导入的类对象

    Raises:
        ImportError: 导入失败或模块不在白名单中
    """
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
        if not any(module_path.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            raise ImportError(f"Security: module '{module_path}' is not in allowed prefixes")
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if not isinstance(cls, type):
            raise ImportError(f"'{dotted_path}' is not a class")
        return cls
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"Failed to import class '{dotted_path}': {exc}") from exc


_PLUGIN_SEARCH_PACKAGES = [
    ("plugins.input", IInputPlugin),
    ("plugins.output", IOutputPlugin),
]


def _discover_plugin_class(name: str) -> type | None:
    """按插件名自动发现插件类。

    在 plugins.input 和 plugins.output 包下查找同名模块，
    导入后扫描其中唯一的 IPlugin 子类。

    Args:
        name: 插件名称，对应模块文件名（如 "track" 对应 track.py）

    Returns:
        找到的插件类，未找到返回 None
    """
    for package_name, base_cls in _PLUGIN_SEARCH_PACKAGES:
        module_name = f"{package_name}.{name}"
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue

        found: list[type] = []
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, base_cls) and attr is not base_cls:
                found.append(attr)

        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            logger.warning(
                "Module '%s' contains %d plugin classes, skipping auto-discovery",
                module_name,
                len(found),
            )

    return None


def _resolve_plugin_class(plugin_conf: dict[str, Any]) -> type | None:
    """从插件配置中解析插件类，支持 class: 和 name: 两种方式。

    优先使用 class: 方式（向后兼容），其次用 name: 方式自动发现。

    Args:
        plugin_conf: 插件配置字典，包含 class 或 name 字段

    Returns:
        解析到的插件类，失败返回 None
    """
    class_path = plugin_conf.get("class", "")
    if class_path:
        _migrated_paths = {
            "plugins.core.llm_core.LLMCore": "plugins.core.llm_core.plugin.LLMCore",
            "plugins.core.tool_core.ToolCore": "plugins.core.tool_core.plugin.ToolCore",
        }
        class_path = _migrated_paths.get(class_path, class_path)
        return _import_class(class_path)

    plugin_name = plugin_conf.get("name", "")
    if not plugin_name:
        return None

    plugin_cls = _discover_plugin_class(plugin_name)
    if plugin_cls is None:
        raise ImportError(f"Plugin '{plugin_name}' not found in plugins.input or plugins.output")
    return plugin_cls


def build_plugin_registry(  # noqa: PLR0912,PLR0915
    config: PipelineConfig,
    model_loader: Any | None = None,
    router: Any | None = None,
) -> PluginRegistry:
    """根据配置构建插件注册表。

    遍历配置中的 plugins 和 core_plugins，
    动态导入插件类、实例化并注册到 PluginRegistry。

    对于 llm_call 类型的 core plugin，若 core_plugins.config 中未指定
    model_name（为空），则自动从 model_loader 读取 llm.yaml 中 defaults.chat
    配置，实现模型配置的集中管理。

    Args:
        config: 管道配置实例
        model_loader: 可选的 ModelConfigLoader 实例，用于加载默认模型配置
        router: 可选的 litellm.Router 实例

    Returns:
        已注册所有插件的 PluginRegistry 实例
    """
    registry = PluginRegistry()
    type_slot = PluginTypeSlot()

    # 收集所有需要注册的插件配置
    # 1. config.plugins（顶层 plugins 字段，带 config 的完整配置）
    # 2. input_routes / output_routes 里引用的插件名（字符串格式，需转成配置）
    all_plugin_confs = list(config.plugins)
    _seen_plugin_ids = {p.get("class") or p.get("name") for p in all_plugin_confs if isinstance(p, dict)}

    for route in config.input_route_table.entries:
        for plugin_name in route.plugins:
            if plugin_name not in _seen_plugin_ids:
                all_plugin_confs.append({"name": plugin_name})
                _seen_plugin_ids.add(plugin_name)

    for route in config.output_route_table.entries:
        for plugin_name in getattr(route, "plugins", []):
            if plugin_name not in _seen_plugin_ids:
                all_plugin_confs.append({"name": plugin_name})
                _seen_plugin_ids.add(plugin_name)

    # 注册普通插件（Input / Output）
    for plugin_conf in all_plugin_confs:
        plugin_config = plugin_conf.get("config", {})
        plugin_id = plugin_conf.get("class", "") or plugin_conf.get("name", "")

        if not plugin_id:
            logger.warning("Plugin config missing 'class' or 'name' field, skipping")
            continue

        try:
            plugin_cls = _resolve_plugin_class(plugin_conf)
            if plugin_cls is None:
                raise ImportError(f"Plugin '{plugin_id}' could not be resolved: config must specify 'class' or 'name'")
            plugin_instance: IPlugin = plugin_cls(config=plugin_config)
            registry.register(plugin_instance)
            plugin_cls.register_types(type_slot)
            logger.info("Plugin loaded: %s", plugin_id)
        except Exception:
            raise

    # 注册核心插件
    for core_type, core_conf in config.core_plugins.items():
        class_path = core_conf.get("class", "")
        plugin_config = core_conf.get("config", {})

        if not class_path:
            logger.warning("Core plugin config missing 'class' field for '%s', skipping", core_type)
            continue

        # FEATURE-llm-config-loader: 对于 llm_call 类型，始终从 model_loader
        # 加载完整模型配置（含 context_window），pipeline config 可覆盖
        if core_type == "llm_call" and model_loader is not None:
            configured_model = plugin_config.get("model_name", "")
            if configured_model:
                llm_conf = model_loader.get_llm_core_config(configured_model)
                if llm_conf:
                    merged_config = dict(llm_conf)
                    merged_config.update(plugin_config)
                    plugin_config = merged_config
                    logger.info(
                        "[build_plugin_registry] 使用模型: %s (context_window=%s)",
                        configured_model,
                        llm_conf.get("context_window"),
                    )
            else:
                default_model_conf = model_loader.get_default_model("chat")
                if default_model_conf:
                    llm_conf = model_loader.get_llm_core_config(default_model_conf.get("_id", "") or "minimax-m2.7")
                    if llm_conf:
                        merged_config = dict(plugin_config)
                        merged_config.update(llm_conf)
                        plugin_config = merged_config
                        logger.info(
                            "[build_plugin_registry] 使用默认模型: %s (%s)",
                            default_model_conf.get("display_name", ""),
                            default_model_conf.get("model_name", ""),
                        )
                else:
                    logger.warning(
                        "[build_plugin_registry] llm.yaml defaults.chat 未配置，使用 core_plugins 中的原有配置"
                    )

            if core_type == "llm_call" and not plugin_config.get("context_window"):
                logger.error(
                    "[build_plugin_registry] llm_call 缺少 context_window！"
                    "上下文守卫将完全失效，可能导致模型调用溢出。"
                    "请在 llm.yaml 的模型配置中添加 context_window 字段。"
                    "model_name=%s",
                    plugin_config.get("model_name", "unknown"),
                )

        try:
            plugin_cls = _import_class(class_path)
            # llm_call: 优先使用 KeyPoolAdapter（按 key 粒度并发控制）
            if core_type == "llm_call" and model_loader is not None:
                from llm.router_factory import get_or_create_adapter  # noqa: PLC0415

                _adapter = get_or_create_adapter(model_loader)
                core_instance: ICorePlugin = plugin_cls(config=plugin_config, adapter=_adapter)
            elif core_type == "llm_call" and router is not None:
                core_instance = plugin_cls(config=plugin_config, router=router)
            else:
                core_instance = plugin_cls(config=plugin_config)
            registry.register_core(core_type, core_instance)
            plugin_cls.register_types(type_slot)
            logger.info("Core plugin loaded: %s (core_type=%s)", class_path, core_type)
        except Exception as exc:
            raise ImportError(f"Core plugin '{core_type}' (class={class_path}) failed to load: {exc}") from exc

    # 将共享的 type_slot 附加到 registry，供下游使用
    registry.plugin_types = type_slot  # type: ignore[attr-defined]

    return registry
