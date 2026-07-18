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
    """created 容器 start 成功 → 探针通过 → 返回 READY 环境。"""
    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "created"
    container.start = MagicMock()
    container.reload = MagicMock()
    container.id = "abc123"
    container.attrs = {"Mounts": []}
    container.exec_run = MagicMock(return_value=(0, b""))  # 活性探针通过

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is not None
    assert result.status == EnvironmentStatus.READY.value
    container.start.assert_called_once()
    container.reload.assert_called_once()


@pytest.mark.asyncio
async def test_find_existing_container_running_reused_directly():
    """running 容器无需 start，探针通过后直接复用为 READY。"""
    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "running"
    container.start = MagicMock()
    container.id = "abc123"
    container.attrs = {"Mounts": []}
    container.exec_run = MagicMock(return_value=(0, b""))  # 活性探针通过

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is not None
    assert result.status == EnvironmentStatus.READY.value
    container.start.assert_not_called()  # running 不需要 start


@pytest.mark.asyncio
async def test_find_existing_container_running_desync_returns_none():
    """running 但 runc 命名空间脱节的容器 → 探针 exec 失败 → 删容器返回 None 触发新建。

    背景（第三层根因）：setns 脱节时 docker inspect 仍报 running，
    _find_existing_container 信任该 status 直接复用坏容器 → setns 自愈的重建
    实际捡回同一个坏容器 → 重试仍失败 → 自愈循环空转。修复：running 容器
    加 exec 探针，setns 脱节则当坏容器删掉返回 None。
    """
    from docker.errors import DockerException

    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "running"
    container.id = "abc123"
    container.attrs = {"Mounts": []}
    # 探针 exec 暴露 setns 脱节（真实样本：exec_run 抛 APIError）
    _setns_err = DockerException(
        "OCI runtime exec failed: exec failed: unable to start container process: "
        "error executing setns process: exit status 1"
    )
    container.exec_run = MagicMock(side_effect=_setns_err)
    container.remove = MagicMock()

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    # 坏容器不复用：返回 None 让上层新建
    assert result is None
    # 尝试删掉坏容器（即使 rm 可能失败也要试，触发上层新建）
    container.remove.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_find_existing_container_running_healthy_probe_ok():
    """running 且探针 exec 成功的容器 → 正常复用为 READY（探针不能误伤健康容器）。"""
    manager = IsolationManager(providers={})
    container = MagicMock()
    container.status = "running"
    container.id = "abc123"
    container.attrs = {"Mounts": []}
    # 健康容器：探针 exec 成功（exit_code=0）
    container.exec_run = MagicMock(return_value=(0, b""))

    with patch("docker.from_env", return_value=_fake_client(container)):
        result = await manager._find_existing_container("cua-ws")

    assert result is not None
    assert result.status == EnvironmentStatus.READY.value
    container.remove.assert_not_called()  # 健康容器不删


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


# ---------------------------------------------------------------------------
# 5. IsolationManager.execute_in_isolation：post-exec 命名空间脱节自愈
# ---------------------------------------------------------------------------

# runc setns 脱节的真实 stderr（取自 healthcheck 日志原样）
_SETNS_DESYNC_ERR = (
    "OCI runtime exec failed: exec failed: unable to start container process: "
    "error executing setns process: exit status 1"
)


@pytest.mark.asyncio
async def test_execute_rebuilds_on_setns_namespace_desync():
    """exec 命中 setns 脱节 → destroy + 重建 + 单次重试 → 在新环境执行成功。

    背景：setns 脱节时 docker inspect 仍报 running，pre-exec 健康检查放行，
    错误只在 exec 时冒泡。本用例锁定 post-exec 自愈：检测到 setns 标记后
    透明重建容器并重试一次。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock()
    manager._providers[IsolationLevel.CONTAINER] = provider

    dead_env = _make_env(env_id="cua-ws")
    healthy_env = _make_env(env_id="cua-ws-new")

    # pre-exec 健康检查放行（setns 脱节时 inspect 仍报 running）
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    # 重建路径：首次返回脱节 env，重建返回健康 env
    manager.get_or_create_environment = AsyncMock(side_effect=[dead_env, healthy_env])
    # 真实流程中 get_or_create_environment 会把 env 注册进 manager._environments；
    # 这里 mock 了该方法，故手动注册脱节 env，使 destroy_environment 能找到它。
    manager._environments["cua-ws"] = dead_env
    # 首次 exec 命中 setns，重试成功
    provider.execute_in_environment = AsyncMock(
        side_effect=[
            ExecutionResult(success=False, output={"stderr": _SETNS_DESYNC_ERR}, error=_SETNS_DESYNC_ERR),
            ExecutionResult(success=True, output={"stdout": "ok"}),
        ]
    )
    provider.destroy_environment = AsyncMock(return_value=True)  # rm 成功，可重建

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "ls"},
    )

    assert result.success is True
    # 重建过一次（get_or_create_environment 被调 2 次：初次 + 重建）
    assert manager.get_or_create_environment.call_count == 2
    # 销毁过脱节的旧 env
    provider.destroy_environment.assert_called_once_with("cua-ws", success=False)
    # 重试在新环境上执行
    second_call_args = provider.execute_in_environment.call_args_list[1]
    assert second_call_args.args[0] == "cua-ws-new"
    # 标记本次结果为自愈恢复
    assert result.metadata.get("namespace_desync_recovered") is True


@pytest.mark.asyncio
async def test_no_rebuild_on_normal_command_failure():
    """普通命令失败（非 setns 脱节）→ 不触发重建，原样返回失败。

    防误判：只有 runc 命名空间脱节才值得重建容器，命令本身的 stderr
    （如 command not found）重试无意义。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock()
    manager._providers[IsolationLevel.CONTAINER] = provider

    env = _make_env(env_id="cua-ws")
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    manager.get_or_create_environment = AsyncMock(return_value=env)
    # 普通命令失败，非 setns 脱节
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(
            success=False, output={"stderr": "sh: command not found: foo"},
            error="sh: command not found: foo",
        ),
    )
    provider.destroy_environment = AsyncMock()

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "foo"},
    )

    assert result.success is False
    # 未重建
    assert manager.get_or_create_environment.call_count == 1
    provider.destroy_environment.assert_not_called()
    assert result.metadata.get("namespace_desync_recovered") is not True


