"""traces 表 llm_usage 用量聚合共享查询 — monitoring / cost_control 单点取数。

两插件均只读直开 SQLite（读-only；capability 正门化为 P4 方向，本轮不做），
路径解析经 kernel_db 共享真值源。本模块单点承载同表（traces）同 JSON 路径
（patch_data.llm_usage.*）的聚合 SQL，防两侧实现漂移；连接生命周期、查询
失败降级策略（monitoring 降级空行/本地计数，cost_control 降级 0/保留内存
账本）与展示装配归各插件自持。

模型归属注意：按模型聚合取 ``$.llm_usage.model``，明细行另含 ``$.llm_model``
（llm_core 双写，两键语义对齐）——两侧各自现状口径，逐字保持。
"""

from __future__ import annotations

import sqlite3
from typing import Any

# 按模型聚合（monitoring token-usage 按模型表格）。
# 早期轨迹无 model/provider 字段 → COALESCE 空串（装配侧归「（未记录模型）」行）。
_AGGREGATE_BY_MODEL_SQL = (
    "SELECT COALESCE(json_extract(patch_data, '$.llm_usage.model'), ''),"
    "       COALESCE(json_extract(patch_data, '$.llm_usage.provider'), ''),"
    "       SUM(json_extract(patch_data, '$.llm_usage.input_tokens')),"
    "       SUM(json_extract(patch_data, '$.llm_usage.output_tokens')), "
    "       SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), "
    "       COUNT(*)"
    " FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
    " GROUP BY 1, 2 ORDER BY 5 DESC"
)

# 按日（UTC）聚合。created_at 为 ISO8601 UTC 文本，`substr(created_at, 1, 10)`
# 即日期——不经 datetime() 解析（纳秒精度 + 时区后缀解析行为版本相关）。
_AGGREGATE_BY_DAY_SQL = (
    "SELECT substr(created_at, 1, 10) AS day,"
    "       SUM(json_extract(patch_data, '$.llm_usage.input_tokens')),"
    "       SUM(json_extract(patch_data, '$.llm_usage.output_tokens')), "
    "       SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), "
    "       COUNT(*)"
    " FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
    " GROUP BY 1 ORDER BY 1 DESC"
)

# 明细行（cost_control 逐行累计 daily/monthly/by_model/records）。
_FETCH_USAGE_ROWS_SQL = (
    "SELECT json_extract(patch_data, '$.llm_usage.input_tokens'),"
    "       json_extract(patch_data, '$.llm_usage.output_tokens'),"
    "       json_extract(patch_data, '$.llm_usage.total_tokens'),"
    "       json_extract(patch_data, '$.llm_model'),"
    "       created_at"
    " FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
)

_SUM_TOTAL_DAILY_SQL = (
    "SELECT COALESCE(SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), 0)"
    " FROM traces"
    " WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
    "   AND substr(created_at, 1, 10) = date('now')"
)

_SUM_TOTAL_MONTHLY_SQL = (
    "SELECT COALESCE(SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), 0)"
    " FROM traces"
    " WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
    "   AND substr(created_at, 1, 7) = strftime('%Y-%m', 'now')"
)


def aggregate_usage_by_model(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """按 (model, provider) 聚合，返回 (model, provider, input, output, total, count) 行。"""
    cur = conn.cursor()
    cur.execute(_AGGREGATE_BY_MODEL_SQL)
    return cur.fetchall()


def aggregate_usage_by_day(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """按日（UTC）聚合，返回 (day, input, output, total, count) 行，日期倒序。"""
    cur = conn.cursor()
    cur.execute(_AGGREGATE_BY_DAY_SQL)
    return cur.fetchall()


def fetch_usage_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """返回 llm_usage 明细行 (input, output, total, llm_model, created_at)。"""
    cur = conn.cursor()
    cur.execute(_FETCH_USAGE_ROWS_SQL)
    return cur.fetchall()


def sum_total_tokens_daily(conn: sqlite3.Connection) -> int:
    """当日（UTC date('now')）total_tokens 总和，无行返回 0。"""
    cur = conn.cursor()
    cur.execute(_SUM_TOTAL_DAILY_SQL)
    return int(cur.fetchone()[0])


def sum_total_tokens_monthly(conn: sqlite3.Connection) -> int:
    """当月（UTC strftime('%Y-%m')）total_tokens 总和，无行返回 0。"""
    cur = conn.cursor()
    cur.execute(_SUM_TOTAL_MONTHLY_SQL)
    return int(cur.fetchone()[0])
