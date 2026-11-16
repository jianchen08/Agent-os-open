#!/usr/bin/env python
"""任务域投影漂移扫描（只读）——父管道 owned 镜像 vs 被拥有任务自身投影。

**本脚本只读不改**：以 SQLite 只读连接（``file:...?mode=ro`` + ``PRAGMA
query_only=1``）打开指定库，只发 SELECT；唯一可能落盘的是可选的待修清单
JSON（``--json-out``，写的是仓内/用户指定路径，不是数据库）。默认 dry-run
语义＝只打印待修清单与建议修正值，**不执行任何数据修正**。

事故背景（2026-10-20，管道 3f968d0d946f）：任务终态 failed，父管道的
``task.owned.<id>.status`` 镜像仍停在登记时的 running——父会话读它作答即读
脏投影。实时路径已修（终态事件派生时同批随写，ADR 草稿
``docs/working/ADR草稿-终态随写口径反转_20261020.md``），但**存量**脏行不会
被自动纠正：启动对账只识别「自身投影未决 × 运行簿记终态」的分歧，自身投影
已终态、镜像仍 running 的形态不构成分歧，无人执笔。本脚本即该存量形态的
一次性修复前置（只出清单，动手由主控在取得用户确认后另行执行）。

扫描口径
--------
- 读 ``pipeline_state`` 表（EAV：pipeline_id / field_key / value /
  value_kind / tenant_id / updated_at）两族键：
  ① 父管道侧 ``task.owned.<id>.status``（子任务登记面）；
  ② 子任务自身 ``task.status``（执行管道投影面）。
- 逐条比对同一 (tenant_id, owned_id) 的两个值，分四类：
  - ``consistent``：两值一致（正常，不列清单）；
  - ``registration_only``：被拥有 id 无自身 ``task.status`` 行——登记型任务
    （容器任务等「只登记不执行」，plugins/shared/tasks/http_api.py 同款口径），
    其 owned 面即状态面，无镜像可校（不列清单，只计数）；
  - ``mirror_stale``：**主修类**——自身投影已终态、镜像仍是旧值（典型
    running）：镜像应改写为自身投影现值；
  - ``mirror_revived``：自身投影在跑（未决态）、镜像却是终态——复活方向残留
    （``run.started`` 复位路径本轮不扩刀，见 TODO）：建议值同为自身投影现值，
    但标为「需确认」由主控裁决是否一并修。
- 未决投影词表与 ``plugins/shared/tasks/reconcile.py`` 的
  ``_UNDECIDED_TASK_STATUSES`` 同口径（""/running/pending/evaluating）。

用法
----
    # 扫描（只读，输出人读清单）
    python scripts/scan_task_projection_drift.py --db "<agentos_kernel.db 路径>"

    # 附加导出机读待修清单（JSON），仍不改库
    python scripts/scan_task_projection_drift.py --db "<...>" --json-out reports/task_projection_drift.json

    # 未来若要接门禁：有待修项时退出码 1（默认恒 0，纯扫描不阻断）
    python scripts/scan_task_projection_drift.py --db "<...>" --fail-on-drift

写库注意（供执行方）
------------------
运行中内核的 state 聚合读面**内存优先、DB 兜底**——直接改库只在下次内核冷启
后对聚合面生效（运行期内可能被内存值盖回）。要即时生效须走 pipeline-state
写面（内核在线 + 授权），本脚本不代劳。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# 未决投影词表（与 tasks/reconcile._UNDECIDED_TASK_STATUSES 同口径）
UNDECIDED_TASK_STATUSES = frozenset({"", "running", "pending", "evaluating"})

OWNED_PREFIX = "task.owned."
OWNED_STATUS_SUFFIX = ".status"
TASK_STATUS_KEY = "task.status"

# 分类 → 人读说明
CLASSIFICATION_LABELS = {
    "consistent": "一致（正常）",
    "registration_only": "登记型任务（无自身投影行，owned 面即状态面）",
    "mirror_stale": "主修类：自身投影已终态、镜像仍是旧值",
    "mirror_revived": "复活方向残留：自身在跑、镜像却是终态（本轮不修，需确认）",
}


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读方式打开库（mode=ro + query_only 双保险）。

    Raises:
        SystemExit: 库不存在或无法以只读方式打开。
    """
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=1")
    except sqlite3.Error as exc:
        raise SystemExit(f"以只读方式打开数据库失败: {db_path}（{exc}）") from exc
    return conn


def _decode(value: str, value_kind: str) -> str:
    """状态值解码（value_kind='str' 即原文；'null' 即空串）。"""
    if value_kind == "null":
        return ""
    return value


