# @feature: FP-0.2.二 可观测性 | @vision: V3 可嵌入 | @ci: python-coverage
"""user_admin 测试 conftest——把插件目录注入 sys.path（data 面 p.3 双钩链）。

插件位于 plugins/shared/user_admin/（plugin_type: tool，不在 system/ 下），
server.py 平铺 import，无第三方强依赖（仅 SDK）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "user_admin"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

_s = str(_PLUGIN_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)