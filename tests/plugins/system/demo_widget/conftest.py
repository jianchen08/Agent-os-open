"""demo_widget 测试 conftest——注入插件目录与 SDK 到 sys.path。

插件 server.py 顶部 `from agentos_plugin_sdk import AgentOSPlugin`，
故需把 plugins/sdk/src 也加入 path（与 verified PYTHONPATH 一致）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰（server.py 跨插件同名）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system" / "demo_widget"
)
_SDK_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "sdk" / "src"
)

# 插件目录在前（裸名 server 解析到本插件），SDK 在后
_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SDK_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
