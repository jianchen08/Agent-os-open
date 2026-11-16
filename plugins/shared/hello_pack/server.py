#!/usr/bin/env python3
"""hello_pack MCP 服务端——最小工具插件（entry "python server.py" 的产物面）。
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

from hello_pack import (
    HELLO_PACK_DESCRIPTION,
    HELLO_PACK_INPUT_SCHEMA,
    HELLO_PACK_OUTPUT_SCHEMA,
    HELLO_PACK_RENDER,
    hello_pack,
)

# 合宿 light 组契约：server.py 必须暴露模块级 plugin 实例（host.py
# getattr(module, "plugin")，缺失即成员装载失败 fail-fast 拖垮整组）；
# 工具注册必须在装载期完成——CohostServer 聚合 plugin._tools，注册留在
# 独立运行入口则合宿下成员零工具。
plugin = AgentOSPlugin("hello_pack")


def create_plugin() -> AgentOSPlugin:
    """注册工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "hello_pack",
        HELLO_PACK_INPUT_SCHEMA,
        hello_pack,
        HELLO_PACK_DESCRIPTION,
        output_schema=HELLO_PACK_OUTPUT_SCHEMA,
        render=HELLO_PACK_RENDER,
    )
    return plugin


# 装载期注册：模块级 plugin 即携带全部工具（对齐 simple/eval_probe 形态；
# register_tool 同名覆写幂等，run() 内再调 create_plugin() 无害）。
create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


TOOL_REGISTRY = {
    "hello_pack": (HELLO_PACK_INPUT_SCHEMA, hello_pack),
}


if __name__ == "__main__":
    run()
