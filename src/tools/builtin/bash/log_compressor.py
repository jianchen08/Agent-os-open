"""
日志压缩器

暴露接口：
- detect_output_type(self, command: str, lines: list[str]) -> OutputType：detect_output_type功能
- extract_progress(self, lines: list[str], output_type: OutputType) -> str | None：extract_progress功能
- count_warnings_errors(self, lines: list[str]) -> tuple[int, int]：count_warnings_errors功能
- get_latest_message(self, lines: list[str], max_length: int) -> str：get_latest_message功能
- compress_errors(self, lines: list[str], dedup: bool = True) -> list[str]：compress_errors功能
- compress(self, lines: list[str], command: str, config: LogCompressorConfig | None = None) -> OutputSummary：compress功能
- LogCompressor：LogCompressor类
"""

from __future__ import annotations

import re
from typing import ClassVar

from .types import LogCompressorConfig, OutputSummary, OutputType


class LogCompressor:
    """
    日志压缩器

    将大量日志输出压缩为3-5行摘要，供LLM快速理解。
    从最近200行中提取关键信息。
    """

    # 输出类型检测模式
    TYPE_PATTERNS: ClassVar[dict[OutputType, list[str]]] = {
        OutputType.NPM_INSTALL: ["npm install", "npm ci", "yarn install", "pnpm install"],
        OutputType.PIP_INSTALL: ["pip install", "pip3 install", "poetry install", "conda install"],
        OutputType.DOCKER_BUILD: ["docker build", "docker-compose build", "docker buildx"],
        OutputType.PYTEST: ["pytest", "python -m pytest", "py.test"],
        OutputType.COMPILATION: ["gcc", "g++", "make", "cmake", "cargo build", "go build", "npm run build"],
        OutputType.GIT: ["git clone", "git pull", "git fetch", "git push"],
    }

    # 进度检测模式
    PROGRESS_PATTERNS: ClassVar[list[tuple[re.Pattern, str]]] = [
        (re.compile(r"(\d+)/(\d+)\s+"), "计数"),
        (re.compile(r"(\d+)%"), "百分比"),
        (re.compile(r"(\d+)\s*of\s*(\d+)"), "计数"),
        (re.compile(r"\[.*(\d+)%.*\]"), "百分比"),
        (re.compile(r"(\d+)\s*packages?"), "包数量"),
    ]

    # 警告和错误模式
    WARNING_PATTERNS: ClassVar[list[re.Pattern]] = [
        re.compile(r"warning:", re.IGNORECASE),
        re.compile(r"warn", re.IGNORECASE),
        re.compile(r"deprecated", re.IGNORECASE),
        re.compile(r"obsolete", re.IGNORECASE),
    ]

    ERROR_PATTERNS: ClassVar[list[re.Pattern]] = [
        re.compile(r"error:", re.IGNORECASE),
        re.compile(r"fatal:", re.IGNORECASE),
        re.compile(r"failed", re.IGNORECASE),
        re.compile(r"exception", re.IGNORECASE),
        re.compile(r"traceback", re.IGNORECASE),
    ]

    def __init__(self, max_lines: int = 200):
        """初始化日志压缩器"""
        self.max_lines = max_lines

    def detect_output_type(self, command: str, lines: list[str]) -> OutputType:  # noqa: PLR0911
        """检测输出类型"""
        cmd_lower = command.lower()

        # 根据命令检测类型
        for output_type, patterns in self.TYPE_PATTERNS.items():
            for pattern in patterns:
                if pattern in cmd_lower:
                    return output_type

        # 根据输出内容检测类型
        for line in lines[:50]:  # 检查前50行
            line_lower = line.lower()
            if "npm" in line_lower and ("install" in line_lower or "package" in line_lower):
                return OutputType.NPM_INSTALL
            if "pip" in line_lower and "install" in line_lower:
                return OutputType.PIP_INSTALL
            if "docker" in line_lower and "build" in line_lower:
                return OutputType.DOCKER_BUILD
            if "pytest" in line_lower or "test session starts" in line_lower:
                return OutputType.PYTEST
            if any(x in line_lower for x in ["compiling", "linking", "building"]):
                return OutputType.COMPILATION

        return OutputType.GENERAL

    def extract_progress(self, lines: list[str], output_type: OutputType) -> str | None:  # noqa: PLR0912
        """提取进度信息"""
        # 从最近50行查找进度
        recent_lines = lines[-50:] if len(lines) > 50 else lines

        for line in reversed(recent_lines):
            # npm/yarn 进度
            if output_type == OutputType.NPM_INSTALL:
                if "packages" in line.lower():
                    match = re.search(r"(\d+)\s+packages?", line)
                    if match:
                        return f"{match.group(1)} packages"

            # pip 进度
            elif output_type == OutputType.PIP_INSTALL:
                if "Collecting" in line or "Installing" in line:
                    match = re.search(r"(Collecting|Installing|Successfully installed)", line)
                    if match:
                        return match.group(1)

            # pytest 进度
            elif output_type == OutputType.PYTEST:
                match = re.search(r"(\d+)\s+passed|(\d+)\s+failed|(\d+)%", line)
                if match:
                    return match.group(0)

            # 通用进度检测
            for pattern, pattern_type in self.PROGRESS_PATTERNS:
                match = pattern.search(line)
                if match:
                    if pattern_type == "计数" and len(match.groups()) >= 2:
                        return f"{match.group(1)}/{match.group(2)}"
                    if pattern_type == "百分比":
                        return match.group(1) + "%"

        return None

    def count_warnings_errors(self, lines: list[str]) -> tuple[int, int]:
        """统计警告和错误数量"""
        warnings = 0
        errors = 0

        for line in lines:
            for pattern in self.WARNING_PATTERNS:
                if pattern.search(line):
                    warnings += 1
                    break

            for pattern in self.ERROR_PATTERNS:
                if pattern.search(line):
                    errors += 1
                    break

        return warnings, errors

    def get_latest_message(self, lines: list[str], max_length: int = 100) -> str:
        """获取最新的非空消息"""
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith(">") and len(stripped) > 5:
                if len(stripped) > max_length:
                    return stripped[:max_length] + "..."
                return stripped

        return ""

    def compress_errors(self, lines: list[str], dedup: bool = True) -> list[str]:
        """
        压缩错误信息

        从日志行中提取错误信息，并可选地合并重复的错误。

        Args:
            lines: 日志行列表
            dedup: 是否合并重复错误，默认为 True

        Returns:
            压缩后的错误列表，最多返回 20 条错误

        Example:
            >>> lines = ["error: file not found", "error: file not found", "error: timeout"]
            >>> compressor.compress_errors(lines, dedup=True)
            ['error: file not found (x2)', 'error: timeout']
        """
        error_lines = []

        # 提取所有错误行
        for line in lines:
            for pattern in self.ERROR_PATTERNS:
                if pattern.search(line):
                    error_lines.append(line.strip())
                    break

        # 如果没有错误，返回空列表
        if not error_lines:
            return []

        # 如果不合并重复，直接返回（最多 20 条）
        if not dedup:
            return error_lines[-20:]

        # 合并重复错误
        error_counts: dict[str, int] = {}
        for error in error_lines:
            # 标准化错误信息（去除时间戳等变化部分）
            normalized = self._normalize_error(error)
            if normalized in error_counts:
                error_counts[normalized] += 1
            else:
                error_counts[normalized] = 1

        # 构建压缩后的错误列表
        compressed_errors = []
        for error, count in error_counts.items():
            if count > 1:
                compressed_errors.append(f"{error} (x{count})")
            else:
                compressed_errors.append(error)

        # 返回最多 20 条错误
        return compressed_errors[-20:]

    def _normalize_error(self, error: str) -> str:
        """
        标准化错误信息

        去除时间戳、行号等变化部分，以便合并相同的错误。

        Args:
            error: 原始错误信息

        Returns:
            标准化后的错误信息
        """
        # 去除常见的时间戳格式
        normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}", "", error)
        # 去除行号
        normalized = re.sub(r":\d+", ":*", normalized)
        # 去除多余的空格
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized

    def compress(
        self,
        lines: list[str],
        command: str,
        config: LogCompressorConfig | None = None,
    ) -> OutputSummary:
        """
        压缩日志为摘要

        根据配置参数将大量日志压缩为简洁的摘要信息。

        Args:
            lines: 日志行列表
            command: 执行的命令
            config: 压缩配置，如果为 None 则使用默认配置

        Returns:
            OutputSummary: 包含摘要信息的输出摘要对象

        Example:
            >>> compressor = LogCompressor()
            >>> config = LogCompressorConfig(compress_threshold=100, recent_lines=5)
            >>> summary = compressor.compress(lines, "npm install", config)
        """
        # 使用默认配置（如果未提供）
        if config is None:
            config = LogCompressorConfig()

        total_lines = len(lines)

        # 根据配置决定是否压缩
        if total_lines <= config.compress_threshold:
            # 日志行数未超过阈值，不压缩
            recent_lines = lines
        else:
            # 日志行数超过阈值，只取最近 max_lines 行进行分析
            recent_lines = lines[-self.max_lines :] if total_lines > self.max_lines else lines

        # 检测类型
        output_type = self.detect_output_type(command, recent_lines)

        # 统计警告和错误
        warnings, errors = self.count_warnings_errors(recent_lines)

        # 提取进度
        progress = self.extract_progress(recent_lines, output_type)

        # 获取最新消息
        latest = self.get_latest_message(recent_lines)

        # 构建摘要行
        summary_lines = []

        # 行数信息
        if total_lines > self.max_lines:
            summary_lines.append(f"[{total_lines}行，显示最近{self.max_lines}行]")
        else:
            summary_lines.append(f"[{total_lines}行]")

        # 类型信息
        type_names = {
            OutputType.NPM_INSTALL: "npm install",
            OutputType.PIP_INSTALL: "pip install",
            OutputType.DOCKER_BUILD: "docker build",
            OutputType.PYTEST: "pytest",
            OutputType.COMPILATION: "编译",
            OutputType.GIT: "git",
            OutputType.GENERAL: "通用命令",
        }
        summary_lines.append(f"类型: {type_names.get(output_type, '通用命令')}")

        # 进度信息
        if progress:
            summary_lines.append(f"进度: {progress}")

        # 警告和错误统计
        summary_lines.append(f"警告: {warnings}, 错误: {errors}")

        # 错误列表（如果配置启用）
        if config.show_errors and errors > 0:
            error_list = self.compress_errors(recent_lines, dedup=config.dedup_errors)
            if error_list:
                summary_lines.append("错误列表:")
                for error in error_list[: config.recent_lines]:
                    summary_lines.append(f"  - {error}")

        # 最新消息
        if latest:
            summary_lines.append(f"最新: {latest}")

        return OutputSummary(
            lines=summary_lines,
            output_type=output_type,
            total_lines=total_lines,
            warnings=warnings,
            errors=errors,
            progress=progress,
            latest_message=latest,
        )
