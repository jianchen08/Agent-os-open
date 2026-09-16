# @feature: FP-0.2.二 continue 退出码保真 | @ci: python-coverage
"""continue 等待期跨过退出清理时点的退出码保真回归。

契约：continue 无论落在哪个时序窗口——进程仍在跑（等待循环）、恰好跨过
「置终态→清内存」时点（轮询下一拍记录已 GONE）、还是早已结束（入口走磁盘
降级）——返回的 exit_code 都必须等于真实退出码，禁止把记录丢失兜成 0。

打桩边界：无——子进程全部真实拉起（python -c 定时退出进程），
不 mock 进程管理层。workspace 全程一致传入（主仓 fail-closed 锚定契约 +
owner 身份派生自 workspace，continue 必须同身份才能过 pid 级校验）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from tool import BashTool

pytestmark = pytest.mark.unit


def _py_cmd(code: str) -> str:
    return f'"{Path(sys.executable).as_posix()}" -c "{code}"'


async def test_continue_inflight_returns_real_exit_code(tmp_path: Path) -> None:
    """continue 在进程运行中发起，等待到退出后必须拿到真实非零退出码。"""
    tool = BashTool()
    ws = str(tmp_path)
    start = time.monotonic()
    r = await tool.execute(
        {
            "action": "execute",
            "command": _py_cmd("import time; time.sleep(2); import sys; sys.exit(7)"),
            "timeout": 1,
            "workspace": ws,
        }
    )
    assert r.success and r.output["status"] == "running"
    pid = r.output["pid"]

    r2 = await tool.execute({"action": "continue", "pid": pid, "timeout": 15, "workspace": ws})
    assert r2.success, r2.error
    # 等待循环至少跨过一次进程运行期（0.5s 轮询粒度），退出码必须保真
    assert r2.output["exit_code"] == 7, f"退出码失真: {r2.output}"
    assert time.monotonic() - start >= 2  # 确实等到了进程退出，而非提前假完成


async def test_continue_inflight_zero_exit_is_success(tmp_path: Path) -> None:
    """符号量级相反组：真实退出码 0 时 continue 返回 completed/0，不误报失败。"""
    tool = BashTool()
    ws = str(tmp_path)
    r = await tool.execute(
        {
            "action": "execute",
            "command": _py_cmd("import time; time.sleep(2); print('done-marker')"),
            "timeout": 1,
            "workspace": ws,
        }
    )
    assert r.success and r.output["status"] == "running"
    pid = r.output["pid"]

    r2 = await tool.execute({"action": "continue", "pid": pid, "timeout": 15, "workspace": ws})
    assert r2.success, r2.error
    assert r2.output["exit_code"] == 0
    assert "done-marker" in str(r2.output.get("output", ""))


async def test_continue_after_cleanup_reads_disk_exit_code(tmp_path: Path) -> None:
    """入口时记录已被清理：磁盘日志尾部 exit code 必须被解析回读。"""
    tool = BashTool()
    ws = str(tmp_path)
    r = await tool.execute(
        {
            "action": "execute",
            "command": _py_cmd("import time; time.sleep(2); import sys; sys.exit(5)"),
            "timeout": 1,
            "workspace": ws,
        }
    )
    assert r.success and r.output["status"] == "running"
    pid = r.output["pid"]

    # 等进程自然结束并被 _on_output_task_done 清理（超过生存期 + 轮询粒度）
    await asyncio.sleep(3.5)
    assert tool.process_manager.get_process_info(pid) is None

    r2 = await tool.execute({"action": "continue", "pid": pid, "timeout": 5, "workspace": ws})
    assert r2.success, r2.error
    assert r2.output["exit_code"] == 5
