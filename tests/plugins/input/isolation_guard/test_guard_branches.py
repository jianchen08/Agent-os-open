# @feature: FP-0.2.二 isolation_guard 缺口分支补测 | @ci: python-coverage
# @ci: python-coverage
"""IsolationGuard 缺口分支补测——守卫早退族/复检异常态/路由分支/运行时配置覆盖。

覆盖面（批五覆盖率冲刺，接 test_container_landing.py 既有容器落地契约）：
- 环境服务调用方缺失（能力未注入）→ 容器落地降级 blocked
- execute 早退族：插件停用（config 级 / agent 运行时级）、非 tool_execute、
  无 tool_calls
- Docker 复检 probe_error 态（可用性不可验证 → fail-closed 拒绝）
- force_host 与容器要求型工具的冲突拒绝（不降级宿主）
- L1 主 agent 会话隔离 + Docker 不可用 → 拒绝
- 宿主路径检测（隔离任务就地拒绝直出 / 非隔离任务路由 host）与 _has_host_path 边界
- task metadata host 降级（task_metadata_downgrade）
- 容器获取守卫：无 workspace / 调用方未注入；args 解析为非 dict 时归空
- task_service 三态：未接线降级 / 正常读取 / 读取异常
- server.py 接口适配层：懒构建缓存、on_load 预热 + caller 注入/降级、
  on_unload 逐出、dict 直返 / skip_remaining / PluginResult 拆包

Docker 探测与环境服务调用属跨进程外部依赖，按测试纪律替换为
stub/假 caller；决策逻辑全真实。
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import plugin
from _libs.pipeline_base.plugin import PluginContext
from _libs.pipeline_base.types import StateKeys

from agentos_plugin_sdk.isolation_types import IsolationLevel
from plugin import IsolationGuard, set_env_caller

# 真实可挂载工作空间（悬空防护下夹具路径必须存在；系统临时区惰性建目录）
_LANDING_WS = str(Path(tempfile.gettempdir()) / "agentos_ig_branches_ws_fixture")

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    """每用例后摘除注入的 caller 假身（模块级全局，防跨用例泄漏）。"""
    yield
    set_env_caller(None)


def _ok_caller(env_id: str = "container-abc") -> Any:
    """落地成功的环境服务调用方假身（tool-executor 正门信封形态）。"""

    async def _call(tool_name: str, args: dict[str, Any]) -> Any:
        return {"success": True, "data": {"env_id": env_id, "status": "ready"}}

    return _call


def _make_guard(docker_available: bool = True, **config: Any) -> Any:
    """构造 IsolationGuard，decider 按策略真值：bash/browser_* → 容器。

    显式传 docker_available 使 _docker_auto=False（构造期不触发真实探测）；
    caller 注入落地成功假身（docker_available=True 的决策不再触碰真实 docker）。
    """
    # 钉 docker 后端：真身配置已启用 wsl_native，不钉则探测目标切到 WSL，
    # _detect_docker 桩失效（本机 WSL 在位即放行）。
    guard = IsolationGuard(config={
        "docker_available": docker_available,
        "providers": {"wsl_native": {"enabled": False}, "linux_native": {"enabled": False}},
        **config,
    })

    def _resolve(tool_name: str, category: Any = None) -> Any:
        mock = MagicMock()
        if tool_name in ("bash", "browser_navigate", "browser_snapshot"):
            mock.isolation = IsolationLevel.CONTAINER
        else:
            mock.isolation = IsolationLevel.HOST
        return mock

    guard._decider.resolve = MagicMock(side_effect=_resolve)
    set_env_caller(_ok_caller())
    return guard


def _base_state(**overrides: Any) -> dict[str, Any]:
    """主会话（无 task_id，L1 缺省）tool_execute 状态：workspace + isolated。

    workspace 用真实存在目录：挂载源悬空防护（d1c3b4213）下不存在路径会被拒建容器。"""
    if not Path(_LANDING_WS).exists():
        Path(_LANDING_WS).mkdir(parents=True, exist_ok=True)
    base = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "",
        "workspace": _LANDING_WS,
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: [{"name": "bash", "args": {"command": "ls"}}],
    }
    base.update(overrides)
    return base


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


# ============================================================
# 环境服务调用方缺失 → 降级 blocked
# ============================================================


class TestEnvCallerUnavailable:
    async def test_caller_missing_container_request_blocked(self) -> None:
        """caller 未注入（tool-executor 能力缺失）→ 落地失败 → blocked（不降级裸跑）。"""
        guard = _make_guard(docker_available=True)
        set_env_caller(None)
        result = await guard.execute(_ctx(_base_state()))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0].get("blocked") is True


# ============================================================
# execute 早退族
# ============================================================


class TestExecuteEarlyReturn:
    async def test_plugin_disabled_by_config(self) -> None:
        """config enabled=False → 空结果，不做任何决策。"""
        guard = _make_guard(docker_available=False, enabled=False)
        result = await guard.execute(_ctx(_base_state()))
        assert result.state_updates == {}

    async def test_plugin_disabled_by_agent_runtime_config(self) -> None:
        """agent 运行时 plugin_configs enabled=False → 空结果。"""
        guard = _make_guard(docker_available=False)
        state = _base_state(
            plugin_configs={"isolation_guard": {"enabled": False}},
        )
        result = await guard.execute(_ctx(state))
        assert result.state_updates == {}

    async def test_non_tool_execute_skipped(self) -> None:
        """纯 LLM 轮（无待执行调用）不触发 docker 决策——静态化判据（ADR
        2026-10-02-loopconfig-parallel-foreach）：调用轮由 raw_tool_calls 非空
        刻画，core_type 退役出早退门（llm_call + 待执行调用 = 照常决策）。"""
        guard = _make_guard(docker_available=False)
        # 非空调用轮：core_type=llm_call 不再短路，docker 不可用 → 拒绝决策落上下文
        pending = _base_state(**{StateKeys.CORE_TYPE: "llm_call"})
        result = await guard.execute(_ctx(pending))
        assert result.state_updates != {}
        assert result.state_updates["execution_contexts"][0]["blocked"] is True
        # 纯 LLM 轮：raw_tool_calls 空 → 零决策
        pure = _base_state(**{StateKeys.CORE_TYPE: "llm_call", StateKeys.RAW_TOOL_CALLS: []})
        result_pure = await guard.execute(_ctx(pure))
        assert result_pure.state_updates == {}

    async def test_empty_tool_calls_skipped(self) -> None:
        """tool_execute 但无调用列表 → 空结果。"""
        guard = _make_guard(docker_available=False)
        state = _base_state(**{StateKeys.RAW_TOOL_CALLS: []})
        result = await guard.execute(_ctx(state))
        assert result.state_updates == {}

    async def test_priority_from_config_and_default(self) -> None:
        """priority 可配置；缺省 40。"""
        assert _make_guard(docker_available=True, priority=7).priority == 7
        assert _make_guard(docker_available=True).priority == 40


# ============================================================
# Docker 复检 probe_error 态
# ============================================================


class TestRecheckProbeError:
    async def test_probe_error_fails_closed_with_detail(self, monkeypatch: Any) -> None:
        """复检探测异常（超时/权限）→ 拒绝原因携带异常原文，fail-closed。"""
        guard = _make_guard(docker_available=False)
        guard._docker_auto = True
        import time as _time

        guard._docker_checked_at = _time.monotonic() - 999.0  # 越过冷却窗口
        guard._ensure_engine = MagicMock()  # 自愈不真跑
        guard._detect_docker = MagicMock(
            return_value=(type(guard)._PROBE_ERROR, "timeout after 3s")
        )
        result = await guard.execute(_ctx(_base_state()))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["blocked"] is True
        assert "timeout after 3s" in contexts[0]["reason"]


# ============================================================
# force_host 与容器要求型工具冲突
# ============================================================


class TestForceHost:
    async def test_force_host_container_tool_denied_not_downgraded(self) -> None:
        """force_host + 容器要求型工具 → 拒绝（不得降级宿主执行）。"""
        guard = _make_guard(docker_available=True, force_host=True)
        result = await guard.execute(_ctx(_base_state()))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["reason"] == "force_host_denied_by_policy"
        assert contexts[0]["blocked"] is True

    async def test_force_host_host_tool_stays_host(self) -> None:
        """force_host 对本就走 host 的工具保持 host（不拒绝）。"""
        guard = _make_guard(docker_available=True, force_host=True)
        state = _base_state(
            **{StateKeys.RAW_TOOL_CALLS: [{"name": "file_write", "args": {}}]}
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "force_host"


# ============================================================
# L1 主 agent + Docker 不可用 → 拒绝
# ============================================================


class TestL1MainAgentDeny:
    async def test_session_isolated_without_docker_denied(self) -> None:
        """主 agent 会话要求容器但 Docker 不可用 → 拒绝（不降级宿主）。"""
        guard = _make_guard(docker_available=False)
        result = await guard.execute(_ctx(_base_state()))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["reason"] == "docker_unavailable_container_required"
        assert contexts[0]["blocked"] is True


# ============================================================
# 宿主路径路由
# ============================================================


class TestHostPathRouting:
    async def test_isolated_task_host_path_rejected_with_prefill(self) -> None:
        """隔离任务 bash 命令含 Windows 盘符路径 → 就地拒绝 + 预填改道话术
        （不路由 host、不弹审批，用户裁定 2026-10-10）。

        L2 任务身份（task_id 非空）——L1 主 agent 分支在宿主路径检查之前
        就已路由，须绕开才能命中本分支。
        """
        guard = _make_guard(docker_available=True)
        state = _base_state(
            **{
                StateKeys.TASK_ID: "t-1",
                StateKeys.AGENT_LEVEL: "L2",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "type D:/secrets.txt"}}
                ],
            }
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["reason"] == "host_path_detected"
        assert contexts[0]["blocked"] is True
        entries = result.state_updates["pre_decided_results"]
        assert len(entries) == 1
        assert entries[0]["success"] is False
        assert entries[0]["metadata"]["decided_by"] == "isolation_guard"

    async def test_non_isolated_task_host_path_routes_to_host(self) -> None:
        """非隔离任务宿主路径命令维持 host 路由（等待审批），不产生预填。"""
        guard = _make_guard(docker_available=True)
        state = _base_state(
            **{
                StateKeys.TASK_ID: "t-1",
                StateKeys.AGENT_LEVEL: "L2",
                "execution_context": {"isolation": {"level": "non_isolated"}},
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "ls", "working_dir": "C:\\Users"}}
                ],
            }
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "host_path_detected"
        assert contexts[0].get("blocked") is not True
        assert "pre_decided_results" not in result.state_updates

    async def test_has_host_path_boundaries(self) -> None:
        """盘符检测边界：URL 片段不误报，容器路径不报，盘符路径必报。"""
        from plugin import IsolationGuard

        assert IsolationGuard._has_host_path({"command": "cat D:/x"}) is True
        assert IsolationGuard._has_host_path({"command": "ls /workspace"}) is False
        assert IsolationGuard._has_host_path({"command": "curl http://x/a:/b"}) is False
        assert IsolationGuard._has_host_path({"other": "D:/x"}) is False


# ============================================================
# task metadata 降级与读取三态
# ============================================================


class TestMetadataOverrideAndTaskService:
    async def test_metadata_host_downgrades_container_tool(self) -> None:
        """task metadata isolation_level=host → 容器工具降级 host 执行。

        L2 任务身份（L1 分支先路由会绕过 metadata 检查）+ 无
        execution_context（ec_iso 缺位，metadata 才生效）。
        """
        guard = _make_guard(docker_available=True)
        guard._get_task_metadata = MagicMock(
            return_value={"isolation_level": "host"}
        )
        state = _base_state(
            **{
                StateKeys.TASK_ID: "t-1",
                StateKeys.AGENT_LEVEL: "L2",
                "execution_context": {},
            }
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "task_metadata_downgrade"

    async def test_task_service_missing_degrades_to_empty(self) -> None:
        """task_service 未接线 → metadata 空（降级语义），只告警一次。"""
        guard = _make_guard(docker_available=True)
        ctx = _ctx({StateKeys.TASK_ID: "t-1"}, services={})
        assert type(guard)._get_task_metadata(guard, ctx) == {}
        assert guard._service_warned is True

    async def test_task_service_returns_metadata(self) -> None:
        """task_service 正常 → 返回任务 metadata。"""
        guard = _make_guard(docker_available=True)

        class _Task:
            metadata = {"isolation_level": "isolated", "workspace": "/task/ws"}

        service = types.SimpleNamespace(get_task=lambda _task_id: _Task())
        ctx = _ctx({StateKeys.TASK_ID: "t-1"}, services={"task_service": service})
        assert type(guard)._get_task_metadata(guard, ctx) == _Task.metadata

    async def test_task_service_failure_degrades_to_empty(self) -> None:
        """task_service.get_task 抛异常 → metadata 空（不阻断管道）。"""
        guard = _make_guard(docker_available=True)

        def _boom(task_id: str) -> Any:
            raise RuntimeError("任务服务抖动")

        service = types.SimpleNamespace(get_task=_boom)
        ctx = _ctx({StateKeys.TASK_ID: "t-1"}, services={"task_service": service})
        assert type(guard)._get_task_metadata(guard, ctx) == {}


# ============================================================
# 容器获取守卫 / args 归一
# ============================================================


class TestContainerAcquisitionGuards:
    async def test_no_workspace_returns_none(self) -> None:
        guard = _make_guard(docker_available=True)
        assert await guard._get_or_create_container(None, _ctx({})) == (None, None, "workspace 未解析")

    async def test_caller_none_guard_in_acquisition(self) -> None:
        """调用方未注入守卫：workspace 有效也不触服务，直接 (None, None)。"""
        guard = _make_guard(docker_available=True)
        set_env_caller(None)
        assert await guard._get_or_create_container(_LANDING_WS, _ctx({})) == (None, None, "环境服务调用方未注入")

    async def test_args_parsed_to_non_dict_normalized_empty(self) -> None:
        """args JSON 解析结果非 dict（如 list）→ 归空 dict 再注入。"""
        guard = _make_guard(docker_available=True)
        set_env_caller(_ok_caller("container-xyz"))
        state = _base_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": '["rm", "-rf"]'}
                ]
            }
        )
        result = await guard.execute(_ctx(state))
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-xyz"
        assert calls[0]["args"]["working_dir"] == "/workspace"


# ============================================================
# 运行时配置覆盖（docker_available / force_host）
# ============================================================


class TestRuntimeConfigOverride:
    async def test_runtime_force_host_overrides_and_denies_container(self) -> None:
        """state plugin_configs 的 force_host/docker_available 覆盖生效。"""
        guard = _make_guard(docker_available=False)
        state = _base_state(
            plugin_configs={
                "isolation_guard": {"docker_available": True, "force_host": True}
            },
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["reason"] == "force_host_denied_by_policy"

    async def test_runtime_docker_available_clears_probe_error(self) -> None:
        """配置显式声明可用性时清掉残留探测异常态（isolation_mode 不再标 host）。"""
        guard = _make_guard(docker_available=False)
        guard._docker_probe_error = "stale probe error"
        state = _base_state(
            plugin_configs={"isolation_guard": {"docker_available": True}},
            **{StateKeys.RAW_TOOL_CALLS: [{"name": "file_read", "args": {}}]},
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        # file_read（host 策略）在 docker 可用后不再带 host 降级标记
        assert "isolation_mode" not in contexts[0]
        assert guard._docker_probe_error is None


# ============================================================
# server.py 接口适配层
# ============================================================


class TestServerAdapter:
    @pytest.fixture
    def server_mod(self, monkeypatch: Any) -> Any:
        import importlib.util

        mod_name = "isolation_guard_server_branches"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        plugin_dir = (
            __import__("pathlib").Path(__file__).resolve().parents[4]
            / "plugins" / "shared" / "isolation_guard"
        )
        spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "server.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    async def test_on_load_warms_and_unload_evicts(self, server_mod: Any) -> None:
        """on_load 预热单例；on_unload 逐出缓存（下次重建新实例）。"""
        await server_mod._on_load({})
        first = server_mod.get_instance()
        assert server_mod.get_instance() is first
        await server_mod._on_unload({})
        second = server_mod.get_instance()
        assert second is not first

    async def test_on_load_injects_env_caller(self, server_mod: Any, monkeypatch: Any) -> None:
        """on_load：tool-executor 能力在 → 注入环境服务调用方（正门 invoke 带 plugin_id）。"""
        captured: dict = {}

        class _FakeHandle:
            async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
                captured["method"] = method
                captured["params"] = params
                return {"success": True, "data": {"env_id": "c1", "status": "ready"}}

        def _get_capability(name: str) -> Any:
            if name == "tool-executor":
                return _FakeHandle()
            raise KeyError(name)

        monkeypatch.setattr(server_mod.plugin, "get_capability", _get_capability)
        injected: list[Any] = []
        monkeypatch.setattr(server_mod, "set_env_caller", lambda c: injected.append(c))
        await server_mod._on_load({})
        assert len(injected) == 1
        # 注入的 caller 路由到 isolation_service 正门（显式 plugin_id + 工具名）；
        # bind_capability_caller 剥掉能力前缀后交句柄（句柄负责拼 "tool-executor."）
        await injected[0]("isolation.create_env", {"task_id": "t-1", "workspace": "/ws"})
        assert captured["method"] == "invoke"
        assert captured["params"]["plugin_id"] == server_mod._ISOLATION_PLUGIN_ID
        assert captured["params"]["tool_name"] == "isolation.create_env"
        assert captured["params"]["args"] == {"task_id": "t-1", "workspace": "/ws"}

    async def test_on_load_degrades_without_capability(self, server_mod: Any, monkeypatch: Any) -> None:
        """on_load：tool-executor 能力缺失 → 注入 None（容器落地降级 blocked，不崩溃）。"""

        def _missing(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(server_mod.plugin, "get_capability", _missing)
        injected: list[Any] = []
        monkeypatch.setattr(server_mod, "set_env_caller", lambda c: injected.append(c))
        await server_mod._on_load({})
        assert injected == [None]

    async def test_on_unload_clears_env_caller(self, server_mod: Any, monkeypatch: Any) -> None:
        """on_unload：调用方一并摘除（卸载窗口期不残留旧句柄）。"""
        cleared: list[Any] = []
        monkeypatch.setattr(server_mod, "set_env_caller", lambda c: cleared.append(c))
        await server_mod._on_unload({})
        assert cleared == [None]

    async def test_execute_dict_result_passthrough(self, server_mod: Any) -> None:
        """插件返回 dict → 原样透传（不拆 state_updates）。"""
        sentinel = {"state_updates": {"custom": True}}

        class _DictPlugin:
            async def execute(self, ctx: Any) -> Any:
                return sentinel

        server_mod.get_instance.cache_clear()
        server_mod.get_instance = _DictPlugin  # type: ignore[assignment]
        out = await server_mod.execute(state={"core_type": "llm_call"})
        assert out is sentinel

    async def test_execute_skip_remaining_flagged(self, server_mod: Any) -> None:
        """PluginResult.skip_remaining=True → 结果带 skip_remaining 标记。"""

        class _SkipPlugin:
            async def execute(self, ctx: Any) -> Any:
                return types.SimpleNamespace(
                    state_updates={"a": 1}, skip_remaining=True
                )

        server_mod.get_instance.cache_clear()
        server_mod.get_instance = _SkipPlugin  # type: ignore[assignment]
        out = await server_mod.execute(state={"core_type": "llm_call"})
        assert out == {"state_updates": {"a": 1}, "skip_remaining": True}

    async def test_execute_real_plugin_result_unwrapped(self, server_mod: Any) -> None:
        """真实 IsolationGuard（llm_call 轮早退）→ 只拆出 state_updates。"""
        server_mod.get_instance.cache_clear()
        await server_mod._on_load({})
        out = await server_mod.execute(state={"core_type": "llm_call"})
        assert out == {"state_updates": {}}
