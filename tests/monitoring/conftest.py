"""monitoring 测试 conftest — 0.2 平铺 import 路径。

0.2 架构下监控模块位于 plugins/shared/system/monitoring/，内部用平铺 import
（from health import ...）。本 conftest 把该目录加入 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MONITORING_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "monitoring"

_s = str(_MONITORING_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
