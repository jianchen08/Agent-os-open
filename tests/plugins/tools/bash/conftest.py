# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""bash 工具进度推送测试 conftest——注入插件目录到 sys.path。

插件位于 plugins/shared/tools/bash/，内部用平铺 import
（from tool import BashTool / from process_manager import ...）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "tools" / "bash"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
