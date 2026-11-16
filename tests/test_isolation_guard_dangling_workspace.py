# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
"""IsolationGuard 悬空挂载防护回归测试。

背景（2026-09-27 R295 装机实证）：state/metadata 里的 workspace 指向宿主已
不存在的目录（按陈旧提示词锚定的 dev 路径），_get_or_create_container 未做
存在性预检就把挂载源交给 docker——WSL docker 下 daemon 对不存在的 bind mount
源静默自动创建空目录，命令落空目录却以成功假象通过。

契约：挂载源不存在 → 拒绝创建容器（返回 (None, None)，调用方标 blocked），
环境服务不被触达；挂载源存在 → 经服务正门（isolation.create_env）落地。
"""
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import tests._isolation_path  # noqa: F401  # 注入 isolation 插件目录到 sys.path

_ISOLATION_GUARD_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "isolation_guard",
)
if _ISOLATION_GUARD_DIR in sys.path:
    sys.path.remove(_ISOLATION_GUARD_DIR)
sys.path.insert(0, _ISOLATION_GUARD_DIR)
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

import plugin as plugin_module  # noqa: E402
from plugin import IsolationGuard  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    """每用例后摘除注入的 caller 假身（模块级全局，防跨用例泄漏）。"""
    yield
    plugin_module.set_env_caller(None)


def _make_guard() -> IsolationGuard:
    with patch("decider.IsolationDecider"), \
         patch.object(IsolationGuard, "_detect_docker", return_value=(True, "")):
        return IsolationGuard(config={"providers": {"wsl_native": {"enabled": False}, "linux_native": {"enabled": False}}})


def _make_ctx() -> object:
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state={})


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _spy_caller(calls: list[dict[str, Any]]) -> Any:
    """记录调用且原样返回成功信封的 caller 假身（触达断言用）。"""

    async def _call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append({"tool_name": tool_name, "args": args})
        return {"success": True, "data": {"env_id": "cua-real_ws", "status": "ready"}}

    return _call


def test_dangling_workspace_refuses_without_touching_service(tmp_path: Path) -> None:
    """workspace 指向不存在的目录 → 返回 (None, None)，环境服务零触达（fail-closed）。"""
    guard = _make_guard()
    dangling = tmp_path / "no_such_ws__wt_deadbeef"
    calls: list[dict[str, Any]] = []
    plugin_module.set_env_caller(_spy_caller(calls))

    out = _run(guard._get_or_create_container(str(dangling), _make_ctx()))

    assert out == (None, None)
    assert calls == []


def test_existing_workspace_goes_through_service(tmp_path: Path) -> None:
    """workspace 真实存在 → 经服务正门落地，返回 (env_id, exec_backend)。"""
    guard = _make_guard()
    ws = tmp_path / "real_ws"
    ws.mkdir()
    calls: list[dict[str, Any]] = []
    plugin_module.set_env_caller(_spy_caller(calls))

    env_id, backend = _run(guard._get_or_create_container(str(ws), _make_ctx()))

    assert env_id == "cua-real_ws"
    assert backend is None
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "isolation.create_env"
    assert calls[0]["args"]["workspace"] == str(ws)


def test_none_workspace_short_circuits(tmp_path: Path) -> None:
    """workspace 为空 → 原样 (None, None)（非隔离路径，不触发存在性检查）。"""
    guard = _make_guard()
    calls: list[dict[str, Any]] = []
    plugin_module.set_env_caller(_spy_caller(calls))
    assert _run(guard._get_or_create_container(None, _make_ctx())) == (None, None)
    assert calls == []
