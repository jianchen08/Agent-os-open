# @feature: FP-0.2.二 模式体系 P2 context_build 物料档注入 | @ci: python-coverage
"""context_build server.py 服务接缝测试（模式体系 P2 取数通道）。

锁三件事（业务逻辑归 plugin.py/mode_material.py 自身测试）：
1. **fetch_mode_profile 调用形状**：tool-executor.invoke + 显式
   plugin_id=mode_<mode> + tool_name=mode.get_profile（eval_harness 先例同形，
   信封原样透传，解析归插件侧消费边界）；
2. **能力句柄缺席上抛**：tool-executor 未注入（KeyError）原样上抛——
   降级裁决在插件侧（_apply_mode_material 捕获后 warning 不注入）；
3. **get_instance 接线**：单例构造带 profile_fetcher=fetch_mode_profile，
   lru_cache 幂等，on_unload 复位后重建。

[来源: docs/working/模式体系落地设计_20260915.md §3.3②/§4.1/§4.2；
服务通道先例 plugins/shared/system/eval_harness/server.py]
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "input" / "context_build"
)


def _load_server() -> Any:
    """以唯一裸名装载 context_build/server.py（同 godot_context 缺口测试先例）。"""
    mod_name = "context_build_server_seam_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCapabilityHandle:
    """tool-executor 句柄替身：记录 call(method, params)，回放脚本化返回。"""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        return self.result


class TestFetchModeProfile:
    def test_invoke_shape_and_envelope_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """调用形状钉死：invoke + 显式 plugin_id + tool_name；信封原样透传。"""
        server = _load_server()
        envelope = {"data": {"mode": "utdemo", "name": "演示模式"}}
        handle = FakeCapabilityHandle(result=envelope)
        monkeypatch.setattr(
            server.plugin, "get_capability", lambda _name: handle
        )

        res = asyncio.run(server.fetch_mode_profile("utdemo"))

        assert res == envelope, "信封原样透传（解析归插件侧 unwrap_mode_profile）"
        assert handle.calls == [
            ("invoke", {"tool_name": "mode.get_profile",
                        "plugin_id": "mode_utdemo", "args": {}})
        ], "tool-executor 显式 plugin_id 通道（mode_<mode>）"

    def test_capability_absent_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool-executor 未注入 → KeyError 上抛（降级裁决在插件侧）。"""
        server = _load_server()

        def _raise(name: str) -> Any:
            raise KeyError(f"capability not injected: {name}")

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        with pytest.raises(KeyError):
            asyncio.run(server.fetch_mode_profile("utdemo"))


class TestGetInstanceWiring:
    def test_singleton_wires_fetcher_and_rebuilds_after_unload(self, monkeypatch) -> None:
        """单例带 fetch_mode_profile 接线；on_unload 复位缓存后重建新实例。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ContextBuildPlugin)
        assert inst._profile_fetcher is server.fetch_mode_profile, (
            "get_instance 必须把 mode.get_profile 取数通道接进插件"
        )
        assert server.get_instance() is inst, "lru_cache 幂等"

        asyncio.run(server._on_unload({}))
        second = server.get_instance()
        assert second is not inst, "on_unload 复位后应重建单例"
        assert second._profile_fetcher is server.fetch_mode_profile
