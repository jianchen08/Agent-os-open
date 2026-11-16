# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation providers/host_provider.py 测试（providers 包覆盖补）。

覆盖：
1. 生命周期：create_environment（provider_info 平台信息/注册）、destroy_environment
   （存在/不存在/失败告警）、get_environment_status（存在/不存在）；
2. execute_in_environment 分派：command/file_operation/python_code/不支持类型；
3. _execute_command：成功/非零退出/超时杀树（真实子进程树全清）/
   杀树失败上报（warn + 错误附失败清单，不吞）/空命令/权限拒绝/
   命令不存在/异常；
4. _execute_file_op：read/write/delete/exists/不支持操作/异常；
5. _execute_python_code：真实 sandbox 成功/空代码/异常；
6. _check_workspace_permission：workspace 内/外/异常。

隔离策略：isolation_types 用真实模块；sandbox 用真实模块（host 模式已审批受信
代码路径，无外部依赖）；超时杀树用例走真实短命子进程树（30s 自灭 +
finally 兜底再清）；create_subprocess_shell 替身仅用于超时清理失败注入
等无法真实构造的故障场景。

路由钉分支：本地执行已收敛到 shell_route 五级路由单源（普查 ⚠️-6），
本文件既有用例经 autouse fixture 钉在系统 shell 分支（等价收敛前语义，
免受机器 wsl/bash 装机差异影响）；路由行为本身由 TestShellRouteConvergence
改钉覆盖。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)

if TYPE_CHECKING:
    from plugins.shared.isolation.providers.host_provider import HostProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/


