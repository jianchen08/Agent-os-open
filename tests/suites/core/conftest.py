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

# 预加载 task_types/service：pytest 收集测试模块时若首次导入 task_types 会经过
# assertion-rewrite 钩子（与标准加载器产生两份 TaskStatus 枚举类，== 比较失败），
# 此处先以标准加载器导入，使收集阶段命中 sys.modules 缓存（同一类实例）。
import task_types  # noqa: E402,F401
import service  # noqa: E402,F401

import pytest


@pytest.fixture(autouse=True)
def _tasks_path_guard():
    """每个测试执行前锁定 tasks 插件目录为 sys.path[0]。

    其他插件测试（如 multimodal）的 autouse fixture 会把各自目录推到
    sys.path[0] 且不恢复；tasks 测试运行期懒加载 `from storage import
    TaskStorage` 依赖 sys.path 首位是本插件目录，此处锁定。
    仅 pop 跨插件竞争名 storage：task_types 必须保留——测试模块收集期
    绑定的 TaskStatus 实例依赖其驻留，重新导入会产生第二份枚举类。
    """
    _s = str(_TASKS_DIR)
    if sys.path[0] != _s:
        sys.path.insert(0, _s)
    sys.modules.pop("storage", None)
    yield
