"""stuck_detector 测试 conftest——注入插件目录与 plugins/shared/ 到 sys.path。

插件位于 plugins/shared/pipeline/output/stuck_detector/，用平铺 import；
同时 from pipeline.plugin / pipeline.types 需要 plugins/shared/ 在 path 上
（pipeline 是 namespace 包）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
用于治理同进程多插件裸名串扰（每个测试执行前据此重置 sys.path/sys.modules）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "output" / "stuck_detector"
)
_SHARED_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "shared"
)

# 优先级从高到低：插件本目录在前，shared 在后（供裸名解析）
_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
