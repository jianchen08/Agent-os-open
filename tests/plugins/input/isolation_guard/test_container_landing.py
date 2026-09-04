# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""IsolationGuard 容器落地测试（GAP 合并：吸收原 session_isolation 语义）。

覆盖：
1. 会话级隔离（L1 主 agent + workspace + isolated）→ provider=docker → 容器注入
2. 容器注入细节：_container_id、working_dir 补 /workspace、显式 working_dir 保留、
   非 bash 不注入、JSON 字符串 args 解析
3. 容器不可达（服务缺失/创建失败/返回 None）→ 对应调用标 blocked（不降级裸跑）
4. L2 任务管道同样走容器落地；非隔离/无 workspace 不注入
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from isolation_types import IsolationLevel
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _make_guard(docker_available: bool = True) -> Any:
    """构造 IsolationGuard，decider 对任意工具返回 CONTAINER 策略。"""
    from plugin import IsolationGuard

    guard = IsolationGuard(config={"docker_available": docker_available})
    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    guard._decider.resolve = MagicMock(return_value=mock_policy)
    guard._get_task_metadata = MagicMock(return_value={})
    return guard


def _fake_manager(container_id: str | None = "container-abc", exc: Exception | None = None) -> Any:
    """构造假 IsolationManager（懒加载缓存位 _manager 直接注入）。"""
    async def _get_or_create(**kwargs: Any) -> Any:
        if exc is not None:
            raise exc
        if container_id is None:
            return None
        return SimpleNamespace(env_id=container_id)

    return SimpleNamespace(get_or_create_environment=_get_or_create)


def _error_env_manager(error: str = "Invalid container name") -> Any:
    """构造返回 ERROR 占位环境的假 manager（容器创建失败路径的真实形态）。

    manager 对创建失败返回 status=error 的占位环境（env_id 是
    "docker-<task_id>" 形状的内存假 id，docker 里并无此容器）。
    """
    env = SimpleNamespace(
        env_id="docker-session",
        status="error",
        provider_info={"error": error},
    )

    async def _get_or_create(**kwargs: Any) -> Any:
        return env

    return SimpleNamespace(get_or_create_environment=_get_or_create)


