"""
VSCode 连接器模块

暴露接口：
- VSCodeConnector: VSCode 连接器实现
- VSCodeChannel: VSCode 消息通道
"""

from connectors.vscode.channel import VSCodeChannel
from connectors.vscode.connector import VSCodeConnector

__all__ = [
    "VSCodeConnector",
    "VSCodeChannel",
]
