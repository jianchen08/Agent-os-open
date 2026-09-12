"""monitoring 测试 conftest — 0.2 平铺 import 路径。

0.2 架构下监控模块位于 plugins/shared/system/monitoring/，内部用平铺 import
（from health import ...）。本 conftest 把该目录加入 sys.path。

``_PLUGIN_SOURCE_DIRS`` 是根 conftest 裸名逐出钩子（tests/conftest.py）的置前
契约：全量车道中各插件测试目录的 conftest 都会模块级 insert(0) 各自源目录，
仅靠本次 insert 的先后次序，``import server`` 会解析到最后加载者的同名模块。
声明本名单后，钩子在每个测试文件收集前把 monitoring 目录推到 sys.path 最前，
平铺导入确定性地解析到本插件。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MONITORING_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "monitoring"

_PLUGIN_SOURCE_DIRS = [str(_MONITORING_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
