"""multimodal 测试 conftest — 0.2 平铺 import 路径。

0.2 架构下多模态模块位于 plugins/shared/system/multimodal/，内部用平铺 import
（from mm_types import ...）。本 conftest 把该目录加入 sys.path。

注意：0.1 的 multimodal.types 在 0.2 重命名为 multimodal.mm_types（模块文件名也变了）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MULTIMODAL_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "multimodal"

_s = str(_MULTIMODAL_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
