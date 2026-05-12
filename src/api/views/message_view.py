"""消息视图层 - 简化 ExecutionRecord 到消息格式的转换

优化目标:
1. 减少转换逻辑复杂度
2. 提升转换性能
3. 统一消息格式处理
4. 支持批量转换优化
"""

from dataclasses import dataclass
from typing import Any

from src.auth.models import UserInDB
from src.db.models import ExecutionRecord


@dataclass
class MessageData:
    """标准消息数据结构"""

    id: str
    session_id: str
    parent_id: str | None  # 父执行记录 ID（用于消息层级过滤）
    role: str  # "user" | "assistant"
    content: str
    timestamp: str
    status: str

    # 发送者信息
    sender_type: str  # "user" | "agent"
    sender_id: str
    sender_name: str

    # Agent 信息
    agent_id: str | None
    agent_name: str

    # 元数据
    metadata: dict[str, Any]


class MessageView:
    """消息视图转换器 - 优化 ExecutionRecord 到消息格式的转换"""

    @staticmethod
    def from_execution_record(
        record: ExecutionRecord,
        current_user: UserInDB,
        session_agent_id: str | None = None,
    ) -> MessageData:
        """
        从 ExecutionRecord 转换为标准消息格式

        Args:
            record: 执行记录
            current_user: 当前用户
            session_agent_id: 会话关联的 Agent ID

        Returns:
            MessageData: 标准消息数据
        """
        # 从 message_data JSON 字段提取数据
        message_data = record.message_data or {}
        executor = message_data.get("executor", {})
        order = message_data.get("order", {})

        # 提取字段
        record_type = message_data.get("record_type", "unknown")
        executor_type = executor.get("type", "")
        executor_id = executor.get("id", "")
        executor_name = executor.get("name", "")
        content = message_data.get("content", "")
        status = message_data.get("status", "unknown")
        input_data = message_data.get("input")
        output_data = message_data.get("output")
        sequence = order.get("sequence", 0)
        # 支持两种字段名：thinking（新）和 thinking_content（旧）
        thinking_content = message_data.get("thinking") or message_data.get("thinking_content")
        tool_calls = message_data.get("tool_calls")  # 提取工具调用信息
        segments = message_data.get("segments")  # 提取消息分段信息

        # 确定角色
        # 支持多种用户消息类型：user_message (旧) 和 user_input (新)
        is_user_message = record_type in ("user_message", "user_input")
        role = "user" if is_user_message else "assistant"

        # 确定发送者信息
        if is_user_message:
            sender_type = "user"
            sender_id = str(current_user.id)
            sender_name = current_user.username
        else:
            sender_type = "agent"
            sender_id = executor_id or session_agent_id or ""
            sender_name = executor_name or "AI助手"

        # 确定 Agent 信息
        agent_id = executor_id or session_agent_id
        agent_name = executor_name or "AI助手"

        # 构建元数据
        metadata = {
            "record_type": record_type,
            "type": message_data.get("type") or ("tool" if record_type == "tool_execution" else None),  # 添加 type 字段（用于识别工具消息）
            "executor_type": executor_type,
            "executor_id": executor_id,
            "executor_name": executor_name,
            "input_data": input_data,
            "output_data": output_data,
            "sequence": sequence,
            "thinking_content": thinking_content,
            "tool_calls": tool_calls,  # 添加工具调用信息到元数据
            "segments": segments,  # 添加消息分段信息到元数据
            # 工具消息相关字段
            "name": message_data.get("name"),
            "tool_call_id": message_data.get("tool_call_id"),
            "status": message_data.get("status"),
            "error": message_data.get("error"),
            "duration_ms": message_data.get("duration_ms"),
        }

        return MessageData(
            id=str(record.id),
            session_id=str(record.session_id),
            parent_id=str(record.parent_record_id) if record.parent_record_id else None,
            role=role,
            content=content,
            timestamp=record.created_at.isoformat(),
            status=status,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_name=sender_name,
            agent_id=agent_id,
            agent_name=agent_name,
            metadata=metadata,
        )

    @staticmethod
    def to_api_response(message: MessageData) -> dict[str, Any]:
        """
        转换为 API 响应格式

        Args:
            message: 消息数据

        Returns:
            Dict: API 响应格式
        """
        # 提取 tool_calls 和 segments 从 metadata
        tool_calls = message.metadata.get("tool_calls") if message.metadata else None
        segments = message.metadata.get("segments") if message.metadata else None

        # 转换 tool_calls 为前端期望的格式
        formatted_tool_calls = None
        if tool_calls and isinstance(tool_calls, list):
            formatted_tool_calls = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    # 处理不同格式的 tool_call
                    formatted_tc = {
                        "call_id": tc.get("id") or tc.get("call_id") or tc.get("tool_call_id", ""),
                        "tool_name": tc.get("name") or tc.get("tool_name") or tc.get("function", {}).get("name", ""),
                        "tool_args": tc.get("args") or tc.get("tool_args") or tc.get("function", {}).get("arguments", {}),
                        "status": tc.get("status", "completed"),
                        "result": tc.get("result"),
                        "error": tc.get("error"),
                        "started_at": tc.get("started_at"),
                        "completed_at": tc.get("ended_at"),
                        "duration_ms": tc.get("duration_ms"),
                    }
                    formatted_tool_calls.append(formatted_tc)

        # 转换 segments 为前端期望的格式（同时支持下划线和驼峰命名）
        formatted_segments = None
        if segments and isinstance(segments, list):
            formatted_segments = []
            for seg in segments:
                if isinstance(seg, dict):
                    formatted_seg = {
                        "type": seg.get("type", "text"),
                    }
                    if seg.get("type") == "text":
                        formatted_seg["content"] = seg.get("content", "")
                    elif seg.get("type") == "tool_call":
                        formatted_seg["toolCallId"] = seg.get("tool_call_id") or seg.get("toolCallId", "")
                        formatted_seg["toolName"] = seg.get("tool_name") or seg.get("toolName", "")
                    formatted_segments.append(formatted_seg)

        return {
            "id": message.id,
            "sessionId": message.session_id,
            "session_id": message.session_id,  # 兼容旧格式
            "parentId": message.parent_id,  # 驼峰命名（前端期望）
            "parent_id": message.parent_id,  # 下划线命名（兼容）
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp,
            "created_at": message.timestamp,  # 兼容旧格式
            "status": message.status,
            "sender_type": message.sender_type,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "agent_id": message.agent_id,
            "agent_name": message.agent_name,
            "metadata": message.metadata,
            "toolCalls": formatted_tool_calls,  # 驼峰命名（前端期望）
            "tool_calls": formatted_tool_calls,  # 下划线命名（兼容）
            "segments": formatted_segments,  # 添加消息分段字段
        }

    @classmethod
    def batch_convert_records(
        cls,
        records_with_session: list[tuple],  # [(ExecutionRecord, session_agent_id), ...]
        current_user: UserInDB,
    ) -> list[dict[str, Any]]:
        """
        批量转换执行记录为消息格式

        Args:
            records_with_session: 执行记录和会话 Agent ID 的元组列表
            current_user: 当前用户

        Returns:
            List[Dict]: API 响应格式的消息列表
        """
        messages = []
        for record, session_agent_id in records_with_session:
            message_data = cls.from_execution_record(
                record=record,
                current_user=current_user,
                session_agent_id=session_agent_id,
            )
            messages.append(cls.to_api_response(message_data))

        return messages


