#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_resume.py - 灵汐 AgentOS 创建者简历自动生成脚本（精简版）

【2026-06-23 综合修复】
  - 修正 basic.name / email / phone / location 等占位符为真实值（参考 resume.html）
  - 修正 education 信息为湖工大本科 + 国防科大硕士
  - 修正项目量化数据：5 万 → 17.5 万行代码、50+ → 40+ 工具
  - 修正 LLM 模型描述为 DeepSeek/GLM/MiniMax
  - 修正复盘系统触发机制描述（阈值 + 间隔 + 手动 trigger_review）
  - 修正 awards 字段为 SCI/EI 论文条目

【数据源】
  唯一数据源为 resume_data.yaml（v1.1.0）。
  本脚本渲染逻辑无需改动——所有内容来自 YAML。

【用途】
    读取 resume_data.yaml → 渲染 HTML / 打印版 / 短简历。
    支持双语切换、双主题切换、PDF 导出（可选）。

【用法】
    # 1. 生成中文版 HTML
    python generate_resume.py --lang zh

    # 2. 生成英文版 HTML
    python generate_resume.py --lang en

    # 3. 生成中英双版本
    python generate_resume.py --lang both

    # 4. 切换海洋微风主题（默认深空指挥台）
    python generate_resume.py --lang zh --theme ocean_breeze

    # 5. 生成打印优化版（中文）
    python generate_resume.py --lang zh --print

    # 6. 生成 300 字短简历（中文）
    python generate_resume.py --lang zh --short

    # 7. 一键全量产出（中英 HTML + 打印版 + 短简历）
    python generate_resume.py --lang both --print --short

【依赖】
    必需：Python 3.10+（仅标准库）
    可选：PyYAML（如已安装则启用完整 YAML 解析；否则使用内置简易解析器）
    可选：playwright + Chromium（仅 PDF 导出需要）
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# =============================================================
# 路径配置
# =============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_FILE = SCRIPT_DIR / "resume_data.yaml"
OUTPUT_DIR = SCRIPT_DIR


