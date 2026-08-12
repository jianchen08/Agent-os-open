"""把 tasks 插件目录加入 sys.path（供 tests/ 顶级的 test_task_*.py 复用）。

0.2 架构下任务模块位于 plugins/shared/system/tasks/，内部用平铺 import
（from service import ...）。本模块由各 task 测试在顶部 import 一次以注入路径。

注意：0.1 的 tasks.types 在 0.2 重命名为 tasks.task_types。
其它子模块（state_machine / storage / service / timer_manager）0.2 保留同名平铺。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"

_s = str(_TASKS_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
