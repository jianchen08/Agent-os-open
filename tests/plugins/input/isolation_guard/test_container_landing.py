# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""IsolationGuard 容器落地测试（GAP 合并：吸收原 session_isolation 语义）。

覆盖：
1. 会话级隔离（L1 主 agent + workspace + isolated）→ provider=docker → 容器注入
2. 容器注入细节：_container_id、working_dir 补 /workspace、显式 working_dir 保留、
   非 bash 不注入、JSON 字符串 args 解析、exec_backend 随服务响应注入
3. 容器不可达（服务不可用/调用异常/业务 error/ERROR 占位/缺 env_id）→
   对应调用标 blocked（不降级裸跑）
4. 服务正门请求形态：tool_name 路由 + task_id 会话级语义 + isolation_level
   服务 enum（isolated = SDK CONTAINER 值）
5. L2 任务管道同样走容器落地；非隔离/无 workspace 不注入

容器获取唯一属主 = isolation 服务（B5 职责归位）：guard 经 tool-executor
正门调 isolation.create_env，测试按该调用形态注入 caller 假身；决策与预填
逻辑全真实。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import plugin
from agentos_plugin_sdk.isolation_types import IsolationLevel
from _libs.pipeline_base.plugin import PluginContext
from _libs.pipeline_base.types import StateKeys
from plugin import IsolationGuard

# 真实可挂载工作空间（悬空防护下夹具路径必须存在；惰性建目录，落在系统临时区）
_LANDING_WS = str(Path(tempfile.gettempdir()) / "agentos_ig_landing_ws_fixture")

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    """每用例后摘除注入的 caller 假身（模块级全局，防跨用例泄漏）。"""
    yield
    plugin.set_env_caller(None)


def _make_guard(docker_available: bool = True) -> Any:
    """构造 IsolationGuard，decider 按真实 policy 语义返回隔离级别。

    bash → CONTAINER（策略真值）；browser_* → CONTAINER（Bridge 模式
    沙箱内发起）；其余工具（file_read/file_write 等）→ HOST（容器内没有工具
    代码，isolation_policy.yaml 真值即 non_isolated）。
    默认注入落地成功的 caller 假身（落地细节由各用例按需覆盖）。
    """
    guard = IsolationGuard(config={"docker_available": docker_available})

    def _resolve(tool_name: str, category: Any = None) -> Any:
        mock = MagicMock()
        if tool_name in ("bash", "browser_navigate", "browser_snapshot", "browser_click",
                         "browser_type", "browser_take_screenshot", "browser_console_messages"):
            mock.isolation = IsolationLevel.CONTAINER
        else:
            mock.isolation = IsolationLevel.HOST
        return mock

    guard._decider.resolve = MagicMock(side_effect=_resolve)
    guard._get_task_metadata = MagicMock(return_value={})
    plugin.set_env_caller(_env_caller_stub())
    return guard


def _env_caller_stub(
    env_id: str | None = "container-abc",
    *,
    envelope: dict[str, Any] | None = None,
    exc: Exception | None = None,
    exec_backend: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    """构造环境服务调用方假身（tool-executor 正门形态）。

    envelope 显式给定时原样返回（业务失败形态用例）；否则按 env_id /
    exec_backend 组成功信封；env_id=None → data 缺 env_id（服务异常形态）。
    calls 传入时记录每次 (tool_name, args)。
    """

    async def _call(tool_name: str, args: dict[str, Any]) -> Any:
        if calls is not None:
            calls.append({"tool_name": tool_name, "args": args})
        if exc is not None:
            raise exc
        if envelope is not None:
            return envelope
        data: dict[str, Any] = {"status": "ready"}
        if env_id is not None:
            data["env_id"] = env_id
        if exec_backend is not None:
            data["exec_backend"] = exec_backend
        return {"success": True, "data": data}

    return _call


def _base_state(**overrides: Any) -> dict[str, Any]:
    """主会话（无 task_id，L1 缺省）tool_execute 状态：workspace + isolated。

    workspace 用真实存在目录：挂载源悬空防护（d1c3b4213）下不存在路径会被
    拒绝建容器，夹具路径必须可挂载。
    tool_output_contracts 对齐生产：tool_schema 插件每轮写入契约表，bash
    已声明 isolation_capable（plugin.json 声明位）。"""
    if not Path(_LANDING_WS).exists():
        Path(_LANDING_WS).mkdir(parents=True, exist_ok=True)
    base = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "",
        "workspace": _LANDING_WS,
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: [{"name": "bash", "args": {"command": "ls"}}],
        "tool_output_contracts": {
            "bash": {"schema": {}, "render": None, "isolation_capable": True},
        },
    }
    base.update(overrides)
    return base


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


# ============================================================
# 会话级隔离：L1 主 agent + workspace + isolated → docker → 容器注入
# ============================================================


