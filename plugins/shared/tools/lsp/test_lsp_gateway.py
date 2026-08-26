# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件 LSP 网关） | @ci: python-coverage
"""gateway LSP 网关测试。

覆盖（对齐 plugins/shared/tools/lsp/gateway.py）：
1. LSP_SERVERS / INSTALL_HINTS 静态配置
2. start：幂等启动专用循环线程
3. initialize：启动循环 + IDE 检测
4. ensure_client：缓存命中、失败重试上限、double-check、启动成功
5. _start_client：不支持语言、启动失败、启动成功
6. get_install_hint / get_client / get_supported_languages / get_ide_info
7. go_to_definition / find_references / get_diagnostics / get_completion：
   语言自动检测、客户端缺失降级、结果透传
8. _detect_language：扩展名映射与默认
9. shutdown：停止客户端并清空缓存
10. get_lsp_gateway：单例 + 初始化

LSP 服务器子进程是外部依赖，client.start 用假客户端替身（真实 LSPClient
构造 + monkeypatch start）；专用事件循环为真实实现。
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

_MOD_NAME = "lsp_gateway_under_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _HERE / "gateway.py")
    assert spec is not None and spec.loader is not None, "cannot load gateway.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gw_mod() -> Any:
    return _load_module()


@pytest.fixture(scope="module")
def lsp_types_mod() -> Any:
    import lsp_types  # noqa: PLC0415

    return lsp_types


class TestStaticConfig:
    def test_servers_cover_supported_languages(self, gw_mod: Any) -> None:
        assert set(gw_mod.LSP_SERVERS.keys()) == {"python", "javascript", "typescript", "go", "rust"}

    def test_server_info_fields(self, gw_mod: Any) -> None:
        for lang, info in gw_mod.LSP_SERVERS.items():
            assert info.language == lang
            assert info.command
            assert isinstance(info.args, list)

    def test_install_hints_cover_servers(self, gw_mod: Any) -> None:
        for lang in gw_mod.LSP_SERVERS:
            assert lang in gw_mod.INSTALL_HINTS

    def test_install_hint_unknown(self, gw_mod: Any) -> None:
        hint = gw_mod.INSTALL_HINTS.get("cobol", f"请安装 cobol 语言的 LSP 服务器")
        assert "cobol" in hint


class TestStart:
    def test_start_idempotent(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        try:
            gw.start()
            first_thread = gw._loop_thread
            assert first_thread is not None and first_thread.is_alive()
            gw.start()
            assert gw._loop_thread is first_thread
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            first_thread.join(timeout=5)

    def test_loop_is_daemon(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        try:
            gw.start()
            assert gw._loop_thread is not None
            assert gw._loop_thread.daemon is True
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)


class TestInitialize:
    def test_initialize_detects_ide(self, gw_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = gw_mod.LSPGateway()
        ide = lsp_types_mod.IDEInfo(type=lsp_types_mod.IDEType.VSCODE, name="Code")
        monkeypatch.setattr(gw_mod.IDEDetector, "detect", staticmethod(lambda: ide))
        try:
            asyncio.run(gw.initialize())
            assert gw.ide_info is ide
            assert gw.get_ide_info() is ide
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_initialize_no_ide(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = gw_mod.LSPGateway()
        monkeypatch.setattr(gw_mod.IDEDetector, "detect", staticmethod(lambda: None))
        try:
            asyncio.run(gw.initialize())
            assert gw.ide_info is None
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)


class TestEnsureClient:
    @staticmethod
    def _make_gateway(gw_mod: Any) -> Any:
        gw = gw_mod.LSPGateway()
        gw.start()
        return gw

    def test_cached_client_returned(self, gw_mod: Any, lsp_types_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            client = gw_mod.LSPClient(gw_mod.LSP_SERVERS["python"])
            client.initialized = True
            gw.clients["python"] = client
            got = asyncio.run(gw.ensure_client("python"))
            assert got is client
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_failed_attempts_cap(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            gw._failed_attempts["python"] = 2
            assert asyncio.run(gw.ensure_client("python")) is None
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_start_success_caches(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            async def fake_start(self) -> bool:
                self.initialized = True
                return True

            monkeypatch.setattr(gw_mod.LSPClient, "start", fake_start)
            client = asyncio.run(gw.ensure_client("python"))
            assert client is not None
            assert gw.clients["python"] is client
            assert client.initialized is True
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_start_failure_increments_attempts(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            async def fake_start(self) -> bool:
                return False

            monkeypatch.setattr(gw_mod.LSPClient, "start", fake_start)
            assert asyncio.run(gw.ensure_client("python")) is None
            assert gw._failed_attempts["python"] == 1
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_start_exception_increments_attempts(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            async def fake_start(self) -> bool:
                raise RuntimeError("spawn failed")

            monkeypatch.setattr(gw_mod.LSPClient, "start", fake_start)
            assert asyncio.run(gw.ensure_client("python")) is None
            assert gw._failed_attempts["python"] == 1
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_double_check_prevents_duplicate_start(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # 锁内 double-check：首个检查后、加锁前并发窗口内已缓存 → 直接返回，不重复启动
        gw = self._make_gateway(gw_mod)
        try:
            client = gw_mod.LSPClient(gw_mod.LSP_SERVERS["python"])
            client.initialized = True
            started: list[Any] = []

            async def fake_start(self) -> bool:
                started.append(self)
                return True

            monkeypatch.setattr(gw_mod.LSPClient, "start", fake_start)
            real_lock = asyncio.Lock()

            async def ensure_lock_with_race() -> asyncio.Lock:
                # 模拟并发窗口：另一协程在加锁前已把客户端写入缓存
                gw.clients["python"] = client
                return real_lock

            monkeypatch.setattr(gw, "_ensure_lock", ensure_lock_with_race)
            got = asyncio.run(gw.ensure_client("python"))
            assert got is client
            assert started == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_unsupported_language(self, gw_mod: Any) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            assert asyncio.run(gw.ensure_client("cobol")) is None
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)


class TestClientAccessors:
    def test_get_client(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        client = object()
        gw.clients["python"] = client  # type: ignore[assignment]
        assert gw.get_client("python") is client
        assert gw.get_client("go") is None

    def test_get_supported_languages(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        langs = gw.get_supported_languages()
        assert set(langs) == {"python", "javascript", "typescript", "go", "rust"}

    def test_get_install_hint_known(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        assert "pip install" in gw.get_install_hint("python")

    def test_get_install_hint_unknown(self, gw_mod: Any) -> None:
        gw = gw_mod.LSPGateway()
        hint = gw.get_install_hint("cobol")
        assert "cobol" in hint


class TestDetectLanguage:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("a.py", "python"),
            ("a.js", "javascript"),
            ("a.jsx", "javascript"),
            ("a.ts", "typescript"),
            ("a.tsx", "typescript"),
            ("a.go", "go"),
            ("a.rs", "rust"),
            ("a.txt", "python"),
            ("noext", "python"),
        ],
    )
    def test_mapping(self, gw_mod: Any, path: str, expected: str) -> None:
        gw = gw_mod.LSPGateway()
        assert gw._detect_language(path) == expected


class TestGatewayOperations:
    @staticmethod
    def _make_gateway(gw_mod: Any) -> Any:
        gw = gw_mod.LSPGateway()
        gw.start()
        return gw

    @pytest.fixture(autouse=True)
    def _no_real_spawn(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """no-client 路径会经 _start_client 真实拉起 pylsp 子进程（外部依赖），
        统一 mock 掉 start 避免真实子进程与管道传输泄漏。"""

        async def fake_start(self) -> bool:
            return False

        monkeypatch.setattr(gw_mod.LSPClient, "start", fake_start)

    @staticmethod
    def _fake_client(gw_mod: Any, lsp_types_mod: Any, results: dict[str, Any]) -> Any:
        class FakeClient:
            initialized = True

            async def go_to_definition(self, uri, position):
                return results["definition"]

            async def find_references(self, uri, position):
                return results["references"]

            async def get_diagnostics(self, uri):
                return results["diagnostics"]

            async def get_completion(self, uri, position):
                return results["completion"]

            async def stop(self) -> None:
                return None

        return FakeClient()

    def test_go_to_definition_with_language(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            loc = lsp_types_mod.Location(
                uri="file:///a.py",
                range=lsp_types_mod.Range(
                    start=lsp_types_mod.Position(line=0, character=0),
                    end=lsp_types_mod.Position(line=0, character=1),
                ),
            )
            client = self._fake_client(gw_mod, lsp_types_mod, {"definition": [loc]})
            gw.clients["python"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            locations = asyncio.run(gw.go_to_definition(str(tmp_path / "a.py"), pos, language="python"))
            assert locations == [loc]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_go_to_definition_auto_language(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            client = self._fake_client(gw_mod, lsp_types_mod, {"definition": []})
            gw.clients["rust"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            assert asyncio.run(gw.go_to_definition(str(tmp_path / "a.rs"), pos)) == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_go_to_definition_no_client(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            pos = lsp_types_mod.Position(line=0, character=0)
            assert asyncio.run(gw.go_to_definition(str(tmp_path / "a.py"), pos, language="python")) == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_find_references(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            loc = lsp_types_mod.Location(
                uri="file:///b.py",
                range=lsp_types_mod.Range(
                    start=lsp_types_mod.Position(line=1, character=0),
                    end=lsp_types_mod.Position(line=1, character=1),
                ),
            )
            client = self._fake_client(gw_mod, lsp_types_mod, {"references": [loc]})
            gw.clients["python"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            refs = asyncio.run(gw.find_references(str(tmp_path / "a.py"), pos, language="python"))
            assert refs == [loc]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_find_references_no_client(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            pos = lsp_types_mod.Position(line=0, character=0)
            assert asyncio.run(gw.find_references(str(tmp_path / "a.py"), pos, language="python")) == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_find_references_auto_language(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            loc = lsp_types_mod.Location(
                uri="file:///b.rs",
                range=lsp_types_mod.Range(
                    start=lsp_types_mod.Position(line=0, character=0),
                    end=lsp_types_mod.Position(line=0, character=1),
                ),
            )
            client = self._fake_client(gw_mod, lsp_types_mod, {"references": [loc]})
            gw.clients["rust"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            refs = asyncio.run(gw.find_references(str(tmp_path / "a.rs"), pos))
            assert refs == [loc]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_diagnostics(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            diag = lsp_types_mod.Diagnostic(
                range=lsp_types_mod.Range(
                    start=lsp_types_mod.Position(line=0, character=0),
                    end=lsp_types_mod.Position(line=0, character=1),
                ),
                severity=1,
                message="boom",
            )
            client = self._fake_client(gw_mod, lsp_types_mod, {"diagnostics": [diag]})
            gw.clients["python"] = client  # type: ignore[assignment]
            diags = asyncio.run(gw.get_diagnostics(str(tmp_path / "a.py"), language="python"))
            assert diags == [diag]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_diagnostics_no_client(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            assert asyncio.run(gw.get_diagnostics(str(tmp_path / "a.py"), language="python")) == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_diagnostics_auto_language(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            diag = lsp_types_mod.Diagnostic(
                range=lsp_types_mod.Range(
                    start=lsp_types_mod.Position(line=0, character=0),
                    end=lsp_types_mod.Position(line=0, character=1),
                ),
                severity=1,
                message="boom",
            )
            client = self._fake_client(gw_mod, lsp_types_mod, {"diagnostics": [diag]})
            gw.clients["go"] = client  # type: ignore[assignment]
            diags = asyncio.run(gw.get_diagnostics(str(tmp_path / "a.go")))
            assert diags == [diag]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_completion(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            item = lsp_types_mod.CompletionItem(label="foo", kind=3)
            client = self._fake_client(gw_mod, lsp_types_mod, {"completion": [item]})
            gw.clients["python"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            items = asyncio.run(gw.get_completion(str(tmp_path / "a.py"), pos, language="python"))
            assert items == [item]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_completion_no_client(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            pos = lsp_types_mod.Position(line=0, character=0)
            assert asyncio.run(gw.get_completion(str(tmp_path / "a.py"), pos, language="python")) == []
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_get_completion_auto_language(self, gw_mod: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            item = lsp_types_mod.CompletionItem(label="foo", kind=3)
            client = self._fake_client(gw_mod, lsp_types_mod, {"completion": [item]})
            gw.clients["typescript"] = client  # type: ignore[assignment]
            pos = lsp_types_mod.Position(line=0, character=0)
            items = asyncio.run(gw.get_completion(str(tmp_path / "a.ts"), pos))
            assert items == [item]
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_shutdown_stops_clients(self, gw_mod: Any, lsp_types_mod: Any) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            stopped: list[str] = []

            class FakeClient:
                initialized = True

                async def stop(self) -> None:
                    stopped.append("stopped")

            gw.clients["python"] = FakeClient()  # type: ignore[assignment]
            gw.clients["go"] = FakeClient()  # type: ignore[assignment]
            asyncio.run(gw.shutdown())
            assert stopped == ["stopped", "stopped"]
            assert gw.clients == {}
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)

    def test_shutdown_stop_error_swallowed(self, gw_mod: Any) -> None:
        gw = self._make_gateway(gw_mod)
        try:
            class FakeClient:
                initialized = True

                async def stop(self) -> None:
                    raise RuntimeError("stop failed")

            gw.clients["python"] = FakeClient()  # type: ignore[assignment]
            asyncio.run(gw.shutdown())
            assert gw.clients == {}
        finally:
            gw._loop.call_soon_threadsafe(gw._loop.stop)
            gw._loop_thread.join(timeout=5)


class TestGetLspGateway:
    def test_singleton_and_initialize(self, gw_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        initialized: list[Any] = []

        class FakeGateway:
            def __init__(self) -> None:
                pass

            async def initialize(self) -> None:
                initialized.append(self)

        monkeypatch.setattr(gw_mod, "LSPGateway", FakeGateway)
        monkeypatch.setattr(gw_mod, "_lsp_gateway", None)
        try:
            gw1 = asyncio.run(gw_mod.get_lsp_gateway())
            gw2 = asyncio.run(gw_mod.get_lsp_gateway())
            assert gw1 is gw2
            assert initialized == [gw1]
        finally:
            monkeypatch.setattr(gw_mod, "_lsp_gateway", None)
