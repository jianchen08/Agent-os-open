# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation providers/docker_provider.py 测试（providers 包覆盖补）。

覆盖：
1. 配置：__init__ 默认/自定义 config、_BUILD_CACHE_VOLUMES 常量；
2. WSL 判定与路径转换：_is_wsl_docker（缓存/各 DOCKER_HOST 前缀）、
   _resolve_mount_path（空/非 WSL/WSL 标准盘符/WSL 非标准路径）；
3. 可用性：is_available（无 CLI/成功/daemon 未运行/FileNotFound/超时/异常）；
4. _run_cmd：非流式（成功/env 合并）、流式（成功/超时）；
5. 生命周期：create_environment（成功/空工作空间/路径不存在/WSL 跳过校验/
   创建失败）、destroy_environment（未知/无 container_id/成功/rm 失败/异常）、
   get_environment_status（未知/无 id/各状态/异常）；
6. 执行：execute_in_environment（环境缺失/错误环境/无 id/分派/不支持类型）、
   _exec_in_container（空命令/成功/非零/超时/异常）、_file_op_in_container
   （read/write/exists 两态/不支持/异常）、_read/_write_container_file；
7. 镜像：_ensure_image（已存在/构建成功/构建失败 raise/auto_build 关 pull/
   pull 失败 raise）；
8. 容器创建重试：_create_and_start（成功/start 失败重建成功/重建仍失败）；
9. 错误识别：_is_namespace_desync_error/_is_io_error（None/bytes/str/不匹配）。

隔离策略：isolation_types 用真实模块；docker CLI 属外部依赖，mock 仅限
subprocess/shutil.which/os.environ；内部方法（_run_cmd/_ensure_image 等）在
测试其调用方时用记录型替身，自身行为单独直测。
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from plugins.shared.system.isolation.isolation_types import (
        EnvironmentStatus,
        ExecutionResult,
        IsolationContext,
        IsolationEnvironment,
        IsolationLevel,
        OperationType,
        TaskType,
    )
    from plugins.shared.system.isolation.providers.docker_provider import DockerProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/

# ── 真实 isolation_types（纯 dataclass/枚举，无外部依赖） ──
_isolation_types_mod = importlib.util.spec_from_file_location(
    "isolation_types", _PLUGIN_DIR / "isolation_types.py"
)
assert _isolation_types_mod is not None and _isolation_types_mod.loader is not None
ISOLATION_TYPES = importlib.util.module_from_spec(_isolation_types_mod)
sys.modules["isolation_types"] = ISOLATION_TYPES
_isolation_types_mod.loader.exec_module(ISOLATION_TYPES)

if not TYPE_CHECKING:
    # 运行期：仍从动态加载的 isolation_types 取真实类（mypy 走上方静态导入）。
    EnvironmentStatus = ISOLATION_TYPES.EnvironmentStatus
    ExecutionResult = ISOLATION_TYPES.ExecutionResult
    IsolationContext = ISOLATION_TYPES.IsolationContext
    IsolationEnvironment = ISOLATION_TYPES.IsolationEnvironment
    IsolationLevel = ISOLATION_TYPES.IsolationLevel
    OperationType = ISOLATION_TYPES.OperationType
    TaskType = ISOLATION_TYPES.TaskType


