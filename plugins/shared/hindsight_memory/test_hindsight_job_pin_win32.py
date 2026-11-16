# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""P0-3 Job 钉死真进程车道（2026-10-20 装机双孤儿归因复现，win32 真实 FFI）。

归因实锤（本机 FFI 探针 + 装机证据链，详见台账 R11）：
- 装机 1.0.8 Job 代码在位（agent-os/resources server.py _pin_process_to_job），
  宿主死亡后跳板（.venv-hindsight python.exe）与真身（uv cpython）双存活成孤儿；
- 根因 = JOBOBJECT_EXTENDED_LIMIT_INFORMATION 的 ctypes 结构不完整：现状 IoInfo
  误写 8 quadword（真实 IO_COUNTERS=6），且缺 IoInfo 之后的
  ProcessMemoryLimit/JobMemoryLimit/PeakProcessMemoryUsed/PeakJobMemoryUsed
  四个 SIZE_T——sizeof=128 ≠ 内核期望 144 → SetInformationJobObject 恒
  ERROR_BAD_LENGTH(24) → 提前 return → AssignProcessToJobObject 从未执行，
  钉死全程静默失效（本机探针：144 字节 Set 成功且回读 LimitFlags=0x2000；
  128/112 均 FAIL err=24）。

三测分工：
1. 挂起起停 + 钉死成功路径（真 FFI：句柄登记、恢复后跳板活、孙出生）；
2. kill-on-close 端到端：关 Job 句柄（模拟宿主死亡时其句柄全关）→ 跳板+孙
   整树陪葬；
3. 竞态对照（防回退锚）：孙先于 Assign 出生 → 孙留在 Job 外存活——锁死
   spawn 面 CREATE_SUSPENDED 次序契约的存在理由，删挂起必红。

trampoline 树用 cmd→ping 恒在件（R3 host_job 真进程先例同构）：真 hindsight
冷启需 900s 级 venv 供给且归因不依赖，dev 车道不供给。mock 仅限外部依赖
（ctypes/sys.platform）；判定逻辑走真实实现与真实内核对象。

[来源: plugins/shared/hindsight_memory/win_job.py（2026-11-06 自 server.py 平移）]
"""

from __future__ import annotations

import ctypes
import importlib.util
import subprocess  # noqa: S404
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

win32_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Job Object / CREATE_SUSPENDED 是 win32 语义，POSIX 车道跳过",
)

_CREATE_SUSPENDED = 0x00000004  # Win32 CREATE_SUSPENDED（winbase.h 常量值）

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import win_job  # noqa: E402 —— 钉死/恢复簇属主模块（平移后 patch 目标）


def _load_module() -> object:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_job_pin_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ── 进程树工具（Toolhelp32 快照，真实内核对象，无 mock）──────────────────


class _ProcessEntry32W(ctypes.Structure):
    """PROCESSENTRY32W（tlhelp32.h，ctypes 算对齐）。"""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _snapshot_pid_ppid_name() -> list[tuple[int, int, str]]:
    """全进程快照 [(pid, ppid, 镜像名), ...]。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    assert snap not in (0, -1), "Toolhelp 进程快照失败（测试自证前提不成立）"
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
    entries: list[tuple[int, int, str]] = []
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        entries.append(
            (entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile)
        )
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return entries


def _child_pids(parent_pid: int, image_name: str = "ping.exe") -> list[int]:
    """指定镜像名的直接子进程（按名过滤：cmd 下瞬时杂子不冒充孙进程）。"""
    return [
        pid
        for pid, ppid, name in _snapshot_pid_ppid_name()
        if ppid == parent_pid and name.lower() == image_name
    ]


def _pid_alive(pid: int) -> bool:
    return any(pid == p for p, _, _ in _snapshot_pid_ppid_name())


def _wait_child(parent_pid: int, deadline_s: float) -> list[int]:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        found = _child_pids(parent_pid)
        if found:
            return found
        time.sleep(0.2)
    return []


def _wait_gone(pid: int, deadline_s: float) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


def _close_job_handles(mod: object) -> None:
    """关掉模块登记的全部 Job 句柄（=模拟宿主进程死亡时句柄全关）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for handle in list(getattr(mod, "_JOB_HANDLES", [])):
        k32.CloseHandle(handle)
    mod._JOB_HANDLES.clear()  # type: ignore[attr-defined]


def _kill_tree(pid: int) -> None:
    """收尾兜底清场（正常路径 Job 关闭已杀树，此处只扫尾）。"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )


