# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""web_ext server.py 接口适配层测试。

覆盖 plugins/shared/tools/web_ext/server.py：
1. 工具注册：web_operate 已注册到 plugin 对象
2. web_operate 工具：成功路径（WebTool 返回成功结果 → output 透传）/
   失败路径（success=False → {"error": ...}）

WebTool 为真实实现，其 httpx 网络依赖以伪客户端打桩（模块级 monkeypatch）。

[来源: 车道实测 web_ext 54.8% → 补测]
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

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """动态加载 server.py（先逐出裸名 plugin/tool，防跨测试劫持）。

    server.py 顶层 `from tool import WebTool` 走 sys.modules 缓存——共跑车
    道里其他插件目录的 tool.py 会占住裸名，不逐出则加载到错误实现。
    """
    mod_name = "web_ext_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules.pop("plugin", None)
    sys.modules.pop("tool", None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self._headers: dict[str, str] = {}

    @property
    def headers(self) -> Any:
        from httpx import Headers

        return Headers(self._headers)

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class _FakeAsyncClient:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._resp


class TestServer:
    def test_tool_registered(self) -> None:
        mod = _load_server()
        assert "web_operate" in mod.plugin._tools
        assert mod.plugin._tools["web_operate"].name == "web_operate"

    def test_src_root_path_injected(self, monkeypatch) -> None:
        """src/ 存在时（0.1 兼容路径）注入 _SRC_ROOT 到 sys.path。"""
        import os

        src_root = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", "..", "..", "..", "src"))
        monkeypatch.setattr(os.path, "isdir", lambda p: p == src_root)
        _load_server()
        assert src_root in sys.path

    def test_web_operate_success(self, monkeypatch) -> None:
        mod = _load_server()
        import tool as tool_mod  # server.py 内 `from tool import WebTool` 解析到的同一模块

        client = _FakeAsyncClient(_FakeResponse(b'{"ok": true}'))
        monkeypatch.setattr(tool_mod.httpx, "AsyncClient", lambda **kw: client)
        monkeypatch.setattr(tool_mod, "resolve_hostname_ips", lambda host: (["93.184.216.34"], None))
        monkeypatch.setattr(tool_mod, "is_private_ip", lambda ip: False)

        out = _run(mod.web_operate(action="get", url="http://example.com/api"))
        assert out["status"] == 200
        assert out["data"] == {"ok": True}

    def test_web_operate_failure(self, monkeypatch) -> None:
        mod = _load_server()
        import tool as tool_mod

        monkeypatch.setattr(tool_mod, "resolve_hostname_ips", lambda host: (None, "无法解析域名: nope"))

        out = _run(mod.web_operate(action="get", url="http://nope.invalid/"))
        assert out == {"error": "URL 安全检查失败: SSRF 防护：域名解析失败: 无法解析域名: nope"}
