# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""isolation providers/linux_native_provider.py 测试。

覆盖：
1. 配置与构造：默认/自定义 config、get_level；
2. 后端元数据：exec_backend_info 形状（sandbox_prefix 隔离模板：根 ro +
   workspace rw + 独立 proc/dev/tmpfs + unshare-pid + 默认禁网 + die-with-parent
   + setenv 注入；network=true 无 --unshare-net——删值实验性质断言）；
3. 可用性：is_available（bwrap 缺失指引双出路/bash 缺失/自检 rc!=0/探测异常
   fail-closed/真实自检通过）；
4. 生命周期（D6 语义过继）：
   - create_environment 成功（metadata 落盘 + exec_backend 注入 provider_info）
     /空工作空间/宿主路径不存在/档写入失败；
   - D6① 幂等收养：遗留 metadata 且 workspace 一致 → 收养（created_at 保持
     种子值证明未重建）；workspace 漂移的陈旧 metadata → 清档重建；
   - find_environment_by_name：命中/缺失/workspace 已消失（清档返 None，
     对应 docker 坏容器探针删除重建）/metadata 损坏；
   - D6③ destroy_environment：有登记删档/无登记按名删（缺档幂等 True）/
     删档失败如实 False（不谎报）；
5. 执行：execute_in_environment（未知环境/错误环境回放/成功输出形状/非零退出/
   空命令/不支持操作/file_operation read-write-exists/argv 形状：prefix 透传 +
   --chdir + -- + bash -c）；
6. 超时杀树：_popen_run 超时杀进程树（杀净 → TimeoutError）/杀树失败
   （错误消息携带失败清单，不吞）；
7. 契约 parity：ExecutionResult 形状与 error 语义钉死（与 docker/wsl_native
   可互换性契约）。

隔离策略：isolation_types 用真实模块；bwrap 属外部依赖，mock 仅限
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
    from plugins.shared.system.isolation.providers.linux_native_provider import LinuxNativeProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/


