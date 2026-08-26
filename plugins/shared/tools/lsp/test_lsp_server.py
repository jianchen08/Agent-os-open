# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件 MCP 服务端适配层） | @ci: python-coverage
"""server MCP 服务端适配层测试。

覆盖（对齐 plugins/shared/tools/lsp/server.py）：
1. 模块装配：4 个工具注册（名称/schema/描述）
2. _on_load：经 get_lsp_gateway 装配全局网关
3. _on_unload：shutdown 并清空网关 / 未初始化时 no-op
4. lsp_definition / lsp_references / lsp_diagnostics：网关未初始化错误、
   结果序列化（locations/references/diagnostics + count）
5. lsp_jump_to_file：FileJumpProtocol 调用与结果回传、位置参数
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_MOD_NAME = "lsp_server_under_test"


def _load_server() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _HERE / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server_mod() -> Any:
    return _load_server()


@pytest.fixture(scope="module")
def lsp_types_mod() -> Any:
    import lsp_types  # noqa: PLC0415

    return lsp_types


class TestModuleAssembly:
    def test_four_tools_registered(self, server_mod: Any) -> None:
        names = set(server_mod.plugin._tools.keys())
        assert names == {"lsp.definition", "lsp.references", "lsp.diagnostics", "lsp.jump_to_file"}

    def test_definition_schema(self, server_mod: Any) -> None:
        tool = server_mod.plugin._tools["lsp.definition"]
        assert tool.schema["required"] == ["file_path", "line"]
        assert tool.schema["properties"]["character"]["default"] == 0

    def test_jump_schema_defaults(self, server_mod: Any) -> None:
        tool = server_mod.plugin._tools["lsp.jump_to_file"]
        assert tool.schema["required"] == ["file_path"]
        assert tool.schema["properties"]["line"]["default"] == 0
        assert tool.schema["properties"]["character"]["default"] == 0

    def test_diagnostics_schema(self, server_mod: Any) -> None:
        tool = server_mod.plugin._tools["lsp.diagnostics"]
        assert tool.schema["required"] == ["file_path"]


class TestOnLoad:
    def test_on_load_assigns_gateway(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_gateway = object()

        async def fake_get() -> Any:
            return fake_gateway

        monkeypatch.setattr(server_mod, "get_lsp_gateway", fake_get)
        try:
            asyncio.run(server_mod._on_load({}))
            assert server_mod._gateway is fake_gateway
        finally:
            server_mod._gateway = None


class TestOnUnload:
    def test_on_unload_shuts_down(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        shutdown_calls: list[Any] = []

        class FakeGateway:
            async def shutdown(self) -> None:
                shutdown_calls.append(self)

        fake = FakeGateway()
        monkeypatch.setattr(server_mod, "_gateway", fake)
        asyncio.run(server_mod._on_unload({}))
        assert shutdown_calls == [fake]
        assert server_mod._gateway is None

    def test_on_unload_no_gateway(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod, "_gateway", None)
        asyncio.run(server_mod._on_unload({}))
        assert server_mod._gateway is None


class TestDefinitionTool:
    def test_gateway_not_initialized(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod, "_gateway", None)
        result = asyncio.run(server_mod.lsp_definition("a.py", 1))
        assert result == {"error": "LSP gateway not initialized"}

    def test_serializes_locations(self, server_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        loc = lsp_types_mod.Location(
            uri="file:///a.py",
            range=lsp_types_mod.Range(
                start=lsp_types_mod.Position(line=1, character=2),
                end=lsp_types_mod.Position(line=3, character=4),
            ),
        )

        class FakeGateway:
            async def go_to_definition(self, file_path, position, language):
                return [loc]

        monkeypatch.setattr(server_mod, "_gateway", FakeGateway())
        result = asyncio.run(server_mod.lsp_definition("a.py", 1, character=2, language="python"))
        assert result["count"] == 1
        assert result["locations"][0]["uri"] == "file:///a.py"
        assert result["locations"][0]["range"]["start"] == {"line": 1, "character": 2}
        assert result["locations"][0]["range"]["end"] == {"line": 3, "character": 4}

    def test_empty_locations(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeGateway:
            async def go_to_definition(self, file_path, position, language):
                return []

        monkeypatch.setattr(server_mod, "_gateway", FakeGateway())
        result = asyncio.run(server_mod.lsp_definition("a.py", 0))
        assert result == {"locations": [], "count": 0}


class TestReferencesTool:
    def test_gateway_not_initialized(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod, "_gateway", None)
        result = asyncio.run(server_mod.lsp_references("a.py", 1))
        assert result == {"error": "LSP gateway not initialized"}

    def test_serializes_references(self, server_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        loc = lsp_types_mod.Location(
            uri="file:///b.py",
            range=lsp_types_mod.Range(
                start=lsp_types_mod.Position(line=0, character=0),
                end=lsp_types_mod.Position(line=0, character=1),
            ),
        )

        class FakeGateway:
            async def find_references(self, file_path, position, language):
                return [loc]

        monkeypatch.setattr(server_mod, "_gateway", FakeGateway())
        result = asyncio.run(server_mod.lsp_references("a.py", 0, language="python"))
        assert result["count"] == 1
        assert result["references"][0]["uri"] == "file:///b.py"


class TestDiagnosticsTool:
    def test_gateway_not_initialized(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod, "_gateway", None)
        result = asyncio.run(server_mod.lsp_diagnostics("a.py"))
        assert result == {"error": "LSP gateway not initialized"}

    def test_serializes_diagnostics(self, server_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        diag = lsp_types_mod.Diagnostic(
            range=lsp_types_mod.Range(
                start=lsp_types_mod.Position(line=0, character=0),
                end=lsp_types_mod.Position(line=0, character=5),
            ),
            severity=2,
            code="W001",
            source="pylsp",
            message="unused import",
        )

        class FakeGateway:
            async def get_diagnostics(self, file_path, language):
                return [diag]

        monkeypatch.setattr(server_mod, "_gateway", FakeGateway())
        result = asyncio.run(server_mod.lsp_diagnostics("a.py", language="python"))
        assert result["count"] == 1
        d = result["diagnostics"][0]
        assert d["severity"] == 2
        assert d["code"] == "W001"
        assert d["source"] == "pylsp"
        assert d["message"] == "unused import"
        assert d["range"]["start"] == {"line": 0, "character": 0}

    def test_empty_diagnostics(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeGateway:
            async def get_diagnostics(self, file_path, language):
                return []

        monkeypatch.setattr(server_mod, "_gateway", FakeGateway())
        result = asyncio.run(server_mod.lsp_diagnostics("a.py"))
        assert result == {"diagnostics": [], "count": 0}


class TestJumpToFileTool:
    def test_jump_success_with_position(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, ...]] = []

        class FakeProtocol:
            @staticmethod
            async def jump_to_file(file_path, position):
                calls.append((file_path, position))
                return True

        fake_mod = type("FakeFileJump", (), {"FileJumpProtocol": FakeProtocol})
        monkeypatch.setitem(sys.modules, "file_jump", fake_mod)
        try:
            result = asyncio.run(server_mod.lsp_jump_to_file("a.py", line=2, character=3))
            assert result == {"success": True, "file_path": "a.py", "line": 2, "character": 3}
            assert len(calls) == 1
            file_path, position = calls[0]
            assert file_path == "a.py"
            assert position is not None
            assert position.line == 2
            assert position.character == 3
        finally:
            monkeypatch.delitem(sys.modules, "file_jump", raising=False)

    def test_jump_zero_position_passes_none(self, server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, ...]] = []

        class FakeProtocol:
            @staticmethod
            async def jump_to_file(file_path, position):
                calls.append((file_path, position))
                return False

        fake_mod = type("FakeFileJump", (), {"FileJumpProtocol": FakeProtocol})
        monkeypatch.setitem(sys.modules, "file_jump", fake_mod)
        try:
            result = asyncio.run(server_mod.lsp_jump_to_file("a.py"))
            assert result["success"] is False
            assert calls[0][1] is None
        finally:
            monkeypatch.delitem(sys.modules, "file_jump", raising=False)
