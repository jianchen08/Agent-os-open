#!/usr/bin/env python3
"""简单工具 MCP 服务端——将 10 个简单工具封装为 MCP 服务。

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

from calc_tools import SCIENTIFIC_CALCULATOR_SCHEMA, scientific_calculator
from converter_tools import (
    BINARY_CONVERTER_SCHEMA,
    UNIT_CONVERTER_SCHEMA,
    binary_converter,
    unit_converter,
)
from ide_tools import (
    IDE_GET_SELECTION_SCHEMA,
    IDE_OPEN_FILE_SCHEMA,
    IDE_SHOW_DIFF_SCHEMA,
    ide_get_selection,
    ide_open_file,
    ide_show_diff,
)
from system_tools import (
    READ_EXECUTION_DETAIL_SCHEMA,
    REGISTER_RESOURCE_SCHEMA,
    YAML_VALIDATE_SCHEMA,
    read_execution_detail,
    register_resource,
    yaml_validate,
)
from workflow_tools import (
    COMPATIBILITY_CHECKER_SCHEMA,
    compatibility_checker,
)


def create_plugin() -> AgentOSPlugin:
    """创建包含全部 10 个简单工具的 AgentOSPlugin 实例。"""
    plugin = AgentOSPlugin("simple_tools")

    plugin.register_tool("unit_converter", UNIT_CONVERTER_SCHEMA, unit_converter, "单位换算工具")
    plugin.register_tool(
        "scientific_calculator", SCIENTIFIC_CALCULATOR_SCHEMA, scientific_calculator, "科学计算器"
    )
    plugin.register_tool("yaml_validate", YAML_VALIDATE_SCHEMA, yaml_validate, "YAML 校验")
    plugin.register_tool(
        "binary_converter", BINARY_CONVERTER_SCHEMA, binary_converter, "二进制文件转 Markdown"
    )
    plugin.register_tool(
        "ide_get_selection", IDE_GET_SELECTION_SCHEMA, ide_get_selection, "IDE 选区获取"
    )
    plugin.register_tool("ide_open_file", IDE_OPEN_FILE_SCHEMA, ide_open_file, "IDE 打开文件")
    plugin.register_tool("ide_show_diff", IDE_SHOW_DIFF_SCHEMA, ide_show_diff, "IDE 差异展示")
    plugin.register_tool(
        "compatibility_checker", COMPATIBILITY_CHECKER_SCHEMA, compatibility_checker, "兼容性检查"
    )
    plugin.register_tool(
        "read_execution_detail",
        READ_EXECUTION_DETAIL_SCHEMA,
        read_execution_detail,
        "执行详情读取",
    )
    plugin.register_tool(
        "register_resource", REGISTER_RESOURCE_SCHEMA, register_resource, "资源注册"
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
    "unit_converter": (UNIT_CONVERTER_SCHEMA, unit_converter),
    "scientific_calculator": (SCIENTIFIC_CALCULATOR_SCHEMA, scientific_calculator),
    "yaml_validate": (YAML_VALIDATE_SCHEMA, yaml_validate),
    "binary_converter": (BINARY_CONVERTER_SCHEMA, binary_converter),
    "ide_get_selection": (IDE_GET_SELECTION_SCHEMA, ide_get_selection),
    "ide_open_file": (IDE_OPEN_FILE_SCHEMA, ide_open_file),
    "ide_show_diff": (IDE_SHOW_DIFF_SCHEMA, ide_show_diff),
    "compatibility_checker": (COMPATIBILITY_CHECKER_SCHEMA, compatibility_checker),
    "read_execution_detail": (READ_EXECUTION_DETAIL_SCHEMA, read_execution_detail),
    "register_resource": (REGISTER_RESOURCE_SCHEMA, register_resource),
}


if __name__ == "__main__":
    run()
