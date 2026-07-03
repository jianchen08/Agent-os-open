"""VSCode 扩展连接器示例。

暴露接口：
- VSCodeConnector：通过 WebSocket 连接 VSCode 扩展的外部工具适配器
"""

from __future__ import annotations

import logging
from typing import Any

from tools.external.adapter import ExternalToolAdapter
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
)

logger = logging.getLogger(__name__)

# ---- 操作 Schema 定义 ----

OPEN_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "要打开的文件路径",
        },
        "line": {
            "type": "integer",
            "description": "跳转到的行号（可选）",
            "default": 0,
        },
        "column": {
            "type": "integer",
            "description": "跳转到的列号（可选）",
            "default": 0,
        },
    },
    "required": ["file_path"],
}

GET_SELECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "文件路径（可选，不传则使用当前活动编辑器）",
        },
    },
}

SHOW_DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "original": {
            "type": "string",
            "description": "原始内容",
        },
        "modified": {
            "type": "string",
            "description": "修改后内容",
        },
        "file_name": {
            "type": "string",
            "description": "文件名（用于标题显示）",
        },
    },
    "required": ["original", "modified"],
}

APPLY_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "range": {
                        "type": "object",
                        "properties": {
                            "start_line": {"type": "integer"},
                            "start_col": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "end_col": {"type": "integer"},
                        },
                        "required": ["start_line", "start_col", "end_line", "end_col"],
                    },
                    "new_text": {"type": "string"},
                },
                "required": ["range", "new_text"],
            },
            "description": "编辑操作列表",
        },
    },
    "required": ["file_path", "edits"],
}

GET_DIAGNOSTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "文件路径（可选，不传则获取所有诊断）",
        },
    },
}

GET_SYMBOLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
        "query": {
            "type": "string",
            "description": "搜索查询（可选）",
        },
    },
    "required": ["file_path"],
}

# 通用输出 Schema
OPERATION_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "data": {"type": "object"},
        "error": {"type": "string"},
    },
}

# 安全路径验证前缀
ALLOWED_PATH_PREFIXES: list[str] = ["/workspace", "/tmp", "/home"]


class VSCodeConnector(ExternalToolAdapter):
    """VSCode 扩展连接器。

    通过 WebSocket 连接 VSCode 扩展，支持：
    - open_file: 打开文件
    - get_selection: 获取选区
    - show_diff: 显示差异
    - apply_edit: 应用编辑
    - get_diagnostics: 获取诊断信息
    - get_symbols: 获取符号列表

    使用前需确保 VSCode 扩展已启动并监听 WebSocket 端口。
    """

    def __init__(self, config: ExternalToolConfig) -> None:
        """初始化 VSCode 连接器。

        Args:
            config: 工具配置（protocol 应为 websocket）
        """
        super().__init__(config)

    def define_schemas(self) -> list[ExternalToolCapability]:
        """定义 VSCode 支持的操作。"""
        return [
            ExternalToolCapability(
                name="open_file",
                description="在 VSCode 中打开文件",
                input_schema=OPEN_FILE_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="get_selection",
                description="获取 VSCode 当前选区内容",
                input_schema=GET_SELECTION_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="show_diff",
                description="在 VSCode 中显示差异对比",
                input_schema=SHOW_DIFF_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="apply_edit",
                description="在 VSCode 中应用文本编辑",
                input_schema=APPLY_EDIT_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
                dangerous=True,
            ),
            ExternalToolCapability(
                name="get_diagnostics",
                description="获取 VSCode 诊断信息（错误/警告）",
                input_schema=GET_DIAGNOSTICS_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="get_symbols",
                description="获取文件中的符号列表",
                input_schema=GET_SYMBOLS_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
        ]

    def validate_input(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """验证输入参数（含路径安全验证）。"""
        validated = super().validate_input(operation, inputs)

        # 对涉及文件路径的操作进行安全验证
        if operation in ("open_file", "apply_edit", "get_symbols"):
            file_path = validated.get("file_path", "")
            self._validate_path(file_path)

        return validated

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行 VSCode 操作。

        Args:
            operation: 操作名称
            inputs: 已验证的输入参数
            context: 执行上下文

        Returns:
            操作结果
        """
        if self._connection is None:
            return {"success": False, "error": "VSCode 连接未建立"}

        try:
            response = await self._connection.send_request(
                operation=operation,
                payload=inputs,
                timeout=self._config.execute_timeout,
            )
            return response

        except Exception as e:
            self._logger.error(
                "VSCode 操作失败 | op=%s | error=%s",
                operation,
                e,
            )
            return {"success": False, "error": str(e), "operation": operation}

    @staticmethod
    def _validate_path(file_path: str) -> None:
        """验证文件路径安全性。

        Args:
            file_path: 文件路径

        Raises:
            ValueError: 路径不安全
        """
        if not file_path:
            return

        # 禁止路径遍历
        if ".." in file_path:
            raise ValueError(f"路径包含非法遍历: {file_path}")

        # 检查路径前缀（可选限制）
        # 在实际使用中，可根据工作空间配置调整允许的路径前缀
