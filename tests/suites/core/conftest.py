"""suites/core 测试 conftest — 注入 tasks 插件目录到 sys.path。

tasks/ 插件内部用平铺 import（from service import / from task_types import），
需把 plugins/shared/system/tasks/ 加入 sys.path 才能解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

for _d in (_TASKS_DIR, _SHARED_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)
