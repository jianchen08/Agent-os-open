# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""monitoring routes_search 本体缺口补测（行号锚定 2026-09-13 插桩车道）。

既有 test_http_routes.py 只在 server 分发层 stub routes_search；本文件直测
search 域本体：

1. ``_tenant_pipeline_ids``：租户缺失 / 库缺失 / 真库 run_status 簿记键直查 /
   库损坏 fail-closed → None（绝不全量兜底）；
2. ``_search_sessions``：租户缺失 / 库缺失 / 大小写不敏感子串 / 租户隔离 /
   LIKE 通配符显式转义（% _ \\ 不充当模式）/ last_active_at 排序 / limit /
   库损坏 → []；
3. ``_search_messages``：不可见集 fail-closed / 管道白名单过滤 / 前览子串
   匹配 / 无前览跳过 / 200 字符截断 / limit 命中即返；
4. ``search()``：type 非法 ValueError / 空词空结果 / 租户缺失 fail-closed /
   type 三值分流。

内核库走真实 SQLite tmp 库（sessions/runs 表与内核落库同形）；
kernel_reads/execution_records 桥为外部依赖 stub。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# kernel_db 位于 plugins/shared 根（共享平铺层），monitoring conftest 只注入
# 插件目录——本文件自补 shared 根（去重插入，防车道残留目录抢占解析）。
_SHARED_ROOT = str(Path(__file__).resolve().parents[2] / "plugins" / "shared")
if _SHARED_ROOT in __import__("sys").path:
    __import__("sys").path.remove(_SHARED_ROOT)
__import__("sys").path.insert(0, _SHARED_ROOT)

import routes_search  # noqa: E402 — tests/monitoring/conftest 注入插件目录


