"""外部工具沙箱执行测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.external.exceptions import ExternalTimeoutError, SandboxError
from tools.external.sandbox import ExternalToolSandbox
from tools.external.types import SandboxResourceLimits


@pytest.fixture
def sandbox() -> ExternalToolSandbox:
    return ExternalToolSandbox()


class TestExternalToolSandbox:

    @pytest.mark.asyncio
    async def test_create_sandbox(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            sid = await sandbox.create_sandbox("test_tool")
            assert sid.startswith("ext_test_tool_")
            assert sandbox.get_sandbox_status(sid) == "ready"

    @pytest.mark.asyncio
    async def test_create_sandbox_with_limits(self, sandbox: ExternalToolSandbox) -> None:
        limits = SandboxResourceLimits(cpu_limit=2.0, memory_limit_mb=1024)
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            sid = await sandbox.create_sandbox("limited_tool", resource_limits=limits)
            assert sandbox.get_sandbox_status(sid) == "ready"

    @pytest.mark.asyncio
    async def test_execute_in_sandbox(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            sid = await sandbox.create_sandbox("test_tool")
            result = await sandbox.execute_in_sandbox(sid, "echo hello")
            assert result["success"] is True
            assert "sandbox_id" in result

    @pytest.mark.asyncio
    async def test_execute_in_nonexistent_sandbox(self, sandbox: ExternalToolSandbox) -> None:
        with pytest.raises(SandboxError, match="沙箱不存在"):
            await sandbox.execute_in_sandbox("nonexistent_id", "echo")

    @pytest.mark.asyncio
    async def test_destroy_sandbox(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            sid = await sandbox.create_sandbox("test_tool")
            assert sandbox.get_sandbox_status(sid) is not None

            await sandbox.destroy_sandbox(sid)
            assert sandbox.get_sandbox_status(sid) is None

    @pytest.mark.asyncio
    async def test_destroy_nonexistent(self, sandbox: ExternalToolSandbox) -> None:
        # 销毁不存在的沙箱应不报错
        await sandbox.destroy_sandbox("nonexistent")

    @pytest.mark.asyncio
    async def test_list_sandboxes(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            await sandbox.create_sandbox("tool_a")
            await sandbox.create_sandbox("tool_b")

            sandboxes = sandbox.list_sandboxes()
            assert len(sandboxes) == 2

    @pytest.mark.asyncio
    async def test_destroy_all(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            await sandbox.create_sandbox("tool_a")
            await sandbox.create_sandbox("tool_b")

            await sandbox.destroy_all()
            assert sandbox.list_sandboxes() == []

    @pytest.mark.asyncio
    async def test_get_sandbox_status_nonexistent(self, sandbox: ExternalToolSandbox) -> None:
        assert sandbox.get_sandbox_status("nonexistent") is None

    @pytest.mark.asyncio
    async def test_create_failure(self, sandbox: ExternalToolSandbox) -> None:
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("环境创建失败"),
        ):
            with pytest.raises(SandboxError, match="沙箱创建失败"):
                await sandbox.create_sandbox("failing_tool")

    @pytest.mark.asyncio
    async def test_execute_timeout(self, sandbox: ExternalToolSandbox) -> None:
        """测试执行超时。"""
        with patch(
            "tools.external.sandbox.ExternalToolSandbox._create_isolation_environment",
            new_callable=AsyncMock,
            return_value={"sandbox_id": "mock", "mock": True},
        ):
            sid = await sandbox.create_sandbox("slow_tool")

        # Mock _execute_command 为无限等待
        async def slow_exec(*args, **kwargs):
            await asyncio.sleep(100)
            return {"success": True}

        with patch.object(sandbox, "_execute_command", side_effect=slow_exec):
            with pytest.raises(ExternalTimeoutError, match="超时"):
                await sandbox.execute_in_sandbox(sid, "slow_cmd", timeout=0.1)
