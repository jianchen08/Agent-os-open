"""tasks 插件测试 conftest——把插件目录注入 sys.path。

插件位于 plugins/shared/system/tasks/，测试运行期懒加载平铺模块
（from storage import ... / import id_utils）。本 conftest 同时暴露
_PLUGIN_SOURCE_DIRS 给收集期（tests/conftest.py pytest_collect_file）与
运行期（tests/plugins/conftest.py pytest_runtest_setup）的统一串扰治理：
同进程多插件组合收集/执行时，workspace 等目录会把本插件目录作为冲突目录
从 sys.path 摘除，本声明保证 tasks 测试期间源目录回到 sys.path 最前。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "tasks"

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

_s = str(_PLUGIN_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
