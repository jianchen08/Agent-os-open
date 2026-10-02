"""tool_core 插件目录内测试的 conftest——声明源目录供逐出驱动晋升。

整树收集（pytest tests/ plugins/）时 plugins/conftest.py 的驱动据
_PLUGIN_SOURCE_DIRS 把本插件目录推到 sys.path 最前，使模块级
``from plugin import ...`` 确定性解析到本目录实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
# plugins/shared 根（目录上溯 core → pipeline → shared）：`pipeline` 包与
# agentos_plugin_sdk 的解析根。
_SHARED_DIR = _PLUGIN_DIR.parents[2]

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
