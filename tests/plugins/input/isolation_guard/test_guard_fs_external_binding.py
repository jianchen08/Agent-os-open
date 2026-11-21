# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""guard fs 族外部绑定路由测试（R11）。

会话存在外部容器绑定时，fs 族工具（file_read/file_write/…）与 bash 同构进
收养容器：ctx 重分类 provider=docker → 落地（create_env 命中绑定）→ 注入
_container_id + _container_workdir（fs 相对路径锚绑定 workdir）。
无绑定 / docker 不可用 / 查询失败 → fs 保持 host（策略缺省行为不变，不回归）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from _libs.pipeline_base.plugin import PluginContext
from _libs.pipeline_base.types import StateKeys

pytestmark = pytest.mark.unit

_GUARD_DIR = Path(__file__).resolve().parents[2] / "isolation_guard"
for _d in (str(_GUARD_DIR),):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import plugin as guard_plugin  # noqa: E402
from plugin import IsolationGuard  # noqa: E402

_WS = str(Path(tempfile.gettempdir()) / "agentos_ig_fs_binding_ws_fixture")


def _make_guard() -> Any:
    guard = IsolationGuard(config={"docker_available": True})

    def _resolve(tool_name: str, category: Any = None) -> Any:
        from unittest.mock import MagicMock

        from agentos_plugin_sdk.isolation_types import IsolationLevel

        mock = MagicMock()
        mock.isolation = (
            IsolationLevel.CONTAINER if tool_name == "bash" else IsolationLevel.HOST
        )
        return mock

    guard._decider.resolve = _resolve
    guard._get_task_metadata = MagicMock(return_value={})
    return guard


def _combined_env_caller(
    calls: list[dict[str, Any]],
    *,
    bound: bool,
    env_id: str = "tb-adopted-container",
    workdir: str = "/app",
    envelope: bool = True,
) -> Any:
    """环境服务假身：同时应答 create_env（落地）与 query_external_binding（判据）。

    envelope=True 按真内核形态包 {success, data} 信封（R11 真跑实证：信封
    解包缺失会把 bound=True 误判为未绑定）；envelope=False 裸载荷（兼容形态）。
    """

    async def _call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append({"tool_name": tool_name, "args": args})
        if tool_name == "isolation.query_external_binding":
            payload = (
                {"bound": bound, "container_name": env_id if bound else "", "workdir": workdir}
                if bound
                else {"bound": False, "container_name": "", "workdir": ""}
            )
            if envelope:
                return {"success": True, "data": payload}
            return payload
        data: dict[str, Any] = {"status": "ready", "env_id": env_id}
        return {"success": True, "data": data}

    return _call


def _base_state(**overrides: Any) -> dict[str, Any]:
    if not Path(_WS).exists():
        Path(_WS).mkdir(parents=True, exist_ok=True)
    base = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "t-r11",
        StateKeys.AGENT_LEVEL: "L2",
        "workspace": _WS,
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: [
            {"name": "bash", "args": {"command": "ls"}},
            {"name": "file_write", "args": {"path": "/app/output/config.json", "content": "{}"}},
        ],
        "tool_output_contracts": {
            "bash": {"schema": {}, "render": None, "isolation_capable": True},
        },
    }
    base.update(overrides)
    return base


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _fs_ctx(execution_contexts: list[dict[str, Any]]) -> dict[str, Any]:
    hits = [c for c in execution_contexts if c.get("tool_name") == "file_write"]
    assert hits, "file_write 执行上下文缺失"
    return hits[0]


