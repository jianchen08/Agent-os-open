# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""security_check 规则缺位回退内联默认规则测试。

契约：
1. config 未传 rules 且注入命名空间无 rules 键时，插件必须回退
   _DEFAULT_RULES 正常判定，_rules 绝不为 None——否则 _match_rules 迭代
   None 直接 TypeError，安全闸门整体失效。
2. 回退（安全规则降级运行）必须有用户侧显式提示：frontend.emit + warning
   日志双通道，禁止静默降级；注入链健康时不提示。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_rules_fallback", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SecurityCheckPlugin = _mod.SecurityCheckPlugin
set_frontend_emit = _mod.set_frontend_emit

pytestmark = pytest.mark.unit


def _ctx_for(command: str) -> Any:
    """构造危险命令执行的最小上下文（host provider、非隔离）。"""
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "bash_execute", "args": {"command": command}}],
        "execution_contexts": [{"provider": "host", "task_isolated": False}],
        "session_id": "sess-fallback-probe",
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


class _EmitRecorder:
    """记录 frontend.emit 调用的装配缝（外部通道边界，显式注入）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []

    async def __call__(self, event: str, payload: dict[str, Any], thread_id: str) -> None:
        self.events.append((event, payload, thread_id))


class TestDefaultRulesFallback:
    """注入链失效 → 回退默认规则，闸门功能保持。"""

    @pytest.mark.asyncio
    async def test_missing_rules_key_falls_back_to_defaults(self) -> None:
        """无 rules 且注入命名空间缺 rules 键：危险命令仍触发审批链（软拦截）。"""
        plugin = SecurityCheckPlugin(
            config={"enabled": True, "security_rules": {"mode": "blacklist"}}
        )
        names = [r.get("name") for r in plugin._rules]
        assert "dangerous_commands" in names, "应回退内联默认规则，绝不为空"

        r = await plugin.execute(_ctx_for("rm -rf /tmp/fallback-target"))
        updates = r.state_updates
        decision = updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert "soft_block" in decision.get("reason", ""), (
            f"default 规则应命中 rm -rf 并走审批链（cap 缺席→软拦截），实际 reason={decision.get('reason')!r}"
        )
        # 软拦截副作用：本轮 raw_tool_calls 必须被清空（拒绝反馈给 LLM）
        assert updates.get("raw_tool_calls") == []

    @pytest.mark.asyncio
    async def test_benign_commands_not_blocked_by_defaults(self) -> None:
        """回退默认规则不等于全员审批：未命中关键词的普通命令照常放行。"""
        plugin = SecurityCheckPlugin(
            config={"enabled": True, "security_rules": {"mode": "blacklist"}}
        )

        for benign in ("ls -la /tmp/demo-dir", "echo ok-from-benign-probe"):
            r = await plugin.execute(_ctx_for(benign))
            updates = r.state_updates
            decision = updates.get("security.decision", {})
            assert decision.get("allowed") is True
            assert "soft_block" not in decision.get("reason", ""), (
                f"普通命令不应被默认规则拦截: {benign!r} → {decision.get('reason')!r}"
            )
            # 放行路径不产生任何拒绝副作用（不写 tool_results、不清空工具调用）
            assert not any("tool" in k and k != "security.decision" for k in updates), (
                f"普通命令放行不应产生工具结果/状态改写: {benign!r} → keys={sorted(updates)}"
            )


class TestRulesDegradedNotice:
    """安全规则降级运行必须显式提示（frontend.emit + 日志双通道，禁静默降级）。"""

    @pytest.mark.asyncio
    async def test_fallback_pushes_frontend_degraded_notice(self) -> None:
        """注入链失效回退时，frontend.emit 必须收到含「安全规则降级运行」的提示。"""
        recorder = _EmitRecorder()
        set_frontend_emit(recorder)
        try:
            plugin = SecurityCheckPlugin(
                config={"enabled": True, "security_rules": {"mode": "blacklist"}}
            )
            assert plugin._rules_degraded is True
            await plugin.execute(_ctx_for("ls /tmp/degraded-probe"))
        finally:
            set_frontend_emit(None)

        assert recorder.events, "降级发生时必须推送前端提示，禁止静默降级"
        event, payload, thread_id = recorder.events[0]
        assert "安全规则降级运行" in payload.get("message", ""), (
            f"提示文案须注明安全规则降级运行: {payload!r}"
        )
        assert thread_id == "sess-fallback-probe", f"提示应按会话路由: {thread_id!r}"

    @pytest.mark.asyncio
    async def test_fallback_logs_degraded_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """降级回退必须落 warning 日志（日志通道），文案注明安全规则降级运行。"""
        with caplog.at_level(logging.WARNING, logger="security_check_plugin_rules_fallback"):
            SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        warnings = [r for r in caplog.records if "安全规则降级运行" in r.getMessage()]
        assert warnings, "降级必须落 warning 日志"
        assert all(r.levelno >= logging.WARNING for r in warnings)

    @pytest.mark.asyncio
    async def test_degraded_notice_sent_once_per_instance(self) -> None:
        """同一降级周期只推一次（多轮工具调用不刷屏）。"""
        recorder = _EmitRecorder()
        set_frontend_emit(recorder)
        try:
            plugin = SecurityCheckPlugin(
                config={"enabled": True, "security_rules": {"mode": "blacklist"}}
            )
            for i in range(3):
                await plugin.execute(_ctx_for(f"echo degraded-round-{i}"))
        finally:
            set_frontend_emit(None)

        assert len(recorder.events) == 1, (
            f"同一实例降级提示只推一次，实际 {len(recorder.events)} 次"
        )

    @pytest.mark.asyncio
    async def test_injected_rules_no_degraded_notice(self) -> None:
        """注入链健康（YAML 规则生效）时不提示——提示只对应降级状态。"""
        recorder = _EmitRecorder()
        set_frontend_emit(recorder)
        try:
            plugin = SecurityCheckPlugin(
                config={
                    "enabled": True,
                    "security_rules": {
                        "mode": "blacklist",
                        "rules": [
                            {
                                "name": "healthy_injected_rule",
                                "tools": ["*"],
                                "params": ["command"],
                                "action": "needs_approval",
                                "patterns": [{"type": "keyword", "value": "curl "}],
                            }
                        ],
                    },
                }
            )
            assert plugin._rules_degraded is False
            await plugin.execute(_ctx_for("curl -s http://example.com/health-probe"))
        finally:
            set_frontend_emit(None)

        assert recorder.events == [], "注入链健康不得误报降级提示"


class TestInjectedSecurityRulesNamespace:
    """manifest config_files 注入（id=security_rules）是生产唯一规则真相源。

    内核按 config_files[].id 命名空间合并进 plugin.get_config()（
    invoker/build_injected_config），形状 = security_rules.yaml 顶层对象
    {mode, rules}。本契约保障：注入存在即生效，YAML 全量规则（含 curl/pip
    install 等）经注入路径进入安全闸门。
    """

    @pytest.mark.asyncio
    async def test_injected_rules_are_loaded_and_enforced(self) -> None:
        """注入命名空间被加载：curl 关键词命中 needs_approval → 走审批链。"""
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "security_rules": {
                    "mode": "blacklist",
                    "rules": [
                        {
                            "name": "injected_curl_rule",
                            "tools": ["*"],
                            "params": ["command", "cmd"],
                            "action": "needs_approval",
                            "patterns": [{"type": "keyword", "value": "curl "}],
                        }
                    ],
                },
            }
        )
        names = [r.get("name") for r in plugin._rules]
        assert "injected_curl_rule" in names, "注入的 security_rules 应成为生效规则"

        r = await plugin.execute(_ctx_for("curl -s http://example.com"))
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), (
            f"注入规则命中 curl 应触发审批链（无交互服务→软拦截），实际={decision.get('reason')!r}"
        )
