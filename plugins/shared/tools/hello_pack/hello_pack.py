"""hello_pack 最小工具插件实现。

本插件声明工具 hello_pack：
- 无入参（input_schema.properties 为空对象）
- 返回固定结构 {"message": "hello pack"}

plugin.json 中 capabilities.tools[0] 即为本工具的声明契约；本文件提供
对应的核心实现与 schema 常量（server.py 经 MCP 面注册上报）。
"""

from __future__ import annotations

from typing import Any, Dict

# 工具名固定常量，对应 plugin.json capabilities.tools[0].name
TOOL_NAME = "hello_pack"

# 入参 schema：与 plugin.json capabilities.tools[0].input_schema 逐字一致
# （G2 声明↔实现对照按 input_schema 严格比对，漂移即剔除）
HELLO_PACK_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
}

# 出参 schema：与 plugin.json capabilities.tools[0].output_schema 一致
HELLO_PACK_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["message"],
    "properties": {
        "message": {
            "type": "string",
            "description": "固定返回 hello pack 消息",
        }
    },
}

# 渲染意图：与 plugin.json capabilities.tools[0].render 一致
HELLO_PACK_RENDER: Dict[str, Any] = {
    "card": "form",
    "title": "Hello Pack",
}

HELLO_PACK_DESCRIPTION = (
    '最小工具 hello_pack：无需任何入参，返回 {"message":"hello pack"}。'
)


def hello_pack(_args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """最小工具 hello_pack 的实现入口。

    参数：
        _args：调用入参。本工具无入参，按约定忽略（传入 None 或 {} 均合法）。

    返回：
        固定结构 ``{"message": "hello pack"}``，与 plugin.json 中
        output_schema 声明一致。
    """
    return {"message": "hello pack"}


# 工具注册表：name -> callable，便于插件系统按 name 分发
TOOL_REGISTRY: Dict[str, Any] = {
    TOOL_NAME: hello_pack,
}


def dispatch(tool_name: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """按工具名分发的统一入口。

    参数：
        tool_name：工具名称（如 "hello_pack"）。
        args：调用入参。

    返回：
        工具执行的返回结果。

    抛出：
        KeyError：当 tool_name 未在 TOOL_REGISTRY 中注册时。
    """
    handler = TOOL_REGISTRY[tool_name]
    return handler(args)


if __name__ == "__main__":
    # 直接运行本文件的最小冒烟测试：打印 hello_pack 的固定返回。
    print(hello_pack())
