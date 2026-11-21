# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""kill_host_tree 宿主级杀树直测（真实进程树，非假后端）。

2026-11-17 WSL 挂死事故补面：``ProcessBackend.kill_host_tree`` 与
``_kill_host_tree_os_fallback`` 合计 ~61 行此前零直测（仅经假后端记录调用
面）。本文件在本机 spawn 真两级进程树（python 父进程再起 python sleep
子进程），调杀树后用 psutil 轮询断言**整树**进程消亡。

覆盖面（如实声明）：本车道跑在 Windows——psutil 主路径（递归枚举后代杀
全树）与 OS 兜底的 Windows ``taskkill /T /F`` 分支为真实覆盖；POSIX 分支
（psutil 主路径代码同形；``os.kill`` 兜底仅杀父不杀树）无法在 Windows
覆盖，不在本文件伪装。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.process_runtime.types import ProcessBackend

pytestmark = pytest.mark.unit

psutil: Any = pytest.importorskip("psutil")


class _MinimalBackend(ProcessBackend):
    """最小可实例化后端：被测方法 kill_host_tree 在基类有真实实现。"""

    async def kill(self, unit: Any, force: bool = True) -> None:
        return None

    async def sample_memory(self) -> float | None:
        return None


def _spawn_real_tree(tmp_path: Path) -> tuple[subprocess.Popen[Any], int]:
    """spawn 真两级树：父 python 起 sleep 子进程并回传子 pid（stdout 首行）。"""
    parent_script = tmp_path / "tree_parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(parent_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    line = proc.stdout.readline().decode("ascii").strip()  # type: ignore[union-attr]
    assert line.isdigit(), f"父进程未回传子 pid（首行={line!r}）——树未建立"
    return proc, int(line)


def _wait_pid(pid: int, want_alive: bool, deadline_s: float = 15.0) -> None:
    """轮询 pid 存活性到期望态（到达期望态即返回；want_alive=False 即等待消亡）。"""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if psutil.pid_exists(pid) is want_alive:
            return
        time.sleep(0.2)
    state = "存活" if want_alive else "消亡"
    raise AssertionError(f"pid {pid} 在 {deadline_s}s 后仍未{state}")


async def test_psutil_path_kills_whole_real_tree(tmp_path: Path) -> None:
    """psutil 主路径：父与 sleep 子进程整树消亡（真实进程，非桩）。"""
    proc, child_pid = _spawn_real_tree(tmp_path)
    _wait_pid(proc.pid, want_alive=True)
    _wait_pid(child_pid, want_alive=True)

    backend = _MinimalBackend()
    await asyncio.wait_for(backend.kill_host_tree(proc.pid), timeout=30)

    assert proc.wait(timeout=15) != 0  # 120s sleep 中被强杀，退出码非 0
    _wait_pid(proc.pid, want_alive=False)
    _wait_pid(child_pid, want_alive=False)  # 整树：后代一并消亡


@pytest.mark.skipif(sys.platform != "win32", reason="OS 兜底仅 Windows taskkill 分支可真覆盖")
async def test_os_fallback_taskkill_kills_whole_real_tree_windows(tmp_path: Path) -> None:
    """OS 兜底（psutil 缺失路径）Windows 分支：taskkill /T /F 同样杀整树。"""
    proc, child_pid = _spawn_real_tree(tmp_path)
    _wait_pid(proc.pid, want_alive=True)
    _wait_pid(child_pid, want_alive=True)

    await asyncio.wait_for(
        ProcessBackend._kill_host_tree_os_fallback(proc.pid), timeout=30  # noqa: SLF001
    )

    assert proc.wait(timeout=15) != 0
    _wait_pid(child_pid, want_alive=False)  # /T 递归：后代一并消亡
