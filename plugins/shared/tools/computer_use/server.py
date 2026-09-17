#!/usr/bin/env python3
"""Computer Use 工具 MCP 服务端——接口适配层。

九件桌面操作工具（看屏/截图/点击/键入/滚动/移动拖拽/快捷键/等待/应用窗口），
经 MCP Bridge 网关转发 Windows-MCP 上游。工具实现见 tool.py（单 BuiltinTool
多 action），此处按工具名分发。

声明单源：plugin.json 是 input_schema/description/output_schema/render 的
唯一书写处，tools/list 从 manifest 读取上报——双写漂移会被内核 G2 净化剔除
（community_ops 同款纪律，BUG-5 判例）。读失败让异常传播（sidecar 起不来
→ G2 观测失败 ≠ 判定失败），不做静默回退。

handler 返回 ToolExecutionResult 对象本身（SDK 层 to_dict 整包序列化
success/output/metadata）：截图工具的 multimodal_content 在 metadata 里，
tool_core inject_multimodal 依赖它把截图回传模型——勿改成 ``.output``
（会把 metadata 丢掉，图片注入断链）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("computer_use")


def _manifest_tools() -> dict[str, dict[str, Any]]:
    manifest = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
    return {t["name"]: t for t in manifest["capabilities"]["tools"]}


_MANIFEST_TOOLS = _manifest_tools()

_impl_cls: dict[str, Any] = {}


def _load_computer_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 ComputerUseTool（合宿防遮蔽）。

    合宿平铺 sys.path 下裸名 ``tool`` 被多个成员的同名模块争用（browser/
    web_ext 同款纪律）：加载期临时把本目录置顶 sys.path，结果按类名模块级
    缓存。
    """
    cached = _impl_cls.get("ComputerUseTool")
    if cached is not None:
        return cached
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location("computer_use_tool_impl", os.path.join(here, "tool.py"))
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["computer_use_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    cls = mod.ComputerUseTool
    _impl_cls["ComputerUseTool"] = cls
    return cls


def _make_handler(tool_name: str) -> Any:
    async def _handler(**kwargs: dict[str, Any]) -> Any:
        cls = _load_computer_tool()
        # 返回 ToolExecutionResult 对象（勿取 .output——metadata 承载截图
        # multimodal_content，见模块 docstring）
        return await cls().execute(dict(kwargs, _tool_name=tool_name))

    return _handler


def _register() -> None:
    for name, spec in _MANIFEST_TOOLS.items():
        plugin.tool(
            name=name,
            schema=spec["input_schema"],
            description=spec["description"],
            output_schema=spec.get("output_schema"),
            render=spec.get("render"),
        )(_make_handler(name))


_register()

if __name__ == "__main__":
    plugin.run()