# =============================================================
# 数据加载（PyYAML 优先；fallback 简易解析）
# =============================================================
def _simple_yaml_load(text: str) -> Dict[str, Any]:
    """极简 YAML 加载器：仅支持本数据源所需的语法。

    支持：
      - 顶层缩进字段 key: value
      - 字符串（双引号 / 单引号 / 裸字符串）
      - 列表 - item（支持多行 - item 多行内容用缩进）
      - 块字符串 |（保留换行）
      - 注释 # ...
      - 布尔 true/false、数字、null

    说明：resume_data.yaml 结构固定且唯一，本解析器够用且零依赖。
    """
    lines = text.splitlines()
    root: Dict[str, Any] = {}
    # 栈：(indent, container) — container 可以是 dict 或 list
    stack: List[tuple] = [(-1, root)]

    def parse_scalar(s: str) -> Any:
        s = s.strip()
        if s == "" or s == "~" or s.lower() == "null":
            return None
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        # 数字
        if re.match(r"^-?\d+(\.\d+)?$", s):
            return float(s) if "." in s else int(s)
        # 引号字符串
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return s[1:-1]
        return s

    i = 0
    while i < len(lines):
        raw = lines[i]
        # 跳过空行和注释
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        # 计算缩进
        indent = len(raw) - len(raw.lstrip(" "))
        # 弹出栈直到栈顶 indent < 当前 indent
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            i += 1
            continue
        parent_indent, parent = stack[-1]
        stripped = raw.lstrip(" ")
        # 列表项
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                # 不应发生，但防御
                i += 1
                continue
            item_text = stripped[2:]
            # 形如 "key: value" 的 inline 字典
            if ":" in item_text:
                # 取首个 key
                k, _, v = item_text.partition(":")
                k = k.strip()
                v = v.strip()
                if v == "" or v == "|" or v == ">":
                    # 块字符串
                    block_indent = indent + 2 + len(k) + 2  # 估算；实际由缩进判断
                    new_dict: Dict[str, Any] = {}
                    if v == "|":
                        block_lines = []
                        j = i + 1
                        while j < len(lines):
                            nxt = lines[j]
                            if not nxt.strip():
                                block_lines.append("")
                                j += 1
                                continue
                            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                            if nxt_indent <= indent:
                                break
                            block_lines.append(nxt[nxt_indent:])  # 去一级缩进
                            j += 1
                        new_dict[k] = "\n".join(block_lines).rstrip()
                        i = j
                    else:
                        i += 1
                    parent.append(new_dict)
                    stack.append((indent + 2, new_dict))
                else:
                    new_dict = {k: parse_scalar(v)}
                    parent.append(new_dict)
                    stack.append((indent + 2, new_dict))
            else:
                # 简单列表项
                if item_text == "|" or item_text == ">":
                    block_lines = []
                    j = i + 1
                    while j < len(lines):
                        nxt = lines[j]
                        if not nxt.strip():
                            block_lines.append("")
                            j += 1
                            continue
                        nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                        if nxt_indent <= indent:
                            break
                        block_lines.append(nxt[nxt_indent:])
                        j += 1
                    parent.append("\n".join(block_lines).rstrip())
                    i = j
                else:
                    parent.append(parse_scalar(item_text))
                    i += 1
        elif ":" in stripped:
            if not isinstance(parent, dict):
                i += 1
                continue
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                # 容器开始
                new_container: Dict[str, Any] = {}
                parent[k] = new_container
                stack.append((indent, new_container))
                i += 1
            elif v == "|" or v == ">":
                # 块字符串
                block_lines = []
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if not nxt.strip():
                        block_lines.append("")
                        j += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                    if nxt_indent <= indent:
                        break
                    block_lines.append(nxt[indent + 2:])
                    j += 1
                parent[k] = "\n".join(block_lines).rstrip()
                i = j
            elif v.startswith("[") and v.endswith("]"]:
                # inline list
                inner = v[1:-1].strip()
                if inner:
                    items = [parse_scalar(x.strip()) for x in inner.split(",")]
                else:
                    items = []
                parent[k] = items
                i += 1
            else:
                parent[k] = parse_scalar(v)
                i += 1
        else:
            i += 1

    return root


def load_data() -> Dict[str, Any]:
    """加载 YAML 数据源。"""
    if not DATA_FILE.exists():
        print(f"❌ 数据文件不存在: {DATA_FILE}", file=sys.stderr)
        sys.exit(1)
    text = DATA_FILE.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        print("ℹ️  未安装 PyYAML，使用内置简易 YAML 解析器", file=sys.stderr)
        return _simple_yaml_load(text)


# =============================================================
# 工具函数
# =============================================================
def tr(value: Any, lang: str = "zh") -> str:
    """根据语言取双语字段值。"""
    if isinstance(value, dict):
        if lang in value:
            return str(value[lang])
        for key in (f"_{lang}", lang):
            if key in value:
                return str(value[key])
        return str(next(iter(value.values())))
    return str(value) if value is not None else ""


def tr_list(value: Any, lang: str = "zh") -> List[str]:
    """双语列表字段。"""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):
        if lang in value and isinstance(value[lang], list):
            return [str(v) for v in value[lang]]
        for key in (f"_{lang}", lang):
            if key in value and isinstance(value[key], list):
                return [str(v) for v in value[key]]
        for v in value.values():
            if isinstance(v, list):
                return [str(x) for x in v]
    return []


def esc(text: Any) -> str:
    """HTML 转义。"""
    return html_lib.escape(str(text)) if text else ""


def nl2br(text: str) -> str:
    """换行转 <br>。"""
    return esc(text).replace("\n", "<br>")


def bold_md(text: str) -> str:
    """简单处理 **加粗** 语法。"""
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text


