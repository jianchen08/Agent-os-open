# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""tool_core server.py 适配层测试——注册面/包装契约/on_load 委托注入。

server.py 是 sidecar 启动面（MCP 服务端入口），CI 插桩基集经本文件获得其
度量面。契约：
- 模块导入即完成 tool_core.execute 工具注册（装饰器在模块级执行）；
- execute 包装契约：core 型 dict 返回包成 ``{"state_updates": <dict>}``；
- on_load 注入两个 capability delegate（tool-executor.invoke / event-bus.emit），
  on_unload 清实例。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_server_module() -> Any:
    """按唯一模块名加载 server.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "tool_core_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestServerAdapter:
    def _server(self) -> Any:
        srv = _load_server_module()
        srv.plugin.get_config = lambda: {}  # type: ignore[method-assign]
        return srv

    def test_module_import_registers_execute_entry(self) -> None:
        """模块导入即完成注册：execute 可调用、插件实例就绪（装饰器模块级执行）。"""
        srv = self._server()
        assert callable(srv.execute)
        assert srv.plugin is not None

    def test_execute_wraps_state_updates_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """core 型 dict 返回包成 {"state_updates": <dict>}（内核反序列化契约）。"""
        srv = self._server()

        class _DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"k": "v"}

        monkeypatch.setattr(srv, "get_instance", lambda: _DictPlugin())
        resp = _run(srv.execute({"messages": []}))
        assert resp == {"state_updates": {"k": "v"}}

    def test_execute_current_call_missing_is_noop(self) -> None:
        """current_call 缺失（非 for-each 上下文误用）→ no-op 空 updates，不抛。"""
        srv = self._server()
        resp = _run(srv.execute({"messages": []}))
        assert resp == {"state_updates": {}}

    def test_on_load_injects_delegates_and_unload_clears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """on_load 注入 tool/event 两委托（行为可观测：invoke 走 tool-executor 桩）；
        on_unload 清实例（下一 get_instance 重建）。"""
        srv = self._server()
        calls: list[tuple[str, dict]] = []

        class _StubCapability:
            def __init__(self, method: str) -> None:
                self._method = method

            async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
                calls.append((f"{self._method}.{method}", params))
                return {"success": True, "data": {"text": "ok"}}

        capabilities: dict[str, Any] = {}

        def _get_capability(name: str) -> Any:
            if name not in capabilities:
                capabilities[name] = _StubCapability(name)
            return capabilities[name]

        monkeypatch.setattr(srv.plugin, "get_capability", _get_capability)
        _run(srv._on_load({}))

        # 委托已注入：execute 走 tool-executor 桩（非 None 委托的失败路径）。
        resp = _run(
            srv.execute(
                {"current_call": {"name": "bash", "id": "c1", "args": {"command": "ls"}}}
            )
        )
        updates = resp["state_updates"]
        invoke_calls = [c for c in calls if c[0] == "tool-executor.invoke"]
        assert invoke_calls, "执行必须经 tool-executor.invoke 委托"
        assert invoke_calls[0][1]["tool_name"] == "bash"
        assert updates["tool_results"][0]["success"] is True

        # on_unload 清实例。
        _run(srv._on_unload({}))
        assert srv._instance is None
