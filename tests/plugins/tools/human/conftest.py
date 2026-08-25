# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""human 插件测试 conftest——把插件目录与父目录注入 sys.path。

插件位于 plugins/shared/tools/human/，内部用包路径导入
（from human.service import ...），父目录 plugins/shared/tools 提供
human 包命名空间；server.py 等装配模块用平铺 import
（from service import ... / from interfaces import ... / from models import ...），
需要插件目录自身。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰（models/server 等在全局碰撞名单内，由共享机制踢缓存；
service/interfaces 不在名单内，故本 conftest 用 autouse fixture 额外清理）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "tools" / "human"
)
_TOOLS_DIR = _PLUGIN_DIR.parent

# 先父目录（human 包命名空间）后插件目录（平铺导入）
_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_TOOLS_DIR)]

for _s in _PLUGIN_SOURCE_DIRS:
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture(autouse=True)
def _evict_human_bare_modules():
    """每个测试前清理 service/interfaces 裸名缓存（不在全局碰撞名单内）。"""
    for _name in ("service", "interfaces"):
        sys.modules.pop(_name, None)
