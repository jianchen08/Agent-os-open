"""会话主管道降级测试 — 无 workspace 时 bash_execute 降级 host 执行。

测试覆盖：
- bash_execute + state 无 workspace（会话主管道）+ Docker 可用 → 降级 host
- bash_execute + state 有 workspace（任务子管道）+ Docker 可用 → 维持 docker
- 非 bash_execute 工具 + 无 workspace → 不触发降级（保持原 policy 决策）
- 无 workspace 降级 + 命令含宿主路径 → host_path_detected 优先（先匹配）
- Docker 不可用 + 无 workspace → 不走降级，按 docker_unavailable 拒绝

背景：会话主管道（主 agent 经 WS 直接对话）整条链路从不注入 workspace，
而容器执行必须挂载 workspace，空 workspace 会被 tool_core 硬拒绝
（"工作空间未解析"）。降级到 host：host 不依赖挂载，且 security_check 的
白名单/审批机制能正常工作（safe_commands 放行 / dangerous_commands 审批）。
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

from tests.suites.plugins.conftest import load_module_from_file

_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))


def _load(module_name, rel_path):
    """加载指定模块。"""
    return load_module_from_file(
        module_name,
        os.path.join(_SRC_DIR, *rel_path),
    )


def _make_ctx(state=None, services=None):
    """创建 Mock PluginContext。"""
    return PluginContext(
        state=state or {},
        config={},
        _services=services or {},
    )


def _make_isolation_guard(config=None):
    """创建 IsolationGuard 实例（patch 掉 decider 避免真实策略加载）。"""
    mod = _load("isolation_guard_downgrade", ["plugins", "input", "isolation_guard", "plugin.py"])
    with patch("isolation.decider.IsolationDecider"):
        return mod.IsolationGuard(config=config)


def _mock_container_policy(plugin):
    """模拟 isolation=container 的策略（bash_execute 默认配置）。"""
    from isolation.types import IsolationLevel

    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    plugin._decider.resolve = MagicMock(return_value=mock_policy)
    return plugin


# ============================================================================
# 1. 会话主管道（无 workspace）bash_execute 降级 host
# ============================================================================


class TestSessionPipelineDowngrade:
    """会话主管道无 workspace 时 bash_execute 降级到 host。"""

    @pytest.mark.asyncio
    async def test_no_workspace_downgrades_to_host(self):
        """bash_execute + state 无 workspace → provider=host，reason=no_workspace_downgrade。"""
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        # state 完全没有 workspace 键（会话主管道场景）
        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls -la"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "no_workspace_downgrade"
        assert contexts[0].get("blocked") is not True

    @pytest.mark.asyncio
    async def test_empty_string_workspace_downgrades_to_host(self):
        """bash_execute + state['workspace'] 为空字符串 → 同样降级 host。

        会话主管道经 engine.run(workspace="") 注入，state["workspace"] 是空字符串而非缺失。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            "workspace": "",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "echo hello"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "no_workspace_downgrade"

    @pytest.mark.asyncio
    async def test_non_bash_tool_no_workspace_not_downgraded(self):
        """非 bash_execute 工具 + 无 workspace → 不触发降级，保持原 policy 决策。

        降级只针对 bash_execute（唯一会进容器执行的命令工具）。
        其它工具本身 policy 就是 host，不进容器，无 workspace 不影响。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        # file_read 的 policy 是 host（不走容器）
        from isolation.types import IsolationLevel

        mock_policy = MagicMock()
        mock_policy.isolation = IsolationLevel.HOST
        guard._decider.resolve = MagicMock(return_value=mock_policy)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "file_read", "args": {"path": "some/file.txt"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        # file_read 本来就是 host，reason 是 policy 而非 no_workspace_downgrade
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "policy"


# ============================================================================
# 2. 任务子管道（有 workspace）维持容器隔离
# ============================================================================


class TestTaskPipelineNotAffected:
    """任务子管道有 workspace，bash_execute 仍走容器隔离（不受降级影响）。"""

    @pytest.mark.asyncio
    async def test_with_workspace_stays_docker(self):
        """bash_execute + state['workspace'] 有值 → 仍进容器（provider=docker）。"""
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            "workspace": "/data/workspaces/container_036fa__wt_726def4a",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls -la /workspace"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "docker"
        assert contexts[0]["reason"] == "policy"

    @pytest.mark.asyncio
    async def test_with_workspace_host_path_still_routes_host(self):
        """任务管道有 workspace + 命令含宿主路径 → host_path_detected 优先于 no_workspace。

        两个降级规则并存时，host_path_detected 先匹配（会话主管道场景下两者都命中，
        但有 workspace 时 host_path 仍应优先生效，因为命令确实访问了容器内不存在的宿主路径）。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            "workspace": "/data/workspaces/some_task",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls D:/myproject/"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        # 有 workspace 时 host_path 规则生效，不是 no_workspace_downgrade
        assert contexts[0]["reason"] == "host_path_detected"


# ============================================================================
# 3. 降级优先级：host_path 与 no_workspace 共存时
# ============================================================================


class TestDowngradePriority:
    """无 workspace 会话主管道场景下，host_path 与 no_workspace 的优先级。"""

    @pytest.mark.asyncio
    async def test_no_workspace_with_host_path_prefers_host_path(self):
        """会话主管道（无 workspace）+ 命令含宿主路径 → host_path_detected 优先。

        host_path 检测在 no_workspace 降级之前执行，二者都命中时
        host_path_detected 先返回（原因更精确：命令确实访问宿主路径）。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            # 无 workspace（会话主管道）+ 命令含 D:/ 盘符路径
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls D:/myproject/"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        # 两个降级规则都指向 host，host_path 更精确的原因优先
        assert contexts[0]["reason"] == "host_path_detected"


# ============================================================================
# 4. Docker 不可用时不走降级（保持拒绝语义）
# ============================================================================


class TestDockerUnavailableNotDowngraded:
    """Docker 不可用 + 无 workspace 时，不走 no_workspace 降级。

    Docker 不可用的分支（policy_isolation == CONTAINER and not docker_available）
    在 docker_available 分支之前不执行，而是走 docker_unavailable 拒绝逻辑。
    no_workspace 降级只在 docker_available 分支内，因此 docker 不可用时
    不触发降级，维持"要求容器即拒绝"的语义。
    """

    @pytest.mark.asyncio
    async def test_docker_unavailable_no_workspace_denied(self):
        """Docker 不可用 + 无 workspace → 拒绝执行（denied），不降级 host。"""
        guard = _make_isolation_guard(config={"docker_available": False})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls -la"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        # Docker 不可用时拒绝，不降级到 host
        assert contexts[0]["provider"] == "denied"
        assert contexts[0].get("blocked") is True
