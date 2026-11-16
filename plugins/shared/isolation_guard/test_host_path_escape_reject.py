# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""隔离任务宿主路径命令就地拒绝（容器逃逸免审批直出）行为锁。

行为契约（用户裁定 2026-10-10）：
- 隔离任务 bash_execute 命令/working_dir 含宿主盘符路径 = 容器逃逸企图：
  不路由 host、不弹审批卡，pre_decided_results 直出拒绝话术给模型
  （拦截原因 / 已越位 / 改用相对路径重试），拒绝面只覆盖逃逸调用本身；
- 非隔离任务的宿主路径命令：维持 host 路由 + security_check 审批（零变化）；
- L1 主 agent 分支（无任务工作空间路由 host / 会话隔离进容器）：零变化；
- 纯相对路径命令：正常进容器执行（零变化）；
- 空命令 / 非 bash 工具：不触发逃逸拒绝。

Docker 探测与环境服务调用属跨进程外部依赖，按测试纪律替换为假身；
决策与预填逻辑全真实。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _THIS_DIR.parents[0]  # plugins/shared/
_SYSTEM_DIR = _SHARED_DIR / "system"

# 置顶注入 + 裸名逐出（同进程多插件 plugin/decider 裸名串扰防护），
# 随后按唯一模块名装载本插件 plugin.py。
for _d in (str(_SYSTEM_DIR), str(_SHARED_DIR), str(_THIS_DIR)):
    if _d in sys.path:
        sys.path.remove(_d)
    sys.path.insert(0, _d)
for _bare in ("plugin", "decider"):
    sys.modules.pop(_bare, None)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402

