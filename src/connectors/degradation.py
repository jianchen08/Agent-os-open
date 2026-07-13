"""
降级管理器

当没有活跃的 IDE 连接器时，提供内置工具作为降级方案。

暴露接口：
- DegradationManager: 降级管理器
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from .types import ActionResult

logger = logging.getLogger(__name__)


class DegradationManager:
    """降级管理器。

    当没有活跃的 IDE 连接器时，使用内置工具提供降级能力。

    降级映射：
    - open_file → 使用 file_read 读取文件内容
    - get_selection → 返回空上下文
    - show_diff → 生成 unified diff 文本
    - insert_content → 返回不支持提示
    - jump_to → 返回不支持提示

    使用方式:
        manager = DegradationManager(registry)
        result = manager.execute_with_fallback("open_file", {"file_path": "/tmp/a.py"})
    """

    # 支持降级处理的操作类型集合
    DEGRADABLE_ACTIONS: frozenset[str] = frozenset(
        {
            "open_file",
            "get_selection",
            "show_diff",
            "insert_content",
            "jump_to",
        }
    )

    def __init__(self) -> None:
        """初始化降级管理器。"""
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def can_handle_locally(self, action_type: str) -> bool:
        """判断指定操作类型是否能用内置工具降级处理。

        Args:
            action_type: 操作类型

        Returns:
            True 表示可以降级处理
        """
        return action_type in self.DEGRADABLE_ACTIONS

    def execute_with_fallback(self, action_type: str, params: dict[str, Any]) -> ActionResult:
        """带降级的执行操作。

        根据操作类型选择合适的降级方案执行。

        Args:
            action_type: 操作类型
            params: 操作参数

        Returns:
            降级执行结果
        """
        handler = self._get_fallback_handler(action_type)
        if handler is None:
            return ActionResult(
                success=False,
                error=f"不支持的操作类型: {action_type}",
            )

        try:
            return handler(params)
        except Exception as e:
            self._logger.error(f"降级执行失败: {action_type}, 错误: {e}")
            return ActionResult(
                success=False,
                error=f"降级执行失败: {str(e)}",
            )

    def _get_fallback_handler(self, action_type: str) -> Any:
        """获取降级处理函数。

        Args:
            action_type: 操作类型

        Returns:
            降级处理函数，不支持时返回 None
        """
        handlers: dict[str, Any] = {
            "open_file": self._fallback_open_file,
            "get_selection": self._fallback_get_selection,
            "show_diff": self._fallback_show_diff,
            "insert_content": self._fallback_unsupported,
            "jump_to": self._fallback_unsupported,
        }
        return handlers.get(action_type)

    def _fallback_open_file(self, params: dict[str, Any]) -> ActionResult:
        """降级：使用 file_read 读取文件内容。

        Args:
            params: 包含 file_path 的参数字典

        Returns:
            包含文件内容的操作结果
        """
        file_path = params.get("file_path", "")
        if not file_path:
            return ActionResult(
                success=False,
                error="降级模式：缺少 file_path 参数",
            )

        try:
            from pathlib import Path  # noqa: PLC0415

            path = Path(file_path)
            if not path.exists():
                return ActionResult(
                    success=False,
                    error=f"降级模式：文件不存在: {file_path}",
                )

            content = path.read_text(encoding="utf-8")
            return ActionResult(
                success=True,
                data={
                    "degraded": True,
                    "message": "无连接器，已降级为读取文件内容",
                    "file_path": file_path,
                    "content": content,
                },
            )
        except Exception as e:
            return ActionResult(
                success=False,
                error=f"降级模式：读取文件失败: {str(e)}",
            )

    def _fallback_get_selection(self, params: dict[str, Any]) -> ActionResult:
        """降级：返回空上下文提示。

        Args:
            params: 参数（未使用）

        Returns:
            提示用户手动提供上下文的结果
        """
        return ActionResult(
            success=True,
            data={
                "degraded": True,
                "message": "无连接器，请手动提供上下文信息（活动文件、选中文本等）",
                "active_file": None,
                "selected_text": None,
                "cursor_position": None,
            },
        )

    def _fallback_show_diff(self, params: dict[str, Any]) -> ActionResult:
        """降级：生成 unified diff 文本。

        Args:
            params: 包含 original_content 和 new_content 的参数字典

        Returns:
            包含 diff 文本的操作结果
        """
        original = params.get("original_content", "")
        new = params.get("new_content", "")
        file_path = params.get("file_path", "unknown")
        title = params.get("title", "")

        original_lines = original.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
        diff_text = "".join(diff)

        if not diff_text:
            diff_text = "(无差异)"

        header = f"[Diff: {title}]\n" if title else ""
        return ActionResult(
            success=True,
            data={
                "degraded": True,
                "message": "无连接器，已降级为文本 diff 输出",
                "diff_text": header + diff_text,
            },
        )

    def _fallback_unsupported(self, params: dict[str, Any]) -> ActionResult:
        """降级：不支持的操作的统一处理。

        Args:
            params: 参数（未使用）

        Returns:
            提示不支持的结果
        """
        return ActionResult(
            success=True,
            data={
                "degraded": True,
                "message": "无连接器，此操作需要 IDE 连接器支持",
            },
        )
