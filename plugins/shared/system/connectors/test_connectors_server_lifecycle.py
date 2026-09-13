# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""connectors server.py 服务面分支测试（与 test_connector_execute.py 互补：
那里锁 connector.execute 路由契约，这里锁其余工具面与生命周期）：

- 未初始化守卫：各工具在 on_load 前调用一律 success=False + "服务未初始化"
- register：内置 vscode 真实实例化成功 / 不支持类型拒绝
- unregister：存在注销 / 不存在 KeyError → 显式错误
- list / get_active / get_status：空表、注册未连接、伪已连接连接器注入
- get_adapter_status：真实摘要 + 异常分支显式报错
- degrade：未初始化守卫 + 真实 DegradationManager 降级执行
- on_unload：清理后回到未初始化态

不依赖真实 IDE——路由用伪连接器（duck-typed）注入真 ConnectorRegistry。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_COLLIDING = ("registry", "degradation", "connector_types")


def _load_server() -> Any:
    mod_name = "connectors_server_lifecycle_ut_v2"
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


class _FakeConnectedConnector:
    """已连接的伪连接器：可注册进真 registry 驱动路由/状态面。"""

    def __init__(self, connector_type: str = "fakeide", capabilities: list[str] | None = None) -> None:
        from connector_types import ConnectorInfo

        self.connector_type = connector_type
        self._info = ConnectorInfo(
            connector_type=connector_type,
            display_name=f"伪 {connector_type}",
            capabilities=capabilities or ["open_file"],
            priority=10,
        )

    @property
    def is_connected(self) -> bool:
        return True

    def get_info(self) -> Any:
        return self._info

    def get_status(self) -> dict[str, Any]:
        return {"connector_type": self.connector_type, "state": "connected"}

    async def execute_action(self, action: Any) -> Any:
        from connector_types import ActionResult

        return ActionResult(success=True, data={"echo": action.action_type})


@pytest.fixture()
def srv() -> Any:
    module = _load_server()
    yield module
    # 每用例回到未初始化态，互不串状态
    import asyncio

    asyncio.run(module._on_unload({}))


async def test_all_tools_guard_uninitialized(srv: Any) -> None:
    """on_load 之前各工具一律服务未初始化（get_adapter_status 无 registry 依赖除外）。"""
    assert await srv.connector_register("vscode") == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_unregister("vscode") == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_list() == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_get_active() == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_get_status() == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_execute("open_file") == {"success": False, "error": "服务未初始化"}
    assert await srv.connector_degrade("open_file") == {"success": False, "error": "服务未初始化"}


async def test_on_load_initializes_and_register_vscode(srv: Any) -> None:
    """on_load 就绪后注册内置 vscode 连接器成功。"""
    await srv._on_load({})
    result = await srv.connector_register("vscode")
    assert result == {"success": True, "connector_type": "vscode"}
    listing = await srv.connector_list()
    assert listing["success"] is True
    assert listing["count"] == 1
    assert listing["connectors"][0]["connector_type"] == "vscode"


async def test_register_unsupported_type_rejected(srv: Any) -> None:
    await srv._on_load({})
    result = await srv.connector_register("jetbrains")
    assert result["success"] is False
    assert "不支持的连接器类型" in result["error"]


async def test_unregister_missing_connector_keyerror_branch(srv: Any) -> None:
    await srv._on_load({})
    result = await srv.connector_unregister("no-such")
    assert result == {"success": False, "error": "连接器不存在: no-such"}

    await srv.connector_register("vscode")
    assert await srv.connector_unregister("vscode") == {"success": True}
    listing = await srv.connector_list()
    assert listing["count"] == 0


async def test_get_active_none_when_registered_but_not_connected(srv: Any) -> None:
    await srv._on_load({})
    await srv.connector_register("vscode")
    # 真实 VSCodeConnector 未连接 → 无活跃连接器
    result = await srv.connector_get_active()
    assert result == {"success": True, "connector": None}


async def test_get_active_returns_status_of_connected(srv: Any) -> None:
    await srv._on_load({})
    fake = _FakeConnectedConnector()
    srv._registry.register(fake)
    result = await srv.connector_get_active()
    assert result["success"] is True
    assert result["connector"] == {"connector_type": "fakeide", "state": "connected"}


async def test_get_status_single_and_missing(srv: Any) -> None:
    await srv._on_load({})
    missing = await srv.connector_get_status("ghost")
    assert missing == {"success": False, "error": "连接器不存在: ghost"}

    srv._registry.register(_FakeConnectedConnector())
    single = await srv.connector_get_status("fakeide")
    assert single == {"success": True, "status": {"connector_type": "fakeide", "state": "connected"}}


async def test_get_status_all_traverses_public_api(srv: Any) -> None:
    await srv._on_load({})
    srv._registry.register(_FakeConnectedConnector("a"))
    srv._registry.register(_FakeConnectedConnector("b"))
    result = await srv.connector_get_status()
    assert result["success"] is True
    assert result["count"] == 2
    assert {s["connector_type"] for s in result["statuses"]} == {"a", "b"}


async def test_get_adapter_status_ok_and_exception_branch(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # 摘要读取依赖运行环境配置（config_center），桩为固定返回锁工具面信封
    monkeypatch.setattr(srv, "get_adapter_status_summary", lambda: [{"id": "dsh", "status": "ok"}])
    ok = await srv.connector_get_adapter_status()
    assert ok == {"success": True, "adapters": [{"id": "dsh", "status": "ok"}]}

    def _boom() -> Any:
        raise RuntimeError("配置读取失败")

    monkeypatch.setattr(srv, "get_adapter_status_summary", _boom)
    failed = await srv.connector_get_adapter_status()
    assert failed == {"success": False, "error": "配置读取失败"}


async def test_degrade_runs_real_fallback(srv: Any) -> None:
    await srv._on_load({})
    result = await srv.connector_degrade("open_file", {"path": "/tmp/x.txt"})
    # DegradationManager 真实降级执行：不抛出且返回 ActionResult 形状
    assert set(result.keys()) == {"success", "data", "error"}


async def test_on_unload_resets_to_uninitialized(srv: Any) -> None:
    await srv._on_load({})
    await srv.connector_register("vscode")
    await srv._on_unload({})
    assert await srv.connector_list() == {"success": False, "error": "服务未初始化"}
    assert srv._registry is None
    assert srv._degradation is None