@pytest.fixture
def kernel_db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """真实 tmp 内核库（sessions / pipeline_state 同形表）+ kernel_db_path 指向它。

    runs 表已退役（ADR 2026-09-18）：执行过 = pipeline_state 有 run_status 簿记键。
    """
    import kernel_db

    db = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT, "
        "tenant_id TEXT, updated_at TEXT, last_active_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, "
        "value TEXT, value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        [
            ("th-a", "灵汐小说连载", "tenant-1", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
            ("th-b", "LINGXI 项目", "tenant-1", "2026-09-01T00:00:00Z", None),
            ("th-c", "别人的会话", "tenant-2", "2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z"),
            ("th-d", "100% 进度讨论", "tenant-1", "2026-09-01T00:00:00Z", None),
        ],
    )
    conn.executemany(
        "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        [
            ("pipe-1", "run_status", "completed", "str", "tenant-1", "2026-09-02T00:00:00Z"),
            ("pipe-2", "run_status", "completed", "str", "tenant-1", "2026-09-01T00:00:00Z"),
            ("pipe-x", "run_status", "completed", "str", "tenant-2", "2026-09-03T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(kernel_db, "kernel_db_path", lambda: db)
    return db


def _corrupt_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kernel_db

    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite file" * 10)
    monkeypatch.setattr(kernel_db, "kernel_db_path", lambda: db)


# ═══════════════════════════════════════════════════════════
# _tenant_pipeline_ids（租户可见管道集）
# ═══════════════════════════════════════════════════════════


class TestTenantPipelineIds:
    def test_missing_tenant_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert routes_search._tenant_pipeline_ids("") is None

    def test_missing_db_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kernel_db

        monkeypatch.setattr(kernel_db, "kernel_db_path", lambda: tmp_path / "nope.db")
        assert routes_search._tenant_pipeline_ids("tenant-1") is None

    def test_real_db_returns_tenant_pipelines(self, kernel_db_file: Path) -> None:
        ids = routes_search._tenant_pipeline_ids("tenant-1")
        assert ids == {"pipe-1", "pipe-2"}

    def test_limit_respected(self, kernel_db_file: Path) -> None:
        ids = routes_search._tenant_pipeline_ids("tenant-1", limit=1)
        assert ids == {"pipe-1"}

    def test_corrupt_db_fail_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _corrupt_db(tmp_path, monkeypatch)
        assert routes_search._tenant_pipeline_ids("tenant-1") is None


# ═══════════════════════════════════════════════════════════
# _search_sessions（sessions 表标题子串）
# ═══════════════════════════════════════════════════════════


class TestSearchSessions:
    def test_missing_tenant_returns_empty(self) -> None:
        assert routes_search._search_sessions("x", 20, "") == []

    def test_missing_db_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kernel_db

        monkeypatch.setattr(kernel_db, "kernel_db_path", lambda: tmp_path / "nope.db")
        assert routes_search._search_sessions("x", 20, "tenant-1") == []

    def test_case_insensitive_substring_tenant_isolated(
        self, kernel_db_file: Path
    ) -> None:
        hits = routes_search._search_sessions("lingxi", 20, "tenant-1")
        assert [h["id"] for h in hits] == ["th-b"]

        hits = routes_search._search_sessions("小说", 20, "tenant-1")
        assert [h["id"] for h in hits] == ["th-a"]
        # 租户隔离：tenant-2 的命中绝不出现在 tenant-1 视角
        assert all(h["id"] != "th-c" for h in hits)

    def test_like_wildcards_are_literal(self, kernel_db_file: Path) -> None:
        """% _ \\ 显式转义：用户输入不充当 LIKE 模式（字面子串匹配）。

        "%" 命中且仅命中标题含字面 % 的 th-d——若 % 是通配符应命中全部 4 行；
        "_" 无任何字面命中——若 _ 是通配符应命中全部。
        """
        assert routes_search._search_sessions("_", 20, "tenant-1") == []
        assert routes_search._search_sessions("\\", 20, "tenant-1") == []
        hits = routes_search._search_sessions("%", 20, "tenant-1")
        assert [h["id"] for h in hits] == ["th-d"]

    def test_ordering_and_limit(self, kernel_db_file: Path) -> None:
        """last_active_at 优先、updated_at 兜底降序；limit 截断。"""
        conn = sqlite3.connect(kernel_db_file)
        conn.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                # probe-b 兜底键 09-06 > probe-a last_active 09-05 → probe-b 先
                ("th-e", "Order Probe A", "tenant-1", "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z"),
                ("th-f", "Order Probe B", "tenant-1", "2026-09-06T00:00:00Z", None),
            ],
        )
        conn.commit()
        conn.close()

        hits = routes_search._search_sessions("order probe", 20, "tenant-1")
        assert [h["id"] for h in hits] == ["th-f", "th-e"]
        limited = routes_search._search_sessions("order probe", 1, "tenant-1")
        assert [h["id"] for h in limited] == ["th-f"]

    def test_corrupt_db_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _corrupt_db(tmp_path, monkeypatch)
        assert routes_search._search_sessions("x", 20, "tenant-1") == []


# ═══════════════════════════════════════════════════════════
# _search_messages（管道白名单 × 前览子串）
# ═══════════════════════════════════════════════════════════


def _seed_message_search(
    monkeypatch: pytest.MonkeyPatch,
    pipelines: list[dict[str, Any]],
    messages_by_pid: dict[str, list[dict[str, Any]]],
) -> None:
    er = routes_search.er
    monkeypatch.setattr(er, "recent_pipelines", AsyncMock(return_value=pipelines))
    kr = routes_search.kernel_reads
    monkeypatch.setattr(
        kr,
        "list_messages",
        AsyncMock(side_effect=lambda pid, limit=None: messages_by_pid.get(pid, [])),
    )


