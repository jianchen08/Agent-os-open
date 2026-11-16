# @feature: FP-GATE 任务域投影漂移只读扫描 | @ci: python-coverage
"""scan_task_projection_drift.py 单测（只读扫描器的行为契约）。

断行为不断实现（输入 → 分类/清单/只读性）：
- 分类矩阵：一致 / 登记型（无自身投影行）/ 主修类（自身终态、镜像旧值）/
  复活方向残留（自身在跑、镜像终态）；
- 建议修正值 = 被拥有任务自身 task.status 现值（不猜、不造词）；
- 键面解析：owned_id 从 ``task.owned.<id>.status`` 精确切出（含 id 内含点号的
  形态——切错即改错键）；
- 只读性：库以只读方式打开（写入被拒）、PRAGMA query_only 生效；
- CLI：--json-out 导出待修清单；--fail-on-drift 有待修项时退出码 1；
- 负控制：真实仓内存在可扫的库形态（合成库覆盖路径与真实表结构一致）。

外部依赖替身边界：sqlite3 为标准库；被扫库为合成 fixture（临时目录内自建
pipeline_state 表，与内核 EAV 结构一致），不依赖任何真实用户数据。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "scan_task_projection_drift", SCRIPTS / "scan_task_projection_drift.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _make_db(tmp_path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> Path:
    """合成 pipeline_state 库（EAV 结构与内核一致）。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pipeline_state ("
        "pipeline_id TEXT NOT NULL, field_key TEXT NOT NULL, value TEXT NOT NULL, "
        "value_kind TEXT NOT NULL DEFAULT 'str', tenant_id TEXT NOT NULL DEFAULT 'default', "
        "updated_at TEXT NOT NULL, PRIMARY KEY (pipeline_id, field_key, tenant_id))"
    )
    conn.executemany(
        "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


def test_classify_matrix():
    """分类矩阵：一致 / 登记型 / 主修类 / 复活方向残留。"""
    assert mod.classify("failed", "failed") == "consistent"
    assert mod.classify(None, "running") == "registration_only"
    assert mod.classify("failed", "running") == "mirror_stale"
    assert mod.classify("completed", "running") == "mirror_stale"
    assert mod.classify("running", "failed") == "mirror_revived"
    assert mod.classify("pending", "failed") == "mirror_revived"
    assert mod.classify("evaluating", "completed") == "mirror_revived"


def test_build_plan_suggests_child_status_and_parses_owned_id():
    """建议修正值 = 自身投影现值；owned_id 精确切出（含 id 内含点）。"""
    owned_rows = [
        {
            "parent_pipeline_id": "chat_main",
            "field_key": "task.owned.child.1.status",
            "value": "running",
            "tenant_id": "default",
            "updated_at": "2026-10-20T10:00:00Z",
        }
    ]
    own_map = {
        ("default", "child.1"): {
            "child_pipeline_id": "child.1",
            "value": "failed",
            "tenant_id": "default",
            "updated_at": "2026-10-20T13:45:53Z",
        }
    }
    plan, counts = mod.build_plan(owned_rows, own_map)
    assert counts["mirror_stale"] == 1
    assert len(plan) == 1
    entry = plan[0]
    assert entry["owned_id"] == "child.1"
    assert entry["current_value"] == "running"
    assert entry["suggested_value"] == "failed"
    assert entry["parent_pipeline_id"] == "chat_main"
    assert entry["field_key"] == "task.owned.child.1.status"


def test_build_plan_skips_consistent_and_registration_only():
    """一致行与登记型任务不进待修清单（只计数）。"""
    owned_rows = [
        {
            "parent_pipeline_id": "chat_main",
            "field_key": "task.owned.a.status",
            "value": "failed",
            "tenant_id": "default",
            "updated_at": "",
        },
        {
            "parent_pipeline_id": "chat_main",
            "field_key": "task.owned.b.status",
            "value": "running",
            "tenant_id": "default",
            "updated_at": "",
        },
    ]
    own_map = {
        ("default", "a"): {
            "child_pipeline_id": "a",
            "value": "failed",
            "tenant_id": "default",
            "updated_at": "",
        }
    }
    plan, counts = mod.build_plan(owned_rows, own_map)
    assert plan == []
    assert counts["consistent"] == 1
    assert counts["registration_only"] == 1


def test_db_opened_readonly_rejects_writes(tmp_path: Path):
    """只读性：写入被 SQLite 拒绝（mode=ro + query_only 双保险）。"""
    db_path = _make_db(
        tmp_path,
        [("chat_main", "task.owned.child.status", "running", "str", "default", "t0")],
    )
    conn = mod.open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "UPDATE pipeline_state SET value='failed' WHERE field_key='task.owned.child.status'"
            )
    finally:
        conn.close()