class TestSessionIsolatedLanding:
    @pytest.mark.asyncio
    async def test_会话隔离主agent_bash注入container_id(self) -> None:
        """L1 主 agent 绑定了 workspace 且 isolated → docker 决策 + 容器注入。"""
        guard = _make_guard(docker_available=True)
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        assert contexts[0]["reason"] == "l1_main_agent_session_isolated"
        assert contexts[0].get("blocked") is not True

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["working_dir"] == "/workspace"

    @pytest.mark.asyncio
    async def test_显式working_dir保留不覆盖(self) -> None:
        guard = _make_guard()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "bash", "args": {"command": "ls", "working_dir": "/host/custom"}}
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["working_dir"] == "/host/custom"
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_混合工具只改bash(self) -> None:
        guard = _make_guard()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "file_read", "args": {"path": "/x"}},
                            {"name": "bash", "args": {"command": "pwd"}},
                            {"name": "file_write", "args": {"path": "/y"}},
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert "_container_id" not in calls[0]["args"]
        assert calls[1]["args"]["_container_id"] == "container-abc"
        assert "_container_id" not in calls[2]["args"]

    @pytest.mark.asyncio
    async def test_args为JSON字符串也能解析注入(self) -> None:
        guard = _make_guard()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "bash", "args": '{"command": "ls"}'}
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["command"] == "ls"

    @pytest.mark.asyncio
    async def test_args非法JSON降级空dict再注入(self) -> None:
        guard = _make_guard()
        result = await guard.execute(
            _ctx(_base_state(**{StateKeys.RAW_TOOL_CALLS: [{"name": "bash", "args": "not-json{"}]}))
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_无workspace的主agent不走docker(self) -> None:
        """L1 主 agent 无 workspace → host（l1_main_agent_host），无容器落地。"""
        guard = _make_guard()
        result = await guard.execute(_ctx(_base_state(workspace="")))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "l1_main_agent_host"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_非隔离会话不注入(self) -> None:
        """isolation=non_isolated → L1 主 agent 仍 host，无 docker 决策。"""
        guard = _make_guard()
        result = await guard.execute(
            _ctx(_base_state(**{"execution_context": {"isolation": {"level": "non_isolated"}}}))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates


# ============================================================
# 服务正门请求形态：路由 / 会话级语义 / 隔离级别 enum
# ============================================================


class TestServiceRequestShape:
    @pytest.mark.asyncio
    async def test_主会话无task_id挂session容器(self) -> None:
        """会话级语义：无任务（task_id 空）→ create_env 请求 task_id="session"。"""
        calls: list[dict[str, Any]] = []
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        await guard.execute(_ctx(_base_state()))

        assert len(calls) == 1
        assert calls[0]["tool_name"] == "isolation.create_env"
        args = calls[0]["args"]
        assert args["task_id"] == "session"
        assert args["task_type"] == "atomic"
        assert args["operation_type"] == "code_execution"
        assert args["workspace"] == _LANDING_WS
        # 服务 enum "isolated" = SDK IsolationLevel.CONTAINER 的值（等价转换）
        assert args["isolation_level"] == "isolated"

    @pytest.mark.asyncio
    async def test_任务管道请求带任务id(self) -> None:
        """L2 任务管道：create_env 请求携带任务 id（容器归任务域）。"""
        calls: list[dict[str, Any]] = []
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        state = _base_state(**{StateKeys.TASK_ID: "t1", StateKeys.AGENT_LEVEL: "L2"})
        await guard.execute(_ctx(state))

        assert calls[0]["args"]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_原生环境exec_backend随响应注入bash(self) -> None:
        """服务响应带 exec_backend（原生环境）→ 已声明的 bash 注入 _exec_backend 传输通道。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu"}
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(exec_backend=backend))
        result = await guard.execute(_ctx(_base_state()))

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["_exec_backend"] == backend

    @pytest.mark.asyncio
    async def test_声明驱动的第三方工具注入exec_backend(self) -> None:
        """非 bash/browser 工具声明 isolation_capable + 决策进容器 → 同样注入
        _exec_backend（声明即注册，guard 无工具名白名单）。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu"}
        guard = _make_guard()

        def _resolve(tool_name: str, category: Any = None) -> Any:
            mock = MagicMock()
            mock.isolation = (
                IsolationLevel.CONTAINER if tool_name == "data_runner" else IsolationLevel.HOST
            )
            return mock

        guard._decider.resolve = MagicMock(side_effect=_resolve)
        plugin.set_env_caller(_env_caller_stub(exec_backend=backend))
        state = _base_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [{"name": "data_runner", "args": {"script": "a.py"}}],
                "tool_output_contracts": {
                    "data_runner": {"schema": {}, "render": None, "isolation_capable": True},
                },
            }
        )
        result = await guard.execute(_ctx(state))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["_exec_backend"] == backend
        # 非 bash 工具不代填 working_dir
        assert "working_dir" not in calls[0]["args"]

    @pytest.mark.asyncio
    async def test_未声明工具进容器只拿container_id(self) -> None:
        """决策进容器但契约表未声明 isolation_capable → 只注入 _container_id
        （声明缺席 = 能力未声明，fail-closed 不注入）。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu"}
        guard = _make_guard()

        def _resolve(tool_name: str, category: Any = None) -> Any:
            mock = MagicMock()
            mock.isolation = (
                IsolationLevel.CONTAINER if tool_name == "undeclared_tool" else IsolationLevel.HOST
            )
            return mock

        guard._decider.resolve = MagicMock(side_effect=_resolve)
        plugin.set_env_caller(_env_caller_stub(exec_backend=backend))
        state = _base_state(
            **{
                StateKeys.RAW_TOOL_CALLS: [{"name": "undeclared_tool", "args": {"x": 1}}],
                "tool_output_contracts": {
                    "undeclared_tool": {"schema": {}, "render": None, "isolation_capable": False},
                },
            }
        )
        result = await guard.execute(_ctx(state))

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert "_exec_backend" not in calls[0]["args"]

    @pytest.mark.asyncio
    async def test_contracts缺失告警且不注入exec_backend(self, caplog: pytest.LogCaptureFixture) -> None:
        """契约表状态缺失（管道无 tool_schema 步骤等）→ bash 仍拿 _container_id
        与 working_dir 缺省，但不注入 _exec_backend；warning 观测不静默。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu"}
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(exec_backend=backend))
        state = _base_state()
        del state["tool_output_contracts"]
        with caplog.at_level("WARNING"):
            result = await guard.execute(_ctx(state))

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["working_dir"] == "/workspace"
        assert "_exec_backend" not in calls[0]["args"]
        assert any("tool_output_contracts 缺失" in r.message for r in caplog.records)


