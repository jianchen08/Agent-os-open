# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件 LSP 客户端） | @ci: python-coverage
"""client LSP 客户端测试。

覆盖（对齐 plugins/shared/tools/lsp/client.py）：
1. is_server_installed：PATH 命中/未命中
2. start：未安装、启动成功（initialize 握手）、启动异常清理
3. stop：已初始化（shutdown+exit）、未初始化、无进程
4. go_to_definition：单 Location / Location[] / 空 / error 翻译
5. find_references：列表 / 空 / error 翻译 / context 默认值
6. get_diagnostics：items / 空 / error 降级为空列表
7. get_completion：CompletionList / 数组 / 单个 / 空 / error 翻译
8. open_document / change_document：通知帧内容
9. _send_request / _send_notification / _read_message：帧编解码、未连接异常
10. _next_id：递增

LSP 服务器子进程是外部依赖，用内存假流（asyncio.StreamReader/StreamWriter
协议）替代；帧组装/解析、结果翻译、错误翻译走真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_MOD_NAME = "lsp_client_under_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _HERE / "client.py")
    assert spec is not None and spec.loader is not None, "cannot load client.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client_mod() -> Any:
    return _load_module()


@pytest.fixture(scope="module")
def lsp_types_mod() -> Any:
    import lsp_types  # noqa: PLC0415

    return lsp_types


class FakeStreamWriter:
    """内存 StreamWriter 替身：记录写入字节，drain 立即完成。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None


