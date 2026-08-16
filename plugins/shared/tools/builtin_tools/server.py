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

# 工具共享层根目录（url_security / workspace_aware 平铺模块所在处 = ../，即
# plugins/shared/tools/），供包内模块以 web_ext 同款平铺方式导入（B4/B5 接入
# 共享层校验）。注意 dirname(__file__) 只到本插件目录，共享层在其父目录。
_TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_ROOT not in sys.path:
    sys.path.insert(0, _TOOLS_ROOT)

# bash 插件目录（C17）：bash_tool.py 以命名空间包导入 bash.tool 的
# DANGEROUS_PATTERNS（单一事实源）；bash.tool 的平铺依赖（bash_types 等）
# 需要本目录在 sys.path。**append 而非 insert(0)**：bash/ 内有旧版
# workspace_aware.py（无 check_path_allowed），若置于 tools 共享层根目录之前，
# 会遮蔽 canonical 副本并污染同进程其它插件的导入。
_BASH_DIR = os.path.join(_TOOLS_ROOT, "bash")
if os.path.isdir(_BASH_DIR) and _BASH_DIR not in sys.path:
    sys.path.append(_BASH_DIR)

from agentos_builtin_tools.server import run  # noqa: E402

if __name__ == "__main__":
    run()