class MessageQueryBuilder:
    """消息查询构建器 - 优化数据库查询，支持嵌套结构"""

    @staticmethod
    def build_base_conditions(
        thread_id: str,
        user_id: str,
        parent_id: str | None = None,
        depth: int | None = None,
    ) -> list:
        """
        构建基础查询条件

        Args:
            thread_id: 线程 ID
            user_id: 用户 ID
            parent_id: 父记录 ID（用于获取嵌套子记录）
            depth: 嵌套深度（用于筛选特定深度的记录）

        Returns:
            List: 查询条件列表
        """

        from src.db.models import ExecutionRecord, Session

        conditions = [
            Session.id == thread_id,
            Session.user_id == user_id,
            ExecutionRecord.session_id == Session.id,
            ExecutionRecord.message_data["version"]["is_current"]
            .as_boolean()
            .is_(True),  # 从 JSON 字段查询
        ]

        # 添加 parent_id 筛选（支持嵌套结构）
        if parent_id is not None:
            conditions.append(ExecutionRecord.parent_record_id == parent_id)

        # 添加 depth 筛选（支持嵌套深度筛选）
        if depth is not None:
            conditions.append(
                ExecutionRecord.message_data["order"]["depth"].as_integer() == depth
            )

        return conditions

    @staticmethod
    def build_agent_filter_condition(agent_id: str):
        """
        构建 Agent 过滤条件

        Args:
            agent_id: Agent ID

        Returns:
            查询条件
        """
        from sqlalchemy import and_, or_

        from src.db.models import ExecutionRecord, Session

        return or_(
            ExecutionRecord.message_data["executor"]["id"].as_string()
            == agent_id,  # 从 JSON 字段匹配
            Session.agent_id == agent_id,  # 或者会话绑定的 Agent
            and_(
                # 支持多种用户消息类型：user_message (旧) 和 user_input (新)
                ExecutionRecord.message_data["record_type"].as_string().in_(
                    ["user_message", "user_input"]
                ),  # 用户消息不过滤
                Session.agent_id == agent_id,
            ),
        )

    @staticmethod
    def build_executor_type_condition(executor_type: str):
        """
        构建执行器类型过滤条件

        Args:
            executor_type: 执行器类型 (agent/tool/user/workflow)

        Returns:
            查询条件
        """
        from src.db.models import ExecutionRecord

        return (
            ExecutionRecord.message_data["executor"]["type"].as_string()
            == executor_type
        )


# Note: MessageCache 已移除
# 原因：
# 1. 数据库查询性能足够（有索引优化）
# 2. 缓存维护复杂度高（需要处理各种失效场景）
# 3. 5分钟缓存对用户体验提升有限
# 4. 减少一层缓存 = 减少一层 bug 可能性
#
# 如需重新启用，请从 git 历史恢复