# ============================================================
# L2 任务管道：docker 决策 → 容器注入
# ============================================================


class TestTaskLanding:
    @pytest.mark.asyncio
    async def test_L2任务bash注入container_id(self) -> None:
        guard = _make_guard()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                    }
                )
            )
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "docker"
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"


# ============================================================
# 挂载源兜底：state.workspace 缺失时按任务坐标键解析
# ============================================================


class TestLandingWorkspaceFallback:
    """挂载源解析链：state.workspace → task metadata → task.ws_meta / parent_ws_meta。

    装机实测（2026-10-17）：shared 模式任务管道不落 state.workspace，且
    task_service 未接线（metadata 恒空）——单一来源缺失使容器落地全程失败，
    整条任务生命期 bash 全被拦（container_create_failed）。
    """

    @pytest.mark.asyncio
    async def test_state无workspace按task_ws_meta落地(self) -> None:
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                        "workspace": "",
                        "task.ws_meta": {
                            "mode": "shared",
                            "path": _LANDING_WS,
                            "project_root": "",
                        },
                    }
                )
            )
        )
        assert calls and calls[0]["args"]["workspace"] == _LANDING_WS
        assert result.state_updates["execution_contexts"][0]["provider"] == "docker"
        injected = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert injected[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_坐标键JSON字符串形态容忍(self) -> None:
        """state 单键两形态容忍：JSON 字符串（持久化形态）解析后取 path。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                        "workspace": "",
                        "lineage.parent_ws_meta": json.dumps(
                            {"mode": "plain", "path": _LANDING_WS}
                        ),
                    }
                )
            )
        )
        assert calls and calls[0]["args"]["workspace"] == _LANDING_WS
        assert result.state_updates["execution_contexts"][0]["provider"] == "docker"

    @pytest.mark.asyncio
    async def test_坐标键残缺仍标blocked(self) -> None:
        """坐标键全缺/无 path 不猜路径：维持 blocked（fail-closed 语义不变）。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                        "workspace": "",
                        "task.ws_meta": {"mode": "shared", "project_root": ""},
                    }
                )
            )
        )
        assert calls == []
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        assert contexts[0]["provider"] == "denied"

    @pytest.mark.asyncio
    async def test_metadata来源优先于任务坐标键(self) -> None:
        """解析链优先级：task metadata 在场时压过 task.ws_meta（服务接线后语义）。"""
        guard = _make_guard()
        guard._get_task_metadata = MagicMock(return_value={"workspace": _LANDING_WS})
        calls: list[dict[str, Any]] = []
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                        "workspace": "",
                        "task.ws_meta": {"mode": "shared", "path": "/nonexistent-coord"},
                    }
                )
            )
        )
        assert calls and calls[0]["args"]["workspace"] == _LANDING_WS

    @pytest.mark.asyncio
    async def test_坐标键JSON非法字符串回退下一键(self) -> None:
        """非法 JSON 字符串坐标键容忍（解析失败视为缺失）→ 回退下一键。"""
        guard = _make_guard()
        calls: list[dict[str, Any]] = []
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.TASK_ID: "t1",
                        StateKeys.AGENT_LEVEL: "L2",
                        "workspace": "",
                        "task.ws_meta": "{不是 JSON",
                        "lineage.parent_ws_meta": json.dumps({"path": _LANDING_WS}),
                    }
                )
            )
        )
        assert calls and calls[0]["args"]["workspace"] == _LANDING_WS


