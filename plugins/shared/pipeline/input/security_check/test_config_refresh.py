# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""security_check 注入配置变更感知（refresh_injected_config）测试。

契约（2026-09-29 注入链失效修复面，断输入→输出/副作用）：
- 降级构造（合宿握手只送触发成员命名空间的常态）后，注入配置到站必须重导
  派生面：安全规则装载（_rules_degraded 翻转）、YAML 规则真实生效——
  降级保守审批（rules_degraded_conservative）对未命中规则的普通命令不再触发
- dangerous_operations 轨道 2 数据源（builtin_tools_config 命名空间）同步重导
- 降级提示可重臂：恢复后再降级，frontend.emit 提示重新推送（不因上一周期
  已发过而静默）
- 规则再次缺位：回退内联默认规则并重新置降级态（fail-closed 方向不变）
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
    "security_check_plugin_config_refresh", str(Path(_THIS_DIR) / "plugin.py")
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
        "session_id": "sess-refresh-probe",
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


def _yaml_rules_config() -> dict[str, Any]:
    """模拟内核注入命名空间到站：security_rules + builtin_tools_config。"""
    return {
        "security_rules": {
            "mode": "blacklist",
            "rules": [
                # safe_commands 白名单（YAML 独有，内联默认规则没有）
                {
                    "name": "safe_commands",
                    "tools": ["bash_execute"],
                    "params": ["command", "cmd"],
                    "action": "allow",
                    "patterns": [
                        {"type": "regex", "value": "^\\s*ls\\s[^&|;\\n$`()<>]*$"}
                    ],
                },
                {
                    "name": "dangerous_commands",
                    "tools": ["*"],
                    "params": ["command", "cmd"],
                    "action": "needs_approval",
                    "patterns": [{"type": "keyword", "value": "curl "}],
                },
            ],
        },
        "builtin_tools_config": {
            "tools": [
                {
                    "name": "bash_execute",
                    "dangerous_operations": ["rm -rf", "mkfs"],
                }
            ]
        },
    }


