# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation providers/wsl_native_provider.py 测试（providers 包覆盖补）。

覆盖：
1. 配置与构造：默认/自定义 config、from_backend_info（roundtrip/非 wsl_native 拒绝）；
2. 可用性：is_available（无 wsl.exe/发行版缺失/UTF-16 列表解码/用户探测失败/
   sandbox 二进制缺失/探测异常 fail-closed）；
3. 路径转换：_to_wsl_path（盘符/POSIX/空）、_map_working_dir（/workspace 前缀/
   其他 POSIX/Windows 路径/None）；
4. 生命周期（D6 语义过继）：
   - create_environment 成功（metadata 落盘 + exec_backend 注入 provider_info）
     /空工作空间/宿主路径不存在；
   - D6① 幂等收养：metadata 已存在且 workspace 仍有效 → 不再调 wsl 直接收养；
     workspace 漂移的陈旧 metadata → 清档重建；
   - D6② 存在性未知：wsl 探测超时/异常 → error env + query_unavailable 标记，
     不落 READY、不写 metadata；
   - find_environment_by_name：命中/缺失/workspace 已消失（清档返 None，
     对应 docker 坏容器探针删除重建）/metadata 损坏；
   - D6③ destroy_environment：有登记删档/无登记按名删（缺档幂等 True）/
     删档失败如实 False（不谎报）；
5. 执行：execute_in_environment（未知环境/错误环境回放/成功输出形状/非零退出/
   空命令/不支持操作/file_operation read-write-exists）；
6. 超时杀树：_popen_run 超时杀进程树（杀净 → TimeoutError）/杀树失败
   （错误消息携带失败清单，不吞）；
7. argv 构建：build_exec_argv（完整形态/无 user/无 env 无 sandbox）/
   build_kill_argv。

隔离策略：isolation_types 用真实模块；wsl.exe 属外部依赖，mock 仅限
subprocess.Popen / shutil.which / provider._popen_run（记录型替身）；
metadata 读写与 workspace 目录用真实文件系统（tmp_path）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationLevel,
    OperationType,
    TaskType,
)

if TYPE_CHECKING:
    from plugins.shared.system.isolation.providers.wsl_native_provider import WslNativeProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/


def _load_mod() -> Any:
    """动态加载 providers/wsl_native_provider.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_wsl_native_provider_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _PLUGIN_DIR / "providers" / "wsl_native_provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    WslNativeProvider = _MOD.WslNativeProvider


def _run(coro: Any) -> Any:
    """共享测试进程中其他测试可能关闭主 loop，须自建独立 loop。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(
    task_id: str = "t-1",
    workspace: str | None = None,
) -> IsolationContext:
    return IsolationContext(
        task_id=task_id,
        task_type=TaskType.ATOMIC,
        operation_type=OperationType.CODE_EXECUTION,
        workspace=workspace,
        isolation_level=IsolationLevel.CONTAINER,
    )


