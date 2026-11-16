# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""cost_control 插件业务面测试：工具函数 + HTTP 业务路由 + traces 聚合降级。

既有 test_config_http.py 覆盖 YAML 配置读写；本文件补齐：
1. 六个工具函数（check_budget 三态/record_usage 告警序列化/get_status/
   get_statistics/reset_task/reset_session）；
2. http_handle 业务路由五支（status/statistics/config/report/reset）与
   503 未初始化守卫；
3. _reshape_usage_statistics / _flatten_cost_config / _build_cost_report
   形状契约（插件内形状 → 前端期望形状）；
4. _trace_stats_dict 聚合（今日/本月/按模型）与 sqlite 故障降级全 0。

BudgetManager 以最小替身注入模块级 _budget_manager（外部服务依赖）；
traces 聚合走真 sqlite 临时库 + 替身行源。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "cost_control"


server_mod: Any = None


def _load_server() -> Any:
    global server_mod
    spec = importlib.util.spec_from_file_location(
        "cost_control_business_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cost_control_business_test_server"] = mod
    spec.loader.exec_module(mod)
    server_mod = mod
    return mod


@pytest.fixture
def server() -> Any:
    mod = _load_server()
    yield mod
    # 卸载清理：复位模块级单例，防跨测试泄漏
    mod._budget_manager = None



class _SqliteDbRead:
    """db-read.query 独立替身：只读 SELECT 落真 sqlite（AGENTOS_DB_PATH）。

    信封按 db_read.unwrap_db_read_envelope 契约（{status:200, body:{rows}}）；
    内核侧租户收窄不参与——测试自建单租户行（生产 GROUP BY 租户列 + 契约
    包装收窄，71dca0fff 纪律在内核侧另测）。"""

    def get_capability(self, name: str) -> "_SqliteDbRead":
        assert name == "db-read", name
        return self

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import os

        assert method == "query"
        conn = sqlite3.connect(os.environ["AGENTOS_DB_PATH"])
        try:
            rows = [list(r) for r in conn.execute(params["sql"]).fetchall()]
        finally:
            conn.close()
        return {"status": 200, "body": {"columns": [], "rows": rows}}


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


class _FakeBudgetManager:
    """BudgetManager 最小替身：记录调用、可编程异常与返回。"""

    def __init__(self) -> None:
        self.check_result: bool = True
        self.check_exc: Exception | None = None
        self.record_alert: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._global_daily_usage = 11
        self._global_monthly_usage = 22
        self._task_usage = {"t1": 1}
        self._session_usage = {"s1": 2}
        self._usage_records = [object()]

    async def check_budget(self, **kw: Any) -> bool:
        self.calls.append(("check_budget", kw))
        if self.check_exc:
            raise self.check_exc
        return self.check_result

    async def record_usage(self, **kw: Any) -> Any:
        self.calls.append(("record_usage", kw))
        return self.record_alert

    def get_budget_status(self, **kw: Any) -> Any:
        self.calls.append(("get_budget_status", kw))
        from budget_manager import BudgetAlertLevel  # noqa: PLC0415 — server bootstrap 后可导入

        return server_mod.BudgetStatus(
            scope="global",
            scope_id=None,
            limit=1000,
            used=250,
            remaining=750,
            usage_percent=25.0,
            alert_level=BudgetAlertLevel.INFO,
            estimated_cost=0.25,
        )

    def get_usage_statistics(self) -> dict[str, Any]:
        return {
            "global": {"daily_tokens": 100, "monthly_tokens": 2000, "estimated_daily_cost": 0.5},
            "tasks": {"t1": {"tokens": 40}},
            "sessions": {"s1": {"tokens": 60}},
            "recent_records": [{"tokens": 10, "model": "m1"}],
        }

    async def reset_task_budget(self, task_id: str) -> None:
        self.calls.append(("reset_task_budget", {"task_id": task_id}))

    async def reset_session_budget(self, session_id: str) -> None:
        self.calls.append(("reset_session_budget", {"session_id": session_id}))


@pytest.fixture
def bm(server: Any) -> _FakeBudgetManager:
    fake = _FakeBudgetManager()
    server._budget_manager = fake
    return fake


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────


