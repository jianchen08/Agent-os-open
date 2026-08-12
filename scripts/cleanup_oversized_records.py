"""一次性清理脚本：截断执行记录中超大 content。

背景：部分管道的执行记录存了失控的工具输出（如 bash read_log 读 grep
无 head 的 637 万字符结果），导致 records.yaml 巨大、inherit pipe 继承后
撑爆子任务 context（f81e12cac33d 卡死根因）。

代码层已加三道截断防线（track 存储 / clone_pipeline_records /
state_builder 加载），本脚本用于清理「已存在」的磁盘坏数据——
代码只防未来新数据，本脚本清存量。

用法：
  python scripts/cleanup_oversized_records.py --dry-run          # 先看有多少
  python scripts/cleanup_oversized_records.py                     # 执行清理（写回，原文件存 .bak）
  python scripts/cleanup_oversized_records.py --max-chars 30000   # 自定义阈值
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

DEFAULT_MAX_CHARS = 20000


def truncate(text: object, max_chars: int) -> tuple[object, bool]:
    """超大文本截断为「头部 + 提示 + 尾部」。返回 (新内容, 是否截断)。"""
    if text is None:
        return text, False
    s = str(text)
    if len(s) <= max_chars:
        return text, False
    orig = len(s)
    half = max_chars // 2
    return (
        f"{s[:half]}\n\n...[清理脚本截断：原始 {orig} 字符"
        f"（工具输出过大），完整数据见工具日志]...\n\n{s[-half:]}",
        True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="截断执行记录中超大 content")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写回")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="单条 content 字符上限")
    parser.add_argument("--data-dir", default="data/pipelines", help="执行记录目录")
    args = parser.parse_args()

    root = Path(args.data_dir)
    if not root.exists():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 1

    total_files = 0
    total_truncated = 0
    total_chars_saved = 0

    for yf in sorted(root.rglob("*.yaml")):
        if yf.suffix == ".bak":
            continue
        try:
            raw = yf.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except Exception as e:  # noqa: BLE001
            print(f"跳过（解析失败）: {yf} - {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        records = data.get("records")
        if not records or not isinstance(records, list):
            continue

        changed = False
        file_truncated = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            c = rec.get("content")
            if c is None:
                continue
            orig_len = len(str(c))
            if orig_len <= args.max_chars:
                continue
            new_c, did = truncate(c, args.max_chars)
            if did:
                rec["content"] = new_c
                file_truncated += 1
                total_chars_saved += orig_len - len(str(new_c))
                changed = True

        if changed:
            total_files += 1
            total_truncated += file_truncated
            tag = "[dry-run] " if args.dry_run else ""
            print(f"{tag}{yf}: 截断 {file_truncated} 条")
            if not args.dry_run:
                shutil.copy2(yf, str(yf) + ".bak")
                yf.write_text(
                    yaml.safe_dump(
                        data,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

    print(
        f"\n汇总: {total_files} 个文件被改, 截断 {total_truncated} 条记录, "
        f"节省 ~{total_chars_saved} 字符"
    )
    if args.dry_run:
        print("(dry-run 模式，未写回；去掉 --dry-run 执行清理)")
    elif total_files:
        print("（原文件已备份为 .bak）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