def test_scan_reports_candidates_and_exports_json(tmp_path: Path, capsys):
    """CLI：打印待修清单 + 导出 JSON；--fail-on-drift 退出码 1。"""
    db_path = _make_db(
        tmp_path,
        [
            ("chat_main", "task.owned.child.status", "running", "str", "default", "t1"),
            ("child", "task.status", "failed", "str", "default", "t2"),
        ],
    )
    json_out = tmp_path / "out" / "plan.json"
    exit_code = mod.main(
        ["--db", str(db_path), "--json-out", str(json_out), "--fail-on-drift"]
    )
    assert exit_code == 1, "有待修项时 --fail-on-drift 必须非零"
    printed = capsys.readouterr().out
    assert "主修类" in printed
    assert "task.owned.child.status" in printed
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["counts"]["mirror_stale"] == 1
    assert payload["candidates"][0]["suggested_value"] == "failed"


def test_scan_clean_db_exits_zero(tmp_path: Path):
    """无漂移：退出码 0（纯扫描不阻断）。"""
    db_path = _make_db(
        tmp_path,
        [
            ("chat_main", "task.owned.child.status", "failed", "str", "default", "t1"),
            ("child", "task.status", "failed", "str", "default", "t2"),
        ],
    )
    assert mod.main(["--db", str(db_path), "--fail-on-drift"]) == 0


def test_tenant_filter_isolates_scan(tmp_path: Path):
    """租户过滤：只扫指定租户（跨租户同名 id 不串）。"""
    db_path = _make_db(
        tmp_path,
        [
            ("chat_main", "task.owned.child.status", "running", "str", "default", "t1"),
            ("child", "task.status", "failed", "str", "default", "t2"),
            ("chat_main", "task.owned.child.status", "running", "str", "other", "t3"),
            ("child", "task.status", "failed", "str", "other", "t4"),
        ],
    )
    conn = mod.open_readonly(db_path)
    try:
        owned_rows, own_map = mod.load_state_rows(conn, "default")
    finally:
        conn.close()
    plan, counts = mod.build_plan(owned_rows, own_map)
    assert counts["mirror_stale"] == 1
    assert len(plan) == 1
    assert plan[0]["tenant_id"] == "default"


def test_missing_db_fails_loudly(tmp_path: Path):
    """库不存在：显式失败（不静默返回空清单）。"""
    with pytest.raises(SystemExit):
        mod.main(["--db", str(tmp_path / "nope.db")])


def test_unopenable_db_fails_loudly(tmp_path: Path):
    """库路径不可打开（如指向目录）：显式失败并带原因（不裸抛栈）。"""
    with pytest.raises(SystemExit):
        mod.open_readonly(tmp_path)


def test_non_sqlite_file_fails_loudly(tmp_path: Path):
    """非 SQLite 文件：读取失败显式报错（不静默返回空清单）。"""
    bogus = tmp_path / "not-a-db.bin"
    bogus.write_bytes(b"definitely not sqlite")
    with pytest.raises(SystemExit):
        mod.main(["--db", str(bogus)])


def test_null_value_kind_decodes_to_empty_status(tmp_path: Path):
    """value_kind='null' 的状态值按空串解码（不把 'null' 当状态词）。"""
    db_path = _make_db(
        tmp_path,
        [
            ("chat_main", "task.owned.child.status", "running", "str", "default", "t1"),
            ("child", "task.status", "", "null", "default", "t2"),
        ],
    )
    conn = mod.open_readonly(db_path)
    try:
        owned_rows, own_map = mod.load_state_rows(conn, None)
    finally:
        conn.close()
    assert own_map[("default", "child")]["value"] == ""
    plan, counts = mod.build_plan(owned_rows, own_map)
    assert counts["mirror_revived"] == 1
    assert plan[0]["suggested_value"] == ""
