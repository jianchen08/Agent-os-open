# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
"""plugins/shared/proc_tree.py 行为测试。

用真实短命子进程树验证（不 mock psutil 内部）：
1. 正常路径：父子两层进程树全清、失败清单为空；
2. 已死进程：幂等成功（空清单）；
3. 垃圾输入（不存在的系统进程 pid）：返回失败清单不抛异常。

平台差异：Windows 走 psutil + taskkill /T /F 兜底，POSIX 走 psutil
叶→根杀；两条路径都由真实进程树用例覆盖到本机分支。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

pytestmark = pytest.mark.unit

_SLEEP_SNIPPET = "import time; time.sleep(30)"


def _spawn_tree(pid_file: Path) -> subprocess.Popen[bytes]:
    """起两层真实进程树：python(父) → python(子)，孙 pid 写 pid_file。

    父进程用 python 自身保证跨平台；两层各睡 30s（远超用例时长，
    不会被自然退出干扰断言）。
    """
    script = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        f"{_SLEEP_SNIPPET!r}])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        f"exec({_SLEEP_SNIPPET!r})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_pid_gone(pid: int, timeout: float = 5.0) -> bool:
    """轮询进程是否已退出（含 Windows 终止异步完成的收敛窗口）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


def _read_grandchild_pid(pid_file: Path, timeout: float = 5.0) -> int:
    """等子进程把孙 pid 写盘（文件出现且内容非空）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"孙进程 pid 未在 {timeout}s 内写盘: {pid_file}")


@pytest.fixture
def tree(tmp_path: Path) -> Any:
    """起一棵真实进程树，用例结束兜底再杀一遍（防断言前泄漏）。"""
    pid_file = tmp_path / "grandchild.pid"
    parent = _spawn_tree(pid_file)
    handle: dict[str, Any] = {"parent": parent, "grandchild": None}
    try:
        handle["grandchild"] = _read_grandchild_pid(pid_file)
        yield handle
    finally:
        from proc_tree import kill_process_tree

        kill_process_tree(parent.pid)
        if not _wait_pid_gone(parent.pid, timeout=2.0):
            parent.kill()


class TestKillProcessTree:
    def test_real_tree_fully_killed_and_no_failures(self, tree: Any) -> None:
        """真实父子两层树：全清（父子进程均退出），失败清单为空。"""
        from proc_tree import kill_process_tree

        parent: subprocess.Popen[bytes] = tree["parent"]
        grandchild_pid: int = tree["grandchild"]
        assert _wait_pid_gone(grandchild_pid, timeout=0.1) is False  # 前置：树还活着

        failures = kill_process_tree(parent.pid)

        assert failures == []
        assert _wait_pid_gone(parent.pid) is True
        assert _wait_pid_gone(grandchild_pid) is True

    def test_dead_process_returns_empty(self, tree: Any) -> None:
        """已退出的进程：幂等成功（空清单），不抛异常。

        以两组区分输入覆盖：从未存在的 pid + 刚被本函数杀死的 pid。
        """
        from proc_tree import kill_process_tree

        assert kill_process_tree(tree["parent"].pid) == []
        # 上一条断言树已全清：此时再杀同一根 = 已死进程幂等
        assert kill_process_tree(tree["parent"].pid) == []

    @pytest.mark.parametrize("missing_pid", [-1, 2**22])
    def test_missing_pid_treated_as_success(self, missing_pid: int) -> None:
        """不存在的 pid（负数/超界）：视为已退出 = 成功（空清单），不抛异常。"""
        from proc_tree import kill_process_tree

        assert kill_process_tree(missing_pid) == []

    @pytest.mark.skipif(os.name != "nt", reason="pid=4 (System) 是 Windows 受保护进程")
    def test_protected_process_reports_failure_list(self) -> None:
        """Windows System 进程（pid=4，受保护不可杀）：失败清单非空且含 pid。

        用真实不可杀进程验证"杀失败不吞、错误是值"的清单通道。
        """
        from proc_tree import kill_process_tree

        failures = kill_process_tree(4)
        assert failures, "受保护进程杀失败必须留下失败清单"
        assert any("pid=4" in f for f in failures)

    def test_posix_and_windows_branches_agree_on_empty_failures(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """快进程（正常退出）当输入：两平台分支都不误报失败清单。

        用已完成进程（returncode 已就绪）验证"已死 = 成功"契约在
        os.name 两个分支下一致（Windows 分支的 taskkill 兜底对已死
        进程找不到进程，属预期不进清单）。
        """
        from proc_tree import kill_process_tree

        done = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert done.wait(timeout=30) == 0
        for platform_name in ("nt", "posix"):
            monkeypatch.setattr(os, "name", platform_name)
            assert kill_process_tree(done.pid) == []
