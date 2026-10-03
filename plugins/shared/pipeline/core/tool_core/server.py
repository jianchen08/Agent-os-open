#!/usr/bin/env python3
"""tool_core core pipeline plugin MCP 服务端——纯接口适配层。

单调用契约的工具执行核（并行 for-each 循环体唯一步骤），业务在 plugin.py；
本文件只做接口适配：通过 MCP SDK 暴露为管道工具。

能力调用通道（统一路径）：on_load 时注入两个 capability delegate——
tool-executor.invoke（内核唯一工具执行漏斗）与 event-bus.emit（事件观测面）。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable

from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk import AgentOSPlugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path（pipeline 包解析）

from plugin import ToolCore  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("tool_core_pipeline")

_instance: ToolCore | None = None


def get_instance() -> ToolCore:
    """懒构建并缓存 ToolCore 单例（线程安全；替代模块级可变全局裸读）。"""
    global _instance
    if _instance is None:
        _instance = ToolCore(config=plugin.get_config())
    return _instance


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize tool_core plugin.

    注入两个 capability delegate（懒解析句柄，调用时才经内核反查能力）：
    tool-executor.invoke 与 event-bus.emit。invoke 第三参 timeout 传大值
    （缺省 600s，config.tool_output.invoke_timeout_s 可覆盖）——SDK 缺省
    30s 面向短调用，长任务工具会先于完成被掐断。
    """

    def _tool_delegate(call_params: dict[str, Any], timeout: float | None = None) -> Awaitable[Any]:
        return plugin.get_capability("tool-executor").call("invoke", call_params, timeout)

    def _event_delegate(call_params: dict[str, Any]) -> Awaitable[Any]:
        return plugin.get_capability("event-bus").call("emit", call_params, None)

    instance = get_instance()
    instance.set_tool_delegate(_tool_delegate)
    instance.set_event_delegate(_event_delegate)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup tool_core plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="tool_core.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Tool Core pipeline plugin (single-call contract)",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the tool_core pipeline plugin.

    Args:
        state: Pipeline state dictionary（含迭代局部键 current_call）.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state_updates.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    # Core 插件返回 state_updates dict，包成 {"state_updates": <dict>} 供内核
    # 反序列化为 PluginResult（与 llm_core server 同款包装契约）。
    return {"state_updates": result}


if __name__ == "__main__":
    plugin.run()
