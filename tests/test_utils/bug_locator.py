"""测试失败 Bug 定位器。

当测试失败时，自动分析 traceback 并输出：
- 失败的断言位置（文件路径 + 行号）
- 该行及上下文的代码片段
- 涉及的源码文件清单（非测试文件 → 高概率 bug 位置）
"""

from __future__ import annotations

import inspect
import linecache
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 项目源码根目录（用于判断是否为项目代码）
_PROJECT_SRC = Path("src").resolve()
_TESTS_DIR = Path("tests").resolve()


@dataclass(frozen=True)
class CodeLocation:
    """源码位置。"""

    file_path: str
    line_number: int
    function_name: str
    code_line: str
    context_before: tuple[str, ...]
    context_after: tuple[str, ...]
    is_project_code: bool
    is_test_code: bool

    def format(self, context_lines: int = 3) -> str:
        """格式化为人类可读的代码片段。"""
        lines: list[str] = []
        marker = ">>>" if self.is_project_code else "   "
        start = self.line_number - len(self.context_before)
        for i, ctx_line in enumerate(self.context_before):
            lines.append(f"  {start + i:4d} | {ctx_line}")
        lines.append(f"{marker} {self.line_number:4d} | {self.code_line}")
        for i, ctx_line in enumerate(self.context_after):
            lines.append(f"  {self.line_number + 1 + i:4d} | {ctx_line}")
        return "\n".join(lines)


@dataclass
class BugLocation:
    """Bug 定位结果。"""

    assertion_location: CodeLocation | None
    bug_candidates: list[CodeLocation] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    raw_traceback: str = ""

    def summary(self) -> str:
        """生成定位摘要报告。"""
        parts: list[str] = ["=" * 60, "🐛 Bug 定位报告", "=" * 60]

        if self.assertion_location:
            parts.append(f"\n📍 断言失败位置: {self.assertion_location.file_path}:{self.assertion_location.line_number}")
            parts.append(f"   函数: {self.assertion_location.function_name}")
            parts.append(f"\n{self.assertion_location.format()}")
        else:
            parts.append("\n⚠️ 未找到断言失败位置")

        if self.bug_candidates:
            parts.append("\n🎯 高概率 Bug 位置（项目源码，非测试代码）:")
            for i, loc in enumerate(self.bug_candidates, 1):
                parts.append(f"\n  [{i}] {loc.file_path}:{loc.line_number} in {loc.function_name}")
                parts.append(f"  {loc.format()}")

        if self.source_files:
            parts.append("\n📁 涉及的源码文件:")
            for fp in self.source_files:
                parts.append(f"  - {fp}")

        parts.append("\n" + "=" * 60)
        return "\n".join(parts)


def locate_bug(exc_info: tuple[type, BaseException, Any] | None = None) -> BugLocation:
    """从异常信息中定位 Bug。

    Args:
        exc_info: 异常三元组 (type, value, tb)。为 None 则使用 sys.exc_info()。

    Returns:
        BugLocation 包含断言位置、候选 bug 位置、涉及文件列表。
    """
    if exc_info is None:
        import sys
        exc_info = sys.exc_info()

    if exc_info is None or exc_info[2] is None:
        return BugLocation(assertion_location=None, raw_traceback="无异常信息")

    exc_type, exc_value, exc_tb = exc_info
    raw_tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

    # 提取 traceback 中的所有帧
    frames = _extract_frames(exc_tb)
    locations = [_frame_to_location(f) for f in frames]

    # 断言位置 = 最内层（最后一个）
    assertion_loc = locations[-1] if locations else None

    # Bug 候选 = 项目源码但非测试代码的帧（倒序，最近的优先）
    candidates = [
        loc for loc in reversed(locations)
        if loc.is_project_code and not loc.is_test_code
    ]

    # 涉及的源码文件
    source_files = sorted({
        loc.file_path for loc in locations
        if loc.is_project_code
    })

    return BugLocation(
        assertion_location=assertion_loc,
        bug_candidates=candidates,
        source_files=source_files,
        raw_traceback=raw_tb,
    )


def _extract_frames(tb: Any) -> list[inspect.FrameInfo]:
    """从 traceback 对象提取帧信息列表。"""
    frames: list[inspect.FrameInfo] = []
    current: Any = tb
    while current is not None:
        frame = current.tb_frame
        lineno = current.tb_lineno
        filename = frame.f_code.co_filename
        function = frame.f_code.co_name
        # 使用 FrameInfo 的子集信息
        frames.append(_make_frame_info(filename, lineno, function, frame))
        current = current.tb_next
    return frames


def _make_frame_info(filename: str, lineno: int, function: str, frame: Any) -> inspect.FrameInfo:
    """构造 FrameInfo 对象。"""
    # 使用 inspect.getframeinfo 获取上下文
    try:
        info = inspect.getframeinfo(frame, context=3)
        return inspect.FrameInfo(
            frame=frame,
            filename=filename,
            lineno=lineno,
            function=function,
            code_context=info.code_context,
            index=info.index,
        )
    except (TypeError, AttributeError):
        # frame 可能已不可用，使用 linecache 回退
        code_context = linecache.getline(filename, lineno).strip() if filename else ""
        return inspect.FrameInfo(
            frame=frame,
            filename=filename,
            lineno=lineno,
            function=function,
            code_context=[code_context] if code_context else None,
            index=0,
        )


def _frame_to_location(fi: inspect.FrameInfo) -> CodeLocation:
    """将 FrameInfo 转为 CodeLocation。"""
    file_path = _relative_path(fi.filename)
    code_lines = fi.code_context or []
    center_line = code_lines[fi.index] if fi.index is not None and fi.index < len(code_lines) else ""
    before = tuple(code_lines[:fi.index]) if fi.index else ()
    after = tuple(code_lines[fi.index + 1:]) if fi.index is not None else ()

    abs_path = Path(fi.filename).resolve()
    is_project = _is_project_code(abs_path)
    is_test = _is_test_code(abs_path)

    return CodeLocation(
        file_path=file_path,
        line_number=fi.lineno,
        function_name=fi.function,
        code_line=center_line.strip(),
        context_before=tuple(line.strip() for line in before),
        context_after=tuple(line.strip() for line in after),
        is_project_code=is_project,
        is_test_code=is_test,
    )


def _relative_path(filepath: str) -> str:
    """转为项目相对路径。"""
    try:
        return os.path.relpath(filepath)
    except ValueError:
        return filepath


def _is_project_code(abs_path: Path) -> bool:
    """判断是否为项目源码。"""
    try:
        abs_path.relative_to(_PROJECT_SRC)
        return True
    except ValueError:
        pass
    try:
        abs_path.relative_to(_TESTS_DIR)
        return True
    except ValueError:
        pass
    return False


def _is_test_code(abs_path: Path) -> bool:
    """判断是否为测试代码。"""
    try:
        abs_path.relative_to(_TESTS_DIR)
        return True
    except ValueError:
        return False


__all__ = ["BugLocation", "CodeLocation", "locate_bug"]
