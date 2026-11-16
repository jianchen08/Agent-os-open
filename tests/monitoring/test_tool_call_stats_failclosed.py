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
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "monitoring" / "server.py"
)
_spec = importlib.util.spec_from_file_location("monitoring_server_stats_fc", _SERVER_PY)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


import asyncio  # noqa: E402 — 既有模块体后追加（读面替身装配需在模块态就绪）
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "plugins" / "shared"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — db-read 契约替身


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_stats_fail_closed_when_kernel_db_missing(tmp_path: Path) -> None:
    """db-read 契约失败（替身指向缺失库，执行期报错归一）→ error 如实回带，
    items 空（fail-closed，不假装空统计成功）。"""
    import kernel_reads  # noqa: F401 — server 同目录模块（provider 缝）

    kernel_reads.set_provider(
        "db-read", FakeDbRead(tmp_path / "nope.db", {"tok-t1": "t1"}).provider
    )
    out = _run(server._query_tool_call_stats({}, tenant_id="t1", authorization="tok-t1"))
    assert out["items"] == []
    assert out["total"] == 0
    assert out["error"]


def test_stats_sqlite_error_degrades_with_reason(tmp_path: Path) -> None:
    """库文件非 SQLite 内容 → sqlite3.Error 文本如实回 error，items 空。"""
    import kernel_reads  # noqa: F401

    junk = tmp_path / "kernel.db"
    junk.write_text("definitely not a sqlite database", encoding="utf-8")
    kernel_reads.set_provider("db-read", FakeDbRead(junk, {"tok-t1": "t1"}).provider)
    out = _run(server._query_tool_call_stats({}, tenant_id="t1", authorization="tok-t1"))
    assert out["items"] == []
    assert out["total"] == 0
    assert out["error"]
