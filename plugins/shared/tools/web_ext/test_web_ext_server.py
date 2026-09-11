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

    handler 经 server._load_web_tool() 的 impl 模块（唯一名
    web_operate_tool_impl）取 WebTool——打桩须落在该模块上，
    裸名 ``tool`` 槽位与 handler 已解耦。
    """
    mod_name = "web_ext_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules.pop("plugin", None)
    sys.modules.pop("tool", None)
    sys.modules.pop("web_operate_tool_impl", None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _impl_tool_mod(server: Any) -> Any:
    """handler 实际使用的 tool.py impl 模块（loader 幂等触发后读唯一名注册位）。"""
    server._load_web_tool()
    impl = sys.modules.get("web_operate_tool_impl")
    assert impl is not None, "server loader 未注册 web_operate_tool_impl"
    return impl


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

    def test_no_legacy_src_path_injection(self) -> None:
        """server.py 不注入已退役的 0.1 src/ 目录，插件装载无 sys.path 残留副作用。"""
        import os

        src_root = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", "..", "..", "..", "src"))
        mod = _load_server()
        assert src_root not in sys.path
        assert "web_operate" in mod.plugin._tools

    def test_web_operate_success(self, monkeypatch) -> None:
        mod = _load_server()
        tool_mod = _impl_tool_mod(mod)

        client = _FakeAsyncClient(_FakeResponse(b'{"ok": true}'))
        monkeypatch.setattr(tool_mod.httpx, "AsyncClient", lambda **kw: client)
        monkeypatch.setattr(tool_mod, "resolve_hostname_ips", lambda host: (["93.184.216.34"], None))
        monkeypatch.setattr(tool_mod, "is_private_ip", lambda ip: False)

        out = _run(mod.web_operate(action="get", url="http://example.com/api"))
        assert out["status"] == 200
        assert out["data"] == {"ok": True}

    def test_web_operate_failure(self, monkeypatch) -> None:
        mod = _load_server()
        tool_mod = _impl_tool_mod(mod)

        monkeypatch.setattr(tool_mod, "resolve_hostname_ips", lambda host: (None, "无法解析域名: nope"))

        out = _run(mod.web_operate(action="get", url="http://nope.invalid/"))
        assert out == {"error": "URL 安全检查失败: SSRF 防护：域名解析失败: 无法解析域名: nope"}


class TestCohostShadowing:
    """合宿平铺下裸名 ``tool`` 槽位被异成员占据时，handler 仍取本目录实现。

    邻成员（task_evaluate 等）exec 期绑定后占据 sys.modules["tool"]，静息态
    槽位是异成员模块——server loader 必须按显式路径命中本目录 tool.py。
    decoy 常驻/不常驻两组输入；另断言缓存幂等（二次取用同一类）。
    """

    _IMPL_KEY = "web_operate_tool_impl"

    @staticmethod
    def _plant_decoy() -> Any:
        import types

        decoy = types.ModuleType("tool")

        class _ForeignWebTool:  # 异成员同名类：命中即错
            pass

        decoy.WebTool = _ForeignWebTool
        sys.modules["tool"] = decoy
        return decoy

    @pytest.mark.parametrize("decoy_resident", [True, False])
    def test_loader_resolves_local_impl(self, decoy_resident: bool) -> None:
        saved_tool = sys.modules.get("tool")
        saved_impl = sys.modules.get(self._IMPL_KEY)
        try:
            server = _load_server()
            decoy = self._plant_decoy() if decoy_resident else None

            cls = server._load_web_tool()
            assert cls.__module__ == self._IMPL_KEY
            if decoy is not None:
                assert cls is not decoy.WebTool
            assert hasattr(cls, "execute")
            # 缓存幂等：二次取用同一类（不重复 exec tool.py）
            assert server._load_web_tool() is cls
        finally:
            sys.modules.pop(self._IMPL_KEY, None)
            sys.modules.pop("tool", None)
            for restored in (saved_tool, saved_impl):
                if restored is not None:
                    sys.modules[restored.__name__] = restored
