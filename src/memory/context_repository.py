"""
上下文数据仓库

负责记忆系统各层数据的持久化读写操作。
封装数据库访问，无业务逻辑。
"""

import json
import logging
from typing import Any

from sqlalchemy import text

from src.core.tokenizer import get_token_counter
from src.db.connection import get_session_context
from src.utils.message_id_helper import generate_execution_record_id

logger = logging.getLogger(__name__)


class ContextRepository:
    """
    上下文数据仓库

    职责：
    - 数据库读写，无业务逻辑
    - 操作 memory_chunks 表
    - 从 execution_records 表读取 L0 层原始执行记录

    注意：
    - 所有方法都是异步的
    - 使用现有的数据库操作方法，不直接写SQL
    """

    def __init__(self):
        """初始化上下文仓库"""
        self.token_counter = get_token_counter()

    async def get_layer(
        self,
        session_id: str,
        layer: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> str:
        """
        获取指定层的内容

        Args:
            session_id: 会话ID
            layer: 层级名称（L1/L2/L3）
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            该层的内容，如果不存在返回空字符串
        """
        async with get_session_context() as session:
            # 构建查询条件
            conditions = ["session_id = :session_id", "layer = :layer"]
            params = {"session_id": session_id, "layer": layer.upper()}

            if executor_type is not None:
                conditions.append("executor_type = :executor_type")
                params["executor_type"] = executor_type
            if executor_id is not None:
                conditions.append("executor_id = :executor_id")
                params["executor_id"] = executor_id

            where_clause = " AND ".join(conditions)
            query = text(f"""
                SELECT content
                FROM memory_chunks
                WHERE {where_clause}
                ORDER BY created_at ASC
            """)

            result = await session.execute(query, params)
            rows = result.fetchall()

            if not rows:
                return ""

            # 合并所有内容（用分隔符连接）
            contents = [row.content for row in rows if row.content]
            return "\n\n---\n\n".join(contents) if contents else ""

    async def save_layer(
        self,
        session_id: str,
        layer: str,
        content: str,
        user_id: str = "",
        executor_id: str = None,
        executor_type: str = None,
        executor_name: str = None,
    ) -> str:
        """
        保存指定层的内容

        Args:
            session_id: 会话ID
            layer: 层级名称（L1/L2/L3）
            content: 要保存的内容
            user_id: 用户ID（可选）
            executor_id: 执行者ID（可选）
            executor_type: 执行者类型（可选）
            executor_name: 执行者名称（可选）

        Returns:
            保存的chunk ID
        """
        import uuid

        chunk_id = str(uuid.uuid4())
        token_count = self.token_counter.count_tokens(content)

        async with get_session_context() as session:
            query = text("""
                INSERT INTO memory_chunks
                (id, user_id, session_id, executor_type, executor_id, executor_name,
                 layer, content, token_count, graduated, created_at)
                VALUES (:id, :user_id, :session_id, :executor_type, :executor_id, :executor_name,
                        :layer, :content, :token_count, FALSE, NOW())
            """)

            await session.execute(
                query,
                {
                    "id": chunk_id,
                    "user_id": user_id or "",
                    "session_id": session_id,
                    "executor_type": executor_type,
                    "executor_id": executor_id,
                    "executor_name": executor_name,
                    "layer": layer.upper(),
                    "content": content,
                    "token_count": token_count,
                }
            )
            await session.commit()

        logger.debug(f"[ContextRepository] 保存 {layer} 层内容: {len(content)} 字符, {token_count} tokens")
        return chunk_id

    async def get_total_tokens(
        self,
        session_id: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> int:
        """
        获取会话的总token数

        计算 memory_chunks 表中该会话所有内容的token总数
        加上 execution_records 表中对应执行者的记录token数

        Args:
            session_id: 会话ID
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            总token数
        """
        async with get_session_context() as session:
            # 获取 memory_chunks 的token数
            chunks_conditions = ["session_id = :session_id"]
            chunks_params = {"session_id": session_id}

            if executor_type is not None:
                chunks_conditions.append("(executor_type = :executor_type OR executor_type IS NULL)")
                chunks_params["executor_type"] = executor_type
            if executor_id is not None:
                chunks_conditions.append("(executor_id = :executor_id OR executor_id IS NULL)")
                chunks_params["executor_id"] = executor_id

            chunks_where = " AND ".join(chunks_conditions)
            chunks_query = text(f"""
                SELECT COALESCE(SUM(token_count), 0) as total
                FROM memory_chunks
                WHERE {chunks_where}
            """)

            result = await session.execute(chunks_query, chunks_params)
            chunks_tokens = result.scalar() or 0

            # 获取 execution_records 的token数（L0层原始记录）
            # 从 message_data 中提取 content 字段计算token
            exec_query = text("""
                SELECT message_data
                FROM execution_records
                WHERE session_id = :session_id
            """)

            result = await session.execute(exec_query, {"session_id": session_id})
            records = result.fetchall()

            exec_tokens = 0
            for row in records:
                message_data = row.message_data if isinstance(row.message_data, dict) else {}

                # 按执行者过滤
                if executor_type or executor_id:
                    executor = message_data.get("executor", {})
                    if executor_type and executor.get("type") != executor_type:
                        continue
                    if executor_id and executor.get("id") != executor_id:
                        continue

                # 计算token数
                content = message_data.get("content", "")
                if content:
                    exec_tokens += self.token_counter.count_tokens(content)

            total = chunks_tokens + exec_tokens
            logger.debug(f"[ContextRepository] 会话 {session_id} 总token数: {total}")
            return total

    async def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        executor_type: str = None,
        executor_id: str = None,
        executor_name: str = None,
    ) -> str:
        """
        追加消息到执行记录表（L0层）

        将消息保存到 execution_records 表作为原始执行记录。

        Args:
            session_id: 会话ID
            message: 消息字典，包含 role 和 content
            executor_type: 执行者类型（agent/tool/workflow）
            executor_id: 执行者ID
            executor_name: 执行者名称

        Returns:
            保存的记录ID
        """
        role = message.get("role", "user")
        content = message.get("content", "")

        if not content:
            logger.warning(f"[ContextRepository] 尝试保存空内容消息，role={role}")
            return ""

        async with get_session_context() as session:
            # 使用 generate_execution_record_id 生成嵌套ID
            record_id = await generate_execution_record_id(session, session_id)

            # 构建 message_data
            message_data = {
                "type": "ai" if role == "assistant" else "tool" if role == "tool" else "human",
                "record_type": "ai_response" if role == "assistant" else "tool_execution" if role == "tool" else "user_input",
                "role": role,
                "content": content,
                "executor": {
                    "type": executor_type or "agent",
                    "id": executor_id or "",
                    "name": executor_name or "",
                } if executor_type or executor_id else message.get("executor", {}),
            }

            query = text("""
                INSERT INTO execution_records
                (id, session_id, parent_record_id, message_data, created_at)
                VALUES (:id, :session_id, NULL, :message_data, datetime('now'))
            """)

            await session.execute(
                query,
                {
                    "id": record_id,
                    "session_id": session_id,
                    "message_data": json.dumps(message_data, ensure_ascii=False),
                }
            )
            await session.commit()

        logger.debug(f"[ContextRepository] 追加消息到 execution_records: role={role}, {len(content)} 字符")
        return record_id

    async def get_recent_messages(
        self,
        session_id: str,
        limit: int = None,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取最近的消息列表（L0层）

        从 execution_records 表读取原始执行记录。

        Args:
            session_id: 会话ID
            limit: 限制返回数量（可选）
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            消息列表，每个消息包含 role 和 content
        """
        async with get_session_context() as session:
            # 获取所有记录，然后在Python中过滤
            if limit:
                query = text("""
                    SELECT message_data
                    FROM execution_records
                    WHERE session_id = :session_id
                    ORDER BY created_at ASC
                    LIMIT :limit
                """)
                result = await session.execute(
                    query,
                    {"session_id": session_id, "limit": limit}
                )
            else:
                query = text("""
                    SELECT message_data
                    FROM execution_records
                    WHERE session_id = :session_id
                    ORDER BY created_at ASC
                """)
                result = await session.execute(query, {"session_id": session_id})

            rows = result.fetchall()
            messages = []

            for row in rows:
                # 解析 message_data（支持 dict 和 JSON 字符串）
                if isinstance(row.message_data, dict):
                    message_data = row.message_data
                elif isinstance(row.message_data, str):
                    try:
                        import json
                        message_data = json.loads(row.message_data)
                    except Exception as e:
                        logger.warning(f"[get_uncompressed_messages] JSON解析失败: {e}")
                        message_data = {}
                else:
                    message_data = {}

                # 按执行者过滤
                if executor_type or executor_id:
                    executor = message_data.get("executor", {})
                    if executor_type and executor.get("type") != executor_type:
                        continue
                    if executor_id and executor.get("id") != executor_id:
                        continue

                # 提取 role 和 content
                role = message_data.get("role", "user")
                content = message_data.get("content", "")

                if content:
                    messages.append({
                        "role": role,
                        "content": content,
                        "executor_type": message_data.get("executor", {}).get("type"),
                        "executor_id": message_data.get("executor", {}).get("id"),
                    })

            return messages

    async def clear_layer(
        self,
        session_id: str,
        layer: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> None:
        """
        清空指定层的内容

        Args:
            session_id: 会话ID
            layer: 层级名称（L1/L2/L3）
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）
        """
        async with get_session_context() as session:
            # 构建查询条件
            conditions = ["session_id = :session_id", "layer = :layer"]
            params = {"session_id": session_id, "layer": layer.upper()}

            if executor_type is not None:
                conditions.append("executor_type = :executor_type")
                params["executor_type"] = executor_type
            if executor_id is not None:
                conditions.append("executor_id = :executor_id")
                params["executor_id"] = executor_id

            where_clause = " AND ".join(conditions)
            query = text(f"""
                DELETE FROM memory_chunks
                WHERE {where_clause}
            """)

            result = await session.execute(query, params)
            await session.commit()

            logger.debug(f"[ContextRepository] 清空 {layer} 层: {result.rowcount} 条记录")

    async def clear_execution_records(
        self,
        session_id: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> None:
        """
        清空执行记录（L0层）

        Args:
            session_id: 会话ID
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）
        """
        async with get_session_context() as session:
            if executor_type or executor_id:
                # 需要按执行者过滤，先查询再删除
                query = text("""
                    SELECT id, message_data
                    FROM execution_records
                    WHERE session_id = :session_id
                """)
                result = await session.execute(query, {"session_id": session_id})
                rows = result.fetchall()

                deleted_count = 0
                for row in rows:
                    message_data = row.message_data if isinstance(row.message_data, dict) else {}
                    executor = message_data.get("executor", {})

                    match = True
                    if executor_type and executor.get("type") != executor_type:
                        match = False
                    if executor_id and executor.get("id") != executor_id:
                        match = False

                    if match:
                        delete_query = text("""
                            DELETE FROM execution_records
                            WHERE id = :id
                        """)
                        await session.execute(delete_query, {"id": row.id})
                        deleted_count += 1

                await session.commit()
                logger.debug(f"[ContextRepository] 清空执行记录: {deleted_count} 条记录")
            else:
                # 清空所有记录
                query = text("""
                    DELETE FROM execution_records
                    WHERE session_id = :session_id
                """)

                result = await session.execute(query, {"session_id": session_id})
                await session.commit()

                logger.debug(f"[ContextRepository] 清空执行记录: {result.rowcount} 条记录")

    async def clear_recent_messages(self, session_id: str) -> None:
        """
        清空最近消息（L0层）- 兼容性方法

        已废弃，请使用 clear_execution_records。

        Args:
            session_id: 会话ID
        """
        await self.clear_execution_records(session_id)

    async def get_layer_message_count(
        self,
        session_id: str,
        layer: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> int:
        """
        获取某层压缩块中包含的消息总数

        用于分层读取策略，计算需要跳过的消息数量。
        例如：L1 层压缩了 50 条消息，则 L0 层读取时跳过前 50 条。

        Args:
            session_id: 会话ID
            layer: 层级名称（L1/L2/L3）
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            消息总数（所有压缩块的 message_count 之和）
        """
        async with get_session_context() as session:
            # 构建查询条件
            conditions = ["session_id = :session_id", "layer = :layer"]
            params = {"session_id": session_id, "layer": layer.upper()}

            if executor_type is not None:
                conditions.append("(executor_type = :executor_type OR executor_type IS NULL)")
                params["executor_type"] = executor_type
            if executor_id is not None:
                conditions.append("(executor_id = :executor_id OR executor_id IS NULL)")
                params["executor_id"] = executor_id

            where_clause = " AND ".join(conditions)
            query = text(f"""
                SELECT COALESCE(SUM(message_count), 0) as total
                FROM memory_chunks
                WHERE {where_clause}
            """)

            result = await session.execute(query, params)
            total_count = result.scalar() or 0

            logger.debug(
                f"[ContextRepository] {layer} 层消息总数: {total_count} | "
                f"session_id={session_id}, executor_id={executor_id}"
            )
            return total_count

    async def get_layer_chunks(
        self,
        session_id: str,
        layer: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取某层的所有压缩块

        用于块级读取，每个块不可分割。

        Args:
            session_id: 会话ID
            layer: 层级名称（L1/L2/L3）
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            压缩块列表，每个块包含 id, content, message_count, token_count 等
        """
        async with get_session_context() as session:
            # 构建查询条件
            conditions = ["session_id = :session_id", "layer = :layer"]
            params = {"session_id": session_id, "layer": layer.upper()}

            if executor_type is not None:
                conditions.append("(executor_type = :executor_type OR executor_type IS NULL)")
                params["executor_type"] = executor_type
            if executor_id is not None:
                conditions.append("(executor_id = :executor_id OR executor_id IS NULL)")
                params["executor_id"] = executor_id

            where_clause = " AND ".join(conditions)
            query = text(f"""
                SELECT id, content, message_count, token_count, created_at
                FROM memory_chunks
                WHERE {where_clause}
                ORDER BY created_at ASC
            """)

            result = await session.execute(query, params)
            rows = result.fetchall()

            chunks = []
            for row in rows:
                chunks.append({
                    "id": row.id,
                    "content": row.content,
                    "message_count": row.message_count,
                    "token_count": row.token_count,
                    "created_at": row.created_at,
                })

            logger.debug(
                f"[ContextRepository] {layer} 层块数: {len(chunks)} | "
                f"session_id={session_id}, executor_id={executor_id}"
            )
            return chunks

    async def get_uncompressed_messages(
        self,
        session_id: str,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取未压缩的原文消息

        即：没有对应 L1 压缩块的执行记录。
        通过计算 L1 层的 message_count 总和，跳过前 N 条执行记录。

        Args:
            session_id: 会话ID
            executor_type: 执行者类型（可选，用于隔离）
            executor_id: 执行者ID（可选，用于隔离）

        Returns:
            未压缩的消息列表
        """
        async with get_session_context() as session:
            # 1. 获取 L1 层压缩的消息总数
            l1_conditions = ["session_id = :session_id", "layer = 'L1'"]
            l1_params = {"session_id": session_id}

            if executor_type is not None:
                l1_conditions.append("(executor_type = :executor_type OR executor_type IS NULL)")
                l1_params["executor_type"] = executor_type
            if executor_id is not None:
                l1_conditions.append("(executor_id = :executor_id OR executor_id IS NULL)")
                l1_params["executor_id"] = executor_id

            l1_where = " AND ".join(l1_conditions)
            l1_query = text(f"""
                SELECT COALESCE(SUM(message_count), 0) as total
                FROM memory_chunks
                WHERE {l1_where}
            """)

            l1_result = await session.execute(l1_query, l1_params)
            compressed_count = l1_result.scalar() or 0

            # 2. 获取所有执行记录
            exec_query = text("""
                SELECT message_data, created_at
                FROM execution_records
                WHERE session_id = :session_id
                ORDER BY created_at ASC
            """)

            exec_result = await session.execute(exec_query, {"session_id": session_id})
            rows = exec_result.fetchall()

            messages = []
            current_index = 0

            logger.debug(f"[get_uncompressed_messages] 查询到 {len(rows)} 条执行记录")

            for row in rows:
                # 解析 message_data（支持 dict 和 JSON 字符串）
                if isinstance(row.message_data, dict):
                    message_data = row.message_data
                elif isinstance(row.message_data, str):
                    try:
                        import json
                        message_data = json.loads(row.message_data)
                    except Exception as e:
                        logger.warning(f"[get_uncompressed_messages] JSON解析失败: {e}")
                        message_data = {}
                else:
                    message_data = {}

                # 按执行者过滤
                if executor_type or executor_id:
                    executor = message_data.get("executor", {})
                    if executor_type and executor.get("type") != executor_type:
                        logger.debug(f"[get_uncompressed_messages] 跳过消息: executor_type不匹配 | 消息={executor.get('type')} | 期望={executor_type}")
                        continue
                    if executor_id and executor.get("id") != executor_id:
                        logger.debug(f"[get_uncompressed_messages] 跳过消息: executor_id不匹配 | 消息={executor.get('id')} | 期望={executor_id}")
                        continue

                # 跳过已压缩的消息
                if current_index < compressed_count:
                    current_index += 1
                    continue

                # 提取消息内容和类型
                msg_type = message_data.get("type", "")
                role = message_data.get("role", "")
                content = message_data.get("content", "")
                tool_call_id = message_data.get("tool_call_id", "")
                tool_name = message_data.get("name", "")

                # 处理工具类型的记录（来自 execute_tools_node）
                if msg_type == "tool":
                    # 工具记录使用 "name" 和 "input" 字段
                    message_data.get("input", {})
                    tool_output = message_data.get("output", {})
                    tool_error = message_data.get("error", "")
                    tool_status = message_data.get("status", "")

                    # 构建工具消息内容
                    # 返回给 LLM 的消息只有两种情况：执行成功或执行失败
                    # 优先使用 status 字段判断，其次检查 output/error 内容
                    if tool_status == "completed" or (tool_output and tool_output != {}):
                        # 执行成功
                        if isinstance(tool_output, dict) and "result" in tool_output:
                            # output 格式: {"result": ...}
                            result_data = tool_output.get("result")
                            if result_data is not None:
                                content = f"工具 {tool_name} 执行成功\n返回结果:\n{json.dumps(result_data, ensure_ascii=False, indent=2)}"
                            else:
                                content = f"工具 {tool_name} 执行成功\n返回结果: 无"
                        else:
                            content = f"工具 {tool_name} 执行成功\n返回结果:\n{json.dumps(tool_output, ensure_ascii=False, indent=2)}"
                    elif tool_status == "failed" or tool_error:
                        # 执行失败
                        content = f"工具 {tool_name} 执行失败\n错误信息:\n{tool_error if tool_error else '未知错误'}"
                    else:
                        # 没有明确状态，默认为执行失败（不应该出现这种情况）
                        logger.warning(
                            f"[get_uncompressed_messages] 工具记录缺少状态信息 | "
                            f"tool_name={tool_name} | tool_call_id={tool_call_id} | "
                            f"status={tool_status} | output={tool_output} | error={tool_error}"
                        )
                        content = f"工具 {tool_name} 执行失败\n错误信息: 工具执行记录不完整，缺少输出或错误信息"

                    role = "tool"

                    messages.append({
                        "role": role,
                        "content": content,
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                    })
                    logger.debug(f"[get_uncompressed_messages] 添加工具消息 | name={tool_name} | tool_call_id={tool_call_id} | content={content[:50]}...")

                # 处理 AI 消息（包括有 tool_calls 但 content 为空的情况）
                elif msg_type in ("human", "ai") or content or message_data.get("tool_calls"):
                    # 支持两种消息格式：
                    # 格式1: {type: "human"/"ai", ...} (来自 MessagePersistence)
                    # 格式2: {role: "user"/"assistant", executor: {...}, ...} (来自 append_message)

                    # 格式转换：type -> role
                    if not role and msg_type:
                        if msg_type == "human":
                            role = "user"
                        elif msg_type == "ai":
                            role = "assistant"
                        else:
                            role = "user"  # 默认
                    elif not role:
                        role = "user"  # 默认

                    # 获取 executor 信息（格式2）
                    msg_executor = message_data.get("executor", {})
                    executor_type = msg_executor.get("type")
                    executor_id = msg_executor.get("id")

                    message_dict = {
                        "role": role,
                        "content": content or "",  # 确保 content 不为 None
                        "executor_type": executor_type,
                        "executor_id": executor_id,
                    }
                    # 提取 tool_calls（如果有）
                    tool_calls = message_data.get("tool_calls")
                    if tool_calls:
                        message_dict["tool_calls"] = tool_calls
                    messages.append(message_dict)
                    logger.debug(f"[get_uncompressed_messages] 添加消息 | role={role} | executor_id={executor_id} | content={content[:50] if content else '(empty)'}...")

            logger.debug(
                f"[ContextRepository] 未压缩消息: {len(messages)} 条 | "
                f"已压缩: {compressed_count} 条 | "
                f"session_id={session_id}, executor_id={executor_id}"
            )
            return messages
