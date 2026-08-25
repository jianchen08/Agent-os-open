"""suites/core 测试 conftest — 注入 tasks 插件目录到 sys.path。

tasks/ 插件内部用平铺 import（from service import / from task_types import），
需把 plugins/shared/system/tasks/ 加入 sys.path 才能解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

# 无条件前置（先去重）：其他插件套件（如 tests/plugins 的 human）的
# conftest 会把各自目录推到 sys.path[0] 且驻留；本套件收集期
# `from service import TaskService` 依赖 tasks 目录在最前（human 的
# service.py 同名劫持会致 ImportError）。先 shared 后 tasks——tasks 最后
# insert(0) 确保它压在最前。
for _d in (_SHARED_DIR, _TASKS_DIR):
    _s = str(_d)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)


@pytest.hookimpl(trylast=True)
def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> None:
    """收集本套件测试文件前把 tasks 目录压到 sys.path[0] 并逐出竞争名。

    pytest 收集包内模块（tests.suites.core.*）时 import_path 会把包根
    （tests/）insert(0)，覆盖 conftest 模块级的路径布置；若其他插件套件
    的目录（human/channel_gateway 的 service.py）已驻留 sys.path，pop 后
    ``from service import TaskService`` 会命中错误模块。本钩子 trylast
    （在根 conftest 的裸名逐出之后）执行，保证 tasks 目录恒在 path[0]。
    仅作用于本套件文件，避免扰动其他套件的路径布置。
    """
    if file_path.suffix == ".py" and str(file_path).replace("\\", "/").find("/tests/suites/core/") >= 0:
        _s = str(_TASKS_DIR)
        if _s in sys.path:
            sys.path.remove(_s)
        sys.path.insert(0, _s)
        sys.modules.pop("service", None)
        sys.modules.pop("storage", None)
    return None

import pytest


@pytest.fixture(autouse=True)
def _tasks_path_guard():
    """每个测试执行前锁定 tasks 插件目录为 sys.path[0]，结束后恢复。

    其他插件测试（如 security_check/multimodal）的收集期注入会把各自目录
    推到 sys.path[0] 且不恢复；tasks 测试运行期懒加载 `from service import
    TaskService` / `from storage import TaskStorage` 依赖 sys.path 首位是
    本插件目录。但 tasks 目录驻留会压制 system/workspace/ 的 namespace 包
    （tasks/workspace.py 裸模块优先于包），故用例结束后恢复原路径序并
    逐出竞争名，让后续测试按需重解析。task_types 必须保留——收集期绑定的
    TaskStatus 实例依赖其驻留，重新导入会产生第二份枚举类（== 失败）。
    """
    _saved = sys.path[:]
    _s = str(_TASKS_DIR)
    if sys.path[0] != _s:
        # 先去重再插到最前：重复副本会让"移除一个副本"的测试（如
        # test_migration_batch3 的 workspace 包解压）残留另一副本，继续
        # 压制 namespace 包解析。
        sys.path[:] = [p for p in sys.path if p != _s]
        sys.path.insert(0, _s)
    sys.modules.pop("service", None)
    sys.modules.pop("storage", None)
    yield
    sys.path[:] = _saved
    sys.modules.pop("service", None)
    sys.modules.pop("storage", None)
