# @feature: FP-0.2.〇 内核库查询脚本行为 | @ci: python-coverage
"""kernel_db_query.py 行为测试：只读守卫、快照隔离、截断、超时、源库零触碰。

脚本非包成员（scripts/ 平铺），按本仓既有 importlib 装载模式（同
plugins/shared/task_submit/test_danger_targets.py）从路径装载被测模块。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "kernel_db_query.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("kernel_db_query_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass/注解解析要求模块可被 sys.modules 查获
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_mod()


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    db = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("create table task_state (pipeline_id text, field_key text, value text)")
        conn.executemany(
            "insert into task_state values (?, ?, ?)",
            [
                ("d72aa876fc2e", "agent.id", "general_agent"),
                ("d72aa876fc2e", "task.status", "cancelled"),
                ("fa3b84079256", "task.status", "running"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


class TestValidateSql:
    def test_rejects_non_read_statements(self, mod) -> None:
        for bad in [
            "update task_state set value = 'x'",
            "insert into task_state values ('p', 'k', 'v')",
            "delete from task_state",
            "drop table task_state",
            "pragma query_only = OFF",
            "attach database 'other.db' as other",
            "-- only comment\n",
            "/* block */",
            "   ",
            "",
        ]:
            with pytest.raises(mod.QueryError):
                mod.validate_sql(bad)

    def test_allows_read_prefixes_with_leading_comments(self, mod) -> None:
        for ok in [
            "select 1",
            "  with x as (select 1 as n) select n from x",
            "explain select 1",
            "values (1)",
            "-- lookup\nselect 1",
            "/* header */ select 1",
            "values(1)",
        ]:
            assert mod.validate_sql(ok).lower().startswith(
                ("select", "with", "explain", "values")
            )


class TestRunQuery:
    def test_returns_columns_and_rows(self, mod, sample_db: Path) -> None:
        outcome = mod.run_query(
            sample_db, "select pipeline_id, field_key from task_state where field_key = 'task.status' order by pipeline_id"
        )
        assert outcome.columns == ["pipeline_id", "field_key"]
        assert outcome.rows == [
            ["d72aa876fc2e", "task.status"],
            ["fa3b84079256", "task.status"],
        ]
        assert outcome.truncated is False

    def test_truncates_at_limit(self, mod, sample_db: Path) -> None:
        outcome = mod.run_query(sample_db, "select pipeline_id from task_state", limit=2)
        assert len(outcome.rows) == 2
        assert outcome.truncated is True

    def test_rejects_multi_statement(self, mod, sample_db: Path) -> None:
        with pytest.raises(mod.QueryError, match="多语句"):
            mod.run_query(sample_db, "select 1; select 2")

    def test_rejects_write_before_snapshot(self, mod, sample_db: Path) -> None:
        with pytest.raises(mod.QueryError, match="只允许只读"):
            mod.run_query(sample_db, "delete from task_state")

    def test_source_db_untouched(self, mod, sample_db: Path) -> None:
        before = sample_db.read_bytes()
        before_files = {p.name for p in sample_db.parent.iterdir()}
        mod.run_query(sample_db, "select count(*) from task_state")
        assert sample_db.read_bytes() == before
        assert {p.name for p in sample_db.parent.iterdir()} == before_files

    def test_timeout_interrupts(self, mod, sample_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_PROGRESS_OPS", 1)
        with pytest.raises(mod.QueryError, match="超时"):
            mod.run_query(sample_db, "select pipeline_id from task_state", timeout_s=0.0)


class TestResolveDb:
    def test_explicit_missing_path_rejected(self, mod, tmp_path: Path) -> None:
        with pytest.raises(mod.QueryError, match="不存在"):
            mod.resolve_db(str(tmp_path / "nope.db"))

    def test_explicit_existing_path_wins(self, mod, sample_db: Path) -> None:
        assert mod.resolve_db(str(sample_db)) == sample_db


class TestFormatTable:
    def test_caps_oversized_cells(self, mod) -> None:
        outcome = mod.QueryOutcome(
            columns=["value"], rows=[["x" * 200]], truncated=False
        )
        table = mod.format_table(outcome)
        rendered_row = table.splitlines()[-1]
        assert len(rendered_row) <= mod._CELL_CAP + 1  # 截断加省略号，不刷屏
        assert rendered_row.endswith("…")
