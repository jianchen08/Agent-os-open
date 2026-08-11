"""外部工具示例连接器。

暴露接口：
- VSCodeConnector：VSCode 扩展连接器
- GodotConnector：Godot 引擎插件连接器
"""

from __future__ import annotations

from tools.external.examples.godot_connector import GodotConnector
from tools.external.examples.vscode_connector import VSCodeConnector

__all__ = [
    "VSCodeConnector",
    "GodotConnector",
]
