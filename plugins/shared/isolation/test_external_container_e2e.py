# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""外部容器通道真实 Docker e2e（判据 A2）。

全链路（真实 docker daemon，非桩）：
1. docker run 一个带已知文件的容器（python:3.12-alpine，/srv/task/known.txt）；
2. ExternalDockerProvider.create_environment 收养（inspect + mkdir /workspace）；
3. execute_in_environment：command 执行（echo + cat 已知文件）与 file read/write；
4. destroy_environment：容器保留（docker inspect 仍在，未 rm）；
5. 清场（测试 teardown docker rm -f）。

门控：本机 docker CLI/daemon 不可用时整文件 skip（环境限制显式声明，
不伪装绿）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    IsolationContext,
    OperationType,
    TaskType,
)

pytestmark = [pytest.mark.e2e, pytest.mark.unit]

_PLUGIN_DIR = Path(__file__).resolve().parent
_IMAGE = "python:3.12-alpine"


def _load_mod() -> Any:
    mod_name = "isolation_external_docker_provider_e2e"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _PLUGIN_DIR / "providers" / "external_docker_provider.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _docker_ok() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_docker(args: list[str], timeout: float = 120) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


DOCKER_AVAILABLE = _docker_ok()

pytestmark_e2e = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="本机 Docker 不可用（环境限制：通道行为由桩级测试覆盖）"
)


@pytest.fixture
def task_container() -> Any:
    """起一个带已知文件的容器（真实 docker run），teardown 强删。"""
    name = f"tb-e2e-{uuid.uuid4().hex[:8]}"
    rc, _, stderr = _run_docker(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            _IMAGE,
            "sh",
            "-c",
            "mkdir -p /srv/task && echo known-content-42 > /srv/task/known.txt && tail -f /dev/null",
        ]
    )
    assert rc == 0, f"docker run 失败: {stderr.decode('utf-8', errors='replace')}"
    yield name
    _run_docker(["docker", "rm", "-f", name], timeout=30)


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机 Docker 不可用（环境限制显式声明）")
class TestExternalContainerRealDocker:
    def test_full_roundtrip(self, task_container: str) -> None:
        """收养 → exec 命令 → 读已知文件 → 写新文件 → 销毁不删容器。"""
        mod = _load_mod()
        provider = mod.ExternalDockerProvider(task_container, workdir="/srv/task")
        ctx = IsolationContext(
            task_id="e2e-1",
            task_type=TaskType.ATOMIC,
            operation_type=OperationType.CODE_EXECUTION,
            workspace="D:\\eval\\e2e-ws",
        )

        async def _scenario() -> None:
            # 1. 收养
            env = await provider.create_environment(ctx)
            assert env.status == "ready"
            assert env.provider_info["container_name"] == task_container

            # 2. exec 命令（默认 cwd = 绑定 workdir）
            result = await provider.execute_in_environment(
                env.env_id, {"type": "command", "command": "cat /srv/task/known.txt"}
            )
            assert result.success is True, f"exec 失败: {result.error}"
            assert "known-content-42" in result.output["stdout"]

            # 3. 读文件（file_operation read）
            result = await provider.execute_in_environment(
                env.env_id,
                {"type": "file_operation", "operation": "read", "path": "/srv/task/known.txt"},
            )
            assert result.success is True
            assert "known-content-42" in result.output

            # 4. 写文件 + 读回断言
            result = await provider.execute_in_environment(
                env.env_id,
                {
                    "type": "file_operation",
                    "operation": "write",
                    "path": "/srv/task/agent-written.txt",
                    "content": "written-by-agentos",
                },
            )
            assert result.success is True, f"write 失败: {result.error}"
            result = await provider.execute_in_environment(
                env.env_id, {"type": "command", "command": "cat /srv/task/agent-written.txt"}
            )
            assert result.success is True
            assert "written-by-agentos" in result.output["stdout"]

            # 5. 销毁：解登记且容器保留（verifier 判分对象不被本系统删除）
            ok = await provider.destroy_environment(env.env_id)
            assert ok is True
            rc, stdout, _ = _run_docker(
                ["docker", "inspect", "-f", "{{.State.Status}}", task_container], timeout=15
            )
            assert rc == 0, "销毁后外部容器必须仍在（永不 docker rm）"
            assert stdout.decode().strip() == "running"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_scenario())
        finally:
            loop.close()

    def test_adopt_missing_container_explicit_error(self) -> None:
        """不存在的容器：收养显式失败（真实 daemon 确认 No such container）。"""
        mod = _load_mod()
        provider = mod.ExternalDockerProvider("tb-e2e-ghost-no-such")
        ctx = IsolationContext(
            task_id="e2e-2",
            task_type=TaskType.ATOMIC,
            operation_type=OperationType.CODE_EXECUTION,
        )

        async def _scenario() -> None:
            env = await provider.create_environment(ctx)
            assert env.status == "error"
            err_text = str(env.provider_info.get("error"))
            assert "外部容器不可达" in err_text
            # daemon 实际报文（不锁大小写）：docker inspect 不可达报 no such object
            assert "no such object" in err_text.lower()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_scenario())
        finally:
            loop.close()
