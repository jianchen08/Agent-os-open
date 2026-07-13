"""容器自愈回归测试。

背景 BUG：容器反复 "container is not running" 卡死，由三处缺陷叠加导致：
1. DockerProvider.create_environment 丢弃 docker start 返回值，start 失败
   （WSL2 挂载脏路径 mkdir: file exists）后容器卡在 created(exit 128)，却被
   标记 READY → 后续 docker exec 报 "is not running"。
2. IsolationManager._find_existing_container 对 created 状态不处理，无条件
   标 READY，使卡死容器被反复误信复用。
3. execute_in_isolation 执行前无健康检查，从不自愈。

修复（见 src/isolation/providers/docker_provider.py、src/isolation/manager.py）：
- _create_and_start：start 失败删除卡死容器并重建重试一次；仍失败标记 ERROR。
- _find_existing_container：created/exited 启动失败 → 删容器返回 None 触发新建。
- execute_in_isolation：执行前 inspect 复核状态，非 READY 透明重建一次。

本测试锁定核心契约：start 失败可自愈、误信异常态被纠正、执行前健康兜底。
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


# ---------------------------------------------------------------------------
# 工具：构造按子命令分支的 _run_cmd 替身
# ---------------------------------------------------------------------------


def _make_run_cmd(create_rc=0, create_out=b"cid\n", start_results=None,
                  rm_rc=0):
    """构造 _run_cmd 替身。

    Args:
        create_rc/create_out: docker create 的返回码与 stdout
        start_results: docker start 的返回码序列（list），按调用顺序消费；
                       首次失败即触发 _create_and_start 的 rm+重建重试。
        rm_rc: docker rm 的返回码
    """
    start_results = list(start_results or [0])
    log: list[str] = []

    async def fake_run(args, timeout=30):
        sub = args[1]
        log.append(sub)
        if sub == "create":
            return create_rc, create_out, b""
        if sub == "start":
            rc = start_results.pop(0) if start_results else 0
            return rc, b"", b"mkdir: file exists" if rc != 0 else b""
        if sub == "rm":
            return rm_rc, b"", b""
        return 0, b"", b""

    return fake_run, log


# ---------------------------------------------------------------------------
# 1. DockerProvider._create_and_start：start 失败删除卡死容器并重建重试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_start_retries_after_start_failure():
    """start 首次失败 → rm 卡死容器 → 重建重试成功 → 返回新 container_id。"""
    provider = DockerProvider()
    fake_run, log = _make_run_cmd(start_results=[1, 0])  # 第一次失败，重试成功
    provider._run_cmd = fake_run

    cid, err = await provider._create_and_start("cua-ws", ["--name", "cua-ws", "img"])

    assert cid == "cid"
    assert err == ""
    # create → start(失败) → rm → create → start(成功)
    assert log == ["create", "start", "rm", "create", "start"]


@pytest.mark.asyncio
async def test_create_and_start_returns_empty_when_start_always_fails():
    """重试后 start 仍失败 → 返回空 container_id + 真实错误。"""
    provider = DockerProvider()
    fake_run, log = _make_run_cmd(start_results=[1, 1])  # 两次都失败
    provider._run_cmd = fake_run

    cid, err = await provider._create_and_start("cua-ws", ["--name", "cua-ws", "img"])

    assert cid == ""
    assert "mkdir" in err
    # 重试失败后应清理第二次创建的卡死容器
    assert log == ["create", "start", "rm", "create", "start", "rm"]


@pytest.mark.asyncio
async def test_create_and_start_no_retry_on_first_success():
    """首次 start 成功 → 不触发重试，直接返回。"""
    provider = DockerProvider()
    fake_run, log = _make_run_cmd(start_results=[0])
    provider._run_cmd = fake_run

    cid, err = await provider._create_and_start("cua-ws", ["--name", "cua-ws", "img"])

    assert cid == "cid"
    assert log == ["create", "start"]


# ---------------------------------------------------------------------------
# 2. DockerProvider.create_environment：端到端状态正确性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_environment_ready_after_retry_success(tmp_path):
    """start 失败重试成功 → 环境标记 READY（而非误标后卡死）。"""
    provider = DockerProvider()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, workspace=str(ws))

    fake_run, _ = _make_run_cmd(start_results=[1, 0])  # 重试成功
    provider._run_cmd = fake_run
    provider._ensure_image = AsyncMock()

    env = await provider.create_environment(ctx, "cua-ws")

    assert env.status == EnvironmentStatus.READY.value
    assert env.provider_info["container_id"] == "cid"


@pytest.mark.asyncio
async def test_create_environment_error_when_start_always_fails(tmp_path):
    """start 重试仍失败 → 环境标记 ERROR，携带真实 stderr（不再误标 READY）。"""
    provider = DockerProvider()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, workspace=str(ws))

    fake_run, _ = _make_run_cmd(start_results=[1, 1])  # 两次都失败
    provider._run_cmd = fake_run
    provider._ensure_image = AsyncMock()

    env = await provider.create_environment(ctx, "cua-ws")

    assert env.status == EnvironmentStatus.ERROR.value
    assert "mkdir" in env.provider_info["error"]


# ---------------------------------------------------------------------------
# 3. IsolationManager._find_existing_container：不误信 created 异常态
# ---------------------------------------------------------------------------


def _fake_client(container):
    """构造 docker.from_env 返回的假 client。"""
    client = MagicMock()
    client.containers.get = MagicMock(return_value=container)
    client.close = MagicMock()
    return client


@pytest.mark.asyncio
async def test_find_existing_container_returns_none_when_start_fails():
    """created 容器 start 失败（挂载脏路径）→ 删容器返回 None，触发上层新建。"""
    from docker.errors import DockerException

    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "created"  # 从未启动成功
    container.start = MagicMock(side_effect=DockerException("mkdir: file exists"))
    container.remove = MagicMock()

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is None
    container.remove.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_find_existing_container_starts_created_then_ready():
    """created 容器 start 成功 → 返回 READY 环境。"""
    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "created"
    container.start = MagicMock()
    container.reload = MagicMock()
    container.id = "abc123"
    container.attrs = {"Mounts": []}

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is not None
    assert result.status == EnvironmentStatus.READY.value
    container.start.assert_called_once()
    container.reload.assert_called_once()


@pytest.mark.asyncio
async def test_find_existing_container_running_reused_directly():
    """running 容器无需 start，直接复用为 READY。"""
    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "running"
    container.start = MagicMock()
    container.id = "abc123"
    container.attrs = {"Mounts": []}

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is not None
    assert result.status == EnvironmentStatus.READY.value
    container.start.assert_not_called()  # running 不需要 start


# ---------------------------------------------------------------------------
# 4. IsolationManager.execute_in_isolation：执行前健康检查 + 透明自愈
# ---------------------------------------------------------------------------


def _make_env(env_id="cua-ws", status=EnvironmentStatus.READY.value):
    return IsolationEnvironment(
        env_id=env_id,
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=status,
        context=IsolationContext(
            task_id="t1", task_type=TaskType.ATOMIC, is_root_task=True,
        ),
    )


@pytest.mark.asyncio
async def test_execute_rebuilds_when_container_not_ready():
    """执行前检测到非 READY → 透明重建 → 用新环境执行。"""
    manager = IsolationManager(providers={})
    provider = MagicMock()
    manager._providers[IsolationLevel.CONTAINER] = provider

    dead_env = _make_env(env_id="cua-ws")
    healthy_env = _make_env(env_id="cua-ws-new")

    # 首次返回卡死 env，重建返回健康 env
    manager.get_or_create_environment = AsyncMock(
        side_effect=[dead_env, healthy_env],
    )
    # 健康检查判定死容器非 READY，触发重建
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.ERROR)
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"}),
    )
    provider.destroy_environment = AsyncMock()

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "ls"},
    )

    assert result.success is True
    # 重建过一次
    assert manager.get_or_create_environment.call_count == 2
    # 用重建后的新 env 执行，而非卡死的旧 env
    provider.execute_in_environment.assert_called_once_with(
        "cua-ws-new", {"type": "command", "command": "ls"},
    )


@pytest.mark.asyncio
async def test_execute_no_rebuild_when_healthy():
    """容器就绪 → 不重建，直接执行。"""
    manager = IsolationManager(providers={})
    provider = MagicMock()
    manager._providers[IsolationLevel.CONTAINER] = provider

    healthy_env = _make_env(env_id="cua-ws")
    manager.get_or_create_environment = AsyncMock(return_value=healthy_env)
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(success=True, output={"stdout": "ok"}),
    )

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "ls"},
    )

    assert result.success is True
    # 未重建
    assert manager.get_or_create_environment.call_count == 1
    provider.execute_in_environment.assert_called_once_with(
        "cua-ws", {"type": "command", "command": "ls"},
    )
