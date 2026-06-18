"""
执行记录查询工具（复盘专用）

暴露接口：
- get_tool_definition() -> Tool：获取工具定义
- ReadExecutionDetailTool：执行记录查询工具类

支持三层渐进披露：
- skeleton（骨架）：每轮一行概览，快速了解全局流程
- L1（压缩块）：查看覆盖该 iteration 的语义摘要
- L0（原始记录）：查看原始记录的特定字段（thinking/tool_calls/error 等）
"""

import json
import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# L0 层 content 字段截断长度，控制 token 消耗
_CONTENT_MAX_LEN = 500


class ReadExecutionDetailTool(BuiltinTool):
    """执行记录查询工具（复盘专用）。

    复盘 Agent 专用的执行记录查询工具，支持三层渐进披露：
    - skeleton：每轮一行 + 锚点标记，纯规则生成，快速浏览全局
    - L1：查看覆盖该 iteration 的 L1 压缩块（八段摘要）
    - L0：查看原始记录的特定字段（thinking/tool_calls/error 等）

    构造函数接收 ExecutionRecordStorage 实例（通过工具注册时注入），
    从内存缓存读取数据，无需磁盘 IO。
    """

    def __init__(self, storage: Any = None):
        """初始化执行记录查询工具。

        Args:
            storage: ExecutionRecordStorage 实例，通过工具注册时注入。
                     如果未提供，工具将在执行时尝试从 injected_params 获取。
        """
        self._storage = storage

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="read_execution_detail",
            description="查看管道执行的详细记录（渐进式披露）。"
            "先看骨架了解全局，再看L1了解语义，最后看L0了解细节。",
            input_schema={
                "type": "object",
                "properties": {
                    "pipeline_run_id": {
                        "type": "string",
                        "description": "管道运行 ID",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["skeleton", "L1", "L0"],
                        "description": (
                            "skeleton=骨架（每轮一行+锚点），"
                            "L1=压缩块（语义摘要），"
                            "L0=原始记录（thinking/tool_calls/error）"
                        ),
                    },
                    "iteration": {
                        "type": "integer",
                        "description": (
                            "目标轮次。skeleton不指定则返回完整骨架。L1/L0必填。"
                        ),
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "L0层专用：thinking, content, tool_calls, tool_input, "
                            "error, all。默认all。"
                        ),
                    },
                },
                "required": ["pipeline_run_id", "level"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.ANALYSIS,
            level=ToolLevel.L1_ONLY,
            tags=["execution", "analysis", "review"],
            injected_params=["storage"],
        )

    async def execute(self, inputs: dict[str, Any]) -> Any:
        """执行工具逻辑，根据 level 参数分派到对应的处理方法。

        Args:
            inputs: 工具输入参数，包含 pipeline_run_id、level、iteration、fields 等

        Returns:
            工具执行结果
        """
        # 获取 storage 实例（优先使用注入的，否则使用构造函数传入的）
        storage = inputs.get("storage") or self._storage
        if storage is None:
            return create_failure_result(
                error="ExecutionRecordStorage 未注入，无法查询执行记录",
                error_code="STORAGE_NOT_AVAILABLE",
            )

        pipeline_run_id = inputs.get("pipeline_run_id")
        level = inputs.get("level")

        if not pipeline_run_id:
            return create_failure_result(
                error="pipeline_run_id 不能为空",
                error_code="MISSING_PIPELINE_RUN_ID",
            )

        if not level:
            return create_failure_result(
                error="level 不能为空",
                error_code="MISSING_LEVEL",
            )

        # 确保 pipeline 数据已加载
        storage._ensure_loaded(pipeline_run_id)

        # 按 level 分派
        if level == "skeleton":
            return self._get_skeleton(storage, pipeline_run_id)
        if level == "L1":
            return self._get_l1_block(storage, inputs, pipeline_run_id)
        if level == "L0":
            return self._get_l0_records(storage, inputs, pipeline_run_id)
        return create_failure_result(
            error=f"不支持的 level: {level}，可选值：skeleton, L1, L0",
            error_code="INVALID_LEVEL",
        )

    def _get_skeleton(self, storage: Any, pipeline_run_id: str) -> Any:
        """生成骨架视图：每条执行记录压缩为一行。

        遍历指定 pipeline 的所有执行记录，按 iteration 分组，
        每条记录生成一行摘要文本，包括类型、名称和错误标记。

        Args:
            storage: ExecutionRecordStorage 实例
            pipeline_run_id: 管道运行 ID

        Returns:
            骨架视图结果，包含 lines 列表和 total_iterations
        """
        records = storage.list_by_pipeline(pipeline_run_id)[0]

        if not records:
            return create_failure_result(
                error=f"未找到管道 {pipeline_run_id} 的执行记录",
                error_code="RECORDS_NOT_FOUND",
            )

        lines: list[str] = []
        for record in records:
            # 构建单行摘要：[iter {n}] {type} {name}
            parts = [f"[iter {record.iteration}]", record.type]

            if record.name:
                parts.append(record.name)

            # 如果有 content，截取前 50 字符作为摘要
            if record.content:
                content_preview = record.content[:50].replace("\n", " ").strip()
                if content_preview:
                    parts.append(content_preview)

            line = " ".join(parts)

            # 如果有 error，追加错误标记
            if record.error:
                error_preview = record.error[:50].replace("\n", " ").strip()
                line += f" -> error: {error_preview}"

            lines.append(line)

        return create_success_result(
            data={
                "pipeline_run_id": pipeline_run_id,
                "level": "skeleton",
                "total_records": len(records),
                "iterations": sorted({r.iteration for r in records}),
                "lines": lines,
            },
            metadata={"action": "skeleton"},
        )

    def _get_l1_block(
        self,
        storage: Any,
        inputs: dict[str, Any],
        pipeline_run_id: str,
    ) -> Any:
        """查找覆盖目标 iteration 的 L1 压缩块。

        根据 iteration 找到对应的 sequence 范围，再查找覆盖该范围的 L1 块。
        当前实现通过 ExecutionRecordStorage 的数据模拟，
        后续接入 ChunkDB 后可直接查询。

        Args:
            storage: ExecutionRecordStorage 实例
            inputs: 工具输入参数，需包含 iteration
            pipeline_run_id: 管道运行 ID

        Returns:
            L1 压缩块内容，或提示信息
        """
        iteration = inputs.get("iteration")
        if iteration is None:
            return create_failure_result(
                error="L1 层需要指定 iteration 参数",
                error_code="MISSING_ITERATION",
            )

        # 获取该 iteration 的所有记录，用于构建上下文摘要
        all_records = storage.list_by_pipeline(pipeline_run_id)[0]
        target_records = [
            r for r in all_records if r.iteration == iteration
        ]

        if not target_records:
            return create_failure_result(
                error=f"未找到 iteration={iteration} 的执行记录",
                error_code="ITERATION_NOT_FOUND",
            )

        # TODO: 接入 ChunkDB 后，应根据 iteration 对应的 sequence 范围，
        # 从 ChunkMetadataStore 查找覆盖该范围的 L1 块，
        # 再通过 MemoryChunkDB.load_chunk_by_id 加载内容。
        # 当前使用 ExecutionRecordStorage 的数据模拟 L1 摘要。

        # 模拟 L1 摘要：从该 iteration 的记录中提取关键信息
        ai_records = [r for r in target_records if r.type == "ai"]
        tool_records = [r for r in target_records if r.type == "tool"]
        error_records = [r for r in target_records if r.error]

        # 构建 L1 风格的八段摘要
        summary_sections = {
            "iteration": iteration,
            "ai_actions": [],
            "tool_calls_summary": [],
            "errors": [],
            "key_decisions": [],
        }

        # AI 记录摘要
        for r in ai_records:
            action = {"type": r.type, "name": r.name or "unknown"}
            if r.content:
                action["content_preview"] = r.content[:200]
            if r.thinking_content:
                action["thinking_preview"] = r.thinking_content[:200]
            summary_sections["ai_actions"].append(action)

        # 工具调用摘要
        for r in tool_records:
            tool_summary = {"name": r.name or "unknown"}
            if r.content:
                tool_summary["result_preview"] = r.content[:200]
            summary_sections["tool_calls_summary"].append(tool_summary)

        # 错误摘要
        for r in error_records:
            summary_sections["errors"].append({
                "type": r.type,
                "name": r.name,
                "error": r.error[:200] if r.error else "",
            })

        # 关键决策（从 thinking_content 中提取）
        for r in ai_records:
            if r.thinking_content:
                summary_sections["key_decisions"].append({
                    "iteration": r.iteration,
                    "thinking_preview": r.thinking_content[:300],
                })

        return create_success_result(
            data={
                "pipeline_run_id": pipeline_run_id,
                "level": "L1",
                "iteration": iteration,
                "record_count": len(target_records),
                "note": "当前为模拟 L1 摘要（基于 L0 记录生成），"
                        "接入 ChunkDB 后将返回真实 L1 压缩块。",
                "summary": summary_sections,
            },
            metadata={"action": "L1"},
        )

    def _get_l0_records(
        self,
        storage: Any,
        inputs: dict[str, Any],
        pipeline_run_id: str,
    ) -> Any:
        """读取指定 iteration 的原始执行记录，按 fields 过滤返回。

        从 ExecutionRecordStorage 的内存缓存按 iteration 过滤记录，
        再按 fields 参数精确返回指定字段。
        - fields=["thinking"] 只返回 thinking_content
        - fields=["tool_calls"] 只返回 tool_calls_json（解析为 dict）
        - fields=["content"] 只返回 content（截断到 500 字符）
        - fields=["error"] 只返回 error
        - fields=["all"] 或未指定返回全部字段

        Args:
            storage: ExecutionRecordStorage 实例
            inputs: 工具输入参数，需包含 iteration，可选 fields
            pipeline_run_id: 管道运行 ID

        Returns:
            按 fields 过滤后的原始记录列表
        """
        iteration = inputs.get("iteration")
        if iteration is None:
            return create_failure_result(
                error="L0 层需要指定 iteration 参数",
                error_code="MISSING_ITERATION",
            )

        fields = inputs.get("fields") or ["all"]
        # 标准化 fields
        if "all" in fields:
            fields = ["all"]

        all_records = storage.list_by_pipeline(pipeline_run_id)[0]
        target_records = [
            r for r in all_records if r.iteration == iteration
        ]

        if not target_records:
            return create_failure_result(
                error=f"未找到 iteration={iteration} 的执行记录",
                error_code="ITERATION_NOT_FOUND",
            )

        # 按 fields 过滤记录
        filtered_records = []
        for record in target_records:
            filtered = self._filter_record_fields(record, fields)
            filtered_records.append(filtered)

        return create_success_result(
            data={
                "pipeline_run_id": pipeline_run_id,
                "level": "L0",
                "iteration": iteration,
                "record_count": len(filtered_records),
                "records": filtered_records,
            },
            metadata={"action": "L0"},
        )

    def _filter_record_fields(
        self, record: Any, fields: list[str]
    ) -> dict[str, Any]:
        """按 fields 列表过滤单条记录的字段。

        支持的字段：thinking, content, tool_calls, tool_input, error, all。
        content 字段截断到 500 字符控制 token 消耗。
        tool_calls 字段将 JSON 字符串解析为字典返回。

        Args:
            record: ExecutionRecordData 实例
            fields: 需要返回的字段列表

        Returns:
            过滤后的记录字典
        """
        # 基础信息始终包含
        result: dict[str, Any] = {
            "record_id": record.record_id,
            "iteration": record.iteration,
            "sequence": record.sequence,
            "type": record.type,
            "name": record.name,
        }

        if "all" in fields:
            # 返回全部字段
            result["role"] = record.role
            result["content"] = self._truncate_text(
                record.content, _CONTENT_MAX_LEN
            )
            result["thinking_content"] = record.thinking_content
            result["tool_calls"] = self._parse_tool_calls(record.tool_calls_json)
            result["tool_input"] = record.tool_input
            result["error"] = record.error
            result["created_at"] = record.created_at
            return result

        # 按指定字段过滤
        if "thinking" in fields and record.thinking_content:
            result["thinking_content"] = record.thinking_content

        if "content" in fields and record.content:
            result["content"] = self._truncate_text(
                record.content, _CONTENT_MAX_LEN
            )

        if "tool_calls" in fields and record.tool_calls_json:
            result["tool_calls"] = self._parse_tool_calls(record.tool_calls_json)

        if "tool_input" in fields and record.tool_input:
            result["tool_input"] = record.tool_input

        if "error" in fields and record.error:
            result["error"] = record.error

        return result

    @staticmethod
    def _truncate_text(text: str | None, max_len: int) -> str | None:
        """截断长文本，控制 token 消耗。

        Args:
            text: 原始文本
            max_len: 最大字符长度

        Returns:
            截断后的文本，超出部分用省略号标记
        """
        if not text:
            return text
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"...(truncated, total {len(text)} chars)"

    @staticmethod
    def _parse_tool_calls(tool_calls_json: str | None) -> Any:
        """解析 tool_calls JSON 字符串为字典/列表。

        Args:
            tool_calls_json: tool_calls 的 JSON 字符串

        Returns:
            解析后的字典/列表，解析失败返回原始字符串
        """
        if not tool_calls_json:
            return None
        try:
            return json.loads(tool_calls_json)
        except (json.JSONDecodeError, TypeError):
            return tool_calls_json
