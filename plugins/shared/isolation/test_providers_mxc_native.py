# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation providers/mxc_native_provider.py 测试。

覆盖：
1. 注册表（选择真值）：缺省两条、非法条目装载期剔除、default_profile 缺失
   回退首条、resolve_backend 未注册错误码、env_name_suffix 作用域；
2. 可用性：执行器未配置/路径不存在 → fail-closed；配置后可用；
3. create_environment：未注册 profile / 空工作空间 / 路径不存在 / 执行器
   缺失 / 备注 require_wsl=False 时成功（metadata + policy 落盘 + exec_backend
   形状）；幂等收养；profile 漂移清档重建；
4. 策略渲染：workspace 读写、cache 任务层读写、基线存在才只读、敏感目录 deny；
5. 执行：fake 执行器（JSON 信封 / 纯文本 / 非零退出 / 超时 / 请求形状）；
6. 文件操作经沙箱命令（read/write/exists 命令形状）；
7. 生命周期：find_environment_by_name（命中/缺失/workspace 消失）、destroy
   （删档幂等）。

外部依赖 mock 仅限 fake 执行器（sys.executable + 临时脚本）；metadata/policy
读写与 workspace 目录用真实文件系统（tmp_path）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/


def _load_mod() -> Any:
    mod_name = "isolation_mxc_native_provider_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _PLUGIN_DIR / "providers" / "mxc_native_provider.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
MxcNativeProvider = _MOD.MxcNativeProvider


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(
    task_id: str = "t-1",
    workspace: str | None = None,
    profile: str | None = None,
) -> IsolationContext:
    metadata = {"execution_profile": profile} if profile else None
    return IsolationContext(
        task_id=task_id,
        task_type=TaskType.ATOMIC,
        operation_type=OperationType.CODE_EXECUTION,
        workspace=workspace,
        isolation_level=IsolationLevel.CONTAINER,
        metadata=dict(metadata) if metadata else {},
    )


def _fake_executor(tmp_path: Path, body: str) -> tuple[str, list[str]]:
    """生成 fake 执行器脚本（sys.executable + 脚本 + {request}）。"""
    script = tmp_path / "fake_executor.py"
    script.write_text(body, encoding="utf-8")
    return sys.executable, [str(script), "{request}"]


def _provider(tmp_path: Path, workspace: Path | None = None, **overrides: Any) -> Any:
    ws = workspace if workspace is not None else tmp_path / "ws"
    if workspace is None:
        Path(ws).mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "state_dir": str(tmp_path / "envs"),
        "require_wsl": False,
        "executor": "",
        "image": "agentos-linux:latest",
    }
    config.update(overrides)
    return MxcNativeProvider(config)


