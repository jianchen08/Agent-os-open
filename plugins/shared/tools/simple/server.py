#!/usr/bin/env python3
"""简单工具 MCP 服务端——将 2 个简单工具封装为 MCP 服务。

从 0.1 src/tools/builtin/ 迁移，提取核心逻辑为纯函数注册。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

from system_tools import (
    READ_EXECUTION_DETAIL_SCHEMA,
    YAML_VALIDATE_SCHEMA,
    read_execution_detail,
    yaml_validate,
)


def create_plugin() -> AgentOSPlugin:
    """创建包含全部 2 个简单工具的 AgentOSPlugin 实例。"""
    plugin = AgentOSPlugin("simple_tools")

    plugin.register_tool("yaml_validate", YAML_VALIDATE_SCHEMA, yaml_validate, "YAML 校验")
    plugin.register_tool(
        "read_execution_detail",
        READ_EXECUTION_DETAIL_SCHEMA,
        read_execution_detail,
        "执行详情读取",
    )

    # 注入 capability_caller：read_execution_detail 经 service-registry 读内核轨迹,
    # L1 摘要经 tool-executor 调 hindsight.recall 读压缩块。
    # caller 按 method 前缀路由到对应 capability handle。
    @plugin.on_load
    async def _on_load(params: dict) -> None:
        handles: dict[str, Any] = {}
        for cap in ("service-registry", "tool-executor"):
            try:
                handles[cap] = plugin.get_capability(cap)
            except KeyError:
                logger.warning("[simple_tools] %s 能力未注入", cap)
        if "service-registry" not in handles:
            logger.warning("[simple_tools] service-registry 缺失，read_execution_detail 不可用")
            return

        from system_tools import set_capability_caller  # noqa: PLC0415

        async def _call(method: str, params_dict: dict) -> Any:
            # 按 method 前缀路由(messages.list → service-registry, invoke → tool-executor)
            for cap_name, handle in handles.items():
                prefix = f"{cap_name}."
                if method.startswith(prefix):
                    return await handle.call(method[len(prefix):], params_dict)
            # 无前缀:默认 service-registry
            return await handles["service-registry"].call(method, params_dict)

        set_capability_caller(_call)
        logger.info("[simple_tools] capability_caller 已注入 (caps=%s)", list(handles))

    return plugin


def run() -> None:
    """启动 MCP 服务端。"""
    create_plugin().run()


TOOL_REGISTRY = {
    "yaml_validate": (YAML_VALIDATE_SCHEMA, yaml_validate),
    "read_execution_detail": (READ_EXECUTION_DETAIL_SCHEMA, read_execution_detail),
}


if __name__ == "__main__":
    run()
