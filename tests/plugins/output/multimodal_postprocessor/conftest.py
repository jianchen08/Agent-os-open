"""multimodal_postprocessor 测试 conftest——注入插件目录与 plugins/shared/ 到 sys.path。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "output" / "multimodal_postprocessor"
)
_SHARED_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "shared"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
