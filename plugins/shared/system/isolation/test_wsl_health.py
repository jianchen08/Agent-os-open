# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""wsl_health.ensure_docker_engine 引擎自愈测试。

背景：WSL2 VM 空闲回收 → dockerd 随 systemd 关闭 → docker 不可达；
插件运行时经 ensure_docker_engine 自愈（幂等保活会话 + 冷却防风暴 +
等待 daemon 就绪），供 DockerProvider.is_available / isolation_guard 复检调用。

平台门控：_CREATE_NO_WINDOW 随 os.name 收口（POSIX=0，非零 creationflags
在 POSIX 上抛 ValueError）；ensure_docker_engine 的 WSL 保活环节是
Windows-only 语义，POSIX 短路直达 docker 可达性。

隔离策略：powershell/wsl/docker 均属外部依赖，mock 仅限 subprocess
（run/Popen）与同模块探测函数（is_docker_reachable/_keepalive_running）；
time.sleep 掐零避免真等；平台分支 monkeypatch os.name 显式钉死，
Windows/POSIX 两分支在任意宿主 OS 上均可确定性复现。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_mod() -> Any:
    """动态加载 wsl_health.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_wsl_health_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "wsl_health.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """掐零等待循环里的真睡，测试不拖延。"""
    monkeypatch.setattr(_MOD.time, "sleep", lambda _: None)
    _MOD._last_engine_ensure = 0.0  # 每个用例从可自愈状态出发


@pytest.fixture
def windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉 Windows 平台：WSL 保活分支在 Linux CI 上同样确定性复现。"""
    monkeypatch.setattr(_MOD.os, "name", "nt")


def _probe_seq(*values: bool) -> Any:
    """按序返回可达性探测结果的迭代器（不足时恒 False）。"""
    it = iter(values)

    def _probe(timeout: float = 5.0) -> bool:
        try:
            return next(it)
        except StopIteration:
            return False

    return _probe


def _spawn_recorder() -> tuple[list[Any], Any]:
    spawned: list[Any] = []

    def _record_spawn(*args: Any, **kwargs: Any) -> None:
        spawned.append(args)

    return spawned, _record_spawn