class FakeStreamReader:
    """内存 StreamReader 替身：按队列吐出 header 行与 body 字节。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if not chunk.endswith(b"\r\n"):
            raise AssertionError("fake reader 只接受以 \\r\\n 结尾的 header 行")
        return chunk

    async def read(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        assert len(chunk) == n, f"fake reader 期望 {n} 字节，实际 {len(chunk)}"
        return chunk


class FakeProcess:
    """子进程替身：记录 terminate/wait 调用。"""

    def __init__(self, writer: FakeStreamWriter, reader: FakeStreamReader) -> None:
        self.stdin = writer
        self.stdout = reader
        self.stderr = None
        self.terminated = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> None:
        self.waited = True


def _frame(payload: dict[str, Any]) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return [f"Content-Length: {len(body)}\r\n".encode(), b"\r\n", body]


def _parse_frames(raw: bytes | bytearray) -> list[dict[str, Any]]:
    """按 Content-Length 帧协议解析写入缓冲，返回消息体列表。"""
    text = bytes(raw).decode("utf-8")
    frames: list[dict[str, Any]] = []
    while text:
        header, text = text.split("\r\n\r\n", 1)
        length = int(header.split(":", 1)[1].strip())
        body, text = text[:length], text[length:]
        frames.append(json.loads(body))
    return frames


def _make_client(client_mod: Any, lsp_types_mod: Any, name: str = "pylsp") -> Any:
    info = lsp_types_mod.LSPServerInfo(name=name, language="python", command=name, args=[])
    return client_mod.LSPClient(info)


def _attach_process(client: Any, writer: FakeStreamWriter, reader: FakeStreamReader) -> FakeProcess:
    proc = FakeProcess(writer, reader)
    client.process = proc
    return proc


class TestIsServerInstalled:
    def test_installed(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        assert client.is_server_installed("python") is True

    def test_not_installed(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        assert client.is_server_installed("definitely-not-a-real-binary-xyz") is False


class TestStart:
    def test_server_not_installed(self, client_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        monkeypatch.setattr(client_mod.LSPClient, "is_server_installed", staticmethod(lambda cmd: False))
        assert asyncio.run(client.start()) is False
        assert client.initialized is False

    def test_start_success(self, client_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        monkeypatch.setattr(client_mod.LSPClient, "is_server_installed", staticmethod(lambda cmd: True))
        writer = FakeStreamWriter()
        reader = FakeStreamReader(_frame({"id": 1, "result": {"capabilities": {}}}))

        async def spawn(*a, **kw):
            return _attach_process(client, writer, reader)

        monkeypatch.setattr(client_mod.asyncio, "create_subprocess_exec", spawn)
        assert asyncio.run(client.start()) is True
        assert client.initialized is True
        # 帧序列：initialize 请求 + initialized 通知
        frames = _parse_frames(writer.buffer)
        assert len(frames) == 2
        assert frames[0]["method"] == "initialize"
        assert frames[0]["params"]["capabilities"]["textDocument"]["definition"] == {"dynamicRegistration": True}
        assert frames[1]["method"] == "initialized"
        assert frames[1]["params"] == {}

    def test_start_initialize_error(self, client_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        monkeypatch.setattr(client_mod.LSPClient, "is_server_installed", staticmethod(lambda cmd: True))
        writer = FakeStreamWriter()
        reader = FakeStreamReader(_frame({"id": 1, "error": {"code": -32603, "message": "init failed"}}))

        async def spawn(*a, **kw):
            return _attach_process(client, writer, reader)

        monkeypatch.setattr(client_mod.asyncio, "create_subprocess_exec", spawn)
        assert asyncio.run(client.start()) is False
        assert client.initialized is False
        assert client.process is None

    def test_start_exception_cleans_process(self, client_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        monkeypatch.setattr(client_mod.LSPClient, "is_server_installed", staticmethod(lambda cmd: True))
        writer = FakeStreamWriter()
        reader = FakeStreamReader([])

        async def spawn(*a, **kw):
            return _attach_process(client, writer, reader)

        monkeypatch.setattr(client_mod.asyncio, "create_subprocess_exec", spawn)
        # 让 _initialize 抛异常：stdout 无数据 → _read_message 抛 "连接已关闭"
        assert asyncio.run(client.start()) is False
        assert client.process is None


class TestStop:
    def test_stop_initialized(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        client.initialized = True
        writer = FakeStreamWriter()
        reader = FakeStreamReader(_frame({"id": 2, "result": None}))
        proc = _attach_process(client, writer, reader)
        asyncio.run(client.stop())
        assert client.initialized is False
        assert client.process is None
        assert proc.terminated is True
        assert proc.waited is True
        frames = _parse_frames(writer.buffer)
        assert frames[0]["method"] == "shutdown"
        assert frames[1]["method"] == "exit"

    def test_stop_not_initialized_no_process(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        asyncio.run(client.stop())
        assert client.process is None


class TestGoToDefinition:
    def test_single_location(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {
            "id": 1,
            "result": {"uri": "file:///a.py", "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 5}}},
        }
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        locations = asyncio.run(client.go_to_definition("file:///a.py", pos))
        assert len(locations) == 1
        assert locations[0].uri == "file:///a.py"
        assert locations[0].range.start.line == 1
        assert locations[0].range.end.character == 5

    def test_multiple_locations(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {
            "id": 1,
            "result": [
                {"uri": "file:///a.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
                {"uri": "file:///b.py", "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 1}}},
            ],
        }
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        locations = asyncio.run(client.go_to_definition("file:///a.py", pos))
        assert [loc.uri for loc in locations] == ["file:///a.py", "file:///b.py"]

    def test_empty_result(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": None})))
        pos = lsp_types_mod.Position(line=0, character=0)
        assert asyncio.run(client.go_to_definition("file:///a.py", pos)) == []

    def test_error_raises(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "error": {"code": -32601, "message": "nope"}})))
        pos = lsp_types_mod.Position(line=0, character=0)
        with pytest.raises(Exception, match="获取定义失败"):
            asyncio.run(client.go_to_definition("file:///a.py", pos))

    def test_request_frame_content(self, client_mod: Any, lsp_types_mod: Any) -> None:
        # 性质断言：请求帧携带 uri/position，且 id 递增
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": None})))
        pos = lsp_types_mod.Position(line=7, character=3)
        asyncio.run(client.go_to_definition("file:///x.py", pos))
        frames = _parse_frames(writer.buffer)
        req = frames[0]
        assert req["method"] == "textDocument/definition"
        assert req["params"]["textDocument"] == {"uri": "file:///x.py"}
        assert req["params"]["position"] == {"line": 7, "character": 3}
        assert req["id"] == 1


class TestFindReferences:
    def test_locations(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {
            "id": 1,
            "result": [{"uri": "file:///a.py", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}],
        }
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        locations = asyncio.run(client.find_references("file:///a.py", pos))
        assert len(locations) == 1
        assert locations[0].uri == "file:///a.py"

    def test_empty(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": []})))
        pos = lsp_types_mod.Position(line=0, character=0)
        assert asyncio.run(client.find_references("file:///a.py", pos)) == []

    def test_default_context(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": []})))
        pos = lsp_types_mod.Position(line=0, character=0)
        asyncio.run(client.find_references("file:///a.py", pos))
        req = _parse_frames(writer.buffer)[0]
        assert req["params"]["context"] == {"includeDeclaration": True}

    def test_custom_context(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": []})))
        pos = lsp_types_mod.Position(line=0, character=0)
        asyncio.run(client.find_references("file:///a.py", pos, context={"includeDeclaration": False}))
        req = _parse_frames(writer.buffer)[0]
        assert req["params"]["context"] == {"includeDeclaration": False}

    def test_error_raises(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "error": {"code": -32601, "message": "nope"}})))
        pos = lsp_types_mod.Position(line=0, character=0)
        with pytest.raises(Exception, match="查找引用失败"):
            asyncio.run(client.find_references("file:///a.py", pos))


class TestGetDiagnostics:
    def test_items(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {
            "id": 1,
            "result": {
                "items": [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        "severity": 1,
                        "code": "E1",
                        "source": "pylsp",
                        "message": "undefined name",
                    }
                ]
            },
        }
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        diags = asyncio.run(client.get_diagnostics("file:///a.py"))
        assert len(diags) == 1
        assert diags[0].severity == 1
        assert diags[0].message == "undefined name"

    def test_no_items(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": {"items": []}})))
        assert asyncio.run(client.get_diagnostics("file:///a.py")) == []

    def test_null_result(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": None})))
        assert asyncio.run(client.get_diagnostics("file:///a.py")) == []

    def test_error_degrades_to_empty(self, client_mod: Any, lsp_types_mod: Any) -> None:
        # 诊断方法不支持时返回空列表（现状契约）
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "error": {"code": -32601, "message": "nope"}})))
        assert asyncio.run(client.get_diagnostics("file:///a.py")) == []


class TestGetCompletion:
    def test_completion_list(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {
            "id": 1,
            "result": {
                "isIncomplete": False,
                "items": [{"label": "foo", "kind": 3}, {"label": "bar", "kind": 3}],
            },
        }
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        items = asyncio.run(client.get_completion("file:///a.py", pos))
        assert [i.label for i in items] == ["foo", "bar"]

    def test_array_result(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {"id": 1, "result": [{"label": "x"}]}
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        items = asyncio.run(client.get_completion("file:///a.py", pos))
        assert [i.label for i in items] == ["x"]

    def test_single_item(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        payload = {"id": 1, "result": {"label": "solo"}}
        _attach_process(client, writer, FakeStreamReader(_frame(payload)))
        pos = lsp_types_mod.Position(line=0, character=0)
        items = asyncio.run(client.get_completion("file:///a.py", pos))
        assert [i.label for i in items] == ["solo"]

    def test_empty(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": None})))
        pos = lsp_types_mod.Position(line=0, character=0)
        assert asyncio.run(client.get_completion("file:///a.py", pos)) == []

    def test_error_raises(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "error": {"code": -32601, "message": "nope"}})))
        pos = lsp_types_mod.Position(line=0, character=0)
        with pytest.raises(Exception, match="获取补全失败"):
            asyncio.run(client.get_completion("file:///a.py", pos))


class TestDocumentNotifications:
    def test_open_document(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader([]))
        asyncio.run(client.open_document("file:///a.py", "python", 1, "print(1)"))
        notif = _parse_frames(writer.buffer)[0]
        assert notif["method"] == "textDocument/didOpen"
        assert notif["params"]["textDocument"] == {
            "uri": "file:///a.py",
            "languageId": "python",
            "version": 1,
            "text": "print(1)",
        }

    def test_change_document(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader([]))
        changes = [{"range": None, "text": "x"}]
        asyncio.run(client.change_document("file:///a.py", 2, changes))
        notif = _parse_frames(writer.buffer)[0]
        assert notif["method"] == "textDocument/didChange"
        assert notif["params"]["textDocument"] == {"uri": "file:///a.py", "version": 2}
        assert notif["params"]["contentChanges"] == changes


class TestSendRequestNotification:
    def test_send_request_not_connected(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        req = lsp_types_mod.LSPRequest(id=1, method="initialize", params={})
        with pytest.raises(Exception, match="LSP 服务器未连接"):
            asyncio.run(client._send_request(req))

    def test_send_notification_not_connected(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        with pytest.raises(Exception, match="LSP 服务器未连接"):
            asyncio.run(client._send_notification("initialized", {}))

    def test_read_message_not_connected(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        with pytest.raises(Exception, match="LSP 服务器未连接"):
            asyncio.run(client._read_message())

    def test_read_message_connection_closed(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        _attach_process(client, FakeStreamWriter(), FakeStreamReader([]))
        with pytest.raises(Exception, match="连接已关闭"):
            asyncio.run(client._read_message())

    def test_read_message_empty_body(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        _attach_process(client, FakeStreamWriter(), FakeStreamReader([b"Content-Length: 0\r\n", b"\r\n"]))
        assert asyncio.run(client._read_message()) == ""

    def test_read_message_utf8_body(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        body = json.dumps({"id": 1, "result": "中文"}, ensure_ascii=False).encode("utf-8")
        reader = FakeStreamReader([f"Content-Length: {len(body)}\r\n".encode(), b"\r\n", body])
        _attach_process(client, FakeStreamWriter(), reader)
        assert json.loads(asyncio.run(client._read_message())) == {"id": 1, "result": "中文"}

    def test_send_request_roundtrip(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader(_frame({"id": 1, "result": {"ok": True}})))
        req = lsp_types_mod.LSPRequest(id=1, method="initialize", params={"a": 1})
        resp = asyncio.run(client._send_request(req))
        assert resp.result == {"ok": True}
        assert resp.error is None
        # 帧头 Content-Length 与 body 字节数一致
        raw = bytes(writer.buffer).decode("utf-8")
        header, body = raw.split("\r\n\r\n", 1)
        assert header == f"Content-Length: {len(body.encode('utf-8'))}"

    def test_send_notification_utf8(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        writer = FakeStreamWriter()
        _attach_process(client, writer, FakeStreamReader([]))
        asyncio.run(client._send_notification("initialized", {"k": "值"}))
        raw = bytes(writer.buffer).decode("utf-8")
        header, body = raw.split("\r\n\r\n", 1)
        assert header == f"Content-Length: {len(body.encode('utf-8'))}"
        assert json.loads(body) == {"jsonrpc": "2.0", "method": "initialized", "params": {"k": "值"}}


class TestNextId:
    def test_increments(self, client_mod: Any, lsp_types_mod: Any) -> None:
        client = _make_client(client_mod, lsp_types_mod)
        assert client._next_id() == 1
        assert client._next_id() == 2
        assert client._next_id() == 3
