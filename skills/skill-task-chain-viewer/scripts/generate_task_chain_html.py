#!/usr/bin/env python
"""任务链可视化生成器。

读取 docs/tasks/ 下的任务文件（YAML frontmatter + Markdown 正文）和
.project/ 下的项目文档，生成单个自包含 HTML 页面。

设计要点：
- 纯标准库，无第三方依赖（frontmatter 用正则解析，避免引入 python-frontmatter）
- HTML 自包含：所有 CSS/JS 内联，可离线打开
- 依赖拓扑用内联 SVG 绘制，不依赖 CDN
- AC 追溯：把任务级 AC 的 traces_to 汇总，暴露「方案级 AC 无人覆盖」的死角

调用：
    python generate_task_chain_html.py --title "项目名称"
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── frontmatter 解析（最小实现，不依赖第三方） ──────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


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
        m_kv = re.match(r"^([\w]+)\s*:\s*(.*)$", line.strip())
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
    # 从 frontmatter 的 acceptance_criteria 提取的 AC 概览
    ac_ids: list[str] = field(default_factory=list)
    ac_traces: list[str] = field(default_factory=list)  # traces_to 目标


def load_tasks(tasks_dir: Path) -> list[Task]:
    """加载并按 task_id 排序所有任务文件。"""
    tasks: list[Task] = []
    for f in sorted(tasks_dir.glob("task_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
        # 从正文提取 AC-ID 和 traces_to（正文里有 "AC-x（traces_to: AC-y）"）
        task.ac_ids = sorted(set(re.findall(r"AC-?\d+", body)))
        task.ac_traces = sorted(
            set(re.findall(r"traces_to[:：]\s*AC-?\d+", body, re.IGNORECASE))
        )
        tasks.append(task)
    return tasks


def load_project_docs(project_dir: Path) -> list[dict[str, str]]:
    """加载项目文档（仅取标题和前几行摘要）。"""
    docs: list[dict[str, str]] = []
    if not project_dir.exists():
        return docs
    for f in sorted(project_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # 取第一个 # 标题作为文档名
        title_m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        docs.append({
            "name": title_m.group(1).strip() if title_m else f.stem,
            "path": str(f),
            "preview": text[:200].replace("\n", " ").strip(),
        })
    return docs


# ── 拓扑：层级布局（简单分层） ──────────────────────


def compute_layers(tasks: list[Task]) -> dict[str, int]:
    """按依赖计算每个任务的层级（无依赖=0，依赖最深者+1）。用于横向布局。"""
    by_id = {t.task_id: t for t in tasks}
    layer: dict[str, int] = {}

    def depth(tid: str, seen: set[str]) -> int:
        if tid in layer:
            return layer[tid]
        if tid in seen:  # 环保护
            return 0
        seen.add(tid)
        task = by_id.get(tid)
        if not task or not task.depends_on:
            layer[tid] = 0
            return 0
        d = 1 + max((depth(dep, seen) for dep in task.depends_on if dep in by_id), default=-1)
        layer[tid] = max(d, 0)
        return layer[tid]

    for t in tasks:
        depth(t.task_id, set())
    return layer


# ── HTML 渲染 ──────────────────────────────────────

_STATUS_STYLE = {
    "completed": ("✅", "#16a34a", "#dcfce7"),
    "done": ("✅", "#16a34a", "#dcfce7"),
    "in_progress": ("🔵", "#2563eb", "#dbeafe"),
    "running": ("🔵", "#2563eb", "#dbeafe"),
    "pending": ("⬜", "#6b7280", "#f3f4f6"),
    "failed": ("❌", "#dc2626", "#fee2e2"),
    "blocked": ("🚫", "#d97706", "#fef3c7"),
}


def _esc(s: str) -> str:
    return html.escape(str(s))


def render_svg_graph(tasks: list[Task], layers: dict[str, int]) -> str:
    """渲染依赖拓扑图（内联 SVG，无外部依赖）。"""
    if not tasks:
        return '<p class="empty">暂无任务</p>'
    by_id = {t.task_id: t for t in tasks}
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

    # 再画节点
    for t in tasks:
        if t.task_id not in pos:
            continue
        x, y = pos[t.task_id]
        icon, color, _ = _STATUS_STYLE.get(t.status, _STATUS_STYLE["pending"])
        label = t.task_name or t.task_id
        parts.append(
            f'<g class="dep-node" onclick="showTask(\'{t.task_id}\')">'
            f'<rect x="{x}" y="{y}" width="180" height="30" rx="6" '
            f'fill="white" stroke="{color}" stroke-width="2"/>'
            f'<text x="{x + 10}" y="{y + 20}" class="node-id">{_esc(t.task_id)}</text>'
            f'<title>{_esc(label)}</title>'
            f'</g>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def render_html(
    title: str,
    tasks: list[Task],
    project_docs: list[dict[str, str]],
    output: Path,
) -> None:
    layers = compute_layers(tasks)
    total = len(tasks)
    status_counts: dict[str, int] = {}
    for t in tasks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1
    completed = status_counts.get("completed", 0) + status_counts.get("done", 0)

    # AC 追溯覆盖
    all_traces = set()
    for t in tasks:
        all_traces.update(t.ac_traces)
    all_ac = set()
    for t in tasks:
        all_ac.update(t.ac_ids)

    # 任务详情数据（JSON 给 JS）
    import json
    tasks_json = json.dumps(
        [
            {
                "id": t.task_id,
                "name": t.task_name,
                "executor": t.executor,
                "status": t.status,
                "depends_on": t.depends_on,
                "ac_ids": t.ac_ids,
                "body": t.body,
            }
            for t in tasks
        ],
        ensure_ascii=False,
    )

    rows = []
    for t in tasks:
        icon, color, bg = _STATUS_STYLE.get(t.status, _STATUS_STYLE["pending"])
        deps = ", ".join(t.depends_on) if t.depends_on else "—"
        rows.append(
            f'<tr onclick="showTask(\'{t.task_id}\')" class="task-row">'
            f'<td><code>{_esc(t.task_id)}</code></td>'
            f'<td>{_esc(t.task_name or "—")}</td>'
            f'<td><span class="executor">{_esc(t.executor or "—")}</span></td>'
            f'<td><span class="status-badge" style="background:{bg};color:{color}">'
            f'{icon} {_esc(t.status)}</span></td>'
            f'<td class="deps">{_esc(deps)}</td>'
            f'<td>{len(t.ac_ids)}</td>'
            f"</tr>"
        )

    doc_items = "\n".join(
        f'<div class="doc-card"><div class="doc-name">{_esc(d["name"])}</div>'
        f'<div class="doc-path"><code>{_esc(d["path"])}</code></div>'
        f'<div class="doc-preview">{_esc(d["preview"])}…</div></div>'
        for d in project_docs
    ) or '<p class="empty">无项目文档</p>'

    graph_svg = render_svg_graph(tasks, layers)
    pct = int(completed / total * 100) if total else 0
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} · 任务链</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #2563eb;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.6; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 28px 0 12px; border-bottom: 2px solid var(--accent); padding-bottom: 6px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 20px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }}
  .stat-num {{ font-size: 28px; font-weight: 700; color: var(--accent); }}
  .stat-label {{ font-size: 12px; color: var(--muted); }}
  .progress-bar {{ height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; margin-top: 8px; }}
  .progress-fill {{ height: 100%; background: var(--accent); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  .task-row {{ cursor: pointer; transition: background 0.15s; }}
  .task-row:hover {{ background: #eff6ff; }}
  code {{ background: #f1f5f9; padding: 1px 6px; border-radius: 4px; font-size: 12px; }}
  .executor {{ color: var(--accent); }}
  .status-badge {{ padding: 2px 8px; border-radius: 12px; font-size: 12px; white-space: nowrap; }}
  .deps {{ color: var(--muted); font-size: 12px; }}
  .dep-graph {{ width: 100%; height: auto; max-height: 500px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; }}
  .dep-node {{ cursor: pointer; }}
  .dep-node:hover rect {{ fill: #eff6ff; }}
  .node-id {{ font-size: 12px; font-weight: 600; fill: var(--text); }}
  .dep-edge {{ stroke: #94a3b8; stroke-width: 1.5; fill: none; }}
  .doc-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
  .doc-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }}
  .doc-name {{ font-weight: 600; margin-bottom: 4px; }}
  .doc-path {{ font-size: 11px; color: var(--muted); margin-bottom: 6px; }}
  .doc-preview {{ font-size: 12px; color: var(--muted); }}
  .empty {{ color: var(--muted); font-style: italic; padding: 12px; }}
  /* 详情抽屉 */
  #drawer {{ position: fixed; top: 0; right: -520px; width: 500px; height: 100vh; background: var(--card);
             box-shadow: -4px 0 16px rgba(0,0,0,0.1); transition: right 0.25s; padding: 24px;
             overflow-y: auto; z-index: 100; border-left: 1px solid var(--border); }}
  #drawer.open {{ right: 0; }}
  #overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.3); opacity: 0; pointer-events: none;
              transition: opacity 0.25s; z-index: 99; }}
  #overlay.open {{ opacity: 1; pointer-events: auto; }}
  .drawer-close {{ position: absolute; top: 16px; right: 16px; cursor: pointer; font-size: 20px; color: var(--muted); }}
  .drawer-body {{ white-space: pre-wrap; font-size: 13px; }}
  .drawer-meta {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }}
  .meta-chip {{ background: #f1f5f9; padding: 2px 8px; border-radius: 12px; font-size: 12px; }}
</style>
</head>
<body>
  <h1>{_esc(title)}</h1>
  <div class="subtitle">任务链可视化 · 生成于 {gen_time}</div>

  <div class="stats">
    <div class="stat-card"><div class="stat-num">{total}</div><div class="stat-label">任务总数</div></div>
    <div class="stat-card"><div class="stat-num">{completed}/{total}</div><div class="stat-label">已完成</div>
      <div class="progress-bar"><div class="progress-fill" style="width:{pct}%"></div></div></div>
    <div class="stat-card"><div class="stat-num">{max(layers.values(), default=0)+1 if tasks else 0}</div><div class="stat-label">依赖深度（层）</div></div>
    <div class="stat-card"><div class="stat-num">{len(all_ac)}</div><div class="stat-label">AC 总数</div></div>
    <div class="stat-card"><div class="stat-num">{len(all_traces)}</div><div class="stat-label">有追溯的 AC</div></div>
    <div class="stat-card"><div class="stat-num">{len(project_docs)}</div><div class="stat-label">项目文档</div></div>
  </div>

  <h2>任务概览</h2>
  <table>
    <thead><tr><th>ID</th><th>名称</th><th>执行者</th><th>状态</th><th>依赖</th><th>AC 数</th></tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="6" class="empty">暂无任务文件。请在 docs/tasks/ 下创建 task_XX_*.md</td></tr>'}
    </tbody>
  </table>

  <h2>依赖拓扑</h2>
  {graph_svg}

  <h2>项目文档</h2>
  <div class="doc-grid">{doc_items}</div>

  <div id="overlay" onclick="closeDrawer()"></div>
  <div id="drawer">
    <span class="drawer-close" onclick="closeDrawer()">✕</span>
    <div id="drawer-content"></div>
  </div>

<script>
const TASKS = {tasks_json};
function showTask(id) {{
  const t = TASKS.find(x => x.id === id);
  if (!t) return;
  const deps = t.depends_on.length ? t.depends_on.join(', ') : '无';
  document.getElementById('drawer-content').innerHTML = `
    <h2 style="margin-top:0">${{t.name || t.id}}</h2>
    <div class="drawer-meta">
      <span class="meta-chip"><code>${{t.id}}</code></span>
      <span class="meta-chip">执行者: ${{t.executor || '—'}}</span>
      <span class="meta-chip">状态: ${{t.status}}</span>
      <span class="meta-chip">依赖: ${{deps}}</span>
    </div>
    <div class="drawer-body">${{t.body || '（无正文）'}}</div>
  `;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}}
function closeDrawer() {{
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
}}
</script>
</body>
</html>"""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


# ── CLI ────────────────────────────────────────────


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

    out = Path(args.output) if args.output else Path("docs/working") / f"{args.title}_task_chain.html"
    render_html(args.title, tasks, docs, out)
    print(f"✅ 生成 {len(tasks)} 个任务的可视化: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
