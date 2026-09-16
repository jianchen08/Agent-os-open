# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""security_check 权限模式分流测试（GAP 权限模式体系）。

覆盖 4 种模式的处置档位（未命中 allow 白名单/记忆指纹时）：
- default      : 弹审批（现状语义）
- accept_edits : 文件类放行；命令类仍弹审批
- auto         : block 规则自动拒绝（不弹审批）；needs_approval/未授权弹审批
- bypass       : 跳过审批放行

会话级模式（_PERMISSION_MODES 表）优先于插件配置默认值。
"""

from __future__ import annotations

import json
import logging
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


def _tool_state(
    tool: str,
    args: dict[str, Any],
    session_id: str = "s1",
    pipeline_id: str = "p1",
) -> dict[str, Any]:
    """构造 tool_execute 状态：权限模式 key 用 session_id（会话稳定键，BUG-15）。

    pipeline_id 默认与 session 键不同值——同会话内管道键漂移不影响显式档命中。
    """
    return {
        StateKeys.CORE_TYPE: "tool_execute",
        "pipeline_id": pipeline_id,
        StateKeys.SESSION_ID: session_id,
        StateKeys.RAW_TOOL_CALLS: [{"name": tool, "args": args}],
    }


def _set_session_mode(session_id: str, mode: str) -> None:
    sc_mod._PERMISSION_MODES[session_id] = mode


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
    async def test_文件类放行(self, tmp_path: Any) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_session_mode("s1", "accept_edits")
        # 普通文件路径（临时目录内）：/etc/hosts 在 Linux 上命中敏感系统目录
        # 硬拦截（设计内安全底线，优先于 accept_edits 放行），不代表"文件类"
        result = await p.execute(_ctx(_tool_state("file_write", {"path": str(tmp_path / "x.txt"), "content": "x"})))
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" not in result.state_updates
        assert len(svc.requests) == 0, "accept_edits 下文件类放行不得发起审批请求"

    @pytest.mark.asyncio
    async def test_命令类仍弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_session_mode("s1", "accept_edits")
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
        _set_session_mode("s1", "auto")
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
        _set_session_mode("s1", "auto")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) >= 1
        assert result.state_updates["security.decision"]["allowed"] is True


class TestBypassMode:
    @pytest.mark.asyncio
    async def test_危险命令放行不弹审批(self) -> None:
        svc = _mock_approval()
        p = _make_plugin()
        _set_session_mode("s1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) == 0
        assert result.state_updates["security.decision"]["allowed"] is True
        assert "tool_results" not in result.state_updates


class TestModePriority:
    @pytest.mark.asyncio
    async def test_会话模式优先于配置默认(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(mode_default="default")
        _set_session_mode("s1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        assert len(svc.requests) == 0

    @pytest.mark.asyncio
    async def test_不同会话互不影响(self) -> None:
        svc = _mock_approval()
        p = _make_plugin(mode_default="default")
        _set_session_mode("s1", "bypass")
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"}, session_id="s2")))
        assert len(svc.requests) >= 1


# ═══════════════════════════════════════════════════════════════
# 2026-09-13 覆盖率补测：execute 早退分支 / 模式表持久化 / 分流短路放行
# ═══════════════════════════════════════════════════════════════


class TestEarlyExitBranches:
    """execute 的三类免检早退与 priority 配置读取。"""

    @pytest.mark.asyncio
    async def test_disabled_plugin_allows_everything(self) -> None:
        """enabled=False → 直接放行，reason 标注 disabled。"""
        p = SecurityCheckPlugin(config={"enabled": False})
        result = await p.execute(_ctx(_tool_state("bash_execute", {"command": "rm -rf /x"})))
        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True
        assert decision["reason"] == "security check disabled"

    @pytest.mark.asyncio
    async def test_non_tool_execute_skips_check(self) -> None:
        """非 tool_execute 核（如 llm_call）→ 免检放行。"""
        p = _make_plugin()
        state = _tool_state("bash_execute", {"command": "rm -rf /x"})
        state[StateKeys.CORE_TYPE] = "llm_call"
        result = await p.execute(_ctx(state))
        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True
        assert decision["reason"] == "not a tool execution"

    @pytest.mark.asyncio
    async def test_empty_tool_calls_skips_check(self) -> None:
        """tool_execute 但无工具调用 → 免检放行。"""
        p = _make_plugin()
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [],
        }
        result = await p.execute(_ctx(state))
        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True
        assert decision["reason"] == "no tool calls to check"

    def test_priority_from_config(self) -> None:
        """priority 缺省 70，可经 config 覆盖。"""
        assert SecurityCheckPlugin(config={}).priority == 70
        assert SecurityCheckPlugin(config={"priority": 99}).priority == 99


class TestPermissionModeTablePersistence:
    """权限模式表持久化：加载过滤未知模式 / 损坏文件容错 / 保存失败留痕。"""

    def test_load_filters_unknown_modes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """持久化文件中未知模式值被过滤，合法模式生效。"""
        saved = dict(sc_mod._PERMISSION_MODES)
        try:
            f = tmp_path / "permission_modes.json"
            f.write_text(json.dumps({"pa": "bypass", "pb": "zombie_mode"}), encoding="utf-8")
            monkeypatch.setattr(sc_mod, "_PERMISSION_MODES_FILE", str(f))
            sc_mod._load_permission_modes()
            assert sc_mod._PERMISSION_MODES == {"pa": "bypass"}
        finally:
            sc_mod._PERMISSION_MODES.clear()
            sc_mod._PERMISSION_MODES.update(saved)

    def test_load_corrupt_file_keeps_table_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """损坏的持久化文件 → 不清空现有表、留 warning、不阻断。"""
        saved = dict(sc_mod._PERMISSION_MODES)
        try:
            f = tmp_path / "broken.json"
            f.write_text("{not-json", encoding="utf-8")
            monkeypatch.setattr(sc_mod, "_PERMISSION_MODES_FILE", str(f))
            with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
                sc_mod._load_permission_modes()
            assert sc_mod._PERMISSION_MODES == saved
            assert any("权限模式表加载失败" in r.getMessage() for r in caplog.records)
        finally:
            sc_mod._PERMISSION_MODES.clear()
            sc_mod._PERMISSION_MODES.update(saved)

    def test_save_failure_warns_and_keeps_memory_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """持久化目录不可写 → warning 留痕，内存态模式不受影响。"""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(sc_mod, "_PERMISSION_MODES_FILE", str(blocker / "permission_modes.json"))
        sc_mod._PERMISSION_MODES["persist-probe"] = "bypass"
        try:
            with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
                sc_mod._save_permission_modes()
            assert sc_mod._PERMISSION_MODES.get("persist-probe") == "bypass"
            assert any("持久化失败" in r.getMessage() for r in caplog.records)
        finally:
            sc_mod._PERMISSION_MODES.pop("persist-probe", None)


class TestDispatchShortCircuits:
    """危险工具分流的短路放行：只读白名单 / allow 规则 / accept_edits 文件类。"""

    @pytest.mark.asyncio
    async def test_read_only_tool_skips_dangerous_judgment(self) -> None:
        """file_read 属只读白名单：即使参数命中危险声明也不弹审批。"""
        svc = _mock_approval()
        p = _make_plugin()
        p._dangerous_ops_by_tool["file_read"] = ["read:/data/secret/"]
        result = await p.execute(_ctx(_tool_state("file_read", {"path": "/data/secret/x"})))
        assert len(svc.requests) == 0, "只读白名单工具不得发起审批"
        assert result.state_updates["security.decision"] == {
            "allowed": True,
            "reason": "all checks passed",
        }

    @pytest.mark.asyncio
    async def test_allow_rule_whitelists_dangerous_tool(self) -> None:
        """危险工具参数命中 allow 白名单 → 放行且零审批。"""
        svc = _mock_approval()
        rules: list[dict[str, Any]] = [{
            "name": "safe_curl_help",
            "tools": ["bash_execute"],
            "params": ["command"],
            "action": "allow",
            "patterns": [{"type": "keyword", "value": "curl --help"}],
        }]
        p = _make_plugin(rules=rules)
        result = await p.execute(
            _ctx(_tool_state("bash_execute", {"command": "curl --help | head"}))
        )
        assert len(svc.requests) == 0, "allow 白名单命中不得发起审批"
        assert result.state_updates["security.decision"]["reason"] == "all checks passed"

    @pytest.mark.asyncio
    async def test_accept_edits_passes_dangerous_file_tools(self) -> None:
        """accept_edits 下文件类工具即使参数危险也放行（档位语义）。"""
        svc = _mock_approval()
        p = _make_plugin()
        _set_session_mode("ae-file-sess", "accept_edits")
        result = await p.execute(
            _ctx(_tool_state(
                "file_write",
                {"path": "/etc/hosts", "content": "x"},
                session_id="ae-file-sess",
            ))
        )
        assert len(svc.requests) == 0, "accept_edits 下文件类放行不得发起审批"
        assert result.state_updates["security.decision"]["reason"] == "all checks passed"