def _load_mod() -> Any:
    """动态加载 providers/host_provider.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_host_provider_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "providers" / "host_provider.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    HostProvider = _MOD.HostProvider

import agentos_plugin_sdk.process_runtime.shell_route as shell_route  # noqa: E402  # 路由单源迁入 SDK（Slice 2）


@pytest.fixture(autouse=True)
def _pin_shell_route_to_shell_branch(monkeypatch: Any) -> None:
    """把 shell 路由钉在系统 shell 分支（收敛前本文件既有语义）。

    收敛后 host provider 走 shell_route 五级路由；不钉分支时，装了 wsl/bash
    的机器会改走 exec 分支，令 mock create_subprocess_shell 的既有用例失真。
    路由行为覆盖见 TestShellRouteConvergence（测试内自行改钉）。
    """

    def _no_shell(_name: str) -> str | None:
        return None

    monkeypatch.setattr(shell_route.shutil, "which", _no_shell)


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


def _provider(tmp_path: Path) -> HostProvider:
    return HostProvider(project_root=str(tmp_path))


class TestLifecycle:
    def test_get_level(self) -> None:
        assert _provider(Path()).get_level() == IsolationLevel.HOST

    def test_is_available_always_true(self) -> None:
        ok, err = _run(_provider(Path()).is_available())
        assert ok is True
        assert err is None

    def test_create_environment_registers_and_reports_platform(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        env = _run(provider.create_environment(_ctx(workspace="ws")))
        assert env.env_id == "host-t-1"
        assert env.level == IsolationLevel.HOST
        assert env.provider_type == "host"
        assert env.status == EnvironmentStatus.READY.value
        assert env.context.task_id == "t-1"
        # provider_info 平台信息性质断言：关键字段存在且与运行平台一致
        assert env.provider_info["platform"] == "Windows" or env.provider_info["platform"] == "Linux"
        assert env.provider_info["project_root"] == str(tmp_path.resolve())
        assert env.provider_info["workspace"] == "ws"
        assert env.created_at and env.last_used_at
        # 注册后可查状态
        assert _run(provider.get_environment_status("host-t-1")) == EnvironmentStatus.READY

    def test_create_environment_uses_task_id_in_env_id(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        env = _run(provider.create_environment(_ctx(task_id="t-42")))
        assert env.env_id == "host-t-42"

    def test_destroy_environment_removes_record(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        assert _run(provider.destroy_environment("host-t-1")) is True
        assert _run(provider.get_environment_status("host-t-1")) == EnvironmentStatus.STOPPED

    def test_destroy_environment_unknown_id_is_idempotent(self, tmp_path: Path) -> None:
        assert _run(_provider(tmp_path).destroy_environment("host-nope")) is True

    def test_destroy_environment_failure_logs_warning(self, tmp_path: Path, caplog: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        with caplog.at_level("WARNING", logger="plugins.shared.isolation.providers.host_provider"):
            assert _run(provider.destroy_environment("host-t-1", success=False)) is True
        assert any("git 层处理" in r.message for r in caplog.records)

    def test_get_environment_status_unknown(self, tmp_path: Path) -> None:
        assert _run(_provider(tmp_path).get_environment_status("host-nope")) == EnvironmentStatus.STOPPED


class TestExecuteDispatch:
    def test_unsupported_operation_type(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        result = _run(provider.execute_in_environment("host-t-1", {"type": "nonsense"}))
        assert result.success is False
        assert result.output is None
        assert "不支持的操作类型" in (result.error or "")

    def test_command_dispatch(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        captured: dict[str, Any] = {}

        async def fake_execute_command(operation: dict[str, Any], context: Any) -> ExecutionResult:
            captured["operation"] = operation
            captured["context"] = context
            return ExecutionResult(success=True, output="ok")

        monkeypatch.setattr(provider, "_execute_command", fake_execute_command)
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "echo hi"}))
        assert result.success is True
        assert captured["operation"]["command"] == "echo hi"
        assert captured["context"] is not None  # 环境存在时传入真实 context

    def test_file_operation_dispatch(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        captured: dict[str, Any] = {}

        async def fake_execute_file_op(operation: dict[str, Any], context: Any) -> ExecutionResult:
            captured["operation"] = operation
            return ExecutionResult(success=True, output="file-ok")

        monkeypatch.setattr(provider, "_execute_file_op", fake_execute_file_op)
        result = _run(provider.execute_in_environment("host-t-1", {"type": "file_operation", "operation": "read"}))
        assert result.success is True
        assert captured["operation"]["operation"] == "read"

    def test_python_code_dispatch(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        captured: dict[str, Any] = {}

        async def fake_execute_python_code(operation: dict[str, Any]) -> ExecutionResult:
            captured["operation"] = operation
            return ExecutionResult(success=True, output="py-ok")

        monkeypatch.setattr(provider, "_execute_python_code", fake_execute_python_code)
        result = _run(provider.execute_in_environment("host-t-1", {"type": "python_code", "code": "1+1"}))
        assert result.success is True
        assert captured["operation"]["code"] == "1+1"

    def test_execute_without_environment_passes_none_context(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        captured: dict[str, Any] = {}

        async def fake_execute_command(operation: dict[str, Any], context: Any) -> ExecutionResult:
            captured["context"] = context
            return ExecutionResult(success=True, output="ok")

        monkeypatch.setattr(provider, "_execute_command", fake_execute_command)
        _run(provider.execute_in_environment("host-missing", {"type": "command", "command": "echo hi"}))
        assert captured["context"] is None


class TestExecuteCommand:
    def test_success(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell",
            _fake_subprocess(0, b"hello\n", b""),
        )
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "echo hello"}))
        assert result.success is True
        assert result.error is None
        out = result.output or {}
        assert out["stdout"] == "hello\n"
        assert out["stderr"] == ""
        assert out["return_code"] == 0
        assert out["command"] == "echo hello"

    def test_nonzero_exit(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell",
            _fake_subprocess(2, b"", b"boom\n"),
        )
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "false"}))
        assert result.success is False
        out = result.output or {}
        assert out["return_code"] == 2
        assert out["stderr"] == "boom\n"
        assert result.error == "boom\n"

    def test_empty_command(self, tmp_path: Path) -> None:
        result = _run(_provider(tmp_path).execute_in_environment("host-t-1", {"type": "command", "command": ""}))
        assert result.success is False
        assert "命令不能为空" in (result.error or "")

    def test_timeout_kills_real_process_tree(self, tmp_path: Path) -> None:
        """超时（真实子进程树）：主进程与孙进程全清，只报超时不夹带清理失败。"""
        import time

        import psutil
        from proc_tree import kill_process_tree as _kill_tree_guard

        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        pid_file = tmp_path / "grandchild.pid"
        child_script = tmp_path / "sleepy_child.py"
        child_script.write_text(
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "exec('import time; time.sleep(30)')\n",
            encoding="utf-8",
        )
        try:
            result = _run(
                provider.execute_in_environment(
                    "host-t-1",
                    {
                        "type": "command",
                        "command": f'"{sys.executable}" "{child_script}"',
                        "timeout": 2.5,  # Windows 解释器冷启+管道层可达 1s+，超时语义不受具体值影响
                    },
                )
            )
            assert result.success is False
            assert "超时" in (result.error or "")
            assert "进程树清理失败" not in (result.error or "")
            grand_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # 树清（含 Windows 终止异步收敛）：孙进程已退出
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and psutil.pid_exists(grand_pid):
                time.sleep(0.05)
            assert psutil.pid_exists(grand_pid) is False, "孙进程成孤儿：树未被清"
        finally:
            # 兜底再清一遍（修法回退时防 30s 悬挂进程泄漏到后续用例）
            if pid_file.exists():
                text = pid_file.read_text(encoding="utf-8").strip()
                if text.isdigit():
                    _kill_tree_guard(int(text))

    def test_timeout_cleanup_failure_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: Any, caplog: Any
    ) -> None:
        """超时后杀树失败：warn 日志 + 错误消息附「进程树清理失败」清单（不吞）。"""

        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))

        class _SlowProc:
            returncode = None
            pid = 424242

            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.sleep(5)
                return b"", b""

        async def _slow_shell(*args: Any, **kwargs: Any) -> Any:
            return _SlowProc()

        def _failing_tree(pid: int) -> list[str]:
            return [f"pid={pid} kill 失败: injected"]

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell", _slow_shell
        )
        monkeypatch.setattr(_MOD, "kill_process_tree", _failing_tree)
        with caplog.at_level("WARNING", logger="plugins.shared.isolation.providers.host_provider"):
            result = _run(
                provider.execute_in_environment("host-t-1", {"type": "command", "command": "sleep 5", "timeout": 0.1})
            )
        assert result.success is False
        assert "超时" in (result.error or "")
        assert "进程树清理失败" in (result.error or "")
        assert "injected" in (result.error or "")
        assert any("进程树清理失败" in r.getMessage() for r in caplog.records)

    def test_permission_denied(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))

        async def _raise_perm(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell", _raise_perm
        )
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "x"}))
        assert result.success is False
        assert "权限不足" in (result.error or "")

    def test_file_not_found(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))

        async def _raise_fnf(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell", _raise_fnf
        )
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "nope"}))
        assert result.success is False
        assert "命令或程序不存在" in (result.error or "")

    def test_generic_exception(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))

        async def _raise_other(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell", _raise_other
        )
        result = _run(provider.execute_in_environment("host-t-1", {"type": "command", "command": "x"}))
        assert result.success is False
        assert "执行命令失败" in (result.error or "")
        assert "kaboom" in (result.error or "")

    def test_workspace_permission_denied_blocks_execution(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx(workspace="ws")))
        called: list[bool] = []

        async def _should_not_run(*args: Any, **kwargs: Any) -> Any:
            called.append(True)
            raise AssertionError("不应执行")

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell", _should_not_run
        )
        outside = tmp_path.parent / "outside" / "x.sh"
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "command", "command": "echo hi", "working_dir": str(outside)}
            )
        )
        assert result.success is False
        assert "权限拒绝" in (result.error or "")
        assert called == []


class TestExecuteFileOp:
    def test_read(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        f = tmp_path / "ws" / "a.txt"
        f.parent.mkdir()
        f.write_text("内容", encoding="utf-8")
        result = _run(
            provider.execute_in_environment("host-t-1", {"type": "file_operation", "operation": "read", "path": str(f)})
        )
        assert result.success is True
        assert result.output == "内容"

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        target = tmp_path / "ws" / "sub" / "b.txt"
        result = _run(
            provider.execute_in_environment(
                "host-t-1",
                {"type": "file_operation", "operation": "write", "path": str(target), "content": "data"},
            )
        )
        assert result.success is True
        assert target.read_text(encoding="utf-8") == "data"

    def test_delete(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        f = tmp_path / "ws" / "del.txt"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        result = _run(
            provider.execute_in_environment("host-t-1", {"type": "file_operation", "operation": "delete", "path": str(f)})
        )
        assert result.success is True
        assert not f.exists()

    def test_exists_true_and_false(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        f = tmp_path / "ws" / "e.txt"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        result = _run(
            provider.execute_in_environment("host-t-1", {"type": "file_operation", "operation": "exists", "path": str(f)})
        )
        assert result.success is True
        assert result.output == {"exists": True}
        result2 = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "file_operation", "operation": "exists", "path": str(tmp_path / "ws" / "nope.txt")}
            )
        )
        assert result2.success is True
        assert result2.output == {"exists": False}

    def test_unsupported_operation(self, tmp_path: Path) -> None:
        result = _run(
            _provider(tmp_path).execute_in_environment(
                "host-t-1", {"type": "file_operation", "operation": "chmod", "path": "x"}
            )
        )
        assert result.success is False
        assert "不支持的文件操作" in (result.error or "")

    def test_operation_error_wrapped(self, tmp_path: Path) -> None:
        result = _run(
            _provider(tmp_path).execute_in_environment(
                "host-t-1", {"type": "file_operation", "operation": "read", "path": str(tmp_path / "missing.txt")}
            )
        )
        assert result.success is False
        assert "文件操作失败" in (result.error or "")

    def test_write_outside_workspace_denied(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx(workspace="ws")))
        outside = tmp_path.parent / "outside.txt"
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "file_operation", "operation": "write", "path": str(outside), "content": "x"}
            )
        )
        assert result.success is False
        assert "权限拒绝" in (result.error or "")
        assert not outside.exists()

    def test_delete_outside_workspace_denied(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx(workspace="ws")))
        outside = tmp_path.parent / "victim.txt"
        outside.write_text("keep", encoding="utf-8")
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "file_operation", "operation": "delete", "path": str(outside)}
            )
        )
        assert result.success is False
        assert "权限拒绝" in (result.error or "")
        assert outside.read_text(encoding="utf-8") == "keep"


class TestExecutePythonCode:
    def test_success_with_real_sandbox(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "python_code", "code": "x = 1 + 1", "context": {"x": 0}}
            )
        )
        assert result.success is True
        out = result.output or {}
        assert out["output"] == ""
        assert out["return_value"] is None

    def test_empty_code_rejected(self, tmp_path: Path) -> None:
        result = _run(
            _provider(tmp_path).execute_in_environment("host-t-1", {"type": "python_code", "code": ""})
        )
        assert result.success is False
        assert "代码不能为空" in (result.error or "")

    def test_sandbox_failure_propagated(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "python_code", "code": "import os"}
            )
        )
        # 沙箱安全拒绝（白名单模式）作为结果错误直接透传
        assert result.success is False
        assert "白名单模式禁止 import 语句" in (result.error or "")

    def test_sandbox_setup_exception_wrapped(self, tmp_path: Path) -> None:
        """沙箱构造抛异常（负超时配置被拒）：落入 except 包装为失败结果。"""
        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        result = _run(
            provider.execute_in_environment(
                "host-t-1", {"type": "python_code", "code": "1+1", "timeout": -1}
            )
        )
        assert result.success is False
        assert "执行 Python 代码失败" in (result.error or "")


class TestWorkspacePermission:
    def test_inside_workspace_allowed(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        ok, err = provider._check_workspace_permission(str(ws / "sub" / "f.txt"), "ws")
        assert ok is True
        assert err is None

    def test_outside_workspace_denied(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        (tmp_path / "ws").mkdir()
        outside = tmp_path.parent / "other" / "f.txt"
        ok, err = provider._check_workspace_permission(str(outside), "ws")
        assert ok is False
        assert err is not None
        assert "权限拒绝" in err

    def test_workspace_itself_allowed(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        ok, _ = provider._check_workspace_permission(str(ws), "ws")
        assert ok is True

    def test_exception_wrapped(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = _provider(tmp_path)
        (tmp_path / "ws").mkdir()

        def _bad_resolve(p: str) -> Path:
            raise OSError("resolve failed")

        monkeypatch.setattr(Path, "resolve", _bad_resolve)
        ok, err = provider._check_workspace_permission("x", "ws")
        assert ok is False
        assert "权限检查失败" in (err or "")


# ── 外部依赖替身：subprocess（asyncio.create_subprocess_shell 的返回进程） ──


def _fake_subprocess(returncode: int, stdout: bytes, stderr: bytes) -> Any:
    """构造 create_subprocess_shell 的替身（外部依赖：子进程）。"""

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, stderr

        def kill(self) -> None:
            pass

        async def wait(self) -> None:
            pass

    async def _factory(*args: Any, **kwargs: Any) -> Any:
        return _FakeProc()

    return _factory


class TestShellRouteConvergence:
    """普查 ⚠️-6 收敛断言：host provider 与 bash 工具同走 shell_route 单源路由。

    同一台 Windows 机器上此前两套语义（bash 工具五级路由、host provider 恒
    cmd 直透传），$VAR/pipefail/路径处理不一致——收敛后两消费方调用同一
    spawn 函数，spawn 形态结构性地不可能分叉。
    """

    def test_provider_delegates_to_shared_spawn_function(self) -> None:
        """结构收敛：provider 模块绑定的 spawn 即 shell_route 单源函数对象。"""
        import agentos_plugin_sdk.process_runtime.shell_route as shell_route

        assert _MOD.spawn_local_process is shell_route.spawn_local_process

    def test_windows_host_provider_same_routing_as_bash_tool(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Windows 有 bash 无 wsl：provider 与直接调路由单源产出同一 exec argv。

        （autouse fixture 钉的系统 shell 分支在此改钉：bash 分支命中，走
        create_subprocess_exec。）
        """

        class _ExecProc:
            pid = 4242
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"ok", b""

        captured: list[tuple[Any, ...]] = []

        async def _rec_exec(*args: Any, **kwargs: Any) -> Any:
            captured.append(args)
            return _ExecProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _rec_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_shell", _rec_exec)
        monkeypatch.setattr(
            shell_route.shutil, "which", lambda name: "bash.exe" if name == "bash" else None
        )

        provider = _provider(tmp_path)
        _run(provider.create_environment(_ctx()))
        command = "echo a && echo b"
        result = _run(
            provider.execute_in_environment("host-t-1", {"type": "command", "command": command})
        )
        assert result.success is True

        _run(shell_route.spawn_local_process(command, None, {}, True))

        # provider 路径与 bash 工具路径（同函数直调）spawn 形态逐字一致：
        # MSYS bash 分支复合命令带 pipefail 前缀
        expected = ("bash", "-c", "set -o pipefail; echo a && echo b")
        assert captured[0] == expected
        assert captured[-1] == expected
