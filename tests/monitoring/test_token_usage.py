# @feature: FP-0.2.可观测性 可观测性基座 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring /ext/monitoring/token-usage 从 pipeline_state 聚合测试。

累计口径真值 = state 的 track.llm_usage（track 插件每轮累加、逐管道自持）：
- token-usage（本文件）读 state 累计——跨运行累计、跨成员热重载保留；
- by-time 读 traces 逐轮明细（时间维度只有 traces 有，见 test_monitoring.py）。

历史（2026-08-18 G4）：token-usage 原恒 0（PerformanceMonitor 不持 token 计数）
→ 曾改为从 traces 聚合；2026-09-15 裁定累计口径归 state（traces 是明细副产物，
两源在当日新产生管道上逐值一致，历史差异源于打包测试期数据 churn）。

调用次数（顶层 request_count 与按模型行 requests）真值 = traces llm_usage
逐轮明细 COUNT（GROUP BY 模型；P-147 修复：原初始化 0 后无累加恒假零）；
DB 缺失/查询失败降级次数 0（token=0 同策略），契约不破坏。顶层
active_requests/error_count/total_response_time 无真值来源已退役（P-149 同刀）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "plugins" / "shared"), str(_REPO_ROOT / "plugins" / "shared" / "monitoring")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SERVER_PY = _REPO_ROOT / "plugins" / "shared" / "monitoring" / "server.py"
_spec = importlib.util.spec_from_file_location("monitoring_server_under_test", _SERVER_PY)
assert _spec is not None, "server.py 模块装载失败"
assert _spec.loader is not None, "server.py 模块装载失败"
monitoring_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_server)

import kernel_reads  # noqa: E402 — db-read provider 缝（server 同目录模块）
from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — 共享测试基建

# db-read 契约替身身份映射（C2 刀2 后读面按身份租户收窄）
_TOK_MAP = {"tok-a": "tenant-a"}


def _install_db_read(db_path: str | Path) -> FakeDbRead:
    """db-read provider 注入（kernel_reads 装配缝；用例后清理见 autoclean 夹具）。"""
    fake = FakeDbRead(Path(db_path), _TOK_MAP)
    kernel_reads.set_provider("db-read", fake.provider)
    return fake


