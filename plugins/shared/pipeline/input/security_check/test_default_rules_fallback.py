# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""security_check 规则缺位回退内联默认规则测试（M3）。

契约：config 未传 rules 且 YAML 数据无 rules 键时，插件必须回退
_DEFAULT_RULES 正常判定，_rules 绝不为 None——否则 _match_rules 迭代
None 直接 TypeError，安全闸门整体失效。
"""

from __future__ import annotations

import importlib.util
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

pytestmark = pytest.mark.unit


def _stub_yaml_payload(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让 config.config_center.get 返回指定载荷（模拟 YAML 加载结果形状）。

    模拟的是外部依赖边界（内核配置中心，P1-7 迁移前尚未落地），非内部实现：
    payload 为 Exception 时按“ConfigCenter 抛意外异常”处理。
    """

    class _FakeCC:
        def get(self, rel: str) -> Any:
            if isinstance(payload, Exception):
                raise payload
            return payload

    stub = types.ModuleType("config.config_center")
    stub.get_config_center = lambda: _FakeCC()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config.config_center", stub)


def _ctx_for(command: str) -> Any:
    """构造危险命令执行的最小上下文（host provider、非隔离）。"""
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "bash_execute", "args": {"command": command}}],
        "execution_contexts": [{"provider": "host", "task_isolated": False}],
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


class TestDefaultRulesFallback:
    """yaml 无 rules 键 → 回退默认规则，闸门功能保持。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # YAML 加载成功但空 map（无 rules 键）
            {"other_section": ["x"]},  # 有内容但同样没有 rules 键
        ],
        ids=["empty-yaml-map", "yaml-without-rules-key"],
    )
    @pytest.mark.asyncio
    async def test_missing_rules_key_falls_back_to_defaults(self, monkeypatch, payload) -> None:
        """危险命令仍触发审批链（软拦截），绝不允许 _rules=None 崩溃。"""
        _stub_yaml_payload(monkeypatch, payload)
        plugin = SecurityCheckPlugin(config={"enabled": True})

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
    async def test_config_center_crash_still_falls_back(self, monkeypatch) -> None:
        """ConfigCenter 抛意外异常也回退默认规则（既有 except 分支回归）。"""
        _stub_yaml_payload(monkeypatch, RuntimeError("config center crashed"))
        plugin = SecurityCheckPlugin(config={"enabled": True})

        r = await plugin.execute(_ctx_for("shutdown now"))
        decision = r.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert "soft_block" in decision.get("reason", "")

    @pytest.mark.asyncio
    async def test_benign_commands_not_blocked_by_defaults(self, monkeypatch) -> None:
        """回退默认规则不等于全员审批：未命中关键词的普通命令照常放行。"""
        _stub_yaml_payload(monkeypatch, {})  # yaml 无 rules 键场景
        plugin = SecurityCheckPlugin(config={"enabled": True})

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


class TestInjectedSecurityRulesNamespace:
    """manifest config_files 注入（id=security_rules）是生产唯一规则真相源。

    内核按 config_files[].id 命名空间合并进 plugin.get_config()（
    invoker/build_injected_config），形状 = security_rules.yaml 顶层对象
    {mode, rules}。本契约保障：注入存在即生效，YAML 全量规则（含 curl/pip
    install 等）不必依赖 0.1 已不存在的 config.config_center。
    """

    def _injected(self, rules: list[dict[str, Any]]) -> dict[str, Any]:
        return {"mode": "blacklist", "rules": rules}

    @pytest.mark.asyncio
    async def test_injected_rules_are_loaded_and_enforced(self) -> None:
        """注入命名空间被加载：curl 关键词命中 needs_approval → 走审批链。"""
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "security_rules": self._injected([
                    {
                        "name": "injected_curl_rule",
                        "tools": ["*"],
                        "params": ["command", "cmd"],
                        "action": "needs_approval",
                        "patterns": [{"type": "keyword", "value": "curl "}],
                    }
                ]),
            }
        )
        names = [r.get("name") for r in plugin._rules]
        assert "injected_curl_rule" in names, "注入的 security_rules 应成为生效规则"

        r = await plugin.execute(_ctx_for("curl -s http://example.com"))
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), (
            f"注入规则命中 curl 应触发审批链（无交互服务→软拦截），实际={decision.get('reason')!r}"
        )

    @pytest.mark.asyncio
    async def test_injected_without_rules_key_falls_back(self, monkeypatch) -> None:
        """注入命名空间存在但无 rules 键 → 走兼容路径（config_center stub → 内联默认）。"""
        _stub_yaml_payload(monkeypatch, {})
        plugin = SecurityCheckPlugin(
            config={"enabled": True, "security_rules": {"mode": "blacklist"}}
        )
        assert plugin._rules, "rules 键缺失时应回退成功，绝不为空"
        r = await plugin.execute(_ctx_for("rm -rf /tmp/fallback-2"))
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), "回退默认规则应仍能命中 rm -rf"
