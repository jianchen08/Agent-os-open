# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""宿主 watchdog 自杀前的子进程清理测试。

契约：心跳停滞触发自杀时，先有界（≤2s）尽力杀掉成员插件登记的活跃子进程
（os._exit 跳过 atexit，不杀则 bash/docker exec 子进程成无主孤儿），再退出。

- 行为级：真实 ProcessManager + 真实 sleep 子进程，watchdog 以短阈值真实
  触发（可调参数注入，不零延迟 mock）后子进程被终止；
- 单元级：预算内逐个杀、杀失败吞并（best-effort 合法点）、无登记表空转。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import types as types_mod
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOST_DIR = _REPO_ROOT / "plugins" / "shared" / "_host"
_BASH_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "bash"

for _d in (str(_HOST_DIR), str(_BASH_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import host  # noqa: E402
from process_manager import ProcessManager  # noqa: E402

pytestmark = pytest.mark.unit

# Windows 下 ProcessManager 的本地 shell 选择依赖 bash/wsl（Git Bash / WSL）
_NEEDS_SHELL = pytest.mark.skipif(
    sys.platform == "win32" and not (shutil.which("bash") or shutil.which("wsl")),
    reason="Windows 下需要 bash/wsl（ProcessManager 的 shell 选择）",
)


def _sleep_child_cmd() -> str:
    """跨 shell 的长睡命令（bash 与 cmd 均可解析）。"""
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -c "import time; time.sleep(30)"'


def _register_fake_bash_member(pm: Any) -> tuple[str, Any]:
    """把持有 pm 的合成成员模块挂到 sys.modules（清理函数的扫描对象）。"""
    member_name = host._MEMBER_MODULE_PREFIX + "fake_bash"
    member = types_mod.ModuleType(member_name)
    member._tool = SimpleNamespace(process_manager=pm)
    saved = sys.modules.get(member_name)
    sys.modules[member_name] = member
    return member_name, saved


@_NEEDS_SHELL
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_watchdog_kills_spawned_child_before_exit(tmp_path: Path) -> None:
    """watchdog 真实触发时，成员登记的 sleep 子进程被终止（非孤儿）。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    member_name: str | None = None
    saved: Any = None
    try:
        pid, _log_file = await pm.start_process(_sleep_child_cmd(), working_dir=str(tmp_path))
        proc = pm.active_processes[pid].process
        assert proc.returncode is None, "子进程启动后应存活"

        member_name, saved = _register_fake_bash_member(pm)

        # 真实时钟 + 短阈值注入（不零延迟 mock）：心跳已停滞 10s，阈值 0.5s
        heartbeat = host._Heartbeat()
        heartbeat.last_beat = time.monotonic() - 10.0
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, 0.5, check_interval_secs=0.02, exit_fn=exits.append
        )
        watchdog.start()
        deadline = time.monotonic() + 5.0
        while not exits and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        watchdog.stop()
        assert exits == [1], "watchdog 应触发自杀"

        # 清理发生在 exit_fn 之前（exits 非空即清理已完成），子进程被杀
        for _ in range(100):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        assert proc.returncode is not None, "自杀前 sleep 子进程应已被清理终止"
    finally:
        if member_name is not None:
            if saved is not None:
                sys.modules[member_name] = saved
            else:
                sys.modules.pop(member_name, None)
        try:
            await pm.shutdown_all()
        except Exception:  # noqa: BLE001 — 兜底清理失败不影响判定
            pass


class TestKillTrackedChildrenUnits:
    def test_no_member_modules_noop(self) -> None:
        """无合宿成员模块 → 空转返回 0（不抛）。"""
        assert host._kill_tracked_children(budget_secs=0.1) == 0

    def test_kills_each_registered_process_and_counts(self) -> None:
        """活跃表内每个进程各杀一次并计数；预算内完成。"""
        killed_pids: list[int] = []

        class _Proc:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def kill(self) -> None:
                killed_pids.append(self.pid)

        pm = SimpleNamespace(
            active_processes={1: SimpleNamespace(process=_Proc(1), backend=None),
                             2: SimpleNamespace(process=_Proc(2), backend=None)}
        )
        member_name = host._MEMBER_MODULE_PREFIX + "unit_probe"
        member = types_mod.ModuleType(member_name)
        member._tool = SimpleNamespace(process_manager=pm)
        sys.modules[member_name] = member
        try:
            assert host._kill_tracked_children(budget_secs=1.0) == 2
        finally:
            sys.modules.pop(member_name, None)
        assert sorted(killed_pids) == [1, 2]

    def test_kill_failure_swallowed_not_raised(self) -> None:
        """单个进程杀失败 → 吞并继续（best-effort 合法点），不阻断自杀。"""

        class _BoomProc:
            pid = 7

            def kill(self) -> None:
                raise RuntimeError("already dead")

        pm = SimpleNamespace(
            active_processes={7: SimpleNamespace(process=_BoomProc(), backend=None),
                              8: SimpleNamespace(process=None, backend=None)}
        )
        member_name = host._MEMBER_MODULE_PREFIX + "unit_boom"
        member = types_mod.ModuleType(member_name)
        member._tool = SimpleNamespace(process_manager=pm)
        sys.modules[member_name] = member
        try:
            assert host._kill_tracked_children(budget_secs=1.0) == 0
        finally:
            sys.modules.pop(member_name, None)

    def test_budget_deadline_stops_killing(self) -> None:
        """预算耗尽立即放弃：剩余进程不再尝试（有界语义）。"""

        class _SlowProc:
            pid = 9

            def kill(self) -> None:
                time.sleep(0.2)

        pm = SimpleNamespace(
            active_processes={
                i: SimpleNamespace(process=_SlowProc(), backend=None) for i in range(10)
            }
        )
        member_name = host._MEMBER_MODULE_PREFIX + "unit_slow"
        member = types_mod.ModuleType(member_name)
        member._tool = SimpleNamespace(process_manager=pm)
        sys.modules[member_name] = member
        try:
            started = time.monotonic()
            killed = host._kill_tracked_children(budget_secs=0.1)
            elapsed = time.monotonic() - started
        finally:
            sys.modules.pop(member_name, None)
        assert killed < 10, "预算内未能杀完全部 → 提前放弃"
        assert elapsed < 1.0, "总耗时受预算约束（≤2s 语义的单测等价面）"
