# tests/plugins/tools/computer_use 装配：computer_use 插件源目录。
#
# tool.py 平铺裸名 import host_desktop（同目录），spec 动态加载后裸名解析
# 依赖 sys.path 命中本目录。_PLUGIN_SOURCE_DIRS 供 tests/plugins/conftest.py
# 治理钩子逐裸名缓存 + 置前（browser/web_ext 同款纪律）。

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS_ROOT = _REPO_ROOT / "plugins" / "shared" / "tools"
_COMPUTER_DIR = _TOOLS_ROOT / "computer_use"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"

# 按优先级排序（第一个最优先）：computer_use 源目录 > sdk > tools 根。
_PLUGIN_SOURCE_DIRS = [str(_COMPUTER_DIR), str(_SDK_DIR), str(_TOOLS_ROOT)]

for _p in [_COMPUTER_DIR, _SDK_DIR, _TOOLS_ROOT]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