from agentos_plugin_sdk.isolation_types import IsolationLevel  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "isolation_guard_plugin_escape_reject", str(_THIS_DIR / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
IsolationGuard = _mod.IsolationGuard

pytestmark = pytest.mark.unit

# 真实可挂载工作空间（悬空挂载防护要求夹具路径存在；惰性建目录，落在系统临时区）
_WS = str(Path(tempfile.gettempdir()) / "agentos_ig_escape_reject_ws_fixture")


# ============================================================
# 辅助
# ============================================================


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    """每用例后摘除注入的 caller 假身（模块级全局，防跨用例泄漏）。"""
    yield
    _mod.set_env_caller(None)


def _make_guard(docker_available: bool = True) -> Any:
    """构造守卫：docker 可用性显式钉死（构造期不真实探测），环境服务 caller
    注入落地成功假身，decider 按策略真值 bash_execute → 容器、其余工具 → host。"""
    guard = IsolationGuard(config={
        "docker_available": docker_available,
        "providers": {"wsl_native": {"enabled": False}},
    })

    def _resolve(tool_name: str, category: Any = None) -> Any:
        mock = MagicMock()
        mock.isolation = (
            IsolationLevel.CONTAINER if tool_name == "bash_execute" else IsolationLevel.HOST
        )
        return mock

    guard._decider.resolve = MagicMock(side_effect=_resolve)

    async def _create_env(tool_name: str, args: dict[str, Any]) -> Any:
        return {"success": True, "data": {"env_id": "container-abc", "status": "ready"}}

    _mod.set_env_caller(_create_env)
    return guard


def _task_state(tool_calls: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """L2 隔离任务调用轮状态（task_id + L2 绕开主 agent 分支，workspace 真实存在）。"""
    if not Path(_WS).exists():
        Path(_WS).mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "t-escape",
        StateKeys.AGENT_LEVEL: "L2",
        "workspace": _WS,
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: tool_calls,
    }
    state.update(overrides)
    return state


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


# ============================================================
# 新行为：隔离任务宿主路径命令拒绝直出
# ============================================================


class TestIsolatedEscapeDirectReject:
    @pytest.mark.parametrize(
        "tool_args",
        [
            {"command": "type C:\\Users\\jc\\secrets.txt"},
            {"command": "cat D:/data/report.csv"},
            {
                "command": "ls .probe",
                "working_dir": "C:\\Users\\jc\\AppData\\Roaming\\agentos\\workspaces\\w1",
            },
        ],
        ids=["win_backslash_cmd", "win_forwardslash_cmd", "host_working_dir"],
    )
    async def test_isolated_task_host_path_rejected_with_prefill(self, tool_args: dict[str, Any]) -> None:
        """隔离任务宿主路径命令：denied + blocked + 预填拒绝话术，不路由 host。

        拒绝话术三要点：拦截原因（宿主路径）、越位事实、改法（相对路径重新调用）。
        """
        guard = _make_guard()
        call_id = "call-esc-1"
        result = await guard.execute(
            _ctx(_task_state([{"name": "bash_execute", "args": tool_args, "id": call_id}]))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["reason"] == "host_path_detected"
        assert contexts[0]["blocked"] is True

        entries = result.state_updates["pre_decided_results"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["call_id"] == call_id
        assert entry["success"] is False
        assert entry["metadata"]["decided_by"] == "isolation_guard"
        error = entry["error"]
        assert isinstance(error, str)
        assert "宿主" in error
        assert "越" in error
        assert "相对路径" in error
        assert "重新调用" in error
        # 纯逃逸批次无容器落地：不注入、不覆盖调用列表
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    async def test_escape_command_mixed_with_relative_still_rejected(self) -> None:
        """命令同时含宿主绝对路径与相对路径（cd 宿主目录 && cat 相对文件）
        → 仍按逃逸拒绝（含宿主路径即越位，不因夹带相对路径放行）。"""
        guard = _make_guard()
        cmd = "cd C:\\Users\\jc\\AppData\\Roaming\\agentos\\workspaces\\w1 && cat .probe/a.txt"
        result = await guard.execute(
            _ctx(_task_state([{"name": "bash_execute", "args": {"command": cmd}, "id": "call-mix"}]))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        entries = result.state_updates["pre_decided_results"]
        assert [e["call_id"] for e in entries] == ["call-mix"]
        assert all(e["success"] is False for e in entries)

    async def test_empty_command_and_non_bash_tool_no_reject(self) -> None:
        """空命令不触发宿主路径检测（照常进容器）；非 bash 工具走 policy
        路由（host），两者都不产生预填。"""
        guard = _make_guard()
        result = await guard.execute(
            _ctx(_task_state([{"name": "bash_execute", "args": {"command": ""}, "id": "call-empty"}]))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        assert "pre_decided_results" not in result.state_updates

        result_fw = await guard.execute(
            _ctx(_task_state([{"name": "file_write", "args": {"path": "D:/x"}, "id": "call-fw"}]))
        )
        contexts_fw = result_fw.state_updates["execution_contexts"]
        assert contexts_fw[0]["provider"] == "host"
        assert contexts_fw[0]["reason"] == "policy"
        assert "pre_decided_results" not in result_fw.state_updates


# ============================================================
# 混合批次语义：拒绝面只覆盖逃逸调用
# ============================================================


class TestMixedBatch:
    async def test_mixed_batch_reject_covers_escape_only(self) -> None:
        """一批两条 bash_execute（一条逃逸一条正常）：逃逸条预填拒绝，
        正常条照常进容器执行（容器落地/注入不受牵连，不陪葬）。"""
        guard = _make_guard()
        tool_calls = [
            {"name": "bash_execute", "args": {"command": "cat C:/outside.txt"}, "id": "call-esc"},
            {"name": "bash_execute", "args": {"command": "cat .probe/a.txt"}, "id": "call-ok"},
        ]
        result = await guard.execute(_ctx(_task_state(tool_calls)))

        entries = result.state_updates["pre_decided_results"]
        # 性质断言：拒绝面 ⊆ 逃逸调用（entry 数 = 逃逸数，正常调用不进预填）
        assert [e["call_id"] for e in entries] == ["call-esc"]
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[1]["provider"] == "docker"
        # 正常调用容器落地照常：注入 _container_id + working_dir 补 /workspace
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        ok_args = next(c["args"] for c in calls if c.get("id") == "call-ok")
        assert ok_args["_container_id"] == "container-abc"
        assert ok_args["working_dir"] == "/workspace"
        assert result.state_updates["isolation.container_name"] == "container-abc"

    async def test_multiple_escape_calls_prefilled_individually(self) -> None:
        """同批多条逃逸调用：每条各得一条预填拒绝（entry 数 = 逃逸数，
        call_id 一一对应）。"""
        guard = _make_guard()
        tool_calls = [
            {"name": "bash_execute", "args": {"command": "ls C:/x"}, "id": "call-e1"},
            {
                "name": "bash_execute",
                "args": {"command": "cat D:/y", "working_dir": "C:/z"},
                "id": "call-e2",
            },
            {"name": "bash_execute", "args": {"command": "pwd"}, "id": "call-ok"},
        ]
        result = await guard.execute(_ctx(_task_state(tool_calls)))
        entries = result.state_updates["pre_decided_results"]
        assert sorted(e["call_id"] for e in entries) == ["call-e1", "call-e2"]
        assert all(e["success"] is False for e in entries)
        assert all(e["metadata"]["decided_by"] == "isolation_guard" for e in entries)


# ============================================================
# 回归红线：不变面钉住
# ============================================================


class TestRegressionPins:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"execution_context": {"isolation": {"level": "non_isolated"}}},
            {"execution_context": {}},
        ],
        ids=["ec_non_isolated", "metadata_host_fallback"],
    )
    async def test_non_isolated_task_host_path_routes_to_host(self, overrides: dict[str, Any]) -> None:
        """非隔离任务宿主路径命令：维持 host 路由 + 审批语义，不产生预填。

        ec 缺位用例由 task metadata isolation_level=host 兜底（桩返回），
        覆盖隔离判定的两条真值源。
        """
        guard = _make_guard()
        if overrides.get("execution_context") == {}:
            guard._get_task_metadata = MagicMock(
                return_value={"isolation_level": "host"}
            )
        state = _task_state(
            [{"name": "bash_execute", "args": {"command": "cat C:/x"}, "id": "call-noniso"}],
            **overrides,
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "host_path_detected"
        assert contexts[0].get("blocked") is not True
        assert "pre_decided_results" not in result.state_updates

    async def test_l1_main_agent_no_workspace_host_path_unchanged(self) -> None:
        """L1 主 agent 无任务工作空间：宿主路径命令仍路由 host
        （l1_main_agent_host 先于宿主路径检测，走权限档审批，不进逃逸拒绝）。"""
        guard = _make_guard()
        state = _task_state(
            [{"name": "bash_execute", "args": {"command": "cat C:/x"}, "id": "call-l1"}],
            **{
                StateKeys.TASK_ID: "",
                StateKeys.AGENT_LEVEL: "L1",
                "workspace": "",
            },
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "l1_main_agent_host"
        assert "pre_decided_results" not in result.state_updates

    async def test_l1_main_agent_session_isolated_container_unchanged(self) -> None:
        """L1 主 agent 绑定 workspace 且 isolated：会话隔离分支先路由 docker
        （宿主路径检测不参与，零变化）。"""
        guard = _make_guard()
        state = _task_state(
            [{"name": "bash_execute", "args": {"command": "cat C:/x"}, "id": "call-l1iso"}],
            **{StateKeys.TASK_ID: "", StateKeys.AGENT_LEVEL: "L1"},
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        assert contexts[0]["reason"] == "l1_main_agent_session_isolated"
        assert "pre_decided_results" not in result.state_updates

    async def test_relative_path_command_container_unchanged(self) -> None:
        """纯相对/容器路径命令：无预填，正常 docker 决策 + 注入。"""
        guard = _make_guard()
        result = await guard.execute(
            _ctx(_task_state([
                {"name": "bash_execute", "args": {"command": "cat .probe/a.txt"}, "id": "call-rel"},
            ]))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        assert contexts[0]["reason"] == "task_metadata"
        assert "pre_decided_results" not in result.state_updates
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
