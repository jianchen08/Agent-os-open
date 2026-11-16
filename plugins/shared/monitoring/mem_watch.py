"""
细粒度内存观测器（mem_watch）

背景（2026-10-13 装机内存 92.2% 事件）：内核侧 mimalloc 快照 60s 一条只覆盖
内核进程自身；electron 渲染层与 python sidecar 无任何 per-process 时序数据，
进程死后无法验尸。本模块以 10s 级采样把 内核/electron/python(sidecar) 三族
进程的 RSS 与整机水位落 JSONL 运维日志，内核 RSS 单采样间隔内跳变超阈即时
告警——下一次内存事件可以在时发作时定位到进程与分钟。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger(__name__)

# 采样目标：按进程名子串匹配（大小写不敏感）。python 匹配覆盖全部 sidecar
# （含 dev 内核侧），灵汐 匹配 electron 主进程/渲染层/GPU 子进程。
TARGET_NAME_KEYS = ("agentos-kernel", "灵汐", "python")
# 单采样间隔 RSS 跳变告警阈（MB）：正常 10s 内单进程波动远低于此值。
JUMP_ALERT_DELTA_MB = 300.0
# 运维日志轮转阈：超过即改名 .1 保留一代，防止无界增长。
ROTATE_MAX_BYTES = 50 * 1024 * 1024
# 单条样本进程数上限（按 RSS 降序截断，控制单行体积）。
MAX_PROCS_PER_SAMPLE = 40
DEFAULT_INTERVAL_SECS = 10.0
MIN_INTERVAL_SECS = 2.0


def filter_target(name: str) -> bool:
    """进程名是否属于采样目标三族（agentos-kernel / 灵汐 / python）。"""
    low = (name or "").lower()
    return any(key.lower() in low for key in TARGET_NAME_KEYS)


def collect_sample() -> dict[str, Any]:
    """采样一次：整机水位 + 目标进程 RSS 列表（按 RSS 降序，截断至上限）。"""
    vm = psutil.virtual_memory()
    procs: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if not filter_target(name):
                continue
            mem = info.get("memory_info")
            rss_mb = round((mem.rss if mem else 0) / (1024 * 1024), 1)
            procs.append({"pid": info.get("pid"), "name": name, "rss_mb": rss_mb})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda item: item["rss_mb"], reverse=True)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sys_used_pct": round(vm.percent, 1),
        "sys_avail_mb": round(vm.available / (1024 * 1024), 1),
        "procs": procs[:MAX_PROCS_PER_SAMPLE],
    }


def detect_jump(prev_rss_mb: float | None, cur_rss_mb: float) -> bool:
    """内核 RSS 单采样间隔增量是否达到跳变告警阈。"""
    if prev_rss_mb is None:
        return False
    return (cur_rss_mb - prev_rss_mb) >= JUMP_ALERT_DELTA_MB


def rotate_if_needed(path: Path, max_bytes: int = ROTATE_MAX_BYTES) -> None:
    """日志超阈即轮转：mem_watch.jsonl -> mem_watch.jsonl.1（保留一代）。"""
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    path.replace(path.with_name(path.name + ".1"))


def find_kernel_rss_mb(sample: dict[str, Any]) -> float | None:
    """从样本中取内核进程 RSS；本机无内核进程（代际间隙）返回 None。"""
    for proc in sample.get("procs", []):
        if "agentos-kernel" in (proc.get("name") or "").lower():
            return float(proc.get("rss_mb") or 0.0)
    return None


class MemWatch:
    """细粒度内存观测循环（独立于 PerformanceMonitor 的 5s 面板采样面）。"""

    def __init__(self, log_path: Path | None = None, interval_secs: float | None = None):
        env_value = os.environ.get("AGENTOS_MEM_WATCH_INTERVAL_SEC")
        try:
            configured = float(env_value) if env_value else None
        except ValueError:
            configured = None
        effective = configured or interval_secs or DEFAULT_INTERVAL_SECS
        self._interval = max(effective, MIN_INTERVAL_SECS)
        if log_path is None:
            base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
            log_path = base / "agentos" / "logs" / "mem_watch.jsonl"
        self._log_path = log_path
        self._prev_kernel_rss_mb: float | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            sample = collect_sample()
            self._record(sample)
            kernel_rss = find_kernel_rss_mb(sample)
            if kernel_rss is not None and detect_jump(self._prev_kernel_rss_mb, kernel_rss):
                logger.warning(
                    "mem_watch 跳变告警: 内核 RSS %sMB -> %sMB（单采样间隔增量超 %sMB）",
                    self._prev_kernel_rss_mb,
                    kernel_rss,
                    JUMP_ALERT_DELTA_MB,
                )
            self._prev_kernel_rss_mb = kernel_rss
            await asyncio.sleep(self._interval)

    def _record(self, sample: dict[str, Any]) -> None:
        rotate_if_needed(self._log_path)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
