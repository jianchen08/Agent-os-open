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
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
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
    """构造危险命令执行的最小上下文（host provider、非隔离）。

    工具名用现行命令面单工具 ``bash``（ADR 2026-11-07 T2 四单词重构：
    bash_execute 退役，isolation_policy 精确匹配键已换名——轨道 1 判定依赖
    policy.resolve(name).execution == command_in_container，退役名会落默认
    host_direct 而绕过危险分类）。"""
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "bash", "args": {"command": command}}],
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
        entries = updates.get("pre_decided_results", [])
        assert entries, "应有预定拒绝条目"
        assert "交互服务不可用" in entries[0]["error"], (
            f"default 规则应命中 rm -rf 并走审批链（cap 缺席→预定拒绝），实际 {entries!r}"
        )
        # 调用级：不清空调用（tool_core 命中预定结果跳过执行并整形反馈）
        assert "raw_tool_calls" not in updates

    @pytest.mark.asyncio
    async def test_benign_commands_get_conservative_approval_when_degraded(self) -> None:
        """D7 裁定（2026-09-24）：降级态保守审批——未命中关键词的普通命令
        不再静默放行，走 needs_approval（人审把关；测试环境交互通道缺席 →
        软拦截 + approval_channel_missing 标记）。

        取代的旧契约（"普通命令照常放行"）被 D7 推翻：规则库失明时无法
        确认参数安全，放行 = 闸门失明继续开车；人审是各档位自身语义。
        """
        plugin = SecurityCheckPlugin(
            config={"enabled": True, "security_rules": {"mode": "blacklist"}}
        )
        assert plugin._rules_degraded is True

        for benign in ("ls -la /tmp/demo-dir", "echo ok-from-benign-probe"):
            r = await plugin.execute(_ctx_for(benign))
            updates = r.state_updates
            # D7 保守审批路径：人审尝试发生（测试环境交互通道缺席 →
            # 预定拒绝 + approval_channel_missing 标记，见 _await_approval）
            entries = updates.get("pre_decided_results", [])
            assert entries, f"降级态普通命令应有预定拒绝（D7），实际 {entries!r}"
            assert entries[0].get("metadata", {}).get("approval_channel_missing") is True, (
                f"降级态普通命令应走保守审批（D7），实际 {entries!r}"
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
        entries = r.state_updates.get("pre_decided_results", [])
        assert entries, "注入规则命中 curl 应触发审批链"
        assert "交互服务不可用" in entries[0]["error"], (
            f"注入规则命中 curl 应触发审批链（无交互服务→预定拒绝），实际={entries!r}"
        )


class TestInlineDefaultsSubsetOfYaml:
    """内联兜底表 ⊆ YAML 规则表对账闸（llm_presets 对账闸同款，防漂移）。

    内联表是降级子集而非同构（_DEFAULT_RULES 注释契约）：仅承载
    dangerous_commands 命令关键词。YAML 侧删掉任一内联已有规则而不同步内联
    （如 dangerous_commands 整条退役/改名）即降级态语义漂移——本闸红。
    """

    def test_inline_rule_names_subset_of_yaml(self) -> None:
        """内联规则名键集 ⊆ YAML 规则名键集；两侧键集均非空（防闸空洞）。"""
        inline_names = {r["name"] for r in SecurityCheckPlugin._DEFAULT_RULES}
        yaml_names = _yaml_rule_names()
        assert inline_names, "内联兜底表为空——降级态失去保守审批底座"
        missing = inline_names - yaml_names
        assert not missing, (
            f"内联兜底规则 {sorted(missing)} 已不在 YAML 规则表中——"
            "降级态语义漂移（内联 ⊆ YAML 契约破坏），同步内联表或 YAML"
        )

    def test_yaml_rules_shape_matches_rule_engine(self) -> None:
        """性质闸：YAML 规则带规则引擎必需键（name/action/patterns）——
        对账真值源自身畸形时闸必须响而非静默放过。"""
        for rule in _yaml_rules():
            assert rule.get("name"), f"YAML 规则缺 name: {rule!r}"
            assert rule.get("action") in ("allow", "block", "needs_approval"), (
                f"YAML 规则 action 非法: {rule!r}"
            )
            assert isinstance(rule.get("patterns"), list) and rule["patterns"], (
                f"YAML 规则缺 patterns: {rule!r}"
            )


_YAML_RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "plugins"
    / "security_check"
    / "security_rules.yaml"
)


def _yaml_rules() -> list[dict[str, Any]]:
    """读取出厂 YAML 规则表（对账真值源；缺失/畸形直接红，不降级放行）。"""
    import yaml  # noqa: PLC0415

    assert _YAML_RULES_PATH.is_file(), f"security_rules.yaml 不存在: {_YAML_RULES_PATH}"
    data = yaml.safe_load(_YAML_RULES_PATH.read_text(encoding="utf-8")) or {}
    rules = data.get("rules") or []
    assert isinstance(rules, list) and rules, "YAML 规则表为空——对账闸失去真值源"
    return [r for r in rules if isinstance(r, dict)]


def _yaml_rule_names() -> set[str]:
    return {str(r["name"]) for r in _yaml_rules() if r.get("name")}
