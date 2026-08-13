"""monitoring 插件测试 conftest——把插件目录注入 sys.path。

插件位于 plugins/shared/system/monitoring/，内部用平铺 import
（from performance_monitor import ... / from health import ... / from metrics import ...）。
本 conftest 同时暴露 _PLUGIN_SOURCE_DIRS 给 tests/plugins/conftest.py 的
pytest_runtest_setup，治理同进程多插件裸名串扰（server/health/metrics 等跨插件同名）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system" / "monitoring"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

_s = str(_PLUGIN_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
