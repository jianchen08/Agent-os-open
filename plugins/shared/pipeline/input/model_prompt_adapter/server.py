#!/usr/bin/env python3
"""model_prompt_adapter 管道插件 MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py（ModelPromptAdapterPlugin），本文件只做接口适配：
- ``model_prompt_adapter.execute``：管道 prepare 链调用（规则命中时
  注入消息组）；config 由内核注入（default_model 等经插件配置下发）。
"""

from __future__ import annotations

import logging
import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from plugin import ModelPromptAdapterPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.pipeline_types import (  # noqa: E402
    PluginContext,
    create_initial_state,
)

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("model_prompt_adapter_pipeline")

_instance: ModelPromptAdapterPlugin | None = None


def get_instance() -> ModelPromptAdapterPlugin:
    """懒构建并缓存插件单例（config 变更时由内核重启 sidecar 生效）。"""
    global _instance
    if _instance is None:
        _instance = ModelPromptAdapterPlugin(config=plugin.get_config())
    return _instance


@plugin.on_load
async def _on_load(_params: dict) -> None:
    get_instance()


@plugin.on_unload
async def _on_unload(_params: dict) -> None:
    global _instance
    _instance = None


@plugin.tool(
    name="model_prompt_adapter.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Model Prompt Adapter pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """管道注入入口：规则命中且未注入时按 position 插入消息组。"""
    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
