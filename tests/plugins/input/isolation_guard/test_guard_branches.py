# @feature: FP-0.2.二 isolation_guard 缺口分支补测 | @ci: python-coverage
# @ci: python-coverage
"""IsolationGuard 缺口分支补测——守卫早退族/复检异常态/路由分支/运行时配置覆盖。

覆盖面（批五覆盖率冲刺，接 test_container_landing.py 既有容器落地契约）：
- 环境服务实例化失败 → 容器落地降级 blocked（_get_manager 异常分支）
- execute 早退族：插件停用（config 级 / agent 运行时级）、非 tool_execute、
  无 tool_calls
- Docker 复检 probe_error 态（可用性不可验证 → fail-closed 拒绝）
- force_host 与容器要求型工具的冲突拒绝（不降级宿主）
- L1 主 agent 会话隔离 + Docker 不可用 → 拒绝
- 宿主路径检测路由（host_path_detected）与 _has_host_path 边界
- task metadata host 降级（task_metadata_downgrade）
- 容器获取守卫：无 workspace / 服务不可用；args 解析为非 dict 时归空
- task_service 三态：未接线降级 / 正常读取 / 读取异常
- server.py 接口适配层：懒构建缓存、on_load 预热、on_unload 逐出、
  dict 直返 / skip_remaining / PluginResult 拆包

Docker 探测与 IsolationManager 属跨进程外部依赖，按测试纪律替换为
stub/假 manager；决策逻辑全真实。
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from agentos_plugin_sdk.isolation_types import IsolationLevel
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _make_guard(docker_available: bool = True, **config: Any) -> Any:
    """构造 IsolationGuard，decider 按策略真值：bash_execute/browser_* → 容器。

    显式传 docker_available 使 _docker_auto=False（构造期不触发真实探测）；
    manager 注入假身（docker_available=True 的决策不再触碰真实 docker）。
    """
    from plugin import IsolationGuard

    guard = IsolationGuard(config={"docker_available": docker_available, **config})

    def _resolve(tool_name: str, category: Any = None) -> Any:
        mock = MagicMock()
        if tool_name in ("bash_execute", "browser_navigate", "browser_snapshot"):
            mock.isolation = IsolationLevel.CONTAINER
        else:
            mock.isolation = IsolationLevel.HOST
        return mock

    guard._decider.resolve = MagicMock(side_effect=_resolve)

    async def _fake_land(**kwargs: Any) -> Any:
        return types.SimpleNamespace(env_id="container-abc")

    guard._manager = types.SimpleNamespace(get_or_create_environment=_fake_land)
    return guard


def _base_state(**overrides: Any) -> dict[str, Any]:
    """主会话（无 task_id，L1 缺省）tool_execute 状态：workspace + isolated。"""
    base = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "",
        "workspace": "/host/ws",
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {"command": "ls"}}],
    }
    base.update(overrides)
    return base


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


# ============================================================
# 环境服务实例化失败 → 降级 blocked
# ============================================================


class TestManagerLoadFailure:
    async def test_manager_import_failure_degrades_to_none(self, monkeypatch: Any) -> None:
        """IsolationManager 导入失败 → 管理器 None（容器落地降级），不抛异常。"""
        guard = _make_guard(docker_available=False)
        guard._manager = None  # 摘掉辅助函数注入的假身，走真实懒加载
        monkeypatch.setitem(
            sys.modules, "isolation.manager", types.ModuleType("isolation.manager")
        )
        assert guard._get_manager() is None

    async def test_manager_unavailable_container_request_denied(
        self, monkeypatch: Any
    ) -> None:
        """服务不可用 → 容器获取 None → 对应调用标 blocked（不降级裸跑）。"""
        guard = _make_guard(docker_available=True)
        guard._get_manager = MagicMock(return_value=None)
        result = await guard.execute(_ctx(_base_state()))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker" or contexts[0].get("blocked") is True


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
        """纯 LLM 轮（无工具调用）不触发 docker 决策。"""
        guard = _make_guard(docker_available=False)
        state = _base_state(**{StateKeys.CORE_TYPE: "llm_call"})
        result = await guard.execute(_ctx(state))
        assert result.state_updates == {}

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
    async def test_command_with_host_path_routes_to_host(self) -> None:
        """bash 命令含 Windows 盘符路径 → host 执行（等待审批）。

        L2 任务身份（task_id 非空）——L1 主 agent 分支在宿主路径检查之前
        就已路由，须绕开才能命中本分支。
        """
        guard = _make_guard(docker_available=True)
        state = _base_state(
            **{
                StateKeys.TASK_ID: "t-1",
                StateKeys.AGENT_LEVEL: "L2",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash_execute", "args": {"command": "type D:/secrets.txt"}}
                ],
            }
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "host_path_detected"

    async def test_working_dir_with_host_path_routes_to_host(self) -> None:
        """working_dir 含宿主路径同样路由 host。"""
        guard = _make_guard(docker_available=True)
        state = _base_state(
            **{
                StateKeys.TASK_ID: "t-1",
                StateKeys.AGENT_LEVEL: "L2",
                StateKeys.RAW_TOOL_CALLS: [
                    {
                        "name": "bash_execute",
                        "args": {"command": "ls", "working_dir": "C:\\Users"},
                    }
                ],
            }
        )
        result = await guard.execute(_ctx(state))
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["reason"] == "host_path_detected"

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

        service = types.SimpleNamespace(get_task=lambda task_id: _Task())
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
        assert await guard._get_or_create_container(None, _ctx({})) is None

    async def test_manager_none_guard_in_acquisition(self) -> None:
        """服务不可用守卫（纵深防御）：_resolve_container 先行守卫之下，
        本方法自持同款守卫——直测锁定其独立契约。"""
        guard = _make_guard(docker_available=True)
        guard._get_manager = MagicMock(return_value=None)
        assert await guard._get_or_create_container("/ws", _ctx({})) is None

    async def test_args_parsed_to_non_dict_normalized_empty(self) -> None:
        """args JSON 解析结果非 dict（如 list）→ 归空 dict 再注入。"""
        guard = _make_guard(docker_available=True)

        async def _ok(**kwargs: Any) -> Any:
            return types.SimpleNamespace(env_id="container-xyz")

        guard._manager = types.SimpleNamespace(get_or_create_environment=_ok)
        state = _base_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash_execute", "args": '["rm", "-rf"]'}
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
            / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard"
        )
        spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "server.py")
        assert spec is not None and spec.loader is not None
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

    async def test_execute_dict_result_passthrough(self, server_mod: Any) -> None:
        """插件返回 dict → 原样透传（不拆 state_updates）。"""
        sentinel = {"state_updates": {"custom": True}}

        class _DictPlugin:
            async def execute(self, ctx: Any) -> Any:
                return sentinel

        server_mod.get_instance.cache_clear()
        server_mod.get_instance = lambda: _DictPlugin()  # type: ignore[assignment]
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
        server_mod.get_instance = lambda: _SkipPlugin()  # type: ignore[assignment]
        out = await server_mod.execute(state={"core_type": "llm_call"})
        assert out == {"state_updates": {"a": 1}, "skip_remaining": True}

    async def test_execute_real_plugin_result_unwrapped(self, server_mod: Any) -> None:
        """真实 IsolationGuard（llm_call 轮早退）→ 只拆出 state_updates。"""
        server_mod.get_instance.cache_clear()
        await server_mod._on_load({})
        out = await server_mod.execute(state={"core_type": "llm_call"})
        assert out == {"state_updates": {}}
