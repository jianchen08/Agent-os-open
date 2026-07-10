# -*- coding: utf-8 -*-
"""
数据库快照对比

支持 SQLite 数据库的快照创建与对比：
- create: 创建指定表的快照（JSON 格式）
- diff: 对比两个快照的差异，输出新增/删除/修改的行
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def get_table_names(db_path: str) -> list[str]:
    """获取数据库中所有表名"""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def get_table_schema(db_path: str, table: str) -> list[str]:
    """获取表的列名列表"""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()
    return columns


def snapshot_table(db_path: str, table: str) -> dict[str, Any]:
    """对单个表创建快照"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    columns = get_table_schema(db_path, table)
    if not columns:
        print(f"  ⚠️  表 '{table}' 无列信息，跳过")
        conn.close()
        return {"table": table, "columns": [], "rows": [], "count": 0}

    cursor = conn.execute(f"SELECT * FROM {table}")
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(row)
        # 为每行生成唯一 hash（用于 diff 对比）
        row_str = json.dumps(row_dict, sort_keys=True, ensure_ascii=False)
        row_hash = hashlib.md5(row_str.encode("utf-8")).hexdigest()
        row_dict["_hash"] = row_hash
        rows.append(row_dict)

    conn.close()
    print(f"  ✅ {table}: {len(rows)} 行")
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "count": len(rows),
    }


def create_snapshot(db_path: str, tables: list[str] | None = None) -> dict[str, Any]:
    """创建数据库快照"""
    path = Path(db_path)
    if not path.exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        sys.exit(1)

    if tables is None or len(tables) == 0:
        tables = get_table_names(db_path)
        print(f"📋 自动检测到 {len(tables)} 个表")

    print(f"📸 创建快照: {db_path}")

    snapshots = {}
    for table in tables:
        snapshots[table] = snapshot_table(db_path, table)

    total_rows = sum(s["count"] for s in snapshots.values())
    print(f"📊 快照完成: {len(snapshots)} 个表, 共 {total_rows} 行")

    return {
        "source": str(path),
        "tables": snapshots,
        "total_rows": total_rows,
    }


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """对比两个快照，返回差异"""
    diff_result = {
        "tables_added": [],
        "tables_removed": [],
        "table_changes": {},
        "summary": {
            "tables_added": 0,
            "tables_removed": 0,
            "tables_modified": 0,
            "rows_added": 0,
            "rows_removed": 0,
            "rows_modified": 0,
        },
    }

    before_tables = set(before.get("tables", {}).keys())
    after_tables = set(after.get("tables", {}).keys())

    # 新增的表
    added_tables = after_tables - before_tables
    for t in sorted(added_tables):
        count = after["tables"][t]["count"]
        diff_result["tables_added"].append({"table": t, "rows": count})
        diff_result["summary"]["tables_added"] += 1
        diff_result["summary"]["rows_added"] += count

    # 删除的表
    removed_tables = before_tables - after_tables
    for t in sorted(removed_tables):
        count = before["tables"][t]["count"]
        diff_result["tables_removed"].append({"table": t, "rows": count})
        diff_result["summary"]["tables_removed"] += 1
        diff_result["summary"]["rows_removed"] += count

    # 共同表逐行对比
    common_tables = before_tables & after_tables
    for table in sorted(common_tables):
        before_rows = before["tables"][table]["rows"]
        after_rows = after["tables"][table]["rows"]

        # 用 hash 构建索引（以第一列作为主键，如果没有 _hash 则以全行对比）
        # 优先使用主键列（假设第一个列）
        before_by_hash = {r["_hash"]: r for r in before_rows}
        after_by_hash = {r["_hash"]: r for r in after_rows}

        before_hashes = set(before_by_hash.keys())
        after_hashes = set(after_by_hash.keys())

        added = after_hashes - before_hashes
        removed = before_hashes - after_hashes
        common = before_hashes & after_hashes

        changes = {
            "rows_added": [
                {k: v for k, v in after_by_hash[h].items() if k != "_hash"}
                for h in sorted(added)
            ],
            "rows_removed": [
                {k: v for k, v in before_by_hash[h].items() if k != "_hash"}
                for h in sorted(removed)
            ],
            "rows_modified": [],  # hash 一致说明内容未变
            "count_added": len(added),
            "count_removed": len(removed),
            "count_unchanged": len(common),
        }

        if added or removed:
            diff_result["table_changes"][table] = changes
            diff_result["summary"]["tables_modified"] += 1
            diff_result["summary"]["rows_added"] += len(added)
            diff_result["summary"]["rows_removed"] += len(removed)

    return diff_result


