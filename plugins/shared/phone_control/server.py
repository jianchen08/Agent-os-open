#!/usr/bin/env python3
"""phone_control MCP 服务端（entry "python server.py" 的产物面）。"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

from phone_control import (
    PHONE_CONTROL_DESCRIPTION,
    PHONE_CONTROL_INPUT_SCHEMA,
    PHONE_CONTROL_OUTPUT_SCHEMA,
    PHONE_CONTROL_RENDER,
    phone_control,
)

# 合宿契约：server.py 必须暴露模块级 plugin 实例且装载期完成工具注册
# （CohostServer 聚合 plugin._tools，注册留在运行入口则合宿成员零工具）。
plugin = AgentOSPlugin("phone_control")


def create_plugin() -> AgentOSPlugin:
    """注册工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "phone_control",
        PHONE_CONTROL_INPUT_SCHEMA,
        phone_control,
        PHONE_CONTROL_DESCRIPTION,
        output_schema=PHONE_CONTROL_OUTPUT_SCHEMA,
        render=PHONE_CONTROL_RENDER,
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