@pytest.mark.asyncio
async def test_setns_no_rebuild_loop_when_destroy_fails():
    """setns 脱节但坏容器删不掉（runc 卡死 rm -f 失败）→ 不空转重建，明确报错。

    背景（第三层根因闭环）：runc 卡死的容器 docker rm -f 会失败，且同名新容器
    create 必冲突（容器名唯一）。旧实现 destroy 不检查返回码、谎报"已销毁"，
    重建走 get_or_create_environment 又被 _find_existing_container 捡回坏容器
    （信任假 running）→ 自愈循环空转 10 次仍失败。修复后：destroy 返回 False 时
    不进重建（重建注定失败），直接返回明确错误提示需重启 docker，避免空转。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock()
    manager._providers[IsolationLevel.CONTAINER] = provider

    dead_env = _make_env(env_id="cua-ws")
    provider.get_environment_status = AsyncMock(return_value=EnvironmentStatus.READY)
    manager.get_or_create_environment = AsyncMock(return_value=dead_env)
    manager._environments["cua-ws"] = dead_env
    provider.execute_in_environment = AsyncMock(
        return_value=ExecutionResult(
            success=False, output={"stderr": _SETNS_DESYNC_ERR}, error=_SETNS_DESYNC_ERR,
        ),
    )
    # rm -f 失败：runc 卡死删不掉
    provider.destroy_environment = AsyncMock(return_value=False)

    result = await manager.execute_in_isolation(
        task_id="t1", task_type=TaskType.ATOMIC,
        operation={"type": "command", "command": "ls"},
    )

    assert result.success is False
    # destroy 失败 → 不进重建（不空转），get_or_create 只调一次（初次）
    assert manager.get_or_create_environment.call_count == 1
    # 明确标记：坏容器删不掉，需重启 docker
    assert result.metadata.get("namespace_desync_unremovable") is True


# ---------------------------------------------------------------------------
# 6. DockerProvider.destroy_environment：rm 失败不谎报（第三层根因诚实性修复）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_environment_honest_on_rm_failure():
    """docker rm -f 失败（runc 卡死删不掉）→ destroy 不谎报成功、保留 env 记录。

    背景（第三层根因）：runc 命名空间脱节的容器 docker rm -f 会失败
    （could not kill container ... did not receive an exit event），
    但旧实现不检查 rm 返回码，照样 log "容器已销毁" 并 pop 掉 env 记录，
    造成"内存里以为删了、docker 里还在"的状态脱节。修复：rm 非零时
    不谎报、不 pop env（保留记录供排查），返回 False。
    """
    provider = DockerProvider()
    # rm -f 返回非零（runc 卡死的真实 stderr）
    async def fake_run(args, timeout=30):
        if args[1] == "rm":
            return 1, b"", (
                b"Error response from daemon: cannot remove container: "
                b"could not kill container: tried to kill container, "
                b"but did not receive an exit event"
            )
        return 0, b"", b""

    provider._run_cmd = fake_run

    # 注册一个 env 让 destroy 能找到它
    from isolation.types import IsolationContext
    env = IsolationEnvironment(
        env_id="cua-ws",
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(
            task_id="t1", task_type=TaskType.ATOMIC, is_root_task=True,
        ),
        provider_info={"container_id": "abc123"},
    )
    provider._environments["cua-ws"] = env

    destroyed_ok = await provider.destroy_environment("cua-ws", success=False)

    # rm 失败：诚实返回 False
    assert destroyed_ok is False
    # 不谎报：env 记录保留（docker 里容器仍在，记录不能丢）
    assert "cua-ws" in provider._environments


@pytest.mark.asyncio
async def test_destroy_environment_true_on_rm_success():
    """docker rm -f 成功 → 返回 True、正常 pop env 记录。"""
    provider = DockerProvider()
    provider._run_cmd = _make_run_cmd(rm_rc=0)[0]

    from isolation.types import IsolationContext
    env = IsolationEnvironment(
        env_id="cua-ws",
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(
            task_id="t1", task_type=TaskType.ATOMIC, is_root_task=True,
        ),
        provider_info={"container_id": "abc123"},
    )
    provider._environments["cua-ws"] = env

    destroyed_ok = await provider.destroy_environment("cua-ws")

    assert destroyed_ok is True
    assert "cua-ws" not in provider._environments
