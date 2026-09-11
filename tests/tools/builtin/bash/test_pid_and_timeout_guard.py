# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""bash 工具入口收口测试：pid 整型校验 + continue timeout 上限收口。

- pid 声明 integer 但 SDK 无 schema 强制：非整型 pid（注入串/路径穿越串）
  会拼进磁盘日志名 bash_<pid>.log，构成任意 .log 读取原语——入口整型化，
  非整型即参数错误（INVALID_PID）；整数字符串规范化透传；
- continue 的 timeout 与 execute 同规收口（min(input, MAX_TIMEOUT)）：
  巨额 timeout 被钳到上限（以 MAX_TIMEOUT 类级覆盖注入小上限验证钳制
  公约，不需要真实等待 290s），正常小值透传生效。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest
from tool import BashTool

pytestmark = pytest.mark.unit


@pytest.fixture
def tool(tmp_path):
    t = BashTool()
    from process_manager import ProcessManager

    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


class _ShortCapTool(BashTool):
    """MAX_TIMEOUT 类级覆盖为 1s：钳制公约 min(input, MAX_TIMEOUT) 的可观测面。"""

    MAX_TIMEOUT = 1


def _write_log_file(log_dir: Path, pid: int, command: str, content: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bash_{pid}.log"
    header = (
        f"# Bash Command Log\n# Command: {command}\n# PID: {pid}\n"
        f"# Started: 2026-01-01T00:00:00\n# Platform: Linux\n"
        f"# ==================================================\n\n"
    )
    log_file.write_text(header + content, encoding="utf-8")
    return log_file


class TestPidEntryValidation:
    @pytest.mark.parametrize(
        ("action", "bad_pid"),
        [
            ("continue", "1 OR x"),
            ("continue", "../../x"),
            ("read_log", "300; rm -rf /"),
            ("terminate", "..\\..\\windows"),
        ],
    )
    @pytest.mark.asyncio
    async def test_non_integer_pid_rejected(self, tool, action: str, bad_pid: str) -> None:
        """非整型 pid → 参数错误（INVALID_PID），不做任何查询/路径拼接。"""
        result = await tool.execute({"action": action, "pid": bad_pid})
        assert result.success is False
        assert result.error_code == "INVALID_PID"

    @pytest.mark.asyncio
    async def test_integer_string_pid_normalized(self, tool, tmp_path) -> None:
        """整数字符串 pid 规范化为 int 后正常走磁盘日志路径。"""
        _write_log_file(tmp_path / "logs", pid=300, command="echo hi", content="out\n")
        result = await tool.execute({"action": "read_log", "pid": "300"})
        assert result.success is True
        assert result.output["pid"] == 300

    @pytest.mark.asyncio
    async def test_missing_pid_still_reports_missing(self, tool) -> None:
        """空 pid 不被收口拦截，保持既有 MISSING_PID 契约。"""
        result = await tool.execute({"action": "continue", "pid": ""})
        assert result.success is False
        assert result.error_code == "MISSING_PID"


_NEEDS_SHELL = pytest.mark.skipif(
    sys.platform == "win32" and not (shutil.which("bash") or shutil.which("wsl")),
    reason="Windows 下需要 bash/wsl（ProcessManager 的 shell 选择）",
)


def _sleep_cmd() -> str:
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -c "import time; time.sleep(30)"'


@_NEEDS_SHELL
class TestContinueTimeoutCap:
    @pytest.mark.timeout(60)
    @pytest.mark.asyncio
    async def test_huge_timeout_clamped_to_max(self, tmp_path) -> None:
        """timeout=99999 被钳到 MAX_TIMEOUT（覆盖为 1s）→ 有限时间内返回 running。"""
        t = _ShortCapTool()
        from process_manager import ProcessManager

        t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
        pid, _ = await t.process_manager.start_process(_sleep_cmd(), working_dir=str(tmp_path))
        try:
            started = time.monotonic()
            result = await t.execute({"action": "continue", "pid": pid, "timeout": 99999})
            wall = time.monotonic() - started
            assert result.success is True
            assert result.output["status"] == "running"
            assert wall < 30, "钳制后必须有限时间返回（未钳制将等待巨额 timeout）"
            assert wall >= 0.9, "返回前应等待满一个钳后上限周期（≥MAX_TIMEOUT）"
        finally:
            try:
                await t.process_manager.terminate_process(pid, force=True)
            except Exception:  # noqa: BLE001 — 兜底清理失败不影响判定
                pass

    @pytest.mark.asyncio
    async def test_small_timeout_passes_through(self, tmp_path) -> None:
        """小于上限的 timeout 透传生效：约 0.5s 即返回，不被上限钳制拉长。"""
        t = _ShortCapTool()
        from process_manager import ProcessManager

        t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
        pid, _ = await t.process_manager.start_process(_sleep_cmd(), working_dir=str(tmp_path))
        try:
            started = time.monotonic()
            result = await t.execute({"action": "continue", "pid": pid, "timeout": 0.5})
            wall = time.monotonic() - started
            assert result.success is True
            assert result.output["status"] == "running"
            assert wall < 1.5, "小 timeout 不应被拉长到钳制上限"
        finally:
            try:
                await t.process_manager.terminate_process(pid, force=True)
            except Exception:  # noqa: BLE001 — 兜底清理失败不影响判定
                pass
