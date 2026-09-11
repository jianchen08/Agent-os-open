#!/usr/bin/env python3
"""tool_schema_validator input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/tool_schema_validator/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import ToolSchemaValidator, set_capability_caller  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

from wiring import make_capability_caller  # noqa: E402  （共享裸名模块，共享根经 bootstrap 入 path）

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("tool_schema_validator_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> ToolSchemaValidator:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return ToolSchemaValidator(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize tool_schema_validator plugin + 注入能力调用器。"""
    get_instance()  # 启动时预热，保持原 on_load 构造时机
    caller = make_capability_caller(plugin)
    if caller:
        set_capability_caller(caller)
    else:
        logger.warning(
            "[tool_schema_validator] 能力调用器未注入，截断检测降级为不可修复口径"
        )


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup tool_schema_validator plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="tool_schema_validator.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Tool Schema Validator pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the tool_schema_validator pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    # Core 插件返回 dict，Input/Output 返回 PluginResult/OutputResult
    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
