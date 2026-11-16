# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""active_requests 指标 end 侧行为测试 —— F-MON-1（start 工具已移除）。

意图（WHY）：
- monitoring.record_llm_request_start 工具因 plugin.json 未声明（声明即注册，
  永不可达）已从 server.py 删除（2026-08-25 全仓扫描 P0 断链修复）。
- 保留的契约：record_llm_request（end 语义）不调 start 时 active_requests
  不得变负，下限为 0（存量调用方只调 end）。

测试通过 server.py 公开工具函数（@plugin.tool 装饰后原样返回，可直接 await 调用）
+ _collect_token_usage 收集器（前端 /ext/monitoring/token-usage 端点数据源）验证。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[4]
for _p in (str(_REPO_ROOT / "plugins" / "shared"),):
    if _p not in __import__("sys").path:
        __import__("sys").path.insert(0, _p)

from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — db-read 契约替身

_TOK_MAP = {"tok-a": "tenant-a"}


def _install_db_read(server, db_path) -> FakeDbRead:
    """db-read provider 注入 server.kernel_reads 装配缝（C2 刀2 读面通道）。"""
    fake = FakeDbRead(db_path, _TOK_MAP)
    server.kernel_reads.set_provider("db-read", fake.provider)
    return fake


def _drun(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# fixtures
# ============================================================


@pytest.fixture
def server_module():
    """导入 server 模块（平铺 import 由 conftest 注入 sys.path 解析）。"""
    import server

    return server


@pytest.fixture
def fresh_monitor(server_module):
    """每个测试用全新 PerformanceMonitor 替换 server 模块级单例，隔离计数状态。

    active_requests 是累加状态，若跨测试共享会互相污染；本 fixture 保证每个测试
    从 active_requests=0 起步，并在结束后还原原单例。
    """
    from performance_monitor import PerformanceMonitor

    old = server_module._monitor
    server_module._monitor = PerformanceMonitor()
    yield server_module._monitor
    server_module._monitor = old


def _active(monitor) -> int:
    """读 monitor 当前 active_requests。"""
    return monitor._llm_stats["active_requests"]


# ============================================================
# _collect_token_usage：traces 聚合与降级
# ============================================================


class TestCollectTokenUsage:
    """/ext/monitoring/token-usage 数据源：traces 聚合真值 + 无 DB 降级 0。"""

    def test_no_db_degrades_to_zero(self, server_module, tmp_path) -> None:
        """db-read 契约失败（替身指向缺失库）→ token 三项降级 0（不抛错）。"""
        _install_db_read(server_module, tmp_path / "absent.db")
        result = _drun(server_module._collect_token_usage("tenant-a", "tok-a"))
        assert result["total_tokens"] == 0
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    def test_aggregates_llm_usage_from_state(
        self, server_module, monkeypatch, tmp_path
    ) -> None:
        """pipeline_state 含多条累计用量 → 跨管道求和（区分度：非零且逐项对应）。

        累计口径的真值源是 pipeline_state（每管道自持累计），非 traces 逐轮轨迹
        ——按模型汇总须先按管道取累计再跨管道相加（ADR 2026-09-15 §决策2）。
        """
        import json
        import sqlite3

        db_path = tmp_path / "kernel.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
            " value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
        )
        # state 值域标量化（ADR 2026-09-18）：json 键存 JSON 文本，标量按 str 原样存
        rows = [
            ("p1", "track.llm_usage", json.dumps({"total_input_tokens": 7, "total_output_tokens": 3, "total_tokens": 10}), "json"),
            ("p2", "track.llm_usage", json.dumps({"total_input_tokens": 5, "total_output_tokens": 5, "total_tokens": 10}), "json"),
            ("p3", "context_window", "32000", "str"),  # 非用量键不计入
        ]
        conn.executemany(
            "INSERT INTO pipeline_state VALUES (?, ?, ?, ?, 'tenant-a', '2026-09-15T00:00:00Z')",
            rows,
        )
        conn.commit()
        conn.close()
        _install_db_read(server_module, db_path)
        result = _drun(server_module._collect_token_usage("tenant-a", "tok-a"))
        assert result["prompt_tokens"] == 12
        assert result["completion_tokens"] == 8
        assert result["total_tokens"] == 20

    def test_token_usage_series_shape_same_source_as_rows(
        self, server_module, monkeypatch, tmp_path
    ) -> None:
        """chart 消费面：labels/datasets 与 rows 同源同值（widget 化图表形状）。"""
        import json
        import sqlite3

        db_path = tmp_path / "kernel.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
            " value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
        )
        rows = [
            ("p1", "track.llm_usage", json.dumps({"total_input_tokens": 7, "total_output_tokens": 3, "total_tokens": 10}), "json"),
            ("p1", "llm_model", "m-a", "str"),
            ("p2", "track.llm_usage", json.dumps({"total_input_tokens": 5, "total_output_tokens": 5, "total_tokens": 10}), "json"),
            ("p2", "llm_model", "m-b", "str"),
        ]
        conn.executemany(
            "INSERT INTO pipeline_state VALUES (?, ?, ?, ?, 'tenant-a', '2026-09-15T00:00:00Z')",
            rows,
        )
        conn.commit()
        conn.close()
        _install_db_read(server_module, db_path)
        result = _drun(server_module._collect_token_usage("tenant-a", "tok-a"))
        assert result["labels"] == ["m-a", "m-b"]
        assert [d["label"] for d in result["datasets"]] == [
            "输入 Tokens",
            "输出 Tokens",
            "缓存命中 Tokens",
        ]
        assert result["datasets"][0]["data"] == [7.0, 5.0]
        assert result["datasets"][1]["data"] == [3.0, 5.0]
        # 早期管道缺 cached 字段：序列按 0 兜底（与「未记录模型」同容错口径）
        assert result["datasets"][2]["data"] == [0.0, 0.0]
        # 同源同值：datasets 数据 = rows 各列（防两套真值漂移）
        assert result["datasets"][0]["data"] == [r["input_tokens"] for r in result["rows"]]
        assert result["datasets"][2]["data"] == [r["cached_tokens"] for r in result["rows"]]