def load_state_rows(
    conn: sqlite3.Connection, tenant: str | None
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """读 owned 状态行与自身投影行。

    Returns:
        (owned_rows, own_status_map)——owned_rows 为父管道登记面行列表；
        own_status_map 以 (tenant_id, pipeline_id) 索引自身 task.status 行。
    """
    tenant_clause = " AND tenant_id = ?" if tenant else ""
    params: tuple[Any, ...] = (tenant,) if tenant else ()
    owned_rows = [
        {
            "parent_pipeline_id": row[0],
            "field_key": row[1],
            "value": _decode(row[2], row[3]),
            "tenant_id": row[4],
            "updated_at": row[5],
        }
        for row in conn.execute(
            "SELECT pipeline_id, field_key, value, value_kind, tenant_id, updated_at "
            "FROM pipeline_state "
            f"WHERE field_key LIKE 'task.owned.%.status'{tenant_clause}",
            params,
        )
    ]
    own_rows = [
        {
            "child_pipeline_id": row[0],
            "value": _decode(row[1], row[2]),
            "tenant_id": row[3],
            "updated_at": row[4],
        }
        for row in conn.execute(
            "SELECT pipeline_id, value, value_kind, tenant_id, updated_at "
            f"FROM pipeline_state WHERE field_key = ?{tenant_clause}",
            (TASK_STATUS_KEY, *params),
        )
    ]
    own_map = {(r["tenant_id"], r["child_pipeline_id"]): r for r in own_rows}
    return owned_rows, own_map


def classify(own_status: str | None, owned_status: str) -> str:
    """两值比对分类（纯函数，可单测）。"""
    if own_status is None:
        return "registration_only"
    if own_status == owned_status:
        return "consistent"
    if own_status in UNDECIDED_TASK_STATUSES:
        return "mirror_revived"
    return "mirror_stale"


def build_plan(
    owned_rows: list[dict[str, Any]], own_map: dict[tuple[str, str], dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构造待修清单与分类计数（纯函数，可单测）。"""
    plan: list[dict[str, Any]] = []
    counts: dict[str, int] = dict.fromkeys(CLASSIFICATION_LABELS, 0)
    for row in owned_rows:
        field_key = str(row["field_key"])
        owned_id = field_key[len(OWNED_PREFIX) : -len(OWNED_STATUS_SUFFIX)]
        own = own_map.get((str(row["tenant_id"]), owned_id))
        own_status = str(own["value"]) if own else None
        kind = classify(own_status, str(row["value"]))
        counts[kind] += 1
        if kind in ("consistent", "registration_only"):
            continue
        plan.append(
            {
                "tenant_id": row["tenant_id"],
                "parent_pipeline_id": row["parent_pipeline_id"],
                "owned_id": owned_id,
                "field_key": field_key,
                "current_value": row["value"],
                "suggested_value": own_status,
                "child_status": own_status,
                "classification": kind,
                "classification_label": CLASSIFICATION_LABELS[kind],
                "parent_updated_at": row["updated_at"],
                "child_updated_at": own["updated_at"] if own else "",
            }
        )
    return plan, counts


def render_report(plan: list[dict[str, Any]], counts: dict[str, int]) -> str:
    """人读清单渲染（纯展示）。"""
    lines = [
        "── 任务域投影漂移扫描（只读，未改任何数据） ──",
        f"owned 状态行总数: {sum(counts.values())}",
        f"  一致: {counts['consistent']}",
        f"  登记型任务（无自身投影）: {counts['registration_only']}",
        f"  主修类（自身终态、镜像旧值）: {counts['mirror_stale']}",
        f"  复活方向残留（自身在跑、镜像终态）: {counts['mirror_revived']}",
        "",
    ]
    if not plan:
        lines.append("无待修行（两族投影一致或属登记型任务）。")
        return "\n".join(lines)
    lines.append("待修清单（建议值 = 被拥有任务自身 task.status 现值）：")
    for i, item in enumerate(plan, 1):
        lines.append(
            f"  {i:>3}. [{item['classification']}] 父管道={item['parent_pipeline_id']} "
            f"owned_id={item['owned_id']} 键={item['field_key']}"
        )
        lines.append(
            f"       现值={item['current_value']!r} → 建议值={item['suggested_value']!r} "
            f"（租户={item['tenant_id']} 父管道 updated_at={item['parent_updated_at']} "
            f"子管道 updated_at={item['child_updated_at']}）"
        )
    lines.append("")
    lines.append("说明：本脚本不改数据；实际修正由主控在取得用户确认后执行。")
    lines.append("      运行中内核的 state 聚合面内存优先，直接改库仅在下次冷启后对聚合面生效。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：只读扫描 + 打印待修清单（可选导出 JSON）。"""
    parser = argparse.ArgumentParser(description="只读扫描 pipeline_state 的 owned 镜像 vs 自身投影漂移（不改数据）")
    parser.add_argument("--db", required=True, help="agentos_kernel.db 路径（只读打开）")
    parser.add_argument("--tenant", default=None, help="只扫指定租户（缺省=全部）")
    parser.add_argument("--json-out", default=None, help="待修清单 JSON 输出路径（可选）")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="存在待修项时退出码 1（默认恒 0；供未来接门禁用）",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser()
    conn = open_readonly(db_path)
    try:
        owned_rows, own_map = load_state_rows(conn, args.tenant)
    except sqlite3.Error as exc:
        raise SystemExit(f"读取 pipeline_state 失败: {exc}") from exc
    finally:
        conn.close()

    plan, counts = build_plan(owned_rows, own_map)
    print(render_report(plan, counts))
    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "db": str(db_path),
                    "tenant": args.tenant,
                    "counts": counts,
                    "candidates": plan,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"待修清单已导出: {out_path}")
    if args.fail_on_drift and plan:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
