"""
LLM 消息日志记录器

实时记录所有发送到 LLM 和从 LLM 接收的完整消息
包括所有消息内容、工具定义、系统提示词等
"""

import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Optional


class LLMMessageLogger:
    """LLM 消息日志记录器

    实时记录所有 LLM 消息到独立文件，记录完整内容
    支持请求ID关联机制，确保请求和响应可以正确配对
    """

    _instance: Optional["LLMMessageLogger"] = None
    _lock: Lock = Lock()

    def __new__(cls) -> "LLMMessageLogger":
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化日志记录器"""
        if hasattr(self, "_initialized"):
            return

        self._initialized = True
        self._file_lock = Lock()

        # 日志文件路径
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        self.log_file = self.log_dir / "llm_messages.log"

        # 请求计数器（用于生成唯一请求ID）
        self._request_counter = 0
        self._counter_lock = Lock()

        # 初始化日志文件（如果不存在）
        if not self.log_file.exists():
            self._write_header()

    def _write_header(self) -> None:
        """写入日志文件头"""
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write("LLM 消息日志 - 完整记录\n")
            f.write(f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

    def _format_timestamp(self) -> str:
        """格式化时间戳"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _generate_request_id(self) -> str:
        """生成唯一请求ID"""
        with self._counter_lock:
            self._request_counter += 1
            # 格式: REQ_YYYYMMDD_HHMMSS_<序号>
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"REQ_{timestamp}_{self._request_counter:06d}"

    def _format_message_full(self, msg: Any | dict[str, Any] | str) -> str:
        """格式化单条消息 - 完整格式，保留多行"""
        if isinstance(msg, str):
            return msg

        # 检查是否有 Message 类型的属性（role, content, tool_calls）
        if hasattr(msg, "role") and hasattr(msg, "content"):
            result = f"[{msg.role}]\n"
            if msg.content:
                result += f"{msg.content}\n"
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                result += "[工具调用]\n"
                for tc in msg.tool_calls:
                    result += f"  - {tc.name}: {json.dumps(tc.arguments, ensure_ascii=False, indent=2)}\n"
            if hasattr(msg, "tool_call_id") and msg.tool_call_id:
                result += f"[工具调用ID] {msg.tool_call_id}\n"
            if hasattr(msg, "name") and msg.name:
                result += f"[工具名称] {msg.name}\n"
            return result
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            result = f"[{role}]\n"
            if "content" in msg and msg["content"]:
                result += f"{msg['content']}\n"
            if "tool_calls" in msg and msg["tool_calls"]:
                result += "[工具调用]\n"
                for tc in msg["tool_calls"]:
                    name = tc.get("name", "unknown")
                    args = tc.get("arguments", {})
                    result += f"  - {name}: {json.dumps(args, ensure_ascii=False, indent=2)}\n"
            if "tool_call_id" in msg and msg["tool_call_id"]:
                result += f"[工具调用ID] {msg['tool_call_id']}\n"
            if "name" in msg and msg["name"]:
                result += f"[工具名称] {msg['name']}\n"
            return result
        if hasattr(msg, "type") and hasattr(msg, "content"):
            # LangChain 消息类型（使用 type 属性）
            # type 映射: tool -> tool, human -> user, ai -> assistant, system -> system
            msg_type = msg.type
            role_mapping = {
                "tool": "tool",
                "human": "user",
                "ai": "assistant",
                "system": "system",
            }
            role = role_mapping.get(msg_type, msg_type)
            result = f"[{role}]\n"
            if msg.content:
                result += f"{msg.content}\n"
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                result += "[工具调用]\n"
                for tc in msg.tool_calls:
                    tc_name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                    tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "arguments", {})
                    result += f"  - {tc_name}: {json.dumps(tc_args, ensure_ascii=False, indent=2)}\n"
            if hasattr(msg, "tool_call_id") and msg.tool_call_id:
                result += f"[工具调用ID] {msg.tool_call_id}\n"
            if hasattr(msg, "name") and msg.name:
                result += f"[工具名称] {msg.name}\n"
            return result
        return str(msg)

    def _format_tools(self, tools: list[Any]) -> str:
        """格式化工具列表

        优先显示实际发送给大模型的 OpenAI 格式，如果已经是 OpenAI 格式则直接显示，
        否则尝试从 Tool 对象转换为 OpenAI 格式。
        """
        if not tools:
            return ""

        result = "[工具定义]\n"
        for tool in tools:
            # 检查是否已经是 OpenAI 格式 {"type": "function", "function": {...}}
            if isinstance(tool, dict) and "type" in tool and "function" in tool:
                # 已经是 OpenAI 格式，直接显示
                func = tool.get("function", {})
                name = func.get("name", "unknown")
                desc = func.get("description", "")
                params = func.get("parameters", {})
                result += f"  - {name}: {desc}\n"
                if params:
                    result += f"    参数: {json.dumps(params, ensure_ascii=False, indent=2)}\n"
            # 检查是否是内部 Tool 对象（有 name 和 parameters 属性）
            elif hasattr(tool, "name"):
                # 尝试转换为 OpenAI 格式
                openai_format = self._convert_to_openai_format(tool)
                if openai_format:
                    func = openai_format.get("function", {})
                    name = func.get("name", tool.name)
                    desc = func.get("description", getattr(tool, "description", ""))
                    params = func.get("parameters", {})
                    result += f"  - {name}: {desc}\n"
                    if params:
                        result += f"    参数: {json.dumps(params, ensure_ascii=False, indent=2)}\n"
            # 简单的 dict 格式
            elif isinstance(tool, dict):
                name = tool.get("name", "unknown")
                desc = tool.get("description", "")
                params = tool.get("parameters", {})
                result += f"  - {name}: {desc}\n"
                if params:
                    result += f"    参数: {json.dumps(params, ensure_ascii=False, indent=2)}\n"
        return result

    def _convert_to_openai_format(self, tool: Any) -> dict[str, Any] | None:
        """将 Tool 对象转换为 OpenAI 格式

        尝试从 Tool 对象提取原始 input_schema，避免 Pydantic 添加额外字段。

        Args:
            tool: Tool 对象

        Returns:
            OpenAI 格式的工具定义，如果转换失败返回 None
        """
        try:
            # 检查是否有 to_llm_format 方法（src.tools.types.Tool 类型）
            if hasattr(tool, "to_llm_format"):
                return tool.to_llm_format()

            # 检查是否有 input_schema 属性（原始 schema）
            if hasattr(tool, "input_schema"):
                input_schema = tool.input_schema
                # 确保 input_schema 有基本的 JSON Schema 结构
                if isinstance(input_schema, dict):
                    # 清理可能的 Pydantic 添加的字段
                    clean_schema = self._clean_schema(input_schema)
                    return {
                        "type": "function",
                        "function": {
                            "name": getattr(tool, "name", "unknown"),
                            "description": getattr(tool, "description", ""),
                            "parameters": clean_schema,
                        },
                    }

            # 检查是否有 parameters 属性
            if hasattr(tool, "parameters"):
                params = tool.parameters
                if isinstance(params, dict):
                    clean_params = self._clean_schema(params)
                    return {
                        "type": "function",
                        "function": {
                            "name": getattr(tool, "name", "unknown"),
                            "description": getattr(tool, "description", ""),
                            "parameters": clean_params,
                        },
                    }

            return None
        except Exception:
            return None

    def _clean_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """清理 JSON Schema，移除 Pydantic 自动添加的字段

        移除的字段：
        - title: Pydantic 自动生成的标题
        - default: null 的默认值

        Args:
            schema: 原始 schema

        Returns:
            清理后的 schema
        """
        if not isinstance(schema, dict):
            return schema

        cleaned = {}
        for key, value in schema.items():
            # 跳过 Pydantic 自动添加的 title 字段
            if key == "title":
                continue

            # 跳过值为 null 的 default 字段
            if key == "default" and value is None:
                continue

            # 递归处理嵌套字典
            if isinstance(value, dict):
                cleaned[key] = self._clean_schema(value)
            # 递归处理列表中的字典
            elif isinstance(value, list):
                cleaned[key] = [
                    self._clean_schema(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                cleaned[key] = value

        return cleaned

    def log_request(
        self,
        model: str,
        messages: list[Any | dict[str, Any]],
        tools: list[Any] | None = None,
        request_id: str | None = None,
        openai_tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> str:
        """记录发送到 LLM 的完整请求

        Args:
            model: 模型名称
            messages: 消息列表
            tools: 原始工具列表（可选）
            request_id: 请求ID（可选，如果不提供则自动生成）
            openai_tools: 转换后的 OpenAI 格式工具列表（可选，优先使用）
            **kwargs: 其他参数

        Returns:
            请求ID（用于关联响应）
        """
        # 生成或使用提供的请求ID
        if request_id is None:
            request_id = self._generate_request_id()

        timestamp = self._format_timestamp()

        output = []
        output.append(f"[{timestamp}] >>> 发送到 {model} | [请求ID: {request_id}]")
        output.append("-" * 80)

        # 记录所有消息
        for msg in messages:
            output.append(self._format_message_full(msg))

        # 记录工具定义（优先使用 openai_tools，这是实际发送给大模型的格式）
        if openai_tools:
            output.append(self._format_tools(openai_tools))
        elif tools:
            output.append(self._format_tools(tools))

        # 记录其他参数
        if kwargs:
            extra_params = {
                k: v
                for k, v in kwargs.items()
                if k not in ["stream", "tools", "tool_choice", "openai_tools"] and v is not None
            }
            if extra_params:
                output.append(
                    f"[其他参数] {json.dumps(extra_params, ensure_ascii=False, indent=2)}"
                )

        output.append("-" * 80)
        output.append("")

        self._write_to_file("\n".join(output))

        return request_id

    def log_response(
        self,
        model: str,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
        finish_reason: str | None = None,
        raw_response: Any | None = None,
        request_id: str | None = None,
        reasoning_content: str | None = None,
        **kwargs,
    ) -> None:
        """记录从 LLM 接收的完整响应

        Args:
            model: 模型名称
            content: 响应内容
            tool_calls: 工具调用列表
            usage: Token 使用情况
            finish_reason: 结束原因
            raw_response: 原始响应对象（可选）
            request_id: 请求ID（用于关联请求）
            reasoning_content: 思考内容（适用于思考模型如 deepseek-reasoner）
            **kwargs: 其他参数
        """
        timestamp = self._format_timestamp()

        output = []
        request_id_str = f" | [请求ID: {request_id}]" if request_id else ""
        output.append(f"[{timestamp}] <<< 从 {model} 接收{request_id_str}")
        output.append("-" * 80)

        # 记录思考内容（如果存在）
        if reasoning_content:
            output.append(f"[思考过程]\n{reasoning_content}")
            output.append("")

        if content:
            output.append(f"[响应内容]\n{content}")

        if tool_calls:
            output.append("[工具调用]")
            for tc in tool_calls:
                name = tc.get("name", "unknown")
                args = tc.get("arguments", {})
                tc_id = tc.get("id", "")
                output.append(
                    f"  [{tc_id}] {name}: {json.dumps(args, ensure_ascii=False, indent=2)}"
                )

        if usage:
            output.append(f"[Token 使用] {json.dumps(usage, ensure_ascii=False)}")

        if finish_reason:
            output.append(f"[结束原因] {finish_reason}")

        output.append("-" * 80)
        output.append("")

        self._write_to_file("\n".join(output))

    def log_stream_start(
        self,
        model: str,
        messages: list[Any | dict[str, Any]],
        tools: list[Any] | None = None,
        openai_tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> None:
        """记录流式请求开始

        Args:
            model: 模型名称
            messages: 消息列表
            tools: 原始工具列表（可选）
            openai_tools: 转换后的 OpenAI 格式工具列表（可选，优先使用）
            **kwargs: 其他参数
        """
        timestamp = self._format_timestamp()

        output = []
        output.append(f"[{timestamp}] >>>>> 流式请求开始: {model}")
        output.append("=" * 80)

        # 记录所有消息
        for msg in messages:
            output.append(self._format_message_full(msg))

        # 记录工具定义（优先使用 openai_tools，这是实际发送给大模型的格式）
        if openai_tools:
            output.append(self._format_tools(openai_tools))
        elif tools:
            output.append(self._format_tools(tools))

        output.append("=" * 80)
        output.append("")

        self._write_to_file("\n".join(output))

    def log_stream_chunk(
        self, model: str, chunk: str, accumulated: str | None = None
    ) -> None:
        """记录流式响应片段（不记录每个chunk，太频繁）"""

    def log_stream_end(
        self,
        model: str,
        full_content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """记录流式响应完成

        Args:
            model: 模型名称
            full_content: 完整内容
            tool_calls: 工具调用列表
            usage: Token 使用情况
            finish_reason: 结束原因
        """
        timestamp = self._format_timestamp()

        output = []
        output.append(f"[{timestamp}] <<<<< 流式响应完成: {model}")
        output.append("=" * 80)

        if full_content:
            output.append(f"[完整内容]\n{full_content}")

        if tool_calls:
            output.append("[工具调用]")
            for tc in tool_calls:
                name = tc.get("name", "unknown")
                args = tc.get("arguments", {})
                tc_id = tc.get("id", "")
                output.append(
                    f"  [{tc_id}] {name}: {json.dumps(args, ensure_ascii=False, indent=2)}"
                )

        if usage:
            output.append(f"[Token 使用] {json.dumps(usage, ensure_ascii=False)}")

        if finish_reason:
            output.append(f"[结束原因] {finish_reason}")

        output.append("=" * 80)
        output.append("")

        self._write_to_file("\n".join(output))

    def log_error(
        self,
        model: str,
        error: Exception,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        """记录错误

        Args:
            model: 模型名称
            error: 错误对象
            context: 上下文信息
            request_id: 请求ID（用于关联请求）
        """
        timestamp = self._format_timestamp()

        output = []
        request_id_str = f" | [请求ID: {request_id}]" if request_id else ""
        output.append(f"[{timestamp}] !!! 错误: {model}{request_id_str}")
        output.append("!" * 80)
        output.append(f"[错误类型] {type(error).__name__}")
        output.append(f"[错误信息] {str(error)}")

        if context:
            output.append(
                f"[上下文] {json.dumps(context, ensure_ascii=False, indent=2)}"
            )

        output.append("!" * 80)
        output.append("")

        self._write_to_file("\n".join(output))

    def _write_to_file(self, content: str) -> None:
        """线程安全地写入文件

        Args:
            content: 要写入的内容
        """
        with self._file_lock, open(self.log_file, "a", encoding="utf-8") as f:
            f.write(content)

    def clear_log(self):
        """清空日志文件"""
        with self._file_lock:
            self._write_header()

    def get_log_path(self) -> str:
        """获取日志文件路径"""
        return str(self.log_file.absolute())


# 全局单例实例
_message_logger: LLMMessageLogger | None = None


def get_message_logger() -> LLMMessageLogger:
    """获取消息日志记录器单例"""
    global _message_logger
    if _message_logger is None:
        _message_logger = LLMMessageLogger()
    return _message_logger
