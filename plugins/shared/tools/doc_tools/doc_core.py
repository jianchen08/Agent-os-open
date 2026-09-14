"""文档处理工具核心——块模型、路径安全、护栏、格式检测。

路径安全（与 builtin_tools fs_tools 同口径，照 dsh_adapter 范式实现）：
- workspace/project_root 为 param_inject 运行时注入参数（不出现在 LLM schema；
  工具函数签名必须声明，否则 SDK 分发层按签名过滤会把注入值静默丢弃）
- 相对路径以根（project_root 优先，回退 workspace）解析；绝对路径必须落在
  根内；越界/无注入一律 fail-closed 拒绝，不做 cwd 兜底
- /workspace 容器挂载点重映射（bash 容器内 LLM 沿用该路径调文件工具）
- 扩展名白名单：只处理 .md/.markdown/.docx/.pdf（天然拒绝凭据类文件）

护栏（D1 教训：任何遍历/转换都要有时限与大小护栏）：
- 输入文件大小上限 MAX_INPUT_BYTES；PDF 页数上限 MAX_PDF_PAGES
- 转换时限 CONVERT_TIMEOUT_SECONDS（asyncio.wait_for 包裹线程执行）

[来源: plugins/shared/tools/builtin_tools/src/agentos_builtin_tools/fs_tools.py
 _check_workspace_path；plugins/shared/system/dsh_adapter/server.py _anchor_to_workspace]
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

# ── 格式与扩展名 ──────────────────────────────────────────────

FORMAT_MD = "md"
FORMAT_DOCX = "docx"
FORMAT_PDF = "pdf"

FORMAT_EXTENSIONS: dict[str, str] = {FORMAT_MD: ".md", FORMAT_DOCX: ".docx", FORMAT_PDF: ".pdf"}
EXTENSION_FORMATS: dict[str, str] = {
    ".md": FORMAT_MD,
    ".markdown": FORMAT_MD,
    ".docx": FORMAT_DOCX,
    ".pdf": FORMAT_PDF,
}
OUTPUT_EXTENSIONS = frozenset(FORMAT_EXTENSIONS.values())

# ── 护栏（D1 教训：时限与大小护栏） ───────────────────────────

MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
CONVERT_TIMEOUT_SECONDS = 60.0


class DocumentError(Exception):
    """文档处理错误——消息面向 LLM/用户，含可操作建议。

    code 为机器可读错误码（供工具失败信封 error_code 字段），
    取值：DOC_PATH_DENIED / DOC_NOT_FOUND / DOC_UNSUPPORTED_FORMAT /
    DOC_TOO_LARGE / DOC_PARSE_FAILED / DOC_SAME_FORMAT / DOC_TIMEOUT /
    DOC_IO_FAILED / DOC_FAILED。
    """

    def __init__(self, message: str, code: str = "DOC_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Block:
    """文档块中间表示（reader → Block[] → writer 三层解耦）。"""

    kind: str  # heading|paragraph|bullet|ordered|code|quote|table
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)
    info: str = ""


# ── 路径安全 ──────────────────────────────────────────────────


def resolve_in_workspace(path: str, workspace: str | None, project_root: str | None) -> Path:
    """工作区边界锚定（fail-closed）。越界/无注入抛 DocumentError。

    范式同 builtin_tools fs_tools 的 _check_workspace_path（dsh_adapter 同款）：
    相对路径以根解析、绝对路径必须落在根内、无注入拒绝、/workspace 重映射。
    """
    root_str = project_root or workspace
    if not root_str:
        raise DocumentError(
            f"workspace/project_root 未注入，无法锚定路径（相对路径禁止以进程 cwd 解析）：{path}",
            code="DOC_PATH_DENIED",
        )
    root = Path(root_str).resolve()
    if path == "/workspace" or path.startswith("/workspace/"):
        path = str(root) + path[len("/workspace") :]
        logger.info("[doc_tools] 容器挂载点 /workspace 重映射到宿主工作区 | -> %s", path)
    target = Path(path)
    resolved = target.resolve() if target.is_absolute() else (root / target).resolve()
    resolved_n = os.path.normcase(str(resolved))
    root_n = os.path.normcase(str(root))
    if not (resolved_n == root_n or resolved_n.startswith(root_n + os.sep)):
        raise DocumentError(
            f"路径超出工作区范围，已拒绝：{path}（工作区：{root}）", code="DOC_PATH_DENIED"
        )
    return resolved


def detect_format(path: Path) -> str:
    """按扩展名判定格式；不支持的扩展名抛 DocumentError。"""
    ext = path.suffix.lower()
    fmt = EXTENSION_FORMATS.get(ext)
    if fmt is None:
        raise DocumentError(
            f"不支持的文件扩展名：{ext or '（无扩展名）'}（{path.name}）。支持：.md/.markdown/.docx/.pdf"
        )
    return fmt


def guard_input_file(resolved: Path) -> None:
    """输入文件护栏：存在性 + 大小上限（超限快速失败，D1 教训）。"""
    if not resolved.exists():
        raise DocumentError(f"文件不存在：{resolved}")
    if not resolved.is_file():
        raise DocumentError(f"不是文件（目录？）：{resolved}")
    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise DocumentError(
            f"文件过大（{size // 1024 // 1024}MB，上限 {MAX_INPUT_BYTES // 1024 // 1024}MB）："
            f"{resolved.name}。请拆分或压缩后重试。"
        )


def guard_output_path(resolved: Path) -> None:
    """输出路径护栏：父目录必须存在（不自动建目录，避免任意目录写入）。"""
    parent = resolved.parent
    if not parent.exists():
        raise DocumentError(f"输出目录不存在：{parent}。请先创建目录后重试。")
    if resolved.exists() and resolved.is_dir():
        raise DocumentError(f"输出路径是目录：{resolved}")


# ── 时限护栏 ──────────────────────────────────────────────────

_T = TypeVar("_T")


async def run_with_timeout(fn: Callable[..., _T], *args: Any) -> _T:
    """线程执行 + 时限护栏（D1 教训：转换不得无限期阻塞事件循环）。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=CONVERT_TIMEOUT_SECONDS)
    except TimeoutError as e:
        raise DocumentError(
            f"转换超时（>{CONVERT_TIMEOUT_SECONDS:.0f} 秒），文件可能过大或结构复杂。请拆分后重试。"
        ) from e


# ── 内联文本解析（md 标记 ↔ 富文本） ─────────────────────────

_INLINE_RE = re.compile(r"\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+)`")


def parse_inline(text: str) -> list[tuple[str, str]]:
    """Markdown 内联文本 → [(文本, 样式)]；样式 ∈ {"", "bold", "italic", "bolditalic", "code"}。"""
    segments: list[tuple[str, str]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos : m.start()], ""))
        if m.group(1) is not None:
            segments.append((m.group(1), "bolditalic"))
        elif m.group(2) is not None:
            segments.append((m.group(2), "bold"))
        elif m.group(3) is not None:
            segments.append((m.group(3), "italic"))
        else:
            segments.append((m.group(4), "code"))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], ""))
    return segments
