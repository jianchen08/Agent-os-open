"""context_window_guard 测试 conftest — 注入插件目录到 sys.path。

插件位于 plugins/shared/pipeline/input/context_window_guard/，其 plugin.py
用平铺 import。本 conftest 把该目录加入 sys.path，使测试可 `from plugin import ...`。
另把 plugins/shared/ 加入 path（pipeline 兼容 shim 需它作为 namespace 包）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "input" / "context_window_guard"
)
_SHARED_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "shared"
)

for _d in (_PLUGIN_DIR, _SHARED_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)
