"""llm_provider_gateway 插件测试的 conftest——声明源目录供逐出驱动晋升。

conftest 位于插件 ``tests/`` 子目录（与 acp_quota_pool 同款：整树收集时
plugins/conftest.py 的驱动据 _PLUGIN_SOURCE_DIRS 把相关目录推到 sys.path
最前，使裸名导入（gateway_manager / gateway_wire）确定性解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_SHARED_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR), str(_TESTS_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