class TestFsExternalBindingRouting:
    @pytest.mark.asyncio
    async def test_bound_fs_routes_to_container_with_workdir(self) -> None:
        """有绑定：file_write 重分类 docker + 注入 _container_id/_container_workdir。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        guard_plugin.set_env_caller(_combined_env_caller(calls, bound=True, workdir="/app"))
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "docker"
        assert fs["level"] == "isolated"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        fw = [c for c in injected if c.get("name") == "file_write"]
        assert len(fw) == 1
        assert fw[0]["args"]["_container_id"] == "tb-adopted-container"
        assert fw[0]["args"]["_container_workdir"] == "/app"
        bash = [c for c in injected if c.get("name") == "bash"]
        assert len(bash) == 1
        assert bash[0]["args"]["_container_id"] == "tb-adopted-container"

    @pytest.mark.asyncio
    async def test_bound_fs_routes_bare_payload_form(self) -> None:
        """裸载荷形态（无信封包裹）同样识别（信封两形态兼容回归）。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        guard_plugin.set_env_caller(
            _combined_env_caller(calls, bound=True, envelope=False, workdir="/app")
        )
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "docker"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        fw = [c for c in injected if c.get("name") == "file_write"]
        assert len(fw) == 1
        assert fw[0]["args"]["_container_id"] == "tb-adopted-container"

    @pytest.mark.asyncio
    async def test_no_binding_fs_stays_host(self) -> None:
        """无绑定（不回归对照）：file_write 保持 host、零容器注入；bash 照常。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        guard_plugin.set_env_caller(
            _combined_env_caller(calls, bound=False, env_id="plain-container-abc")
        )
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "host"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        fw = [c for c in injected if c.get("name") == "file_write"]
        bash = [c for c in injected if c.get("name") == "bash"]
        assert len(fw) == 1
        assert "_container_id" not in fw[0]["args"]
        assert len(bash) == 1
        assert bash[0]["args"].get("_container_id") == "plain-container-abc"

    @pytest.mark.asyncio
    async def test_local_probe_false_binding_wins(self) -> None:
        """本地 docker 探测 False + 有绑定：绑定命中仍路由（真内核三连实证：
        3s 探测高负载假阴性，绑定在 bind/收养时已验活，命中即可达性证明；
        落地失败仍 fail-closed blocked，见 test_create_failed_fs_blocked_with_detail）。"""
        guard = IsolationGuard(config={"docker_available": False})
        calls: list[dict[str, Any]] = []
        guard_plugin.set_env_caller(
            _combined_env_caller(calls, bound=True, env_id="bound-container", workdir="/app")
        )
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "docker"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        fw = [c for c in injected if c.get("name") == "file_write"]
        assert len(fw) == 1
        assert fw[0]["args"]["_container_id"] == "bound-container"
        assert fw[0]["args"]["_container_workdir"] == "/app"

    @pytest.mark.asyncio
    async def test_query_failure_fs_stays_host(self) -> None:
        """查询异常（服务缺失）：按未绑定处理（fail-soft），fs 保持 host。"""

        async def _raising_caller(tool_name: str, args: dict[str, Any]) -> Any:
            if tool_name == "isolation.query_external_binding":
                raise RuntimeError("isolation service down")
            data = {"status": "ready", "env_id": "container-abc"}
            return {"success": True, "data": data}

        guard = _make_guard()
        guard_plugin.set_env_caller(_raising_caller)
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "host"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        fw = [c for c in injected if c.get("name") == "file_write"]
        assert len(fw) == 1
        assert "_container_id" not in fw[0]["args"]

    @pytest.mark.asyncio
    async def test_create_failed_fs_blocked_with_detail(self) -> None:
        """落地失败（绑定命中但容器不可达）：fs 与 bash 同构 blocked + 底层错。"""
        guard = _make_guard()

        async def _failing_caller(tool_name: str, args: dict[str, Any]) -> Any:
            if tool_name == "isolation.query_external_binding":
                return {"bound": True, "container_name": "ghost-container", "workdir": "/app"}
            data = {"status": "error", "env_id": "external-ghost-container",
                    "provider_error": "外部容器不可达: No such container"}
            return {"success": True, "data": data}

        guard_plugin.set_env_caller(_failing_caller)
        result = await guard.execute(_ctx(_base_state()))
        fs = _fs_ctx(result.state_updates["execution_contexts"])
        assert fs["provider"] == "denied"
        assert fs["reason"] == "container_create_failed"
        assert "外部容器不可达" in fs.get("create_error", "")
        # 信封 message 语义由 pre_decided_results 承载（原始错随信封出站）
        entries = result.state_updates.get("pre_decided_results") or []
        assert entries, "落地失败必须有预定拒绝结果"
        assert all("外部容器不可达" in str(e.get("error", "")) for e in entries)
