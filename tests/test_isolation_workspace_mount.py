"""工作空间挂载校验测试。

覆盖修复点（BUG-FIX-fix_20260625_isolated_no_workspace）：
- 容器隔离创建时，工作空间缺失 / 路径不存在 → 拒绝创建无挂载容器，
  返回 status=ERROR 的环境（而非静默创建一个不挂载、命令会落到空目录的容器）。
- ERROR 环境在 execute_in_environment 中直接返回清晰错误，
  不再以模糊的“容器ID不存在”掩盖真实原因。

涉及模块：src/isolation/providers/docker_provider.py
"""
import pytest

from isolation.providers.docker_provider import DockerProvider
from isolation.types import (
    EnvironmentStatus,
    IsolationContext,
    TaskType,
)


def _make_context(workspace: str | None = None) -> IsolationContext:
    """构造测试用隔离上下文。"""
    return IsolationContext(
        task_id="task-mount-test",
        task_type=TaskType.ATOMIC,
        workspace=workspace,
    )


class TestWorkspaceMountGuard:
    """容器创建时工作空间挂载校验：缺失/不存在即拒绝，绝不创建无挂载容器。"""

    @pytest.mark.asyncio
    async def test_empty_workspace_rejected(self):
        """工作空间为空 → 拒绝创建容器，返回 ERROR 环境。"""
        provider = DockerProvider()
        ctx = _make_context(workspace=None)

        env = await provider.create_environment(ctx, "cua-mount-test")

        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in env.provider_info["error"]

    @pytest.mark.asyncio
    async def test_nonexistent_workspace_rejected(self):
        """工作空间路径不存在 → 拒绝创建容器，返回 ERROR 环境。"""
        provider = DockerProvider()
        ctx = _make_context(workspace="/definitely/not/exist/xyz_224042d3b925")

        env = await provider.create_environment(ctx, "cua-mount-test")

        assert env.status == EnvironmentStatus.ERROR.value
        assert "路径不存在" in env.provider_info["error"]

    @pytest.mark.asyncio
    async def test_error_env_propagates_clear_error_on_execute(self):
        """ERROR 环境执行命令时返回其真实错误，而非“容器ID不存在”。"""
        provider = DockerProvider()
        ctx = _make_context(workspace=None)

        env = await provider.create_environment(ctx, "cua-mount-test")

        result = await provider.execute_in_environment(
            env.env_id, {"type": "command", "command": "ls"},
        )

        assert result.success is False
        assert "工作空间为空" in (result.error or "")
