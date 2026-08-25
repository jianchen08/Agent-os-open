"""suites/task 测试 conftest — 注入 tasks 插件目录到 sys.path。

tasks/ 插件内部用平铺 import（from storage import / from task_types import），
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

import pytest


@pytest.fixture(autouse=True)
def _tasks_path_guard():
    """每个测试执行前锁定 tasks 插件目录为 sys.path[0]，结束后恢复。

    其他插件测试的收集期/运行期注入会把各自目录推到 sys.path[0] 且不
    恢复；本套件测试（如 timer_manager 竞态）的函数体懒加载依赖 sys.path
    首位是本插件目录。用例结束后恢复原路径序——tasks 目录驻留会压制
    system/workspace/ 的 namespace 包（tasks/workspace.py 裸模块优先）。
    """
    _saved = sys.path[:]
    _s = str(_TASKS_DIR)
    if sys.path[0] != _s:
        sys.path[:] = [p for p in sys.path if p != _s]
        sys.path.insert(0, _s)
    yield
    sys.path[:] = _saved
