"""全景图校验层：frontmatter schema / 双链悬空 / type-status 矩阵。

真值：``docs/schemas/文档类型规范.md`` §一/§二/§六 + 方案 §4.4（校验三件）。
纯函数、无独立 CLI——方案 §4.4 红线"不存在只校验不出图的独立入口"：校验由
``graph_aggregate.py`` 三合一入口调用，数据文件是校验的顺手副产物。

执法范围 = **有 frontmatter 且声明了 doc_id** 的文档（存量渐进补齐口径：
无声明者无元数据可校验，由"新文档强制、存量触碰即补"的写面纪律兜底）。
type/status 为空串按"渐进补齐中间态"跳过枚举执法，只纠声明了非法值者。
"""

from __future__ import annotations

import graph_model as gm


def check_graph(
    data: dict,
    metas: list[gm.DocMeta],
    dangling: list[str],
) -> list[str]:
    """校验聚合结果，返回问题列表（空 = 绿）。

    ``data`` 为 viewer 契约形态（消费 ``edges`` 查取代边）；``metas`` 为
    文档半边 frontmatter 元数据；``dangling`` 为装配阶段记录的未解析双链。
    """
    findings: list[str] = []
    findings.extend(dangling)

    active = [m for m in metas if m.doc_id]

    # doc_id 冲突：同一 doc_id 声明在多份文档（节点 id 相撞，图数据被静默吞并）
    seen: dict[str, str] = {}
    for meta in active:
        if meta.doc_id in seen and seen[meta.doc_id] != meta.path:
            findings.append(f"doc_id 冲突: {meta.doc_id} 同时出现在 {seen[meta.doc_id]} 与 {meta.path}")
        else:
            seen.setdefault(meta.doc_id, meta.path)

    # superseded 的取代指向边集合（s 端 = 发起取代的文档）
    supersede_sources = {edge.get("s") for edge in data.get("edges", []) if edge.get("k") == gm.REL_SUPERSEDES}

    for meta in active:
        path = meta.path or meta.doc_id
        if not gm.DOC_ID_RE.match(meta.doc_id):
            findings.append(f"{path}: doc_id 形态非法: {meta.doc_id}（应为 YYYYMMDD-小写连字符串）")
        if meta.type and meta.type not in gm.DOC_TYPES:
            findings.append(f"{path}: type 非法: {meta.type}（允许 {sorted(gm.DOC_TYPES)}）")
        if meta.status and meta.status not in gm.DOC_STATUSES:
            findings.append(f"{path}: status 非法: {meta.status}（允许 {sorted(gm.DOC_STATUSES)}）")
        if meta.extra_fields:
            findings.append(
                f"{path}: frontmatter 未知字段 {list(meta.extra_fields)}"
                "（白名单 doc_id/type/status，关系数据走正文双链）"
            )
        findings.extend(_check_matrix(meta, path, supersede_sources))
    return findings


def _check_matrix(
    meta: gm.DocMeta,
    path: str,
    supersede_sources: set[str | None],
) -> list[str]:
    """type-status 合法矩阵（文档类型规范 §六）+ superseded 必有取代指向。"""
    findings: list[str] = []
    if meta.type == "plan" and meta.status in gm.DOC_STATUSES and meta.status not in {"draft", "archived", "deprecated"}:
        findings.append(f"{path}: plan 的 status 须为 draft/archived/deprecated，现 {meta.status}（§六矩阵）")
    if meta.type == "report" and meta.status == "draft":
        findings.append(f"{path}: report 产出即终态，不得 draft（§六矩阵）")
    if meta.status == "superseded" and meta.doc_id not in supersede_sources:
        findings.append(f"{path}: status=superseded 必有 [[目标|取代]] 双链指向（§六）")
    if meta.type == "adr" and meta.status == "deprecated":
        findings.append(
            f"{path}: adr 不得用 status=deprecated（ADR 退出语义 = superseded 被推翻 / archived 停用，§六矩阵）"
        )
    if meta.type == "standard" and not str(meta.path or "").startswith(gm.STANDARD_ROOTS):
        findings.append(
            f"{path}: type=standard 的归宿只有 docs/guides/ 与 docs/schemas/（§二类型=链位），现路径越界"
        )
    return findings
