"""P-14 待测双链计数器（提案：docs/working/全项目扫描改造提案_20261029.md P-14）。

P-14 缺口 = 关系数据（正文双链）仅零星文件在用、图关系网为空，且历史「4 件」
计数被终稿核查证伪（grep 命中实为形态描述文本，计数口径不可复现）。本脚本
产出可复跑的基线数，供 P-14 补写动作做前后对比。

口径（三真源，改动须同步）：

- 方案 §4.2/§4.3（``docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md``）
  —— 双链 = 正文三段式 ``[[目标|关系|说明]]``，关系词表开放，图边由机械扫描提取；
- ``scripts/graph_model.py`` 解析原语（``iter_wikilinks`` 内含 ``strip_code``：
  围栏代码块与行内代码内的写法示例不算——终稿核查 P-5 指认的口径污染面）；
- ``scripts/graph_aggregate.py`` 的 ``discover_files`` 源发现面取 ``docs/`` 子集
  （即含 rp_prompt_material/ 素材池排除——``[[thread]]`` 是素材模板语法非双链）。

分类（互斥穷尽，对齐 ``graph_aggregate`` 的边生成语义）：

- ``relation``：关系段非空——关系网数据本体（P-14 关心面）；
- ``bare_reference``：关系段空但目标形似真实引用（doc_id/路径/代码文件后缀）——
  图上按默认关系成边；
- ``placeholder``：关系段空且目标非形似引用——正文写法示例，聚合器忽略不计边。

确定性：输出为纯树状态函数——无时间戳、无 commit、全排序稳定，同树两次运行
逐字节一致（P-14 基线数可复跑对账的前提）。落盘时间与 HEAD 由使用者在报告
文档记录，不入机器输出。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import graph_aggregate  # noqa: E402
import graph_model as gm  # noqa: E402

#: P-14 落位面（提案 P-14：重要设计/两件真源 + 落地 ADR 补写双链）。
#: 路径按 E2-3 证据与终稿核查实测定位；缺失时如实记 ``missing: true``，不静默跳过。
P14_FOCUS_DIRS: tuple[str, ...] = ("docs/working/重要设计/",)
P14_FOCUS_FILES: tuple[str, ...] = (
    "docs/working/任务面板用户需求落地_台账.md",
    "docs/working/重要设计/AI辅助编程体系完整总结_修订版v3.md",
    "docs/decisions/2026-10-27-ai-coding-system-agentos-landing.md",
)

_KINDS: tuple[str, ...] = ("relation", "bare_reference", "placeholder")


def classify_link(target: str, relation: str) -> str:
    """单条双链分类：relation / bare_reference / placeholder（互斥穷尽）。"""
    if relation:
        return "relation"
    if gm.looks_like_reference(target):
        return "bare_reference"
    return "placeholder"


def count_file(root: Path, rel: str) -> dict:
    """单文件双链计数（frontmatter 剥离后计正文，与图提取器同读法）。"""
    text = (root / rel).read_text(encoding="utf-8", errors="replace")
    _, body = gm.parse_frontmatter(text)
    links = gm.iter_wikilinks(body)
    counts = {f"{kind}_links": 0 for kind in _KINDS}
    targets: dict[str, list[dict[str, object]]] = {kind: [] for kind in _KINDS}
    for target, relation, note in links:
        kind = classify_link(target, relation)
        counts[f"{kind}_links"] += 1
        targets[kind].append(
            {
                "target": target,
                "relation": relation,
                "note": note,
                "reference_like": bool(gm.looks_like_reference(target)),
            }
        )
    return {
        "path": rel,
        "links_total": len(links),
        **counts,
        "relation_targets": targets["relation"],
        "bare_reference_targets": targets["bare_reference"],
        "placeholder_targets": targets["placeholder"],
    }


def _empty_totals() -> dict[str, int]:
    return {
        "files_scanned": 0,
        "files_with_links": 0,
        "links_total": 0,
        "files_with_relation_links": 0,
        "relation_links_on_reference_targets": 0,
        **{f"{kind}_links": 0 for kind in _KINDS},
    }


def _absorb(totals: dict[str, int], entry: dict) -> None:
    """把单文件（或目录聚合的文件集合）计数并入累计器。"""
    totals["files_scanned"] += entry["files_scanned"]
    totals["files_with_links"] += entry["files_with_links"]
    totals["links_total"] += entry["links_total"]
    totals["files_with_relation_links"] += entry["files_with_relation_links"]
    totals["relation_links_on_reference_targets"] += entry["relation_links_on_reference_targets"]
    for kind in _KINDS:
        totals[f"{kind}_links"] += entry[f"{kind}_links"]


def _aggregate(paths: list[str], file_stats: dict[str, dict]) -> dict:
    """文件集合级聚合（目录分布与落位面目录条目共用）。"""
    totals = _empty_totals()
    for rel in paths:
        stats = file_stats[rel]
        totals["files_scanned"] += 1
        if stats["links_total"] > 0:
            totals["files_with_links"] += 1
        if stats["relation_links"] > 0:
            totals["files_with_relation_links"] += 1
        totals["links_total"] += stats["links_total"]
        totals["relation_links_on_reference_targets"] += sum(
            1 for item in stats["relation_targets"] if item["reference_like"]
        )
        for kind in _KINDS:
            totals[f"{kind}_links"] += stats[f"{kind}_links"]
    return totals


def _dir_group(rel: str) -> str:
    parent = rel.rsplit("/", 1)[0]
    return f"{parent}/" if parent else "docs/（根）"


def count_root(root: Path) -> dict:
    """按 P-14 口径对整个 docs/ 面计数，返回结构化结果（纯树状态函数）。"""
    doc_files = sorted(
        rel for rel in graph_aggregate.discover_files(root) if rel.startswith("docs/") and rel.endswith(".md")
    )
    file_stats = {rel: count_file(root, rel) for rel in doc_files}

    dir_groups = _group_counts(doc_files, file_stats)

    focus: list[dict] = []
    for dir_prefix in P14_FOCUS_DIRS:
        members = [rel for rel in doc_files if rel.startswith(dir_prefix)]
        entry = _aggregate(members, file_stats)
        focus.append({"path": dir_prefix, "kind": "dir", **entry})
    for rel in P14_FOCUS_FILES:
        if rel in file_stats:
            stats = file_stats[rel]
            focus.append(
                {
                    "path": rel,
                    "kind": "file",
                    "missing": False,
                    "files_scanned": 1,
                    "files_with_links": int(stats["links_total"] > 0),
                    "links_total": stats["links_total"],
                    "files_with_relation_links": int(stats["relation_links"] > 0),
                    "relation_links_on_reference_targets": sum(
                        1 for item in stats["relation_targets"] if item["reference_like"]
                    ),
                    **{f"{kind}_links": stats[f"{kind}_links"] for kind in _KINDS},
                }
            )
        else:
            missing = _empty_totals()
            missing["missing"] = True
            focus.append({"path": rel, "kind": "file", **missing})

    totals = _aggregate(doc_files, file_stats)
    return {
        "scope": {
            "discovery": "graph_aggregate.discover_files 取 docs/*.md 子集"
            "（含 rp_prompt_material/ 素材池排除，与图管线扫描面同源）",
            "parser": "graph_model.iter_wikilinks（strip_code 剔除代码块与行内代码内的形态描述文本）",
            "frontmatter": "graph_model.parse_frontmatter 剥离后计正文",
            "classification": {
                "relation": "关系段非空（关系网数据本体）",
                "bare_reference": "关系段空且目标形似真实引用（图上按默认关系成边）",
                "placeholder": "关系段空且目标非形似引用（正文写法示例，聚合器不计边）",
            },
        },
        "totals": totals,
        "by_directory": dir_groups,
        "p14_focus": focus,
        "files": [
            {key: stats[key] for key in stats if not key.endswith("_targets")}
            | {"relation_targets": stats["relation_targets"]}
            for stats in (file_stats[rel] for rel in doc_files)
            if stats["links_total"] > 0
        ],
    }


def _group_counts(doc_files: list[str], file_stats: dict[str, dict]) -> list[dict]:
    buckets: dict[str, list[str]] = {}
    for rel in doc_files:
        buckets.setdefault(_dir_group(rel), []).append(rel)
    return [{"path": group, **_aggregate(members, file_stats)} for group, members in sorted(buckets.items())]


def render_json(result: dict) -> str:
    """确定性 JSON 序列化（键全排序、固定缩进，同输入同字节）。"""
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_markdown(result: dict) -> str:
    """人读摘要表格（数据取自同一结构化结果，不另行计数）。"""
    totals = result["totals"]
    lines = [
        "# P-14 待测双链计数（机器产出摘要）",
        "",
        "口径：双链 = 正文三段式 `[[目标|关系|说明]]`（方案 §4.2/§4.3）；解析与"
        "扫描面与图管线同源（见 JSON scope 节）。分类之和恒等总数。",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 扫描文件数 | {totals['files_scanned']} |",
        f"| 含双链文件数 | {totals['files_with_links']} |",
        f"| 双链总数 | {totals['links_total']} |",
        f"| 关系双链（关系段非空） | {totals['relation_links']} |",
        f"| 关系双链·目标形似引用（成边候选） | {totals['relation_links_on_reference_targets']} |",
        f"| 裸引用双链 | {totals['bare_reference_links']} |",
        f"| 占位写法示例 | {totals['placeholder_links']} |",
        "",
        "## 目录分布",
        "",
        "| 目录 | 扫描 | 含链 | 双链 | 关系 | 裸引用 | 占位 |",
        "|---|---|---|---|---|---|---|",
    ]
    for group in result["by_directory"]:
        lines.append(
            f"| {group['path']} | {group['files_scanned']} | {group['files_with_links']}"
            f" | {group['links_total']} | {group['relation_links']}"
            f" | {group['bare_reference_links']} | {group['placeholder_links']} |"
        )
    lines += [
        "",
        "## P-14 落位面",
        "",
        "| 路径 | 扫描 | 含链 | 双链 | 关系 | 备注 |",
        "|---|---|---|---|---|---|",
    ]
    for entry in result["p14_focus"]:
        note = "文件缺失（记 missing，未静默跳过）" if entry.get("missing") else ""
        lines.append(
            f"| {entry['path']} | {entry['files_scanned']} | {entry['files_with_links']}"
            f" | {entry['links_total']} | {entry['relation_links']} | {note} |"
        )
    lines += [
        "",
        "## 含双链文件明细",
        "",
        "| 文件 | 关系 | 裸引用 | 占位 | 合计 |",
        "|---|---|---|---|---|",
    ]
    for stats in result["files"]:
        lines.append(
            f"| {stats['path']} | {stats['relation_links']}"
            f" | {stats['bare_reference_links']} | {stats['placeholder_links']}"
            f" | {stats['links_total']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P-14 待测双链计数（提案 20261029 P-14 基线量化）")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="仓库根目录（默认：脚本上级目录）",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="stdout 输出形态",
    )
    parser.add_argument("--json", dest="json_path", help="JSON 结果落盘路径（可选）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"--root 不存在或非目录：{root}")

    result = count_root(root)
    output = render_json(result) if args.format == "json" else render_markdown(result)
    sys.stdout.write(output)
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(render_json(result), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
