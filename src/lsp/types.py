"""
LSP 类型定义

暴露接口：
- LSPErrorCode：LSPErrorCode类
- IDEType：IDEType类
- Position：Position类
- Range：Range类
- Location：Location类
- Diagnostic：Diagnostic类
- CompletionItem：CompletionItem类
- LSPRequest：LSPRequest类
- LSPResponse：LSPResponse类
- LSPServerInfo：LSPServerInfo类
- IDEInfo：IDEInfo类
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LSPErrorCode(int, Enum):
    """LSP 错误码"""

    # 定义在 JSON RPC
    ParseError = -32700
    InvalidRequest = -32600
    MethodNotFound = -32601
    InvalidParams = -32602
    InternalError = -32603

    # 定义在 LSP
    ServerNotInitialized = -32001
    UnknownErrorCode = -32002
    RequestCancelled = -32800
    ContentModified = -32801


class IDEType(str, Enum):
    """IDE 类型"""

    VSCODE = "vscode"
    JETBRAINS = "jetbrains"
    NVIM = "nvim"
    EMACS = "emacs"
    VS = "visual_studio"
    UNKNOWN = "unknown"


class Position(BaseModel):
    """文档中的位置"""

    line: int = Field(..., description="行号（从0开始）")
    character: int = Field(..., description="字符偏移（从0开始）")


class Range(BaseModel):
    """文档中的范围"""

    start: Position = Field(..., description="起始位置")
    end: Position = Field(..., description="结束位置")


class Location(BaseModel):
    """定义或引用的位置"""

    uri: str = Field(..., description="文档 URI")
    range: Range = Field(..., description="范围")


class Diagnostic(BaseModel):
    """诊断信息"""

    range: Range = Field(..., description="诊断范围")
    severity: int = Field(..., description="严重程度：1=Error, 2=Warning, 3=Info, 4=Hint")
    code: str | None = Field(None, description="诊断代码")
    source: str | None = Field(None, description="诊断源（如 'python'）")
    message: str = Field(..., description="诊断消息")


class CompletionItem(BaseModel):
    """代码补全项"""

    label: str = Field(..., description="补全项显示文本")
    kind: int | None = Field(None, description="补全项类型")
    detail: str | None = Field(None, description="补全项详情")
    documentation: str | None = Field(None, description="补全项文档")
    sortText: str | None = Field(None, description="排序文本")  # noqa: N815
    insertText: str | None = Field(None, description="插入文本")  # noqa: N815


class LSPRequest(BaseModel):
    """LSP 请求"""

    id: str | int = Field(..., description="请求 ID")
    method: str = Field(..., description="方法名")
    params: dict[str, Any] | None = Field(None, description="参数")
    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")


class LSPResponse(BaseModel):
    """LSP 响应"""

    id: str | int | None = Field(..., description="请求 ID")
    result: Any | None = Field(None, description="结果")
    error: dict[str, Any] | None = Field(None, description="错误信息")
    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")


class LSPServerInfo(BaseModel):
    """LSP 服务器信息"""

    name: str = Field(..., description="服务器名称")
    version: str | None = Field(None, description="服务器版本")
    language: str = Field(..., description="支持的语言")
    command: str = Field(..., description="启动命令")
    args: list[str] = Field(default_factory=list, description="启动参数")
    env: dict[str, str] | None = Field(None, description="环境变量")


class IDEInfo(BaseModel):
    """IDE 信息"""

    type: IDEType = Field(..., description="IDE 类型")
    name: str = Field(..., description="IDE 名称")
    version: str | None = Field(None, description="IDE 版本")
    port: int | None = Field(None, description="LSP 端口（如果有）")
    workspace: str | None = Field(None, description="工作区路径")