class TestEnsureDockerEngine:
    def test_reachable_fast_path_no_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """引擎已可达 → 直接 True，零额外探测/spawn。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert spawned == []

    def test_missing_keepalive_spawns_then_healthy(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """无保活会话 → detached 拉起 sleep infinity 持有 VM，等 daemon 就绪后 True。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert len(spawned) == 1
        args = spawned[0][0]
        assert "wsl.exe" in args and "sleep infinity" in args

    def test_keepalive_present_waits_without_spawn(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """保活会话在但 daemon 未就绪 → 不重复拉起，等待后 True。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert spawned == []

    def test_cooldown_prevents_repeated_spawn(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """冷却窗口内不可重复拉起（防多插件/多轮次风暴），直接 False。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: False)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False  # 首轮拉起，等满超时
        assert len(spawned) == 1
        assert _MOD.ensure_docker_engine(timeout=5.0) is False  # 冷却内：不拉起不等待
        assert len(spawned) == 1

    def test_unhealthy_timeout_returns_false(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """等待超时仍不可达 → False（拉起已做，冷却留给下一轮）。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: False)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False
        assert len(spawned) == 1

    def test_probe_exception_treated_unreachable(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """探测依赖抛异常（docker CLI 缺失等）→ is_docker_reachable 捕获视为不可达。"""
        def _boom(*a: Any, **kw: Any) -> Any:
            raise FileNotFoundError

        monkeypatch.setattr(_MOD.subprocess, "run", _boom)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False
        assert spawned == []  # 保活在，只等待不拉起


class TestPlatformGate:
    """平台门控：_CREATE_NO_WINDOW 随 os.name 收口 + POSIX 跳过 WSL 自愈环节。"""

    def test_windows_flags_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows 语义：隐藏窗口标志保持现值 0x08000000（现行为回归）。"""
        monkeypatch.setattr(os, "name", "nt")
        mod = _load_mod()  # 常量在模块加载期按 os.name 计算，重载取值
        assert mod._CREATE_NO_WINDOW == 0x08000000

    def test_posix_flags_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX 语义：标志必须为 0——非零 creationflags 在 POSIX 抛 ValueError。"""
        monkeypatch.setattr(os, "name", "posix")
        mod = _load_mod()
        assert mod._CREATE_NO_WINDOW == 0

    def test_posix_skips_wsl_selfheal_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX 下不进 PowerShell 保活探测、不拉起 wsl.exe，直达可达性结果。"""
        monkeypatch.setattr(_MOD.os, "name", "posix")
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False))
        keepalive_probe = MagicMock()
        monkeypatch.setattr(_MOD, "_keepalive_running", keepalive_probe)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)

        assert _MOD.ensure_docker_engine() is False
        keepalive_probe.assert_not_called()
        assert spawned == []

    def test_posix_reports_reachable_when_docker_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX 下 docker 可达 → True（Linux 走 is_docker_reachable 即可）。"""
        monkeypatch.setattr(_MOD.os, "name", "posix")
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(True))
        keepalive_probe = MagicMock()
        monkeypatch.setattr(_MOD, "_keepalive_running", keepalive_probe)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)

        assert _MOD.ensure_docker_engine() is True
        keepalive_probe.assert_not_called()
        assert spawned == []

    def test_windows_keepalive_branch_intact(self, monkeypatch: pytest.MonkeyPatch, windows_platform: None) -> None:
        """Windows 现行为回归：无保活会话时仍经 PowerShell 探测 + wsl.exe 拉起。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)

        assert _MOD.ensure_docker_engine() is True
        assert len(spawned) == 1
        assert "wsl.exe" in spawned[0][0]


# ──────────────────────────────────────────────
# 探测/守护/端口转发的 subprocess 交互分支
# ──────────────────────────────────────────────


class _RunStub:
    """subprocess.run 替身：按序返回 (rc, stdout, stderr) 或抛预设异常，记录调用。"""

    def __init__(self, *results: Any) -> None:
        self.calls: list[list[Any]] = []
        self._results = list(results)

    def __call__(self, args: Any, **kwargs: Any) -> Any:
        self.calls.append(list(args))
        preset = self._results.pop(0) if self._results else (0, "ok", "")
        if isinstance(preset, Exception):
            raise preset
        rc, out, err = preset
        result = MagicMock()
        result.returncode = rc
        result.stdout = out
        result.stderr = err
        return result