class TestTokenUsageByTimeSeries:
    """/ext/monitoring/token-usage/by-time：表格 {columns,rows} 与图表 {labels,datasets} 两形态。"""

    def _seed_db(self, tmp_path):
        import json
        import sqlite3

        db_path = tmp_path / "kernel.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE traces (patch_data TEXT, created_at TEXT, tenant_id TEXT)")
        data = [
            ({"llm_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}, "2026-09-09T10:00:00Z"),
            ({"llm_usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}, "2026-09-10T11:00:00Z"),
        ]
        conn.executemany(
            "INSERT INTO traces (patch_data, created_at, tenant_id) VALUES (?, ?, 'tenant-a')",
            [(json.dumps(r), ts) for r, ts in data],
        )
        conn.commit()
        conn.close()
        return db_path

    def test_by_time_series_same_source_as_rows(self, server_module, monkeypatch, tmp_path) -> None:
        db_path = self._seed_db(tmp_path)
        _install_db_read(server_module, db_path)
        result = _drun(server_module._collect_token_usage_by_time("tenant-a", "tok-a"))
        by_day = {r["date"]: r for r in result["rows"]}
        assert set(result["labels"]) == set(by_day)
        # 同源同值：每日 datasets 输入 = rows 输入
        inp = dict(zip(result["labels"], result["datasets"][0]["data"]))
        for day, row in by_day.items():
            assert inp[day] == row["input_tokens"]

    def test_by_time_no_db_degrades_empty_series(self, server_module, tmp_path) -> None:
        _install_db_read(server_module, tmp_path / "absent.db")
        result = _drun(server_module._collect_token_usage_by_time("tenant-a", "tok-a"))
        assert result["rows"] == []
        assert result["labels"] == []
        assert result["datasets"][0]["data"] == []
