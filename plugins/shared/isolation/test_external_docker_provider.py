# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""providers/external_docker_provider.py 测试（Terminal-Bench 外部容器通道）。

覆盖：
1. 构造：空 container_ref 拒绝 / 正常属性（container_ref/workdir/get_level）；
2. 收养验证：inspect_target（成功解析 Id|Name / 容器不存在 rc!=0 显式失败）；
3. 生命周期：create_environment（收养成功 READY + provider_info 完整 /
   容器不可达 ERROR 显式失败不降级 / ensure /workspace 失败不阻断收养）；
4. 销毁语义：destroy_environment **永不 docker rm**（无任何 rm 调用，恒 True，
   登记清除）；
5. 执行：execute_in_environment（command 成功输出断言 / command 失败带 stderr /
   空 command 拒绝 / file read/write/exists / 不支持类型 / 环境缺失 / ERROR
   环境直接报 provider_error）；
6. working_dir 语义：command 缺省用绑定 workdir（非 /workspace 的绑定值）。

隔离策略：isolation_types 用真实模块；docker CLI 属外部依赖，_run_cmd 用
记录型替身（_CmdScript），不 mock 被测对象。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationLevel,
    OperationType,
    TaskType,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_mod() -> Any:
    """动态加载 providers/external_docker_provider.py（唯一模块名防冲突）。"""
    mod_name = "isolation_external_docker_provider_test"
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


_MOD = _load_mod()
ExternalDockerProvider = _MOD.ExternalDockerProvider