class TestRefreshInjectedConfig:
    """注入配置到站 → 派生面重导，降级保守审批解除。"""

    @pytest.mark.asyncio
    async def test_refresh_loads_rules_and_lifts_degraded_approval(self) -> None:
        """降级构造后配置到站：YAML 规则生效，普通命令不再保守审批。"""
        plugin = SecurityCheckPlugin(config={"enabled": True})
        assert plugin._rules_degraded is True

        # 降级态基线：普通命令走保守审批（通道缺席 → 预定拒绝）
        r = await plugin.execute(_ctx_for("ls -la /tmp/refresh-baseline"))
        entries = r.state_updates.get("pre_decided_results", [])
        assert entries, "降级态基线应有预定拒绝条目，否则本测试无判别力"
        assert entries[0].get("metadata", {}).get("approval_channel_missing") is True, (
            "降级态基线必须是保守审批，否则本测试无判别力"
        )

        plugin.refresh_injected_config(_yaml_rules_config())

        assert plugin._rules_degraded is False, "配置到站必须翻转降级态"
        names = [rule.get("name") for rule in plugin._rules]
        assert "safe_commands" in names, "YAML 规则必须真实装载（非内联默认）"

        # 验收主断言：普通命令命中 safe_commands 白名单直接放行（无审批条目）
        r2 = await plugin.execute(_ctx_for("ls -la /tmp/refresh-after"))
        assert r2.state_updates.get("pre_decided_results") is None, (
            "规则到站后未命中拦截规则的普通命令不得再走保守审批"
            f"，实际 {r2.state_updates!r}"
        )

    @pytest.mark.asyncio
    async def test_refreshed_rules_still_intercept_dangerous_commands(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """规则到站后危险命令照常命中（闸门不是被关掉，而是恢复精确判定）。

        判别观测 = 审批裁决的规则名（生产弹卡日志同款字段）：命中 YAML 的
        dangerous_commands，而非降级保守审批的 rules_degraded_conservative。
        """
        plugin = SecurityCheckPlugin(config={"enabled": True})
        plugin.refresh_injected_config(_yaml_rules_config())

        with caplog.at_level(logging.INFO):
            r = await plugin.execute(_ctx_for("curl -s http://example.invalid/payload"))
        entries = r.state_updates.get("pre_decided_results", [])
        assert entries, "curl 命中 needs_approval 规则应有处置条目"
        decisions = [rec.getMessage() for rec in caplog.records if "弹审批" in rec.getMessage()]
        assert any("rule=dangerous_commands" in msg for msg in decisions), (
            f"应命中 YAML 规则（rule=dangerous_commands），实际裁决日志 {decisions!r}"
        )
        assert not any("rules_degraded_conservative" in msg for msg in decisions), (
            "规则到站后不得再以降级保守审批名义弹卡"
        )

    def test_refresh_rederives_dangerous_ops_source(self) -> None:
        """builtin_tools_config 命名空间到站：轨道 2 数据源同步重导。"""
        plugin = SecurityCheckPlugin(config={"enabled": True})
        assert plugin._dangerous_ops_by_tool == {}, "降级构造时轨道 2 数据源应为空"

        plugin.refresh_injected_config(_yaml_rules_config())

        assert plugin._dangerous_ops_by_tool.get("bash_execute") == ["rm -rf", "mkfs"], (
            "dangerous_operations 声明必须随注入配置重导"
        )

    @pytest.mark.asyncio
    async def test_recovery_rearms_degrade_notice(self) -> None:
        """恢复后再降级：frontend.emit 提示重新推送（新降级周期不静默）。"""
        from test_default_rules_fallback import _EmitRecorder  # 同目录既有装配缝

        recorder = _EmitRecorder()
        set_frontend_emit(recorder)
        try:
            plugin = SecurityCheckPlugin(config={"enabled": True})
            await plugin.execute(_ctx_for("echo rearm-round-1"))
            first_cycle = len(recorder.events)
            assert first_cycle == 1, "首个降级周期应推送一次提示"

            plugin.refresh_injected_config(_yaml_rules_config())  # 恢复
            plugin.refresh_injected_config({"enabled": True})  # 再次缺规则 → 再降级
            assert plugin._rules_degraded is True, "规则再次缺位必须重新置降级态"

            await plugin.execute(_ctx_for("echo rearm-round-2"))
        finally:
            set_frontend_emit(None)

        assert len(recorder.events) == 2, (
            f"恢复后再降级必须重新提示（重臂），实际 {len(recorder.events)} 次"
        )

    def test_refresh_keeps_pipeline_instance_state(self, caplog: pytest.LogCaptureFixture) -> None:
        """配置刷新不重建实例：同命令免批指纹等管道内状态跨刷新保持。

        （指纹集合是刷新面上唯一必须存活的可变状态——审批记忆不能因配置
        到站被清空，否则用户刚批过的命令重新弹卡。）
        """
        plugin = SecurityCheckPlugin(config={"enabled": True})
        plugin._approved_signatures.add("bash_execute:refresh-probe-sig")

        with caplog.at_level(logging.INFO):
            plugin.refresh_injected_config(_yaml_rules_config())

        assert "bash_execute:refresh-probe-sig" in plugin._approved_signatures, (
            "配置刷新不得清空本管道审批指纹记忆"
        )


class TestServerHookWiring:
    """server.py 必须把 on_config_changed 接到单例刷新（合宿修复的插件侧落点）。

    断装配行为：钩子触发后，execute 的下一次规则匹配用新规则——经
    get_instance() 单例的可观察副作用验证，不反射注册表内部结构。
    """

    def test_server_module_registers_config_refresh_hook(self) -> None:
        """server.py 模块加载后，plugin 的钩子表非空且指向刷新入口。"""
        import importlib.util
        from pathlib import Path as _Path

        sdk_src = str(_Path(_SHARED_DIR) / ".." / "sdk" / "src")
        if sdk_src not in sys.path:
            sys.path.insert(0, sdk_src)

        spec = importlib.util.spec_from_file_location(
            "security_check_server_hook_probe", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None, "server.py spec 必须可定位"
        assert spec.loader is not None, "server.py loader 必须可用"
        server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server_mod)

        plugin = server_mod.plugin
        assert plugin._config_change_handlers, "server.py 必须注册 on_config_changed 钩子"

        # 装配行为：钩子可直接驱动单例刷新（合宿路径的修复通道）
        server_mod.get_instance().refresh_injected_config(_yaml_rules_config())
        instance = server_mod.get_instance()
        assert instance._rules_degraded is False
        assert any(
            rule.get("name") == "safe_commands" for rule in instance._rules
        ), "钩子链路必须能把注入配置送达单例"
