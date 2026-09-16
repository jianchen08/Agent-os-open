# @feature: FP-0.2.二 导演服务面 | @ci: python-coverage
"""director server/plugin.json 一致性护栏：服务声明 = 实现（G2 同源口径）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).parent


def _load_server() -> Any:
    sys.path.insert(0, str(_PLUGIN_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "director_server_test", _PLUGIN_DIR / "server.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["director_server_test"] = module
        spec.loader.exec_module(module)
    finally:
        while str(_PLUGIN_DIR) in sys.path:
            sys.path.remove(str(_PLUGIN_DIR))
    return module


def test_server_registers_all_declared_services() -> None:
    """manifest services 每一项都必须在 server.py 注册（声明=实现）。"""
    mod = _load_server()
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = {s["name"] for s in manifest["capabilities"]["services"]}
    registered = set(mod.plugin._tools.keys())  # noqa: SLF001 注册表内省
    assert declared == registered
    assert all(name.startswith("director.") for name in declared)


def test_manifest_is_system_plugin_persistent() -> None:
    """常驻系统插件：idle_timeout=0（不参与空闲回收）。"""
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["plugin_type"] == "system"
    assert manifest["lifecycle"]["idle_timeout_secs"] == 0
