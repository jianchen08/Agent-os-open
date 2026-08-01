"""9p/drvfs EIO 自愈集成测试（post-exec：修宿主挂载 + 重建容器 + 重试）。

与 test_isolation_container_self_heal.py 的 setns 自愈测试（第 5 节）对称，
锁定 EIO 自愈的核心契约：
1. exec 命中 EIO → 修宿主挂载 + destroy + 重建 + 单次重试
2. 非 WSL docker 模式跳过宿主修复，仅重建容器
3. 普通命令失败不触发 EIO 自愈（防误判）
4. 自愈成功标记 io_error_recovered / host_mount_repaired 元数据

EIO 与 setns 的关键差异：EIO 根因在宿主 /mnt/<盘> 的 9p 通道，只重建容器
不够（新容器 bind mount 同一坏路径仍 EIO），故自愈需先 umount+mount 修宿主。
本测试 mock 掉 _repair_host_mount 与 _rebuild_and_retry_exec，聚焦于
_remount_and_retry_exec 的编排逻辑与 post-exec 钩子的触发条件。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from isolation.manager import IsolationManager
from isolation.providers.docker_provider import DockerProvider
from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    TaskType,
)

# 真实 EIO 样本（agent 任务报错原样）
_EIO_ERR = "ls: cannot access '/workspace/docs/working/x.md': Input/output error"


def _make_env_with_workspace(env_id="cua-ws", workspace="/mnt/d/myproject/ws"):
    """构造带 workspace 的 env（EIO 自愈需从 workspace 解析挂载盘）。"""
    return IsolationEnvironment(
        env_id=env_id,
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(
            task_id="t1",
            task_type=TaskType.ATOMIC,
            is_root_task=True,
            workspace=workspace,
        ),
    )


# ---------------------------------------------------------------------------
# 1. post-exec 钩子：EIO 触发 _remount_and_retry_exec（修宿主+重建+重试）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eio_triggers_remount_and_retry():
    """exec 命中 EIO → 触发 _remount_and_retry_exec（非 setns 自愈路径）。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    manager.get_or_create_environment = AsyncMock(return_value=env)
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(success=False, output={"stderr": _EIO_ERR}, error=_EIO_ERR),
    )
    provider.destroy_environment = AsyncMock(return_value=True)

    # mock _remount_and_retry_exec 验证它被触发，而非 _rebuild_and_retry_exec
    manager._remount_and_retry_exec = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"},
                                     metadata={"io_error_recovered": True}),
    )

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "ls"},
    )

    assert result.success is True
    manager._remount_and_retry_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_eio_not_triggered_on_normal_failure():
    """普通命令失败（非 EIO）→ 不触发 EIO 自愈。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    manager.get_or_create_environment = AsyncMock(return_value=env)
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(
            success=False, output={"stderr": "command not found"},
            error="command not found",
        ),
    )

    manager._remount_and_retry_exec = AsyncMock()
    manager._rebuild_and_retry_exec = AsyncMock()

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "badcmd"},
    )

    assert result.success is False
    manager._remount_and_retry_exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_eio_not_triggered_for_file_operation():
    """file_operation 的 EIO 不触发自愈（与 setns 覆盖范围对齐）。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    manager.get_or_create_environment = AsyncMock(return_value=env)
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(success=False, output=None, error=_EIO_ERR),
    )

    manager._remount_and_retry_exec = AsyncMock()

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "file_operation", "operation": "read", "path": "/workspace/x"},
    )

    assert result.success is False
    manager._remount_and_retry_exec.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. _remount_and_retry_exec：编排逻辑（WSL docker 模式修宿主 + 复用 setns 路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remount_and_retry_repairs_host_then_rebuilds():
    """WSL docker 模式：先修宿主挂载，再复用 _rebuild_and_retry_exec 重建+重试。

    成功时标记 io_error_recovered + host_mount_repaired。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    provider._is_wsl_docker = MagicMock(return_value=True)
    provider._resolve_mount_path = MagicMock(return_value="/mnt/d/myproject/ws")
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    rebuild_kwargs = {"task_id": "t1", "task_type": TaskType.ATOMIC,
                      "operation": {"type": "command", "command": "ls"}}

    manager._repair_host_mount = AsyncMock(return_value=True)  # 宿主修复成功
    manager._rebuild_and_retry_exec = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"},
                                     metadata={}),
    )

    result = await manager._remount_and_retry_exec(
        env, rebuild_kwargs, {"type": "command", "command": "ls"},
        original_error=_EIO_ERR,
    )

    assert result.success is True
    manager._repair_host_mount.assert_awaited_once_with("/mnt/d/myproject/ws", env.env_id)
    manager._rebuild_and_retry_exec.assert_awaited_once()
    assert result.metadata.get("io_error_recovered") is True
    assert result.metadata.get("host_mount_repaired") is True


@pytest.mark.asyncio
async def test_remount_and_retry_skips_host_repair_for_non_wsl_docker():
    """非 WSL docker 模式（Docker Desktop）：跳过宿主修复，仅重建容器。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    provider._is_wsl_docker = MagicMock(return_value=False)
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    rebuild_kwargs = {"task_id": "t1", "task_type": TaskType.ATOMIC,
                      "operation": {"type": "command", "command": "ls"}}

    manager._repair_host_mount = AsyncMock()
    manager._rebuild_and_retry_exec = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"},
                                     metadata={}),
    )

    result = await manager._remount_and_retry_exec(
        env, rebuild_kwargs, {"type": "command", "command": "ls"},
        original_error=_EIO_ERR,
    )

    assert result.success is True
    manager._repair_host_mount.assert_not_awaited()  # 非 WSL 不修宿主
    assert result.metadata.get("io_error_recovered") is True
    assert result.metadata.get("host_mount_repaired") is not True  # 未修宿主


