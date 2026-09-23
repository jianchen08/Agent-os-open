# @feature: FP-0.2.可观测性 可观测性基座 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring /ext/monitoring/token-usage 从 pipeline_state 聚合测试。

累计口径真值 = state 的 track.llm_usage（track 插件每轮累加、逐管道自持）：
- token-usage（本文件）读 state 累计——跨运行累计、跨成员热重载保留；
- by-time 读 traces 逐轮明细（时间维度只有 traces 有，见 test_monitoring.py）。

历史（2026-08-18 G4）：token-usage 原恒 0（PerformanceMonitor 不持 token 计数）
→ 曾改为从 traces 聚合；2026-09-15 裁定累计口径归 state（traces 是明细副产物，
两源在当日新产生管道上逐值一致，历史差异源于打包测试期数据 churn）。

请求数/错误/耗时保持读 PerformanceMonitor 本地计数（record_llm_request 口径）；
DB 缺失/查询失败降级本地计数（token=0），契约不破坏。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SERVER_PY = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring" / "server.py"
)
_spec = importlib.util.spec_from_file_location("monitoring_server_under_test", _SERVER_PY)
monitoring_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_server)


def _seed_state(db_path: str) -> None:
    """写 state 累计行（模拟 track 插件落库形态）+ 一条与用量无关的键。

    state 值域标量化（ADR 2026-09-18）：标量按 value_kind='str' 原样存，
    非标量声明键（track.llm_usage 过渡键）按 value_kind='json' 存 JSON 文本。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT,"
            " value TEXT, value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
        )
        rows = [
            # 两管道同模型：累计相加（1500 + 1000）
            (
                "pipe-a",
                "track.llm_usage",
                json.dumps({"total_input_tokens": 1200, "total_output_tokens": 300, "total_tokens": 1500}),
                "json",
            ),
            ("pipe-a", "llm_model", "deepseek-v4-flash", "str"),
            (
                "pipe-b",
                "track.llm_usage",
                json.dumps({"total_input_tokens": 800, "total_output_tokens": 200, "total_tokens": 1000}),
                "json",
            ),
            ("pipe-b", "llm_model", "deepseek-v4-flash", "str"),
            # 与用量无关的键不得进入统计
            ("pipe-c", "task.status", "completed", "str"),
        ]
        for pid, key, val, kind in rows:
            conn.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)"
                " VALUES (?, ?, ?, ?, 'default', '2026-09-15T00:00:00Z')",
                (pid, key, val, kind),
            )
        conn.commit()
    finally:
        conn.close()


class TestTokenUsageFromState:
    """_collect_token_usage 聚合行为（累计真值源 = state）。"""

    def test_aggregates_state_llm_usage(self, monkeypatch) -> None:
        """DB 有 track.llm_usage：跨管道累加（2000/500/2500），本地计数保持。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state(db_path)
            monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

            monitor = monitoring_server._ensure_monitor()
            monitor._llm_stats.update({"request_count": 7, "active_requests": 1})
            usage = monitoring_server._collect_token_usage()

            assert usage["prompt_tokens"] == 2000
            assert usage["completion_tokens"] == 500
            assert usage["total_tokens"] == 2500
            # 本地计数保持（非零即真源）
            assert usage["request_count"] == 7
            assert usage["active_requests"] == 1

    def test_groups_by_model_from_state(self, monkeypatch) -> None:
        """按模型聚合：同模型多管道相加为一行（state 无 provider 键，图标按模型名）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state(db_path)
            monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

            usage = monitoring_server._collect_token_usage()
            rows = {r["model"]: r for r in usage["rows"]}

            assert set(rows) == {"deepseek-v4-flash"}
            assert rows["deepseek-v4-flash"]["total_tokens"] == 2500
            assert rows["deepseek-v4-flash"]["icon"] == "🐋"
            # 性质断言：总量卡 = 各模型行之和（同一查询源，不许两套账）
            assert usage["total_tokens"] == sum(r["total_tokens"] for r in usage["rows"])

    def test_db_missing_falls_back_local_counts(self, monkeypatch) -> None:
        """DB 不存在：token 为 0，本地计数照常返回，不抛异常。"""
        monkeypatch.setenv("AGENTOS_DB_PATH", "Z:/nonexistent/kernel.db")
        # 确保 _kernel_db_path 读 env（_collect_token_usage 内直接调用，不 mock）

        monitor = monitoring_server._ensure_monitor()
        monitor._llm_stats.update({"request_count": 3, "error_count": 1})
        usage = monitoring_server._collect_token_usage()

        assert usage["total_tokens"] == 0
        assert usage["prompt_tokens"] == 0
        assert usage["request_count"] == 3
        assert usage["error_count"] == 1


def _seed_state_rows(db_path: str, rows: list[tuple[str, str, str, str]]) -> None:
    """写 pipeline_state 原始行（value/value_kind 为待解码原文，含畸形输入）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT,"
            " value TEXT, value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)"
            " VALUES (?, ?, ?, ?, 'default', '2026-09-16T00:00:00Z')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestTokenUsageMalformedStateRows:
    """state 行容错：值不可解析 / 非对象形态时降级为「无用量」，不炸整次聚合。"""

    def test_unparsable_json_value_degrades_to_none(self, monkeypatch) -> None:
        """value_kind='json' 但 value 不是合法 JSON（历史脏写）→ 槽位 None，不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state_rows(
                db_path,
                [
                    ("pipe-bad", "track.llm_usage", "{not-json", "json"),
                    ("pipe-bad", "llm_model", "deepseek-v4-flash", "str"),
                    # 同库另一管道正常：脏行不得污染健康行的统计
                    (
                        "pipe-ok",
                        "track.llm_usage",
                        json.dumps(
                            {"total_input_tokens": 10, "total_output_tokens": 5, "total_tokens": 15}
                        ),
                        "json",
                    ),
                    ("pipe-ok", "llm_model", "deepseek-v4-flash", "str"),
                ],
            )
            monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

            usage = monitoring_server._collect_token_usage()

            # 健康管道照常计入；脏行按 0 贡献（同模型合并为一行）
            assert usage["total_tokens"] == 15
            assert usage["prompt_tokens"] == 10
            assert usage["completion_tokens"] == 5
            assert [r["model"] for r in usage["rows"]] == ["deepseek-v4-flash"]

    def test_non_dict_llm_usage_normalized_to_empty(self, monkeypatch) -> None:
        """track.llm_usage 是 JSON 标量（非对象）→ 视作无用量，不计入 token（574）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state_rows(
                db_path,
                [
                    ("pipe-scalar", "track.llm_usage", json.dumps(42), "json"),
                    ("pipe-scalar", "llm_model", "glm-4", "str"),
                    ("pipe-null", "track.llm_usage", json.dumps(None), "json"),
                    ("pipe-null", "llm_model", "glm-4", "str"),
                ],
            )
            monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

            usage = monitoring_server._collect_token_usage()

            assert usage["total_tokens"] == 0
            assert usage["prompt_tokens"] == 0
            # 模型行仍在（有记录就该出现在按模型表里），只是 token 列全 0
            assert [r["model"] for r in usage["rows"]] == ["glm-4"]
            assert usage["rows"][0]["total_tokens"] == 0

    def test_non_string_llm_model_falls_back_to_unrecorded_label(self, monkeypatch) -> None:
        """llm_model 非字符串（json 键存数字）→ 归「（未记录模型）」，不把数字当模型名。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state_rows(
                db_path,
                [
                    (
                        "pipe-num",
                        "track.llm_usage",
                        json.dumps(
                            {"total_input_tokens": 1, "total_output_tokens": 1, "total_tokens": 2}
                        ),
                        "json",
                    ),
                    ("pipe-num", "llm_model", json.dumps(123), "json"),
                ],
            )
            monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

            usage = monitoring_server._collect_token_usage()

            assert [r["model"] for r in usage["rows"]] == ["（未记录模型）"]
            assert usage["total_tokens"] == 2


class TestTokenUsageByTimeRangeFilters:
    """by-time 端点的时间区间参数（server.py:654-662）。

    契约：since / until 为闭区间显式日期且优先于 days；days 只在两者都缺省时
    生效（days=0 表示不限）。断言落在返回的按日行集合上。
    """

    def _seed_traces(self, tmp_path: Path) -> str:
        db_path = str(tmp_path / "kernel.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE traces (patch_data TEXT, created_at TEXT)")
            rows = [
                ({"llm_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}, "2026-09-01T01:00:00Z"),
                ({"llm_usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}}, "2026-09-05T02:00:00Z"),
                ({"llm_usage": {"input_tokens": 4, "output_tokens": 4, "total_tokens": 8}}, "2026-09-10T03:00:00Z"),
                # 无 llm_usage 的轨迹不得进入按日统计
                ({"iteration": 1}, "2026-09-06T04:00:00Z"),
            ]
            conn.executemany(
                "INSERT INTO traces (patch_data, created_at) VALUES (?, ?)",
                [(json.dumps(p), ts) for p, ts in rows],
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_since_bound_is_inclusive(self, monkeypatch, tmp_path) -> None:
        """since 闭区间下界：等于下界当天在内，早于它的整日被排除（654-656）。"""
        db_path = self._seed_traces(tmp_path)
        monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

        result = monitoring_server._collect_token_usage_by_time(since="2026-09-05")

        assert [r["date"] for r in result["rows"]] == ["2026-09-10", "2026-09-05"]
        assert result["total"] == 2
        assert [r["date"] for r in result["rows"]] == result["labels"]

    def test_until_bound_is_inclusive(self, monkeypatch, tmp_path) -> None:
        """until 闭区间上界：等于上界当天在内，晚于它的整日被排除（658-659）。"""
        db_path = self._seed_traces(tmp_path)
        monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

        result = monitoring_server._collect_token_usage_by_time(until="2026-09-05")

        assert [r["date"] for r in result["rows"]] == ["2026-09-05", "2026-09-01"]

    def test_since_and_until_combined_define_window(self, monkeypatch, tmp_path) -> None:
        """since + until 叠加为闭区间窗口（两侧条件同时下推 SQL）。"""
        db_path = self._seed_traces(tmp_path)
        monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

        result = monitoring_server._collect_token_usage_by_time(
            since="2026-09-05", until="2026-09-05"
        )

        assert [r["date"] for r in result["rows"]] == ["2026-09-05"]
        assert result["rows"][0]["input_tokens"] == 2

    def test_days_window_applied_only_without_explicit_bounds(self, monkeypatch, tmp_path) -> None:
        """days>0 且未给显式区间时按最近 N 天裁剪（661-662）；窗口内数据保留。"""
        db_path = self._seed_traces(tmp_path)
        monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)
        # 固定"今天"：days 用 SQL date('now') 计算，注入固定日期避免随运行日漂移
        monkeypatch.setattr(monitoring_server, "_today_utc", lambda: "2026-09-10", raising=False)

        # 用极大的 days 保证全部历史命中（性质：窗口宽松则不减行）
        wide = monitoring_server._collect_token_usage_by_time(days=36500)

        assert {r["date"] for r in wide["rows"]} == {"2026-09-01", "2026-09-05", "2026-09-10"}
        assert wide["total"] == 3

        # days=0 表示不限（与 days>0 分支区分）
        unlimited = monitoring_server._collect_token_usage_by_time(days=0)
        assert {r["date"] for r in unlimited["rows"]} == {r["date"] for r in wide["rows"]}

    def test_explicit_bounds_take_precedence_over_days(self, monkeypatch, tmp_path) -> None:
        """同时给 since 与 days → 只按 since 过滤（days 不生效）。"""
        db_path = self._seed_traces(tmp_path)
        monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: db_path)

        result = monitoring_server._collect_token_usage_by_time(since="2026-09-10", days=1)

        assert [r["date"] for r in result["rows"]] == ["2026-09-10"]
