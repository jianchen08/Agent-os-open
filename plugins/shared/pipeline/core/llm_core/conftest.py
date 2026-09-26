"""llm_core 插件目录内测试的 conftest——声明源目录供逐出驱动晋升。

整树收集（pytest tests/ plugins/）时 plugins/conftest.py 的驱动据
_PLUGIN_SOURCE_DIRS 把本插件目录推到 sys.path 最前，使模块级
``from adapter import ...`` 确定性解析到 llm_core 自己的 adapter.py
（成熟度评估 §0.3(4)b 类：曾命中 channel_qq/adapter.py 致 ImportError）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parents[1]

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
