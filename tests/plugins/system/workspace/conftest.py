"""workspace 测试 conftest——把 workspace 源目录与 system sidecar 父目录注入 sys.path。

server.py 平铺导入同目录 workspace_service/models，跨插件引用
``tasks.service_access`` / ``connectors.registry`` / ``artifacts.artifact_service``
（命名空间包）→ 需 plugins/shared/system/ 在 path 上。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。

_PLUGIN_CONFLICT_DIRS（2026-08-23）：本插件目录是无 __init__.py 的 namespace
包（'workspace'），而 tasks/、isolation/ 目录内有同名裸模块 workspace.py——
PathFinder 规则是普通模块优先于 namespace portion，这两个目录只要还在
sys.path 上（tasks 测试收集/夹具会插入），``from workspace.models import``
就把 'workspace' 槽位解析成裸模块 → 99 ERROR 簇。钩子在每个测试前把它们
摘除（与 tests/channels/conftest.py use_channel 同款纪律）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "workspace"
_SYSTEM_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SYSTEM_DIR)]

# 含同名裸模块 workspace.py 的兄弟目录（tests/plugins/system/tasks 的测试
# 会把 tasks 目录插回 sys.path，故需每测试期持续摘除而非一次性移除）。
_PLUGIN_CONFLICT_DIRS = [str(_SYSTEM_DIR / "tasks"), str(_SYSTEM_DIR / "isolation")]

for _s in _PLUGIN_SOURCE_DIRS:
    if _s not in sys.path:
        sys.path.insert(0, _s)
