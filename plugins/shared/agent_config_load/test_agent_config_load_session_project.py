# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""agent_config_load 会话挂靠解析测试（ADR 2026-10-19-session-workspace-project-binding）。

主会话（无 task.parent_project_id）显式声明工作空间时，state.workspace 对
项目登记簿路径规范化精确匹配 → 注入 state.project_roots——会话选空间随建
即登记（registerSessionProject）后，主 agent 的 {{project_agents_md_anchor}}
才有展开资格。任务挂靠键在场时优先走任务路径，不回落会话匹配。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_session_project_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


def _caller_returning(
    paths: dict[str, str], calls: list[int] | None = None
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def _caller(method: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if calls is not None:
            calls.append(1)
        assert method == "list_paths"
        return {"paths": paths}

    return _caller


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户根与 factory 根都钉到 tmp（agent 配置装载与仓库布局解耦）。"""
    user_root = tmp_path / "user-root"
    factory = tmp_path / "factory"
    user_root.mkdir()
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))


def test_workspace_match_injects_registered_path(tmp_path: Path, isolated_roots: None) -> None:
    """会话工作空间命中登记 → 注入登记路径原文（规范化匹配：盘符大小写/
    分隔符变体同判；注入值 = 登记原文，与任务路径注入同形）。"""
    proj = tmp_path / "demo-proj"
    proj.mkdir()
    registered = str(proj)
    variant = registered.replace("\\", "/")
    if len(variant) >= 2 and variant[1] == ":":
        variant = variant[0].lower() + variant[1:]
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(_caller_returning({"p1": registered}))
    res = _run(plugin.execute(_ctx({"workspace": variant})))
    assert res.state_updates.get("project_roots") == [registered]


def test_workspace_miss_no_injection(tmp_path: Path, isolated_roots: None) -> None:
    """不命中登记 → 不注入（默认会话沙箱与登记路径构造性互斥）。"""
    ws = tmp_path / "sandbox-session"
    ws.mkdir()
    elsewhere = tmp_path / "registered-elsewhere"
    elsewhere.mkdir()
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(_caller_returning({"p1": str(elsewhere)}))
    res = _run(plugin.execute(_ctx({"workspace": str(ws)})))
    assert "project_roots" not in res.state_updates


def test_no_workspace_short_circuits_without_service_call(isolated_roots: None) -> None:
    """未声明工作空间 → 零注入且不打登记服务（聊天热路径默认形态零开销）。"""
    calls: list[int] = []
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(_caller_returning({"p1": "/x"}, calls))
    res = _run(plugin.execute(_ctx({})))
    assert "project_roots" not in res.state_updates
    assert calls == [], "无 workspace 不得发起登记服务调用"


def test_task_key_present_no_session_fallback(tmp_path: Path, isolated_roots: None) -> None:
    """任务挂靠键在场但登记未命中 → 保持降级不注入，不回落会话匹配
    （登记异常显性暴露为 warning，静默换源会掩盖问题）。"""
    proj = tmp_path / "demo-proj"
    proj.mkdir()
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(_caller_returning({}))  # 登记空
    res = _run(
        plugin.execute(
            _ctx({"task.parent_project_id": "proj-x", "workspace": str(proj)})
        )
    )
    assert "project_roots" not in res.state_updates


def test_service_absent_or_error_degrades(tmp_path: Path, isolated_roots: None) -> None:
    """服务未接线 / 调用异常 → 空表降级，不阻断装载。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(None)
    res = _run(plugin.execute(_ctx({"workspace": str(ws)})))
    assert "project_roots" not in res.state_updates

    async def _boom(method: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        raise RuntimeError("service down")

    plugin2 = mod.AgentConfigLoadPlugin(config={})
    plugin2.set_project_service_caller(_boom)
    res2 = _run(plugin2.execute(_ctx({"workspace": str(ws)})))
    assert "project_roots" not in res2.state_updates
