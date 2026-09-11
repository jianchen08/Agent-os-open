# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""connector.execute 服务测试——按动作类型路由最佳连接器并执行。

workspace 的 IDE 打开面（open_file_in_ide/open_workspace_in_ide）经
tool-executor 正门消费本服务（显式 plugin_id=connectors_service）；
本测试锁服务契约：无连接器 no_connector 标记 / 成功透传 connector_type+data /
业务失败透传 error / manifest services 声明在册。

不依赖真实 IDE 连接器——registry 用伪对象注入（duck-typed）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

# cost_control 平铺模块族与 llm 的 exceptions 同名教训同款：先逐出再载入
_COLLIDING = ("registry", "degradation", "connector_types")


def _load_server() -> Any:
    mod_name = "connectors_server_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    for m in _COLLIDING:
        sys.modules.pop(m, None)
    d = str(_PLUGIN_DIR)
    while d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConnector:
    def __init__(self, connector_type: str, result: Any) -> None:
        self.connector_type = connector_type
        self._result = result
        self.received_action: Any = None

    async def execute_action(self, action: Any) -> Any:
        self.received_action = action
        return self._result


class _FakeRegistry:
    def __init__(self, connector: Any | None) -> None:
        self._connector = connector

    def get_best_connector_for(self, action_type: str) -> Any:
        return self._connector


@pytest.fixture()
def srv() -> Any:
    return _load_server()


def _action_result(srv: Any, success: bool, data: Any = None, error: str | None = None) -> Any:
    return srv.ActionResult(success=success, data=data, error=error)


def test_manifest_declares_connector_execute_service() -> None:
    """plugin.json services 声明 connector.execute（服务缺口补齐的声明面在册锁）。"""
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    names = [s["name"] for s in manifest["capabilities"]["services"]]
    assert "connector.execute" in names


async def test_execute_routes_to_best_connector(srv: Any) -> None:
    """有匹配连接器：构建 ConnectorAction 透传参数，返回 connector_type+data。"""
    conn = _FakeConnector("vscode", _action_result(srv, True, data={"opened": True}))
    srv._registry = _FakeRegistry(conn)
    try:
        resp = await srv.connector_execute("open_file", {"file_path": "/a/b.py"})
    finally:
        srv._registry = None

    assert resp["success"] is True
    assert resp["connector_type"] == "vscode"
    assert resp["data"] == {"opened": True}
    assert isinstance(conn.received_action, srv.ConnectorAction)
    assert conn.received_action.action_type == "open_file"
    assert conn.received_action.parameters == {"file_path": "/a/b.py"}


async def test_execute_parameters_default_empty(srv: Any) -> None:
    """parameters 缺省 → 空 dict（不传 None 给 ConnectorAction）。"""
    conn = _FakeConnector("vscode", _action_result(srv, True))
    srv._registry = _FakeRegistry(conn)
    try:
        await srv.connector_execute("open_folder")
    finally:
        srv._registry = None
    assert conn.received_action.parameters == {}


async def test_execute_business_failure_passes_error(srv: Any) -> None:
    """连接器执行失败：success=False + error 透传（无 no_connector 标记）。"""
    conn = _FakeConnector("vscode", _action_result(srv, False, error="IDE 未就绪"))
    srv._registry = _FakeRegistry(conn)
    try:
        resp = await srv.connector_execute("open_file", {})
    finally:
        srv._registry = None
    assert resp["success"] is False
    assert resp["error"] == "IDE 未就绪"
    assert "no_connector" not in resp


async def test_execute_no_connector_marks_no_connector(srv: Any) -> None:
    """无已连接连接器：no_connector 标记（调用方据此走自身降级路径）。"""
    srv._registry = _FakeRegistry(None)
    try:
        resp = await srv.connector_execute("open_file", {})
    finally:
        srv._registry = None
    assert resp["success"] is False
    assert resp["no_connector"] is True
    assert "error" in resp


async def test_execute_service_uninitialized(srv: Any) -> None:
    """服务未初始化（on_load 前）：success=False + 服务未初始化。"""
    saved = srv._registry
    srv._registry = None
    try:
        resp = await srv.connector_execute("open_file", {})
    finally:
        srv._registry = saved
    assert resp["success"] is False
    assert "服务未初始化" in resp["error"]
