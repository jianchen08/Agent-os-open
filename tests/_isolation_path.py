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
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_DIR = _SHARED_DIR / "system"
_ISOLATION_DIR = _SYSTEM_DIR / "isolation"
# isolation 插件源目录（平铺 module 直取目录名是 isolation_guard，测试引用
# pipeline/input/isolation_guard/ 下的 plugin.py）
_ISOLATION_GUARD_DIR = _SHARED_DIR / "pipeline" / "input" / "isolation_guard"

# 先加 plugins/shared/（pipeline namespace 包父目录，from pipeline.plugin 需要），
# 再加 system/（isolation 命名空间包父目录，patch("isolation.decider.*") 需要），
# 最后加 isolation_guard/ 子目录（平铺模块 import 需要）。
# 置顶 + 裸名逐出：先导的测试（add_plugin_dir 系如 security_check）会把自家目录
# 钉在 sys.path[0] 并缓存 plugin 模块，本目录不置顶不逐出则重 import 被劫持
# （实测 security_check/plugin.py 抢走 `plugin` 名 → ImportError: IsolationGuard）。
for _d in (_SHARED_DIR, _SYSTEM_DIR, _ISOLATION_DIR, _ISOLATION_GUARD_DIR):
    _s = str(_d)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)
