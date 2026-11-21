# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""宿主 watchdog 自杀前的停滞取证栈落盘测试。

契约：心跳停滞超阈值后、清理子进程/自杀之前，把全部线程（含事件循环线程）
的 Python 栈写入 dump 文件——用于定位事件循环被哪一帧阻塞。落盘 best-effort：
失败只告警，自杀语义（cleanup_fn → exit_fn(1)）不变。

- 目录解析：AGENTOS_STALL_DUMP_DIR > %APPDATA%\\agentos\\logs > ~/.agentos/logs；
- 行为级：fake clock 触发停滞 → dump 文件生成（栈头 + 当前线程栈）→ 清理 → 自杀；
- 真实阻塞：事件循环线程被同步调用冻住时，dump 里能看到该阻塞帧（取证目的本体）；
- 失败面：dump 目录不可创建 → 仍 exit_fn(1) 且告警。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import host
import pytest

pytestmark = pytest.mark.unit

# 文件名契约：host_stall_<UTC时间戳>_<pid>.log
_DUMP_NAME_RE = re.compile(r"^host_stall_\d{8}T\d{6}Z_\d+\.log$")


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """轮询等待（真实时间仅数十毫秒；停滞量由 fake clock 推进，不真 sleep）。"""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)


def _blocking_member_call(seconds: float) -> None:
    """模拟成员工具的同步阻塞调用（事件循环线程被冻住的现场帧）。"""
    time.sleep(seconds)


# ── 落盘目录解析 ─────────────────────────────────────────


class TestStallDumpDir:
    """目录优先级与平台回退（纯函数，注入 env dict，无 IO）。"""

    def test_env_override_wins(self, tmp_path: Path) -> None:
        env = {"AGENTOS_STALL_DUMP_DIR": str(tmp_path), "APPDATA": "C:/ignored"}
        assert host._stall_dump_dir(env) == tmp_path

    def test_reads_process_environ_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_STALL_DUMP_DIR", str(tmp_path))
        assert host._stall_dump_dir() == tmp_path

    def test_blank_env_falls_through_to_appdata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(host.sys, "platform", "win32")
        env = {"AGENTOS_STALL_DUMP_DIR": "   ", "APPDATA": str(tmp_path)}
        assert host._stall_dump_dir(env) == tmp_path / "agentos" / "logs"

    def test_windows_appdata_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(host.sys, "platform", "win32")
        assert host._stall_dump_dir({"APPDATA": str(tmp_path)}) == (
            tmp_path / "agentos" / "logs"
        )

    def test_windows_without_appdata_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host.sys, "platform", "win32")
        assert host._stall_dump_dir({}) == Path.home() / ".agentos" / "logs"

    def test_posix_falls_back_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(host.sys, "platform", "linux")
        assert host._stall_dump_dir({}) == Path.home() / ".agentos" / "logs"


# ── 停滞触发的取证落盘 ───────────────────────────────────


class TestStallDumpOnSuicide:
    """停滞超阈值：dump 先落盘，再清理子进程、自杀（顺序与内容可观察）。"""

    def test_dump_written_before_cleanup_and_exit(self, tmp_path: Path) -> None:
        now = {"t": 1000.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        dump_seen_at_cleanup: list[bool] = []

        def cleanup() -> int:
            dump_seen_at_cleanup.append(any(tmp_path.glob("host_stall_*.log")))
            return 0

        watchdog = host._LoopWatchdog(
            heartbeat,
            30.0,
            check_interval_secs=0.005,
            clock=clock,
            exit_fn=exits.append,
            cleanup_fn=cleanup,
            dump_dir=tmp_path,
            host_name="light:1",
        )
        watchdog.start()
        now["t"] += 31.5  # 停滞严格超过阈值
        _wait_until(lambda: bool(exits))
        watchdog.stop()

        assert exits == [1], "取证不得改变自杀语义"
        assert dump_seen_at_cleanup == [True], "dump 必须先于清理/自杀落盘"

        dumps = sorted(tmp_path.glob("host_stall_*.log"))
        assert len(dumps) == 1
        assert _DUMP_NAME_RE.match(dumps[0].name)
        text = dumps[0].read_text(encoding="utf-8")
        # 栈头：停滞量/阈值/pid/宿主名/UTC 时间/心跳最后打点
        assert "stalled_secs=31.500" in text
        assert "stall_threshold_secs=30.000" in text
        assert f"pid={os.getpid()}" in text
        assert "host=light:1" in text
        assert re.search(r"time_utc=\d{8}T\d{6}Z", text)
        assert f"heartbeat_last_beat={heartbeat.last_beat:.6f}" in text
        # 栈体：faulthandler 全线程 dump 含当前测试线程栈
        assert "test_watchdog_stall_dump.py" in text
        assert "Thread 0x" in text

    @pytest.mark.timeout(30)
    async def test_dump_captures_blocked_event_loop_frame(self, tmp_path: Path) -> None:
        """事件循环线程被同步阻塞时，dump 里能看到阻塞帧（取证目的本体）。"""
        heartbeat = host._Heartbeat()
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat,
            0.3,
            check_interval_secs=0.02,
            exit_fn=exits.append,
            dump_dir=tmp_path,
        )
        watchdog.start()
        try:
            await asyncio.sleep(0.05)  # watchdog 进入检查循环；心跳不再推进
            _blocking_member_call(0.8)  # 同步冻住事件循环线程
        finally:
            watchdog.stop()

        assert exits == [1], "停滞期间 watchdog 应触发自杀（此处以记录器承接）"
        dumps = sorted(tmp_path.glob("host_stall_*.log"))
        assert len(dumps) == 1
        text = dumps[0].read_text(encoding="utf-8")
        assert "_blocking_member_call" in text, "dump 必须含被阻塞的事件循环帧"
        assert "host=unknown" in text, "未注入宿主名时如实标注 unknown"

    def test_creates_missing_dump_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "logs"
        watchdog = host._LoopWatchdog(
            host._Heartbeat(clock=lambda: 0.0), 30.0, dump_dir=target
        )
        path = watchdog._dump_stall_stacks(42.0)
        assert path is not None
        assert target.is_dir()
        assert path.parent == target
        assert path.name.endswith(f"_{os.getpid()}.log")
        text = path.read_text(encoding="utf-8")
        assert "stalled_secs=42.000" in text
        assert f"heartbeat_last_beat={0.0:.6f}" in text

    def test_env_dir_used_when_not_injected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """未注入 dump_dir 时走 AGENTOS_STALL_DUMP_DIR（生产默认路径面）。"""
        monkeypatch.setenv("AGENTOS_STALL_DUMP_DIR", str(tmp_path))
        watchdog = host._LoopWatchdog(host._Heartbeat(clock=lambda: 5.0), 30.0)
        path = watchdog._dump_stall_stacks(31.0)
        assert path is not None
        assert path.parent == tmp_path
        assert "stalled_secs=31.000" in path.read_text(encoding="utf-8")

    def test_dump_failure_still_exits_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """落盘失败（目标路径是普通文件）→ 告警 + 仍 exit_fn(1)，不阻断自杀。"""
        caplog.set_level(logging.WARNING)
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        now = {"t": 0.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat,
            30.0,
            check_interval_secs=0.005,
            clock=clock,
            exit_fn=exits.append,
            dump_dir=blocker,
        )
        watchdog.start()
        now["t"] += 31.0
        _wait_until(lambda: bool(exits))
        watchdog.stop()

        assert exits == [1], "dump 失败不得阻断自杀"
        assert any("停滞取证栈落盘失败" in record.getMessage() for record in caplog.records)