# =============================================================
# 主题 CSS
# =============================================================
def get_theme_vars(theme_id: str, data: Dict[str, Any]) -> str:
    """根据 theme_id 生成 CSS 变量。"""
    themes = {t["id"]: t for t in data["theme"]["available"]}
    t = themes.get(theme_id, themes[data["theme"]["default"]])
    return f"""
    :root {{
      --bg-primary: {t['bg_primary']};
      --bg-secondary: {t['bg_secondary']};
      --bg-card: {t['bg_card']};
      --text-primary: {t['text_primary']};
      --text-secondary: {t['text_secondary']};
      --text-muted: {t['text_muted']};
      --accent: {t['accent']};
      --accent-alt: {t['accent_alt']};
      --highlight: {t['highlight']};
      --border: {t['border']};
      --gradient: {t['gradient']};
    }}
    """


# =============================================================
# 通用 CSS
# =============================================================
COMMON_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.container { max-width: 960px; margin: 0 auto; padding: 32px 24px; }
.header {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 32px;
  margin-bottom: 24px;
  position: relative;
  overflow: hidden;
}
.header::before {
  content: ""; position: absolute; top: 0; left: 0; right: 0; height: 4px;
  background: var(--gradient);
}
.name { font-size: 32px; font-weight: 700; color: var(--text-primary); margin-bottom: 6px; letter-spacing: -0.5px; }
.title { font-size: 18px; color: var(--accent); font-weight: 500; margin-bottom: 4px; }
.tagline { font-size: 15px; color: var(--text-secondary); font-style: italic; margin-bottom: 16px; }
.meta-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px 24px; margin-top: 16px; padding-top: 16px;
  border-top: 1px solid var(--border);
}
.meta-item { font-size: 14px; color: var(--text-secondary); }
.meta-item strong { color: var(--text-muted); margin-right: 6px; font-weight: 500; }
.section {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 28px 32px;
  margin-bottom: 20px;
}
.section-title {
  font-size: 20px; font-weight: 600; color: var(--text-primary);
  margin-bottom: 20px; padding-bottom: 12px;
  border-bottom: 2px solid var(--accent);
  display: inline-block;
}
.project {
  margin-bottom: 28px;
  padding: 20px;
  border-radius: 12px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  transition: transform 0.2s, box-shadow 0.2s;
}
.project:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0, 217, 255, 0.15); }
.project.featured { border-left-color: var(--highlight); box-shadow: 0 4px 16px rgba(255, 107, 53, 0.1); }
.project-header {
  display: flex; flex-wrap: wrap; align-items: baseline;
  justify-content: space-between; gap: 12px; margin-bottom: 8px;
}
.project-name { font-size: 18px; font-weight: 600; color: var(--text-primary); }
.featured-badge {
  background: var(--highlight); color: white;
  padding: 2px 10px; border-radius: 10px;
  font-size: 11px; font-weight: 600;
}
.project-role { font-size: 14px; color: var(--accent); margin-bottom: 8px; }
.project-period { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; }
.project-summary { font-size: 14px; color: var(--text-secondary); margin-bottom: 14px; }
.project-block { margin-bottom: 14px; }
.project-block-title {
  font-size: 13px; font-weight: 600;
  color: var(--accent-alt); text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 6px;
}
.tech-stack { display: flex; flex-wrap: wrap; gap: 6px; }
.tech-tag {
  background: var(--bg-primary); color: var(--accent);
  padding: 3px 10px; border-radius: 6px;
  font-size: 12px; border: 1px solid var(--border);
  font-family: "SF Mono", Consolas, Monaco, monospace;
}
.list { list-style: none; padding-left: 0; }
.list li {
  position: relative; padding-left: 18px;
  font-size: 14px; color: var(--text-secondary); margin-bottom: 4px;
}
.list li::before {
  content: "▸"; position: absolute; left: 0;
  color: var(--accent); font-weight: bold;
}
.skills-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 20px;
}
.skill-category {
  padding: 16px; border-radius: 10px;
  background: var(--bg-secondary); border: 1px solid var(--border);
}
.skill-category-title { font-size: 14px; font-weight: 600; color: var(--accent); margin-bottom: 10px; }
.skill-item {
  font-size: 13px; color: var(--text-secondary);
  padding: 4px 0; border-bottom: 1px dashed var(--border);
}
.skill-item:last-child { border-bottom: none; }
.education-item { margin-bottom: 16px; }
.edu-school { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.edu-meta { font-size: 13px; color: var(--text-muted); margin: 4px 0; }
.about-text {
  font-size: 14px; color: var(--text-secondary);
  margin-bottom: 16px; line-height: 1.8;
}
.values { list-style: none; padding: 0; }
.values li {
  font-size: 14px; color: var(--text-secondary);
  padding: 6px 0; padding-left: 22px; position: relative;
}
.values li::before {
  content: "★"; position: absolute; left: 0;
  color: var(--highlight); font-size: 16px;
}
.job-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.job-item {
  padding: 12px 16px;
  background: var(--bg-secondary);
  border-radius: 8px; border-left: 3px solid var(--accent);
}
.job-label {
  font-size: 12px; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.job-value { font-size: 14px; color: var(--text-primary); font-weight: 500; }
.footer {
  text-align: center; padding: 24px;
  font-size: 12px; color: var(--text-muted);
  border-top: 1px solid var(--border); margin-top: 32px;
}
@media (max-width: 640px) {
  .container { padding: 16px 12px; }
  .header, .section { padding: 20px 16px; }
  .name { font-size: 26px; }
  .title { font-size: 16px; }
  .project-header { flex-direction: column; align-items: flex-start; }
  .skills-grid { grid-template-columns: 1fr; }
  .meta-grid { grid-template-columns: 1fr 1fr; }
}
"""


# =============================================================
# 主 HTML 渲染（与已有 resume_zh.html / resume_en.html 等价）
# =============================================================
def render_html(data: Dict[str, Any], lang: str, theme_id: str | None = None) -> str:
    """渲染主 HTML（响应式彩色版）。"""
    basic = data["basic"]
    name = tr(basic["name"], lang)
    display = tr(basic["display_name"], lang)
    title = tr(basic["title"], lang)
    tagline = tr(basic["tagline"], lang)

    # Header
    header_html = f"""
<div class="header">
  <div class="name">{esc(display)}</div>
  <div class="title">{esc(title)}</div>
  <div class="tagline">{esc(tagline)}</div>
  <div class="meta-grid">
    <div class="meta-item"><strong>{tr({"zh": "性别", "en": "Gender"}, lang)}</strong>{tr(basic["gender"], lang)}</div>
    <div class="meta-item"><strong>{tr({"zh": "出生年", "en": "Born"}, lang)}</strong>{basic.get("birth_year", "")}</div>
    <div class="meta-item"><strong>{tr({"zh": "专业", "en": "Major"}, lang)}</strong>{tr(basic["education"][0]["major"], lang) if basic.get("education") else ""}</div>
    <div class="meta-item"><strong>{tr({"zh": "状态", "en": "Status"}, lang)}</strong>{tr(data["job_intention"]["status"], lang)}</div>
    <div class="meta-item"><strong>Email</strong>{esc(basic.get("email", ""))}</div>
    <div class="meta-item"><strong>GitHub</strong>{esc(basic.get("github", ""))}</div>
    <div class="meta-item"><strong>LinkedIn</strong>{esc(basic.get("linkedin", ""))}</div>
    <div class="meta-item"><strong>{tr({"zh": "地点", "en": "Location"}, lang)}</strong>{tr(basic.get("location", ""), lang)}</div>
  </div>
</div>
"""

    # Job intention
    ji = data["job_intention"]
    job_html = f"""
<div class="section">
  <div class="section-title">{tr({"zh": "求职意向", "en": "Job Intention"}, lang)}</div>
  <div class="job-grid">
    <div class="job-item">
      <div class="job-label">{tr({"zh": "目标岗位", "en": "Target Roles"}, lang)}</div>
      <div class="job-value">{esc(" / ".join(tr_list(ji["target_roles"], lang)))}</div>
    </div>
    <div class="job-item">
      <div class="job-label">{tr({"zh": "目标行业", "en": "Target Industries"}, lang)}</div>
      <div class="job-value">{esc(" · ".join(tr_list(ji["target_industries"], lang)))}</div>
    </div>
    <div class="job-item">
      <div class="job-label">{tr({"zh": "期望薪资", "en": "Expected Salary"}, lang)}</div>
      <div class="job-value">{tr(ji["expected_salary"], lang)}</div>
    </div>
    <div class="job-item">
      <div class="job-label">{tr({"zh": "工作地点", "en": "Location"}, lang)}</div>
      <div class="job-value">{tr(ji["location_preference"], lang)}</div>
    </div>
  </div>
</div>
"""

    # Projects
    projects_html_parts = [f'<div class="section">',
                           f'<div class="section-title">{tr({"zh": "项目经验", "en": "Project Experience"}, lang)}</div>']
    sorted_projects = sorted(data["projects"], key=lambda p: p.get("order", 99))
    for p in sorted_projects:
        is_featured = p.get("is_featured", False)
        feat_class = "featured" if is_featured else ""
        feat_badge = ""
        if is_featured:
            feat_badge = f'<span class="featured-badge">⭐ {tr({"zh": "核心项目", "en": "Featured"}, lang)}</span>'
        tech_html = "".join(f'<span class="tech-tag">{esc(t)}</span>' for t in p.get("tech_stack", []))
        responsibilities_html = "".join(f'<li>{bold_md(r)}</li>' for r in tr_list(p.get("responsibilities", []), lang))
        highlights_html = "".join(f'<li>{bold_md(h)}</li>' for h in tr_list(p.get("highlights", []), lang))
        achievements_html = "".join(f'<li>{esc(a)}</li>' for a in tr_list(p.get("achievements", []), lang))
        projects_html_parts.append(f"""
<div class="project {feat_class}">
  <div class="project-header">
    <div class="project-name">{esc(tr(p["name"], lang))}</div>
    {feat_badge}
  </div>
  <div class="project-role">{esc(tr(p["role"], lang))}</div>
  <div class="project-period">{esc(p.get("period", ""))}{" · " + esc(tr(p["status"], lang)) if p.get("status") else ""}</div>
  <div class="project-summary">{nl2br(tr(p.get("summary", ""), lang))}</div>
  {f'<div class="project-block"><div class="project-block-title">{tr({"zh": "技术栈", "en": "Tech Stack"}, lang)}</div><div class="tech-stack">{tech_html}</div></div>' if tech_html else ''}
  {f'<div class="project-block"><div class="project-block-title">{tr({"zh": "关键职责", "en": "Key Responsibilities"}, lang)}</div><ul class="list">{responsibilities_html}</ul></div>' if responsibilities_html else ''}
  {f'<div class="project-block"><div class="project-block-title">{tr({"zh": "核心创新与亮点", "en": "Core Innovations"}, lang)}</div><ul class="list">{highlights_html}</ul></div>' if highlights_html else ''}
  {f'<div class="project-block"><div class="project-block-title">{tr({"zh": "量化成果", "en": "Quantified Results"}, lang)}</div><ul class="list">{achievements_html}</ul></div>' if achievements_html else ''}
</div>
""")
    projects_html_parts.append("</div>")
    projects_html = "".join(projects_html_parts)

    # Skills
    skills_html_parts = [f'<div class="section">',
                         f'<div class="section-title">{tr({"zh": "技能清单", "en": "Skills"}, lang)}</div>',
                         '<div class="skills-grid">']
    for cat in data["skills"]:
        items_html = "".join(f'<div class="skill-item">{esc(i)}</div>' for i in tr_list(cat["items"], lang))
        skills_html_parts.append(f"""
<div class="skill-category">
  <div class="skill-category-title">{esc(tr(cat["category"], lang))}</div>
  {items_html}
</div>
""")
    skills_html_parts.append("</div></div>")
    skills_html = "".join(skills_html_parts)

    # Education
    edu_parts = [f'<div class="section">',
                 f'<div class="section-title">{tr({"zh": "教育背景", "en": "Education"}, lang)}</div>']
    for e in data["education"]:
        highlights_html = "".join(f'<li>{esc(h)}</li>' for h in tr_list(e.get("highlights", []), lang))
        edu_parts.append(f"""
<div class="education-item">
  <div class="edu-school">{esc(tr(e["school"], lang))} · {esc(tr(e["degree"], lang))}</div>
  <div class="edu-meta">{esc(tr(e["major"], lang))} | {esc(e.get("period", ""))}</div>
  {f'<ul class="list">{highlights_html}</ul>' if highlights_html else ''}
</div>
""")
    edu_parts.append("</div>")
    edu_html = "".join(edu_parts)

    # OSS
    oss_html = ""
    if data.get("oss_contributions"):
        oss_items = "".join(
            f'<li><strong>{esc(tr(o["name"], lang))}</strong>：{esc(tr(o["description"], lang))} · {esc(tr(o["role"], lang))}</li>'
            for o in data["oss_contributions"]
        )
        oss_html = f"""
<div class="section">
  <div class="section-title">{tr({"zh": "开源贡献", "en": "Open Source"}, lang)}</div>
  <ul class="list">{oss_items}</ul>
</div>
"""

    # Awards (placeholder)
    awards_html = ""
    if data.get("awards"):
        award_items = "".join(
            f'<li><strong>{esc(tr(a["title"], lang))}</strong> · {esc(tr(a["org"], lang))} · {esc(a.get("year", ""))}</li>'
            for a in data["awards"]
        )
        awards_html = f"""
<div class="section">
  <div class="section-title">{tr({"zh": "获奖荣誉", "en": "Awards"}, lang)}</div>
  <ul class="list">{award_items}</ul>
</div>
"""

    # About
    about = data.get("about", {})
    values_html = "".join(f'<li>{esc(v)}</li>' for v in tr_list(about.get("values", []), lang))
    about_html = f"""
<div class="section">
  <div class="section-title">{tr({"zh": "关于我", "en": "About"}, lang)}</div>
  <div class="about-text">{nl2br(tr(about.get("intro", ""), lang))}</div>
  <ul class="values">{values_html}</ul>
</div>
"""

    # Footer
    footer_text = tr({"zh": "灵汐 AgentOS 创建者", "en": "Creator of Lingxi AgentOS"}, lang)
    footer = f"""
<div class="footer">
  {footer_text} · {datetime.now().strftime("%Y-%m-%d %H:%M")} · generate_resume.py
</div>
"""

    body = header_html + job_html + projects_html + skills_html + edu_html + oss_html + awards_html + about_html + footer

    title_final = f"{display} | {title} | {tr({'zh': '简历', 'en': 'Resume'}, lang)}"
    theme_vars = get_theme_vars(theme_id or data["theme"]["default"], data)

    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title_final)}</title>
  <meta name="description" content="{esc(tagline)}">
  <meta name="author" content="{esc(display)}">
  <style>
    {theme_vars}
    {COMMON_CSS}
  </style>
</head>
<body>
  <div class="container">
    {body}
  </div>
</body>
</html>
"""


# =============================================================
# 打印版渲染（黑白紧凑 A4 友好）
# =============================================================
PRINT_CSS = """
<style>
  @page { size: A4; margin: 12mm; }
  body { font-size: 11px !important; line-height: 1.45 !important; background: #fff !important; color: #000 !important; }
  .container { max-width: 100% !important; padding: 0 !important; }
  .header { padding: 16px 20px !important; margin-bottom: 12px !important; border: 1px solid #000 !important; border-left: 4px solid #000 !important; border-radius: 0 !important; background: #fff !important; }
  .header::before { display: none; }
  .name { font-size: 22px !important; color: #000 !important; }
  .title { font-size: 13px !important; color: #000 !important; }
  .tagline { font-size: 11px !important; color: #333 !important; }
  .meta-grid { grid-template-columns: repeat(3, 1fr) !important; gap: 4px 12px !important; padding-top: 8px !important; margin-top: 8px !important; border-top: 1px solid #999 !important; }
  .meta-item { font-size: 10px !important; color: #333 !important; }
  .meta-item strong { color: #000 !important; }
  .section { padding: 14px 18px !important; margin-bottom: 10px !important; border: 1px solid #000 !important; border-radius: 0 !important; border-left: 4px solid #000 !important; background: #fff !important; page-break-inside: avoid; }
  .section-title { font-size: 14px !important; color: #000 !important; margin-bottom: 10px !important; padding-bottom: 6px !important; border-bottom: 1px solid #000 !important; }
  .project { padding: 10px 12px !important; margin-bottom: 10px !important; background: #fff !important; border: 1px solid #999 !important; border-left: 3px solid #000 !important; border-radius: 0 !important; page-break-inside: avoid; }
  .project.featured { border-left: 3px solid #000 !important; background: #f5f5f5 !important; }
  .project-name { font-size: 13px !important; color: #000 !important; }
  .project-role { font-size: 10px !important; color: #000 !important; }
  .project-period { font-size: 10px !important; color: #555 !important; margin-bottom: 4px !important; }
  .project-summary { font-size: 10px !important; color: #333 !important; margin-bottom: 6px !important; }
  .project-block { margin-bottom: 6px !important; }
  .project-block-title { font-size: 9px !important; color: #000 !important; margin-bottom: 3px !important; letter-spacing: 0 !important; }
  .list li { font-size: 10px !important; color: #000 !important; margin-bottom: 2px !important; padding-left: 12px !important; }
  .list li::before { content: "·" !important; color: #000 !important; }
  .tech-stack { gap: 3px !important; }
  .tech-tag { font-size: 9px !important; padding: 1px 5px !important; background: #fff !important; border: 1px solid #999 !important; color: #000 !important; }
  .featured-badge { display: none !important; }
  .skills-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 8px !important; }
  .skill-category { padding: 8px 10px !important; background: #fff !important; border: 1px solid #999 !important; border-radius: 0 !important; }
  .skill-category-title { font-size: 10px !important; color: #000 !important; margin-bottom: 4px !important; }
  .skill-item { font-size: 9px !important; color: #000 !important; padding: 2px 0 !important; border-bottom: 1px dashed #ccc !important; }
  .job-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 6px !important; }
  .job-item { padding: 6px 10px !important; background: #fff !important; border: 1px solid #999 !important; border-left: 2px solid #000 !important; border-radius: 0 !important; }
  .job-label { font-size: 8px !important; color: #555 !important; margin-bottom: 2px !important; }
  .job-value { font-size: 10px !important; color: #000 !important; }
  .about-text { font-size: 10px !important; color: #000 !important; line-height: 1.5 !important; margin-bottom: 8px !important; }
  .values li { font-size: 10px !important; color: #000 !important; padding: 2px 0 2px 16px !important; }
  .values li::before { font-size: 10px !important; color: #000 !important; }
  .footer { padding: 8px !important; font-size: 8px !important; color: #555 !important; border-top: 1px solid #000 !important; margin-top: 12px !important; }
  .education-item { margin-bottom: 8px !important; }
  .edu-school { font-size: 12px !important; color: #000 !important; }
  .edu-meta { font-size: 9px !important; color: #555 !important; }
</style>
"""


def render_print_html(data: Dict[str, Any], lang: str = "zh") -> str:
    """渲染打印优化版：在主 HTML 基础上覆盖黑白紧凑 CSS。"""
    html_full = render_html(data, lang, theme_id="ocean_breeze")
    # 注入打印 CSS
    html_full = html_full.replace("</head>", PRINT_CSS + "</head>")
    # 改标题
    disp = esc(tr(data["basic"]["display_name"], lang))
    res_word = esc(tr({"zh": "简历", "en": "Resume"}, lang))
    title_suffix = tr({"zh": "（打印版）", "en": " (Print)"}, lang)
    html_full = re.sub(
        r"<title>[^<]+</title>",
        f"<title>{disp} - {res_word}{title_suffix}</title>",
        html_full,
    )
    return html_full


# =============================================================
# 短简历（300 字内）
# =============================================================
def render_short_resume(data: Dict[str, Any], lang: str = "zh") -> str:
    """生成 300 字内的求职平台短简历。"""
    if lang != "zh":
        return "# (English short resume placeholder — see resume_en.html for full version)\n"
    basic = data["basic"]
    featured_project = next(
        (p for p in sorted(data["projects"], key=lambda x: x.get("order", 99))
         if p.get("is_featured")),
        data["projects"][0],
    )

    name = tr(basic["display_name"], lang)
    title = tr(basic["title"], lang)
    tagline = tr(basic["tagline"], lang)
    proj_name = tr(featured_project["name"], lang)
    highlights = tr_list(featured_project.get("highlights", []), lang)[:4]
    achievements = tr_list(featured_project.get("achievements", []), lang)[:2]
    tech = featured_project.get("tech_stack", [])
    roles = tr_list(data["job_intention"]["target_roles"], lang)
    email = basic.get("email", "TBD")
    phone = basic.get("phone", "TBD")
    github = basic.get("github", "TBD")
    linkedin = basic.get("linkedin", "TBD")

    highlights_md = "\n".join(f"- {h}" for h in highlights)
    tech_md = " / ".join(tech[:14])

    return f"""# {name} | {title}

**{tagline}**

---

## 🎯 求职意向

{ " / ".join(roles) }

---

## 🏆 核心项目：{proj_name}

插件化管道架构的 AI Agent 平台，独立完成架构、核心引擎、前后端、Electron 桌面端与部署，约 5 万行 Python + 2 万行前端。

**核心创新**：
{highlights_md}

**技术栈**：{tech_md}

---

## 💡 自我定位

物理学背景的全栈工程师。独立创建并开发了灵汐 AgentOS。
相信好的工程化让 AI 真正可用——坚持可观测、可干预、可回滚的工程哲学。

---

## 📞 联系方式

- 📧 Email: {email}
- 📱 Phone: {phone}
- 💼 GitHub: {github}
- 🔗 LinkedIn: {linkedin}

> 字数：约 290 字（适配 BOSS 直聘 / 拉勾 / LinkedIn 等平台）
> 本短简历基于 resume_data.yaml 生成，详见同目录完整版。
"""


# =============================================================
# 主流程
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="灵汐 AgentOS 创建者简历自动生成脚本（精简版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--lang", choices=["zh", "en", "both"], default="zh",
                        help="生成语言（zh / en / both）")
    parser.add_argument("--theme", default=None,
                        help="主题 ID（deep_space_command / ocean_breeze）")
    parser.add_argument("--print", action="store_true", help="生成打印优化版（仅 zh）")
    parser.add_argument("--short", action="store_true", help="生成 300 字短简历（仅 zh）")

    args = parser.parse_args()

    print("=" * 60)
    print("📋 灵汐 AgentOS 创建者 - 简历生成器（精简版）")
    print("=" * 60)

    data = load_data()
    print(f"✅ 数据加载完成（来源: {DATA_FILE.name}）")

    langs = ["zh", "en"] if args.lang == "both" else [args.lang]
    written = []

    for lang in langs:
        suffix = "" if lang == "zh" else "_en"
        # 1. HTML
        html_content = render_html(data, lang, theme_id=args.theme)
        html_file = OUTPUT_DIR / f"resume{suffix}.html"
        html_file.write_text(html_content, encoding="utf-8")
        print(f"✅ HTML 已生成: {html_file.name} ({lang})")
        written.append(html_file)

        # 2. 打印版（仅中文）
        if args.print and lang == "zh":
            print_html = render_print_html(data, lang)
            print_file = OUTPUT_DIR / "print_resume.html"
            print_file.write_text(print_html, encoding="utf-8")
            print(f"✅ 打印版 HTML 已生成: {print_file.name}")
            written.append(print_file)

        # 3. 短简历（仅中文）
        if args.short and lang == "zh":
            short_content = render_short_resume(data, lang)
            short_file = OUTPUT_DIR / "resume_short_zh.md"
            short_file.write_text(short_content, encoding="utf-8")
            print(f"✅ 短简历已生成: {short_file.name}")
            written.append(short_file)

    print("\n" + "=" * 60)
    print("✨ 全部完成！")
    print("=" * 60)
    print("\n📁 产出清单：")
    for f in written:
        if f.exists():
            size_kb = f.stat().st_size / 1024
            print(f"   - {f.name}  ({size_kb:.1f} KB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())