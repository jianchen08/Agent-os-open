# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""connectors 插件（连接器降级 + 注册表）单元测试。

覆盖（对齐 plugins/shared/system/connectors/）：
1. DegradationManager —— open_file/get_selection/show_diff/unsupported 降级路径
2. ConnectorRegistry —— 注册/注销/活跃连接器/能力匹配/排序

测试不依赖真实 IDE 连接器——用伪 connector 对象（duck-typed）注入注册表。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SYSTEM_DIR = _PLUGIN_DIR.parent
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))

_CONNECTOR_TYPES_MOD = None


def _load(name: str, unique: str) -> Any:
    """按唯一模块名加载模块，避免污染全局命名空间。"""
    mod_name = unique
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _connector_types() -> Any:
    global _CONNECTOR_TYPES_MOD
    if _CONNECTOR_TYPES_MOD is None:
        _CONNECTOR_TYPES_MOD = _load("connector_types", "connector_types_test")
    return _CONNECTOR_TYPES_MOD


def _load_degradation() -> Any:
    return _load("degradation", "connectors_degradation_test")


def _load_registry() -> Any:
    return _load("registry", "connectors_registry_test")


# ═══════════════════════════════════════════════════════════
# DegradationManager
# ═══════════════════════════════════════════════════════════


class TestDegradationManager:
    def test_can_handle_locally(self) -> None:
        mgr = _load_degradation().DegradationManager()
        for action in ("open_file", "get_selection", "show_diff", "insert_content", "jump_to"):
            assert mgr.can_handle_locally(action) is True
        assert mgr.can_handle_locally("unknown_action") is False

    def test_unsupported_action_fails(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback("not_supported", {})
        assert result.success is False
        assert "不支持的操作类型" in (result.error or "")

    def test_open_file_success(self, tmp_path: Path) -> None:
        mgr = _load_degradation().DegradationManager()
        f = tmp_path / "a.py"
        f.write_text("print(1)", encoding="utf-8")
        result = mgr.execute_with_fallback("open_file", {"file_path": str(f)})
        assert result.success is True
        assert result.data["content"] == "print(1)"  # type: ignore[index]
        assert result.data["degraded"] is True  # type: ignore[index]

    def test_open_file_missing_path(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback("open_file", {})
        assert result.success is False
        assert "file_path" in (result.error or "")

    def test_open_file_not_found(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback("open_file", {"file_path": "/no/such/file.txt"})
        assert result.success is False
        assert "不存在" in (result.error or "")

    def test_get_selection_empty_context(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback("get_selection", {})
        assert result.success is True
        assert result.data["active_file"] is None  # type: ignore[index]

    def test_show_diff_with_changes(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback(
            "show_diff",
            {"original_content": "a\nb\n", "new_content": "a\nc\n", "file_path": "x.py", "title": "改 b→c"},
        )
        assert result.success is True
        assert "-b" in result.data["diff_text"]  # type: ignore[index]
        assert "+c" in result.data["diff_text"]  # type: ignore[index]
        assert result.data["diff_text"].startswith("[Diff: 改 b→c]\n")  # type: ignore[index]

    def test_show_diff_no_changes(self) -> None:
        mgr = _load_degradation().DegradationManager()
        result = mgr.execute_with_fallback("show_diff", {"original_content": "same", "new_content": "same"})
        assert result.success is True
        assert "无差异" in result.data["diff_text"]  # type: ignore[index]

    def test_unsupported_actions_return_hint(self) -> None:
        mgr = _load_degradation().DegradationManager()
        for action in ("insert_content", "jump_to"):
            result = mgr.execute_with_fallback(action, {})
            assert result.success is True
            assert result.data["degraded"] is True  # type: ignore[index]

    def test_handler_exception_is_caught(self, monkeypatch) -> None:
        """降级处理器抛异常 → 返回失败结果而非上抛。"""
        mgr = _load_degradation().DegradationManager()

        def _boom(params):
            raise RuntimeError("disk error")

        monkeypatch.setattr(mgr, "_fallback_open_file", _boom)
        result = mgr.execute_with_fallback("open_file", {"file_path": "x"})
        assert result.success is False
        assert "disk error" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# ConnectorRegistry
# ═══════════════════════════════════════════════════════════


class _FakeConnector:
    """duck-typed 伪连接器。"""

    def __init__(self, conn_type: str, connected: bool, priority: int = 0, capabilities: list[str] | None = None):
        self.connector_type = conn_type
        self.is_connected = connected
        self._priority = priority
        self._capabilities = capabilities or []

    def get_info(self) -> Any:
        types_mod = _connector_types()
        return types_mod.ConnectorInfo(
            connector_type=self.connector_type,
            display_name=f"conn-{self.connector_type}",
            capabilities=list(self._capabilities),
            priority=self._priority,
        )


class TestConnectorRegistry:
    def test_register_and_get(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        c = _FakeConnector("vscode", True)
        reg.register(c)
        assert reg.get_connector("vscode") is c
        assert reg.has("vscode") is True
        assert reg.count() == 1

    def test_register_overwrite(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("vscode", True))
        reg.register(_FakeConnector("vscode", False))
        assert reg.get_connector("vscode").is_connected is False  # type: ignore[union-attr]

    def test_unregister(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("vim", True))
        assert reg.unregister("vim") is None
        with pytest.raises(KeyError):
            reg.unregister("vim")

    def test_get_active_connector_none(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("vim", connected=False))
        assert reg.get_active_connector() is None

    def test_get_active_connector_priority_and_name(self) -> None:
        """已连接优先 + 优先级大者优先 + 同优先级按类型名字母序。"""
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("zebra", connected=False, priority=100))
        reg.register(_FakeConnector("vscode", connected=True, priority=5))
        reg.register(_FakeConnector("idea", connected=True, priority=10))
        assert reg.get_active_connector().connector_type == "idea"  # type: ignore[union-attr]

        reg2 = _load_registry().ConnectorRegistry()
        reg2.register(_FakeConnector("zeta", connected=True, priority=1))
        reg2.register(_FakeConnector("alpha", connected=True, priority=1))
        assert reg2.get_active_connector().connector_type == "alpha"  # type: ignore[union-attr]

    def test_list_connectors_sorted(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("low", True, priority=1))
        reg.register(_FakeConnector("high", True, priority=10))
        infos = reg.list_connectors()
        assert [i.connector_type for i in infos] == ["high", "low"]

    def test_get_best_connector_for(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("no-cap", True, priority=99, capabilities=["other"]))
        reg.register(_FakeConnector("mid", True, priority=5, capabilities=["open_file", "show_diff"]))
        reg.register(_FakeConnector("top", True, priority=10, capabilities=["open_file"]))
        reg.register(_FakeConnector("off", False, priority=100, capabilities=["open_file"]))

        best = reg.get_best_connector_for("open_file")
        assert best.connector_type == "top"  # type: ignore[union-attr]
        assert reg.get_best_connector_for("not-a-cap") is None

    def test_clear(self) -> None:
        reg = _load_registry().ConnectorRegistry()
        reg.register(_FakeConnector("a", True))
        reg.clear()
        assert reg.count() == 0
