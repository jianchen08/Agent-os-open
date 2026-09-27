# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
"""DockerProvider WSL 挂载源存在性校验回归测试。

背景（2026-09-27 R295 装机实证）：WSL docker 分支原注释假设「挂载不存在路径
docker 会报错」——实际 daemon 对不存在的 bind mount 源静默自动创建空目录，
容器内命令落空目录却以成功假象通过（悬空挂载被掩盖）。

契约：
1. _wsl_dir_exists：rc==0 → True；rc!=0 → False；探测异常/超时 → False
   （无法证实存在即拒绝，fail-closed）。
2. WSL docker（DOCKER_HOST=tcp://）+ 挂载源悬空 → create_environment 返回
   ERROR 环境、消息含「悬空」，不触发 docker create。
3. WSL docker + 挂载源存在 → 正常走 create 流程。
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests._isolation_path  # noqa: F401  # 注入 isolation 插件目录到 sys.path

from agentos_plugin_sdk.isolation_types import EnvironmentStatus, IsolationContext, TaskType
from providers.docker_provider import DockerProvider  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. _wsl_dir_exists 三态
# ---------------------------------------------------------------------------


def _run_ret(rc: int):
    done = MagicMock()
    done.returncode = rc
    return done


def test_wsl_dir_exists_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _run_ret(0))
    assert DockerProvider._wsl_dir_exists("/mnt/d/ws") is True


def test_wsl_dir_missing_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _run_ret(1))
    assert DockerProvider._wsl_dir_exists("/mnt/d/nope") is False


def test_wsl_probe_error_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测自身失败（超时/无法 spawn）与目录不存在同判（fail-closed）。"""
    import subprocess

    def _raise(*a: object, **k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="wsl", timeout=60)

    monkeypatch.setattr("subprocess.run", _raise)
    assert DockerProvider._wsl_dir_exists("/mnt/d/ws") is False


# ---------------------------------------------------------------------------
# 2. create_environment 的 WSL 悬空拦截
# ---------------------------------------------------------------------------


def _wsl_ctx(tmp_path, missing: bool) -> IsolationContext:
    ws = tmp_path / ("ghost__wt_deadbeef" if missing else "real_ws")
    if not missing:
        ws.mkdir()
    return IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, workspace=str(ws))


async def _create(provider, ctx):
    return await provider.create_environment(ctx, "cua-x")


@pytest.mark.asyncio
async def test_wsl_dangling_mount_refuses_create(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WSL docker + 挂载源悬空 → ERROR 环境带「悬空」，不触达 docker create。"""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    provider = DockerProvider()
    provider._wsl_docker_cache = True
    monkeypatch.setattr(DockerProvider, "_wsl_dir_exists", staticmethod(lambda p: False))
    provider._run_cmd = AsyncMock()
    provider._ensure_image = AsyncMock()

    env = await _create(provider, _wsl_ctx(tmp_path, missing=True))

    assert env.status == EnvironmentStatus.ERROR.value
    assert "悬空" in (env.provider_info or {}).get("error", "")
    provider._run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_wsl_existing_mount_proceeds(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WSL docker + 挂载源存在 → 正常走 create（不再一刀切跳过校验）。"""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    provider = DockerProvider()
    provider._wsl_docker_cache = True
    monkeypatch.setattr(DockerProvider, "_wsl_dir_exists", staticmethod(lambda p: True))

    async def fake_run(args, timeout=30):
        sub = args[1]
        if sub == "create":
            return 0, b"cid\n", b""
        return 0, b"", b""

    provider._run_cmd = fake_run
    provider._ensure_image = AsyncMock()

    env = await _create(provider, _wsl_ctx(tmp_path, missing=False))

    assert env.status == EnvironmentStatus.READY.value
