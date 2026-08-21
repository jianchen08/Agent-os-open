# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""multimodal 测试 conftest——把插件目录注入 sys.path。

插件位于 plugins/shared/system/multimodal/，内部用平铺 import
（from adapter import ... / from capabilities import ... / from asr import ...）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰（adapter/capabilities/asr 等跨插件同名）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system" / "multimodal"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

_s = str(_PLUGIN_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)