class TestSearchMessages:
    async def test_no_allowed_pipelines_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await routes_search._search_messages("x", 20, None) == []
        assert await routes_search._search_messages("x", 20, set()) == []

    async def test_pipeline_whitelist_and_substring_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_message_search(
            monkeypatch,
            pipelines=[{"pipeline_id": "pipe-1"}, {"pipeline_id": "pipe-2"}, {"pipeline_id": "pipe-x"}],
            messages_by_pid={
                "pipe-1": [
                    {"message_id": "m1", "content_preview": "今天写第二章 灵汐", "role": "assistant",
                     "created_at": "t1", "seq_in_branch": 1},
                    {"message_id": "m2", "content_preview": "无关消息", "role": "user",
                     "created_at": "t2", "seq_in_branch": 2},
                ],
                "pipe-x": [  # 不在租户白名单：绝不扫描
                    {"message_id": "mx", "content_preview": "灵汐", "role": "user",
                     "created_at": "t3", "seq_in_branch": 0},
                ],
            },
        )
        hits = await routes_search._search_messages("灵汐", 20, {"pipe-1"})
        assert [h["id"] for h in hits] == ["m1"]
        assert hits[0]["session_id"] == "pipe-1"
        assert hits[0]["role"] == "assistant"
        assert hits[0]["sequence"] == 1

    async def test_empty_preview_skipped_and_content_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        long_content = "灵汐" + "长" * 300
        _seed_message_search(
            monkeypatch,
            pipelines=[{"pipeline_id": "pipe-1"}],
            messages_by_pid={
                "pipe-1": [
                    {"message_id": "m0", "content_preview": "", "role": "user"},
                    {"message_id": "m1", "content_preview": long_content, "role": "assistant",
                     "created_at": "t1", "seq_in_branch": 1},
                ],
            },
        )
        hits = await routes_search._search_messages("灵汐", 20, {"pipe-1"})
        assert len(hits) == 1
        assert len(hits[0]["content"]) == routes_search._MESSAGE_CONTENT_MAX
        assert hits[0]["content"].startswith("灵汐")

    async def test_limit_stops_scan_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_message_search(
            monkeypatch,
            pipelines=[{"pipeline_id": "pipe-1"}],
            messages_by_pid={
                "pipe-1": [
                    {"message_id": f"m{i}", "content_preview": "灵汐命中", "role": "user",
                     "created_at": "t", "seq_in_branch": i}
                    for i in range(5)
                ],
            },
        )
        hits = await routes_search._search_messages("灵汐", 2, {"pipe-1"})
        assert len(hits) == 2


# ═══════════════════════════════════════════════════════════
# search() 统一入口
# ═══════════════════════════════════════════════════════════


class TestSearchEntry:
    async def test_invalid_type_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="type"):
            await routes_search.search("q", type="bogus")

    @pytest.mark.parametrize("q", ["", "   "])
    async def test_blank_query_returns_empty(self, q: str) -> None:
        out = await routes_search.search(q, type="all", tenant_id="tenant-1")
        assert out == {"query": q, "type": "all", "sessions": [], "messages": []}

    async def test_missing_tenant_fail_closed(self, kernel_db_file: Path) -> None:
        """租户缺失：q 非空也返回空集（跨租户泄露防线，不报错）。"""
        out = await routes_search.search("灵汐", type="all", tenant_id="")
        assert out["sessions"] == [] and out["messages"] == []

    async def test_type_session_scopes_to_sessions(self, kernel_db_file: Path) -> None:
        out = await routes_search.search("lingxi", type="session", tenant_id="tenant-1")
        assert [s["id"] for s in out["sessions"]] == ["th-b"]
        assert out["messages"] == []

    async def test_type_message_and_all(self, kernel_db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_message_search(
            monkeypatch,
            pipelines=[{"pipeline_id": "pipe-1"}],
            messages_by_pid={
                "pipe-1": [
                    {"message_id": "m1", "content_preview": "关于 lingxi 的消息", "role": "user",
                     "created_at": "t", "seq_in_branch": 0},
                ],
            },
        )
        out = await routes_search.search("lingxi", type="message", tenant_id="tenant-1")
        assert out["sessions"] == []
        assert [m["id"] for m in out["messages"]] == ["m1"]

        out = await routes_search.search("lingxi", type="all", tenant_id="tenant-1")
        assert len(out["sessions"]) == 1 and len(out["messages"]) == 1
