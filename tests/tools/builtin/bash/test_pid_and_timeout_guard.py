# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""bash 工具入口收口测试：handle 校验 + log(block=true) 等待上限收口。

- handle 声明 string 且自带校验和：畸形/篡改 handle（注入串/路径穿越串/
  校验和不符）在入口即拒（INVALID_HANDLE），不进任何查询/路径拼接；
  handle 缺失 MISSING_HANDLE；
- log(block=true) 的等待上限与 bash 同规收口（min(默认, MAX_TIMEOUT)）：
  上限恒 ≤ MAX_TIMEOUT（以 MAX_TIMEOUT 类级覆盖注入小上限验证钳制公约，
  不需要真实等待），显式小上限等待按上限生效。

2026-11-07 四工具重构（ADR T2）：pid 整型收口退役（handle 校验和取代），
continue timeout 参数退役（等待上限内化为 log block=true 缺省节拍）。
"""

from __future__ import annotations

import shutil
import sys
import time

import pytest
from tool import BashTool, encode_handle

pytestmark = pytest.mark.unit


@pytest.fixture
def tool(tmp_path):
    t = BashTool()
    from agentos_plugin_sdk.process_runtime import ProcessManager

    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


class TestHandleEntryValidation:
    @pytest.mark.asyncio
    async def test_tampered_handle_rejected(self, tool) -> None:
        """篡改/畸形 handle → 参数错误（INVALID_HANDLE），不做任何查询/路径拼接。"""
        good = encode_handle(300, None)
        for bad in (
            good[:-2] + "zz",  # 校验和篡改
            "xx." + good.split(".", 1)[1],  # 未知 scheme
            "../../x",
            "300; rm -rf /",
            12345,  # 非字符串
        ):
            result = await tool.read_log({"handle": bad, "block": False})
            assert result.success is False, f"{bad!r} 应被拒绝"
            assert result.error_code == "INVALID_HANDLE", f"{bad!r} → INVALID_HANDLE"

    @pytest.mark.asyncio
    async def test_valid_handle_passes_entry(self, tool, tmp_path) -> None:
        """合法 handle 通过入口后正常走磁盘日志路径。"""
        log_dir = tool.process_manager.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "bash_300.log").write_text(
            "# Bash Command Log\n# Command: echo hi\n# PID: 300\n"
            "# Started: 2026-01-01T00:00:00\n# Platform: Linux\n# ===\n\nout\n",
            encoding="utf-8",
        )
        result = await tool.read_log({"handle": encode_handle(300, None), "block": False})
        assert result.success is True
        assert result.output["handle"] == encode_handle(300, None)

    @pytest.mark.asyncio
    async def test_missing_handle_still_reports_missing(self, tool) -> None:
        """空 handle 保持 MISSING_HANDLE 契约。"""
        result = await tool.read_log({"handle": "", "block": True})
        assert result.success is False
        assert result.error_code == "MISSING_HANDLE"


_NEEDS_SHELL = pytest.mark.skipif(
    sys.platform == "win32" and not (shutil.which("bash") or shutil.which("wsl")),
    reason="Windows 下需要 bash/wsl（ProcessManager 的 shell 选择）",
)


def _sleep_cmd() -> str:
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -c "import time; time.sleep(30)"'


@_NEEDS_SHELL
class TestBlockWaitCap:
    @pytest.mark.timeout(60)
    @pytest.mark.asyncio
    async def test_wait_cap_bounded_by_max_timeout(self, tmp_path) -> None:
        """等待上限恒 ≤ MAX_TIMEOUT（覆盖为 1s）→ 有限时间内返回 running。"""
        from agentos_plugin_sdk.process_runtime import ProcessManager

        class _ShortCapTool(BashTool):
            MAX_TIMEOUT = 1

        t = _ShortCapTool()
        t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
        pid, _ = await t.process_manager.start_process(_sleep_cmd(), working_dir=str(tmp_path))
        try:
            started = time.monotonic()
            result = await t.read_log({"handle": encode_handle(pid, None), "block": True})
            wall = time.monotonic() - started
            assert result.success is True
            assert result.output["status"] == "running"
            assert wall < 30, "钳制后必须有限时间返回（未钳制将按缺省 30s 等待）"
            assert wall >= 0.9, "返回前应等待满一个钳后上限周期（≥MAX_TIMEOUT）"
        finally:
            try:
                await t.process_manager.terminate_process(pid, force=True)
            except Exception:  # noqa: BLE001 — 兜底清理失败不影响判定
                pass
