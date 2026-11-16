"""acp_quota_pool 插件测试的 conftest——声明源目录供逐出驱动晋升。

conftest 位于插件 ``tests/`` 子目录（payload scripts/ 不含测试，播种面纯净）。
整树收集（pytest tests/ plugins/）时 plugins/conftest.py 的驱动据
_PLUGIN_SOURCE_DIRS 把相关目录推到 sys.path 最前，使裸名导入
（acp_registry / acp_ledger / acp_engine / server / user_space）确定性解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_SCRIPTS_DIR = _PLUGIN_DIR / "scripts"
_SHARED_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [str(_SCRIPTS_DIR), str(_PLUGIN_DIR), str(_SHARED_DIR), str(_TESTS_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
