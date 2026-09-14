"""文档读取器——md/docx/pdf → Block[] 中间表示。

成熟模式移植（不自己造解析器）：
- Markdown：markdown-it-py token 流（CommonMark + GFM 表格）
- Word：python-docx 官方 API（iter_inner_content 按文档顺序遍历段落/表格）
- PDF：pypdf 文本提取（无文本层显式报错，不静默返回空；加密/损坏显式失败）

[来源: markdown-it-py 官方 README（token 流解析）；python-docx 官方文档
 （Document.iter_inner_content / Paragraph.style）；pypdf 官方文档（PdfReader）]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from doc_core import MAX_PDF_PAGES, Block, DocumentError

# ── Markdown 读取 ─────────────────────────────────────────────


def read_markdown(text: str) -> list[Block]:
    """Markdown 文本 → Block[]（markdown-it-py token 流解析）。"""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("table")
    return _tokens_to_blocks(md.parse(text))


def _inline_text(token: Any) -> str:
    """inline token → 文本（text/code_inline/softbreak/image 处理）。"""
    if token is None:
        return ""
    parts: list[str] = []
    for child in token.children or []:
        if child.type == "text":
            parts.append(child.content)
        elif child.type == "code_inline":
            parts.append(f"`{child.content}`")
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content or child.attrGet("alt") or "")
    return "".join(parts)


def _tokens_to_blocks(tokens: list[Any]) -> list[Block]:
    """markdown-it token 流 → Block[]（标题/段落/列表/代码块/引用/表格）。"""
    blocks: list[Block] = []
    i = 0
    n = len(tokens)
    list_depth = 0
    ordered_stack: list[bool] = []
    in_quote = False
    table_rows: list[list[str]] | None = None
    current_row: list[str] | None = None
    while i < n:
        tok = tokens[i]
        ttype = tok.type
        if ttype == "heading_open":
            level = int(tok.tag[1:]) if tok.tag[1:].isdigit() else 1
            blocks.append(Block(kind="heading", text=_inline_text(tokens[i + 1]), level=level))
            i += 3
            continue
        if ttype == "paragraph_open":
            text = _inline_text(tokens[i + 1])
            if in_quote:
                blocks.append(Block(kind="quote", text=text))
            elif list_depth > 0:
                kind = "ordered" if ordered_stack[-1] else "bullet"
                blocks.append(Block(kind=kind, text=text, level=list_depth))
            else:
                blocks.append(Block(kind="paragraph", text=text))
            i += 3
            continue
        if ttype == "bullet_list_open":
            list_depth += 1
            ordered_stack.append(False)
            i += 1
            continue
        if ttype == "ordered_list_open":
            list_depth += 1
            ordered_stack.append(True)
            i += 1
            continue
        if ttype in ("bullet_list_close", "ordered_list_close"):
            list_depth = max(0, list_depth - 1)
            if ordered_stack:
                ordered_stack.pop()
            i += 1
            continue
        if ttype == "blockquote_open":
            in_quote = True
            i += 1
            continue
        if ttype == "blockquote_close":
            in_quote = False
            i += 1
            continue
        if ttype in ("fence", "code_block"):
            blocks.append(
                Block(kind="code", text=tok.content.rstrip("\n"), info=(tok.info or "").strip())
            )
            i += 1
            continue
        if ttype == "table_open":
            table_rows = []
            i += 1
            continue
        if ttype == "tr_open":
            current_row = []
            i += 1
            continue
        if ttype in ("th_open", "td_open"):
            if current_row is not None:
                current_row.append(_inline_text(tokens[i + 1]))
            i += 3
            continue
        if ttype == "tr_close":
            if table_rows is not None and current_row is not None:
                table_rows.append(current_row)
            current_row = None
            i += 1
            continue
        if ttype == "table_close":
            if table_rows:
                blocks.append(Block(kind="table", rows=table_rows))
            table_rows = None
            i += 1
            continue
        i += 1
    return blocks


# ── Word 读取 ─────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(?:Heading|标题)\s*(\d+)$")


def read_docx(path: Path) -> list[Block]:
    """Word 文档 → Block[]（python-docx 官方 API，iter_inner_content 保序）。"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(str(path))
        items = list(doc.iter_inner_content())
    except Exception as e:  # noqa: BLE001 - 解析失败统一转可操作错误
        raise DocumentError(
            f"无法解析 Word 文档（文件可能已损坏或非 .docx 格式）：{path.name}"
            f"（{type(e).__name__}: {e}）"
        ) from e
    blocks: list[Block] = []
    for item in items:
        if isinstance(item, Paragraph):
            block = _docx_paragraph_to_block(item)
            if block is not None:
                blocks.append(block)
        elif isinstance(item, Table):
            rows = [[cell.text.strip() for cell in row.cells] for row in item.rows]
            if rows:
                blocks.append(Block(kind="table", rows=rows))
    return blocks


def _docx_paragraph_to_block(para: Any) -> Block | None:
    """段落 → Block（标题/列表/引用按样式映射；空段落跳过）。"""
    text = _docx_runs_text(para)
    if not text.strip():
        return None
    style = para.style
    name = (style.name or "") if style is not None else ""
    style_id = (style.style_id or "") if style is not None else ""
    m = _HEADING_RE.match(name) or _HEADING_RE.match(style_id)
    if m:
        return Block(kind="heading", text=text, level=min(int(m.group(1)), 6))
    if name.startswith("List Bullet") or style_id.startswith("ListBullet"):
        return Block(kind="bullet", text=text, level=1)
    if name.startswith("List Number") or style_id.startswith("ListNumber"):
        return Block(kind="ordered", text=text, level=1)
    if name.startswith("Quote") or style_id.startswith("Quote"):
        return Block(kind="quote", text=text)
    return Block(kind="paragraph", text=text)


def _docx_runs_text(para: Any) -> str:
    """段落 runs → 带 md 内联标记的文本（粗体/斜体保留）。"""
    parts: list[str] = []
    for run in para.runs:
        text = run.text
        if not text:
            continue
        if run.bold and run.italic:
            text = f"***{text}***"
        elif run.bold:
            text = f"**{text}**"
        elif run.italic:
            text = f"*{text}*"
        parts.append(text)
    return "".join(parts)


# ── PDF 读取 ──────────────────────────────────────────────────


def read_pdf(path: Path) -> list[Block]:
    """PDF → Block[]（pypdf 文本提取；加密/损坏/无文本层显式报错）。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocumentError(f"PDF 已加密，无法读取：{path.name}。请先解密后重试。")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise DocumentError(
                f"PDF 页数过多（{page_count} 页，上限 {MAX_PDF_PAGES} 页）：{path.name}。请拆分后重试。"
            )
        texts = [page.extract_text() or "" for page in reader.pages]
    except DocumentError:
        raise
    except Exception as e:  # noqa: BLE001 - 解析失败统一转可操作错误
        raise DocumentError(
            f"无法解析 PDF（文件可能已损坏）：{path.name}（{type(e).__name__}: {e}）"
        ) from e
    joined = "\n".join(texts)
    if not joined.strip():
        raise DocumentError(
            f"PDF 未提取到文本（可能是扫描件/图片型 PDF，无文本层）：{path.name}。"
            f"请改用 OCR 工具或提供文本版文件。"
        )
    return [Block(kind="paragraph", text=p) for p in _split_paragraphs(joined)]


def _split_paragraphs(text: str) -> list[str]:
    """按空行分段（PDF 文本流 → 段落列表）。"""
    paras: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line.strip())
        elif current:
            paras.append(" ".join(current))
            current = []
    if current:
        paras.append(" ".join(current))
    return paras
