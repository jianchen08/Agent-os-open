# @feature: B11-L3 工具注册面单源 | @ci: python-coverage
"""eval_probe 注册面断言：create_plugin 注册表 = manifest 声明面（双向对账）。

server 的工具注册（create_plugin → register_tool）是工具面唯一真值——曾并存
的 TOOL_REGISTRY 字面量表已删（死副本，全仓零消费方）。本测试把注册面与
plugin.json 的 capabilities.tools[].name 逐向对账：漏注册/多注册/改名漂移
任一发生即红（与 builtin_tools tests/test_tools.py 的 parity 锁同型）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_server() -> Any:
    """按唯一模块名装载 server.py（导入即注册，模块级 plugin 实例即合宿契约面）。"""
    spec = importlib.util.spec_from_file_location("eval_probe_registration_probe", _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registered_tool_ids(plugin: Any) -> set[str]:
    """注册面枚举（plugin_probe 探针同款 getattr 形态）。"""
    return set(getattr(plugin, "_tools", {}) or {})


def _manifest_tool_ids() -> set[str]:
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return {t["name"] for t in (manifest.get("capabilities", {}).get("tools") or [])}


def test_registered_tools_match_manifest() -> None:
    module = _load_server()
    registered = _registered_tool_ids(module.plugin)
    declared = _manifest_tool_ids()
    assert registered == declared, f"注册面与 manifest 声明面漂移：多注册 {registered - declared}，漏注册 {declared - registered}"
