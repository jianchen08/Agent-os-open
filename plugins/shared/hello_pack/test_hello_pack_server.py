# @feature: FP-0.2.二 内部模块 manifest(BUG-51 hello_pack server 补齐) | @ci: none-local
"""hello_pack/server.py 插件封装层测试。

plugin.json entry 声明 "python server.py"，但产物曾缺失该文件——sidecar/合宿
装载即失败（BUG-51 必败工具根源）。本测试锁两个契约：
1. server.py 可加载并暴露模块级 plugin 实例（合宿 light 组契约：host.py
   getattr(module, "plugin")，缺失即成员装载失败 fail-fast）；
2. 注册的 hello_pack 工具声明与 plugin.json 一致（G2 声明↔实现对照的测试面
   等价物），分发返回固定结构 {"message": "hello pack"}。

server.py 经 importlib 显式路径 + 唯一模块名加载；加载前逐出模块依赖的裸名
（hello_pack / agentos_plugin_sdk），避免污染其它测试加载的同名模块实例。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

MOD_NAME = "hello_pack_server_test"


@pytest.fixture
def server_mod() -> Any:
    """加载 server.py（每次重建，隔离模块级状态）。"""
    if MOD_NAME in sys.modules:
        del sys.modules[MOD_NAME]
    for bare in ("hello_pack", "agentos_plugin_sdk"):
        sys.modules.pop(bare, None)
    spec = importlib.util.spec_from_file_location(MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


def _read_manifest() -> dict[str, Any]:
    return json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))


class TestServerModule:
    def test_module_exposes_cohost_plugin_instance(self, server_mod: Any) -> None:
        # 合宿契约：模块级 plugin 实例必须存在（host.py getattr 缺失即装载失败）
        plugin = getattr(server_mod, "plugin", None)
        assert plugin is not None, "server.py 必须暴露模块级 plugin（合宿 light 组契约）"

    def test_registers_hello_pack_tool(self, server_mod: Any) -> None:
        plugin = server_mod.create_plugin()
        assert "hello_pack" in plugin._tools, "hello_pack 工具必须注册"
        tool = plugin._tools["hello_pack"]
        assert tool.schema is not None
        assert callable(tool.handler)

    def test_registered_input_schema_matches_manifest(self, server_mod: Any) -> None:
        # G2 对照测试面等价物：注册 schema 与 plugin.json 声明逐字一致
        # （{"type":"object","properties":{}} ≠ {"type":"object"}，声明↔实现
        # 漂移会被 G2 剔除）
        declared = _read_manifest()["capabilities"]["tools"][0]["input_schema"]
        plugin = server_mod.create_plugin()
        assert plugin._tools["hello_pack"].schema == declared

    def test_output_schema_declared_in_manifest(self, server_mod: Any) -> None:
        manifest_tool = _read_manifest()["capabilities"]["tools"][0]
        assert manifest_tool["output_schema"]["required"] == ["message"]


class TestRun:
    def test_run_starts_server(self, server_mod: Any) -> None:
        """run() 启动模块级 plugin 的阻塞入口（验证接线）。"""
        started: list[str] = []

        class _FakePlugin:
            def run(self) -> None:
                started.append("started")

        with patch.object(server_mod, "plugin", _FakePlugin()):
            assert server_mod.run() is None
        assert started == ["started"]


class TestDispatch:
    def test_hello_pack_returns_fixed_message(self, server_mod: Any) -> None:
        from hello_pack import dispatch

        assert dispatch("hello_pack", {}) == {"message": "hello pack"}
        assert dispatch("hello_pack", None) == {"message": "hello pack"}

    def test_unknown_tool_raises_key_error(self, server_mod: Any) -> None:
        from hello_pack import dispatch

        with pytest.raises(KeyError):
            dispatch("ghost_tool", {})
