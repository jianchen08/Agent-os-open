# @feature: FP-0.2.可观测性 可观测性基座 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring /ext/monitoring/token-usage 从 traces 表聚合测试（G4）。

2026-08-18 G4 修复：
- token-usage 原恒 0（本地 PerformanceMonitor 不持有 token 计数）→ 改为从
  traces.patch_data.llm_usage 聚合（llm_core 每轮写入的单轮用量，跨运行累计）；
- 请求数/错误/耗时保持读 PerformanceMonitor 本地计数（record_llm_request 口径）；
- DB 缺失/查询失败降级本地计数（token=0），契约不破坏。
"""

from __future__ import annotations

import importlib.util
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


def _seed_traces(db_path: str) -> None:
    """写两行 llm_usage trace（模拟 llm_core 落库形态）+ 一行无 usage 的 trace。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT, created_at TEXT)"
        )
        trace_a = (
            '{"llm_usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,'
            ' "cached_tokens": 1000}}'
        )
        trace_b = (
            '{"llm_usage": {"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000,'
            ' "cached_tokens": 0}}'
        )
        conn.execute(
            "INSERT INTO traces VALUES ('t1', 'core', ?, '2026-08-18')",
            (trace_a,),
        )
        conn.execute(
            "INSERT INTO traces VALUES ('t2', 'core', ?, '2026-08-18')",
            (trace_b,),
        )
        conn.execute(
            "INSERT INTO traces VALUES ('t3', 'core', '{\"messages\": {\"_ops\": []}}', '2026-08-18')"
        )
        conn.commit()
    finally:
        conn.close()


class TestTokenUsageFromTraces:
    """_collect_token_usage 聚合行为。"""

    def test_aggregates_traces_llm_usage(self, monkeypatch) -> None:
        """DB 有 llm_usage trace：跨运行累加（2000/500/2500），本地计数保持。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kernel.db")
            _seed_traces(db_path)
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