class _Recorder:
    """_popen_run 记录型替身：记住调用参数，按脚本回放 (rc, out, err)。"""

    def __init__(self, script: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._script = script or (lambda args: (0, b"", b""))
        self._exc = exc

    async def __call__(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        self.calls.append(list(args))
        if self._exc is not None:
            raise self._exc
        return self._script(args)


def _make_provider(
    tmp_path: Path,
    workspace: Path | None = None,
    **overrides: Any,
) -> Any:
    """构造受测 provider：state_dir 指向 tmp_path。

    workspace 为 None 时创建默认临时目录；显式传入的 workspace 保持调用方
    给定的存在状态（如「宿主路径不存在」用例）。默认安装 _popen_run 记录
    替身；需要测真实 _popen_run（超时杀树）时对实例属性 del 即还原类方法。
    """
    ws = workspace if workspace is not None else tmp_path / "ws"
    if workspace is None:
        Path(ws).mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "distro": "Ubuntu",
        "state_dir": str(tmp_path / "envs"),
        "wsl_exe": "wsl",
    }
    config.update(overrides)
    provider = WslNativeProvider(config)
    provider._popen_run = _Recorder()  # type: ignore[method-assign]
    provider._workspace = str(ws)
    return provider


def _create(provider: Any, tmp_path: Path, name: str = "cua-ws1") -> Any:
    return _run(provider.create_environment(_ctx(workspace=provider._workspace), name))


def _metadata_path(provider: Any, name: str) -> Path:
    return Path(provider._state_dir) / f"{name}.json"


# ── 1. 配置与构造 ────────────────────────────────────────────


class TestConfig:
    def test_default_config(self, tmp_path: Path) -> None:
        provider = WslNativeProvider({})
        assert provider._distro == "Ubuntu"
        assert provider._user is None
        assert provider._sandbox_cmd == []
        assert provider._wsl_exe == "wsl"
        assert str(provider._state_dir).endswith("wsl_native_envs")

    def test_custom_config(self, tmp_path: Path) -> None:
        provider = WslNativeProvider(
            {
                "distro": "Debian",
                "user": "agentos",
                "sandbox_cmd": ["bwrap", "--dev", "/dev"],
                "state_dir": str(tmp_path / "s"),
                "wsl_exe": "C:/Tools/wsl.exe",
            }
        )
        assert provider._distro == "Debian"
        assert provider._user == "agentos"
        assert provider._sandbox_cmd == ["bwrap", "--dev", "/dev"]
        assert provider._wsl_exe == "C:/Tools/wsl.exe"

    def test_get_level_is_container(self) -> None:
        assert WslNativeProvider({}).get_level() == IsolationLevel.CONTAINER

    def test_from_backend_info_roundtrip(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path, user="agentos", sandbox_cmd=["bwrap"])
        info = provider.exec_backend_info(workspace_wsl="/mnt/d/ws")
        assert info["backend"] == "wsl_native"
        clone = WslNativeProvider.from_backend_info(info)
        assert clone._distro == provider._distro
        assert clone._user == "agentos"
        assert clone._sandbox_cmd == ["bwrap"]
        assert clone._state_dir == provider._state_dir

    def test_from_backend_info_rejects_foreign_backend(self) -> None:
        with pytest.raises(ValueError, match="wsl_native"):
            WslNativeProvider.from_backend_info({"backend": "docker"})


# ── 2. is_available ──────────────────────────────────────────


class TestAvailability:
    def test_wsl_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda name: None)
        ok, reason = _run(WslNativeProvider({}).is_available())
        assert ok is False
        assert "WSL" in (reason or "")

    def test_distro_missing(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (0, b"Debian\n", b""))
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "Ubuntu" in (reason or "")

    def test_distro_list_utf16_decode(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(
            script=lambda args: (0, "Ubuntu\r\nDebian\r\n".encode("utf-16-le"), b"")
        )
        ok, reason = _run(provider.is_available())
        assert ok is True
        assert reason is None

    def test_user_probe_failure(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path, user="agentos")

        def script(args: list[str]) -> tuple[int, bytes, bytes]:
            if "-u" in args and "agentos" in args and "true" in args:
                return (1, b"", b"denied")
            return (0, b"Ubuntu\r\n", b"")

        provider._popen_run = _Recorder(script=script)
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "agentos" in (reason or "")

    def test_sandbox_binary_missing(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path, sandbox_cmd=["bwrap"])

        def script(args: list[str]) -> tuple[int, bytes, bytes]:
            if any("command -v bwrap" in a for a in args):
                return (1, b"", b"")
            return (0, b"Ubuntu\r\n", b"")

        provider._popen_run = _Recorder(script=script)
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "bwrap" in (reason or "")

    def test_probe_exception_fail_closed(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(exc=TimeoutError("wsl 假死"))
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert reason  # 携带异常摘要，不静默


# ── 3. 路径转换 ──────────────────────────────────────────────


class TestPathMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("D:\\myproject\\ws", "/mnt/d/myproject/ws"),
            ("d:\\x", "/mnt/d/x"),
            ("/mnt/d/already", "/mnt/d/already"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_to_wsl_path(self, raw: Any, expected: str) -> None:
        assert WslNativeProvider._to_wsl_path(raw) == expected

    def test_map_working_dir(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._workspace_wsl = "/mnt/d/ws"
        assert provider._map_working_dir(None) == "/mnt/d/ws"
        assert provider._map_working_dir("") == "/mnt/d/ws"
        assert provider._map_working_dir("/workspace") == "/mnt/d/ws"
        assert provider._map_working_dir("/workspace/sub/f.txt") == "/mnt/d/ws/sub/f.txt"
        assert provider._map_working_dir("/tmp/other") == "/tmp/other"
        assert provider._map_working_dir("D:\\host\\dir") == "D:\\host\\dir"
        # 工具层 ntpath 规整产物（反斜杠形态）同样命中映射
        assert provider._map_working_dir("\\workspace") == "/mnt/d/ws"
        assert provider._map_working_dir("\\workspace\\sub") == "/mnt/d/ws/sub"


# ── 4. 生命周期（D6 语义过继） ───────────────────────────────


class TestCreateEnvironment:
    def test_create_success(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        recorder = _Recorder(script=lambda args: (0, b"", b""))
        provider._popen_run = recorder
        name = "cua-ws1"
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))

        assert env.env_id == name
        assert env.level == IsolationLevel.CONTAINER
        assert env.provider_type == "wsl_native"
        assert env.status == EnvironmentStatus.READY.value
        info = env.provider_info
        assert info["container_name"] == name
        assert info["exec_backend"]["backend"] == "wsl_native"
        assert info["exec_backend"]["distro"] == "Ubuntu"
        assert info["exec_backend"]["workspace_wsl"] == WslNativeProvider._to_wsl_path(
            provider._workspace
        )
        # metadata 已落盘且已注册
        assert _metadata_path(provider, name).is_file()
        assert name in provider._environments
        # mkdir -p 走 wsl：argv 含发行版与 mkdir
        assert recorder.calls, "wsl mkdir 未被调用"
        assert any("mkdir" in " ".join(c) for c in recorder.calls)

    def test_create_empty_workspace_rejected(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=None), "cua-x"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in str(env.provider_info.get("error"))
        assert "query_unavailable" not in env.provider_info
        assert provider._popen_run.calls == []  # 未触发任何 wsl 调用

    def test_create_workspace_not_exists_on_host(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-ws"
        provider = _make_provider(tmp_path, workspace=missing)
        env = _run(provider.create_environment(_ctx(workspace=str(missing)), "cua-x"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间路径不存在" in str(env.provider_info.get("error"))

    def test_create_adopts_existing_metadata_d6_1(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        name = "cua-ws1"
        # 预置遗留 metadata（服务重启前的环境），workspace 仍有效
        _metadata_path(provider, name).parent.mkdir(parents=True, exist_ok=True)
        _metadata_path(provider, name).write_text(
            json.dumps(
                {
                    "name": name,
                    "workspace": provider._workspace,
                    "workspace_wsl": WslNativeProvider._to_wsl_path(provider._workspace),
                    "created_at": "2026-09-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))
        # D6①：按名收养，不再触发任何 wsl 调用（无 mkdir/无重复创建）
        assert env.status == EnvironmentStatus.READY.value
        assert env.env_id == name
        assert provider._popen_run.calls == []
        assert env.provider_info["container_name"] == name

    def test_create_stale_metadata_recreated(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        name = "cua-ws1"
        _metadata_path(provider, name).parent.mkdir(parents=True, exist_ok=True)
        _metadata_path(provider, name).write_text(
            json.dumps({"name": name, "workspace": "D:/somewhere/else"}),
            encoding="utf-8",
        )
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))
        # workspace 漂移的陈旧档：清掉重建（不收养错环境）
        assert env.status == EnvironmentStatus.READY.value
        stored = json.loads(_metadata_path(provider, name).read_text(encoding="utf-8"))
        assert stored["workspace"] == provider._workspace

    def test_create_query_unavailable_d6_2(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(exc=TimeoutError("wsl 超时"))
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), "cua-x"))
        # D6②：存在性未知 → 不落 READY、不写 metadata、带 query_unavailable 标记
        assert env.status == EnvironmentStatus.ERROR.value
        assert env.provider_info.get("query_unavailable") is True
        assert _metadata_path(provider, "cua-x").exists() is False

    def test_create_mkdir_deterministic_failure(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"permission denied"))
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), "cua-x"))
        # 确定性失败（非存在性未知）：error env，不带 query_unavailable
        assert env.status == EnvironmentStatus.ERROR.value
        assert env.provider_info.get("query_unavailable") is None


class TestFindByName:
    def test_find_returns_ready_env(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _create(provider, tmp_path, name="cua-ws1")
        provider._environments.clear()  # 模拟服务重启后内存登记为空
        found = _run(provider.find_environment_by_name("cua-ws1"))
        assert found is not None
        assert found.env_id == "cua-ws1"
        assert found.status == EnvironmentStatus.READY.value
        assert found.provider_info["container_name"] == "cua-ws1"
        assert "exec_backend" in found.provider_info

    def test_find_missing_metadata(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        assert _run(provider.find_environment_by_name("cua-none")) is None

    def test_find_workspace_gone_cleans_metadata(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _create(provider, tmp_path, name="cua-ws1")
        # workspace 目录被外力删除：坏环境 → 清档返回 None（上层走重建）
        import shutil as _shutil

        _shutil.rmtree(provider._workspace)
        assert _run(provider.find_environment_by_name("cua-ws1")) is None
        assert _metadata_path(provider, "cua-ws1").exists() is False

    def test_find_corrupt_metadata(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _metadata_path(provider, "cua-bad").parent.mkdir(parents=True, exist_ok=True)
        _metadata_path(provider, "cua-bad").write_text("{not json", encoding="utf-8")
        assert _run(provider.find_environment_by_name("cua-bad")) is None
        assert _metadata_path(provider, "cua-bad").exists() is False


class TestDestroyEnvironment:
    def test_destroy_registered(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _create(provider, tmp_path, name="cua-ws1")
        assert _run(provider.destroy_environment("cua-ws1")) is True
        assert _metadata_path(provider, "cua-ws1").exists() is False
        assert "cua-ws1" not in provider._environments

    def test_destroy_without_registration_idempotent_d6_3(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        # 无登记 + 无 metadata = 底层真不存在，幂等成功（非谎报：
        # wsl_native 无常驻资源，档不存在即环境不存在）
        assert _run(provider.destroy_environment("cua-never")) is True

    def test_destroy_without_registration_removes_metadata_d6_3(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _create(provider, tmp_path, name="cua-ws1")
        provider._environments.clear()  # 模拟重启后按名销毁
        assert _run(provider.destroy_environment("cua-ws1")) is True
        assert _metadata_path(provider, "cua-ws1").exists() is False

    def test_destroy_failure_reported_honestly_d6_3(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        _create(provider, tmp_path, name="cua-ws1")
        provider._environments.clear()  # 模拟重启后按名销毁

        # Path.unlink 打补丁模拟删档失败
        import pathlib

        original_unlink = pathlib.Path.unlink

        def fake_unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".json"):
                raise OSError("disk on fire")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(pathlib.Path, "unlink", fake_unlink)
        try:
            assert _run(provider.destroy_environment("cua-ws1")) is False
        finally:
            monkeypatch.undo()


# ── 5. execute_in_environment ────────────────────────────────


class TestExecute:
    def test_unknown_env(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        result = _run(provider.execute_in_environment("cua-none", {"type": "command", "command": "ls"}))
        assert result.success is False
        assert "环境不存在" in (result.error or "")

    def test_error_env_replays_error(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=None), "cua-bad"))
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "ls"}))
        assert result.success is False
        assert "工作空间为空" in (result.error or "")

    def test_command_success_output_shape(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        recorder = _Recorder(script=lambda args: (0, b"hello\n", b""))
        provider._popen_run = recorder
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "command", "command": "echo hello", "timeout": 9}
            )
        )
        assert result.success is True
        assert result.error is None
        assert result.output["stdout"] == "hello\n"
        assert result.output["stderr"] == ""
        assert result.output["return_code"] == 0
        assert result.output["command"] == "echo hello"
        # argv：wsl -d distro --cd <映射后工作目录> -- sh -c command
        args = recorder.calls[-1]
        assert args[0] == "wsl"
        assert "-d" in args and "Ubuntu" in args
        wd = args[args.index("--cd") + 1]
        assert wd == WslNativeProvider._to_wsl_path(provider._workspace)
        assert args[-3:-1] == ["bash", "-c"]
        assert args[-1] == "echo hello"

    def test_command_nonzero_exit(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"boom"))
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "false"}))
        assert result.success is False
        assert result.error == "boom"
        assert result.output["return_code"] == 1

    def test_empty_command(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": ""}))
        assert result.success is False
        assert "命令不能为空" in (result.error or "")

    def test_unsupported_op(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        result = _run(provider.execute_in_environment(env.env_id, {"type": "teleport"}))
        assert result.success is False
        assert "不支持的操作类型" in (result.error or "")

    def test_file_op_read(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (0, b"file-content", b""))
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a.txt"})
        )
        assert result.success is True
        assert result.output == "file-content"

    def test_file_op_exists_two_states(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (0, b"yes\n", b""))
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "file_operation", "operation": "exists", "path": "/workspace/a.txt"})
        )
        assert result.success is True
        assert result.output == {"exists": True}
        provider._popen_run = _Recorder(script=lambda args: (0, b"no\n", b""))
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "file_operation", "operation": "exists", "path": "/workspace/b.txt"})
        )
        assert result.output == {"exists": False}


