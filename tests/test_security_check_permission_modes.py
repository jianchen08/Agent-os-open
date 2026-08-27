# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""security_check 权限模式分流测试（GAP 权限模式体系）。

覆盖 5 种模式的处置档位（未命中 allow 白名单/记忆指纹时）：
- default      : 弹审批（现状语义）
- accept_edits : 文件类放行；命令类仍弹审批
- auto         : block 规则自动拒绝（不弹审批）；needs_approval/未授权弹审批
- plan         : 写类自动拒绝（不弹审批）；读类放行
- bypass       : 跳过审批放行

会话级模式（_PERMISSION_MODES 表）优先于插件配置默认值。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402
from plugin import SecurityCheckPlugin  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_cap_routing():
    """每个测试结束后摘除显式注入的审批通道，恢复按 plugin 引用自动解析。"""
    yield
    sc_mod.set_human_interaction_cap(None)


def _make_plugin(mode_default: str = "default", rules: list[dict[str, Any]] | None = None) -> Any:
    """构造插件：mock 轨道 1（policy 非命令类）+ 注入轨道 2 数据源 + 审批 mock。"""
    mock_policy = MagicMock()
    mock_policy.execution = "host_direct"
    sc_mod._policy_loader.resolve = MagicMock(return_value=mock_policy)  # type: ignore[method-assign]
    p = SecurityCheckPlugin(config={"enabled": True, "rules": rules or [], "mode": mode_default})
    p._dangerous_ops_by_tool = {
        "file_read": ["read:/etc/"],
        "file_write": ["write:/etc/"],
        "bash_execute": ["rm -rf", "curl"],
    }
    return p


def _mock_approval(selected: str = "approved_once") -> Any:
    """经公开装配缝注入假 human-interaction capability，返回服务边界观察器。

    观察器的 ``requests`` 记录发往交互服务的每次 create_choice 请求载荷，
    是"是否弹了审批"的可观察副作用；空列表 = 全程未发起审批。
    """
    requests: list[dict[str, Any]] = []

    async def _call(name: str, params: dict, **kwargs: Any):
        if name == "create_choice":
            requests.append(dict(params))
            return {"request_id": f"req-{len(requests)}"}
        if name == "wait_for_choice":
            return {"selected_option": selected}
        raise AssertionError(f"unexpected cap.call: {name}")

    fake_cap = AsyncMock()
    fake_cap.call.side_effect = _call
    sc_mod.set_human_interaction_cap(fake_cap)
    return SimpleNamespace(requests=requests)


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _tool_state(tool: str, args: dict[str, Any], pipeline_id: str = "p1") -> dict[str, Any]:
    """构造 tool_execute 状态：权限模式 key 用 pipeline_id（每管道独立）。"""
    return {
        StateKeys.CORE_TYPE: "tool_execute",
        "pipeline_id": pipeline_id,
        StateKeys.SESSION_ID: "s1",
        StateKeys.RAW_TOOL_CALLS: [{"name": tool, "args": args}],
    }


def _set_pipeline_mode(pipeline_id: str, mode: str) -> None:
    sc_mod._PERMISSION_MODES[pipeline_id] = mode


def _clear_session_modes() -> None:
    sc_mod._PERMISSION_MODES.clear()


class TestDefaultMode:
    @pytest.mark.asyncio
    async def test_危险命令弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) >= 1
        assert result.state_updates["security.decision"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_无会话模式时用配置默认(self) -> None:
        _clear_session_modes()
        svc = _mock_approval()
        p = _make_plugin(mode_default="default")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        # 配置默认模式为 default：危险命令走审批链，交互服务恰好收到一次审批请求
        assert len(svc.requests) == 1
        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True
        assert decision["reason"] == "approved"


class TestAcceptEdits:
    @pytest.mark.asyncio
    async def test_文件类放行(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "accept_edits")
        result = await p.execute(_ctx(_tool_state("file_write", {"path": "/etc/hosts", "content": "x"})))
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" not in result.state_updates
        assert len(svc.requests) == 0, "accept_edits 下文件类放行不得发起审批请求"

    @pytest.mark.asyncio
    async def test_命令类仍弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "accept_edits")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) >= 1
        assert result.state_updates["security.decision"]["allowed"] is True


class TestAutoMode:
    @pytest.mark.asyncio
    async def test_block规则自动拒绝不弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(
            rules=[
                {
                    "name": "danger_paths",
                    "tools": ["bash_execute"],
                    "params": ["command"],
                    "action": "block",
                    "patterns": [{"type": "keyword", "value": "danger-x"}],
                }
            ]
        )
        _set_pipeline_mode("p1", "auto")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf danger-x /a"})))
        assert len(svc.requests) == 0, "block 规则自动拒绝不得发起审批请求"
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" in result.state_updates

    @pytest.mark.asyncio
    async def test_needs_approval规则弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(
            rules=[
                {
                    "name": "risky_cmd",
                    "tools": ["bash_execute"],
                    "params": ["command"],
                    "action": "needs_approval",
                    "patterns": [{"type": "keyword", "value": "rm -rf"}],
                }
            ]
        )
        _set_pipeline_mode("p1", "auto")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) >= 1
        assert result.state_updates["security.decision"]["allowed"] is True


class TestPlanMode:
    @pytest.mark.asyncio
    async def test_写类自动拒绝不弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "plan")
        result = await p.execute(_ctx(_tool_state("file_write", {"path": "/etc/hosts", "content": "x"})))
        assert len(svc.requests) == 0
        assert "tool_results" in result.state_updates

    @pytest.mark.asyncio
    async def test_bash命令自动拒绝不弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "plan")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) == 0
        assert "tool_results" in result.state_updates

    @pytest.mark.asyncio
    async def test_读类放行(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "plan")
        result = await p.execute(_ctx(_tool_state("file_read", {"path": "/etc/passwd"})))
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" not in result.state_updates
        assert len(svc.requests) == 0


class TestBypassMode:
    @pytest.mark.asyncio
    async def test_危险命令放行不弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_pipeline_mode("p1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) == 0
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" not in result.state_updates


class TestModePriority:
    @pytest.mark.asyncio
    async def test_会话模式优先于配置默认(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(mode_default="default")
        _set_pipeline_mode("p1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) == 0

    @pytest.mark.asyncio
    async def test_不同会话互不影响(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(mode_default="default")
        _set_pipeline_mode("p1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"}, pipeline_id="p2")))
        assert len(svc.requests) >= 1