def _load_mod() -> Any:
    """动态加载 providers/linux_native_provider.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_linux_native_provider_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _PLUGIN_DIR / "providers" / "linux_native_provider.py"
    )
    assert spec is not None
    assert spec.loader is not None
    # 先移除模块体将注入的共享根，使 sys.path.insert 分支确定性执行
    # （同进程先导测试可能已插入同一路径，令该分支被跳过）
    shared_root = str((_PLUGIN_DIR / "providers" / "linux_native_provider.py").resolve().parents[2])
    if shared_root in sys.path:
        sys.path.remove(shared_root)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    LinuxNativeProvider = _MOD.LinuxNativeProvider


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
        "state_dir": str(tmp_path / "envs"),
        "workspace": str(ws),
    }
    config.update(overrides)
    provider = LinuxNativeProvider(config)
    provider._popen_run = _Recorder()  # type: ignore[method-assign]
    return provider


def _create(provider: Any, tmp_path: Path, name: str = "cua-ws1") -> Any:
    return _run(provider.create_environment(_ctx(workspace=provider._workspace), name))


def _metadata_path(provider: Any, name: str) -> Path:
    return Path(provider._state_dir) / f"{name}.json"


# ── 1. 配置与构造 ────────────────────────────────────────────


class TestConfig:
    def test_default_config(self, tmp_path: Path) -> None:
        provider = LinuxNativeProvider({})
        assert provider._network_enabled is False
        assert provider._bwrap_path == "bwrap"
        assert str(provider._state_dir).endswith("linux_native_envs")

    def test_custom_config(self, tmp_path: Path) -> None:
        provider = LinuxNativeProvider(
            {
                "network": True,
                "bwrap_path": "/usr/local/bin/bwrap",
                "state_dir": str(tmp_path / "s"),
                "bridge_url": "http://1.2.3.4:8765",
            }
        )
        assert provider._network_enabled is True
        assert provider._bwrap_path == "/usr/local/bin/bwrap"
        assert provider._state_dir == str(tmp_path / "s")
        assert provider._bridge_url == "http://1.2.3.4:8765"

    def test_get_level_is_container(self) -> None:
        assert LinuxNativeProvider({}).get_level() == IsolationLevel.CONTAINER


# ── 2. 后端元数据（sandbox_prefix 隔离模板）──────────────────


class TestExecBackendInfo:
    def test_shape_and_backend_tag(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        info = provider.exec_backend_info()
        assert info["backend"] == "linux_native"
        assert info["bwrap_path"] == "bwrap"
        assert info["network"] is False
        assert info["state_dir"] == provider._state_dir
        assert info["workspace"] == provider._workspace
        assert isinstance(info["sandbox_prefix"], list)

    def test_sandbox_prefix_isolation_template(self, tmp_path: Path) -> None:
        """默认模板：根 ro + workspace rw(/workspace) + 独立 proc/dev/tmpfs
        + unshare-pid + 禁网 + die-with-parent。"""
        provider = _make_provider(tmp_path, network=False)
        prefix = provider.exec_backend_info()["sandbox_prefix"]
        ws = provider._workspace
        assert prefix[0] == "bwrap"
        assert ["--ro-bind", "/", "/"] == prefix[1:4]
        assert ["--bind", ws, ws] == prefix[4:7]  # 同路径 rw-bind（真实路径覆盖）
        assert "--proc" in prefix and prefix[prefix.index("--proc") + 1] == "/proc"
        assert "--dev" in prefix and prefix[prefix.index("--dev") + 1] == "/dev"
        assert "--tmpfs" in prefix and prefix[prefix.index("--tmpfs") + 1] == "/tmp"
        assert "--unshare-pid" in prefix
        assert "--unshare-net" in prefix  # 默认禁网
        assert "--die-with-parent" in prefix
        # 选项段不含执行面（chdir/命令由消费方续接）
        assert "--chdir" not in prefix
        assert "bash" not in prefix

    def test_sandbox_prefix_network_open_removes_unshare_net(self, tmp_path: Path) -> None:
        """network=true → 无 --unshare-net（Codex network_access 开关精神）。
        删值实验性质：翻转配置项，对应 flag 必须随之消失。"""
        provider = _make_provider(tmp_path, network=True)
        prefix = provider.exec_backend_info()["sandbox_prefix"]
        assert "--unshare-net" not in prefix

    def test_sandbox_prefix_setenv_injection(self, tmp_path: Path) -> None:
        """bridge + runtime env 经 --setenv 注入（缓存副作用约束进 workspace）。"""
        provider = _make_provider(
            tmp_path, bridge_url="http://1.2.3.4:8765", network=False
        )
        _run(provider.create_environment(_ctx(workspace=provider._workspace), "cua-ws1"))
        prefix = provider.exec_backend_info()["sandbox_prefix"]
        joined = " ".join(prefix)
        rt = f"{provider._workspace.rstrip('/')}/.task_runtime"
        assert "--setenv AGENTOS_BRIDGE_URL http://1.2.3.4:8765" in joined
        assert f"--setenv XDG_CACHE_HOME {rt}/cache" in joined
        assert f"--setenv npm_config_cache {rt}/npm-cache" in joined
        assert f"--setenv PYTHONUSERBASE {rt}/pyuser" in joined

    def test_sandbox_prefix_no_runtime_env_without_workspace(self, tmp_path: Path) -> None:
        """workspace 未知（异常形态）不注入 runtime env，防值指向文件系统根。"""
        provider = _make_provider(tmp_path)
        provider._workspace = ""
        prefix = provider.exec_backend_info()["sandbox_prefix"]
        assert not any("XDG_CACHE_HOME" in a for a in prefix)


# ── 3. is_available ──────────────────────────────────────────


class TestAvailability:
    def test_bwrap_missing_fail_closed_with_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bwrap 缺失 → False + 双出路指引（装 bubblewrap / 显式回退 Docker），
        禁止静默回退（F11）。"""
        monkeypatch.setattr(_MOD.shutil, "which", lambda name: None)
        ok, reason = _run(LinuxNativeProvider({}).is_available())
        assert ok is False
        assert "bubblewrap" in (reason or "")
        assert "linux_native.enabled" in (reason or "")
        assert "Docker" in (reason or "")

    def test_bash_missing_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name == "bwrap" else None

        monkeypatch.setattr(_MOD.shutil, "which", _which)
        ok, reason = _run(LinuxNativeProvider({}).is_available())
        assert ok is False
        assert "bash" in (reason or "")

    def test_probe_spawn_failure_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实沙箱自检非零（如内核关闭 unprivileged userns）→ False 携带细节。"""
        monkeypatch.setattr(_MOD.shutil, "which", lambda name: f"/usr/bin/{name}")
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"operation not permitted"))
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "operation not permitted" in (reason or "")
        # 自检走最小真实沙箱（-- 分隔真命令），非 only-which
        argv = provider._popen_run.calls[0]
        assert argv[-2:] == ["--", "/bin/true"]
        assert "--unshare-pid" in argv and "--unshare-net" in argv

    def test_probe_exception_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda name: f"/usr/bin/{name}")
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(exc=TimeoutError("bwrap 假死"))
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "bwrap 假死" in (reason or "")  # 携带异常摘要，不静默

    def test_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.shutil, "which", lambda name: f"/usr/bin/{name}")
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (0, b"", b""))
        ok, reason = _run(provider.is_available())
        assert ok is True
        assert reason is None


# ── 4. 生命周期（D6 语义过继） ───────────────────────────────


class TestCreateEnvironment:
    def test_create_success(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        name = "cua-ws1"
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))

        assert env.env_id == name
        assert env.level == IsolationLevel.CONTAINER
        assert env.provider_type == "linux_native"
        assert env.status == EnvironmentStatus.READY.value
        info = env.provider_info
        assert info["container_name"] == name
        assert info["workspace"] == provider._workspace
        assert info["exec_backend"]["backend"] == "linux_native"
        assert info["exec_backend"]["workspace"] == provider._workspace
        # metadata 已落盘且已注册
        assert _metadata_path(provider, name).is_file()
        assert name in provider._environments
        stored = json.loads(_metadata_path(provider, name).read_text(encoding="utf-8"))
        assert stored["workspace"] == provider._workspace
        assert stored["created_at"]

    def test_create_empty_workspace_rejected(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=None), "cua-x"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in str(env.provider_info.get("error"))
        assert "query_unavailable" not in env.provider_info
        assert _metadata_path(provider, "cua-x").exists() is False

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
                    "created_at": "2026-10-12T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))
        # D6①：按名收养（created_at 保持种子值 = 未重建）
        assert env.status == EnvironmentStatus.READY.value
        assert env.env_id == name
        stored = json.loads(_metadata_path(provider, name).read_text(encoding="utf-8"))
        assert stored["created_at"] == "2026-10-12T00:00:00+00:00"
        assert env.provider_info["container_name"] == name

    def test_create_stale_metadata_recreated(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        name = "cua-ws1"
        _metadata_path(provider, name).parent.mkdir(parents=True, exist_ok=True)
        _metadata_path(provider, name).write_text(
            json.dumps({"name": name, "workspace": "/somewhere/else"}),
            encoding="utf-8",
        )
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), name))
        # workspace 漂移的陈旧档：清掉重建（不收养错环境）
        assert env.status == EnvironmentStatus.READY.value
        stored = json.loads(_metadata_path(provider, name).read_text(encoding="utf-8"))
        assert stored["workspace"] == provider._workspace

    def test_create_metadata_write_failure(self, tmp_path: Path) -> None:
        """state_dir 指向普通文件 → 写档 OSError → error env（确定性失败）。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        provider = _make_provider(tmp_path, state_dir=str(blocker))
        env = _run(provider.create_environment(_ctx(workspace=provider._workspace), "cua-x"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "metadata 档写入失败" in str(env.provider_info.get("error"))


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
        # 原生后端无常驻资源，档不存在即环境不存在）
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
        # argv：[sandbox_prefix] + --chdir <workspace 真实路径> + -- + bash -c command
        args = recorder.calls[-1]
        prefix = provider.exec_backend_info()["sandbox_prefix"]
        assert args[: len(prefix)] == prefix  # 模板原样透传，不重建
        wd = args[args.index("--chdir") + 1]
        assert wd == provider._workspace  # /workspace 缺省映射到环境真实路径
        assert args[-4:-1] == ["--", "bash", "-c"]
        assert args[-1] == "echo hello"

    def test_command_working_dir_mapping(self, tmp_path: Path) -> None:
        """/workspace 约定 → 环境真实路径；绝对路径透传；相对落 workspace。"""
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        ws = provider._workspace
        for wd, expected in (
            ("/workspace", ws),
            ("/workspace/sub/f.txt", ws + "/sub/f.txt"),
            ("/tmp/other", "/tmp/other"),
            ("relative/dir", ws),
            (None, ws),
            ("", ws),
        ):
            recorder = _Recorder()
            provider._popen_run = recorder
            _run(
                provider.execute_in_environment(
                    env.env_id,
                    {"type": "command", "command": "ls", "working_dir": wd},
                )
            )
            args = recorder.calls[-1]
            assert args[args.index("--chdir") + 1] == expected, f"wd={wd!r}"

    def test_working_dir_backslash_normalized(self, tmp_path: Path) -> None:
        """工具层 ntpath 规整产物（反斜杠 workspace 形态）同命中映射（wsl 同契约）。"""
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        recorder = _Recorder()
        provider._popen_run = recorder
        _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "command", "command": "ls", "working_dir": "\\workspace\\sub"},
            )
        )
        args = recorder.calls[-1]
        assert args[args.index("--chdir") + 1] == provider._workspace + "/sub"

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
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is True
        assert result.output == "file-content"
        # 读走沙箱（cat 经隔离模板），/workspace 约定映射到环境真实路径
        args = provider._popen_run.calls[-1]
        assert args[-2:] == ["cat", f"{provider._workspace}/a.txt"]

    def test_file_op_write(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder()
        result = _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "file_operation", "operation": "write", "path": "/workspace/d/a.txt", "content": "x"},
            )
        )
        assert result.success is True
        args = provider._popen_run.calls[-1]
        # 写走沙箱 sh + python3（写域边界由挂载模板强制，不重抄）
        assert args[args.index("--") + 1 : args.index("--") + 3] == ["sh", "-c"]
        assert f"{provider._workspace}/d" in args
        assert f"{provider._workspace}/d/a.txt" in args

    def test_file_op_exists_two_states(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (0, b"yes\n", b""))
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "exists", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is True
        assert result.output == {"exists": True}
        provider._popen_run = _Recorder(script=lambda args: (0, b"no\n", b""))
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "exists", "path": "/workspace/b.txt"}
            )
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
            raise subprocess.TimeoutExpired(cmd="bwrap", timeout=timeout if timeout is not None else 0.0)
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
            _run(provider._popen_run(["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"], timeout=0.2))
        assert killed == [4321]
        assert proc.waited  # 树已终止后回收子进程资源

    def test_timeout_kill_failure_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(tmp_path)
        del provider._popen_run  # 还原真实实现
        proc = _FakeProc(pid=4321, behavior="timeout")
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(_MOD, "kill_process_tree", lambda pid: ["pid-4321 清理失败"])
        with pytest.raises(TimeoutError, match="清理失败"):
            _run(provider._popen_run(["bwrap", "--", "/bin/true"], timeout=0.2))

    def test_normal_run_returns_triplet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_provider(tmp_path)
        del provider._popen_run  # 还原真实实现
        proc = _FakeProc(pid=1, behavior="normal")
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **k: proc)
        rc, out, err = _run(provider._popen_run(["bwrap", "--", "/bin/true"], timeout=5))
        assert (rc, out, err) == (0, b"out", b"err")


