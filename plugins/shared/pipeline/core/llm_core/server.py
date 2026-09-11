#!/usr/bin/env python3
"""llm_core core pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/core/llm_core/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。

LLM 调用通道（统一路径）：on_load 时向 plugin.py 注入 capability caller
（``set_capability_caller``），execute 经内核 tool-executor 能力跨进程调用
llm_service 的 llm.complete_stream；逐字流式由 llm_service 内部 event-bus
推送（本插件不再维护 chunk 桥接/消费者队列）。
"""
from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# system/llm：复用 _config_models 配置注入桥（与 system/llm/server.py 共享）。
_paths = bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path
_system_llm_dir = os.path.join(_paths.shared_root, "system", "llm")
sys.path.insert(0, _system_llm_dir)
# 插入顺序即优先序：llm_core 目录必须压过 system/llm——两处均有 adapter.py，
# 裸名 `import adapter` 必须命中本目录实现（类型/协议归属地），否则会静默
# 加载 llm_service 的 adapter（其 import litellm，会把 litellm 依赖拖回本
# 已拆除此依赖的进程）。
sys.path.insert(0, _paths.plugin_dir)

from plugin import LLMCore  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("llm_core_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> LLMCore:
    """懒构建并缓存 LLMCore 单例（线程安全；替代模块级可变 `_instance` 全局）。

    构造前注入 _config_models 配置 shim（供 LLMCore._apply_model_from_state 的
    get_llm_core_config 解析 llm.yaml；与 system/llm/server.py 对齐）。
    """
    from _config_models import set_config  # noqa: PLC0415

    config = plugin.get_config()
    set_config(config)
    return LLMCore(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize llm_core plugin.

    注入 capability caller：LLM 调用（llm.complete_stream）经内核
    tool-executor 能力跨进程转发到 llm_service（统一路径唯一调用通道；
    llm_core 不再直连 LLM API）。随后预热构建 LLMCore 单例。
    """
    from plugin import set_capability_caller  # noqa: PLC0415

    set_capability_caller(
        lambda method, params_, timeout=None: plugin.get_capability(
            "tool-executor"
        ).call(method, params_, timeout)
    )
    get_instance()  # 预热：注入配置 shim + 构建 LLMCore（保持原 on_load 构造时机）


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup llm_core plugin."""
    from plugin import set_capability_caller  # noqa: PLC0415

    set_capability_caller(None)
    get_instance.cache_clear()


@plugin.tool(
    name="llm_core.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute LLM Core pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the llm_core pipeline plugin.

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

    # Core 插件（LLMCore）返回 state_updates dict（含 raw_result 等），
    # 需包成 {"state_updates": <dict>} 供内核反序列化为 PluginResult。
    if isinstance(result, dict):
        return {"state_updates": result}

    data: dict = {"state_updates": result.state_updates}
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
