"""把 channel_api 插件目录加入 sys.path（供 tests/ 顶级的 channel_api 相关测试复用）。

0.2 架构下 channel_api 模块位于 plugins/shared/system/channel_api/，内部用平铺 import
（from memory_store import ...）。本模块由各测试在顶部 import 一次以注入路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANNEL_API_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "channel_api"

_s = str(_CHANNEL_API_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