# ── 6. 超时杀树（_popen_run 内部纪律） ───────────────────────


class _FakeProc:
    def __init__(self, pid: int, behavior: str) -> None:
        self.pid = pid
        self._behavior = behavior
        self.returncode = 0
        self.waited = False
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        if self._behavior == "timeout":
            raise subprocess.TimeoutExpired(cmd="wsl", timeout=timeout if timeout is not None else 0.0)
        return (b"out", b"err")

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


class TestPopenRunTimeout:
    def test_timeout_kills_tree_then_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(tmp_path)
        del provider._popen_run  # 还原真实实现（_make_provider 默认装了记录替身）
        proc = _FakeProc(pid=4321, behavior="timeout")
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **k: proc)
        killed: list[int] = []

        def fake_tree_kill(pid: int) -> list[str]:
            killed.append(pid)
            return []

        monkeypatch.setattr(_MOD, "kill_process_tree", fake_tree_kill)
        with pytest.raises(TimeoutError, match="命令执行超时"):
            _run(provider._popen_run(["wsl", "-d", "Ubuntu", "--", "true"], timeout=0.2))
        assert killed == [4321]
        assert proc.waited  # 树已终止后回收子进程资源

    def test_timeout_kill_failure_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(tmp_path)
        del provider._popen_run  # 还原真实实现
        proc = _FakeProc(pid=4321, behavior="timeout")
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(_MOD, "kill_process_tree", lambda pid: ["pid-4321 清理失败"])
        with pytest.raises(TimeoutError, match="清理失败"):
            _run(provider._popen_run(["wsl", "-d", "Ubuntu", "--", "true"], timeout=0.2))

    def test_normal_run_returns_triplet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(tmp_path)
        del provider._popen_run  # 还原真实实现
        proc = _FakeProc(pid=1, behavior="normal")
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **k: proc)
        rc, out, err = _run(provider._popen_run(["wsl", "--", "true"], timeout=5))
        assert (rc, out, err) == (0, b"out", b"err")


