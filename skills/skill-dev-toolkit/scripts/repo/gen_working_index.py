#!/usr/bin/env python3
"""docs/working/ 主题索引生成器（提案 P-29 ③；自动生成，禁手编）。

扫描 docs/working/**/*.md 按目录分层列举生成 docs/working/INDEX.md：
- 输出以三字段 frontmatter 开头（生成器常量产出，重生成不再剥手补字段）；
- 每文件附首个一级标题（无标题文件只列文件名）；
- docs/working/private/ 整域排除（个人材料不进索引、不导流）；
- 幂等全量重生成（内容只依赖文件集，无时间戳，重跑零 diff）。

用法：python scripts/gen_working_index.py [--root <仓库根>]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

EXCLUDED_DIRS = {"private"}
HEADER_NOTE = (
    "<!-- 自动生成：python scripts/gen_working_index.py —— 禁手编"
    "（提案 P-29 ③，复用基线总表自动生成模式；重跑即全量刷新） -->\n"
)

# INDEX.md frontmatter 三字段为生成器常量产出（F3拆账②：此前靠手补，重生成即剥字段）。
# 值与 scripts/graph_model.py 词表对应：report ∈ DOC_TYPES、active ∈ DOC_STATUSES，
# doc_id/type/status 三键 ∈ FRONTMATTER_FIELDS 白名单（doc-frontmatter/graph 闸可解析）。
INDEX_DOC_ID = "20261030-working-index"
INDEX_DOC_TYPE = "report"
INDEX_DOC_STATUS = "active"


def first_h1(path: Path) -> str:
    """取文件首个一级标题文本；无标题返回空串（编码容错）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("##"):
                    return stripped[2:].strip()
    except OSError:
        return ""
    return ""


def collect(working: Path) -> dict[str, list[Path]]:
    """按相对目录分组收集 .md 文件（排除 private/ 域）。返回 {相对目录: 文件列表}。"""
    grouped: dict[str, list[Path]] = defaultdict(list)
    for md in working.rglob("*.md"):
        rel = md.relative_to(working)
        if rel.parts[0] in EXCLUDED_DIRS:
            continue
        grouped["" if len(rel.parts) == 1 else str(rel.parent)].append(md)
    # 排序键平台不变量化（POSIX 语义 str 序）：Path 对象的内置排序在
    # Windows 大小写不敏感、Linux 敏感——INDEX 生成序跨平台分裂，
    # working-index-fresh 闸在 CI 恒红的真因（2026-11-06 三轮 CI 实证）。
    return {d: sorted(files, key=str) for d, files in grouped.items()}


def render(grouped: dict[str, list[Path]], working: Path) -> str:
    """渲染 INDEX.md 内容：frontmatter + 头注 + 总计 + 按目录分节列举。"""
    total = sum(len(files) for files in grouped.values())
    lines: list[str] = [
        "---",
        f"doc_id: {INDEX_DOC_ID}",
        f"type: {INDEX_DOC_TYPE}",
        f"status: {INDEX_DOC_STATUS}",
        "---",
        "",
        HEADER_NOTE.rstrip("\n"),
        "",
        "# docs/working 主题索引",
        "",
        f"共 {total} 篇 .md、{len(grouped)} 个目录层（含根层）。标题取各文件首个一级标题；",
        "`private/` 域个人材料不列入本索引。",
        "",
    ]
    for dir_name in sorted(grouped, key=lambda d: (d != "", str(d))):
        label = "根目录" if dir_name == "" else f"{dir_name}/"
        lines.append(f"## {label}")
        lines.append("")
        for md in grouped[dir_name]:
            rel_posix = md.relative_to(working).as_posix()
            title = first_h1(md)
            entry = f"- [{md.name}]({md.name})" if dir_name == "" else f"- [{md.name}](../{rel_posix})"
            lines.append(f"{entry} — {title}" if title else entry)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 docs/working/INDEX.md（自动生成，禁手编）")
    parser.add_argument("--root", help="仓库根（缺省 = 脚本所在仓）")
    ns = parser.parse_args(argv)
    root = Path(ns.root).resolve() if ns.root else Path(__file__).resolve().parent.parent
    working = root / "docs" / "working"
    if not working.is_dir():
        parser.error(f"目录不存在：{working}")
    index = working / "INDEX.md"
    index.write_text(render(collect(working), working), encoding="utf-8", newline="\n")
    print(f"gen_working_index: 已生成 {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