def _spawn_trampoline(suspended: bool) -> subprocess.Popen[bytes]:
    """cmd /c ping：cmd=跳板（spawn 后等待子进程），ping=孙（真身同位）。"""
    return subprocess.Popen(
        ["cmd.exe", "/c", "ping", "-n", "20", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_SUSPENDED if suspended else 0,
    )


# ── 真进程三测（win32）──────────────────────────────────────────────


@win32_only
class TestJobPinRealProcess:
    def test_suspend_pin_registers_handle_and_tree_alive(self):
        """挂起起停→钉死→恢复：句柄登记 + 恢复后跳板活、孙出生。

        现状红点：结构 128 字节 → SetInfo 恒败 → 提前 return → 句柄不登记。
        """
        mod = _load_module()
        proc = _spawn_trampoline(suspended=True)
        try:
            mod._pin_process_to_job(proc, suspended=True)
            assert mod._JOB_HANDLES, (
                "钉死成功路径必须登记 Job 句柄"
                "（现状：SetInfo 因结构 128≠144 恒败提前 return）"
            )
            assert proc.poll() is None, "挂起起停恢复后跳板必须存活"
            assert _wait_child(proc.pid, 10), "恢复后跳板必须能生成孙进程（ping）"
        finally:
            _close_job_handles(mod)
            _kill_tree(proc.pid)

    def test_job_close_kills_trampoline_and_grandchild(self):
        """kill-on-close 端到端：关 Job 句柄 → 跳板+孙整树陪葬。

        现状红点：无 Job → 跳板与孙都死不了（装机 10-20 双孤儿形态复现）。
        """
        mod = _load_module()
        proc = _spawn_trampoline(suspended=True)
        try:
            mod._pin_process_to_job(proc, suspended=True)
            assert mod._JOB_HANDLES, "钉死登记（同上）"
            grandchildren = _wait_child(proc.pid, 10)
            assert grandchildren, "孙进程出生（uv cpython 真身同位）"
            _close_job_handles(mod)
            assert _wait_gone(proc.pid, 5), "跳板必须随 Job 关闭陪葬（现状红）"
            for pid in grandchildren:
                assert _wait_gone(pid, 5), "孙进程必须整树陪葬（现状红）"
        finally:
            _kill_tree(proc.pid)

    def test_grandchild_born_before_assign_escapes_job(self):
        """竞态对照（防回退锚）：孙先出生后钉 → 跳板陪葬、孙留 Job 外存活。

        锁死 spawn 面 CREATE_SUSPENDED 次序契约：无挂起时 Assign 前孙可能已
        出生（venv 跳板启动即 spawn 真身），关 Job 只杀跳板、真身成新孤儿。
        删挂起改回普通起停必红（孙将随跳板同葬或早逃逸，语义漂移）。
        现状红点：无 Job → 跳板不死。
        """
        mod = _load_module()
        proc = _spawn_trampoline(suspended=False)
        grandchildren: list[int] = []
        try:
            grandchildren = _wait_child(proc.pid, 10)
            assert grandchildren, "前置：孙进程必须已出生（否则不构成竞态前提）"
            mod._pin_process_to_job(proc, suspended=False)
            assert mod._JOB_HANDLES, "修复后 Assign 成功必须登记句柄（现状红）"
            _close_job_handles(mod)
            assert _wait_gone(proc.pid, 5), "跳板在 Job 内必须陪葬（现状红）"
            time.sleep(1.5)  # kill-on-close 传播余量：确认孙未被牵连
            for pid in grandchildren:
                assert _pid_alive(pid), "孙先于 Assign 出生 → 必须留在 Job 外存活"
        finally:
            for pid in grandchildren:
                _kill_tree(pid)
            _kill_tree(proc.pid)


# ── 失败分支与次序契约（fake ctypes，全平台可跑）──────────────────────


class _FakeKernel32Pin:
    """可编程 fake kernel32：记录调用、按预设返回值驱动失败分支。

    方法名镜像 Win32 API（N802 豁免）——假替身的契约面就是 API 本名。
    """

    def __init__(self, *, create=4242, set_info=1, assign=1):
        self.calls: list[tuple] = []
        self._create = create
        self._set_info = set_info
        self._assign = assign

    def CreateJobObjectW(self, attrs, name):  # noqa: N802
        self.calls.append(("create",))
        return self._create

    def SetInformationJobObject(self, job, cls, info, size):  # noqa: N802
        real = getattr(info, "_obj", info)  # byref → CArgObject._obj
        self.calls.append(("set", job, real.BasicLimitInformation.LimitFlags, size))
        return self._set_info

    def AssignProcessToJobObject(self, job, proc):  # noqa: N802
        self.calls.append(("assign", job, proc))
        return self._assign

    def CloseHandle(self, handle):  # noqa: N802
        self.calls.append(("close", handle))
        return 1


def _fake_ctypes(kernel32: object) -> types.ModuleType:
    """假 ctypes 模块：WinDLL 直接返回注入的 fake kernel32（与实现调用形态
    `WinDLL(...).<函数属性>` 同形），结构类型用真 ctypes。"""
    import ctypes as real_ctypes

    fake = types.ModuleType("ctypes_fake_jobpin")
    fake.WinDLL = lambda _name, **_kw: kernel32  # 实现以 use_last_error=True 调用
    fake.get_last_error = lambda: 24  # ERROR_BAD_LENGTH（结构尺寸不符的实机实测值）
    for name in (
        "Structure",
        "c_int64",
        "c_uint32",
        "c_uint64",
        "c_size_t",
        "c_long",
        "byref",
        "sizeof",
    ):
        setattr(fake, name, getattr(real_ctypes, name))
    return fake


@pytest.fixture
def mod_win(monkeypatch: pytest.MonkeyPatch):
    """假 win32 平台模块实例（分支测试全平台可跑）。"""
    m = _load_module()
    monkeypatch.setattr(m, "sys", SimpleNamespace(platform="win32"), raising=False)
    return m


@win32_only
class TestPinBranchContract:
    def test_create_failure_returns_without_touching_anything(self, mod_win, monkeypatch):
        """CreateJobObjectW 失败 → 无 Assign、无登记、无关闭（job 从未存在）。"""
        k32 = _FakeKernel32Pin(create=0)
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))
        assert mod_win._JOB_HANDLES == []
        assert not any(c[0] == "close" for c in k32.calls)

    def test_pin_outer_exception_caught(self, mod_win, monkeypatch):
        """ctypes 注入残缺（缺 WinDLL）→ 外层异常兜底告警，不向 spawn 抛。"""
        broken = types.ModuleType("ctypes_broken_jobpin")
        monkeypatch.setitem(sys.modules, "ctypes", broken)
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))  # 不抛即可

    def test_set_info_failure_closes_halfbuilt_job(self, mod_win, monkeypatch):
        """SetInfo 失败 → 关半成品 Job（无成员关闭无副作用）+ 不 Assign + 不登记。"""
        k32 = _FakeKernel32Pin(set_info=0)
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))
        assert not any(c[0] == "assign" for c in k32.calls), "SetInfo 失败不得 Assign"
        assert mod_win._JOB_HANDLES == [], "失败路径不得登记句柄"
        assert ("close", 4242) in k32.calls, "半成品 Job 必须关闭（不泄内核对象）"

    def test_assign_failure_closes_job(self, mod_win, monkeypatch):
        """Assign 失败 → 关 Job + 不登记。"""
        k32 = _FakeKernel32Pin(assign=0)
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))
        assert mod_win._JOB_HANDLES == []
        assert ("close", 4242) in k32.calls

    def test_success_appends_without_closing(self, mod_win, monkeypatch):
        """成功路径 → 登记、不关（句柄活到宿主死亡才触发 kill-on-close）。"""
        k32 = _FakeKernel32Pin()
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))
        assert 4242 in mod_win._JOB_HANDLES, "句柄必须登记防提前关闭"
        assert not any(c[0] == "close" for c in k32.calls), "成功路径不得关句柄"
        sets = [c for c in k32.calls if c[0] == "set"]
        assert sets, "SetInformationJobObject 必须被调用"
        assert sets[0][2] & 0x2000, "LimitFlags 必须带 KILL_ON_JOB_CLOSE"

    def test_suspended_resume_called_even_on_failure(self, mod_win, monkeypatch):
        """finally 恢复语义：钉死失败也必须恢复主线程（进程不许被吊死在挂起态）。"""
        k32 = _FakeKernel32Pin(set_info=0)
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        resumed: list[object] = []
        monkeypatch.setattr(win_job, "_resume_suspended_process", resumed.append)
        proc = SimpleNamespace(_handle=777, pid=123)
        mod_win._pin_process_to_job(proc, suspended=True)
        assert resumed == [proc], "失败路径也必须恢复挂起进程（finally）"

    def test_not_suspended_skips_resume(self, mod_win, monkeypatch):
        """非挂起起停不调用恢复（普通 spawn 语义不变）。"""
        k32 = _FakeKernel32Pin()
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        resumed: list[object] = []
        monkeypatch.setattr(win_job, "_resume_suspended_process", resumed.append)
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))
        assert resumed == []