class TestProbeWslAlive:
    def test_script_missing_returns_rc1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: False)
        result = _MOD.probe_wsl_alive()
        assert (result.rc, result.message) == (1, "probe script not found")

    @pytest.mark.parametrize(
        ("rc", "expect_rc", "fragment"),
        [(0, 0, "OK"), (124, 124, "timeout"), (2, 2, "disk lost"), (5, 5, "rc=5")],
    )
    def test_returncode_branches(
        self, monkeypatch: pytest.MonkeyPatch, rc: int, expect_rc: int, fragment: str
    ) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub((rc, "out", "err"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.probe_wsl_alive()
        assert result.rc == expect_rc
        assert fragment in result.message
        assert stub.calls[0][0] == "powershell" and "-File" in stub.calls[0]

    def test_subprocess_timeout_maps_to_124(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(_MOD.subprocess.TimeoutExpired(cmd="x", timeout=1))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.probe_wsl_alive()
        assert result.rc == 124 and "external" in result.message

    def test_unexpected_exception_maps_to_rc1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(OSError("powershell missing"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.probe_wsl_alive()
        assert result.rc == 1 and "powershell missing" in result.message


class TestCheckWslKernelHealth:
    @pytest.mark.parametrize(
        ("rc", "expect_rc", "fragment"),
        [(0, 0, "healthy"), (8, 8, "polluted"), (3, 3, "pollution")],
    )
    def test_returncode_branches(
        self, monkeypatch: pytest.MonkeyPatch, rc: int, expect_rc: int, fragment: str
    ) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub((rc, "", ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.check_wsl_kernel_health("/srv/agentos")
        assert result.rc == expect_rc
        assert fragment in result.message
        assert "wsl_health_probe.sh /srv/agentos" in " ".join(stub.calls[0])

    def test_script_missing_returns_rc1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: False)
        result = _MOD.check_wsl_kernel_health("/x")
        assert (result.rc, result.message) == (1, "health probe script not found")

    def test_timeout_treated_as_pollution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(_MOD.subprocess.TimeoutExpired(cmd="x", timeout=1))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.check_wsl_kernel_health("/x")
        assert result.rc == 8 and "timeout" in result.message

    def test_exception_treated_as_pollution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(OSError("wsl missing"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.check_wsl_kernel_health("/x")
        assert result.rc == 8 and "wsl missing" in result.message


class TestEnsureDockerd:
    def test_script_missing_returns_rc1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: False)
        result = _MOD.ensure_dockerd("/x")
        assert (result.rc, result.message) == (1, "daemon script not found")

    @pytest.mark.parametrize(("rc", "expect_rc"), [(0, 0), (7, 7), (9, 9)])
    def test_returncode_passthrough(
        self, monkeypatch: pytest.MonkeyPatch, rc: int, expect_rc: int
    ) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub((rc, "", ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.ensure_dockerd("/x").rc == expect_rc

    def test_timeout_maps_to_rc7(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(_MOD.subprocess.TimeoutExpired(cmd="x", timeout=1))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.ensure_dockerd("/x").rc == 7

    def test_exception_maps_to_rc7(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os.path, "exists", lambda _: True)
        stub = _RunStub(OSError("boom"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        result = _MOD.ensure_dockerd("/x")
        assert result.rc == 7 and "boom" in result.message


class TestGetWslIp:
    def test_returns_first_token_of_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub((0, "172.20.1.5 10.255.255.254\n", ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.get_wsl_ip() == "172.20.1.5"

    @pytest.mark.parametrize(("rc", "stdout"), [(1, "172.20.1.5"), (0, "   \n"), (0, "")])
    def test_failure_shapes_return_none(
        self, monkeypatch: pytest.MonkeyPatch, rc: int, stdout: str
    ) -> None:
        stub = _RunStub((rc, stdout, ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.get_wsl_ip() is None

    def test_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub(OSError("no wsl"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.get_wsl_ip() is None


class TestDockerReachability:
    def test_reachable_when_version_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub((0, "26.0.0", ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.is_docker_reachable() is True

    def test_unreachable_when_cli_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub((1, "", "Cannot connect to the Docker daemon"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.is_docker_reachable() is False

    def test_exception_means_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub(_MOD.subprocess.TimeoutExpired(cmd="docker", timeout=5))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD.is_docker_reachable() is False


class TestKeepaliveRunning:
    def test_positive_count_means_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub((0, "2", ""))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD._keepalive_running() is True

    @pytest.mark.parametrize(("rc", "stdout"), [(0, "0"), (1, "")])
    def test_zero_count_or_failure_means_not_running(
        self, monkeypatch: pytest.MonkeyPatch, rc: int, stdout: str
    ) -> None:
        stub = _RunStub((rc, stdout, "probe boom"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD._keepalive_running() is False

    def test_exception_means_not_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _RunStub(OSError("powershell gone"))
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        assert _MOD._keepalive_running() is False


class TestSetupPortForward:
    """写 bat → 提权执行 → netsh 验证三段；验证输出含前端端口才算成功。"""

    @staticmethod
    def _mock_run_seq(monkeypatch: pytest.MonkeyPatch, results: list[Any]) -> _RunStub:
        stub = _RunStub(*results)
        monkeypatch.setattr(_MOD.subprocess, "run", stub)
        return stub

    @staticmethod
    def _pin_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
        import tempfile

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        return tmp_path / "agent_portproxy.bat"

    def test_bat_rules_written_and_verify_hit_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        bat_path = self._pin_tempdir(monkeypatch, tmp_path)
        stub = self._mock_run_seq(monkeypatch, [(0, "", ""), (0, "listenport=8000 ... 10.0.0.1", "")])
        assert _MOD.setup_port_forward("10.0.0.1", "8000", "6379") is True
        bat = bat_path.read_text(encoding="gbk")
        assert "netsh interface portproxy reset" in bat
        assert "listenport=8000" in bat and "connectaddress=10.0.0.1" in bat
        assert "listenport=6379" in bat
        # 第一次调用是 powershell 提权执行 bat
        assert stub.calls[0][0] == "powershell" and "agent_portproxy.bat" in " ".join(stub.calls[0])
        # 第二次调用是 netsh 验证
        assert stub.calls[1][:4] == ["netsh", "interface", "portproxy", "show"]

    def test_verify_miss_reports_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        self._pin_tempdir(monkeypatch, tmp_path)
        self._mock_run_seq(monkeypatch, [(0, "", ""), (0, "listenport=9999", "")])
        assert _MOD.setup_port_forward("10.0.0.1", "8000", "6379") is False

    def test_run_exception_reports_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        self._pin_tempdir(monkeypatch, tmp_path)
        self._mock_run_seq(monkeypatch, [OSError("netsh gone")])
        assert _MOD.setup_port_forward("10.0.0.1", "8000", "6379") is False


# ── 保活会话句柄生命周期：句柄存引用 + terminate_keepalive 幂等终止 ──


class _FakeKeepaliveProc:
    """Popen 替身：记录 terminate/wait/kill 调用（外部依赖边界）。"""

    def __init__(self, terminate_error: Exception | None = None) -> None:
        self.terminated = False
        self.killed = False
        self.wait_timeout: float | None = None
        self._terminate_error = terminate_error

    def terminate(self) -> None:
        if self._terminate_error is not None:
            raise self._terminate_error
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def _clean_keepalive_ref():
    """每用例前后清空模块级保活句柄，防跨用例泄漏。"""
    _MOD._keepalive_proc = None
    yield
    _MOD._keepalive_proc = None


class TestKeepaliveLifecycle:
    def test_spawn_stores_module_ref_and_terminate_clears(
        self, monkeypatch: pytest.MonkeyPatch, windows_platform: None, _clean_keepalive_ref: None
    ) -> None:
        """拉起后句柄存模块级引用；terminate_keepalive 终止并清空引用。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        proc = _FakeKeepaliveProc()
        monkeypatch.setattr(_MOD.subprocess, "Popen", lambda *a, **kw: proc)

        assert _MOD.ensure_docker_engine() is True
        assert _MOD._keepalive_proc is proc, "Popen 句柄必须存引用（即弃则卸载时无从终止）"

        _MOD.terminate_keepalive()

        assert proc.terminated is True
        assert proc.wait_timeout == 5
        assert _MOD._keepalive_proc is None

    def test_terminate_kill_fallback_on_terminate_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, _clean_keepalive_ref: None
    ) -> None:
        """terminate 抛错 → 升级 kill，告警留痕。"""
        proc = _FakeKeepaliveProc(terminate_error=OSError("term denied"))
        _MOD._keepalive_proc = proc

        with caplog.at_level("WARNING"):
            _MOD.terminate_keepalive()

        assert proc.killed is True
        assert proc.terminated is False
        assert _MOD._keepalive_proc is None
        assert any("terminate 失败" in r.getMessage() for r in caplog.records)

    def test_terminate_idempotent_when_none(self, _clean_keepalive_ref: None) -> None:
        """句柄为 None（未拉起/已终止）时 terminate_keepalive 幂等空操作。"""
        assert _MOD._keepalive_proc is None
        _MOD.terminate_keepalive()  # 不抛
        assert _MOD._keepalive_proc is None
