"""
LSP (Language Server Protocol) 网关服务

提供代码理解能力，支持：
- Definition: 跳转到定义
- References: 查找引用
- Diagnostics: 代码诊断
- Completion: 代码补全
"""

from src.lsp.client import LSPClient
from src.lsp.detector import IDEDetector
from src.lsp.gateway import LSPGateway

__all__ = [
    "LSPGateway",
    "LSPClient",
    "IDEDetector",
]
