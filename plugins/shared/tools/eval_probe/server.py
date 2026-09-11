#!/usr/bin/env python3
"""评测探针 MCP 服务端——eval_sleep / eval_echo 两个故意失败 case 载体工具。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

from eval_tools import (
    EVAL_BROKEN_SCHEMA,
    EVAL_ECHO_SCHEMA,
    EVAL_SLEEP_SCHEMA,
    eval_broken,
    eval_echo,
    eval_sleep,
)

# 合宿 light 组契约：server.py 必须暴露模块级 plugin 实例（host.py
# getattr(module, "plugin")，缺失即成员装载失败 fail-fast 拖垮整组）
plugin = AgentOSPlugin("eval_probe")


def create_plugin() -> AgentOSPlugin:
    """注册工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool("eval_sleep", EVAL_SLEEP_SCHEMA, eval_sleep, "评测探针·睡眠")
    plugin.register_tool("eval_echo", EVAL_ECHO_SCHEMA, eval_echo, "评测探针·回显")
    plugin.register_tool("eval_broken", EVAL_BROKEN_SCHEMA, eval_broken, "评测探针·必失败")
    return plugin


# 导入即注册：模块级 plugin 实例（合宿契约）必须在导入后即携带工具注册——
# 与 e2e_lifecycle_probe 等模块级 @plugin.tool 形态可见性等价（冒烟矩阵/
# 合宿装载都按「模块级 plugin 的已注册工具」枚举）。register_tool 同名
# 覆写幂等，run() 内再调 create_plugin() 无害。
create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    create_plugin().run()


TOOL_REGISTRY: dict[str, tuple[dict[str, Any], Any]] = {
    "eval_sleep": (EVAL_SLEEP_SCHEMA, eval_sleep),
    "eval_echo": (EVAL_ECHO_SCHEMA, eval_echo),
    "eval_broken": (EVAL_BROKEN_SCHEMA, eval_broken),
}


if __name__ == "__main__":
    run()
