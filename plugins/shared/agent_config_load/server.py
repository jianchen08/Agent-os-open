#!/usr/bin/env python3
"""agent_config_load input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/agent_config_load/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import AgentConfigLoadPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("agent_config_load_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> AgentConfigLoadPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    return AgentConfigLoadPlugin(config=plugin.get_config())


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize agent_config_load plugin.

    注入 tool-surface capability caller：模式声明工具并入的启用判定走活注册表
    探针（接线先例 tool_schema/server.py on_load）；注入 card 质检服务 caller：
    声明面 lint + actions 归一经 mode_roleplay provides namespace ``card``
    直连（T6c，approval→human-interaction 消费先例）。caller 挂在
    get_instance() 缓存的单例上——合宿下同进程多个 pipeline 插件共享裸名
    `plugin` 模块，实例属性身份唯一。随后预热构建插件单例。
    """
    get_instance().set_capability_caller(
        lambda method, params_, timeout=None: plugin.get_capability(
            "tool-surface"
        ).call(method, params_, timeout)
    )
    get_instance().set_card_service_caller(
        lambda method, params_, timeout=None: plugin.get_capability("card").call(
            method, params_, timeout
        )
    )
    # project.* 服务 caller（T6a）：project_roots 解析经 project_manager
    # provides namespace ``project`` 直连（登记簿读面收口项目域主）。
    get_instance().set_project_service_caller(
        lambda method, params_, timeout=None: plugin.get_capability("project").call(
            method, params_, timeout
        )
    )
    # llm 服务 caller（落定键 2026-10-18）：模型落定步经内核 tool-executor
    # 调 llm_service 的 model.resolve（与 llm_core 同一跨进程通道形态）。
    get_instance().set_llm_service_caller(
        lambda method, params_, timeout=None: plugin.get_capability(
            "tool-executor"
        ).call(method, params_, timeout)
    )
    get_instance()  # 启动时预热，保持原 on_load 构造时机


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup agent_config_load plugin."""
    get_instance().set_capability_caller(None)
    get_instance().set_card_service_caller(None)
    get_instance().set_project_service_caller(None)
    get_instance().set_llm_service_caller(None)
    get_instance.cache_clear()


@plugin.tool(
    name="agent_config_load.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Agent Config Load pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the agent_config_load pipeline plugin.

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
