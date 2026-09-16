# @feature: FP-0.2.可观测性 可观测性基座 | @vision: V3 可嵌入 | @ci: python-coverage
"""traces_usage 共享查询模块测试 + monitoring/cost_control 两调用面一致性。

- 共享 SQL 单点（aggregate_usage_by_model / aggregate_usage_by_day /
  fetch_usage_rows / sum_total_tokens_daily / sum_total_tokens_monthly）：
  同库同输入下各聚合面的总量与请求数互相一致（性质断言，防再漂移）；
- 跨插件面：monitoring._collect_token_usage 与 cost_control._trace_stats_dict
  对同一 traces 库产出同量 token 总量（收敛后天然成立，锁住防再漂移）。

防拟合：≥2 组有区分度输入（两模型/两日期/无 model 字段行/无 llm_usage 行），
字面值断言配总量恒等式性质断言。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))


def _load_plugin_server(name: str, rel: Path, colliding: tuple[str, ...] = ()) -> Any:
    """按显式路径加载插件 server.py（各测试文件独立模块命名空间）。

    colliding：该插件平铺 import 的跨插件同名裸模块（如 cost_control 的
    budget_manager/exceptions 与 llm 插件同名）——加载前从 sys.modules 逐出
    防劫持。插件目录晋升 sys.path 后不摘除：插件懒 import 发生在调用期
    （如 monitoring._ensure_monitor），对齐 tests/conftest.py 收集钩子
    "晋升后不恢复"的既有约定。
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    plugin_dir = str(rel.parent)
    for m in colliding:
        sys.modules.pop(m, None)
    # promote 语义（先摘后插到最前）：collection 期各 conftest 可能已把本目录
    # 插在 path 中段，而更早测试晋升的目录仍占 path[0]（如 llm 的 exceptions
    # 劫持 cost_control 的 from exceptions import）。
    while plugin_dir in sys.path:
        sys.path.remove(plugin_dir)
    sys.path.insert(0, plugin_dir)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def seeded_db() -> Generator[str, None, None]:
    """traces 库 + pipeline_state 库（两模型/两日期/无 model 字段行/无 llm_usage 行）。

    两张表是同库不同源（同一真值的两个写面）：
    - ``traces`` = 逐轮 append-only 轨迹：成本账（cost_control）与按时间聚合读它；
    - ``pipeline_state`` = 每管道累计标量投影：累计 token 口径（monitoring 按模型）
      读它（ADR 2026-09-15 监控页加载治理 §决策2）。
    本 fixture 让两表同量，跨面一致性断言才有可断的基础。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "kernel.db")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT, created_at TEXT)"
            )
            rows = [
                # 模型 A ×2（同日聚合累加）
                ("t1", "core", '{"llm_usage": {"model": "gpt-x", "provider": "openai", "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}, "llm_model": "gpt-x"}', today),
                ("t2", "core", '{"llm_usage": {"model": "gpt-x", "provider": "openai", "input_tokens": 800, "output_tokens": 200, "total_tokens": 1000}, "llm_model": "gpt-x"}', today),
                # 模型 B（另一日期）
                ("t3", "core", '{"llm_usage": {"model": "deepseek-r1", "provider": "deepseek", "input_tokens": 100, "output_tokens": 50, "total_tokens": 150}, "llm_model": "deepseek-r1"}', "2026-01-15T08:00:00"),
                # 无 model 字段（归「（未记录模型）」/unknown 行）
                ("t4", "core", '{"llm_usage": {"input_tokens": 50, "output_tokens": 27, "total_tokens": 77}}', "2026-01-15T09:00:00"),
                # 无 llm_usage（所有聚合面排除）
                ("t5", "core", '{"messages": {"_ops": []}}', today),
            ]
            conn.executemany("INSERT INTO traces VALUES (?, ?, ?, ?)", rows)

            # pipeline_state：与上行同量的累计投影（monitoring 按模型口径读它）
            conn.execute(
                "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, field_value TEXT)"
            )
            state_rows = [
                # gpt-x 两管道（1500 + 1000 = 2500）
                ("p-a", "track.llm_usage", '{"total_input_tokens": 1200, "total_output_tokens": 300, "total_tokens": 1500}'),
                ("p-a", "llm_model", '"gpt-x"'),
                ("p-b", "track.llm_usage", '{"total_input_tokens": 800, "total_output_tokens": 200, "total_tokens": 1000}'),
                ("p-b", "llm_model", '"gpt-x"'),
                # deepseek-r1
                ("p-c", "track.llm_usage", '{"total_input_tokens": 100, "total_output_tokens": 50, "total_tokens": 150}'),
                ("p-c", "llm_model", '"deepseek-r1"'),
                # 无 llm_model → 「（未记录模型）」行
                ("p-d", "track.llm_usage", '{"total_input_tokens": 50, "total_output_tokens": 27, "total_tokens": 77}'),
                # 非用量键（聚合面不得计入）
                ("p-d", "context_window", "32000"),
            ]
            conn.executemany("INSERT INTO pipeline_state VALUES (?, ?, ?)", state_rows)
            conn.commit()
        finally:
            conn.close()
        yield db_path


def test_shared_aggregations_mutually_consistent(seeded_db: str) -> None:
    """同一库上五个共享查询面总量/请求数恒等（单点 SQL 的防漂移锁）。"""
    import traces_usage

    conn = sqlite3.connect(seeded_db)
    try:
        by_model = traces_usage.aggregate_usage_by_model(conn)
        by_day = traces_usage.aggregate_usage_by_day(conn)
        raw_rows = traces_usage.fetch_usage_rows(conn)
        daily = traces_usage.sum_total_tokens_daily(conn)
        monthly = traces_usage.sum_total_tokens_monthly(conn)
    finally:
        conn.close()

    # 形状：4 行 llm_usage；by_model 3 组（gpt-x/deepseek-r1/空）；by_day 2 天
    assert len(by_model) == 3
    assert len(by_day) == 2
    assert len(raw_rows) == 4

    # 字面锚点：gpt-x 行 = 1200+800 in / 2000 total
    gptx = next(r for r in by_model if r[0] == "gpt-x")
    assert gptx[2] == 2000 and gptx[5] == 2
    # 无 model 字段 → 空串（COALESCE 行为保持）
    assert any(r[0] == "" for r in by_model)

    # 性质恒等：总量与请求数跨面一致
    total_by_model = sum(r[4] for r in by_model)
    total_by_day = sum(r[3] for r in by_day)
    total_raw = sum(r[2] for r in raw_rows)
    assert total_by_model == total_by_day == total_raw == 2727
    assert sum(r[5] for r in by_model) == sum(r[4] for r in by_day) == len(raw_rows) == 4

    # 日/月标量和 ≤ 全量；当日行与 by_day 对齐
    assert daily <= total_by_day
    assert monthly <= total_by_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_row = next(r for r in by_day if r[0] == today)
    assert daily == day_row[3]


def test_cross_face_monitoring_vs_cost_control(seeded_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """两插件调用面对同一 traces 库产出同量 token 总量（收敛后天然成立）。"""
    monitoring_server = _load_plugin_server(
        "traces_usage_monitoring_under_test",
        _REPO_ROOT / "plugins" / "shared" / "system" / "monitoring" / "server.py",
    )
    cost_control_server = _load_plugin_server(
        "traces_usage_cost_control_under_test",
        _REPO_ROOT / "plugins" / "shared" / "system" / "cost_control" / "server.py",
        colliding=("budget_manager", "exceptions", "config"),
    )

    monkeypatch.setattr(monitoring_server, "_kernel_db_path", lambda: seeded_db)
    monkeypatch.setattr(cost_control_server, "kernel_db_path", lambda: Path(seeded_db))

    usage = monitoring_server._collect_token_usage()
    stats = cost_control_server._trace_stats_dict()

    # 总量恒等（monitoring 按模型行求和 = cost_control 逐行累计）
    assert usage["total_tokens"] == stats["total"] == 2727.0
    # 按模型一致（命名行；monitoring 空模型行 vs cost_control unknown 差异为装配层契约）
    by_model_rows = {r["model"]: r["total_tokens"] for r in usage["rows"]}
    assert by_model_rows["gpt-x"] == stats["by_model"]["gpt-x"] == 2500.0
    assert by_model_rows["deepseek-r1"] == stats["by_model"]["deepseek-r1"] == 150.0