def _binding_manager(alive: bool, resolve_id: str = "container-abc") -> Any:
    """构造带验活接口的假 manager（state 绑定直读路径）。

    ensure_container_alive 恒返回 alive；get_or_create_environment 返回
    resolve_id（落地路径），并记录落地调用次数。
    """
    state = {"resolve_calls": 0}

    async def _alive(name: str) -> bool:
        return alive

    async def _get_or_create(**kwargs: Any) -> Any:
        state["resolve_calls"] += 1
        return SimpleNamespace(env_id=resolve_id)

    manager = SimpleNamespace(ensure_container_alive=_alive, get_or_create_environment=_get_or_create)
    manager._test_state = state  # type: ignore[attr-defined]
    return manager


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
        guard._manager = _fake_manager("container-abc")
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
        guard._manager = _fake_manager()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "bash_execute", "args": {"command": "ls", "working_dir": "/host/custom"}}
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["working_dir"] == "/host/custom"
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_混合工具只改bash_execute(self) -> None:
        guard = _make_guard()
        guard._manager = _fake_manager()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "file_read", "args": {"path": "/x"}},
                            {"name": "bash_execute", "args": {"command": "pwd"}},
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
        guard._manager = _fake_manager()
        result = await guard.execute(
            _ctx(
                _base_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "bash_execute", "args": '{"command": "ls"}'}
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
        guard._manager = _fake_manager()
        result = await guard.execute(
            _ctx(_base_state(**{StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": "not-json{"}]}))
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_无workspace的主agent不走docker(self) -> None:
        """L1 主 agent 无 workspace → host（l1_main_agent_host），无容器落地。"""
        guard = _make_guard()
        guard._manager = _fake_manager()
        result = await guard.execute(_ctx(_base_state(workspace="")))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "l1_main_agent_host"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_非隔离会话不注入(self) -> None:
        """isolation=non_isolated → L1 主 agent 仍 host，无 docker 决策。"""
        guard = _make_guard()
        guard._manager = _fake_manager()
        result = await guard.execute(
            _ctx(_base_state(**{"execution_context": {"isolation": {"level": "non_isolated"}}}))
        )
        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "host"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates


# ============================================================
# L2 任务管道：docker 决策 → 容器注入
# ============================================================


class TestTaskLanding:
    @pytest.mark.asyncio
    async def test_L2任务bash注入container_id(self) -> None:
        guard = _make_guard()
        guard._manager = _fake_manager()
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
# 容器不可达：blocked，不降级裸跑
# ============================================================


class TestContainerUnavailable:
    @pytest.mark.asyncio
    async def test_容器创建异常标blocked(self) -> None:
        guard = _make_guard()
        guard._manager = _fake_manager(exc=RuntimeError("docker down"))
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["provider"] == "denied"
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        assert result.state_updates["isolation.blocked"] is True
        # 不注入 _container_id（工具不会被放行到裸跑）
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_ERROR占位环境不注入标blocked(self) -> None:
        """manager 返回 ERROR 占位环境（创建失败）→ 不把假 env_id 注入工具。

        回归锚：中文 workspace 曾使容器创建失败，guard 把 ERROR 占位环境的
        env_id（docker-<task_id>，如 docker-session）当容器 id 注入，bash
        报晦涩的 No such container: docker-session。
        """
        guard = _make_guard()
        guard._manager = _error_env_manager("Invalid container name (cua-修仙游戏)")
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates

    @pytest.mark.asyncio
    async def test_容器获取返回None标blocked(self) -> None:
        guard = _make_guard()
        guard._manager = _fake_manager(container_id=None)
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert contexts[0]["reason"] == "container_create_failed"

    @pytest.mark.asyncio
    async def test_环境服务不可用标blocked(self) -> None:
        """_get_manager 返回 None（服务实例化失败）→ 容器落地失败 → blocked。"""
        guard = _make_guard()
        guard._get_manager = MagicMock(return_value=None)  # type: ignore[method-assign]
        result = await guard.execute(_ctx(_base_state()))

        contexts = result.state_updates["execution_contexts"]
        assert contexts[0]["blocked"] is True
        assert result.state_updates["isolation.blocked"] is True


# ============================================================
# state 容器绑定：执行环境显式数据，直读/失活重落地
# ============================================================


class TestStateBinding:
    @pytest.mark.asyncio
    async def test_首轮落地写入绑定(self) -> None:
        """无绑定时走落地，容器 id 写入 state（幂等键）。"""
        guard = _make_guard()
        guard._manager = _fake_manager("container-abc")
        result = await guard.execute(_ctx(_base_state()))

        assert result.state_updates["isolation.container_name"] == "container-abc"
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_state绑定存活_直读不落地(self) -> None:
        """state 已有绑定且验活通过 → 凭绑定注入，不走 get_or_create 落地。"""
        guard = _make_guard()
        manager = _binding_manager(alive=True)
        guard._manager = manager
        result = await guard.execute(
            _ctx(_base_state(**{"isolation.container_name": "container-bound"}))
        )

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-bound"
        assert manager._test_state["resolve_calls"] == 0
        # 绑定幂等回写，值不变
        assert result.state_updates["isolation.container_name"] == "container-bound"

    @pytest.mark.asyncio
    async def test_state绑定失活_重新落地并刷新绑定(self) -> None:
        """绑定验活失败（容器被删/失活）→ 重新落地，绑定刷新为新容器。"""
        guard = _make_guard()
        manager = _binding_manager(alive=False, resolve_id="container-new")
        guard._manager = manager
        result = await guard.execute(
            _ctx(_base_state(**{"isolation.container_name": "container-stale"}))
        )

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-new"
        assert manager._test_state["resolve_calls"] == 1
        assert result.state_updates["isolation.container_name"] == "container-new"

    @pytest.mark.asyncio
    async def test_state绑定验活异常_重新落地兜底(self) -> None:
        """验活调用本身抛异常（daemon 慢/服务异常）→ 视为失活，重新落地。"""
        guard = _make_guard()
        manager = _binding_manager(alive=True, resolve_id="container-abc")

        async def _boom(name: str) -> bool:
            raise RuntimeError("daemon slow")

        manager.ensure_container_alive = _boom
        guard._manager = manager
        result = await guard.execute(
            _ctx(_base_state(**{"isolation.container_name": "container-bound"}))
        )

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert manager._test_state["resolve_calls"] == 1
