# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
"""browser server.py `_make_handler` 内层函数体缺口补测（第 139-142 行）。

handler 契约：从模块级缓存加载本目录 BrowserTool 实现 → 构造 → 以
`_tool_name` 注入分发 → ToolExecutionResult 信封归一到 MCP 形态
（成功取 output / 失败包 {"error", "status": 500}）。

打桩边界：Bridge HTTP 面是外部依赖（宿主机 Playwright 网关），以
`sys.modules["browser_tool_impl"]` 上的 BridgeClient 替身注入；其余
（loader、构造、分发表归一）走真实实现。两个工具名做区分输入。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent
BROWSER_DIR = HERE.parents[3] / "plugins" / "shared" / "tools" / "browser"


def _evict_foreign_bare_modules() -> None:
    """逐出指向 browser 目录之外的裸名缓存（tool/bridge_client 为多插件同名裸模块）。"""
    for name in ("tool", "bridge_client"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", None)
        if mod_file and Path(mod_file).resolve() == (BROWSER_DIR / f"{name}.py").resolve():
            continue
        sys.modules.pop(name, None)


def _load_browser_server() -> object:
    """按显式文件路径装载 browser/server.py（裸名 server 会被异成员劫持）。"""
    mod_name = "browser_server_gaps_under_test"
    if mod_name not in sys.modules:
        _evict_foreign_bare_modules()
        here = str(BROWSER_DIR)
        if here not in sys.path:
            sys.path.insert(0, here)
        spec = importlib.util.spec_from_file_location(mod_name, BROWSER_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[mod_name]


@pytest.fixture(scope="module")
def server():
    return _load_browser_server()


class _StubBridgeClient:
    """Bridge 客户端替身（外部网关）：返回固定 MCP result。"""

    response: dict = {}
    last_call: tuple = ()

    def __init__(self, config, session_id="", caller="host") -> None:
        self._config = config

    def call(self, tool_name: str, arguments: dict) -> dict:
        type(self).last_call = (tool_name, arguments)
        return type(self).response


@pytest.fixture()
def stub_bridge(server, monkeypatch):
    """把 browser_tool_impl 的 BridgeClient 换成替身（loader 已装载该模块）。"""
    server._load_browser_tool()  # 确保实现模块进 sys.modules["browser_tool_impl"]
    impl = sys.modules["browser_tool_impl"]
    stub = _StubBridgeClient
    stub.response = {}
    stub.last_call = ()
    monkeypatch.setattr(impl, "BridgeClient", stub)
    return stub


class TestMakeHandlerSuccessBranch:
    """139-141 行：cls 装载 → 构造 → execute → result.output 回传。"""

    @pytest.mark.parametrize(
        ("tool_name", "kwargs", "response", "expected"),
        [
            (
                "browser_navigate",
                {"url": "https://example.com/"},
                {"content": [{"type": "text", "text": "page snapshot text"}]},
                {"url": "https://example.com/", "status": 200},
            ),
            (
                "browser_snapshot",
                {},
                {"content": [{"type": "text", "text": "a11y tree"}]},
                {"snapshot_text": "a11y tree", "status": 200},
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_handler_returns_tool_output(
        self, server, stub_bridge, tool_name, kwargs, response, expected
    ) -> None:
        stub_bridge.response = response
        handler = server.plugin._tools[tool_name].handler

        out = await handler(**kwargs)

        assert expected.items() <= out.items()
        assert out["tool"] == tool_name
        assert stub_bridge.last_call[0] == tool_name
        assert "error" not in out

    @pytest.mark.asyncio
    async def test_handler_constructs_tool_via_cached_loader(self, server, stub_bridge) -> None:
        """cls 来自模块级缓存 loader（139 行真调用），二次取用同一类。"""
        stub_bridge.response = {"content": [{"type": "text", "text": "ok"}]}

        first = server._load_browser_tool()
        second = server._load_browser_tool()

        assert first is second
        handler = server.plugin._tools["browser_snapshot"].handler
        assert (await handler())["status"] == 200


class TestMakeHandlerFailureBranch:
    """142 行失败侧：result.success=False → {"error", "status": 500}。"""

    @pytest.mark.parametrize("tool_name", ["browser_navigate", "browser_snapshot"])
    @pytest.mark.asyncio
    async def test_handler_wraps_failure_envelope(self, server, stub_bridge, tool_name) -> None:
        stub_bridge.response = {
            "isError": True,
            "content": [{"type": "text", "text": "upstream boom"}],
        }
        handler = server.plugin._tools[tool_name].handler

        out = await handler()

        assert out["status"] == 500
        assert out["error"] == "upstream boom"

    @pytest.mark.asyncio
    async def test_unknown_tool_name_via_handler_still_failure(self, server, stub_bridge) -> None:
        """对照：实现面拒绝（未知 action）与上游失败同归一为 error 信封。"""
        stub_bridge.response = {"content": []}
        handler = server._make_handler("browser_hover")

        out = await handler()

        assert out["status"] == 500
        assert "browser_hover" in out["error"]
