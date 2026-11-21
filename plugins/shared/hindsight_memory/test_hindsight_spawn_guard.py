# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""hindsight spawn 守卫四件（方案 P0-3，2026-10-10 commit 爆涨同环修复）。

1. _build_api_env：BLAS/OMP 线程钳制默认注入，外部显式设置保留（setdefault 语义）；
2. _pre_clean_port：孤儿占端口（cmdline 含 hindsight_api.main）→ 清掉再起；
   活占用 → fail-fast 报错含占用者（不静默换端口）；无占用 → 放行；
3. _pin_process_to_job：win32 Job Object 钉死（KILL_ON_JOB_CLOSE——宿主无论
   优雅还是崩溃死亡，孙进程整树陪葬，孤儿结构性归零）；句柄存模块级防提前
   关闭；非 Windows no-op；win32 spawn 面 CREATE_SUSPENDED 起停 + 钉后恢复
   （次序契约，真进程车道见 test_hindsight_job_pin_win32.py）；
4. _start_api_server 接线：env 走 _build_api_env、spawn 前 _pre_clean_port、
   spawn 后 _pin_process_to_job（win32 挂起起停）。

mock 仅限外部依赖（netstat/wmic 子进程、ctypes、Popen）；判定逻辑走真实实现。

[来源: plugins/shared/hindsight_memory/server.py]
"""

from __future__ import annotations

import importlib.util
import subprocess  # noqa: S404
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> object:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_spawn_guard_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch):
    """加载模块并隔离外部副作用（不真 spawn/不真杀进程）。"""
    m = _load_module()
    monkeypatch.setattr(m, "kill_process_tree", lambda pid: [f"killed-{pid}"])
    return m


class TestBuildApiEnv:
    def test_default_clamps_blas_and_omp(self, mod, monkeypatch):
        monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        env = mod._build_api_env()
        assert env["OPENBLAS_NUM_THREADS"] == "1"
        assert env["OMP_NUM_THREADS"] == "1"

    def test_default_forces_utf8(self, mod, monkeypatch):
        """PYTHONUTF8 缺省注入 "1"：api 子进程 stdout 非 ctrl 台时按 ANSI 代码页
        编码，zh-CN=GBK 下启动横幅方块字符不可编码即秒死（装机 2026-10-21 实证）。"""
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        env = mod._build_api_env()
        assert env["PYTHONUTF8"] == "1"

    def test_external_setting_wins(self, mod, monkeypatch):
        """外部显式设置优先（部署调优不被插件钳制覆盖）。"""
        monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
        monkeypatch.setenv("OMP_NUM_THREADS", "2")
        monkeypatch.setenv("PYTHONUTF8", "0")
        env = mod._build_api_env()
        assert env["OPENBLAS_NUM_THREADS"] == "4"
        assert env["OMP_NUM_THREADS"] == "2"
        assert env["PYTHONUTF8"] == "0"


class TestPreCleanPort:
    def test_clear_when_no_listener(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_find_port_listener_pid", lambda port: None)
        assert "clear" in mod._pre_clean_port(8420)

    def test_orphan_listener_killed(self, mod, monkeypatch):
        """cmdline 含 hindsight_api.main 的占用者=孤儿 → 清掉放行。"""
        monkeypatch.setattr(mod, "_find_port_listener_pid", lambda port: 3176)
        monkeypatch.setattr(
            mod, "_process_cmdline", lambda pid: ["python", "-m", "hindsight_api.main"]
        )
        result = mod._pre_clean_port(8420)
        assert "3176" in result

    def test_live_foreign_listener_fails_fast(self, mod, monkeypatch):
        """无关活进程占用 → fail-fast（报错含占用者，不静默换端口）。"""
        monkeypatch.setattr(mod, "_find_port_listener_pid", lambda port: 999)
        monkeypatch.setattr(mod, "_process_cmdline", lambda pid: ["nginx"])
        with pytest.raises(RuntimeError, match="999"):
            mod._pre_clean_port(8420)


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object / CREATE_SUSPENDED 是 win32 语义，POSIX 车道跳过")
class TestPinProcessToJob:
    def test_win32_pins_with_kill_on_job_close(self, mod, monkeypatch):
        """win32：CreateJobObject→SetInformation(KILL_ON_JOB_CLOSE)→Assign，
        句柄存模块级防提前关闭。ctypes 经 sys.modules 注入（实现为函数内 import），
        Structure/基础类型用真 ctypes（winstruct 布局不被 fake 漂移）。"""
        import ctypes as real_ctypes

        calls: list[str | tuple[str, object, object]] = []

        class FakeKernel32:
            def CreateJobObjectW(self, attrs, name):
                calls.append("create")
                return 4242

            def SetInformationJobObject(self, job, cls, info, size):
                real = getattr(info, "_obj", info)  # byref → CArgObject._obj
                flags = real.BasicLimitInformation.LimitFlags
                calls.append(("set", job, flags))
                return 1

            def AssignProcessToJobObject(self, job, proc):
                calls.append(("assign", job, proc))
                return 1

        class _FakeWinDLL:
            kernel32 = FakeKernel32()

        fake_ctypes = types.ModuleType("ctypes_fake_pin")
        fake_ctypes.WinDLL = lambda _name, **_kw: _FakeWinDLL().kernel32
        fake_ctypes.get_last_error = lambda: 0
        for name in ("Structure", "c_int64", "c_uint32", "c_uint64", "c_size_t", "byref", "sizeof"):
            setattr(fake_ctypes, name, getattr(real_ctypes, name))
        monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
        fake_proc = SimpleNamespace(_handle=777, pid=123)
        mod._pin_process_to_job(fake_proc)
        assert calls[0] == "create"
        sets = [c for c in calls if isinstance(c, tuple) and c[0] == "set"]
        assert sets and sets[0][1] == 4242
        assert sets and isinstance(sets[0][2], int) and sets[0][2] & 0x2000, "LimitFlags 必须带 KILL_ON_JOB_CLOSE"
        assigns = [c for c in calls if isinstance(c, tuple) and c[0] == "assign"]
        assert assigns and assigns[0][2] == 777
        assert 4242 in getattr(mod, "_JOB_HANDLES", []), "句柄必须存模块级防提前关闭"

    def test_non_win_noop(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"), raising=False)
        mod._pin_process_to_job(SimpleNamespace(_handle=1, pid=1))  # 不抛即 no-op


class TestStartApiServerWiring:
    def test_popen_env_and_pin_wired(self, mod, monkeypatch, tmp_path):
        """接线：Popen 收钳制 env + spawn 前 _pre_clean_port + spawn 后钉 job
        （win32 挂起起停 creationflags=CREATE_SUSPENDED，POSIX 0）。"""
        venv_dir = tmp_path / ".venv-hindsight"
        venv_dir.mkdir()
        py_name = "python.exe" if sys.platform == "win32" else "python"
        (venv_dir / py_name).write_bytes(b"")
        monkeypatch.setattr(mod, "_aux_venv_python", lambda _d: str(venv_dir / py_name))
        monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        clean_calls: list[object] = []

        def _record_clean(port: object) -> str:
            clean_calls.append(port)
            return "clear"

        monkeypatch.setattr(mod, "_pre_clean_port", _record_clean)
        pinned: list[object] = []
        monkeypatch.setattr(
            mod,
            "_pin_process_to_job",
            lambda p, suspended=False: pinned.append((p.pid, suspended)),
        )
        fake_proc = SimpleNamespace(pid=555, _handle=1)
        captured: dict = {}
        fake_subprocess = types.ModuleType("subprocess_fake_guard")
        fake_subprocess.DEVNULL = -3
        fake_subprocess.PIPE = -1
        fake_subprocess.Popen = (
            lambda argv, stdout, stderr, env, creationflags: captured.update(
                argv=argv, env=env, creationflags=creationflags
            )
            or fake_proc
        )
        monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
        monkeypatch.setattr(mod, "_spawn_stream_drain", lambda p, stream, path: None)
        mod._start_api_server(8420, str(tmp_path))
        assert clean_calls == [8420], "spawn 前必须端口清障"
        expected_flags = 0x4 if sys.platform == "win32" else 0
        assert pinned == [(555, sys.platform == "win32")], (
            "spawn 后必须钉 Job Object（win32 挂起起停语义）"
        )
        assert captured["creationflags"] == expected_flags, (
            "win32 必须 CREATE_SUSPENDED 起停（真身原子入 Job），POSIX 0"
        )
        assert captured["env"]["OPENBLAS_NUM_THREADS"] == "1"
        assert captured["env"]["OMP_NUM_THREADS"] == "1"
        assert captured["env"]["PYTHONUTF8"] == "1", (
            "Popen env 必须带 PYTHONUTF8 钳制（GBK 代码页下 api 横幅秒死）"
        )