# ── 7. argv 构建 ─────────────────────────────────────────────


class TestArgvBuilders:
    def test_build_exec_argv_full(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path, user="agentos", sandbox_cmd=["bwrap", "--dev", "/dev"])
        argv = provider._build_exec_argv(
            working_dir="/workspace",
            command="echo hi",
            bridge_env={"AGENTOS_BRIDGE_URL": "http://1.2.3.4:8765"},
            timeout=30,
        )
        assert argv[:2] == ["wsl", "-d"]
        assert "Ubuntu" in argv
        assert argv[argv.index("-u") + 1] == "agentos"
        assert argv[argv.index("--cd") + 1] == provider._workspace_wsl
        assert argv[argv.index("--exec") + 1 :][:4] == [
            "env",
            "AGENTOS_BRIDGE_URL=http://1.2.3.4:8765",
            "bwrap",
            "--dev",
        ]
        assert argv[-4:-1] == ["/dev", "bash", "-c"]

    def test_build_exec_argv_minimal(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        argv = provider._build_exec_argv(working_dir=None, command="ls", bridge_env={}, timeout=5)
        assert "-u" not in argv
        assert "env" not in argv
        assert argv[-3:] == ["bash", "-c", "ls"]
        assert argv[argv.index("--exec") - 1] == provider._map_working_dir(None)

    def test_build_kill_argv(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path, user="agentos")
        argv = provider._build_kill_argv(pid=1234)
        assert argv[:2] == ["wsl", "-d"]
        assert argv[argv.index("-u") + 1] == "agentos"
        assert argv[-3:] == ["bash", "-c", "kill -9 1234"]
