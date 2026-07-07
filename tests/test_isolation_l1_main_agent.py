"""L1 主 agent bash_execute 路由测试。

核心契约：
- L1 主 agent（agent_level 缺省或 <=1）的 bash_execute 一律走 host，
  不进容器。主 agent 没有任务工作空间（直接在 project_root 操作），
  强制进容器会因无 workspace 被 tool_core 拒绝（报"工作空间未解析"）。
- 子任务（L2+）不受影响，仍按 policy/metadata 决策走容器或 host。

背景：bash_execute 工具级 policy 写死 isolation=isolated，且 isolation_guard
原决策只看 policy + 任务 isolation_level，不看 agent 层级，导致 L1 主 agent
的 bash_execute 被错误路由进容器。
"""
from unittest.mock import MagicMock, patch

import pytest

from isolation.types import IsolationLevel
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.input.isolation_guard.plugin import IsolationGuard


def _make_guard(docker_available: bool = True) -> IsolationGuard:
    """构造 IsolationGuard，并让 decider 对 bash_execute 返回 container 策略。"""
    with patch("isolation.decider.IsolationDecider"):
        guard = IsolationGuard(config={"docker_available": docker_available})
    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    guard._decider.resolve = MagicMock(return_value=mock_policy)
    guard._get_task_metadata = MagicMock(return_value={"isolation_level": "isolated"})
    return guard


def _make_ctx(agent_level: str | None = None) -> PluginContext:
    """构造 tool_execute 上下文，可选注入 agent_level。"""
    state: dict = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {"command": "ls"}}],
    }
    if agent_level is not None:
        state[StateKeys.AGENT_LEVEL] = agent_level
    return PluginContext(state=state, config={}, _services={})


# ============================================================================
# 1. _is_main_agent 层级判定
# ============================================================================


class TestIsMainAgent:
    """_is_main_agent 的层级解析与边界。"""

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (None, True),      # 缺省按 L1
            ("L1", True),      # 主 agent
            ("l1", True),      # 大小写不敏感
            ("1", True),       # 纯数字
            ("L2", False),     # 子任务
            ("L3", False),     # 深层子任务
            ("L10", False),    # 远端子任务
            ("garbage", True), # 解析失败按 L1（保守，走 host 审批）
        ],
    )
    def test_level_resolution(self, level, expected):
        assert IsolationGuard._is_main_agent({"agent_level": level}) is expected

    def test_missing_key_defaults_to_main(self):
        """state 完全没有 agent_level 键 → 按主 agent 处理。"""
        assert IsolationGuard._is_main_agent({}) is True

    def test_fallback_context_key(self):
        """StateKeys.AGENT_LEVEL 缺失时回退到 context.agent_level。"""
        assert IsolationGuard._is_main_agent({"context.agent_level": "L2"}) is False


# ============================================================================
# 2. L1 主 agent bash_execute 路由到 host
# ============================================================================


class TestL1RoutesToHost:
    """L1 主 agent 的 bash_execute 一律走 host，不进容器。"""

    @pytest.mark.asyncio
    async def test_l1_bash_execute_routes_to_host(self):
        """L1 + bash_execute + docker 可用 → host（不被塞进容器）。"""
        guard = _make_guard(docker_available=True)
        result = await guard.execute(_make_ctx("L1"))

        contexts = result.state_updates.get("execution_contexts", [])
        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "l1_main_agent_host"
        assert contexts[0].get("blocked") is not True

    @pytest.mark.asyncio
    async def test_no_level_treated_as_l1(self):
        """agent_level 缺失 → 按主 agent → host。"""
        guard = _make_guard(docker_available=True)
        result = await guard.execute(_make_ctx(None))

        contexts = result.state_updates.get("execution_contexts", [])
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "l1_main_agent_host"

    @pytest.mark.asyncio
    async def test_l1_routes_to_host_even_when_docker_down(self):
        """L1 + docker 不可用 → 仍 host（主 agent 不依赖 docker）。"""
        guard = _make_guard(docker_available=False)
        result = await guard.execute(_make_ctx("L1"))

        contexts = result.state_updates.get("execution_contexts", [])
        assert contexts[0]["provider"] == "host"
        assert contexts[0].get("blocked") is not True


# ============================================================================
# 3. 子任务（L2+）不受影响
# ============================================================================


class TestSubtaskUnaffected:
    """L2+ 子任务的 bash_execute 仍按 policy/metadata 决策。"""

    @pytest.mark.asyncio
    async def test_l2_goes_to_docker_when_available(self):
        """L2 + docker 可用 → 容器执行（主 agent 路由不应波及子任务）。"""
        guard = _make_guard(docker_available=True)
        result = await guard.execute(_make_ctx("L2"))

        contexts = result.state_updates.get("execution_contexts", [])
        assert contexts[0]["provider"] == "docker"

    @pytest.mark.asyncio
    async def test_l2_blocked_when_docker_unavailable(self):
        """L2 + docker 不可用 → blocked（不降级，保持原有契约）。"""
        guard = _make_guard(docker_available=False)
        result = await guard.execute(_make_ctx("L2"))

        contexts = result.state_updates.get("execution_contexts", [])
        assert contexts[0].get("blocked") is True
        assert contexts[0]["provider"] == "denied"