def print_diff_summary(diff: dict[str, Any]):
    """打印差异摘要"""
    s = diff["summary"]
    print("\n" + "=" * 60)
    print("📊 快照对比结果")
    print("=" * 60)
    print(f"  新增表: {s['tables_added']}")
    print(f"  删除表: {s['tables_removed']}")
    print(f"  变更表: {s['tables_modified']}")
    print(f"  新增行: {s['rows_added']}")
    print(f"  删除行: {s['rows_removed']}")
    print(f"  修改行: {s['rows_modified']}")

    if diff["tables_added"]:
        print("\n--- 新增的表 ---")
        for t in diff["tables_added"]:
            print(f"  + {t['table']} ({t['rows']} 行)")

    if diff["tables_removed"]:
        print("\n--- 删除的表 ---")
        for t in diff["tables_removed"]:
            print(f"  - {t['table']} ({t['rows']} 行)")

    for table, changes in diff["table_changes"].items():
        print(f"\n--- 表: {table} ---")
        print(f"  新增 {changes['count_added']} 行, 删除 {changes['count_removed']} 行, "
              f"不变 {changes['count_unchanged']} 行")
        if changes["rows_added"]:
            print("  新增行:")
            for row in changes["rows_added"][:5]:  # 最多显示5条
                row_str = json.dumps(row, ensure_ascii=False, default=str)
                print(f"    + {row_str[:120]}")
            if len(changes["rows_added"]) > 5:
                print(f"    ... 还有 {len(changes['rows_added']) - 5} 行")
        if changes["rows_removed"]:
            print("  删除行:")
            for row in changes["rows_removed"][:5]:
                row_str = json.dumps(row, ensure_ascii=False, default=str)
                print(f"    - {row_str[:120]}")
            if len(changes["rows_removed"]) > 5:
                print(f"    ... 还有 {len(changes['rows_removed']) - 5} 行")

    # 判断是否有变化
    has_changes = (s["tables_added"] + s["tables_removed"] + s["tables_modified"]
                   + s["rows_added"] + s["rows_removed"] + s["rows_modified"]) > 0
    if not has_changes:
        print("\n✅ 两个快照完全一致，无差异。")

    return has_changes


def cmd_create(args):
    """执行 create 子命令"""
    tables = args.tables.split(",") if args.tables else None
    snapshot = create_snapshot(args.db, tables)

    output_path = args.output or "snapshot.json"
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
    print(f"💾 快照已保存: {output_path}")


def cmd_diff(args):
    """执行 diff 子命令"""
    before_path = Path(args.before)
    after_path = Path(args.after)

    if not before_path.exists():
        print(f"❌ 快照文件不存在: {args.before}")
        sys.exit(1)
    if not after_path.exists():
        print(f"❌ 快照文件不存在: {args.after}")
        sys.exit(1)

    with open(before_path, encoding="utf-8") as f:
        before = json.load(f)
    with open(after_path, encoding="utf-8") as f:
        after = json.load(f)

    diff = diff_snapshots(before, after)
    has_changes = print_diff_summary(diff)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(diff, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n💾 对比结果已保存: {args.output}")

    # 有变化退出码 0，无变化退出码 1（便于脚本判断"测试是否产生了预期的数据变更"）
    sys.exit(0 if has_changes else 1)


def main():
    parser = argparse.ArgumentParser(
        description="数据库快照对比（SQLite）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 创建快照（操作前）
  python db_snapshot.py create --db app.db --tables users,tasks --output before.json

  # 创建快照（操作后）
  python db_snapshot.py create --db app.db --tables users,tasks --output after.json

  # 对比两个快照
  python db_snapshot.py diff --before before.json --after after.json --output diff_result.json
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # create 子命令
    create_parser = subparsers.add_parser("create", help="创建数据库快照")
    create_parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    create_parser.add_argument("--tables", help="要快照的表名（逗号分隔），不指定则快照所有表")
    create_parser.add_argument("--output", default="snapshot.json", help="快照输出路径（默认 snapshot.json）")

    # diff 子命令
    diff_parser = subparsers.add_parser("diff", help="对比两个快照")
    diff_parser.add_argument("--before", required=True, help="操作前快照文件路径")
    diff_parser.add_argument("--after", required=True, help="操作后快照文件路径")
    diff_parser.add_argument("--output", help="对比结果输出路径（不指定则仅打印）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    print("=" * 60)
    print("📸 数据库快照工具")
    print("=" * 60)

    if args.command == "create":
        cmd_create(args)
    elif args.command == "diff":
        cmd_diff(args)


if __name__ == "__main__":
    main()
