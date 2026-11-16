# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""内核库读面跨插件一致性（原 test_traces_usage 跨面锁，traces_usage 单点迁
kernel_db 契约后改道 env 驱动）。

monitoring._collect_token_usage（pipeline_state 累计投影口径，ADR 2026-09-15
决策2）与 cost_control._trace_stats_dict（traces 明细聚合口径，kernel_db 契约
视图）对同一真值库产出同量 token 总量——两侧 SQL 单点不同源（state 投影 vs
traces 明细），本锁防两读面再漂移。

防拟合：≥2 组有区分度输入（两模型/两日期/无 model 字段行/无 llm_usage 行），
字面值断言配总量恒等式性质断言。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
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
    assert spec is not None
    assert spec.loader is not None
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


@pytest.fixture
def seeded_db(monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """traces 库 + pipeline_state 库（两模型/两日期/无 model 字段行/无 llm_usage 行）。

    两张表是同库不同源（同一真值的两个写面）：
    - ``traces`` = 逐轮 append-only 轨迹：成本账（cost_control 经 kernel_db
      契约 traces 面按租户收窄读它）；
    - ``pipeline_state`` = 每管道累计标量投影：累计 token 口径（monitoring 按
      模型读它，ADR 2026-09-15 监控页加载治理 §决策2）。
    本 fixture 让两表同量，跨面一致性断言才有可断的基础；经 AGENTOS_DB_PATH
    env 同源注入（两插件读面同一解析单点，不 mock 连接层）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "kernel.db"
        today = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE traces (trace_id TEXT, plugin_id TEXT, patch_data TEXT,"
                " tenant_id TEXT, created_at TEXT)"
            )
            rows = [
                # 模型 A ×2（同日聚合累加）
                ("t1", "core", '{"llm_usage": {"model": "gpt-x", "provider": "openai", "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}, "llm_model": "gpt-x"}', "tenant-x", today),
                ("t2", "core", '{"llm_usage": {"model": "gpt-x", "provider": "openai", "input_tokens": 800, "output_tokens": 200, "total_tokens": 1000}, "llm_model": "gpt-x"}', "tenant-x", today),
                # 模型 B（另一日期）
                ("t3", "core", '{"llm_usage": {"model": "deepseek-r1", "provider": "deepseek", "input_tokens": 100, "output_tokens": 50, "total_tokens": 150}, "llm_model": "deepseek-r1"}', "tenant-x", "2026-01-15T08:00:00"),
                # 无 model 字段（归「（未记录模型）」/unknown 行）
                ("t4", "core", '{"llm_usage": {"input_tokens": 50, "output_tokens": 27, "total_tokens": 77}}', "tenant-x", "2026-01-15T09:00:00"),
                # 无 llm_usage（所有聚合面排除）
                ("t5", "core", '{"messages": {"_ops": []}}', "tenant-x", today),
                # 他租户行（契约 traces 面按租户收窄，tenant-x 视角不可见）
                ("t6", "core", '{"llm_usage": {"model": "gpt-x", "provider": "openai", "input_tokens": 60, "output_tokens": 39, "total_tokens": 99}, "llm_model": "gpt-x"}', "tenant-y", today),
            ]
            conn.executemany("INSERT INTO traces VALUES (?, ?, ?, ?, ?)", rows)

            # pipeline_state：与上行同量的累计投影（monitoring 按模型口径读它）
            conn.execute(
                "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT, value_kind TEXT, tenant_id TEXT, updated_at TEXT)"
            )
            # state 值域标量化（ADR 2026-09-18）：json 键存 JSON 文本，标量按 str 原样存
            state_rows = [
                # gpt-x 两管道（1500 + 1000 = 2500）
                ("p-a", "track.llm_usage", '{"total_input_tokens": 1200, "total_output_tokens": 300, "total_tokens": 1500}', "json"),
                ("p-a", "llm_model", "gpt-x", "str"),
                ("p-b", "track.llm_usage", '{"total_input_tokens": 800, "total_output_tokens": 200, "total_tokens": 1000}', "json"),
                ("p-b", "llm_model", "gpt-x", "str"),
                # deepseek-r1
                ("p-c", "track.llm_usage", '{"total_input_tokens": 100, "total_output_tokens": 50, "total_tokens": 150}', "json"),
                ("p-c", "llm_model", "deepseek-r1", "str"),
                # 无 llm_model → 「（未记录模型）」行
                ("p-d", "track.llm_usage", '{"total_input_tokens": 50, "total_output_tokens": 27, "total_tokens": 77}', "json"),
                # 非用量键（聚合面不得计入）
                ("p-d", "context_window", "32000", "str"),
            ]
            conn.executemany(
                "INSERT INTO pipeline_state VALUES (?, ?, ?, ?, 'tenant-x', '2026-01-01T00:00:00Z')",
                state_rows,
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db_path))
        yield db_path


def test_cross_face_monitoring_vs_cost_control(seeded_db: Path) -> None:
    """两插件调用面对同一真值库产出同量 token 总量（防两读面漂移的跨面锁）。

    cost_control 读面已迁 db-read 契约（C2 刀2）：替身按身份租户收窄仿真
    （收窄执法本体归 Rust 契约测试）；monitoring 累计面暂走直连（C3 同刀
    随后迁移），经 AGENTOS_DB_PATH env 同源取数。
    """
    monitoring_server = _load_plugin_server(
        "kernel_read_faces_monitoring_under_test",
        _REPO_ROOT / "plugins" / "shared" / "monitoring" / "server.py",
    )
    cost_control_server = _load_plugin_server(
        "kernel_read_faces_cost_control_under_test",
        _REPO_ROOT / "plugins" / "shared" / "cost_control" / "server.py",
        colliding=("budget_manager", "exceptions", "config"),
    )

    import kernel_reads as _kr
    from _libs.testing.db_read_fake import FakeDbRead

    tok_x = "Bearer tok-x"
    tok_y = "Bearer tok-y"
    _kr.set_provider(
        "db-read", FakeDbRead(seeded_db, {tok_x: "tenant-x", tok_y: "tenant-y"}).provider
    )
    cost_control_server.plugin._capabilities["db-read"] = FakeDbRead(
        seeded_db, {tok_x: "tenant-x", tok_y: "tenant-y"}
    )
    cost_control_server.plugin._capabilities.setdefault("metrics", _NullHandle())

    usage = _run(coro=monitoring_server._collect_token_usage("tenant-x", tok_x))
    stats = _run(coro=cost_control_server._trace_stats_dict("tenant-x", tok_x))

    # 总量恒等（monitoring 按模型行求和 = cost_control 逐行累计；均为 tenant-x
    # 租户口径——traces 行 tenant_id 已按租户播种，他租户 t6 行不可见）
    assert usage["total_tokens"] == stats["total"] == 2727.0
    # 按模型一致（命名行；monitoring 空模型行 vs cost_control unknown 差异为装配层契约）
    by_model_rows = {r["model"]: r["total_tokens"] for r in usage["rows"]}
    assert by_model_rows["gpt-x"] == stats["by_model"]["gpt-x"] == 2500.0
    assert by_model_rows["deepseek-r1"] == stats["by_model"]["deepseek-r1"] == 150.0

    # 性质恒等：cost_control 逐行累计 = 按模型分组之和（同一明细行的两种装配）
    assert sum(stats["by_model"].values()) == stats["total"]

    # 租户隔离：他租户视角只见自己（t6 = 99），本租户行不可达
    other = _run(coro=cost_control_server._trace_stats_dict("tenant-y", tok_y))
    assert other["total"] == 99.0
    assert set(other["by_model"]) == {"gpt-x"}


class _NullHandle:
    """metrics capability 空句柄（record_metric 插桩缺失时静默跳过）。"""

    async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        return {}


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