class TestTools:
    def test_check_budget_allowed(self, server: Any, bm: _FakeBudgetManager) -> None:
        out = _run(server.cost_control_check_budget(estimated_tokens=500, task_id="t1"))
        assert out == {"allowed": True}
        assert bm.calls[0][0] == "check_budget"

    def test_check_budget_exceeded_returns_error_dict(self, server: Any, bm: _FakeBudgetManager) -> None:
        bm.check_exc = server_mod.BudgetExceededException("over limit", current_usage=900, limit=500, limit_type="task")
        out = _run(server.cost_control_check_budget(estimated_tokens=1))
        assert out["allowed"] is False
        assert "error" in out

    def test_check_budget_quota_exhausted_returns_error_dict(self, server: Any, bm: _FakeBudgetManager) -> None:
        bm.check_exc = server_mod.QuotaExhaustedException("quota gone", usage_percent=100.0, quota_type="global")
        out = _run(server.cost_control_check_budget(estimated_tokens=1))
        assert out["allowed"] is False and "error" in out

    def test_record_usage_with_alert_serialization(self, server: Any, bm: _FakeBudgetManager) -> None:
        from budget_manager import BudgetAlertAction, BudgetAlertLevel  # noqa: PLC0415

        bm.record_alert = server_mod.BudgetAlert(
            level=BudgetAlertLevel.WARNING,
            usage_percent=72.5,
            message="approaching limit",
            scope="task",
            scope_id="t9",
            timestamp=datetime(2026, 9, 11, 12, 0, 0),
            action_taken=BudgetAlertAction.SAVE_CHECKPOINT,
        )
        out = _run(server.cost_control_record_usage(tokens=100, model="m1", task_id="t9"))
        assert out["recorded"] is True
        alert = out["alert"]
        assert alert["level"] == "warning"
        assert alert["action_taken"] == "save_checkpoint"
        assert alert["timestamp"] == "2026-09-11T12:00:00"

    def test_record_usage_without_alert_returns_none(self, server: Any, bm: _FakeBudgetManager) -> None:
        out = _run(server.cost_control_record_usage(tokens=1, model="m1"))
        assert out == {"recorded": True, "alert": None}

    def test_get_status_serialized(self, server: Any, bm: _FakeBudgetManager) -> None:
        out = _run(server.cost_control_get_status(task_id="t1"))
        assert out["scope"] == "global"
        assert out["alert_level"] == "info"
        assert out["usage_percent"] == 25.0

    def test_get_statistics_passthrough(self, server: Any, bm: _FakeBudgetManager) -> None:
        out = _run(server.cost_control_get_statistics())
        assert out["global"]["daily_tokens"] == 100

    def test_reset_task_and_session(self, server: Any, bm: _FakeBudgetManager) -> None:
        out_task = _run(server.cost_control_reset_task_budget(task_id="t1"))
        out_sess = _run(server.cost_control_reset_session_budget(session_id="s1"))
        assert out_task == {"reset": True, "task_id": "t1"}
        assert out_sess == {"reset": True, "session_id": "s1"}


# ──────────────────────────────────────────────
# on_load / on_unload
# ──────────────────────────────────────────────


def test_on_load_initializes_and_on_unload_resets(server: Any) -> None:
    assert server._budget_manager is None
    _run(server._on_load({}))
    assert server._budget_manager is not None
    _run(server._on_unload({}))
    assert server._budget_manager is None


# ──────────────────────────────────────────────
# http_handle 业务路由
# ──────────────────────────────────────────────


class TestHttpBusinessRoutes:
    def test_503_when_not_initialized(self, server: Any) -> None:
        server._budget_manager = None
        result = _run(server.http_handle(path="/ext/cost_control/budget/status", method="GET"))
        # _error 信封：success=False + data.status=503（前端 axios 据此降级）
        assert result["success"] is False
        assert result["data"]["status"] == 503
        assert "not initialized" in result["error"]

    def test_budget_status_route(self, server: Any, bm: _FakeBudgetManager) -> None:
        status, body = _decode(
            _run(server.http_handle(path="/ext/cost_control/budget/status", method="GET"))
        )
        assert status == 200
        assert body["scope"] == "global" and body["alert_level"] == "info"

    def test_usage_statistics_reshaped(self, server: Any, bm: _FakeBudgetManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # traces 库不存在 → 消耗字段走聚合单点降级全 0（F10：账本不作消耗兜底，
        # manager 只持限额/费率派生基准——此处 raw 无限额键，占比为 0）；
        # 请求方租户身份经内核注入的 X-AgentOS-Tenant 头透传（本租户口径）
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "nope.db"))
        status, body = _decode(
            _run(server.http_handle(path="/ext/cost_control/usage/statistics", method="GET",
                                    headers={"X-AgentOS-Tenant": "tenant-a"}))
        )
        assert status == 200
        # 前端形状契约：global_stats + tasks/sessions 数组 + updated_at
        assert body["global_stats"]["daily_tokens"] == 0
        assert body["global_stats"]["monthly_tokens"] == 0
        assert body["tasks"] == [{"task_id": "t1", "tokens": 40}]
        assert body["sessions"] == [{"session_id": "s1", "tokens": 60}]
        assert body["updated_at"]

    def test_config_route_flattens(self, server: Any, bm: _FakeBudgetManager) -> None:
        status, body = _decode(
            _run(server.http_handle(path="/ext/cost_control/config", method="GET"))
        )
        assert status == 200
        assert set(body) >= {
            "daily_token_limit",
            "monthly_token_limit",
            "per_task_token_limit",
            "per_session_token_limit",
            "warning_threshold",
            "critical_threshold",
            "auto_save_at_warning",
            "auto_pause_at_critical",
            "auto_stop_at_exhausted",
        }

    def test_report_route_periods(self, server: Any, bm: _FakeBudgetManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(server, "plugin", _SqliteDbRead())  # db-read 契约替身（真 sqlite 背书）
        # 真库单行 traces（非本月日期 → 日/月窗 0、全量明细 15）：report 装配
        # 消费 kernel_db 契约 traces 面真实聚合（F10 单点），读面经 env 同源
        # 注入；请求方租户身份经头透传（本租户口径）
        db = tmp_path / "agentos_kernel.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT,"
                " tenant_id TEXT, created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO traces VALUES ('t-1', 'core', ?, 'tenant-a', '2026-01-01T00:00:00Z')",
                (json.dumps({"llm_usage": {"input_tokens": 10, "output_tokens": 5,
                                           "total_tokens": 15},
                             "llm_model": "m1"}),),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db))
        for period in ("daily", "weekly", "monthly"):
            status, body = _decode(
                _run(server.http_handle(path="/ext/cost_control/report", method="GET",
                                        query={"period": period},
                                        headers={"X-AgentOS-Tenant": "tenant-a"}))
            )
            assert status == 200
            assert body["period"] == period
            assert body["total_tokens"] == 15
            assert body["by_model"]["m1"]["tokens"] == 15

    def test_budget_reset_route_clears_ledgers(self, server: Any, bm: _FakeBudgetManager) -> None:
        status, body = _decode(
            _run(server.http_handle(path="/ext/cost_control/budget/reset", method="POST"))
        )
        assert status == 200 and body["success"] is True
        assert bm._global_daily_usage == 0
        assert bm._task_usage == {} and bm._session_usage == {}
        assert bm._usage_records == []


