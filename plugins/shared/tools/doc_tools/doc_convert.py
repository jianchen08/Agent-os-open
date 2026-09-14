"""文档转换编排——格式检测 → 读取（Block[]）→ 写出。

转换矩阵（v1 范围：Markdown↔Word↔PDF 互转）：
- md → docx / pdf
- docx → md / pdf
- pdf → md / docx
- 同格式转换显式拒绝（无意义操作，不静默复制）

文本提取（doc_extract_text）复用同一读取链：任意格式 → Block[] → 纯文本，
供 LLM 做摘要/翻译的内容理解（本工具只做格式转换与文本提取）。

[来源: 任务简报 docs/working/batch_20260913/R4v2_doc_tool_plugin.md]
"""

from __future__ import annotations

from pathlib import Path

from doc_core import (
    FORMAT_DOCX,
    FORMAT_MD,
    FORMAT_PDF,
    Block,
    DocumentError,
    detect_format,
    guard_input_file,
    guard_output_path,
)
from doc_readers import read_docx, read_markdown, read_pdf
from doc_writers import blocks_to_markdown, write_docx, write_pdf

SUPPORTED_TARGETS = (FORMAT_MD, FORMAT_DOCX, FORMAT_PDF)


def read_blocks(path: Path, fmt: str) -> list[Block]:
    """按格式读取为 Block[]（统一读取入口）。"""
    if fmt == FORMAT_MD:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise DocumentError(
                f"Markdown 文件编码不是 UTF-8：{path.name}。请转为 UTF-8 后重试。"
            ) from e
        except OSError as e:
            raise DocumentError(f"读取文件失败：{path.name}（{e}）") from e
        return read_markdown(text)
    if fmt == FORMAT_DOCX:
        return read_docx(path)
    if fmt == FORMAT_PDF:
        return read_pdf(path)
    raise DocumentError(f"不支持的格式：{fmt}")


def write_blocks(blocks: list[Block], path: Path, fmt: str) -> None:
    """Block[] 按目标格式写出（统一写出入口）。"""
    if fmt == FORMAT_MD:
        try:
            path.write_text(blocks_to_markdown(blocks), encoding="utf-8")
        except OSError as e:
            raise DocumentError(f"写入 Markdown 文件失败：{path.name}（{e}）") from e
    elif fmt == FORMAT_DOCX:
        write_docx(blocks, path)
    elif fmt == FORMAT_PDF:
        write_pdf(blocks, path)
    else:
        raise DocumentError(f"不支持的目标格式：{fmt}")


def convert_file(source: Path, target: Path, target_format: str) -> dict[str, object]:
    """单文件转换（同步，供线程执行）：source → target_format → target。

    Returns:
        {"source_format", "target_format", "blocks", "output"} 摘要信息。
    """
    guard_input_file(source)
    source_format = detect_format(source)
    if target_format not in SUPPORTED_TARGETS:
        raise DocumentError(
            f"不支持的目标格式：{target_format}（支持：{', '.join(SUPPORTED_TARGETS)}）"
        )
    if source_format == target_format:
        raise DocumentError(
            f"源格式与目标格式相同（{source_format}），无需转换。请指定不同目标格式。"
        )
    guard_output_path(target)
    blocks = read_blocks(source, source_format)
    write_blocks(blocks, target, target_format)
    return {
        "source_format": source_format,
        "target_format": target_format,
        "blocks": len(blocks),
        "output": str(target),
    }


def blocks_to_plain_text(blocks: list[Block]) -> str:
    """Block[] → 纯文本（供 LLM 摘要/翻译消费；列表/表格按可读文本展开）。"""
    lines: list[str] = []
    for b in blocks:
        if b.kind == "table" and b.rows:
            for row in b.rows:
                lines.append(" | ".join(row))
        elif b.kind in ("bullet", "ordered"):
            lines.append(("  " * max(0, b.level - 1)) + "• " + b.text)
        else:
            lines.append(b.text)
    return "\n".join(lines).strip()


def extract_text(path: Path, max_chars: int | None = None) -> dict[str, object]:
    """文本提取（同步，供线程执行）：任意支持格式 → 纯文本。

    Args:
        max_chars: 截断上限（None = 不截断）。
    """
    guard_input_file(path)
    fmt = detect_format(path)
    blocks = read_blocks(path, fmt)
    text = blocks_to_plain_text(blocks)
    truncated = False
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "source_format": fmt,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }
