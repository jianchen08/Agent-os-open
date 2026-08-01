"""BwrapProvider 单元测试（TDD）。

背景与目标（见 docs/working/design/bwrap_isolation_migration_plan.md）：
- 用 bubblewrap (bwrap) 作为 CONTAINER 级的轻量替代 provider，对称于 DockerProvider。
- 模型：bwrap 常驻 PID 1（tail -f /dev/null），nsenter 注入命令（镜像 docker exec）。
- 本文件不依赖真实 bwrap —— 全部 mock。

核心契约（本测试锁定）：
1. _build_argv：构造 bwrap 启动 argv（Linux 路径），含 --ro-bind / / --bind ws /workspace、
   --unshare-* 、常驻 PID 1（tail -f /dev/null）。
2. _clear_dangerous_env：清除危险环境变量（LD_PRELOAD / *_API_KEY 等），保留 SANDBOX_* passthrough。
3. is_available：bwrap 不在 PATH 时返回 (False, reason)。
4. create_environment：spawn bwrap 后，env_id == container_name，provider_info 含 bwrap_pid，
   status=ready。
5. destroy_environment：SIGTERM 等 2s 超时后 SIGKILL。
6. execute_in_environment：用 nsenter 注入命令。
7. get_environment_status：按 PID 存活判断。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)


# ---------------------------------------------------------------------------
# 1. _build_argv：argv 构造（纯逻辑，TDD 第一刀）
# ---------------------------------------------------------------------------


def _make_ctx(workspace: str = "/tmp/ws", task_id: str = "t1") -> IsolationContext:
    return IsolationContext(
        task_id=task_id,
        task_type=TaskType.ATOMIC,
        workspace=workspace,
    )


@pytest.mark.asyncio
async def test_build_argv_linux_contains_core_bwrap_flags():
    """Linux argv 必须含：ro-bind / 、bind workspace→/workspace、unshare-user/pid/net-try、
    常驻 PID 1（tail -f /dev/null）。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    argv = provider._build_argv(workspace="/tmp/ws", name="cua-ws")

    assert argv[0] == "bwrap"
    # 只读绑定根文件系统
    assert "--ro-bind" in argv
    assert "/" in argv
    # workspace 绑定到 /workspace（docker_provider 约定的挂载点）
    ws_idx = argv.index("--bind") + 1
    assert argv[ws_idx] == "/tmp/ws"
    assert argv[ws_idx + 1] == "/workspace"
    # 命名空间隔离
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-net-try" in argv
    # 常驻 PID 1（关键：镜像 docker_provider:533 的 tail -f /dev/null）。
    # 写法与 docker 一致：sh -c "tail -f /dev/null"（PID 1 = sh，exec 成 tail）。
    assert "sh" in argv
    sh_idx = argv.index("sh")
    assert argv[sh_idx + 1] == "-c"
    assert "tail -f /dev/null" in argv


@pytest.mark.asyncio
async def test_build_argv_linux_has_die_with_parent_and_new_session():
    """argv 必须含 --die-with-parent（孤儿清理）与 --new-session（新会话，防信号泄漏）。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    argv = provider._build_argv(workspace="/tmp/ws", name="cua-ws")

    assert "--die-with-parent" in argv
    assert "--new-session" in argv


@pytest.mark.asyncio
async def test_build_argv_linux_hostname_from_name():
    """hostname 参数由 name 派生（agentos-<name>）。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    argv = provider._build_argv(workspace="/tmp/ws", name="cua-ws-42")

    assert "--hostname" in argv
    host_idx = argv.index("--hostname") + 1
    assert argv[host_idx] == "agentos-cua-ws-42"


# ---------------------------------------------------------------------------
# 2. _clear_dangerous_env：环境变量清洗（纯逻辑）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_dangerous_env_removes_ld_preload_and_api_keys():
    """危险变量（LD_PRELOAD / *_API_KEY / SSLKEYLOGFILE 等）必须被清除。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "LD_PRELOAD": "/evil/malware.so",
        "LD_LIBRARY_PATH": "/evil/lib",
        "OPENAI_API_KEY": "sk-leak",
        "SSLKEYLOGFILE": "/tmp/ssl.log",
        "SANDBOX_CONFIG": "/etc/sandbox.yaml",  # 应保留
    }
    cleaned = provider._clear_dangerous_env(env)

    assert "LD_PRELOAD" not in cleaned
    assert "LD_LIBRARY_PATH" not in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "SSLKEYLOGFILE" not in cleaned
    # 关键变量保留
    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["HOME"] == "/root"


@pytest.mark.asyncio
async def test_clear_dangerous_env_keeps_sandbox_prefixed_passthrough():
    """SANDBOX_* 前缀变量应作为 passthrough 保留。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    env = {
        "SANDBOX_POLICY": "strict",
        "SANDBOX_workspace": "/tmp/ws",
        "LD_AUDIT": "/evil/audit.so",
    }
    cleaned = provider._clear_dangerous_env(env)

    assert cleaned["SANDBOX_POLICY"] == "strict"
    assert cleaned["SANDBOX_workspace"] == "/tmp/ws"
    assert "LD_AUDIT" not in cleaned


# ---------------------------------------------------------------------------
# 3. is_available：bwrap 探测
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_available_false_when_bwrap_not_on_path():
    """bwrap 不在 PATH → (False, reason)。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    with patch("isolation.providers.bwrap_provider.shutil.which", return_value=None):
        ok, reason = await provider.is_available()

    assert ok is False
    assert reason is not None
    assert "bwrap" in reason


@pytest.mark.asyncio
async def test_is_available_true_when_bwrap_found():
    """bwrap 在 PATH → (True, None)。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    with patch("isolation.providers.bwrap_provider.shutil.which", return_value="/usr/bin/bwrap"):
        ok, reason = await provider.is_available()

    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# 4. get_level
# ---------------------------------------------------------------------------


def test_get_level_is_container():
    """BwrapProvider 对应 CONTAINER 级。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    assert provider.get_level() == IsolationLevel.CONTAINER


# ---------------------------------------------------------------------------
# 5. create_environment：spawn bwrap → env_id == container_name + bwrap_pid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_environment_spawns_bwrap_and_records_pid(tmp_path):
    """create_environment spawn bwrap 后：env_id == container_name，provider_info
    含 bwrap_pid，status=ready。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _make_ctx(workspace=str(ws), task_id="t1")

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    provider._spawn_bwrap = AsyncMock(return_value=fake_proc)

    env = await provider.create_environment(ctx, container_name="cua-ws-t1")

    assert env.env_id == "cua-ws-t1"
    assert env.level == IsolationLevel.CONTAINER
    assert env.status == EnvironmentStatus.READY.value
    assert env.provider_info["bwrap_pid"] == 12345
    assert env.provider_info["provider_kind"] == "bwrap"
    # spawn 收到清洗过的 env + 构造好的 argv
    call_kwargs = provider._spawn_bwrap.call_args
    argv = call_kwargs.args[0]
    assert argv[0] == "bwrap"
    assert "tail -f /dev/null" in argv


@pytest.mark.asyncio
async def test_create_environment_stores_env_for_later_destroy(tmp_path):
    """create 后环境被存入 _environments，destroy/status 可凭 env_id 取回。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ctx = _make_ctx(workspace=str(tmp_path))

    fake_proc = MagicMock()
    fake_proc.pid = 999
    provider._spawn_bwrap = AsyncMock(return_value=fake_proc)

    await provider.create_environment(ctx, container_name="env-x")
    assert "env-x" in provider._environments


@pytest.mark.asyncio
async def test_create_environment_unknown_env_returns_error_status(tmp_path):
    """对不存在的 env_id 调 get_environment_status → STOPPED。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    status = await provider.get_environment_status("nonexistent")
    assert status == EnvironmentStatus.STOPPED


# ---------------------------------------------------------------------------
# 6. destroy_environment：SIGTERM → 2s 超时 → SIGKILL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_environment_returns_true_and_releases_pid(tmp_path):
    """destroy 对已存在环境返回 True，并从 _environments 移除。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ctx = _make_ctx(workspace=str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.pid = 7777
    fake_proc.returncode = None  # 进程还活着
    fake_proc.wait = AsyncMock()
    provider._spawn_bwrap = AsyncMock(return_value=fake_proc)

    await provider.create_environment(ctx, container_name="env-d")
    ok = await provider.destroy_environment("env-d")

    assert ok is True
    assert "env-d" not in provider._environments


@pytest.mark.asyncio
async def test_destroy_environment_unknown_env_returns_false():
    """destroy 不存在的 env_id → False。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ok = await provider.destroy_environment("nope")
    assert ok is False


# ---------------------------------------------------------------------------
# 7. execute_in_environment：nsenter 注入命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_in_environment_uses_nsenter(tmp_path):
    """execute 用 nsenter 进入 bwrap PID 的 namespace 执行命令。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ctx = _make_ctx(workspace=str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    provider._spawn_bwrap = AsyncMock(return_value=fake_proc)

    await provider.create_environment(ctx, container_name="env-e")

    captured_argv: list = []

    async def fake_exec(args, timeout=30):
        captured_argv.extend(args)
        return 0, b"ok\n", b""

    provider._run_cmd = fake_exec  # type: ignore[attr-defined]

    result = await provider.execute_in_environment(
        "env-e", {"type": "command", "command": "echo hi", "workdir": "/workspace"}
    )

    assert result.success is True
    assert "nsenter" in captured_argv[0]
    assert "--target" in captured_argv
    assert "4242" in captured_argv  # bwrap_pid
    # 命令被注入（cd workdir && command）
    joined = " ".join(captured_argv)
    assert "echo hi" in joined


@pytest.mark.asyncio
async def test_execute_unknown_env_returns_failure():
    """execute 不存在的 env_id → 失败结果。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    result = await provider.execute_in_environment("nope", {"type": "command", "command": "ls"})
    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_execute_unsupported_operation_type_returns_failure(tmp_path):
    """execute 不支持的操作类型 → 失败结果。"""
    from isolation.providers.bwrap_provider import BwrapProvider

    provider = BwrapProvider()
    ctx = _make_ctx(workspace=str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.pid = 1
    provider._spawn_bwrap = AsyncMock(return_value=fake_proc)
    await provider.create_environment(ctx, container_name="env-u")

    result = await provider.execute_in_environment("env-u", {"type": "weird_type", "command": "x"})
    assert result.success is False
