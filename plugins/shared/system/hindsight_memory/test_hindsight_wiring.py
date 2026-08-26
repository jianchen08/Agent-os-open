# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""wiring.py（记忆后端接线工具）测试。

验证内容：
1. _bind_caller 剥掉能力前缀（tool-executor.invoke → invoke），非前缀方法原样透传；
2. make_capability_caller：tool-executor 句柄注入 → 可用 caller；
   未注入（get_capability 抛 KeyError）→ None + 告警日志（不崩溃）；
3. build_memory_backend：能力缺失 → None；get_memory_backend 抛错 → None（降级）；
   正常 → HindsightBackend 实例，配置透传（plugin.get_config 被读取）。

capability 句柄用真实 SDK CapabilityHandle 构造（call_fn 注入断言），
不依赖真实 hindsight 包。

[来源: plugins/shared/system/hindsight_memory/wiring.py]
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 wiring.py（每次新建，避免模块级 sys.path 污染累积）。"""
    mod_name = "hindsight_wiring_test"
    path = _PLUGIN_DIR / "wiring.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load wiring.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


def test_module_self_registers_on_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件目录不在 sys.path 时，wiring 导入自插（sidecar 本地模块可达性）。"""
    plugin_dir = str(_PLUGIN_DIR)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != plugin_dir])

    assert plugin_dir not in sys.path
    _load_module()
    assert plugin_dir in sys.path


class _FakePlugin:
    """AgentOSPlugin 替身：get_capability / get_config 可编程。"""

    def __init__(self, handle: Any | None, config: dict[str, Any] | None = None) -> None:
        self._handle = handle
        self._config = config or {}

    def get_capability(self, name: str) -> Any:
        if self._handle is None:
            raise KeyError(f"capability not injected: {name}")
        return self._handle

    def get_config(self) -> dict[str, Any]:
        return self._config


# ═══════════════════════════════════════════════════════════
# 1. _bind_caller：能力前缀剥离
# ═══════════════════════════════════════════════════════════


class TestBindCaller:
    async def test_strips_capability_prefix(self, mod: Any) -> None:
        """memory_backend 传完整 wire method（tool-executor.invoke），
        闭包剥掉前缀后转交 handle.call（避免双命名空间）。"""
        handle = MagicMock()
        handle.call = AsyncMock(return_value={"ok": 1})
        caller = mod._bind_caller(handle, "tool-executor")

        result = await caller("tool-executor.invoke", {"tool_name": "hindsight.retain"})

        assert result == {"ok": 1}
        handle.call.assert_awaited_once_with("invoke", {"tool_name": "hindsight.retain"})

    async def test_passes_non_prefixed_method_unchanged(self, mod: Any) -> None:
        """非本能力前缀的 method 原样透传（不做错误的前缀剥离）。"""
        handle = MagicMock()
        handle.call = AsyncMock(return_value={"ok": 1})
        caller = mod._bind_caller(handle, "tool-executor")

        result = await caller("memory.create", {"x": 1})

        assert result == {"ok": 1}
        handle.call.assert_awaited_once_with("memory.create", {"x": 1})

    async def test_return_value_flows_through(self, mod: Any) -> None:
        """caller 的返回值原样返回（调用方取业务 dict）。"""
        handle = MagicMock()
        handle.call = AsyncMock(return_value={"id": "m1", "stored": True})
        caller = mod._bind_caller(handle, "tool-executor")

        result = await caller("tool-executor.invoke", {})

        assert result == {"id": "m1", "stored": True}


# ═══════════════════════════════════════════════════════════
# 2. make_capability_caller
# ═══════════════════════════════════════════════════════════


class TestMakeCapabilityCaller:
    async def test_returns_working_caller_when_injected(self, mod: Any) -> None:
        """tool-executor 句柄注入 → 返回绑定 caller（可 await 调用）。"""
        handle = MagicMock()
        handle.call = AsyncMock(return_value={"results": []})
        plugin = _FakePlugin(handle)

        caller = mod.make_capability_caller(plugin)

        assert caller is not None
        result = await caller("tool-executor.invoke", {"tool_name": "hindsight.recall"})
        assert result == {"results": []}
        handle.call.assert_awaited_once_with("invoke", {"tool_name": "hindsight.recall"})

    def test_missing_capability_returns_none_and_warns(self, mod: Any, caplog: pytest.LogCaptureFixture) -> None:
        """能力未注入（KeyError）→ None + 告警日志（插件降级不崩溃）。"""
        plugin = _FakePlugin(None)

        with caplog.at_level(logging.WARNING):
            caller = mod.make_capability_caller(plugin)

        assert caller is None
        assert any("tool-executor" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# 3. build_memory_backend
# ═══════════════════════════════════════════════════════════


class TestBuildMemoryBackend:
    async def test_returns_backend_with_config(self, mod: Any) -> None:
        """能力就绪 → get_memory_backend 被调用且配置透传，返回 HindsightBackend 实例。"""
        handle = AsyncMock()
        plugin = _FakePlugin(handle, {"default_bank_id": "tenant-1"})

        backend = mod.build_memory_backend(plugin)

        assert backend is not None
        # 构造的 caller 可 await（真实接线语义：method 前缀剥离后转交 handle）
        await backend.search(query="q", user_id="u")
        handle.call.assert_awaited_once_with("invoke", {
            "tool_name": "hindsight.recall", "plugin_id": "hindsight_memory_service",
            "args": {"bank_id": "u", "query": "q", "top_k": 5},
        })

    def test_missing_capability_returns_none(self, mod: Any) -> None:
        """能力未注入 → None（build 不抛）。"""
        backend = mod.build_memory_backend(_FakePlugin(None))
        assert backend is None

    def test_factory_error_returns_none_and_warns(self, mod: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """get_memory_backend 抛错（如后端配置非法）→ None + 告警（sidecar 降级）。"""
        plugin = _FakePlugin(AsyncMock())

        def _boom(config: Any, capability_caller: Any) -> Any:
            raise ValueError("backend 配置非法")

        # wiring 在函数体内 `from memory_backend import get_memory_backend`——
        # 经 sys.modules 查找，须替换该模块对象使构建路径抛错
        stub = types.ModuleType("memory_backend")
        stub.get_memory_backend = _boom
        monkeypatch.setitem(sys.modules, "memory_backend", stub)
        with caplog.at_level(logging.WARNING):
            backend = mod.build_memory_backend(plugin)

        assert backend is None
        assert any("构建失败" in r.getMessage() for r in caplog.records)
