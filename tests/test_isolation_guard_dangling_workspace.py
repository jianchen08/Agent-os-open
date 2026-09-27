# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
"""IsolationGuard 悬空挂载防护回归测试。

背景（2026-09-27 R295 装机实证）：state/metadata 里的 workspace 指向宿主已
不存在的目录（按陈旧提示词锚定的 dev 路径），_get_or_create_container 未做
存在性预检就把挂载源交给 docker——WSL docker 下 daemon 对不存在的 bind mount
源静默自动创建空目录，命令落空目录却以成功假象通过。

契约：挂载源不存在 → 拒绝创建容器（返回 None，调用方标 blocked），manager
不被触达；挂载源存在 → 正常走 manager。
"""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests._isolation_path  # noqa: F401  # 注入 isolation 插件目录到 sys.path

_ISOLATION_GUARD_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard",
)
if _ISOLATION_GUARD_DIR in sys.path:
    sys.path.remove(_ISOLATION_GUARD_DIR)
sys.path.insert(0, _ISOLATION_GUARD_DIR)
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

import plugin as plugin_module  # noqa: E402
from plugin import IsolationGuard  # noqa: E402

pytestmark = pytest.mark.unit


def _make_guard() -> IsolationGuard:
    with patch("decider.IsolationDecider"), \
         patch.object(IsolationGuard, "_detect_docker", return_value=(True, "")):
        return IsolationGuard(config={"providers": {"wsl_native": {"enabled": False}}})


def _make_ctx() -> object:
    from pipeline.plugin import PluginContext

    return PluginContext(state={})


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_dangling_workspace_refuses_without_touching_manager(tmp_path, monkeypatch):
    """workspace 指向不存在的目录 → 返回 None，manager 零触达（fail-closed）。"""
    guard = _make_guard()
    dangling = tmp_path / "no_such_ws__wt_deadbeef"
    manager = MagicMock()
    manager.get_or_create_environment = AsyncMock()
    monkeypatch.setattr(guard, "_get_manager", lambda: manager)

    out = _run(guard._get_or_create_container(str(dangling), _make_ctx()))

    assert out is None
    manager.get_or_create_environment.assert_not_awaited()


def test_existing_workspace_goes_through_manager(tmp_path, monkeypatch):
    """workspace 真实存在 → 正常请求 manager 创建容器，返回 env_id。"""
    guard = _make_guard()
    ws = tmp_path / "real_ws"
    ws.mkdir()
    env = types.SimpleNamespace(status="READY", env_id="cua-real_ws")
    manager = MagicMock()
    manager.get_or_create_environment = AsyncMock(return_value=env)
    monkeypatch.setattr(guard, "_get_manager", lambda: manager)

    out = _run(guard._get_or_create_container(str(ws), _make_ctx()))

    assert out == "cua-real_ws"
    manager.get_or_create_environment.assert_awaited_once()


def test_none_workspace_short_circuits(tmp_path, monkeypatch):
    """workspace 为空 → 原样 None（非隔离路径，不触发存在性检查）。"""
    guard = _make_guard()
    manager = MagicMock()
    manager.get_or_create_environment = AsyncMock()
    monkeypatch.setattr(guard, "_get_manager", lambda: manager)
    assert _run(guard._get_or_create_container(None, _make_ctx())) is None
    manager.get_or_create_environment.assert_not_awaited()
