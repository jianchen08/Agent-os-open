#!/usr/bin/env python
"""任务链可视化生成器。

读取 docs/tasks/ 下的任务文件（YAML frontmatter + Markdown 正文）和
.project/ 下的项目文档，生成单个自包含 HTML 页面。

设计要点：
- 纯标准库，无第三方依赖（frontmatter 用正则解析，避免引入 python-frontmatter）
- HTML 自包含：所有 CSS/JS 内联，可离线打开
- 依赖拓扑用内联 SVG 绘制，不依赖 CDN
- 所有来自任务文件的不可信值（task_id 等）一律经属性转义后放入 data-* 属性，
  绝不拼入内联事件处理器，杜绝 HTML/JS 注入

注意：本工具统计任务文件正文里出现的 AC 编号与 traces_to 追溯条目数，
用于快速感知 AC 覆盖规模；它不读取方案文档中的「方案级 AC 列表」，
因此不能判断某条方案级 AC 是否被任务覆盖。若需 AC 死角分析，请人工
对照方案文档核对。

调用：
    python generate_task_chain_html.py --title "项目名称"
"""

from __future__ import annotations

import argparse
import html
import json
import re
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── frontmatter 解析（最小实现，不依赖第三方） ──────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_OL_RE = re.compile(r"^\d+\.\s+")
_AC_REF_RE = re.compile(r"AC-?\d+")
_AC_TRACE_RE = re.compile(r"traces_to[:：]\s*(AC-?\d+)", re.IGNORECASE)