@win32_only
class TestCloseJobHandle:
    def test_close_failure_warns_not_raises(self, mod_win, monkeypatch):
        """半成品句柄关闭失败 → 告警不抛（关闭是清扫位，不阻断 spawn）。"""
        k32 = _FakeKernel32Pin(set_info=0)

        def _boom(handle):
            k32.calls.append(("close", handle))
            raise OSError("simulated CloseHandle failure")

        k32.CloseHandle = _boom  # type: ignore[method-assign]
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._pin_process_to_job(SimpleNamespace(_handle=777, pid=123))  # 不抛即可
        assert ("close", 4242) in k32.calls, "失败路径尝试了关闭"


class _FakeKernel32Resume:
    """可编程 fake kernel32（恢复主线程分支驱动）。方法名镜像 Win32 API（N802）。"""

    def __init__(
        self,
        *,
        snapshot=-1,
        first=0,
        next_call=0,
        owner_pid=0,
        tid=0,
        open_thread=0,
        resume=0,
    ):
        self.calls: list[tuple] = []
        self._snapshot = snapshot
        self._first = first
        self._next_call = next_call
        self._owner_pid = owner_pid
        self._tid = tid
        self._open_thread = open_thread
        self._resume = resume

    def CreateToolhelp32Snapshot(self, flags, pid):  # noqa: N802
        self.calls.append(("snapshot",))
        return self._snapshot

    def Thread32First(self, snap, ref):  # noqa: N802
        entry = getattr(ref, "_obj", ref)
        entry.th32OwnerProcessID = self._owner_pid
        entry.th32ThreadID = self._tid
        self.calls.append(("first", entry.th32OwnerProcessID))
        return self._first

    def Thread32Next(self, snap, ref):  # noqa: N802
        self.calls.append(("next",))
        return self._next_call

    def OpenThread(self, access, inherit, tid):  # noqa: N802
        self.calls.append(("open", tid))
        return self._open_thread

    def ResumeThread(self, thread):  # noqa: N802
        self.calls.append(("resume", thread))
        return self._resume

    def CloseHandle(self, handle):  # noqa: N802
        self.calls.append(("close", handle))
        return 1


