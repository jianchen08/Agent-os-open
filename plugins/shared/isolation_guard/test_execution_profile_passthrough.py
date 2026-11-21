# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""isolation_guard → isolation.create_env 的 execution_profile 透传测试。

锁点：
- guard 从 execution_context.execution.profile（服务端信任链）读取执行档，
  原样带进 create_env 载荷；LLM 工具参数不参与（此处 ctx.state 即服务端面）；
- 缺省不写键（provider 用注册表缺省 profile）；兼容平铺形态
  execution_context.execution_profile。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _THIS_DIR.parents[0]
_SYSTEM_DIR = _SHARED_DIR / "system"

for _d in (str(_SYSTEM_DIR), str(_SHARED_DIR), str(_THIS_DIR)):
    if _d in sys.path:
        sys.path.remove(_d)
    sys.path.insert(0, _d)
for _bare in ("plugin", "decider"):
    sys.modules.pop(_bare, None)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402

from agentos_plugin_sdk.isolation_types import IsolationLevel  # noqa: E402

pytestmark = pytest.mark.unit


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "isolation_guard_plugin_execution_profile", str(_THIS_DIR / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
IsolationGuard = _mod.IsolationGuard


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    yield
    _mod.set_env_caller(None)


def _guard() -> Any:
    guard = IsolationGuard(
        config={
            "docker_available": False,
            "providers": {"wsl_native": {"enabled": False}, "linux_native": {"enabled": False}},
        }
    )
    guard._decider.resolve = MagicMock()
    return guard


def _capture_caller(captured: list[dict[str, Any]]) -> Any:
    async def _create_env(tool_name: str, args: dict[str, Any]) -> Any:
        captured.append(dict(args))
        return {"success": True, "data": {"env_id": "cua-ws", "status": "ready"}}

    return _create_env


class TestExecutionProfilePassthrough:
    def test_profile_from_execution_context(self, tmp_path: Path) -> None:
        captured: list[dict[str, Any]] = []
        _mod.set_env_caller(_capture_caller(captured))
        ctx = PluginContext(
            state={
                StateKeys.TASK_ID: "t1",
                "execution_context": {"execution": {"profile": "native-win"}},
            },
            config={},
        )
        env_id, backend, error = _run(
            _guard()._get_or_create_container(str(tmp_path), ctx)
        )
        assert error == ""
        assert env_id == "cua-ws"
        assert captured[0]["execution_profile"] == "native-win"
        assert captured[0]["isolation_level"] == "isolated"

    def test_flat_profile_shape_compat(self, tmp_path: Path) -> None:
        captured: list[dict[str, Any]] = []
        _mod.set_env_caller(_capture_caller(captured))
        ctx = PluginContext(
            state={
                StateKeys.TASK_ID: "t1",
                "execution_context": {"execution_profile": "linux-bash"},
            },
            config={},
        )
        _run(_guard()._get_or_create_container(str(tmp_path), ctx))
        assert captured[0]["execution_profile"] == "linux-bash"

    def test_no_profile_omits_key(self, tmp_path: Path) -> None:
        captured: list[dict[str, Any]] = []
        _mod.set_env_caller(_capture_caller(captured))
        ctx = PluginContext(state={StateKeys.TASK_ID: "t1"}, config={})
        _run(_guard()._get_or_create_container(str(tmp_path), ctx))
        assert "execution_profile" not in captured[0]

    def test_execution_profile_helper(self) -> None:
        guard = _guard()
        ctx = PluginContext(
            state={"execution_context": {"execution": {"profile": "native-win"}}}, config={}
        )
        assert guard._execution_profile(ctx) == "native-win"
        assert guard._execution_profile(PluginContext(state={}, config={})) is None


class TestNativeWinHostPath:
    """native-win 档：宿主路径是常态，不是逃逸信号（设计草案 §10）。"""

    @staticmethod
    def _policy() -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(isolation=IsolationLevel.CONTAINER, approval=False)

    def _guard_with_container_policy(self) -> Any:
        guard = IsolationGuard(
            config={
                "docker_available": True,
                "providers": {"wsl_native": {"enabled": False}, "linux_native": {"enabled": False}},
            }
        )
        guard._docker_available = True
        guard._decider.resolve = MagicMock(return_value=self._policy())
        return guard

    def _ctx(self, profile: str | None) -> Any:
        execution_context: dict[str, Any] = {}
        if profile:
            execution_context["execution"] = {"profile": profile}
        return PluginContext(
            state={
                "agent_level": 2,
                "workspace": "C:\\ws",
                "execution_context": execution_context,
            },
            config={},
        )

    def test_native_win_allows_host_path(self) -> None:
        guard = self._guard_with_container_policy()
        context = guard._decide_isolation(
            "bash", self._ctx("native-win"), {"command": "cargo build --manifest-path D:\\ws\\Cargo.toml"}
        )
        assert context.get("blocked") is not True
        assert context["provider"] == "docker"

    def test_linux_bash_still_rejects_host_path(self) -> None:
        guard = self._guard_with_container_policy()
        context = guard._decide_isolation(
            "bash", self._ctx("linux-bash"), {"command": "cat D:\\ws\\a.txt"}
        )
        assert context["blocked"] is True
        assert context["reason"] == "host_path_detected"