# ──────────────────────────────────────────────
# _trace_stats_dict 聚合与降级
# ──────────────────────────────────────────────


class TestTraceStats:
    def test_db_missing_returns_zero_stats(self, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "nope.db"))
        stats = _run(server._trace_stats_dict("tenant-a"))
        assert stats == {"daily": 0, "monthly": 0, "total": 0, "by_model": {}, "records": []}

    def test_missing_tenant_fail_closed_zero(self, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # 请求方身份缺失（X-AgentOS-Tenant 头为空）→ fail-closed 全 0，
        # 绝不跨租户兜底（成本页不得退化为全租户消耗泄露）
        db = tmp_path / "k.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT,"
                " tenant_id TEXT, created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO traces VALUES ('t-0', 'core', ?, 'tenant-a', ?)",
                (json.dumps({"llm_usage": {"input_tokens": 10, "output_tokens": 5,
                                           "total_tokens": 15},
                             "llm_model": "m1"}), "2020-01-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db))
        assert _run(server._trace_stats_dict("")) == {
            "daily": 0, "monthly": 0, "total": 0, "by_model": {}, "records": [],
        }

    def test_aggregation_buckets_today_and_month(self, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(server, "plugin", _SqliteDbRead())  # db-read 契约替身（真 sqlite 背书）
        # 真 traces 表多行：窗口语义 = kernel_db 契约视图（UTC 日/月窗，F10
        # 单点统一口径）；期望值由播种时间戳按 UTC 现算，不硬编码。
        now = datetime.now(UTC)
        fmt = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
        yesterday = now - timedelta(days=1)
        db = tmp_path / "k.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT,"
                " tenant_id TEXT, created_at TEXT)"
            )
            for i, (inp, out, total, model, created) in enumerate([
                (10, 5, 15, "m1", fmt(now)),                 # 今日 + 本月
                (0, 20, 20, "m2", fmt(yesterday)),           # 昨日（本月与否按现算）
                (None, None, None, None, "2020-01-01T00:00:00Z"),  # 全空行 → total 0
            ]):
                conn.execute(
                    "INSERT INTO traces VALUES (?, ?, ?, ?, ?)",
                    (
                        f"t-{i}",
                        "core",
                        json.dumps({"llm_usage": {"input_tokens": inp, "output_tokens": out,
                                                  "total_tokens": total},
                                    "llm_model": model}),
                        "tenant-a",
                        created,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db))
        stats = _run(server._trace_stats_dict("tenant-a"))
        assert stats["total"] == 35
        assert stats["daily"] == 15
        expected_monthly = 15 + (20 if yesterday.strftime("%Y-%m") == now.strftime("%Y-%m") else 0)
        assert stats["monthly"] == expected_monthly  # 2020 行不进本月；昨日行跨月边界现算
        assert stats["monthly"] <= stats["total"]
        assert stats["by_model"] == {"m1": 15, "m2": 20, "unknown": 0}
        assert len(stats["records"]) == 3

    def test_sqlite_error_degrades_to_zero(self, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db = tmp_path / "not_a_db.db"
        db.write_text("this is not sqlite", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db))
        stats = _run(server._trace_stats_dict("tenant-a"))
        assert stats["total"] == 0 and stats["records"] == []
