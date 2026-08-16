"""把 isolation 插件目录加入 sys.path（供 tests/ 顶级的 test_isolation_*.py 复用）。

0.2 架构下隔离模块位于 plugins/shared/system/isolation/，内部用平铺 import
（from manager import ...）。本模块由各 isolation 测试在顶部 import 一次以注入路径。

注意：0.1 的 isolation.types 在 0.2 重命名为 isolation_types。
0.1 的 isolation.providers.bwrap_provider 与 isolation._workspace_git_ops /
_workspace_merge_ops 在 0.2 已移除（未迁移），相关测试应删除而非重写。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
_ISOLATION_DIR = _SYSTEM_DIR / "isolation"

# 先加 system/（isolation 命名空间包父目录，patch("isolation.decider.*") 需要），
# 再加 isolation/ 子目录（平铺模块 import 需要）。
for _d in (_SYSTEM_DIR, _ISOLATION_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)
