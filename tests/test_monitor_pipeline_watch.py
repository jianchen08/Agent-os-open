# @feature: FP-0.2.〇 可观测面 | @vision: V5 可运维 | @ci: python-coverage
"""monitor_pipeline_watch 分类契约测试（长稳 B9：等待态不误报 STUCK）。

判定优先级契约：human_interaction 等待（Suspended + pending_interaction_request_id）
与停泊挂起必须先于 RUNNING_STALE 判定短路——长稳测试中任务升级等人工被
"无进展"口径误报卡死，触发无效运维干预。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "monitor_pipeline_watch", str(_DIR.parent / "scripts" / "monitor_pipeline_watch.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["monitor_pipeline_watch"] = _mod
_spec.loader.exec_module(_mod)

_RECENT = datetime.now(timezone.utc) - timedelta(minutes=1)
_STALE = datetime.now(timezone.utc) - timedelta(hours=2)
_CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=30)


def _run(**over):
    base = {
        "run_id": "r1",
        "pipeline_id": "p1",
        "status": "running",
        "created_at": _RECENT,
        "last_trace_at": _RECENT,
        "task_status": "running",
        "interaction_request_id": None,
    }
    base.update(over)
    return base


class TestClassifyPriority:
    def test_interaction_wait_beats_stale_trace(self) -> None:
        """等待人工且 trace 停滞 2 小时 → WAITING_HUMAN（绝不 STUCK）。"""
        run = _run(
            status="suspended",
            last_trace_at=_STALE,
            interaction_request_id="req-7",
        )
        assert _mod.classify(run, _CUTOFF) == "WAITING_HUMAN"

    def test_suspended_beats_stale_trace(self) -> None:
        """停泊挂起（子任务挂号等待唤醒等）trace 停滞 → SUSPENDED。"""
        run = _run(status="suspended", last_trace_at=_STALE)
        assert _mod.classify(run, _CUTOFF) == "SUSPENDED"

    def test_running_with_stale_trace_is_stuck(self) -> None:
        run = _run(last_trace_at=_STALE)
        assert _mod.classify(run, _CUTOFF) == "RUNNING_STALE"

    def test_running_with_active_trace(self) -> None:
        assert _mod.classify(_run(), _CUTOFF) == "RUNNING"

    def test_running_never_traced_uses_created_at_anchor(self) -> None:
        """从未落 trace 的 run 以 created_at 为停滞锚点（出生即无产出）。"""
        run = _run(last_trace_at=None, created_at=_STALE)
        assert _mod.classify(run, _CUTOFF) == "RUNNING_STALE"


class TestDbLoading:
    def _setup_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "k.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE runs (run_id TEXT, pipeline_id TEXT, status TEXT,
                created_at TEXT, ended_at TEXT, metadata TEXT);
            CREATE TABLE traces (run_id TEXT, created_at TEXT);
            CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, field_value TEXT);
            INSERT INTO runs VALUES ('r-h', 'p1', 'suspended', '2026-09-04T06:00:00Z', NULL,
                '{"pending_interaction_request_id": "req-9"}');
            INSERT INTO runs VALUES ('r-s', 'p2', 'running', '2026-09-04T06:00:00Z', NULL, NULL);
            INSERT INTO traces VALUES ('r-s', '2026-09-04T05:00:00Z');
            INSERT INTO pipeline_state VALUES ('p1', 'task.status', 'running');
            """
        )
        conn.commit()
        conn.close()
        return db

    def test_interaction_metadata_parsed_and_state_joined(self, tmp_path: Path) -> None:
        db = self._setup_db(tmp_path)
        conn = sqlite3.connect(db)
        try:
            runs = _mod._load_active_runs(conn)
        finally:
            conn.close()
        by_id = {r["run_id"]: r for r in runs}
        assert set(by_id) == {"r-h", "r-s"}, "只加载活跃 run"
        assert by_id["r-h"]["interaction_request_id"] == "req-9"
        assert by_id["r-h"]["task_status"] == "running"
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        assert _mod.classify(by_id["r-h"], cutoff) == "WAITING_HUMAN"
        assert _mod.classify(by_id["r-s"], cutoff) == "RUNNING_STALE"

    def test_corrupt_metadata_is_not_interaction_wait(self, tmp_path: Path) -> None:
        db = tmp_path / "k.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE runs (run_id TEXT, pipeline_id TEXT, status TEXT,
                created_at TEXT, ended_at TEXT, metadata TEXT);
            CREATE TABLE traces (run_id TEXT, created_at TEXT);
            CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, field_value TEXT);
            INSERT INTO runs VALUES ('r-x', 'p9', 'suspended', '2026-09-04T06:00:00Z', NULL, '{broken');
            """
        )
        conn.commit()
        runs = _mod._load_active_runs(conn)
        conn.close()
        assert runs[0]["interaction_request_id"] is None
        assert _mod.classify(runs[0], _CUTOFF) == "SUSPENDED"
