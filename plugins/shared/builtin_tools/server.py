#!/usr/bin/env python3
"""内置工具插件 MCP 服务端启动入口。

内核通过 `python3 server.py` 启动本插件（与其它工具插件 entry 格式一致）。
合宿契约：暴露模块级 plugin 实例（host.py getattr(module, "plugin")），
工具注册在装载期完成；run() 复用同一实例启动 MCP 服务。
"""

from __future__ import annotations

import os
import sys

# 本目录加入 sys.path，使 agentos_builtin_tools 包可被 import（包平铺在插件
# 根，与合宿成员平铺布局一致——合宿装载器同样把本目录置顶 sys.path）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from agentos_builtin_tools.server import create_plugin  # noqa: E402

plugin = create_plugin()


def run() -> None:
    """启动 MCP 服务端（复用模块级 plugin 实例）。"""
    plugin.run()


if __name__ == "__main__":
    run()