@pytest.mark.asyncio
async def test_remount_and_retry_still_rebuilds_when_host_repair_fails():
    """宿主修复失败 → 不放弃，仍走 _rebuild_and_retry_exec（兜底只重建容器）。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    provider._is_wsl_docker = MagicMock(return_value=True)
    provider._resolve_mount_path = MagicMock(return_value="/mnt/d/myproject/ws")
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    rebuild_kwargs = {"task_id": "t1", "task_type": TaskType.ATOMIC,
                      "operation": {"type": "command", "command": "ls"}}

    manager._repair_host_mount = AsyncMock(return_value=False)  # 宿主修复失败
    manager._rebuild_and_retry_exec = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"},
                                     metadata={}),
    )

    result = await manager._remount_and_retry_exec(
        env, rebuild_kwargs, {"type": "command", "command": "ls"},
        original_error=_EIO_ERR,
    )

    assert result.success is True
    manager._repair_host_mount.assert_awaited_once()
    manager._rebuild_and_retry_exec.assert_awaited_once()  # 仍重建
    assert result.metadata.get("io_error_recovered") is True
    assert result.metadata.get("host_mount_repaired") is not True  # 宿主未修好


@pytest.mark.asyncio
async def test_remount_and_retry_no_metadata_when_rebuild_fails():
    """重建+重试仍失败 → 不标记 io_error_recovered（仅成功才标记）。"""
    manager = IsolationManager(providers={})
    provider = MagicMock(spec=DockerProvider)
    provider._is_wsl_docker = MagicMock(return_value=True)
    provider._resolve_mount_path = MagicMock(return_value="/mnt/d/myproject/ws")
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env_with_workspace()
    rebuild_kwargs = {"task_id": "t1", "task_type": TaskType.ATOMIC,
                      "operation": {"type": "command", "command": "ls"}}

    manager._repair_host_mount = AsyncMock(return_value=True)
    manager._rebuild_and_retry_exec = AsyncMock(
        return_value=ExecutionResult(success=False, output=None, error=_EIO_ERR, metadata={}),
    )

    result = await manager._remount_and_retry_exec(
        env, rebuild_kwargs, {"type": "command", "command": "ls"},
        original_error=_EIO_ERR,
    )

    assert result.success is False
    assert result.metadata.get("io_error_recovered") is not True
