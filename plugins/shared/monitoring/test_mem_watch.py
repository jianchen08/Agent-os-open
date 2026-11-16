# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可观测 | @ci: python-coverage
"""mem_watch 细粒度内存观测器单元测试。

覆盖（对齐 plugins/shared/monitoring/mem_watch.py）：
1. filter_target 三族匹配与排除
2. detect_jump 跳变判定（首样/增量/回落）
3. rotate_if_needed 轮转（未达阈不动 / 达阈改名 .1）
4. collect_sample 结构与排序（psutil 伪进程注入，不依赖真实进程面）
5. find_kernel_rss_mb 命中与代际间隙
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import mem_watch


def test_filter_target_matches_three_families() -> None:
    assert mem_watch.filter_target("agentos-kernel.exe") is True
    assert mem_watch.filter_target("灵汐助手.exe") is True
    assert mem_watch.filter_target("PYTHON.EXE") is True
    assert mem_watch.filter_target("chrome.exe") is False
    assert mem_watch.filter_target("svchost.exe") is False
    assert mem_watch.filter_target("") is False


def test_detect_jump_first_sample_and_threshold() -> None:
    assert mem_watch.detect_jump(None, 500.0) is False
    assert mem_watch.detect_jump(100.0, 399.9) is False
    assert mem_watch.detect_jump(100.0, 400.0) is True
    assert mem_watch.detect_jump(100.0, 50.0) is False


def test_rotate_if_needed(tmp_path: Path) -> None:
    small = tmp_path / "mem_watch.jsonl"
    small.write_text("x", encoding="utf-8")
    mem_watch.rotate_if_needed(small, max_bytes=100)
    assert small.exists() and not Path(str(small) + ".1").exists()
    small.write_text("y" * 200, encoding="utf-8")
    mem_watch.rotate_if_needed(small, max_bytes=100)
    assert not small.exists()
    rotated = Path(str(small) + ".1")
    assert rotated.exists() and len(rotated.read_text(encoding="utf-8")) == 200


def test_collect_sample_shape_and_sort(tmp_path: Path, monkeypatch: Any) -> None:
    fake_procs = [
        SimpleNamespace(info={"pid": 1, "name": "python.exe", "memory_info": SimpleNamespace(rss=100 * 1024 * 1024)}),
        SimpleNamespace(info={"pid": 2, "name": "agentos-kernel.exe", "memory_info": SimpleNamespace(rss=300 * 1024 * 1024)}),
        SimpleNamespace(info={"pid": 3, "name": "灵汐助手.exe", "memory_info": SimpleNamespace(rss=200 * 1024 * 1024)}),
        SimpleNamespace(info={"pid": 4, "name": "chrome.exe", "memory_info": SimpleNamespace(rss=999 * 1024 * 1024)}),
    ]

    class FakePsutil:
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        @staticmethod
        def process_iter(attrs: list[str]) -> list[Any]:
            return fake_procs

        @staticmethod
        def virtual_memory() -> SimpleNamespace:
            return SimpleNamespace(percent=55.5, available=7 * 1024 * 1024 * 1024)

    monkeypatch.setattr(mem_watch, "psutil", FakePsutil)
    sample = mem_watch.collect_sample()
    assert sample["sys_used_pct"] == 55.5
    assert sample["sys_avail_mb"] == 7168.0
    names = [p["name"] for p in sample["procs"]]
    assert names == ["agentos-kernel.exe", "灵汐助手.exe", "python.exe"]
    assert sample["procs"][0]["rss_mb"] == 300.0
    assert set(sample.keys()) == {"ts", "sys_used_pct", "sys_avail_mb", "procs"}


def test_collect_sample_survives_dying_process(monkeypatch: Any) -> None:
    class DyingProc:
        info: dict[str, Any] = {}

        def __init__(self, info: dict[str, Any]) -> None:
            self.info = info

    class FakePsutil:
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        @staticmethod
        def process_iter(attrs: list[str]) -> list[Any]:
            good = SimpleNamespace(
                info={"pid": 9, "name": "python.exe", "memory_info": SimpleNamespace(rss=10 * 1024 * 1024)}
            )
            return [DyingProc({"pid": 8, "name": "agentos-kernel.exe", "memory_info": None}), good]

        @staticmethod
        def virtual_memory() -> SimpleNamespace:
            return SimpleNamespace(percent=1.0, available=1)

    monkeypatch.setattr(mem_watch, "psutil", FakePsutil)
    sample = mem_watch.collect_sample()
    assert len(sample["procs"]) == 2
    by_name = {p["name"]: p["rss_mb"] for p in sample["procs"]}
    assert by_name["agentos-kernel.exe"] == 0.0
    assert by_name["python.exe"] == 10.0


def test_find_kernel_rss_mb_hit_and_gap() -> None:
    hit = {"procs": [{"name": "python.exe", "rss_mb": 1.0}, {"name": "agentos-kernel.exe", "rss_mb": 213.0}]}
    assert mem_watch.find_kernel_rss_mb(hit) == 213.0
    assert mem_watch.find_kernel_rss_mb({"procs": [{"name": "python.exe", "rss_mb": 1.0}]}) is None
    assert mem_watch.find_kernel_rss_mb({"procs": []}) is None


def test_mem_watch_record_and_rotate_end_to_end(tmp_path: Path) -> None:
    log_path = tmp_path / "mem_watch.jsonl"
    watch = mem_watch.MemWatch(log_path=log_path, interval_secs=5.0)
    watch._record({"ts": "T1", "procs": []})
    watch._record({"ts": "T2", "procs": []})
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["ts"] for line in lines] == ["T1", "T2"]
    assert watch._interval == 5.0


def test_mem_watch_interval_env_floor(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTOS_MEM_WATCH_INTERVAL_SEC", "0.5")
    watch = mem_watch.MemWatch(log_path=tmp_path / "w.jsonl")
    assert watch._interval == mem_watch.MIN_INTERVAL_SECS
    monkeypatch.setenv("AGENTOS_MEM_WATCH_INTERVAL_SEC", "not-a-number")
    watch2 = mem_watch.MemWatch(log_path=tmp_path / "w2.jsonl", interval_secs=15.0)
    assert watch2._interval == 15.0