class TestRegistry:
    def test_default_registry_both_profiles(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        assert provider.registered_profiles() == {
            "linux-bash": "wslc",
            "native-win": "processcontainer",
        }
        assert provider.default_profile() == "linux-bash"

    def test_invalid_backend_entry_stripped(self, tmp_path: Path) -> None:
        provider = _provider(
            tmp_path,
            backends={"linux-bash": "docker", "native-win": "processcontainer"},
        )
        assert provider.registered_profiles() == {"native-win": "processcontainer"}

    def test_unknown_profile_entry_stripped(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path, backends={"alien": "wslc"})
        # 全被剔除 → 回退出厂注册表
        assert provider.registered_profiles() == {
            "linux-bash": "wslc",
            "native-win": "processcontainer",
        }

    def test_default_profile_falls_back_to_registered(self, tmp_path: Path) -> None:
        provider = _provider(
            tmp_path,
            backends={"native-win": "processcontainer"},
            default_profile="linux-bash",
        )
        assert provider.default_profile() == "native-win"

    def test_resolve_backend_unregistered(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        backend, error = provider.resolve_backend("alien")
        assert backend is None
        assert error == "MXC_BACKEND_NOT_REGISTERED"

    def test_env_name_suffix_scoped_by_profile(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        assert provider.env_name_suffix({}) == ""
        assert provider.env_name_suffix({"execution_profile": "linux-bash"}) == ""
        assert provider.env_name_suffix({"execution_profile": "native-win"}) == "--native-win"


class TestAvailability:
    def test_executor_missing_fail_closed(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        ok, reason = _run(provider.is_available())
        assert ok is False
        assert "MXC_EXECUTOR_MISSING" in str(reason)

    def test_executor_path_present(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ok, reason = _run(provider.is_available())
        assert ok is True
        assert reason is None

    def test_executor_path_absent(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path, executor=str(tmp_path / "missing.exe"))
        ok, _ = _run(provider.is_available())
        assert ok is False


class TestCreateEnvironment:
    def test_unregistered_profile_error_env(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path, backends={"native-win": "processcontainer"})
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws), profile="linux-bash")))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "MXC_BACKEND_NOT_REGISTERED" in env.provider_info["error"]

    def test_empty_workspace_error_env(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=None)))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in env.provider_info["error"]

    def test_missing_workspace_error_env(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace=str(tmp_path / "nope"))))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间路径不存在" in env.provider_info["error"]

    def test_executor_missing_error_env(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws))))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "MXC_EXECUTOR_MISSING" in env.provider_info["error"]

    def test_create_success_metadata_and_backend(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        assert env.status == EnvironmentStatus.READY.value
        assert env.provider_type == "mxc_native"
        assert env.provider_info["execution_profile"] == "linux-bash"
        assert env.provider_info["mxc_backend"] == "wslc"
        backend = env.provider_info["exec_backend"]
        assert backend["backend"] == "mxc_native"
        assert backend["executor"] == str(exe)
        assert backend["workspace"] == str(ws)
        assert backend["image"] == "agentos-linux:latest"

        meta_path = Path(provider._state_dir) / "cua-ws.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["execution_profile"] == "linux-bash"
        assert meta["backend"] == "wslc"
        policy_path = Path(meta["policy_path"])
        assert policy_path.is_file()
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        assert str(ws) in policy["readwritePaths"]

    def test_create_adopts_existing(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        ctx = _ctx(workspace=str(ws))
        first = _run(provider.create_environment(ctx, container_name="cua-ws"))
        meta_path = Path(provider._state_dir) / "cua-ws.json"
        created_at = json.loads(meta_path.read_text(encoding="utf-8"))["created_at"]
        second = _run(provider.create_environment(ctx, container_name="cua-ws"))
        assert second.env_id == first.env_id
        assert json.loads(meta_path.read_text(encoding="utf-8"))["created_at"] == created_at

    def test_profile_drift_rebuilds(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        _run(
            provider.create_environment(
                _ctx(workspace=str(ws), profile="linux-bash"), container_name="cua-ws"
            )
        )
        env = _run(
            provider.create_environment(
                _ctx(workspace=str(ws), profile="native-win"), container_name="cua-ws"
            )
        )
        assert env.provider_info["execution_profile"] == "native-win"
        meta = json.loads(
            (Path(provider._state_dir) / "cua-ws.json").read_text(encoding="utf-8")
        )
        assert meta["execution_profile"] == "native-win"


class TestPolicy:
    def test_policy_cache_baseline_only_existing(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        (cache / "registry").mkdir(parents=True)
        provider = _provider(tmp_path, cache_root=str(cache))
        policy = provider._render_policy("linux-bash", str(tmp_path / "ws"), "task-1")
        assert str(tmp_path / "ws") in policy["readwritePaths"]
        assert str(cache / "tasks" / "task-1") in policy["readwritePaths"]
        assert policy["readonlyPaths"] == [str(cache / "registry")]
        # wslc 只挂授权路径，deny 清单不下发（避免解析宿主受保护目录）
        assert "deniedPaths" not in policy

    def test_policy_native_win_carries_denied_paths(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        policy = provider._render_policy("native-win", str(tmp_path / "ws"), "task-1")
        assert policy["deniedPaths"] == provider._denied_paths

    def test_policy_no_cache_root(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        policy = provider._render_policy("native-win", str(tmp_path / "ws"), "task-1")
        assert policy["readwritePaths"] == [str(tmp_path / "ws")]
        assert policy["readonlyPaths"] == []

    def test_to_wslc_path_maps_drive_and_keeps_posix(self) -> None:
        assert _MOD._to_wslc_path(r"E:\workspace\a.txt") == "/mnt/e/workspace/a.txt"
        assert _MOD._to_wslc_path("C:/ws") == "/mnt/c/ws"
        assert _MOD._to_wslc_path("/mnt/d/ws") == "/mnt/d/ws"

    def test_default_denied_paths_are_absolute(self) -> None:
        paths = _MOD._default_denied_paths()
        assert paths, "敏感目录 deny 清单不应为空"
        for raw in paths:
            assert Path(raw).is_absolute(), f"deny 路径必须是绝对路径: {raw}"

    def test_network_allow_normalizes_strings_and_keeps_rules(self, tmp_path: Path) -> None:
        provider = _provider(
            tmp_path,
            network={
                "default": "deny",
                "allow": [
                    "203.0.113.0/24",
                    {"to": [{"cidr": "198.51.100.0/24"}], "ports": [{"protocol": "tcp", "port": 443}]},
                ],
            },
        )
        network = provider.exec_backend_info(
            env_name="e",
            profile="wslc",
            backend="wslc",
            workspace=str(tmp_path),
            policy={},
        )["network"]
        assert network["egress"]["default"] == "deny"
        assert network["egress"]["allow"] == [
            {"to": [{"cidr": "203.0.113.0/24"}]},
            {"to": [{"cidr": "198.51.100.0/24"}], "ports": [{"protocol": "tcp", "port": 443}]},
        ]


class TestOutputDecode:
    def test_utf8_first(self) -> None:
        assert _MOD._decode_process_output("中文".encode()) == "中文"

    def test_falls_back_to_system_codepage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.locale, "getpreferredencoding", lambda *a, **k: "cp936")  # noqa: ARG005
        assert _MOD._decode_process_output("拒绝访问".encode("gbk")) == "拒绝访问"

    def test_empty(self) -> None:
        assert _MOD._decode_process_output(None) == ""
        assert _MOD._decode_process_output(b"") == ""


class TestExecute:
    def _ready_env(self, tmp_path: Path, body: str) -> tuple[Any, Any]:
        executor, args = _fake_executor(tmp_path, body)
        provider = _provider(tmp_path, executor=executor, executor_args=args)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        assert env.status == EnvironmentStatus.READY.value
        return provider, env

    def test_exec_json_envelope(self, tmp_path: Path) -> None:
        body = (
            "import json,sys\n"
            "req = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "print(json.dumps({'exitCode': 0, 'stdout': 'hello ' + req['containment'], 'stderr': ''}))\n"
        )
        provider, env = self._ready_env(tmp_path, body)
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "echo hi"})
        )
        assert result.success is True
        assert result.output["stdout"] == "hello wslc"
        assert result.output["return_code"] == 0

    def test_exec_nonzero_exit(self, tmp_path: Path) -> None:
        body = (
            "import json,sys\n"
            "print(json.dumps({'exitCode': 3, 'stdout': '', 'stderr': 'boom'}))\n"
        )
        provider, env = self._ready_env(tmp_path, body)
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "false"})
        )
        assert result.success is False
        assert "boom" in str(result.error)

    def test_exec_error_envelope_is_failure(self, tmp_path: Path) -> None:
        body = (
            "import json\n"
            "print(json.dumps({'error': {'code': 'malformed_request', 'message': 'bad config'}}))\n"
        )
        provider, env = self._ready_env(tmp_path, body)
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "echo hi"})
        )
        assert result.success is False
        assert "malformed_request" in str(result.error)
        assert "bad config" in str(result.error)

    def test_exec_plain_text_output(self, tmp_path: Path) -> None:
        provider, env = self._ready_env(tmp_path, "print('plain-output')\n")
        result = _run(
            provider.execute_in_environment(env.env_id, {"type": "command", "command": "echo x"})
        )
        assert result.success is True
        assert result.output["stdout"].strip() == "plain-output"

    def test_exec_timeout(self, tmp_path: Path) -> None:
        provider, env = self._ready_env(tmp_path, "import time\ntime.sleep(5)\n")
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "command", "command": "sleep 5", "timeout": 0.2}
            )
        )
        assert result.success is False
        assert "超时" in str(result.error)

    def test_exec_request_shape(self, tmp_path: Path) -> None:
        capture = tmp_path / "capture.json"
        body = (
            "import json,sys\n"
            f"req = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            f"open({str(capture)!r}, 'w', encoding='utf-8').write(json.dumps(req))\n"
            "print('ok')\n"
        )
        provider, env = self._ready_env(tmp_path, body)
        _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "command", "command": "echo hi", "working_dir": "/workspace/sub"},
            )
        )
        request = json.loads(capture.read_text(encoding="utf-8"))
        assert request["version"] == "1.0.0"
        assert request["containment"] == "wslc"
        assert request["process"]["cwd"].replace("\\", "/").endswith("/ws/sub")
        assert request["process"]["timeout"] == 30000
        assert request["wslc"]["image"] == "agentos-linux:latest"
        assert request["lifecycle"] == {"destroyOnExit": True}

    def test_native_win_request_wraps_cmd_and_opens_ui(self, tmp_path: Path) -> None:
        capture = tmp_path / "capture_native.json"
        body = (
            "import json,sys\n"
            f"req = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            f"open({str(capture)!r}, 'w', encoding='utf-8').write(json.dumps(req))\n"
            "print('ok')\n"
        )
        executor, args = _fake_executor(tmp_path, body)
        provider = _provider(tmp_path, executor=executor, executor_args=args)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(
            provider.create_environment(
                _ctx(workspace=str(ws), profile="native-win"), container_name="cua-native"
            )
        )
        assert env.status == EnvironmentStatus.READY.value
        _run(
            provider.execute_in_environment(
                env.env_id, {"type": "command", "command": "powershell -Command Get-ChildItem"}
            )
        )
        request = json.loads(capture.read_text(encoding="utf-8"))
        assert request["containment"] == "processcontainer"
        assert request["process"]["commandLine"] == (
            'cmd.exe /d /s /c "powershell -Command Get-ChildItem"'
        )
        assert request["ui"] == {"disable": False}
        assert "wslc" not in request

    def test_exec_unknown_env(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        result = _run(
            provider.execute_in_environment("missing", {"type": "command", "command": "echo"})
        )
        assert result.success is False
        assert "环境不存在" in str(result.error)

    def test_exec_unsupported_op(self, tmp_path: Path) -> None:
        provider, env = self._ready_env(tmp_path, "print('ok')\n")
        result = _run(provider.execute_in_environment(env.env_id, {"type": "alien"}))
        assert result.success is False
        assert "不支持的操作类型" in str(result.error)


class TestFileOps:
    def _env_and_capture(self, tmp_path: Path) -> tuple[Any, Any, Path]:
        capture = tmp_path / "command.txt"
        body = (
            "import json,sys\n"
            "req = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            f"open({str(capture)!r}, 'w', encoding='utf-8').write(req['process']['commandLine'])\n"
            "print('file-content')\n"
        )
        executor, args = _fake_executor(tmp_path, body)
        provider = _provider(tmp_path, executor=executor, executor_args=args)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        return provider, env, capture

    def test_read_maps_workspace_and_uses_cat(self, tmp_path: Path) -> None:
        provider, env, capture = self._env_and_capture(tmp_path)
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is True
        assert result.output.strip() == "file-content"
        command = capture.read_text(encoding="utf-8")
        assert command.startswith("cat -- ")
        expected = _MOD._to_wslc_path(str(tmp_path / "ws" / "a.txt"))
        assert expected in command

    def test_write_uses_base64(self, tmp_path: Path) -> None:
        provider, env, capture = self._env_and_capture(tmp_path)
        result = _run(
            provider.execute_in_environment(
                env.env_id,
                {
                    "type": "file_operation",
                    "operation": "write",
                    "path": "/workspace/a.txt",
                    "content": "hi",
                },
            )
        )
        assert result.success is True
        assert "base64 -d" in capture.read_text(encoding="utf-8")

    def test_exists_parses_yes(self, tmp_path: Path) -> None:
        provider, env, _ = self._env_and_capture(tmp_path)
        # fake 输出 file-content 不含 yes → False
        result = _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "file_operation", "operation": "exists", "path": "/workspace/a.txt"},
            )
        )
        assert result.success is True
        assert result.output == {"exists": False}

    def test_native_win_read_uses_powershell(self, tmp_path: Path) -> None:
        body = (
            "import json,sys\n"
            "req = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            f"open({str(tmp_path / 'cmd_win.txt')!r}, 'w', encoding='utf-8')"
            ".write(req['process']['commandLine'])\n"
            "print('win-file-content')\n"
        )
        executor, args = _fake_executor(tmp_path, body)
        provider = _provider(tmp_path, executor=executor, executor_args=args)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(
            provider.create_environment(
                _ctx(workspace=str(ws), profile="native-win"), container_name="cua-win"
            )
        )
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a.txt"}
            )
        )
        assert result.success is True
        command = (tmp_path / "cmd_win.txt").read_text(encoding="utf-8")
        assert "powershell" in command
        assert "cat -- " not in command

    def test_unsupported_file_op(self, tmp_path: Path) -> None:
        provider, env, _ = self._env_and_capture(tmp_path)
        result = _run(
            provider.execute_in_environment(
                env.env_id, {"type": "file_operation", "operation": "chmod", "path": "x"}
            )
        )
        assert result.success is False


class TestLifecycle:
    def test_find_by_name_hit_and_missing(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        found = _run(provider.find_environment_by_name("cua-ws"))
        assert found is not None
        assert found.status == EnvironmentStatus.READY.value
        assert _run(provider.find_environment_by_name("missing")) is None

    def test_find_by_name_workspace_gone_clears(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        ws.rmdir()
        assert _run(provider.find_environment_by_name("cua-ws")) is None
        assert not (Path(provider._state_dir) / "cua-ws.json").exists()

    def test_destroy_removes_metadata_and_policy_idempotent(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        assert _run(provider.destroy_environment(env.env_id)) is True
        assert not (Path(provider._state_dir) / "cua-ws.json").exists()
        assert not (Path(provider._state_dir) / "cua-ws.policy.json").exists()
        assert _run(provider.destroy_environment(env.env_id)) is True

    def test_status(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        provider = _provider(tmp_path, executor=str(exe))
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws"))
        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.READY
        assert _run(provider.get_environment_status("missing")) == EnvironmentStatus.STOPPED
