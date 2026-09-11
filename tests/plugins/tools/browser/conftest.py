# tests/plugins/tools/browser 装配：browser_tool 插件 + mcp-bridge 网关源目录。
#
# browser 工具平铺 import bridge_client（同目录），bridge_client 测试需 mcp-bridge
# 目录（stub_upstream 复用）。_PLUGIN_SOURCE_DIRS 供 tests/plugins/conftest.py
# 治理钩子逐裸名缓存 + 置前（web_ext/shared 同款纪律）。

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS_ROOT = _REPO_ROOT / "plugins" / "shared" / "tools"
_BROWSER_DIR = _TOOLS_ROOT / "browser"
_BRIDGE_DIR = _REPO_ROOT / "mcp-servers" / "mcp-bridge"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"

# 按优先级排序（第一个最优先）：browser 源目录 > bridge > sdk > tools 根。
_PLUGIN_SOURCE_DIRS = [str(_BROWSER_DIR), str(_BRIDGE_DIR), str(_SDK_DIR), str(_TOOLS_ROOT)]

for _p in [_BROWSER_DIR, _BRIDGE_DIR, _SDK_DIR, _TOOLS_ROOT]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))