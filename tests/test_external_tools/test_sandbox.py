"""外部工具沙箱执行环境测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tools.external.exceptions import ExternalTimeoutError, SandboxError
from tools.external.sandbox import ExternalToolSandbox
from tools.external.types import SandboxResourceLimits


# ════════════════════════════════════════════
# 沙箱创建
# ════════════════════════════════════════════


class TestCreateSandbox:
    """沙箱创建测试。"""

    @pytest.mark.asyncio
    async def test_create_sandbox_success(self) -> None:
        """创建沙箱成功，返回 sandbox_id。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("test_tool")
        assert sid.startswith("ext_test_tool_")
        assert sandbox.get_sandbox_status(sid) == "ready"

    @pytest.mark.asyncio
    async def test_create_sandbox_with_limits(self) -> None:
        """自定义资源限制。"""
        sandbox = ExternalToolSandbox()
        limits = SandboxResourceLimits(cpu_limit=2.0, memory_limit_mb=1024)
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool", limits)
        info = sandbox.list_sandboxes()
        assert len(info) == 1
        assert info[0]["limits"]["cpu"] == 2.0
        assert info[0]["limits"]["memory_mb"] == 1024

    @pytest.mark.asyncio
    async def test_create_sandbox_default_limits(self) -> None:
        """默认资源限制。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool")
        info = sandbox.list_sandboxes()
        assert info[0]["limits"]["cpu"] == 1.0
        assert info[0]["limits"]["memory_mb"] == 512

    @pytest.mark.asyncio
    async def test_create_sandbox_failure_raises(self) -> None:
        """创建失败抛出 SandboxError。"""
        sandbox = ExternalToolSandbox()
        with patch.object(
            sandbox,
            "_create_isolation_environment",
            side_effect=RuntimeError("docker unavailable"),
        ):
            with pytest.raises(SandboxError) as exc_info:
                await sandbox.create_sandbox("tool")
            assert "创建失败" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_multiple_sandboxes(self) -> None:
        """创建多个沙箱。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            id1 = await sandbox.create_sandbox("tool_a")
            id2 = await sandbox.create_sandbox("tool_b")
        assert id1 != id2
        assert len(sandbox.list_sandboxes()) == 2


# ════════════════════════════════════════════
# 沙箱执行
# ════════════════════════════════════════════


class TestExecuteInSandbox:
    """沙箱中执行命令测试。"""

    @pytest.mark.asyncio
    async def test_execute_success_mock(self) -> None:
        """模拟沙箱执行成功。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool")
        result = await sandbox.execute_in_sandbox(sid, "echo hello")
        assert result["success"] is True
        assert "echo hello" in result["output"]

    @pytest.mark.asyncio
    async def test_execute_nonexistent_sandbox(self) -> None:
        """不存在的沙箱抛出 SandboxError。"""
        sandbox = ExternalToolSandbox()
        with pytest.raises(SandboxError):
            await sandbox.execute_in_sandbox("fake_id", "echo hi")

    @pytest.mark.asyncio
    async def test_execute_timeout(self) -> None:
        """执行超时抛出 ExternalTimeoutError。"""
        sandbox = ExternalToolSandbox()
        limits = SandboxResourceLimits(timeout_seconds=0.1)

        async def slow_exec(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(10)

        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool", limits)
        with patch.object(sandbox, "_execute_command", side_effect=slow_exec):
            with pytest.raises(ExternalTimeoutError) as exc_info:
                await sandbox.execute_in_sandbox(sid, "slow_cmd")
            assert "超时" in str(exc_info.value)
        assert sandbox.get_sandbox_status(sid) == "error"

    @pytest.mark.asyncio
    async def test_execute_custom_timeout(self) -> None:
        """自定义超时覆盖默认值。"""
        sandbox = ExternalToolSandbox()
        limits = SandboxResourceLimits(timeout_seconds=60.0)

        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool", limits)

        async def slow_exec(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(10)

        with patch.object(sandbox, "_execute_command", side_effect=slow_exec):
            with pytest.raises(ExternalTimeoutError):
                await sandbox.execute_in_sandbox(sid, "slow", timeout=0.1)

    @pytest.mark.asyncio
    async def test_execute_failure_sets_error_status(self) -> None:
        """执行失败状态变为 error。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool")
        with patch.object(sandbox, "_execute_command", side_effect=RuntimeError("crash")):
            with pytest.raises(SandboxError):
                await sandbox.execute_in_sandbox(sid, "bad_cmd")
        assert sandbox.get_sandbox_status(sid) == "error"


# ════════════════════════════════════════════
# 沙箱销毁
# ════════════════════════════════════════════


class TestDestroySandbox:
    """沙箱销毁测试。"""

    @pytest.mark.asyncio
    async def test_destroy_existing(self) -> None:
        """销毁存在的沙箱。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool")
        assert sandbox.get_sandbox_status(sid) is not None
        await sandbox.destroy_sandbox(sid)
        assert sandbox.get_sandbox_status(sid) is None

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_no_error(self) -> None:
        """销毁不存在的沙箱不报错。"""
        sandbox = ExternalToolSandbox()
        await sandbox.destroy_sandbox("fake_id")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_destroy_all(self) -> None:
        """销毁所有沙箱。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            await sandbox.create_sandbox("a")
            await sandbox.create_sandbox("b")
        assert len(sandbox.list_sandboxes()) == 2
        await sandbox.destroy_all()
        assert len(sandbox.list_sandboxes()) == 0


# ════════════════════════════════════════════
# 沙箱状态
# ════════════════════════════════════════════


class TestSandboxStatus:
    """沙箱状态查询测试。"""

    @pytest.mark.asyncio
    async def test_status_ready_after_create(self) -> None:
        """创建后状态为 ready。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            sid = await sandbox.create_sandbox("tool")
        assert sandbox.get_sandbox_status(sid) == "ready"

    @pytest.mark.asyncio
    async def test_status_nonexistent_returns_none(self) -> None:
        """不存在的沙箱返回 None。"""
        sandbox = ExternalToolSandbox()
        assert sandbox.get_sandbox_status("nope") is None

    @pytest.mark.asyncio
    async def test_list_sandboxes(self) -> None:
        """列出所有沙箱信息。"""
        sandbox = ExternalToolSandbox()
        with patch.object(sandbox, "_create_isolation_environment", return_value={"mock": True}):
            id1 = await sandbox.create_sandbox("tool_a")
            id2 = await sandbox.create_sandbox("tool_b")
        info = sandbox.list_sandboxes()
        assert len(info) == 2
        names = {i["tool_name"] for i in info}
        assert names == {"tool_a", "tool_b"}
