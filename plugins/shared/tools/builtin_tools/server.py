#!/usr/bin/env python3
"""内置工具插件 MCP 服务端启动入口。

转发到 src/agentos_builtin_tools/server.py 的 run()。
内核通过 `python3 server.py` 启动本插件（与其它工具插件 entry 格式一致）。
"""

from __future__ import annotations

import os
import sys

# 把 src/ 加入 sys.path，使 agentos_builtin_tools 包可被 import
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agentos_builtin_tools.server import run  # noqa: E402

if __name__ == "__main__":
    run()
