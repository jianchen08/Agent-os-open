#!/usr/bin/env python3
"""session_isolation input pipeline plugin MCP 服务端——纯接口适配层。

plugin.py 承载会话级隔离守卫逻辑（主会话 bash_execute 路由到容器），
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

# plugin.py 内 lazy import infrastructure.session.session_workspace（原位于 0.1 src/ 下）。
# 0.1 src/ 已归档为 reference/0.1_src/（参考文件，不参与运行时）。
# 该 lazy import 在 reference 不在 sys.path 时走 fallback（plugin.py 内已 try/except），
# 故此处不再注入 src 路径。若未来需要 reference 中的参考实现，可显式将
# reference/0.1_src 加入 sys.path 做调试。
_project_root = os.path.abspath(os.path.join(_this_dir, "..", "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import SessionIsolationPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("session_isolation_pipeline")

_instance: SessionIsolationPlugin | None = None


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize session_isolation plugin."""
    global _instance
    config = plugin.get_config()
    _instance = SessionIsolationPlugin(config=config)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup session_isolation plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="session_isolation.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Session Isolation pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the session_isolation pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await _instance.execute(ctx)

    # Core 插件返回 dict，Input/Output 返回 PluginResult/OutputResult
    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    route_sig = getattr(result, "route_signal", None)
    if route_sig:
        data["route_signal"] = {
            "route_type": route_sig.route_type,
            "target": route_sig.target,
            "reason": route_sig.reason,
        }
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
