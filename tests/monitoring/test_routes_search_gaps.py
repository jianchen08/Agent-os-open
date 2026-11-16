# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""monitoring routes_search 本体缺口补测（行号锚定 2026-09-13 插桩车道）。

既有 test_http_routes.py 只在 server 分发层 stub routes_search；本文件直测
search 域本体（C2 刀2 后数据源 = db-read 契约，替身 = _libs/testing 共享
FakeDbRead：真 SQL 落测试自有 sqlite + tenant 档收窄包装仿真）：

1. ``_tenant_pipeline_ids``：租户缺失 / 契约失败 fail-closed → None（绝不
   全量兜底）/ 真库 run_status 簿记键直查（身份经 authorization 穿线）/
   limit / 他租户谓词值过度限制；
2. ``_search_sessions``：租户缺失 / 大小写不敏感子串 / 租户隔离 / LIKE
   通配符显式转义（% _ \\ 不充当模式）/ last_active_at 排序 / limit /
   契约失败 → []；
3. ``_search_messages``：不可见集 fail-closed / 管道白名单过滤 / 前览子串
   匹配 / 无前览跳过 / 200 字符截断 / limit 命中即返；
4. ``search()``：type 非法 ValueError / 空词空结果 / 租户缺失 fail-closed /
   type 三值分流。

收窄执法本体由 Rust 契约测试承担（read_faces/tests.rs）；本文件钉插件侧
SQL 形状与装配契约。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# kernel_reads/db_read/_libs 位于 plugins/shared 根（共享平铺层），monitoring
# conftest 只注入插件目录——本文件自补 shared 根（去重插入，防车道残留目录
# 抢占解析）。
_SHARED_ROOT = str(Path(__file__).resolve().parents[2] / "plugins" / "shared")
if _SHARED_ROOT in sys.path:
    sys.path.remove(_SHARED_ROOT)
sys.path.insert(0, _SHARED_ROOT)

import kernel_reads  # noqa: E402 — tests/monitoring/conftest 注入插件目录
import routes_search  # noqa: E402
from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402


@pytest.fixture
def kernel_db_file(tmp_path: Path) -> Path:
    """真实 tmp 库（sessions / pipeline_state 同形表），供替身执行真 SQL。

    runs 表已退役（ADR 2026-09-18）：执行过 = pipeline_state 有 run_status 簿记键。
    """
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
    return db


@pytest.fixture(autouse=True)
def _reset_kernel_reads_providers():
    """用例后清空 kernel_reads provider 注册表（db-read 注入不跨用例渗漏）。"""
    yield
    kernel_reads.reset_providers()


def _install_db_read(
    db_path: Path,
    token_tenants: dict[str, str] | None = None,
    fail: Exception | None = None,
) -> FakeDbRead:
    fake = FakeDbRead(db_path, token_tenants, fail)
    kernel_reads.set_provider("db-read", fake.provider)
    return fake


# ═══════════════════════════════════════════════════════════
# _tenant_pipeline_ids（租户可见管道集）
# ═══════════════════════════════════════════════════════════


class TestTenantPipelineIds:
    async def test_missing_tenant_returns_none(self) -> None:
        fake = _install_db_read(Path("unused.db"))
        assert await routes_search._tenant_pipeline_ids("") is None
        assert fake.calls == []  # 身份缺失不发起契约查询（fail-closed 前置）

    async def test_contract_failure_returns_none(self, tmp_path: Path) -> None:
        # 通道异常 / 内核 403 信封（无凭证）一律 None——绝不退化全量兜底
        _install_db_read(
            tmp_path / "nope.db", {"tok-1": "tenant-1"}, fail=RuntimeError("bridge down")
        )
        assert await routes_search._tenant_pipeline_ids("tenant-1", "tok-1") is None

        _install_db_read(tmp_path / "nope.db", token_tenants={})
        assert await routes_search._tenant_pipeline_ids("tenant-1", "anon") is None

    async def test_real_db_returns_tenant_pipelines(self, kernel_db_file: Path) -> None:
        fake = _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        ids = await routes_search._tenant_pipeline_ids("tenant-1", "tok-1")
        assert ids == {"pipe-1", "pipe-2"}
        # 身份与数据面逐值穿线（face=pipeline_state，tenant 档）
        assert fake.calls[0]["face"] == "pipeline_state"
        assert fake.calls[0]["_authorization"] == "tok-1"
        assert fake.calls[0]["params"]["tenant"] == "tenant-1"

    async def test_limit_respected(self, kernel_db_file: Path) -> None:
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        ids = await routes_search._tenant_pipeline_ids("tenant-1", "tok-1", limit=1)
        assert ids == {"pipe-1"}

    async def test_wrong_tenant_predicate_over_restricts(self, kernel_db_file: Path) -> None:
        # 内层租户谓词值与身份租户不一致 = 过度限制（空集），绝不越界到他租户
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        assert await routes_search._tenant_pipeline_ids("tenant-2", "tok-1") == set()


