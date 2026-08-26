# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_common 渠道共享包测试（A5.2 补）。

覆盖 base_combo_adapter / input_adapter / output_adapter 三文件的
默认实现与抽象契约。模块经 importlib 显式加载（唯一模块名），
不依赖 use_channel 的 sys.path 副作用。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_CC_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "channel_common"
_FILES = {
    "base_combo": "base_combo_adapter.py",
    "input": "input_adapter.py",
    "output": "output_adapter.py",
}
_EVICT = {"base_combo_adapter", "input_adapter", "output_adapter"}


@pytest.fixture()
def load_common() -> Iterator[dict[str, Any]]:
    """加载 channel_common 三模块，teardown 恢复被逐出的同名模块。"""
    saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
    loaded: dict[str, Any] = {}
    try:
        for m in _EVICT:
            sys.modules.pop(m, None)
        for name, filename in _FILES.items():
            mod_name = f"channel_common_{name}_test"
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _CC_DIR / filename)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in _FILES:
            sys.modules.pop(f"channel_common_{name}_test", None)
        for name, mod in saved_modules.items():
            sys.modules[name] = mod


class TestBaseComboAdapter:
    """BaseComboAdapter 基于 stream_client 的状态查询默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], connected: bool) -> Any:
        base = load_common["base_combo"]

        def _init(self: Any) -> None:
            self.stream_client = SimpleNamespace(is_connected=connected)

        dummy_cls = type(
            "Dummy",
            (base.BaseComboAdapter,),
            {"channel_type": "dummy", "__init__": _init},
        )
        return dummy_cls()

    def test_is_connected_delegates_to_stream_client(
        self, load_common: dict[str, Any]
    ) -> None:
        assert self._make(load_common, True).is_connected is True
        assert self._make(load_common, False).is_connected is False

    @pytest.mark.asyncio
    async def test_health_check_reflects_stream_client(
        self, load_common: dict[str, Any]
    ) -> None:
        assert await self._make(load_common, True).health_check() is True
        assert await self._make(load_common, False).health_check() is False

    def test_get_status_shape(self, load_common: dict[str, Any]) -> None:
        connected = self._make(load_common, True).get_status()
        assert connected == {"type": "dummy", "connected": True, "healthy": True}
        disconnected = self._make(load_common, False).get_status()
        assert disconnected["type"] == "dummy"
        assert disconnected["connected"] is False
        # 性质断言：healthy 与 connected 同源
        assert disconnected["healthy"] == disconnected["connected"]


class TestIInputAdapter:
    """IInputAdapter 抽象契约与默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], name: str) -> Any:
        mod = load_common["input"]

        async def _receive(self: Any) -> dict[str, Any]:
            return {"user_input": "x"}

        return type(name, (mod.IInputAdapter,), {"receive": _receive})()

    def test_abstract_cannot_instantiate(self, load_common: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            load_common["input"].IInputAdapter()

    @pytest.mark.asyncio
    async def test_default_health_check_and_connected(
        self, load_common: dict[str, Any]
    ) -> None:
        adapter = self._make(load_common, "AlphaInput")
        assert await adapter.health_check() is True
        assert adapter.is_connected is True

    def test_get_status_uses_class_name(self, load_common: dict[str, Any]) -> None:
        alpha = self._make(load_common, "AlphaInput").get_status()
        assert alpha == {"type": "AlphaInput", "connected": True, "healthy": True}
        beta = self._make(load_common, "BetaInput").get_status()
        assert beta["type"] == "BetaInput"
        assert beta["connected"] is True


class TestIOutputAdapter:
    """IOutputAdapter 抽象契约与默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], name: str) -> Any:
        mod = load_common["output"]

        async def _send(self: Any, state: dict[str, Any]) -> None:
            return None

        async def _send_stream(self: Any, chunk: dict[str, Any]) -> None:
            return None

        return type(
            name,
            (mod.IOutputAdapter,),
            {"send": _send, "send_stream": _send_stream},
        )()

    def test_abstract_cannot_instantiate(self, load_common: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            load_common["output"].IOutputAdapter()

    @pytest.mark.asyncio
    async def test_default_health_check_and_connected(
        self, load_common: dict[str, Any]
    ) -> None:
        adapter = self._make(load_common, "AlphaOutput")
        assert await adapter.health_check() is True
        assert adapter.is_connected is True

    def test_get_status_uses_class_name(self, load_common: dict[str, Any]) -> None:
        alpha = self._make(load_common, "AlphaOutput").get_status()
        assert alpha == {"type": "AlphaOutput", "connected": True, "healthy": True}
        beta = self._make(load_common, "BetaOutput").get_status()
        assert beta["type"] == "BetaOutput"