def _run(coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """跑聚合协程到完成（聚合器返回 dict[str, Any]，给 mypy 可索引的返回类型）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_kernel_reads_providers():
    yield
    kernel_reads.reset_providers()


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
            # 两管道同模型：累计相加（1500 + 1000）；缓存命中相加（900 + 200）
            (
                "pipe-a",
                "track.llm_usage",
                json.dumps({"total_input_tokens": 1200, "total_output_tokens": 300, "total_tokens": 1500, "total_cached_tokens": 900}),
                "json",
            ),
            ("pipe-a", "llm_model", "deepseek-v4-flash", "str"),
            (
                "pipe-b",
                "track.llm_usage",
                json.dumps({"total_input_tokens": 800, "total_output_tokens": 200, "total_tokens": 1000, "total_cached_tokens": 200}),
                "json",
            ),
            ("pipe-b", "llm_model", "deepseek-v4-flash", "str"),
            # 与用量无关的键不得进入统计
            ("pipe-c", "task.status", "completed", "str"),
        ]
        for pid, key, val, kind in rows:
            conn.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)"
                " VALUES (?, ?, ?, ?, 'tenant-a', '2026-09-15T00:00:00Z')",
                (pid, key, val, kind),
            )
        conn.commit()
    finally:
        conn.close()


class TestTokenUsageFromState:
    """_collect_token_usage 聚合行为（累计真值源 = state）。"""

    def test_aggregates_state_llm_usage(self, monkeypatch) -> None:
        """DB 有 track.llm_usage：跨管道累加（2000/500/2500）。

        缓存命中同源累加（cached 900+200=1100，missed 300+600=900，
        命中率 = 1100/2000 = 55.0%）。本库无 traces 表：调用次数降级真零，
        退役死字段不在响应体。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state(db_path)
            _install_db_read(db_path)

            usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

            assert usage["prompt_tokens"] == 2000
            assert usage["completion_tokens"] == 500
            assert usage["total_tokens"] == 2500
            assert usage["cached_tokens"] == 1100
            assert usage["missed_tokens"] == 900
            assert usage["cache_hit_pct"] == 55.0
            # 性质断言：cached ≤ input；命中率 = round(cached/input*100, 1)
            assert usage["cached_tokens"] <= usage["prompt_tokens"]
            assert usage["cache_hit_pct"] == round(
                usage["cached_tokens"] / usage["prompt_tokens"] * 100, 1
            )
            # 无 traces 表：次数降级真零；死字段退役（P-149）不在响应体
            assert usage["request_count"] == 0
            assert "active_requests" not in usage

    def test_groups_by_model_from_state(self, monkeypatch) -> None:
        """按模型聚合：同模型多管道相加为一行（state 无 provider 键，图标按模型名）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_state(db_path)
            _install_db_read(db_path)

            usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))
            rows = {r["model"]: r for r in usage["rows"]}

            assert set(rows) == {"deepseek-v4-flash"}
            assert rows["deepseek-v4-flash"]["total_tokens"] == 2500
            assert rows["deepseek-v4-flash"]["icon"] == "🐋"
            # 缓存命中逐行口径：同模型两管道 cached/missed 相加，pct 按行累计值算
            assert rows["deepseek-v4-flash"]["cached_tokens"] == 1100
            assert rows["deepseek-v4-flash"]["missed_tokens"] == 900
            assert rows["deepseek-v4-flash"]["cache_hit_pct"] == 55.0
            # 性质断言：总量卡 = 各模型行之和（同一查询源，不许两套账）
            assert usage["total_tokens"] == sum(r["total_tokens"] for r in usage["rows"])
            assert usage["cached_tokens"] == sum(r["cached_tokens"] for r in usage["rows"])

    def test_db_missing_degrades_to_zero(self, monkeypatch) -> None:
        """DB 不存在：token 与调用次数均降级 0，死字段不在响应体，不抛异常。"""
        _install_db_read("Z:/nonexistent/kernel.db")

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

        assert usage["total_tokens"] == 0
        assert usage["prompt_tokens"] == 0
        assert usage["request_count"] == 0
        # P-149 退役死字段不在响应体（进程内计数器无生产写入口，恒 0 假数据）
        assert "active_requests" not in usage
        assert "error_count" not in usage
        assert "total_response_time" not in usage
        # 降级路径新字段同步归 0（缓存命中卡在无 DB 时不显示脏值）
        assert usage["cached_tokens"] == 0
        assert usage["missed_tokens"] == 0
        assert usage["cache_hit_pct"] == 0.0


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
            " VALUES (?, ?, ?, ?, 'tenant-a', '2026-09-16T00:00:00Z')",
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
            _install_db_read(db_path)

            usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

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
            _install_db_read(db_path)

            usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

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
            _install_db_read(db_path)

            usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

            assert [r["model"] for r in usage["rows"]] == ["（未记录模型）"]
            assert usage["total_tokens"] == 2


class TestTokenUsageRequestsTruth:
    """调用次数真值（P-147）：按模型行 requests 与顶层 request_count。

    真值源 = traces llm_usage 逐轮明细 COUNT（GROUP BY 模型，与 by-time 同款
    查询模式）；state 累计键不含次数。两组不同分布输入防拟合 + 性质断言
    （顶层 = 各行之和 = llm_usage 轨迹总数）。
    """

    def _seed_state_and_traces(
        self,
        db_path: str,
        state_rows: list[tuple[str, str, str, str]],
        traces: list[tuple[dict[str, Any], str]],
    ) -> None:
        """写 pipeline_state（token 累计面）+ traces（逐轮明细面），均 tenant-a。"""
        _seed_state_rows(db_path, state_rows)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE traces (patch_data TEXT, created_at TEXT, tenant_id TEXT)")
            conn.executemany(
                "INSERT INTO traces (patch_data, created_at, tenant_id) VALUES (?, ?, 'tenant-a')",
                [(json.dumps(p), ts) for p, ts in traces],
            )
            conn.commit()
        finally:
            conn.close()

    _STATE_TWO_MODELS = [
        (
            "pipe-a",
            "track.llm_usage",
            json.dumps({"total_input_tokens": 100, "total_output_tokens": 10, "total_tokens": 110}),
            "json",
        ),
        ("pipe-a", "llm_model", "deepseek-v4-flash", "str"),
        (
            "pipe-b",
            "track.llm_usage",
            json.dumps({"total_input_tokens": 50, "total_output_tokens": 5, "total_tokens": 55}),
            "json",
        ),
        ("pipe-b", "llm_model", "glm-5", "str"),
    ]

    def _trace(self, model: str | None) -> dict[str, Any]:
        usage: dict[str, Any] = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        if model is not None:
            usage["model"] = model
        return {"llm_usage": usage}

    def test_requests_per_model_counted_from_traces(self, monkeypatch, tmp_path) -> None:
        """分布 A：deepseek×3 / glm×1 / 缺 model 键×1 → 逐行真 COUNT。

        缺 model 键的轨迹归「（未记录模型）」行（与 state 无 llm_model 同容错
        口径）；无 llm_usage 的轨迹不进计数。
        """
        db_path = str(tmp_path / "kernel.db")
        self._seed_state_and_traces(
            db_path,
            self._STATE_TWO_MODELS,
            [
                (self._trace("deepseek-v4-flash"), "2026-09-01T01:00:00Z"),
                (self._trace("deepseek-v4-flash"), "2026-09-01T02:00:00Z"),
                (self._trace("deepseek-v4-flash"), "2026-09-02T01:00:00Z"),
                (self._trace("glm-5"), "2026-09-02T02:00:00Z"),
                (self._trace(None), "2026-09-02T03:00:00Z"),
                # 无 llm_usage 的轨迹（工具轮等）不得计入调用次数
                ({"iteration": 1}, "2026-09-02T04:00:00Z"),
            ],
        )
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))
        rows = {r["model"]: r for r in usage["rows"]}

        assert rows["deepseek-v4-flash"]["requests"] == 3
        assert rows["glm-5"]["requests"] == 1
        assert rows["（未记录模型）"]["requests"] == 1
        # token 面仍归 state 累计（真值源分工：累计=state，逐轮=traces）
        assert rows["deepseek-v4-flash"]["total_tokens"] == 110.0

    def test_requests_property_sum_equals_trace_total(self, monkeypatch, tmp_path) -> None:
        """分布 B：deepseek×2 / glm×2 / kimi×3 → 顶层 = 各行之和 = 轨迹总数。

        kimi 只有 traces 无 state 行：setdefault 补行出表（requests 真、
        token 面按 0——累计 token 真值源仍是 state）。
        """
        db_path = str(tmp_path / "kernel.db")
        traces = [
            (self._trace("deepseek-v4-flash"), "2026-09-03T01:00:00Z"),
            (self._trace("deepseek-v4-flash"), "2026-09-03T02:00:00Z"),
            (self._trace("glm-5"), "2026-09-03T03:00:00Z"),
            (self._trace("glm-5"), "2026-09-03T04:00:00Z"),
            (self._trace("kimi-k3"), "2026-09-03T05:00:00Z"),
            (self._trace("kimi-k3"), "2026-09-03T06:00:00Z"),
            (self._trace("kimi-k3"), "2026-09-03T07:00:00Z"),
        ]
        self._seed_state_and_traces(db_path, self._STATE_TWO_MODELS, traces)
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))
        rows = {r["model"]: r for r in usage["rows"]}

        assert rows["deepseek-v4-flash"]["requests"] == 2
        assert rows["glm-5"]["requests"] == 2
        assert rows["kimi-k3"]["requests"] == 3
        assert rows["kimi-k3"]["total_tokens"] == 0.0
        # 性质断言：顶层调用次数 = 各行之和 = 种入的 llm_usage 轨迹数（7）
        assert usage["request_count"] == sum(r["requests"] for r in usage["rows"])
        assert usage["request_count"] == 7

    def test_empty_traces_yield_true_zero_requests(self, monkeypatch, tmp_path) -> None:
        """traces 表存在但空：次数 = 0 是真零（有表有行集），非假零语义。"""
        db_path = str(tmp_path / "kernel.db")
        self._seed_state_and_traces(db_path, self._STATE_TWO_MODELS, [])
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

        assert usage["rows"], "state 面的模型行仍在"
        assert all(r["requests"] == 0 for r in usage["rows"])
        assert usage["request_count"] == 0

    def test_traces_unavailable_degrades_requests_only(self, monkeypatch, tmp_path) -> None:
        """库无 traces 表：次数降级 0，token 行不受牵连（独立降级面）。"""
        db_path = str(tmp_path / "kernel.db")
        _seed_state_rows(db_path, self._STATE_TWO_MODELS)
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

        assert usage["total_tokens"] == 165.0
        assert all(r["requests"] == 0 for r in usage["rows"])
        assert usage["request_count"] == 0

    def test_dead_counters_retired_from_response(self, monkeypatch, tmp_path) -> None:
        """P-149：进程内死计数器不再进响应体；request_count 只认 traces 真值。"""
        db_path = str(tmp_path / "kernel.db")
        self._seed_state_and_traces(
            db_path,
            self._STATE_TWO_MODELS,
            [(self._trace("deepseek-v4-flash"), "2026-09-04T01:00:00Z")],
        )
        _install_db_read(db_path)
        # 死计数器灌陈旧值：响应体必须无视之（原实现原样透传 = 恒 0/脏值假数据）
        monitor = monitoring_server._ensure_monitor()
        monitor._llm_stats.update({
            "request_count": 7,
            "active_requests": 1,
            "error_count": 2,
            "total_response_time": 9.5,
        })

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

        assert "active_requests" not in usage
        assert "error_count" not in usage
        assert "total_response_time" not in usage
        assert usage["request_count"] == 1  # traces 真值，非死计数器的 7


class TestTokenUsageCacheHit:
    """缓存命中（prompt cache）逐行与汇总口径。

    命中率是行级派生值：pct = round(cached/input*100, 1)，input≤0 时 0.0
    （不除零）；missed 按各管道 input−cached 夹非负累加。
    """

    def test_two_models_with_distinct_hit_ratios(self, monkeypatch, tmp_path) -> None:
        """两模型命中率有区分度（80% vs 10%）：逐行 cached/missed/pct 各自正确。

        高命中走 deepseek、低命中走 glm，验证「按模型聚合」不把两行命中率
        混算；顶层 pct 用总 cached/总 input 派生（非各行 pct 相加）。
        """
        db_path = str(tmp_path / "kernel.db")
        _seed_state_rows(
            db_path,
            [
                (
                    "pipe-hi",
                    "track.llm_usage",
                    json.dumps(
                        {
                            "total_input_tokens": 1000,
                            "total_output_tokens": 100,
                            "total_tokens": 1100,
                            "total_cached_tokens": 800,
                        }
                    ),
                    "json",
                ),
                ("pipe-hi", "llm_model", "deepseek-v4-flash", "str"),
                (
                    "pipe-lo",
                    "track.llm_usage",
                    json.dumps(
                        {
                            "total_input_tokens": 400,
                            "total_output_tokens": 50,
                            "total_tokens": 450,
                            "total_cached_tokens": 40,
                        }
                    ),
                    "json",
                ),
                ("pipe-lo", "llm_model", "glm-5", "str"),
            ],
        )
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))
        rows = {r["model"]: r for r in usage["rows"]}

        assert rows["deepseek-v4-flash"]["cached_tokens"] == 800
        assert rows["deepseek-v4-flash"]["missed_tokens"] == 200
        assert rows["deepseek-v4-flash"]["cache_hit_pct"] == 80.0
        assert rows["glm-5"]["cached_tokens"] == 40
        assert rows["glm-5"]["missed_tokens"] == 360
        assert rows["glm-5"]["cache_hit_pct"] == 10.0
        # 性质断言：cached ≤ input 且 pct = round(cached/input*100, 1) 逐行成立
        for row in usage["rows"]:
            assert 0 <= row["cached_tokens"] <= row["input_tokens"]
            assert row["cache_hit_pct"] == round(
                row["cached_tokens"] / row["input_tokens"] * 100, 1
            )
        # 顶层汇总 = 各行之和；pct 按总量派生（840/1400 = 60.0，≠各行 pct 均值）
        assert usage["cached_tokens"] == 840
        assert usage["missed_tokens"] == 560
        assert usage["cache_hit_pct"] == 60.0
        # widget 消费形：表格列尾两列为缓存命中，图表序列含「缓存命中 Tokens」
        assert [c["key"] for c in usage["columns"]][-2:] == ["cached_tokens", "cache_hit_pct"]
        assert usage["datasets"][-1] == {
            "label": "缓存命中 Tokens",
            "data": [r["cached_tokens"] for r in usage["rows"]],
        }

    def test_zero_input_row_pct_is_zero(self, monkeypatch, tmp_path) -> None:
        """input=0 行：pct=0.0 不除零；缺 cached 字段（早期管道）按 0 兜底。"""
        db_path = str(tmp_path / "kernel.db")
        _seed_state_rows(
            db_path,
            [
                (
                    "pipe-zero",
                    "track.llm_usage",
                    json.dumps({"total_input_tokens": 0, "total_output_tokens": 5, "total_tokens": 5}),
                    "json",
                ),
                ("pipe-zero", "llm_model", "glm-5", "str"),
            ],
        )
        _install_db_read(db_path)

        usage = _run(monitoring_server._collect_token_usage("tenant-a", "tok-a"))

        row = usage["rows"][0]
        assert row["input_tokens"] == 0
        assert row["cached_tokens"] == 0
        assert row["missed_tokens"] == 0
        assert row["cache_hit_pct"] == 0.0
        # 顶层 prompt=0：pct 同样走 0.0 分支，不抛 ZeroDivisionError
        assert usage["cache_hit_pct"] == 0.0


class TestTokenUsageByTimeCacheHit:
    """by-time 按日缓存命中聚合：traces 逐轮 cached_tokens SUM 到日粒度。

    有区分度三组：高命中日（两轮 SUM）、input=0 日（pct 不除零）、
    无 cached 字段的早期轨迹日（按 0 计，missed=input）。
    """

    def _seed_traces_with_cache(self, tmp_path: Path) -> str:
        db_path = str(tmp_path / "kernel.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE traces (patch_data TEXT, created_at TEXT, tenant_id TEXT)")
            rows = [
                ({"llm_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cached_tokens": 80}}, "2026-09-01T08:00:00Z"),
                ({"llm_usage": {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220, "cached_tokens": 100}}, "2026-09-01T09:00:00Z"),
                ({"llm_usage": {"input_tokens": 0, "output_tokens": 2, "total_tokens": 2}}, "2026-09-02T10:00:00Z"),
                # 早期轨迹无 cached 字段：json_extract 为 NULL → COALESCE 按 0
                ({"llm_usage": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}}, "2026-09-03T11:00:00Z"),
            ]
            conn.executemany(
                "INSERT INTO traces (patch_data, created_at, tenant_id) VALUES (?, ?, 'tenant-a')",
                [(json.dumps(p), ts) for p, ts in rows],
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_daily_cache_hit_aggregation(self, monkeypatch, tmp_path) -> None:
        db_path = self._seed_traces_with_cache(tmp_path)
        _install_db_read(db_path)

        result = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a"))
        by_day = {r["date"]: r for r in result["rows"]}

        # 高命中日：两轮 SUM（input 300 / cached 180 / missed 120 / 60.0%）
        assert by_day["2026-09-01"]["input_tokens"] == 300
        assert by_day["2026-09-01"]["cached_tokens"] == 180
        assert by_day["2026-09-01"]["missed_tokens"] == 120
        assert by_day["2026-09-01"]["cache_hit_pct"] == 60.0
        # input=0 日：pct=0.0 不除零
        assert by_day["2026-09-02"]["input_tokens"] == 0
        assert by_day["2026-09-02"]["cache_hit_pct"] == 0.0
        # 早期轨迹日：cached 按 0 计，missed = input
        assert by_day["2026-09-03"]["cached_tokens"] == 0
        assert by_day["2026-09-03"]["missed_tokens"] == 50
        assert by_day["2026-09-03"]["cache_hit_pct"] == 0.0
        # 性质断言：cached ≤ input 且 pct 公式逐日成立；图表序列与 rows 同源同值
        for row in result["rows"]:
            assert 0 <= row["cached_tokens"] <= row["input_tokens"]
            if row["input_tokens"] > 0:
                assert row["cache_hit_pct"] == round(
                    row["cached_tokens"] / row["input_tokens"] * 100, 1
                )
            else:
                assert row["cache_hit_pct"] == 0.0
        assert [c["key"] for c in result["columns"]][-2:] == ["cached_tokens", "cache_hit_pct"]
        assert result["datasets"][-1] == {
            "label": "缓存命中 Tokens",
            "data": [r["cached_tokens"] for r in result["rows"]],
        }


class TestTokenUsageByTimeRangeFilters:
    """by-time 端点的时间区间参数（server.py:654-662）。

    契约：since / until 为闭区间显式日期且优先于 days；days 只在两者都缺省时
    生效（days=0 表示不限）。断言落在返回的按日行集合上。
    """

    def _seed_traces(self, tmp_path: Path) -> str:
        db_path = str(tmp_path / "kernel.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE traces (patch_data TEXT, created_at TEXT, tenant_id TEXT)")
            rows = [
                ({"llm_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}, "2026-09-01T01:00:00Z"),
                ({"llm_usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}}, "2026-09-05T02:00:00Z"),
                ({"llm_usage": {"input_tokens": 4, "output_tokens": 4, "total_tokens": 8}}, "2026-09-10T03:00:00Z"),
                # 无 llm_usage 的轨迹不得进入按日统计
                ({"iteration": 1}, "2026-09-06T04:00:00Z"),
            ]
            conn.executemany(
                "INSERT INTO traces (patch_data, created_at, tenant_id) VALUES (?, ?, 'tenant-a')",
                [(json.dumps(p), ts) for p, ts in rows],
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_since_bound_is_inclusive(self, monkeypatch, tmp_path) -> None:
        """since 闭区间下界：等于下界当天在内，早于它的整日被排除（654-656）。"""
        db_path = self._seed_traces(tmp_path)
        _install_db_read(db_path)

        result = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a", since="2026-09-05"))

        assert [r["date"] for r in result["rows"]] == ["2026-09-10", "2026-09-05"]
        assert result["total"] == 2
        assert [r["date"] for r in result["rows"]] == result["labels"]

    def test_until_bound_is_inclusive(self, monkeypatch, tmp_path) -> None:
        """until 闭区间上界：等于上界当天在内，晚于它的整日被排除（658-659）。"""
        db_path = self._seed_traces(tmp_path)
        _install_db_read(db_path)

        result = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a", until="2026-09-05"))

        assert [r["date"] for r in result["rows"]] == ["2026-09-05", "2026-09-01"]

    def test_since_and_until_combined_define_window(self, monkeypatch, tmp_path) -> None:
        """since + until 叠加为闭区间窗口（两侧条件同时下推 SQL）。"""
        db_path = self._seed_traces(tmp_path)
        _install_db_read(db_path)

        result = _run(monitoring_server._collect_token_usage_by_time(
            "tenant-a", "tok-a", since="2026-09-05", until="2026-09-05"
        ))

        assert [r["date"] for r in result["rows"]] == ["2026-09-05"]
        assert result["rows"][0]["input_tokens"] == 2

    def test_days_window_applied_only_without_explicit_bounds(self, monkeypatch, tmp_path) -> None:
        """days>0 且未给显式区间时按最近 N 天裁剪（661-662）；窗口内数据保留。"""
        db_path = self._seed_traces(tmp_path)
        _install_db_read(db_path)
        # 固定"今天"：days 用 SQL date('now') 计算，注入固定日期避免随运行日漂移
        monkeypatch.setattr(monitoring_server, "_today_utc", lambda: "2026-09-10", raising=False)

        # 用极大的 days 保证全部历史命中（性质：窗口宽松则不减行）
        wide = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a", days=36500))

        assert {r["date"] for r in wide["rows"]} == {"2026-09-01", "2026-09-05", "2026-09-10"}
        assert wide["total"] == 3

        # days=0 表示不限（与 days>0 分支区分）
        unlimited = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a", days=0))
        assert {r["date"] for r in unlimited["rows"]} == {r["date"] for r in wide["rows"]}

    def test_explicit_bounds_take_precedence_over_days(self, monkeypatch, tmp_path) -> None:
        """同时给 since 与 days → 只按 since 过滤（days 不生效）。"""
        db_path = self._seed_traces(tmp_path)
        _install_db_read(db_path)

        result = _run(monitoring_server._collect_token_usage_by_time("tenant-a", "tok-a", since="2026-09-10", days=1))

        assert [r["date"] for r in result["rows"]] == ["2026-09-10"]