# ═══════════════════════════════════════════════════════════
# _search_sessions（sessions 表标题子串）
# ═══════════════════════════════════════════════════════════


class TestSearchSessions:
    async def test_missing_tenant_returns_empty(self) -> None:
        fake = _install_db_read(Path("unused.db"))
        assert await routes_search._search_sessions("x", 20, "") == []
        assert fake.calls == []

    async def test_contract_failure_returns_empty(self, tmp_path: Path) -> None:
        _install_db_read(
            tmp_path / "nope.db", {"tok-1": "tenant-1"}, fail=RuntimeError("bridge down")
        )
        assert await routes_search._search_sessions("x", 20, "tenant-1", "tok-1") == []

    async def test_case_insensitive_substring_tenant_isolated(
        self, kernel_db_file: Path
    ) -> None:
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        hits = await routes_search._search_sessions("lingxi", 20, "tenant-1", "tok-1")
        assert [h["id"] for h in hits] == ["th-b"]

        hits = await routes_search._search_sessions("小说", 20, "tenant-1", "tok-1")
        assert [h["id"] for h in hits] == ["th-a"]
        # 租户隔离：tenant-2 的命中绝不出现在 tenant-1 视角
        assert all(h["id"] != "th-c" for h in hits)

    async def test_like_wildcards_are_literal(self, kernel_db_file: Path) -> None:
        """% _ \\ 显式转义：用户输入不充当 LIKE 模式（字面子串匹配）。

        "%" 命中且仅命中标题含字面 % 的 th-d——若 % 是通配符应命中全部 4 行；
        "_" 无任何字面命中——若 _ 是通配符应命中全部。
        """
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        assert await routes_search._search_sessions("_", 20, "tenant-1", "tok-1") == []
        assert await routes_search._search_sessions("\\", 20, "tenant-1", "tok-1") == []
        hits = await routes_search._search_sessions("%", 20, "tenant-1", "tok-1")
        assert [h["id"] for h in hits] == ["th-d"]

    async def test_ordering_and_limit(self, kernel_db_file: Path) -> None:
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

        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        hits = await routes_search._search_sessions("order probe", 20, "tenant-1", "tok-1")
        assert [h["id"] for h in hits] == ["th-f", "th-e"]
        limited = await routes_search._search_sessions("order probe", 1, "tenant-1", "tok-1")
        assert [h["id"] for h in limited] == ["th-f"]


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
        """租户缺失：q 非空也返回空集（跨租户泄露防线，不报错、不发查询）。"""
        fake = _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        out = await routes_search.search("灵汐", type="all", tenant_id="")
        assert out["sessions"] == [] and out["messages"] == []
        assert fake.calls == []

    async def test_type_session_scopes_to_sessions(self, kernel_db_file: Path) -> None:
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        out = await routes_search.search(
            "lingxi", type="session", tenant_id="tenant-1", authorization="tok-1"
        )
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
        _install_db_read(kernel_db_file, {"tok-1": "tenant-1"})
        out = await routes_search.search(
            "lingxi", type="message", tenant_id="tenant-1", authorization="tok-1"
        )
        assert out["sessions"] == []
        assert [m["id"] for m in out["messages"]] == ["m1"]

        out = await routes_search.search(
            "lingxi", type="all", tenant_id="tenant-1", authorization="tok-1"
        )
        assert len(out["sessions"]) == 1 and len(out["messages"]) == 1