def _load_mod() -> Any:
    """动态加载 providers/docker_provider.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_docker_provider_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "providers" / "docker_provider.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    DockerProvider = _MOD.DockerProvider


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


def _provider(config: dict[str, Any] | None = None) -> Any:
    return DockerProvider(config)


class _CmdScript:
    """_run_cmd 记录型替身：按脚本顺序返回 (rc, stdout, stderr)。"""

    def __init__(self, results: list[tuple[int, bytes, bytes]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        args: list[str],
        timeout: float = 30,
        env: dict[str, str] | None = None,
        stream_log: bool = False,
    ) -> tuple[int, bytes, bytes]:
        self.calls.append({"args": list(args), "timeout": timeout, "env": env, "stream_log": stream_log})
        return self.results.pop(0)


class TestInit:
    def test_default_config(self, tmp_path: Path) -> None:
        provider = _provider()
        assert provider._image == "agentos:latest"
        assert provider._cpu_limit == "1.0"
        assert provider._memory_limit == "512m"
        # swap 默认 = memory（禁止 swap 超额）
        assert provider._memory_swap == provider._memory_limit
        assert provider._pids_limit == 100
        assert provider._network_mode == "bridge"
        assert provider._publish_ports == []
        assert provider._workspace_mount is True
        assert provider._max_docker_concurrency == 4
        assert provider._auto_build is True
        assert provider._build_timeout == 1800.0
        assert provider._extra_cache_volumes == []
        # 默认 dockerfile/build_context 相对仓库根解析
        assert provider._dockerfile_path == provider._repo_root / "docker" / "agentos" / "Dockerfile"
        assert provider._build_context == provider._repo_root / "."

    def test_custom_config(self, tmp_path: Path) -> None:
        config = {
            "image": "myimg:1",
            "cpu_limit": "2.0",
            "memory_limit": "1g",
            "memory_swap": "2g",
            "pids_limit": 200,
            "network_mode": "host",
            "publish_ports": ["8080:8080"],
            "workspace_mount": False,
            "max_docker_concurrency": 2,
            "auto_build": False,
            "dockerfile_path": str(tmp_path / "df"),
            "build_context": str(tmp_path / "ctx"),
            "build_timeout": 60,
            "extra_cache_volumes": [("vol-x", "/x")],
        }
        provider = _provider(config)
        assert provider._image == "myimg:1"
        assert provider._cpu_limit == "2.0"
        assert provider._memory_limit == "1g"
        assert provider._memory_swap == "2g"
        assert provider._pids_limit == 200
        assert provider._network_mode == "host"
        assert provider._publish_ports == ["8080:8080"]
        assert provider._workspace_mount is False
        assert provider._max_docker_concurrency == 2
        assert provider._auto_build is False
        # 绝对路径原样保留
        assert provider._dockerfile_path == Path(str(tmp_path / "df"))
        assert provider._build_context == Path(str(tmp_path / "ctx"))
        assert provider._build_timeout == 60.0
        assert provider._extra_cache_volumes == [("vol-x", "/x")]

    def test_build_cache_volumes_constant(self) -> None:
        vols = DockerProvider._BUILD_CACHE_VOLUMES
        assert len(vols) >= 4  # Rust/Node/Python/Go 至少四组
        assert all(isinstance(v, tuple) and len(v) == 2 and v[0] and v[1] for v in vols)

    def test_get_level(self) -> None:
        assert _provider().get_level() == IsolationLevel.CONTAINER


class TestIsWslDocker:
    def test_cache_hit_true(self, monkeypatch: Any) -> None:
        provider = _provider()
        provider._wsl_docker_cache = True
        monkeypatch.setenv("DOCKER_HOST", "")
        assert provider._is_wsl_docker() is True

    def test_cache_hit_false(self, monkeypatch: Any) -> None:
        provider = _provider()
        provider._wsl_docker_cache = False
        monkeypatch.setenv("DOCKER_HOST", "tcp://localhost:2375")
        assert provider._is_wsl_docker() is False

    @pytest.mark.parametrize(
        "docker_host",
        ["tcp://localhost:2375", "unix:///var/run/docker.sock", "ssh://user@host"],
    )
    def test_remote_prefixes_are_wsl(self, docker_host: str, monkeypatch: Any) -> None:
        provider = _provider()
        provider._wsl_docker_cache = None
        monkeypatch.setenv("DOCKER_HOST", docker_host)
        assert provider._is_wsl_docker() is True

    @pytest.mark.parametrize("docker_host", ["npipe:////./pipe/docker_engine", ""])
    def test_desktop_or_empty_is_not_wsl(self, docker_host: str, monkeypatch: Any) -> None:
        provider = _provider()
        provider._wsl_docker_cache = None
        monkeypatch.setenv("DOCKER_HOST", docker_host)
        assert provider._is_wsl_docker() is False


class TestResolveMountPath:
    def test_empty_workspace(self) -> None:
        assert _provider()._resolve_mount_path(None) == ""
        assert _provider()._resolve_mount_path("") == ""

    def test_non_wsl_passthrough(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        assert provider._resolve_mount_path(r"D:\myproject\ws") == r"D:\myproject\ws"

    def test_wsl_windows_drive_conversion(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: True)
        assert provider._resolve_mount_path(r"D:\myproject\ws") == "/mnt/d/myproject/ws"
        assert provider._resolve_mount_path("C:/other/path") == "/mnt/c/other/path"

    def test_wsl_non_standard_path_passthrough(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: True)
        assert provider._resolve_mount_path("//server/share") == "//server/share"


class TestIsAvailable:
    def test_no_docker_cli(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: None)
        ok, err = _run(_provider().is_available())
        assert ok is False
        assert "Docker CLI 未安装" in (err or "")

    def test_daemon_ok(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: "docker")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: _Completed(0, b"24.0.0", b"")
        )
        ok, err = _run(provider.is_available())
        assert ok is True
        assert err is None
        assert provider._docker_available is True

    def test_daemon_not_running(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: "docker")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: _Completed(1, b"", b"cannot connect")
        )
        ok, err = _run(_provider().is_available())
        assert ok is False
        assert "Docker daemon 未运行" in (err or "")
        assert "cannot connect" in (err or "")

    def test_cli_missing_raises_filenotfound(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: "docker")

        def _raise_fnf(*a: Any, **kw: Any) -> Any:
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", _raise_fnf)
        ok, err = _run(_provider().is_available())
        assert ok is False
        assert "Docker CLI 未找到" in (err or "")

    def test_timeout(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: "docker")

        def _raise_timeout(*a: Any, **kw: Any) -> Any:
            raise TimeoutError

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        ok, err = _run(_provider().is_available())
        assert ok is False
        assert "Docker 检查超时" in (err or "")

    def test_generic_exception(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda _: "docker")

        def _raise_other(*a: Any, **kw: Any) -> Any:
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "run", _raise_other)
        ok, err = _run(_provider().is_available())
        assert ok is False
        assert "Docker 检查失败" in (err or "")
        assert "boom" in (err or "")


class _Completed:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunCmd:
    def test_non_stream_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        captured: dict[str, Any] = {}

        def fake_run(args: list[str], capture_output: bool = True, timeout: float = 30, env: Any = None) -> Any:
            captured["args"] = args
            captured["timeout"] = timeout
            captured["env"] = env
            return _Completed(0, b"out", b"err")

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc, out, err = _run(provider._run_cmd(["docker", "version"], timeout=15))
        assert rc == 0
        assert out == b"out"
        assert err == b"err"
        assert captured["args"] == ["docker", "version"]
        assert captured["timeout"] == 15

    def test_non_stream_env_merged(self, monkeypatch: Any) -> None:
        provider = _provider()
        captured: dict[str, Any] = {}

        def fake_run(args: list[str], capture_output: bool = True, timeout: float = 30, env: Any = None) -> Any:
            captured["env"] = env
            return _Completed(0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        _run(provider._run_cmd(["docker", "build"], env={"DOCKER_BUILDKIT": "1"}))
        # env 合并到宿主环境上，未提到的变量保持继承
        assert captured["env"] is not None
        assert captured["env"]["DOCKER_BUILDKIT"] == "1"
        assert "PATH" in captured["env"]

    def test_stream_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        rc, out, err = _run(provider._run_cmd(["docker", "build", "."], stream_log=True))
        assert rc == 0
        assert out == b"out-data"
        assert err == b"line1\nline2\n"

    def test_stream_timeout_raises(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)
        with pytest.raises(subprocess.TimeoutExpired):
            _run(provider._run_cmd(["docker", "build", "."], stream_log=True))


class _FakePopen:
    def __init__(self, args: list[str], stdout: Any = None, stderr: Any = None, env: Any = None, text: bool = False, bufsize: int = 1) -> None:
        self.args = args
        self.env = env
        self.stdout = io.BytesIO(b"out-data")
        self.stderr = [b"line1\n", b"line2\n"]
        self.returncode = 0
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _TimeoutPopen:
    def __init__(self, args: list[str], stdout: Any = None, stderr: Any = None, env: Any = None, text: bool = False, bufsize: int = 1) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr: list[bytes] = []
        self.returncode: int | None = None
        self._waited = False

    def wait(self, timeout: float | None = None) -> int:
        if not self._waited:
            self._waited = True
            raise subprocess.TimeoutExpired("docker build", timeout if timeout is not None else 30)
        self.returncode = -9
        return -9

    def kill(self) -> None:
        pass


class TestBuildRunArgs:
    def test_default_args(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        ws = tmp_path / "ws"
        ws.mkdir()
        args = provider._build_run_args("c1", _ctx(workspace=str(ws)))
        assert "--name" in args and args[args.index("--name") + 1] == "c1"
        assert "--init" in args
        assert "--cpus" in args and args[args.index("--cpus") + 1] == "1.0"
        assert "--memory" in args and args[args.index("--memory") + 1] == "512m"
        assert "--memory-swap" in args and args[args.index("--memory-swap") + 1] == "512m"
        assert "--pids-limit" in args and args[args.index("--pids-limit") + 1] == "100"
        assert "--network" in args and args[args.index("--network") + 1] == "bridge"
        assert "-i" in args and "-t" in args
        # 工作空间挂载 + 缓存卷 + 镜像 + 常驻命令
        assert f"{ws}:/workspace" in args
        assert any(v.startswith("agentos-") for v in args if v.endswith(":/cargo-cache"))
        assert args[-4:] == ["agentos:latest", "sh", "-c", "tail -f /dev/null"]

    def test_publish_ports(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider({"publish_ports": ["8080:8080", "9090:9090"]})
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        ws = tmp_path / "ws"
        ws.mkdir()
        args = provider._build_run_args("c1", _ctx(workspace=str(ws)))
        assert "-p" in args
        assert args[args.index("-p") + 1] == "8080:8080"
        assert args[args.index("-p") + 3] == "9090:9090"

    def test_extra_cache_volumes(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider({"extra_cache_volumes": [("vol-x", "/x")]})
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        ws = tmp_path / "ws"
        ws.mkdir()
        args = provider._build_run_args("c1", _ctx(workspace=str(ws)))
        assert "vol-x:/x" in args

    def test_no_workspace_mount(self, tmp_path: Path) -> None:
        provider = _provider({"workspace_mount": False})
        args = provider._build_run_args("c1", _ctx(workspace=str(tmp_path / "ws")))
        assert not any(a.endswith(":/workspace") for a in args)

    def test_wsl_mount_converted(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: True)
        args = provider._build_run_args("c1", _ctx(workspace=r"D:\myproject\ws"))
        assert r"D:\myproject\ws:/workspace" not in args
        assert "/mnt/d/myproject/ws:/workspace" in args


class TestNamespaceDesync:
    @pytest.mark.parametrize(
        "err",
        [
            None,
            b"",
            "",
            "command not found",
            b"permission denied",
        ],
    )
    def test_not_desync(self, err: str | bytes | None) -> None:
        assert DockerProvider._is_namespace_desync_error(err) is False

    @pytest.mark.parametrize(
        "err",
        [
            "OCI runtime exec failed: ... error executing setns process: exit status 1",
            b"unable to start container process: exec: ...",
            "Error response: error executing setns",
        ],
    )
    def test_desync_markers(self, err: str | bytes) -> None:
        assert DockerProvider._is_namespace_desync_error(err) is True


class TestIoError:
    @pytest.mark.parametrize(
        "err",
        [
            None,
            b"",
            "",
            "ls: cannot access '/workspace/x': No such file or directory",
            b"permission denied",
        ],
    )
    def test_not_io_error(self, err: str | bytes | None) -> None:
        assert DockerProvider._is_io_error(err) is False

    @pytest.mark.parametrize(
        "err",
        [
            "ls: cannot access '/workspace/docs/report.md': Input/output error",
            b"cat: /workspace/x: input/output error",
        ],
    )
    def test_io_error_markers(self, err: str | bytes) -> None:
        assert DockerProvider._is_io_error(err) is True


class TestEnsureImage:
    def test_image_exists_fast_path(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._ensure_image())
        assert len(script.calls) == 1
        assert script.calls[0]["args"][:3] == ["docker", "image", "inspect"]

    def test_image_exists_after_lock_second_check(self, tmp_path: Path, monkeypatch: Any) -> None:
        """锁内二次检查命中：并发场景下另一协程已构建完成，直接返回。"""
        provider = _provider()
        script = _CmdScript([(1, b"", b""), (0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._ensure_image())
        # 两次 inspect（锁外失败 + 锁内成功），无 build/pull
        assert len(script.calls) == 2
        assert all(c["args"][:3] == ["docker", "image", "inspect"] for c in script.calls)

    def test_build_success(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        df = tmp_path / "Dockerfile"
        df.write_text("FROM scratch", encoding="utf-8")
        provider._dockerfile_path = df
        provider._build_context = tmp_path
        script = _CmdScript([(1, b"", b""), (1, b"", b""), (0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._ensure_image())
        # 两次 inspect（锁外 + 锁内二次检查）+ 一次 build
        assert len(script.calls) == 3
        build_call = script.calls[2]
        assert build_call["args"][:2] == ["docker", "build"]
        assert build_call["args"][build_call["args"].index("-t") + 1] == "agentos:latest"
        assert build_call["env"] == {"DOCKER_BUILDKIT": "1"}
        assert build_call["stream_log"] is True

    def test_build_failure_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        df = tmp_path / "Dockerfile"
        df.write_text("FROM scratch", encoding="utf-8")
        provider._dockerfile_path = df
        provider._build_context = tmp_path
        script = _CmdScript([(1, b"", b""), (1, b"", b""), (1, b"", b"build exploded")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        with pytest.raises(RuntimeError) as exc:
            _run(provider._ensure_image())
        assert "自动构建失败" in str(exc.value)
        assert "build exploded" in str(exc.value)

    def test_pull_when_auto_build_off(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider({"auto_build": False})
        script = _CmdScript([(1, b"", b""), (1, b"", b""), (0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._ensure_image())
        assert len(script.calls) == 3
        assert script.calls[2]["args"][:2] == ["docker", "pull"]

    def test_pull_failure_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider({"auto_build": False})
        script = _CmdScript([(1, b"", b""), (1, b"", b""), (1, b"", b"pull failed")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        with pytest.raises(RuntimeError) as exc:
            _run(provider._ensure_image())
        assert "拉取失败" in str(exc.value)
        assert "pull failed" in str(exc.value)


class TestCreateOneStartOne:
    def test_create_one_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"cid-9\n", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        cid, err = _run(provider._create_one(["--name", "c1"]))
        assert cid == "cid-9"
        assert err == ""
        assert script.calls[0]["args"][:2] == ["docker", "create"]

    def test_create_one_failure(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(1, b"", b"create boom")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        cid, err = _run(provider._create_one(["--name", "c1"]))
        assert cid == ""
        assert "create boom" in err

    def test_start_one_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        ok, err = _run(provider._start_one("cid-1"))
        assert ok is True
        assert err == ""
        assert script.calls[0]["args"] == ["docker", "start", "cid-1"]

    def test_start_one_failure(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(1, b"", b"start boom")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        ok, err = _run(provider._start_one("cid-1"))
        assert ok is False
        assert "start boom" in err


class TestCreateAndStart:
    def test_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_create_one", _fake_create_one("id-1"))
        monkeypatch.setattr(provider, "_start_one", _fake_start_one(True))
        cid, err = _run(provider._create_and_start("c1", ["--name", "c1"]))
        assert cid == "id-1"
        assert err == ""

    def test_create_failure(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_create_one", _fake_create_one(""))
        monkeypatch.setattr(provider, "_start_one", _fake_start_one(True))
        cid, err = _run(provider._create_and_start("c1", ["--name", "c1"]))
        assert cid == ""
        assert "create failed" in err

    def test_start_failure_rebuild_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        rm_calls: list[list[str]] = []
        script = _CmdScript([])
        monkeypatch.setattr(provider, "_run_cmd", script)
        # 记录 rm 调用
        original = script

        async def fake_rm(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            rm_calls.append(list(args))
            return 0, b"", b""

        monkeypatch.setattr(provider, "_run_cmd", fake_rm)
        monkeypatch.setattr(provider, "_create_one", _fake_create_one("id-1", "id-2"))
        monkeypatch.setattr(provider, "_start_one", _fake_start_one(False, True))
        cid, err = _run(provider._create_and_start("c1", ["--name", "c1"]))
        assert cid == "id-2"
        assert err == ""
        # 失败后删除卡死容器再重建
        assert rm_calls == [["docker", "rm", "-f", "id-1"]]

    def test_start_failure_rebuild_failure(self, monkeypatch: Any) -> None:
        provider = _provider()
        rm_calls: list[list[str]] = []

        async def fake_rm(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            rm_calls.append(list(args))
            return 0, b"", b""

        monkeypatch.setattr(provider, "_run_cmd", fake_rm)
        monkeypatch.setattr(provider, "_create_one", _fake_create_one("id-1", "id-2"))
        monkeypatch.setattr(provider, "_start_one", _fake_start_one(False, False))
        cid, err = _run(provider._create_and_start("c1", ["--name", "c1"]))
        assert cid == ""
        assert "start failed" in err
        assert rm_calls == [["docker", "rm", "-f", "id-1"], ["docker", "rm", "-f", "id-2"]]

    def test_rebuild_create_failure(self, monkeypatch: Any) -> None:
        """start 失败删除后，重建 create 也失败：返回第二次 create 的错误。"""
        provider = _provider()
        rm_calls: list[list[str]] = []

        async def fake_rm(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            rm_calls.append(list(args))
            return 0, b"", b""

        monkeypatch.setattr(provider, "_run_cmd", fake_rm)
        monkeypatch.setattr(provider, "_create_one", _fake_create_one("id-1", ""))
        monkeypatch.setattr(provider, "_start_one", _fake_start_one(False))
        cid, err = _run(provider._create_and_start("c1", ["--name", "c1"]))
        assert cid == ""
        assert "create failed" in err
        assert rm_calls == [["docker", "rm", "-f", "id-1"]]


def _fake_create_one(*ids: str) -> Any:
    """_create_one 替身：按序返回 (id, "") 或 ("", err)。"""
    remaining = list(ids)

    async def fake(args: list[str], timeout: float = 30) -> tuple[str, str]:
        if not remaining:
            return "", "create failed"
        cid = remaining.pop(0)
        if cid == "":
            return "", "create failed"
        return cid, ""

    return fake


def _fake_start_one(*oks: bool) -> Any:
    """_start_one 替身：按序返回 (ok, "") 或 (False, err)。"""
    remaining = list(oks)

    async def fake(container_id: str, timeout: float = 15) -> tuple[bool, str]:
        if not remaining:
            return False, "start failed"
        ok = remaining.pop(0)
        if ok:
            return True, ""
        return False, "start failed"

    return fake


class TestCreateEnvironment:
    def test_success(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(provider, "_ensure_image", _fake_ensure_image())
        monkeypatch.setattr(provider, "_create_and_start", _fake_create_and_start("abc123", ""))
        # get_environment_status 的 docker inspect 属外部依赖，stub 为 running
        script = _CmdScript([(0, b"running", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), "c1"))
        assert env.env_id == "c1"
        assert env.status == EnvironmentStatus.READY.value
        assert env.provider_info["container_id"] == "abc123"
        assert env.provider_info["container_name"] == "c1"
        assert env.provider_info["image"] == "agentos:latest"
        # 注册后可查状态
        assert _run(provider.get_environment_status("c1")) == EnvironmentStatus.READY

    def test_empty_workspace_rejected(self, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        env = _run(provider.create_environment(_ctx(workspace=None), "c1"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in env.provider_info.get("error", "")

    def test_workspace_path_missing_rejected(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        env = _run(provider.create_environment(_ctx(workspace="no_such_dir_xyz"), "c1"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间路径不存在" in env.provider_info.get("error", "")

    def test_wsl_skips_host_path_check(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: True)
        monkeypatch.setattr(provider, "_ensure_image", _fake_ensure_image())
        captured: dict[str, Any] = {}

        async def fake_create_and_start(name: str, run_args: list[str]) -> tuple[str, str]:
            captured["run_args"] = list(run_args)
            return "abc123", ""

        monkeypatch.setattr(provider, "_create_and_start", fake_create_and_start)
        # WSL 路径在 Windows 宿主上不存在，但跳过宿主校验直接交给 daemon
        env = _run(provider.create_environment(_ctx(workspace=r"D:\myproject\ws"), "c1"))
        assert env.status == EnvironmentStatus.READY.value
        assert "/mnt/d/myproject/ws:/workspace" in captured["run_args"]

    def test_workspace_mount_disabled_skips_validation(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider({"workspace_mount": False})
        monkeypatch.setattr(provider, "_ensure_image", _fake_ensure_image())
        monkeypatch.setattr(provider, "_create_and_start", _fake_create_and_start("abc123", ""))
        env = _run(provider.create_environment(_ctx(workspace=None), "c1"))
        assert env.status == EnvironmentStatus.READY.value

    def test_create_failure_returns_error_env(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(provider, "_ensure_image", _fake_ensure_image())
        monkeypatch.setattr(provider, "_create_and_start", _fake_create_and_start("", "daemon exploded"))
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), "c1"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "daemon exploded" in env.provider_info.get("error", "")


def _fake_ensure_image() -> Any:
    async def fake() -> None:
        return None

    return fake


def _fake_create_and_start(cid: str, err: str) -> Any:
    async def fake(name: str, run_args: list[str]) -> tuple[str, str]:
        return cid, err

    return fake


class TestDestroyEnvironment:
    def test_unknown_id_idempotent(self) -> None:
        assert _run(_provider().destroy_environment("docker-nope")) is True

    def test_no_container_id_pops_record(self) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
        )
        provider._environments["docker-x"] = env
        assert _run(provider.destroy_environment("docker-x")) is True
        assert "docker-x" not in provider._environments

    def test_rm_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        script = _CmdScript([(0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        assert _run(provider.destroy_environment("docker-x")) is True
        assert "docker-x" not in provider._environments
        assert script.calls[0]["args"] == ["docker", "rm", "-f", "cid-1"]

    def test_rm_failure_keeps_record(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        script = _CmdScript([(1, b"", b"could not kill container")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        assert _run(provider.destroy_environment("docker-x")) is False
        # rm 失败不谎报：记录保留，docker 里容器仍在
        assert "docker-x" in provider._environments

    def test_rm_exception_keeps_record(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env

        async def fake_rm(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            raise RuntimeError("daemon gone")

        monkeypatch.setattr(provider, "_run_cmd", fake_rm)
        assert _run(provider.destroy_environment("docker-x")) is False
        assert "docker-x" in provider._environments


class TestExecuteInEnvironment:
    def test_env_missing(self) -> None:
        result = _run(_provider().execute_in_environment("docker-nope", {"type": "command"}))
        assert result.success is False
        assert "环境不存在" in (result.error or "")

    def test_error_env_returns_stored_error(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider()
        monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
        env = _run(provider.create_environment(_ctx(workspace=None), "c1"))
        assert env.status == EnvironmentStatus.ERROR.value
        # 错误环境以 docker-{task_id} 注册（_make_error_environment）
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "ls"}))
        assert result.success is False
        assert "工作空间为空" in (result.error or "")

    def test_no_container_id(self) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
        )
        provider._environments["docker-x"] = env
        result = _run(provider.execute_in_environment("docker-x", {"type": "command", "command": "ls"}))
        assert result.success is False
        assert "容器ID不存在" in (result.error or "")

    def test_command_dispatch(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        captured: dict[str, Any] = {}

        async def fake_exec(container_id: str, operation: dict[str, Any]) -> ExecutionResult:
            captured["container_id"] = container_id
            captured["operation"] = operation
            return ExecutionResult(success=True, output="ok")

        monkeypatch.setattr(provider, "_exec_in_container", fake_exec)
        result = _run(provider.execute_in_environment("docker-x", {"type": "command", "command": "ls"}))
        assert result.success is True
        assert captured["container_id"] == "cid-1"
        assert captured["operation"]["command"] == "ls"

    def test_file_operation_dispatch(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        captured: dict[str, Any] = {}

        async def fake_file_op(container_id: str, operation: dict[str, Any]) -> ExecutionResult:
            captured["operation"] = operation
            return ExecutionResult(success=True, output="file-ok")

        monkeypatch.setattr(provider, "_file_op_in_container", fake_file_op)
        result = _run(provider.execute_in_environment("docker-x", {"type": "file_operation", "operation": "read"}))
        assert result.success is True
        assert captured["operation"]["operation"] == "read"

    def test_unsupported_type(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        result = _run(provider.execute_in_environment("docker-x", {"type": "nonsense"}))
        assert result.success is False
        assert "不支持的操作类型" in (result.error or "")


class TestGetEnvironmentStatus:
    def test_unknown_env(self) -> None:
        assert _run(_provider().get_environment_status("docker-nope")) == EnvironmentStatus.STOPPED

    def test_no_container_id(self) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
        )
        provider._environments["docker-x"] = env
        assert _run(provider.get_environment_status("docker-x")) == EnvironmentStatus.ERROR

    @pytest.mark.parametrize(
        ("docker_status", "expected"),
        [
            ("running", EnvironmentStatus.READY),
            ("created", EnvironmentStatus.CREATING),
            ("paused", EnvironmentStatus.BUSY),
            ("exited", EnvironmentStatus.STOPPED),
            ("dead", EnvironmentStatus.ERROR),
            ("restarting", EnvironmentStatus.ERROR),
        ],
    )
    def test_status_mapping(self, docker_status: str, expected: Any, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env
        script = _CmdScript([(0, docker_status.encode(), b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        assert _run(provider.get_environment_status("docker-x")) == expected

    def test_inspect_exception_returns_error(self, monkeypatch: Any) -> None:
        provider = _provider()
        env = IsolationEnvironment(
            env_id="docker-x",
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
            provider_info={"container_id": "cid-1"},
        )
        provider._environments["docker-x"] = env

        async def fake_inspect(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            raise RuntimeError("daemon gone")

        monkeypatch.setattr(provider, "_run_cmd", fake_inspect)
        assert _run(provider.get_environment_status("docker-x")) == EnvironmentStatus.ERROR


class TestExecInContainer:
    def test_empty_command(self) -> None:
        result = _run(_provider()._exec_in_container("cid-1", {"command": ""}))
        assert result.success is False
        assert "命令不能为空" in (result.error or "")

    def test_success(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"hello\n", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        result = _run(provider._exec_in_container("cid-1", {"command": "echo hello"}))
        assert result.success is True
        assert result.error is None
        out = result.output or {}
        assert out["stdout"] == "hello\n"
        assert out["return_code"] == 0
        assert out["command"] == "echo hello"
        # exec 参数：-w 工作目录 + 容器 id + sh -c
        assert script.calls[0]["args"][:4] == ["docker", "exec", "-w", "/workspace"]
        assert script.calls[0]["args"][4] == "cid-1"

    def test_nonzero_exit(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(1, b"", b"command not found")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        result = _run(provider._exec_in_container("cid-1", {"command": "nope"}))
        assert result.success is False
        out = result.output or {}
        assert out["return_code"] == 1
        assert result.error == "command not found"

    def test_timeout(self, monkeypatch: Any) -> None:
        provider = _provider()

        async def fake_run(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            raise subprocess.TimeoutExpired("docker exec", timeout)

        monkeypatch.setattr(provider, "_run_cmd", fake_run)
        result = _run(provider._exec_in_container("cid-1", {"command": "sleep 5", "timeout": 0.1}))
        assert result.success is False
        assert "超时" in (result.error or "")

    def test_generic_exception(self, monkeypatch: Any) -> None:
        provider = _provider()

        async def fake_run(args: list[str], timeout: float = 30, env: Any = None, stream_log: bool = False) -> tuple[int, bytes, bytes]:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(provider, "_run_cmd", fake_run)
        result = _run(provider._exec_in_container("cid-1", {"command": "ls"}))
        assert result.success is False
        assert "执行命令失败" in (result.error or "")
        assert "kaboom" in (result.error or "")


class TestFileOpInContainer:
    def test_read(self, monkeypatch: Any) -> None:
        provider = _provider()

        async def fake_read(container_id: str, path: str) -> str:
            return "file-content"

        monkeypatch.setattr(provider, "_read_container_file", fake_read)
        result = _run(provider._file_op_in_container("cid-1", {"operation": "read", "path": "/workspace/a.txt"}))
        assert result.success is True
        assert result.output == "file-content"

    def test_write(self, monkeypatch: Any) -> None:
        provider = _provider()
        captured: dict[str, Any] = {}

        async def fake_write(container_id: str, path: str, content: str) -> None:
            captured["container_id"] = container_id
            captured["path"] = path
            captured["content"] = content

        monkeypatch.setattr(provider, "_write_container_file", fake_write)
        result = _run(provider._file_op_in_container("cid-1", {"operation": "write", "path": "/workspace/b.txt", "content": "data"}))
        assert result.success is True
        assert captured == {"container_id": "cid-1", "path": "/workspace/b.txt", "content": "data"}

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [("yes\n", True), ("no\n", False)],
    )
    def test_exists(self, stdout: str, expected: bool, monkeypatch: Any) -> None:
        provider = _provider()

        async def fake_exec(container_id: str, operation: dict[str, Any]) -> ExecutionResult:
            return ExecutionResult(success=True, output={"stdout": stdout})

        monkeypatch.setattr(provider, "_exec_in_container", fake_exec)
        result = _run(provider._file_op_in_container("cid-1", {"operation": "exists", "path": "/workspace/c.txt"}))
        assert result.success is True
        assert result.output == {"exists": expected}

    def test_unsupported_operation(self) -> None:
        result = _run(_provider()._file_op_in_container("cid-1", {"operation": "chmod", "path": "/x"}))
        assert result.success is False
        assert "不支持的文件操作" in (result.error or "")

    def test_exception_wrapped(self, monkeypatch: Any) -> None:
        provider = _provider()

        async def fake_read(container_id: str, path: str) -> str:
            raise RuntimeError("read boom")

        monkeypatch.setattr(provider, "_read_container_file", fake_read)
        result = _run(provider._file_op_in_container("cid-1", {"operation": "read", "path": "/x"}))
        assert result.success is False
        assert "文件操作失败" in (result.error or "")
        assert "read boom" in (result.error or "")


class TestReadWriteContainerFile:
    def test_read(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"data-bytes", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        content = _run(provider._read_container_file("cid-1", "/workspace/a.txt"))
        assert content == "data-bytes"
        assert script.calls[0]["args"] == ["docker", "exec", "cid-1", "cat", "/workspace/a.txt"]

    def test_write_mkdir_then_python(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"", b""), (0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._write_container_file("cid-1", "/workspace/sub/b.txt", "line1\nline2"))
        assert len(script.calls) == 2
        assert script.calls[0]["args"] == ["docker", "exec", "cid-1", "mkdir", "-p", "/workspace/sub"]
        # F-ISO-2：content 作为独立 argv 传入（json 编码），不嵌入源码
        second = script.calls[1]["args"]
        assert second[:4] == ["docker", "exec", "cid-1", "python3"]
        assert second[4] == "-c"
        assert second[5] == "import json,sys; open(sys.argv[1],'w').write(json.loads(sys.argv[2]))"
        assert second[6] == "/workspace/sub/b.txt"
        assert second[7] == json.dumps("line1\nline2")

    def test_write_no_dir_component(self, monkeypatch: Any) -> None:
        provider = _provider()
        script = _CmdScript([(0, b"", b""), (0, b"", b"")])
        monkeypatch.setattr(provider, "_run_cmd", script)
        _run(provider._write_container_file("cid-1", "top.txt", "x"))
        assert script.calls[0]["args"] == ["docker", "exec", "cid-1", "mkdir", "-p", "."]
