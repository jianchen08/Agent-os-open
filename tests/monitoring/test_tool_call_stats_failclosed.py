# @feature: FP-0.2.可观测性 可观测性基座 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring /ext/monitoring/tool-calls/stats fail-closed 分支补测。

BUG-47 窗口化统计查询（_query_tool_call_stats）的两条诚实降级分支：
- 内核库文件缺失 → error 标注，不静默空成功；
- 库文件不可读（非 SQLite 内容）→ sqlite3.Error 如实透传。

独立文件补位（test_http_routes.py 覆盖明细查询同族分支与统计成功面），
importlib 唯一模块名防平铺 import 串扰。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SERVER_PY = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring" / "server.py"
)
_spec = importlib.util.spec_from_file_location("monitoring_server_stats_fc", _SERVER_PY)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def test_stats_fail_closed_when_kernel_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """库文件缺失 → error=kernel db not found（fail-closed，不假装空统计成功）。"""
    monkeypatch.setattr(server, "_kernel_db_path", lambda: str(tmp_path / "nope.db"))
    out = server._query_tool_call_stats({}, tenant_id="t1")
    assert out["items"] == [] and out["total"] == 0
    assert out["error"] == "kernel db not found"


def test_stats_sqlite_error_degrades_with_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """库文件非 SQLite 内容 → sqlite3.Error 文本如实回 error，items 空。"""
    junk = tmp_path / "kernel.db"
    junk.write_text("definitely not a sqlite database", encoding="utf-8")
    monkeypatch.setattr(server, "_kernel_db_path", lambda: str(junk))
    out = server._query_tool_call_stats({}, tenant_id="t1")
    assert out["items"] == [] and out["total"] == 0
    assert out["error"]
    assert "not a database" in out["error"]