# ============================================================
# 容器不可达：blocked，不降级裸跑
# ============================================================


class TestContainerUnavailable:
    @pytest.mark.asyncio
    async def test_服务调用异常标blocked(self) -> None:
        """caller 抛异常（网络/超时/服务崩溃）→ 容器获取 None → blocked。"""
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(exc=RuntimeError("docker down")))
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        # 结果预填（ADR 2026-09-28）：blocked 调用直出预定拒绝结果
        entries = result.state_updates["pre_decided_results"]
        assert len(entries) == 1
        assert entries[0]["tool_name"] == _base_state()[StateKeys.RAW_TOOL_CALLS][0]["name"]
        assert entries[0]["success"] is False
        assert "container_create_failed" in entries[0]["error"]
        assert entries[0]["metadata"]["decided_by"] == "isolation_guard"
        # 不注入 _container_id（工具不会被放行到裸跑）
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_ERROR占位环境不注入标blocked(self) -> None:
        """服务返回 ERROR 占位环境（创建失败）→ 不把假 env_id 注入工具。

        回归锚：中文 workspace 曾使容器创建失败，guard 把 ERROR 占位环境的
        env_id（docker-<task_id>，如 docker-session）当容器 id 注入，bash
        报晦涩的 No such container: docker-session。
        """
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(envelope={
            "success": True,
            "data": {
                "env_id": "docker-session",
                "status": "error",
                "provider_error": "Invalid container name (cua-修仙游戏)",
            },
        }))
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_服务业务失败标blocked且原因留日志(self, caplog: pytest.LogCaptureFixture) -> None:
        """服务显式失败（success=False，如配额超限）→ blocked，真实原因落日志。"""
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(envelope={
            "success": False,
            "error": "隔离环境数量已达上限 (3/3)，无法为任务创建新环境。",
        }))
        with caplog.at_level("WARNING", logger="plugin"):
            result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        assert result.state_updates["pre_decided_results"][0]["success"] is False
        assert any("已达上限" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_服务裸返error业务载荷标blocked(self, caplog: pytest.LogCaptureFixture) -> None:
        """data 带 error 键（裸返 error dict 旧形态，内核包成 success=true）→ blocked。"""
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(envelope={
            "success": True,
            "data": {"error": "隔离服务未初始化"},
        }))
        with caplog.at_level("WARNING", logger="plugin"):
            result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert any("隔离服务未初始化" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_服务响应缺env_id标blocked(self) -> None:
        """成功信封但 data 缺 env_id（服务异常形态）→ blocked（不注入空 id）。"""
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(env_id=None))
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"

    @pytest.mark.asyncio
    async def test_环境服务不可用标blocked(self) -> None:
        """caller 未注入（tool-executor 能力缺失）→ 容器落地失败 → blocked。"""
        guard = _make_guard()
        plugin.set_env_caller(None)
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert result.state_updates["pre_decided_results"][0]["success"] is False


# ============================================================
# state 容器绑定：销毁锚与观测数据，落地每轮幂等回写
# ============================================================


class TestStateBinding:
    @pytest.mark.asyncio
    async def test_首轮落地写入绑定(self) -> None:
        """落地成功 → 容器 id 写入 state（environment_lifecycle exit 的按名销毁锚）。"""
        guard = _make_guard()
        result = await guard.execute(_ctx(_base_state()))

        assert result.state_updates["isolation.container_name"] == "container-abc"
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_绑定在场仍经服务幂等重取_绑定值稳定(self) -> None:
        """容器获取唯一属主是服务：绑定在场不跳过服务（无本地登记态可凭），
        create_env 幂等返回同一容器 → 注入 id 与绑定回写值稳定不变。"""
        calls: list[dict[str, Any]] = []
        guard = _make_guard()
        plugin.set_env_caller(_env_caller_stub(calls=calls))
        result = await guard.execute(
            _ctx(_base_state(**{"isolation.container_name": "container-abc"}))
        )

        assert len(calls) == 1
        calls_out = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls_out[0]["args"]["_container_id"] == "container-abc"
        assert result.state_updates["isolation.container_name"] == "container-abc"
