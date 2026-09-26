# @feature: FP-0.2.二 管道插件服务接缝 | @ci: python-coverage
"""context_build server.py 服务接缝测试。

context_build 回归纯上下文构建后（2026-09-24 模式物料架构重构），取数通道
fetch_mode_profile 归 mode_material_inject 插件（其接缝测试随插件目录），
本文件锁单例接线语义：
1. **get_instance 接线**：单例按插件配置构造，lru_cache 幂等，on_unload
   复位后重建。

[来源: tests/plugins/input/mode_material_inject 同形接缝先例]
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


class TestGetInstanceWiring:
    def test_singleton_rebuilds_after_unload(self) -> None:
        """单例 lru_cache 幂等；on_unload 复位缓存后重建新实例。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ContextBuildPlugin)
        assert server.get_instance() is inst, "lru_cache 幂等"

        asyncio.run(server._on_unload({}))
        second = server.get_instance()
        assert second is not inst, "on_unload 复位后应重建单例"

    def test_no_mode_profile_fetcher_wiring_remains(self) -> None:
        """纯上下文构建契约：server 不再持有 mode.get_profile 取数通道。"""
        server = _load_server()
        assert not hasattr(server, "fetch_mode_profile"), (
            "模式物料取数通道已移交 mode_material_inject，不得残留"
        )