# ── 6.5 状态与失败分支 ───────────────────────────────────────


class TestStatusAndFailureBranches:
    def test_get_environment_status_two_states(self, tmp_path: Path) -> None:
        """登记即 READY（沙箱环境无运行态）；未知环境 STOPPED。"""
        provider = _make_provider(tmp_path)
        assert _run(provider.get_environment_status("cua-none")) == EnvironmentStatus.STOPPED
        _create(provider, tmp_path, name="cua-ws1")
        assert _run(provider.get_environment_status("cua-ws1")) == EnvironmentStatus.READY

    def test_exec_timeout_returns_error(self, tmp_path: Path) -> None:
        """命令超时 → 失败结果携带超时语义（不向上抛 Core 异常）。"""
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(exc=TimeoutError("命令执行超时（5秒）"))
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "sleep 100", "timeout": 5})
        )
        assert result.success is False
        assert "超时" in (result.error or "")

    def test_exec_exception_returns_error(self, tmp_path: Path) -> None:
        """spawn 异常（如 bwrap 不在）→ 失败结果携带原因，不吞。"""
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(exc=FileNotFoundError("bwrap: No such file"))
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "ls"}))
        assert result.success is False
        assert "执行命令失败" in (result.error or "")

    def test_file_op_read_failure(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"cat: No such file"))
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/none.txt"}
            )
        )
        assert result.success is False
        assert "读取失败" in (result.error or "")

    def test_file_op_write_failure(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"read-only file system"))
        result = _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "file_operation", "operation": "write", "path": "/workspace/a.txt", "content": "x"},
            )
        )
        assert result.success is False
        assert "写入失败" in (result.error or "")

    def test_file_op_unsupported_name(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "chmod", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is False
        assert "不支持的文件操作" in (result.error or "")

    def test_file_op_exception_wrapped(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")

        async def _boom(args: Any, timeout: float = 30) -> tuple[int, bytes, bytes]:
            raise OSError("sandbox spawn failed")

        provider._popen_run = _boom  # type: ignore[method-assign]
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is False
        assert "文件操作失败" in (result.error or "")

    def test_bridge_token_env_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "tok-1")
        provider = _make_provider(tmp_path)
        prefix = provider._sandbox_prefix()
        assert "--setenv AGENTOS_BRIDGE_TOKEN tok-1" in " ".join(prefix)


# ── 7. 契约 parity（与 docker/wsl_native 可互换性契约钉死）──


class TestContractParity:
    def test_execution_result_output_keys_parity(self, tmp_path: Path) -> None:
        """成功命令输出形状 = docker/wsl_native 契约（四键全集）。"""
        provider = _make_provider(tmp_path)
        env = _create(provider, tmp_path, name="cua-ws1")
        provider._popen_run = _Recorder(script=lambda args: (0, b"o", b"e"))
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "x"}))
        assert set(result.output.keys()) == {"stdout", "stderr", "return_code", "command"}

    def test_error_env_semantics_parity(self, tmp_path: Path) -> None:
        """error env 形状对齐：env_id 前缀/provider_type/错误回放不推进。"""
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=None), "cua-bad"))
        assert env.env_id.startswith("linux-")
        assert env.provider_type == "linux_native"
        assert env.status == EnvironmentStatus.ERROR.value
        result = _run(provider.execute_in_environment(env.env_id, {"type": "command", "command": "x"}))
        assert result.success is False
        assert "工作空间为空" in (result.error or "")

    def test_destroy_idempotency_parity(self, tmp_path: Path) -> None:
        """按名销毁幂等成功（D6③ 不谎报口径与 wsl_native 一致）。"""
        provider = _make_provider(tmp_path)
        assert _run(provider.destroy_environment("cua-never")) is True
        assert _run(provider.destroy_environment("cua-never")) is True
