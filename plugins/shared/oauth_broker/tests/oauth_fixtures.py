"""oauth_broker 测试共用装载器与助手（独名模块——跨插件测试同进程不互扰）。

与 ``ecosystem_import/tests/codex_fixtures.py`` 同款：源目录晋升（插件目录 +
共享根 + 本 tests 目录入 ``sys.path``）+ 按唯一模块名装载 ``tool.py`` /
``server.py``（裸名 ``tool``/``server`` 跨插件同名）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_SHARED_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR), str(_TESTS_DIR)]
for _dir in _PLUGIN_SOURCE_DIRS:
    if _dir not in sys.path:
        sys.path.insert(0, _dir)


def load_tool_module() -> Any:
    """按唯一模块名装载本插件 ``tool.py``。"""
    module_name = "oauth_broker_tool_impl"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_server_module() -> Any:
    """按唯一模块名装载 ``server.py``（导入即注册，模块级 plugin 实例即合宿契约面）。"""
    module_name = "oauth_broker_server_probe"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
