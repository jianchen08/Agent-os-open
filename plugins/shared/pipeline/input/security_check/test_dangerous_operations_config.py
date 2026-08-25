# @ci: python-coverage
"""security_check 危险工具轨道 2 配置接线测试（punch W-A4）。

plugin.json 声明 config_files 引用 config/tools/builtin_tools_config.yaml
（id= builtin_tools_config）；内核按 id 命名空间合并进 plugin.get_config()，
SecurityCheckPlugin(config=...) 据此构建 tool_name → dangerous_operations 表，
_is_dangerous_tool 轨道 2 优先读注入配置（其次 tool_registry 服务）。

同时验证：tools.global_registry 死分支已删除——registry 不可用时返回 []
而非试图导入不存在的模块。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_danger_ops", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SecurityCheckPlugin = _mod.SecurityCheckPlugin

pytestmark = pytest.mark.unit


def _ctx_without_services() -> SimpleNamespace:
    """get_service 恒抛 KeyError 的最小 PluginContext 替身。"""

    def _get_service(_name: str) -> Any:
        raise KeyError(_name)

    return SimpleNamespace(state={}, get_service=_get_service)


def _config_with_dangerous_ops() -> dict[str, Any]:
    """模拟内核注入的 config：builtin_tools_config 命名空间（tools 列表形状）。"""
    return {
        "builtin_tools_config": {
            "tool_cache": {"enabled": True},
            "tools": [
                {"name": "task_submit", "dangerous_operations": ["submit:arbitrary"]},
                {"name": "state_update", "dangerous_operations": ["delete:"]},
                {"name": "memory", "dangerous_operations": []},
                {"name": "no_ops_declared"},
                "not-a-dict-entry",
            ],
        }
    }


@pytest.fixture
def host_direct_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定 policy.execution=host_direct（轨道 1 恒不触发），隔离验证轨道 2。

    测试环境无 ConfigCenter，policy 走内置默认（execution=command_in_container），
    会把所有工具判成轨道 1 危险——mock 掉才能单独断言轨道 2 的配置接线。
    """

    class _StubLoader:
        @staticmethod
        def resolve(_tool_name: str) -> Any:
            return SimpleNamespace(execution="host_direct")

    monkeypatch.setattr(_mod, "_policy_loader", _StubLoader())


class TestDangerousOperationsFromInjectedConfig:
    def test_config_declared_tool_args_hit_is_dangerous(self, host_direct_policy) -> None:
        """注入配置声明 + 参数命中 dangerous_operations → 判危险（轨道 2 参数级判定）。

        GAP 修复后的契约是参数级判定：声明本身不触发危险，参数命中声明模式
        才判危险（消除常规操作 100% 弹审批的过度打扰）。
        """
        plugin = SecurityCheckPlugin(config=_config_with_dangerous_ops())
        ctx = _ctx_without_services()
        # submit:arbitrary → 路径前缀模式，path 参数命中
        assert plugin._is_dangerous_tool(ctx, "task_submit", {"path": "arbitrary/zone"}) is True
        # delete: → 空模式操作匹配，operation 参数值命中
        assert plugin._is_dangerous_tool(ctx, "state_update", {"operation": "delete"}) is True

    def test_declared_but_args_not_hit_not_dangerous(self, host_direct_policy) -> None:
        """声明了 dangerous_operations 但参数未命中 / 无参数 → 不判危险。"""
        plugin = SecurityCheckPlugin(config=_config_with_dangerous_ops())
        ctx = _ctx_without_services()
        assert plugin._is_dangerous_tool(ctx, "task_submit", {"path": "workspace/x"}) is False
        assert plugin._is_dangerous_tool(ctx, "task_submit") is False

    def test_empty_or_missing_ops_not_dangerous_via_track2(self, host_direct_policy) -> None:
        """空 dangerous_operations / 未声明 / 未注入的工具 → 轨道 2 不判危险。"""
        plugin = SecurityCheckPlugin(config=_config_with_dangerous_ops())
        ctx = _ctx_without_services()
        assert plugin._is_dangerous_tool(ctx, "memory") is False
        assert plugin._is_dangerous_tool(ctx, "no_ops_declared") is False
        assert plugin._is_dangerous_tool(ctx, "unknown_tool") is False

    def test_without_config_track2_stays_silent(self, host_direct_policy) -> None:
        """未注入 builtin_tools_config 且无 registry → 轨道 2 为空，不判危险。"""
        plugin = SecurityCheckPlugin(config={})
        ctx = _ctx_without_services()
        assert plugin._is_dangerous_tool(ctx, "task_submit") is False

    def test_get_dangerous_operations_prefers_config_over_registry(self) -> None:
        """注入配置优先于 tool_registry 服务（config 命中即短路）。"""
        plugin = SecurityCheckPlugin(config=_config_with_dangerous_ops())

        registry = SimpleNamespace(
            get=lambda _name: SimpleNamespace(dangerous_operations=["from-registry"])
        )
        ctx = SimpleNamespace(
            state={},
            get_service=lambda name: registry if name == "tool_registry" else None,
        )
        assert plugin._get_dangerous_operations(ctx, "task_submit") == ["submit:arbitrary"]

    def test_registry_used_when_config_not_injected(self) -> None:
        """未注入配置时回退 tool_registry 服务（原有轨道保留）。"""
        plugin = SecurityCheckPlugin(config={})
        registry = SimpleNamespace(
            get=lambda _name: SimpleNamespace(dangerous_operations=["from-registry"])
        )
        ctx = SimpleNamespace(
            state={},
            get_service=lambda name: registry if name == "tool_registry" else None,
        )
        assert plugin._get_dangerous_operations(ctx, "some_tool") == ["from-registry"]

    def test_no_registry_no_config_returns_empty(self) -> None:
        """配置与 registry 均不可用 → 空列表（兜底；tools.global_registry 死分支已删）。"""
        plugin = SecurityCheckPlugin(config={})
        ctx = _ctx_without_services()
        assert plugin._get_dangerous_operations(ctx, "some_tool") == []

    def test_malformed_config_shapes_degrade_to_empty(self) -> None:
        """配置形状异常（非 dict / tools 非 list / 项缺 name）不崩溃，降级空表。"""
        for bad in (
            {"builtin_tools_config": "not-a-dict"},
            {"builtin_tools_config": {"tools": "not-a-list"}},
            {"builtin_tools_config": {"tools": [{"no_name": 1}]}},
        ):
            plugin = SecurityCheckPlugin(config=bad)
            assert plugin._dangerous_ops_by_tool == {}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
