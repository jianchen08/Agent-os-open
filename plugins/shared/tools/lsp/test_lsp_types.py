# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件类型契约） | @ci: python-coverage
"""lsp_types 数据模型契约测试。

覆盖（对齐 plugins/shared/tools/lsp/lsp_types.py）：
1. 枚举：LSPErrorCode 数值、IDEType 字符串值
2. Position/Range/Location 位置模型：构造、默认值、序列化
3. Diagnostic/CompletionItem：可选字段默认值、必填字段校验
4. LSPRequest/LSPResponse：jsonrpc 默认值、可选字段
5. LSPServerInfo/IDEInfo：默认工厂、可选字段
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lsp_types import (
    CompletionItem,
    Diagnostic,
    IDEInfo,
    IDEType,
    LSPErrorCode,
    LSPRequest,
    LSPResponse,
    LSPServerInfo,
    Location,
    Position,
    Range,
)

pytestmark = pytest.mark.unit


class TestLSPErrorCode:
    def test_json_rpc_error_codes(self) -> None:
        assert LSPErrorCode.ParseError == -32700
        assert LSPErrorCode.InvalidRequest == -32600
        assert LSPErrorCode.MethodNotFound == -32601
        assert LSPErrorCode.InvalidParams == -32602
        assert LSPErrorCode.InternalError == -32603

    def test_lsp_error_codes(self) -> None:
        assert LSPErrorCode.ServerNotInitialized == -32001
        assert LSPErrorCode.UnknownErrorCode == -32002
        assert LSPErrorCode.RequestCancelled == -32800
        assert LSPErrorCode.ContentModified == -32801

    def test_error_codes_are_negative(self) -> None:
        # 性质断言：JSON-RPC/LSP 错误码全部为负值
        assert all(code.value < 0 for code in LSPErrorCode)


class TestIDEType:
    def test_values(self) -> None:
        assert IDEType.VSCODE == "vscode"
        assert IDEType.JETBRAINS == "jetbrains"
        assert IDEType.NVIM == "nvim"
        assert IDEType.EMACS == "emacs"
        assert IDEType.VS == "visual_studio"
        assert IDEType.UNKNOWN == "unknown"

    def test_from_value(self) -> None:
        assert IDEType("vscode") is IDEType.VSCODE
        assert IDEType("unknown") is IDEType.UNKNOWN


class TestPosition:
    def test_construct(self) -> None:
        pos = Position(line=3, character=7)
        assert pos.line == 3
        assert pos.character == 7

    def test_zero_based(self) -> None:
        # 性质断言：位置允许 0（LSP 从 0 开始）
        pos = Position(line=0, character=0)
        assert pos.line == 0 and pos.character == 0

    def test_negative_accepted(self) -> None:
        # 现状契约：Position 无 ge=0 约束，负值可构造（LSP 语义由调用方保证）
        pos = Position(line=-1, character=0)
        assert pos.line == -1

    def test_missing_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Position(line=1)  # type: ignore[call-arg]

    def test_dict_roundtrip(self) -> None:
        pos = Position(line=1, character=2)
        assert pos.dict() == {"line": 1, "character": 2}


class TestRange:
    def test_construct(self) -> None:
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=2))
        assert rng.start.line == 0
        assert rng.end.character == 2

    def test_missing_end_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Range(start=Position(line=0, character=0))  # type: ignore[call-arg]


class TestLocation:
    def test_construct(self) -> None:
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=2))
        loc = Location(uri="file:///a.py", range=rng)
        assert loc.uri == "file:///a.py"
        assert loc.range.end.line == 1

    def test_missing_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Location(uri="file:///a.py")  # type: ignore[call-arg]


class TestDiagnostic:
    def test_optional_fields_default(self) -> None:
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=1))
        diag = Diagnostic(range=rng, severity=1, message="boom")
        assert diag.code is None
        assert diag.source is None

    def test_full_fields(self) -> None:
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=1))
        diag = Diagnostic(range=rng, severity=2, code="E123", source="pylsp", message="warn")
        assert diag.code == "E123"
        assert diag.source == "pylsp"

    def test_severity_range(self) -> None:
        # 性质断言：severity 语义 1=Error..4=Hint
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=1))
        for sev in (1, 2, 3, 4):
            assert Diagnostic(range=rng, severity=sev, message="m").severity == sev

    def test_missing_message_rejected(self) -> None:
        rng = Range(start=Position(line=0, character=0), end=Position(line=1, character=1))
        with pytest.raises(ValidationError):
            Diagnostic(range=rng, severity=1)  # type: ignore[call-arg]


class TestCompletionItem:
    def test_optional_fields_default(self) -> None:
        item = CompletionItem(label="foo")
        assert item.kind is None
        assert item.detail is None
        assert item.documentation is None
        assert item.sortText is None
        assert item.insertText is None

    def test_full_fields(self) -> None:
        item = CompletionItem(
            label="foo",
            kind=3,
            detail="def foo()",
            documentation="docs",
            sortText="0001",
            insertText="foo()",
        )
        assert item.kind == 3
        assert item.insertText == "foo()"

    def test_missing_label_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CompletionItem()  # type: ignore[call-arg]


class TestLSPRequest:
    def test_jsonrpc_default(self) -> None:
        req = LSPRequest(id=1, method="initialize", params={})
        assert req.jsonrpc == "2.0"

    def test_params_optional(self) -> None:
        req = LSPRequest(id="abc", method="shutdown")
        assert req.params is None

    def test_id_types(self) -> None:
        assert LSPRequest(id=1, method="m").id == 1
        assert LSPRequest(id="x", method="m").id == "x"


class TestLSPResponse:
    def test_defaults(self) -> None:
        resp = LSPResponse(id=1)
        assert resp.result is None
        assert resp.error is None
        assert resp.jsonrpc == "2.0"

    def test_result_and_error(self) -> None:
        resp = LSPResponse(id=1, result={"a": 1}, error={"code": -32601, "message": "nope"})
        assert resp.result == {"a": 1}
        assert resp.error == {"code": -32601, "message": "nope"}

    def test_id_none_allowed(self) -> None:
        resp = LSPResponse(id=None)
        assert resp.id is None


class TestLSPServerInfo:
    def test_defaults(self) -> None:
        info = LSPServerInfo(name="pylsp", language="python", command="pylsp")
        assert info.args == []
        assert info.version is None
        assert info.env is None

    def test_full_fields(self) -> None:
        info = LSPServerInfo(
            name="gopls",
            version="1.0",
            language="go",
            command="gopls",
            args=["serve"],
            env={"GOPATH": "/tmp/go"},
        )
        assert info.args == ["serve"]
        assert info.env == {"GOPATH": "/tmp/go"}

    def test_missing_command_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LSPServerInfo(name="x", language="y")  # type: ignore[call-arg]


class TestIDEInfo:
    def test_defaults(self) -> None:
        info = IDEInfo(type=IDEType.VSCODE, name="Code")
        assert info.version is None
        assert info.port is None
        assert info.workspace is None

    def test_full_fields(self) -> None:
        info = IDEInfo(type=IDEType.NVIM, name="nvim", version="0.9", port=1234, workspace="/ws")
        assert info.port == 1234
        assert info.workspace == "/ws"

    def test_missing_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IDEInfo(name="x")  # type: ignore[call-arg]
