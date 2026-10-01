# @feature: FP-0.2.CFG 插件配置热感知 | @vision: V2 安全 | @ci: python-test
"""CohostServer 每调用配置路由测试（2026-09-29 注入链失效修复面）。

行为契约（断输入→输出/副作用，不钉实现）：
- 合宿宿主必须接线 on_call_config：内核每次调用现算的属主成员配置在分发前
  路由到属主成员的配置视图（缺失 = 合宿成员配置冻结在握手快照，2026-09-28
  llm_core 事故同构、security_rules 注入链失效直接根因之一）
- 路由按工具名 `{plugin_id}.` 前缀定向属主：最长前缀匹配；其他成员的配置
  视图不得被串写（命名空间隔离，P6 各成员只收自己的 config_files）
- 工具名不匹配任何成员 / 无工具名 / 非 dict config：不串写任何成员（防御）
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

pytestmark = pytest.mark.unit


def _plugin_with_tool(plugin_id: str, tool_name: str) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.tool(name=tool_name, schema={"type": "object"}, description="config route probe")
    async def probe() -> dict:
        return {"ok": True}

    return plugin


def _cohost() -> CohostServer:
    """两成员合宿：握手配置为触发成员的命名空间（此处模拟只带 a_ns）。"""
    members = {
        "alpha": _plugin_with_tool("alpha", "alpha_echo"),
        "beta": _plugin_with_tool("beta", "beta_echo"),
    }
    server = CohostServer(members)
    params = {"capabilities": {}, "config": {"a_ns": {"from": "handshake"}}}
    server._server._on_initialize(params)
    return server


class TestCohostCallConfigRouting:
    def test_call_config_routes_to_owning_member(self) -> None:
        """beta 工具调用携带的配置只进 beta 的配置视图。"""
        server = _cohost()
        import asyncio

        asyncio.run(
            server._server._handle_tools_call(
                {
                    "name": "beta.beta_echo",
                    "arguments": {"config": {"b_ns": {"rules": [1, 2]}}},
                }
            )
        )

        beta = server._members["beta"]
        assert beta.get_config().get("b_ns") == {"rules": [1, 2]}, "属主成员必须在分发前收到本次调用的注入配置"
        alpha = server._members["alpha"]
        assert "b_ns" not in alpha.get_config(), "非属主成员的配置视图不得被串写（命名空间隔离）"

    def test_call_config_fires_owner_config_changed_hook(self) -> None:
        """属主成员的 on_config_changed 钩子在配置真变时触发。"""
        server = _cohost()
        fired: list[dict[str, Any]] = []
        server._members["beta"].on_config_changed(fired.append)
        import asyncio

        asyncio.run(
            server._server._handle_tools_call(
                {
                    "name": "beta.beta_echo",
                    "arguments": {"config": {"b_ns": {"rules": ["x"]}}},
                }
            )
        )

        assert fired == [{"b_ns": {"rules": ["x"]}}], "合宿下属主成员的配置变更钩子必须可用（独占同语义）"

    def test_unknown_tool_name_no_member_contaminated(self) -> None:
        """工具名不匹配任何成员：不串写任何成员配置视图（调用本身按未知工具报错）。"""
        server = _cohost()
        import asyncio

        from mcp.shared.exceptions import MCPError

        with pytest.raises(MCPError):
            asyncio.run(
                server._server._handle_tools_call(
                    {
                        "name": "ghost.speak",
                        "arguments": {"config": {"x_ns": {"v": 1}}},
                    }
                )
            )

        for pid, plugin in server._members.items():
            assert "x_ns" not in plugin.get_config(), f"成员 {pid} 不应收幽灵工具的配置"

    def test_missing_or_non_dict_config_ignored(self) -> None:
        """无 config / 非 dict config（旧内核形态）：静默忽略，不报错不串写。"""
        server = _cohost()
        import asyncio

        asyncio.run(server._server._handle_tools_call({"name": "beta.beta_echo", "arguments": {}}))
        asyncio.run(server._server._handle_tools_call({"name": "beta.beta_echo", "arguments": {"config": "junk"}}))

        for pid, plugin in server._members.items():
            assert "b_ns" not in plugin.get_config(), f"成员 {pid} 视图不得被垃圾输入污染"