def _parse_yaml_block(text: str) -> dict[str, Any]:
    """极简 YAML 解析，只支持本 skill 用到的结构。

    覆盖：顶层标量、列表、嵌套列表项的简单字段。复杂结构退化为基础解析，
    避免引入 PyYAML 依赖（虽然项目有 pyyaml，但脚本独立运行时不应假设环境）。
    """
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    current_list: list[Any] = []

    def flush() -> None:
        nonlocal current_list_key, current_list
        if current_list_key is not None:
            result[current_list_key] = current_list
            current_list_key = None
            current_list = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        # 列表项 "- value"
        m_item = re.match(r"^-\s+(.*)$", line.strip())
        if m_item and current_list_key:
            val = m_item.group(1).strip().strip('"').strip("'")
            current_list.append(val)
            continue
        # key: value
        m_kv = re.compile(r"^([\w]+)\s*:\s*(.*)$").match(line.strip())
        if m_kv:
            flush()
            k, v = m_kv.group(1), m_kv.group(2).strip()
            if v == "":
                # 后续是列表
                current_list_key = k
                current_list = []
            else:
                result[k] = v.strip('"').strip("'")
                current_list_key = None
    flush()
    return result


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """返回 (frontmatter dict, 正文)。无 frontmatter 则返回 ({}, 全文)。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    return _parse_yaml_block(m.group(1)), m.group(2)


# ── 数据模型 ─────────────────────────────────────────


@dataclass
class Task:
    task_id: str
    task_name: str = ""
    executor: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    body: str = ""
    file_path: Path = field(default_factory=Path)
    # 从 frontmatter 的 acceptance_criteria 提取的 AC 概览（均为纯编号，可相互比较）
    ac_ids: list[str] = field(default_factory=list)
    ac_traces: list[str] = field(default_factory=list)  # traces_to 命中的 AC 编号


def load_tasks(tasks_dir: Path) -> list[Task]:
    """加载并按 task_id 排序所有任务文件。无法读取的文件输出警告并跳过。"""
    tasks: list[Task] = []
    for f in sorted(tasks_dir.glob("task_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"⚠️ 跳过无法读取的任务文件: {f}（{e}）", file=sys.stderr)
            continue
        fm, body = parse_frontmatter(text)
        task = Task(
            task_id=fm.get("task_id", f.stem),
            task_name=fm.get("task_name", ""),
            executor=fm.get("executor", ""),
            status=fm.get("status", "pending"),
            body=body,
            file_path=f,
        )
        # depends_on 可能是 "[t1, t2]" 字符串或已解析列表
        deps = fm.get("depends_on", [])
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.strip("[]").split(",") if d.strip()]
        task.depends_on = list(deps)
        # AC 编号统一规范化为大写，与 traces 同为纯编号，二者可比且不重复计数
        task.ac_ids = sorted({m.upper() for m in _AC_REF_RE.findall(body)})
        task.ac_traces = sorted({m.upper() for m in _AC_TRACE_RE.findall(body)})
        tasks.append(task)
    return tasks


def load_project_docs(project_dir: Path) -> list[dict[str, str]]:
    """加载项目文档（读全文，供点击后渲染）。无法读取的文件输出警告并跳过。"""
    docs: list[dict[str, str]] = []
    if not project_dir.exists():
        return docs
    for f in sorted(project_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"⚠️ 跳过无法读取的项目文档: {f}（{e}）", file=sys.stderr)
            continue
        # 取第一个 # 标题作为文档名
        title_m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        docs.append({
            "name": title_m.group(1).strip() if title_m else f.stem,
            "path": str(f),
            "body": text,
            "preview": text[:200].replace("\n", " ").strip(),
        })
    return docs


# ── 拓扑：层级布局（简单分层） ──────────────────────


def compute_layers(tasks: list[Task]) -> dict[str, int]:
    """按依赖计算每个任务的层级（无依赖=0，依赖最深者+1）。用于横向布局。

    检测到依赖环时输出 stderr 警告（环使层级计算不准确，通常是规划错误）。
    """
    by_id = {t.task_id: t for t in tasks}
    layer: dict[str, int] = {}
    cyclic_nodes: set[str] = set()

    def depth(tid: str, path: list[str]) -> int:
        if tid in layer:
            return layer[tid]
        if tid in path:  # 环：tid 在当前 DFS 路径上再次出现
            start = path.index(tid)
            cyclic_nodes.update(path[start:] + [tid])
            return 0
        task = by_id.get(tid)
        if not task or not task.depends_on:
            layer[tid] = 0
            return 0
        d = 1 + max(
            (depth(dep, path + [tid]) for dep in task.depends_on if dep in by_id),
            default=-1,
        )
        layer[tid] = max(d, 0)
        return layer[tid]

    for t in tasks:
        if t.task_id not in layer:
            depth(t.task_id, [])
    if cyclic_nodes:
        print(
            f"⚠️ 检测到依赖环（涉及 {len(cyclic_nodes)} 个任务），"
            f"层级计算可能不准确: {' → '.join(sorted(cyclic_nodes))}",
            file=sys.stderr,
        )
    return layer


# ── HTML 渲染辅助 ──────────────────────────────────

_STATUS_STYLE = {
    "completed": ("✅", "#16a34a", "#dcfce7"),
    "done": ("✅", "#16a34a", "#dcfce7"),
    "in_progress": ("🔵", "#2563eb", "#dbeafe"),
    "running": ("🔵", "#2563eb", "#dbeafe"),
    "pending": ("⬜", "#6b7280", "#f3f4f6"),
    "failed": ("❌", "#dc2626", "#fee2e2"),
    "blocked": ("🚫", "#d97706", "#fef3c7"),
}


def _esc(s: Any) -> str:
    """HTML 文本转义（用于元素内容）。"""
    return html.escape(str(s))


def _attr_esc(s: Any) -> str:
    """HTML 属性值转义：单双引号、尖括号、& 全部转义，防止脱离属性上下文。"""
    return html.escape(str(s), quote=True)


def markdown_to_html(md: str) -> str:
    """极简 Markdown → HTML 渲染（无外部依赖）。

    支持：标题(#/##/###)、无序列表(-/*)、有序列表(1.)、代码块(```)、
    GFM 表格、行内代码(`code`)、加粗(**x**)、段落。够展示项目文档。
    """
    # 先按代码块切分，代码块内不转义内部 markdown
    parts = re.split(r"```(\w*)\n(.*?)```", md, flags=re.DOTALL)
    out: list[str] = []
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if i + 1 < len(parts) and i % 3 == 0:
            # chunk 是普通文本段
            out.append(_render_md_block(chunk))
            # parts[i+1] = 语言, parts[i+2] = 代码内容
            if i + 2 < len(parts):
                code = _esc(parts[i + 2])
                out.append(f'<pre><code>{code}</code></pre>')
            i += 3
        else:
            out.append(_render_md_block(chunk))
            i += 1
    return "\n".join(out)


def _render_md_block(text: str) -> str:
    """渲染非代码块的 markdown 文本（标题/列表/表格/段落/行内）。"""
    out: list[str] = []
    # 先把表格段切出来整体渲染，剩余文本段走标题/列表/段落逻辑
    for seg_kind, seg_lines in _split_table_segments(text.split("\n")):
        if seg_kind == "table":
            out.append(_render_table(seg_lines))
        else:
            out.append(_render_text_lines(seg_lines))
    return "\n".join(out)


def _render_text_lines(lines: list[str]) -> str:
    """渲染标题/列表/段落（不含表格）。"""
    out: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            close_lists()
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(stripped[m.end(1)+1:])}</h{level}>")
            continue
        # 无序列表
        if re.match(r"^[-*+]\s+", stripped):
            if not in_ul:
                close_lists()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(stripped[2:].strip())}</li>")
            continue
        # 有序列表
        if re.match(r"^\d+\.\s+", stripped):
            if not in_ol:
                close_lists()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(_OL_RE.sub('', stripped))}</li>")
            continue
        # 普通段落
        close_lists()
        out.append(f"<p>{_inline(stripped)}</p>")
    close_lists()
    return "\n".join(out)


_TBL_ROW_RE = re.compile(r"^\|.*\|\s*$")
_TBL_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _render_table(rows: list[str]) -> str:
    """把 GFM 表格的原始行渲染成 HTML <table>。

    rows[0]=表头行，rows[1]=分隔行（含对齐标记），rows[2:]=数据行。
    对齐：:--- left、:---: center、---: right、--- left（默认）。
    """
    def cells(row: str) -> list[str]:
        parts = row.strip().strip("|").split("|")
        return [_inline(p.strip()) for p in parts]

    def align_of(spec: str) -> str:
        s = spec.strip()
        left, right = s.startswith(":"), s.endswith(":")
        return "center" if (left and right) else ("right" if right else "left")

    header = cells(rows[0])
    # 分隔单元格不渲染行内样式，仅用于解析对齐
    aligns = [align_of(c) for c in rows[1].strip().strip("|").split("|")]
    body = [cells(r) for r in rows[2:]]

    def style(i: int) -> str:
        a = aligns[i] if i < len(aligns) else "left"
        return f' style="text-align:{a}"'

    out = ['<table><thead><tr>']
    for i, h in enumerate(header):
        out.append(f"<th{style(i)}>{h}</th>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for i, c in enumerate(row):
            out.append(f"<td{style(i)}>{c}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _split_table_segments(lines: list[str]) -> list[tuple[str, list[str]]]:
    """把行序列切成 (类型, 行) 段。

    类型 'table' 的段含完整 GFM 表格行（表头+分隔+数据），其余为 'text'。
    后续对 text 段继续走标题/列表/段落逻辑，table 段整体渲染。
    """
    segments: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        # 表格触发：当前行是 | 行，且下一行是分隔行，且之后还有数据行
        if (
            _TBL_ROW_RE.match(lines[i])
            and i + 1 < n
            and _TBL_SEP_RE.match(lines[i + 1])
        ):
            if buf:
                segments.append(("text", buf))
                buf = []
            tbl = [lines[i], lines[i + 1]]
            j = i + 2
            while j < n and _TBL_ROW_RE.match(lines[j]):
                tbl.append(lines[j])
                j += 1
            segments.append(("table", tbl))
            i = j
            continue
        buf.append(lines[i])
        i += 1
    if buf:
        segments.append(("text", buf))
    return segments


def _inline(text: str) -> str:
    """行内渲染：行内代码、加粗。"""
    t = _esc(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def render_svg_graph(tasks: list[Task], layers: dict[str, int]) -> str:
    """渲染依赖拓扑图（内联 SVG，无外部依赖）。"""
    if not tasks:
        return '<p class="empty">暂无任务</p>'
    max_layer = max(layers.values(), default=0)
    # 每层节点数，用于纵向排布
    layer_counts: dict[int, int] = {}
    for t in tasks:
        layer_counts.setdefault(layers[t.task_id], 0)
        layer_counts[layers[t.task_id]] += 1
    max_col = max(layer_counts.values(), default=1)

    col_w, row_h = 220, 90
    margin = 40
    width = margin * 2 + (max_layer + 1) * col_w
    height = margin * 2 + max_col * row_h

    # 计算每个节点坐标
    pos: dict[str, tuple[float, float]] = {}
    cursor: dict[int, int] = {}
    for t in sorted(tasks, key=lambda x: (layers[x.task_id], x.task_id)):
        lay = layers[t.task_id]
        row = cursor.get(lay, 0)
        cursor[lay] = row + 1
        x = margin + lay * col_w
        y = margin + row * row_h
        pos[t.task_id] = (x, y)

    parts = [f'<svg class="dep-graph" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']

    # 先画边（依赖箭头）
    for t in tasks:
        if t.task_id not in pos:
            continue
        x1, y1 = pos[t.task_id]
        for dep in t.depends_on:
            if dep not in pos:
                continue
            x2, y2 = pos[dep]
            parts.append(
                f'<line x1="{x2 + 80}" y1="{y2 + 15}" x2="{x1}" y2="{y1 + 15}" '
                f'class="dep-edge" marker-end="url(#arrow)"/>'
            )

    # 箭头标记定义
    parts.append(
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker></defs>'
    )

    # 再画节点（task_id 经属性转义放入 data-task-id，不拼入内联事件）
    for t in tasks:
        if t.task_id not in pos:
            continue
        x, y = pos[t.task_id]
        icon, color, _ = _STATUS_STYLE.get(t.status, _STATUS_STYLE["pending"])
        label = t.task_name or t.task_id
        parts.append(
            f'<g class="dep-node" data-task-id="{_attr_esc(t.task_id)}">'
            f'<rect x="{x}" y="{y}" width="180" height="30" rx="6" '
            f'fill="white" stroke="{color}" stroke-width="2"/>'
            f'<text x="{x + 10}" y="{y + 20}" class="node-id">{_esc(t.task_id)}</text>'
            f'<title>{_esc(label)}</title>'
            f'</g>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


# ── 页面装配（render_html 的拆分子函数） ────────────


def _safe_json(obj: Any) -> str:
    """序列化为 JSON，并把 < 转义为 \\u003c，防止正文里的 </script> 截断脚本块。"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def _build_tasks_json(tasks: list[Task]) -> str:
    return _safe_json(
        [
            {
                "id": t.task_id,
                "name": t.task_name,
                "executor": t.executor,
                "status": t.status,
                "depends_on": t.depends_on,
                "ac_ids": t.ac_ids,
                "body": t.body,
                # body 在 Python 端预渲染成 html，避免 JS 端再渲染
                "html": markdown_to_html(t.body),
            }
            for t in tasks
        ]
    )


def _build_docs_json(docs: list[dict[str, str]]) -> str:
    return _safe_json(
        [
            {
                "name": d["name"],
                "path": d["path"],
                "body": d["body"],
                "html": markdown_to_html(d["body"]),
            }
            for d in docs
        ]
    )


@dataclass
class _Stats:
    total: int
    completed: int
    pct: int
    depth: int
    ac_total: int
    ac_traced: int
    doc_count: int


def _compute_stats(tasks: list[Task], layers: dict[str, int], doc_count: int) -> _Stats:
    status_counts: dict[str, int] = {}
    for t in tasks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1
    completed = status_counts.get("completed", 0) + status_counts.get("done", 0)
    total = len(tasks)
    # AC 覆盖：跨任务汇总编号集合（ac_ids / ac_traces 同为纯编号，可比较）
    all_ac: set[str] = set()
    all_traces: set[str] = set()
    for t in tasks:
        all_ac.update(t.ac_ids)
        all_traces.update(t.ac_traces)
    return _Stats(
        total=total,
        completed=completed,
        pct=int(completed / total * 100) if total else 0,
        depth=(max(layers.values(), default=0) + 1) if tasks else 0,
        ac_total=len(all_ac),
        ac_traced=len(all_traces),
        doc_count=doc_count,
    )


def _render_task_rows(tasks: list[Task]) -> str:
    if not tasks:
        return '<tr><td colspan="6" class="empty">暂无任务文件。请在 docs/tasks/ 下创建 task_XX_*.md</td></tr>'
    rows: list[str] = []
    for t in tasks:
        icon, color, bg = _STATUS_STYLE.get(t.status, _STATUS_STYLE["pending"])
        deps = ", ".join(t.depends_on) if t.depends_on else "—"
        rows.append(
            # task_id 经属性转义放入 data-task-id，由 JS 事件委托读取，不拼入内联事件
            f'<tr data-task-id="{_attr_esc(t.task_id)}" class="task-row">'
            f'<td><code>{_esc(t.task_id)}</code></td>'
            f'<td>{_esc(t.task_name or "—")}</td>'
            f'<td><span class="executor">{_esc(t.executor or "—")}</span></td>'
            f'<td><span class="status-badge" style="background:{bg};color:{color}">'
            f'{icon} {_esc(t.status)}</span></td>'
            f'<td class="deps">{_esc(deps)}</td>'
            f'<td>{len(t.ac_ids)}</td>'
            f"</tr>"
        )
    return "".join(rows)


def _render_doc_items(docs: list[dict[str, str]]) -> str:
    if not docs:
        return '<p class="empty">无项目文档</p>'
    return "\n".join(
        f'<div class="doc-card" onclick="showDoc({i})">'
        f'<div class="doc-name">{_esc(d["name"])}</div>'
        f'<div class="doc-path"><code>{_esc(d["path"])}</code></div>'
        f'<div class="doc-preview">{_esc(d["preview"])}…</div>'
        f'<div class="doc-click-hint">点击查看全文 →</div></div>'
        for i, d in enumerate(docs)
    )


# 页面模板：用 string.Template 占位（${var}），CSS/JS 中的 { } 无需转义。
# JS 不使用反引号模板字符串（正文含 ``` 会破坏之），所有动态文本走 esc() 或预渲染 html。
_HTML_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title} · 任务链</title>
<style>
  :root {
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #2563eb;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.6; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 28px 0 12px; border-bottom: 2px solid var(--accent); padding-bottom: 6px; }
  .subtitle { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-num { font-size: 28px; font-weight: 700; color: var(--accent); }
  .stat-label { font-size: 12px; color: var(--muted); }
  .progress-bar { height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; margin-top: 8px; }
  .progress-fill { height: 100%; background: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
  th { background: #f1f5f9; font-weight: 600; }
  .task-row { cursor: pointer; transition: background 0.15s; }
  .task-row:hover { background: #eff6ff; }
  code { background: #f1f5f9; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
  .executor { color: var(--accent); }
  .status-badge { padding: 2px 8px; border-radius: 12px; font-size: 12px; white-space: nowrap; }
  .deps { color: var(--muted); font-size: 12px; }
  .dep-graph { width: 100%; height: auto; max-height: 500px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; }
  .dep-node { cursor: pointer; }
  .dep-node:hover rect { fill: #eff6ff; }
  .node-id { font-size: 12px; font-weight: 600; fill: var(--text); }
  .dep-edge { stroke: #94a3b8; stroke-width: 1.5; fill: none; }
  .doc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
  .doc-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px;
              cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; }
  .doc-card:hover { border-color: var(--accent); box-shadow: 0 2px 8px rgba(37,99,235,0.12); }
  .doc-name { font-weight: 600; margin-bottom: 4px; }
  .doc-path { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .doc-preview { font-size: 12px; color: var(--muted); }
  .doc-click-hint { font-size: 11px; color: var(--accent); margin-top: 8px; font-weight: 600; }
  .empty { color: var(--muted); font-style: italic; padding: 12px; }
  /* 详情抽屉：固定头部 + 独立可滚动内容区，保证长内容能滚 */
  #drawer { position: fixed; top: 0; right: -520px; width: 500px; height: 100vh; background: var(--card);
             box-shadow: -4px 0 16px rgba(0,0,0,0.1); transition: right 0.25s;
             display: flex; flex-direction: column; z-index: 100; border-left: 1px solid var(--border); }
  #drawer.open { right: 0; }
  #overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.3); opacity: 0; pointer-events: none;
              transition: opacity 0.25s; z-index: 99; }
  #overlay.open { opacity: 1; pointer-events: auto; }
  /* 头部：固定不滚，含关闭按钮 */
  .drawer-header { position: relative; flex: 0 0 auto; padding: 20px 24px 12px; border-bottom: 1px solid var(--border); }
  .drawer-close { position: absolute; top: 16px; right: 16px; cursor: pointer; font-size: 22px; color: var(--muted); line-height: 1; }
  .drawer-close:hover { color: var(--text); }
  /* 内容区：独立滚动，padding 在此 */
  #drawer-content { flex: 1 1 auto; overflow-y: auto; padding: 20px 24px; min-height: 0; }
  .drawer-body { white-space: pre-wrap; font-size: 13px; }
  .drawer-body h1,.drawer-body h2,.drawer-body h3 { margin: 16px 0 8px; white-space: normal; }
  .drawer-body h1 { font-size: 18px; } .drawer-body h2 { font-size: 16px; } .drawer-body h3 { font-size: 14px; }
  .drawer-body ul,.drawer-body ol { margin: 6px 0; padding-left: 22px; white-space: normal; }
  .drawer-body li { margin: 3px 0; }
  .drawer-body pre { background: #f1f5f9; padding: 10px; border-radius: 6px; overflow-x: auto; white-space: pre; font-size: 12px; }
  .drawer-body code { background: #f1f5f9; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  .drawer-body pre code { background: none; padding: 0; }
  .drawer-body p { margin: 6px 0; white-space: normal; }
  .drawer-meta { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  .meta-chip { background: #f1f5f9; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
</style>
</head>
<body>
  <h1>${title}</h1>
  <div class="subtitle">任务链可视化 · 生成于 ${gen_time}</div>

  <div class="stats">
    <div class="stat-card"><div class="stat-num">${total}</div><div class="stat-label">任务总数</div></div>
    <div class="stat-card"><div class="stat-num">${completed}/${total}</div><div class="stat-label">已完成</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div></div>
    <div class="stat-card"><div class="stat-num">${depth}</div><div class="stat-label">依赖深度（层）</div></div>
    <div class="stat-card"><div class="stat-num">${ac_total}</div><div class="stat-label">AC 总数</div></div>
    <div class="stat-card"><div class="stat-num">${ac_traced}</div><div class="stat-label">有追溯的 AC</div></div>
    <div class="stat-card"><div class="stat-num">${doc_count}</div><div class="stat-label">项目文档</div></div>
  </div>

  <h2>任务概览</h2>
  <table>
    <thead><tr><th>ID</th><th>名称</th><th>执行者</th><th>状态</th><th>依赖</th><th>AC 数</th></tr></thead>
    <tbody>
      ${rows}
    </tbody>
  </table>

  <h2>依赖拓扑</h2>
  ${graph_svg}

  <h2>项目文档</h2>
  <div class="doc-grid">${doc_items}</div>

  <div id="overlay" onclick="closeDrawer()"></div>
  <div id="drawer">
    <div class="drawer-header">
      <span class="drawer-title" style="font-size:13px;color:var(--muted);font-weight:600;">详情</span>
      <span class="drawer-close" onclick="closeDrawer()">✕</span>
    </div>
    <div id="drawer-content"></div>
  </div>

<script>
const TASKS = ${tasks_json};
const DOCS = ${docs_json};
// 文本转义：textContent->innerHTML，防止标题/状态等动态文本里的 < > & 破坏 DOM。
// 正文 html 字段是 Python 端预渲染的安全 HTML，不走 esc，直接拼入 innerHTML。
function esc(s) {
  const d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '' : String(s);
  return d.innerHTML;
}
function openDrawer(content) {
  document.getElementById('drawer-content').innerHTML = content;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}
// 用普通字符串拼接构造抽屉内容，不用反引号模板字符串：
// 正文里大量 ``` 代码块反引号会提前终止模板字符串导致整个脚本语法错误、点击不渲染。
function showTask(id) {
  const t = TASKS.find(x => x.id === id);
  if (!t) return;
  const deps = t.depends_on.length ? t.depends_on.join(', ') : '无';
  const head = '<h2 style="margin-top:0">' + esc(t.name || t.id) + '</h2>'
    + '<div class="drawer-meta">'
    + '<span class="meta-chip"><code>' + esc(t.id) + '</code></span>'
    + '<span class="meta-chip">执行者: ' + esc(t.executor || '—') + '</span>'
    + '<span class="meta-chip">状态: ' + esc(t.status) + '</span>'
    + '<span class="meta-chip">依赖: ' + esc(deps) + '</span>'
    + '</div>';
  const body = '<div class="drawer-body">' + (t.html || esc(t.body) || '（无正文）') + '</div>';
  openDrawer(head + body);
}
function showDoc(index) {
  const d = DOCS[index];
  if (!d) return;
  const head = '<h2 style="margin-top:0">' + esc(d.name) + '</h2>'
    + '<div class="drawer-meta"><span class="meta-chip"><code>' + esc(d.path) + '</code></span></div>';
  const body = '<div class="drawer-body">' + (d.html || esc(d.body)) + '</div>';
  openDrawer(head + body);
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
}
// 事件委托：带 data-task-id 的元素（表格行、SVG 节点）点击后展示对应任务详情。
// 避免在内联 onclick 中拼接 task_id，杜绝 HTML/JS 注入。
document.addEventListener('click', function(e) {
  var el = e.target.closest('[data-task-id]');
  if (el) showTask(el.dataset.taskId);
});
</script>
</body>
</html>""")


def render_html(
    title: str,
    tasks: list[Task],
    project_docs: list[dict[str, str]],
    output: Path,
) -> None:
    """装配并写出 HTML 页面。只做编排，具体计算/渲染交给子函数。"""
    layers = compute_layers(tasks)
    stats = _compute_stats(tasks, layers, len(project_docs))
    page = _HTML_TEMPLATE.substitute(
        title=_esc(title),
        gen_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total=stats.total,
        completed=stats.completed,
        pct=stats.pct,
        depth=stats.depth,
        ac_total=stats.ac_total,
        ac_traced=stats.ac_traced,
        doc_count=stats.doc_count,
        rows=_render_task_rows(tasks),
        graph_svg=render_svg_graph(tasks, layers),
        doc_items=_render_doc_items(project_docs),
        tasks_json=_build_tasks_json(tasks),
        docs_json=_build_docs_json(project_docs),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


# ── CLI ────────────────────────────────────────────


def _sanitize_filename(name: str) -> str:
    """剥离路径分隔符与非法文件名字符，防止 title 注入输出路径。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "task_chain"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成任务链可视化 HTML")
    parser.add_argument("--title", default="项目任务链")
    parser.add_argument("--tasks-dir", default="docs/tasks/")
    parser.add_argument("--project-dir", default=".project/")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    tasks_dir = Path(args.tasks_dir)
    project_dir = Path(args.project_dir)
    if not tasks_dir.exists():
        print(f"⚠️ 任务目录不存在: {tasks_dir}", file=sys.stderr)

    tasks = load_tasks(tasks_dir)
    docs = load_project_docs(project_dir)

    safe_title = _sanitize_filename(args.title)
    out = Path(args.output) if args.output else Path("docs/working") / f"{safe_title}_task_chain.html"
    render_html(args.title, tasks, docs, out)
    print(f"✅ 生成 {len(tasks)} 个任务的可视化: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
