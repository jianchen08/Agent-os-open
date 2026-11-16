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


class TestCohostHandshakeMemberConfigsRouting:
    """合宿握手 member_configs 按成员路由（2026-10-09 isolation_guard 注入断裂修复面）。

    行为契约（断输入→输出/副作用）：内核 initialize 的 ``config`` 只承载触发
    成员的 config_files 命名空间；params 附 ``member_configs``（{plugin_id: 该
    成员注入命名空间}）时，扇出必须把每成员自己的命名空间送进各自的配置视图
    （非触发成员构造期配置落空 = 部署形态 isolation_guard fail-closed 拒全部
    容器要求型工具的精确根因）；字段缺席（旧内核）回落顶层 config（双向兼容）。
    """

    def test_member_configs_routes_own_namespace_per_member(self) -> None:
        """member_configs 在场：每成员配置视图 = 自己的命名空间，互不可见。"""

        members = {
            "alpha": _plugin_with_tool("alpha", "alpha_echo"),
            "beta": _plugin_with_tool("beta", "beta_echo"),
        }
        server = CohostServer(members)
        server._server._on_initialize(
            {
                "capabilities": {},
                "config": {"a_ns": {"from": "handshake"}},
                "member_configs": {
                    "alpha": {"a_ns": {"v": 1}},
                    "beta": {"b_ns": {"v": 2}},
                },
            }
        )

        assert server._members["alpha"].get_config() == {"a_ns": {"v": 1}}, (
            "alpha 必须只收到自己的命名空间（构造期注入，非并集）"
        )
        assert server._members["beta"].get_config() == {"b_ns": {"v": 2}}, (
            "非触发成员 beta 必须在握手期拿到自己的命名空间（部署形态回归面）"
        )

    def test_member_configs_absent_falls_back_to_config(self) -> None:
        """旧内核（无 member_configs 字段）：两成员都收顶层 config（行为零变化）。"""
        server = _cohost()

        for pid, plugin in server._members.items():
            assert plugin.get_config() == {"a_ns": {"from": "handshake"}}, (
                f"无 member_configs 时成员 {pid} 回落顶层 config（旧内核兼容）"
            )

    def test_member_configs_missing_entry_falls_back_to_config(self) -> None:
        """member_configs 无该成员条目（manifest 并发卸载窗口）：回落顶层 config。"""
        members = {
            "alpha": _plugin_with_tool("alpha", "alpha_echo"),
            "beta": _plugin_with_tool("beta", "beta_echo"),
        }
        server = CohostServer(members)
        server._server._on_initialize(
            {
                "capabilities": {},
                "config": {"a_ns": {"from": "handshake"}},
                "member_configs": {"alpha": {"a_ns": {"v": 1}}},
            }
        )

        assert server._members["alpha"].get_config() == {"a_ns": {"v": 1}}
        assert server._members["beta"].get_config() == {"a_ns": {"from": "handshake"}}, (
            "无条目成员必须回落顶层 config（旧行为），不得收空配置"
        )

    def test_swap_member_replay_routes_member_configs(self) -> None:
        """成员热换入的握手重放：换入成员拿到自己的命名空间（非触发成员的）。"""

        members = {
            "alpha": _plugin_with_tool("alpha", "alpha_echo"),
            "beta": _plugin_with_tool("beta", "beta_echo"),
        }
        server = CohostServer(members)
        server._server._on_initialize(
            {
                "capabilities": {},
                "config": {"a_ns": {"from": "handshake"}},
                "member_configs": {
                    "alpha": {"a_ns": {"v": 1}},
                    "beta": {"b_ns": {"v": 2}},
                },
            }
        )

        replacement = _plugin_with_tool("beta", "beta_echo")
        server.swap_member("beta", replacement)
        assert replacement.get_config() == {"b_ns": {"v": 2}}, (
            "热换入成员的重放注入必须按 member_configs 路由到该成员自己的命名空间"
        )
