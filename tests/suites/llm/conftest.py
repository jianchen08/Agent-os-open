"""llm 套件 conftest — 0.2 平铺 import 路径。

0.2 架构下 llm 模块位于 plugins/shared/system/llm/，内部用平铺 import。
本 conftest 把该目录加入 sys.path，使 ``from llm.key_pool import`` 等可解析。
同时加入 plugins/shared/（pipeline 兼容 shim 的 namespace 包根）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

for _d in (_LLM_DIR, _SHARED_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)