def _run(coro: Any) -> Any:
    """共享测试进程中其他测试可能关闭主 loop，须自建独立 loop。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(task_id: str = "t-1", workspace: str | None = "ws") -> IsolationContext:
    return IsolationContext(
        task_id=task_id,
        task_type=TaskType.ATOMIC,
        operation_type=OperationType.CODE_EXECUTION,
        workspace=workspace,
    )


class _CmdScript:
    """_run_cmd 记录型替身：按脚本顺序返回 (rc, stdout, stderr) 并记录调用。"""

    def __init__(self, results: list[tuple[int, bytes, bytes]]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        self.calls.append(list(args))
        return self.results.pop(0)


def _provider(container: str = "harbor-main", workdir: str = "/app") -> Any:
    return ExternalDockerProvider(container, workdir=workdir)


def _attach(provider: Any, script: _CmdScript) -> None:
    provider._run_cmd = script  # type: ignore[method-assign]


# ── 1. 构造 ─────────────────────────────────────────────────────


class TestInit:
    def test_empty_container_ref_rejected(self) -> None:
        with pytest.raises(ValueError, match="container_ref"):
            ExternalDockerProvider("")
        with pytest.raises(ValueError, match="container_ref"):
            ExternalDockerProvider("   ")

    def test_basic_properties(self) -> None:
        provider = _provider()
        assert provider.container_ref == "harbor-main"
        assert provider.workdir == "/app"
        assert provider.get_level() == IsolationLevel.CONTAINER


# ── 2/3. 收养验证与生命周期 ─────────────────────────────────────


class TestCreateEnvironment:
    def test_adopt_success_ready_env(self) -> None:
        provider = _provider("tb-task-1", workdir="/app")
        inspect_out = b"abc123def|/tb_task_1\n"
        script = _CmdScript(
            [
                (0, inspect_out, b""),  # docker inspect
                (0, b"", b""),  # mkdir -p /workspace
            ]
        )
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        assert env.status == EnvironmentStatus.READY.value
        assert env.provider_type == "external_docker"
        assert env.env_id == "tb_task_1"  # inspect .Name 剥前导斜杠
        assert env.provider_info["container_id"] == "abc123def"
        assert env.provider_info["container_name"] == "tb_task_1"
        assert env.provider_info["workdir"] == "/app"
        assert env.provider_info["external"] is True
        # mkdir -p /workspace 确实发出
        assert any(args[0:4] == ["docker", "exec", "tb_task_1", "mkdir"] for args in script.calls)

    def test_adopt_missing_container_explicit_error_no_fallback(self) -> None:
        provider = _provider("ghost-container")
        script = _CmdScript([(1, b"", b"Error: No such container: ghost-container")])
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        # 显式失败：ERROR 状态 + 错误信息可辨，绝不造出 READY 环境
        assert env.status == EnvironmentStatus.ERROR.value
        assert "No such container" in str(env.provider_info.get("error"))
        assert "外部容器不可达" in str(env.provider_info.get("error"))
        # 全程只有 inspect 一次调用：没有 create/run 兜底
        assert len(script.calls) == 1
        assert script.calls[0][0:2] == ["docker", "inspect"]

    def test_adopt_ensure_workspace_failure_non_blocking(self) -> None:
        provider = _provider("tb-task-2")
        script = _CmdScript(
            [
                (0, b"deadbeef|/tb-task-2\n", b""),
                (1, b"", b"mkdir failed"),  # ensure /workspace 失败
            ]
        )
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        # mkdir 失败只留痕，不阻断收养（exec 时错误会显式冒泡）
        assert env.status == EnvironmentStatus.READY.value


# ── 4. 销毁语义（通道命门：永不删外部容器） ─────────────────────


class TestDestroyNeverDeletes:
    def test_destroy_unregisters_and_never_runs_docker_rm(self) -> None:
        provider = _provider("tb-task-3")
        script = _CmdScript(
            [
                (0, b"id123|/tb-task-3\n", b""),
                (0, b"", b""),
            ]
        )
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        assert env.env_id == "tb-task-3"
        ok = _run(provider.destroy_environment("tb-task-3"))
        assert ok is True
        assert provider._environments == {}
        # 销毁全程零 docker 调用（收养消耗 2 次：inspect + mkdir；destroy 0 次，
        # 尤其没有 docker rm）
        assert len(script.calls) == 2
        assert not any("rm" in args for args in script.calls)

    def test_destroy_unknown_env_idempotent_no_calls(self) -> None:
        provider = _provider("tb-task-4")
        script = _CmdScript([])
        _attach(provider, script)
        ok = _run(provider.destroy_environment("never-existed"))
        assert ok is True
        assert script.calls == []


# ── 5. 执行面 ───────────────────────────────────────────────────


class TestExecute:
    def _ready_provider(self, container: str = "tb-run") -> tuple[Any, _CmdScript]:
        provider = _provider(container, workdir="/srv/task")
        script = _CmdScript(
            [
                (0, b"cid|/tb-run\n", b""),
                (0, b"", b""),
            ]
        )
        _attach(provider, script)
        _run(provider.create_environment(_ctx()))
        return provider, script

    def test_command_success_output_asserted(self) -> None:
        provider, script = self._ready_provider()
        script.results = [(0, b"hello from container\n", b"")]
        result = _run(
            provider.execute_in_environment(
                "tb-run", {"type": "command", "command": "echo hello from container"}
            )
        )
        assert result.success is True
        assert result.output["stdout"] == "hello from container\n"
        assert result.output["return_code"] == 0
        assert result.error is None
        # exec -w 用绑定 workdir（/srv/task），不是缺省 /workspace
        exec_call = script.calls[-1]
        assert exec_call[0:5] == ["docker", "exec", "-w", "/srv/task", "tb-run"]
        assert exec_call[5:8] == ["sh", "-c", "echo hello from container"]

    def test_command_failure_carries_stderr(self) -> None:
        provider, script = self._ready_provider()
        script.results = [(2, b"", b"ls: cannot access '/nope': No such file")]
        result = _run(
            provider.execute_in_environment(
                "tb-run", {"type": "command", "command": "ls /nope"}
            )
        )
        assert result.success is False
        assert "No such file" in str(result.error)
        assert result.output["return_code"] == 2

    def test_command_working_dir_override(self) -> None:
        provider, script = self._ready_provider()
        script.results = [(0, b"ok\n", b"")]
        _run(
            provider.execute_in_environment(
                "tb-run",
                {"type": "command", "command": "true", "working_dir": "/tmp/x"},
            )
        )
        assert script.calls[-1][3] == "/tmp/x"

    def test_empty_command_rejected(self) -> None:
        provider, _ = self._ready_provider()
        result = _run(provider.execute_in_environment("tb-run", {"type": "command"}))
        assert result.success is False
        assert "命令不能为空" in str(result.error)

    def test_unknown_env_rejected(self) -> None:
        provider, _ = self._ready_provider()
        result = _run(provider.execute_in_environment("nope", {"type": "command", "command": "x"}))
        assert result.success is False
        assert "环境不存在" in str(result.error)

    def test_error_env_surfaces_provider_error(self) -> None:
        provider = _provider("ghost-x")
        script = _CmdScript([(1, b"", b"No such container")])
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "x"})
        )
        assert result.success is False
        assert "外部容器不可达" in str(result.error)

    def test_unsupported_operation_type(self) -> None:
        provider, _ = self._ready_provider()
        result = _run(provider.execute_in_environment("tb-run", {"type": "teleport"}))
        assert result.success is False
        assert "不支持的操作类型" in str(result.error)

    def test_file_read_write_exists(self) -> None:
        provider, script = self._ready_provider()
        # read：cat 输出文件内容
        script.results = [(0, b"file-content-42\n", b"")]
        result = _run(
            provider.execute_in_environment(
                "tb-run",
                {"type": "file_operation", "operation": "read", "path": "/srv/task/out.txt"},
            )
        )
        assert result.success is True
        assert result.output == "file-content-42\n"

        # write：mkdir + python3 argv 写入（path/content 独立传参）
        script.results = [(0, b"", b""), (0, b"", b"")]
        result = _run(
            provider.execute_in_environment(
                "tb-run",
                {
                    "type": "file_operation",
                    "operation": "write",
                    "path": "/srv/task/new.txt",
                    "content": "line1\nline2",
                },
            )
        )
        assert result.success is True
        write_call = script.calls[-1]
        assert write_call[0:2] == ["docker", "exec"]
        # argv 形状：[docker, exec, container, python3, -c, <写脚本>, path, encoded]
        assert write_call[-2] == "/srv/task/new.txt"
        assert "line1" in write_call[-1]

        # exists：命中 yes 分支
        script.results = [(0, b"yes\n", b"")]
        result = _run(
            provider.execute_in_environment(
                "tb-run",
                {"type": "file_operation", "operation": "exists", "path": "/srv/task/new.txt"},
            )
        )
        assert result.success is True
        assert result.output == {"exists": True}

    def test_file_read_propagates_error(self) -> None:
        provider, script = self._ready_provider()
        script.results = [(1, b"", b"cat: /missing: No such file or directory")]
        result = _run(
            provider.execute_in_environment(
                "tb-run",
                {"type": "file_operation", "operation": "read", "path": "/missing"},
            )
        )
        assert result.success is False
        assert "No such file" in str(result.error)


# ── 6. 状态探针 ─────────────────────────────────────────────────


class TestGetStatus:
    def test_status_mapping(self) -> None:
        provider = _provider("tb-st")
        script = _CmdScript(
            [
                (0, b"cid|/tb-st\n", b""),
                (0, b"", b""),
                (0, b"running\n", b""),
                (0, b"exited\n", b""),
                (1, b"", b"No such container"),
            ]
        )
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))
        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.READY
        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.STOPPED
        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.ERROR


# ── 7. 可用性与异常分支（覆盖率棘轮） ──────────────────────────


class TestAvailabilityAndErrorBranches:
    def test_is_available_no_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        provider = _provider("tb-av")
        ok, err = _run(provider.is_available())
        assert ok is False
        assert err is not None
        assert "未安装" in err

    def test_is_available_daemon_down(self) -> None:
        provider = _provider("tb-av")
        _attach(provider, _CmdScript([(1, b"", b"daemon down")]))
        ok, err = _run(provider.is_available())
        assert ok is False
        assert err is not None
        assert "daemon down" in err

    def test_is_available_ok(self) -> None:
        provider = _provider("tb-av")
        version_line = b"29.6.1" + b"\n"
        _attach(provider, _CmdScript([(0, version_line, b"")]))
        ok, err = _run(provider.is_available())
        assert ok is True
        assert err is None

    def test_exec_timeout_returns_timeout_error(self) -> None:
        import subprocess as _sp

        provider = _provider("tb-to")
        inspect_line = b"cid|/tb-to" + b"\n"
        script = _CmdScript([(0, inspect_line, b""), (0, b"", b"")])
        _attach(provider, script)
        _run(provider.create_environment(_ctx()))

        def _timeout(args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
            raise _sp.TimeoutExpired(args, timeout)

        provider._run_cmd = _timeout  # type: ignore[method-assign]
        result = _run(
            provider.execute_in_environment("tb-to", {"type": "command", "command": "x"})
        )
        assert result.success is False
        assert "超时" in str(result.error)

    def test_exec_general_exception_surfaced(self) -> None:
        provider = _provider("tb-ex")
        inspect_line = b"cid|/tb-ex" + b"\n"
        script = _CmdScript([(0, inspect_line, b""), (0, b"", b"")])
        _attach(provider, script)
        _run(provider.create_environment(_ctx()))

        def _boom(args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
            raise OSError("docker client exploded")

        provider._run_cmd = _boom  # type: ignore[method-assign]
        result = _run(
            provider.execute_in_environment("tb-ex", {"type": "command", "command": "x"})
        )
        assert result.success is False
        assert "exploded" in str(result.error)

    def test_write_failure_raises_and_surfaces(self) -> None:
        provider = _provider("tb-wf")
        inspect_line = b"cid|/tb-wf" + b"\n"
        script = _CmdScript(
            [
                (0, inspect_line, b""),  # inspect
                (0, b"", b""),  # mkdir /workspace
                (0, b"", b""),  # mkdir 目标目录
                (1, b"", b"python3: not found"),  # 写入失败
            ]
        )
        _attach(provider, script)
        _run(provider.create_environment(_ctx()))
        result = _run(
            provider.execute_in_environment(
                "tb-wf",
                {
                    "type": "file_operation",
                    "operation": "write",
                    "path": "/srv/f.txt",
                    "content": "x",
                },
            )
        )
        assert result.success is False
        assert "写文件失败" in str(result.error)

    def test_status_probe_exception_maps_error(self) -> None:
        provider = _provider("tb-se")
        inspect_line = b"cid|/tb-se" + b"\n"
        script = _CmdScript([(0, inspect_line, b""), (0, b"", b"")])
        _attach(provider, script)
        env = _run(provider.create_environment(_ctx()))

        def _explode(args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
            raise TimeoutError("daemon frozen")

        provider._run_cmd = _explode  # type: ignore[method-assign]
        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.ERROR
