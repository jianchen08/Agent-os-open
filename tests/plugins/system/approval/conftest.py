"""approval 测试 conftest——把插件目录注入 sys.path。

插件位于 plugins/shared/system/approval/，server.py 用平铺 import
（``import server`` 解析模块本体）。本 conftest 把源目录推到 sys.path 最前，
并对齐 rollback 测试的 _PLUGIN_SOURCE_DIRS 约定（供 tests/plugins/conftest.py
的 pytest_runtest_setup 治理同进程多插件裸名串扰）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system" / "approval"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

_s = str(_PLUGIN_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