@win32_only
class TestResumeSuspendedProcess:
    def _run(self, mod_win, monkeypatch, **kwargs):
        k32 = _FakeKernel32Resume(**kwargs)
        monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(k32))
        mod_win._resume_suspended_process(SimpleNamespace(pid=500))
        return k32

    def test_broken_ctypes_caught_by_outer_guard(self, mod_win, monkeypatch):
        """ctypes 注入残缺（缺 WinDLL）→ 外层异常兜底告警，不向调用方抛。"""
        broken = types.ModuleType("ctypes_broken_resume")
        monkeypatch.setitem(sys.modules, "ctypes", broken)
        mod_win._resume_suspended_process(SimpleNamespace(pid=500))  # 不抛即可

    def test_snapshot_failure_returns_without_open(self, mod_win, monkeypatch):
        k32 = self._run(mod_win, monkeypatch, snapshot=-1)
        assert not any(c[0] == "open" for c in k32.calls)

    def test_thread32first_failure_finds_no_tid(self, mod_win, monkeypatch):
        k32 = self._run(mod_win, monkeypatch, snapshot=7, first=0)
        assert ("close", 7) in k32.calls, "快照句柄必须关闭"
        assert not any(c[0] == "open" for c in k32.calls)

    def test_owner_mismatch_exhausts_snapshot(self, mod_win, monkeypatch):
        """快照遍历完无本进程线程 → 无 OpenThread（不误恢复他者线程）。"""
        k32 = self._run(
            mod_win,
            monkeypatch,
            snapshot=7,
            first=1,
            next_call=0,
            owner_pid=999,
            tid=1,
        )
        assert ("next",) in k32.calls
        assert not any(c[0] == "open" for c in k32.calls)

    def test_open_thread_failure_no_resume(self, mod_win, monkeypatch):
        k32 = self._run(
            mod_win,
            monkeypatch,
            snapshot=7,
            first=1,
            owner_pid=500,
            tid=42,
            open_thread=0,
        )
        assert ("open", 42) in k32.calls
        assert not any(c[0] == "resume" for c in k32.calls)

    def test_resume_success_closes_thread_handle(self, mod_win, monkeypatch):
        k32 = self._run(
            mod_win,
            monkeypatch,
            snapshot=7,
            first=1,
            owner_pid=500,
            tid=42,
            open_thread=9,
            resume=0,
        )
        assert ("resume", 9) in k32.calls
        assert ("close", 9) in k32.calls, "线程句柄必须关闭"

    def test_resume_error_still_closes_handle(self, mod_win, monkeypatch):
        """ResumeThread 失败（返回 -1）也必须关线程句柄，不静默吞。"""
        k32 = self._run(
            mod_win,
            monkeypatch,
            snapshot=7,
            first=1,
            owner_pid=500,
            tid=42,
            open_thread=9,
            resume=-1,
        )
        assert ("resume", 9) in k32.calls
        assert ("close", 9) in k32.calls
