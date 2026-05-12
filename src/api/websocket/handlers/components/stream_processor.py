"""
流式处理器

负责处理 Agent 流式输出
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from src.api.websocket.unified_hub import get_unified_message_hub

logger = logging.getLogger(__name__)


@dataclass
class StreamResult:
    """流式处理结果"""

    message_id: str
    final_content: str
    thinking_content: str
    has_error: bool
    error_detail: str | None = None
    tool_calls: list | None = None
    second_ai_message_id: str | None = None  # 第二条AI消息的ID（工具调用后的回复）
    has_tool_calls: bool = False  # 是否发生了工具调用
    first_ai_message_content: str = ""  # 第一条AI消息的内容（包含tool_calls时的内容）

    def __init__(
        self,
        message_id: str,
        final_content: str,
        thinking_content: str,
        has_error: bool,
        error_detail: str | None = None,
        tool_calls: list | None = None,
        second_ai_message_id: str | None = None,
        has_tool_calls: bool = False,
        first_ai_message_content: str = "",
    ):
        self.message_id = message_id
        self.final_content = final_content
        self.thinking_content = thinking_content
        self.has_error = has_error
        self.error_detail = error_detail
        self.tool_calls = tool_calls or []  # 默认为空列表
        self.second_ai_message_id = second_ai_message_id
        self.has_tool_calls = has_tool_calls
        self.first_ai_message_content = first_ai_message_content


class StreamProcessor:
    """
    流式处理器

    负责处理 Agent 的流式输出，包括：
    - 普通文本流式输出
    - 思考模式输出
    - 错误处理
    """

    async def process(
        self,
        thread_id: str,
        agent_loop: Any,
        content: str,
        enable_thinking: bool = False,
        message_id: str | None = None,
    ) -> StreamResult:
        """
        处理 Agent 流式输出

        Args:
            thread_id: 线程 ID
            agent_loop: Agent 循环实例
            content: 用户输入内容
            enable_thinking: 是否启用思考模式
            message_id: 消息 ID（可选，自动生成）

        Returns:
            StreamResult: 流式处理结果
        """
        # 获取统一消息中心
        hub = get_unified_message_hub()

        message_id = message_id or str(uuid.uuid4())
        final_content = ""
        thinking_content = ""
        has_error = False
        error_detail = None
        thinking_start_time = None
        tool_calls = []  # 跟踪工具调用信息

        # 用于获取纯净工具结果的数据库会话
        db_session = None

        # 跟踪工具调用状态和第二条AI消息
        has_tool_calls = False  # 是否发生了工具调用
        tool_execution_completed = False  # 工具执行是否已完成
        first_ai_message_id = message_id  # 第一条AI消息的ID
        second_ai_message_id = None  # 第二条AI消息的ID（工具调用后的回复）
        first_ai_message_content = ""  # 第一条AI消息的内容（包含tool_calls）

        try:
            logger.info(
                f"[STREAM] Agent 开始执行 | thread_id={thread_id} | "
                f"enable_thinking={enable_thinking} | message_id={message_id}"
            )

            # 使用统一消息中心处理用户输入
            from src.api.websocket.unified_hub import UnifiedIncomingMessage
            # BUG-FIX-fix_20260226_user_id_null: 从 agent_loop 获取真实用户 ID
            # 问题根因: user_id 被硬编码为 "default"，导致任务创建时 user_id 为 None
            # 修复方案: 从 agent_loop.user_id 获取真实用户 ID
            user_id = getattr(agent_loop, "user_id", "anonymous") or "anonymous"
            user_message = UnifiedIncomingMessage(
                type="user_input",
                content=content,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
            )
            await hub.on_user_input(user_message)

            # 如果启用思考模式，发送思考开始消息并创建回调
            if enable_thinking:
                thinking_start_time = time.time()
                await hub.on_thinking_start(message_id)

                # 创建思考内容回调函数（用于流式发送）
                async def thinking_callback(content: str) -> None:
                    """思考内容回调函数"""
                    if content:
                        logger.debug(f"[思考回调] 收到思考内容 | len={len(content)}")
                        await hub.on_thinking_chunk(message_id, content)
            else:
                thinking_callback = None

            # 处理流式事件
            event_count = 0
            chunk_count = 0
            tool_call_sent = set()  # 跟踪已发送的工具调用开始消息，避免重复发送
            tool_call_end_sent = set()  # 跟踪已发送的工具调用结束消息，避免重复发送
            skip_content = False  # 标记当前消息的 content 是否应该跳过（工具消息）
            tool_calls = []  # 工具调用列表

            # 发送流式开始事件
            await hub.on_stream_start(message_id, thread_id)

            async for event in agent_loop.stream(
                content,
                stream_mode="messages",  # 使用 messages 模式以接收 ToolMessage
                enable_thinking=enable_thinking,
                thinking_callback=thinking_callback,
                execution_record_id=message_id,  # 传递 AI 消息 ID 作为工具调用的父记录 ID
            ):
                event_count += 1
                skip_content = False  # 重置跳过标记
                logger.debug(f"[STREAM] 收到事件 | #{event_count} | type={type(event)}")

                # 处理 stream_mode="updates" 返回的格式: {node_name: {update_dict}}
                if isinstance(event, dict):
                    # 从 updates 格式中提取消息
                    # 格式: {"call_model": {"final_output": "内容"}} 或 {"call_model": {"messages": [msg]}}
                    msg = None
                    for node_name, update in event.items():
                        if isinstance(update, dict):
                            # 检查是否有 final_output（call_model 节点返回）
                            if "final_output" in update:
                                final_output = update["final_output"]
                                if final_output and isinstance(final_output, str):
                                    logger.info(
                                        f"[STREAM] 从 {node_name} 提取到 final_output | "
                                        f"len={len(final_output)}"
                                    )
                                    # 创建模拟的 AIMessage 对象
                                    from langchain_core.messages import AIMessage
                                    msg = AIMessage(content=final_output)
                                    break
                            # 检查是否有 messages 列表
                            if "messages" in update and isinstance(update["messages"], list):
                                if update["messages"]:
                                    msg = update["messages"][-1]  # 取最后一条消息
                                    logger.debug(
                                        f"[STREAM] 从 {node_name} 提取到消息 | "
                                        f"type={type(msg).__name__}"
                                    )
                                    break
                else:
                    msg = (
                        event[0] if isinstance(event, tuple) and len(event) >= 1 else event
                    )

                # 处理工具调用检测（优先处理，确保工具调用消息及时发送）
                # 方法1: 检查 AIMessage 的 tool_calls 属性（智谱、Anthropic 等）
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    logger.info(
                        f"[STREAM] 检测到 tool_calls 属性 | count={len(msg.tool_calls)}"
                    )
                    # 标记发生了工具调用
                    has_tool_calls = True
                    for tool_call in msg.tool_calls:
                        tool_call_id = tool_call.get("id") or tool_call.get("id", "")
                        # 使用 tool_call_sent 集合防止重复处理同一个工具调用
                        if tool_call_id and tool_call_id not in tool_call_sent:
                            tool_name = tool_call.get("name") or tool_call.get(
                                "function", {}
                            ).get("name", "")
                            tool_args = tool_call.get("args") or tool_call.get(
                                "function", {}
                            ).get("arguments", {})

                            logger.info(
                                f"[STREAM] 检测到工具调用 (属性) | "
                                f"tool_name={tool_name} | "
                                f"tool_call_id={tool_call_id}"
                            )

                            # 发送工具调用开始消息
                            await hub.on_tool_start(message_id, tool_call_id, tool_name, tool_args)

                            # 记录工具调用信息（使用 OpenAI 标准格式）
                            # 检查是否已存在相同的 tool_call_id，避免重复添加
                            existing_ids = {tc.get("id") or tc.get("call_id", "") for tc in tool_calls}
                            if tool_call_id not in existing_ids:
                                tool_calls.append(
                                    {
                                        "id": tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": json.dumps(tool_args, ensure_ascii=False),
                                        },
                                        "status": "running",
                                        "started_at": time.time(),
                                    }
                                )

                            tool_call_sent.add(tool_call_id)

                            # 不再将工具调用标记嵌入消息内容
                            # 工具调用信息通过独立的 tool_calls 数组传递
                            # 前端从 message.toolCalls 获取并渲染工具卡片
                            logger.info(
                                f"[STREAM] 工具调用已记录 | tool_name={tool_name} | tool_call_id={tool_call_id}"
                            )

                # 方法2: 解析 OpenAI 格式的文本标记 (OpenAI、DeepSeek 等)
                # 格式: <<<TOOL_CALL:{json}>>>
                elif hasattr(msg, "content") and msg.content:
                    import re

                    tool_call_pattern = r"<<<TOOL_CALL:({.+?})>>>"
                    matches = re.findall(tool_call_pattern, msg.content)

                    for match_json in matches:
                        try:
                            tool_calls_data = json.loads(match_json)
                            if isinstance(tool_calls_data, list):
                                for tool_call in tool_calls_data:
                                    tool_call_id = tool_call.get("id", "")
                                    if (
                                        tool_call_id
                                        and tool_call_id not in tool_call_sent
                                    ):
                                        tool_name = tool_call.get("name", "")
                                        tool_args = tool_call.get("args", {})

                                        logger.info(
                                            f"[STREAM] 检测到工具调用 (文本标记) | "
                                            f"tool_name={tool_name} | "
                                            f"tool_call_id={tool_call_id}"
                                        )

                                        # 发送工具调用开始消息
                                        await hub.on_tool_start(message_id, tool_call_id, tool_name, tool_args)
                                        # 记录工具调用信息（使用 OpenAI 标准格式）
                                        tool_calls.append(
                                            {
                                                "id": tool_call_id,
                                                "type": "function",
                                                "function": {
                                                    "name": tool_name,
                                                    "arguments": json.dumps(tool_args, ensure_ascii=False),
                                                },
                                                "status": "running",
                                                "started_at": time.time(),
                                            }
                                        )
                                        tool_call_sent.add(tool_call_id)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"[STREAM] 解析工具调用文本标记失败 | content={msg.content[:200]}"
                            )

                # 处理工具调用结果（ToolMessage）
                # 统一工具消息检测逻辑，使用单一检测方式避免重复处理
                tool_call_id = None
                tool_name = ""
                tool_content = ""
                is_tool_message = False

                # 检查消息类型名称（最可靠的方式）
                msg_type_name = type(msg).__name__

                # 检测方式：优先使用类型名称检查，然后检查属性
                if msg_type_name == "ToolMessage":
                    # LangChain ToolMessage
                    tool_call_id = getattr(msg, "tool_call_id", "")
                    tool_name = getattr(msg, "name", "")
                    tool_content = getattr(msg, "content", "")
                    is_tool_message = True
                    logger.debug("[STREAM] 工具消息检测: type_name='ToolMessage'")
                elif hasattr(msg, "role") and msg.role == "tool":
                    # 内部 Message 对象
                    tool_call_id = getattr(msg, "tool_call_id", "")
                    tool_name = getattr(msg, "name", "")
                    tool_content = getattr(msg, "content", "")
                    is_tool_message = True
                    logger.debug("[STREAM] 工具消息检测: role='tool'")
                elif hasattr(msg, "type") and msg.type in ("tool", "ToolMessage"):
                    # 有 type 属性的消息
                    tool_call_id = getattr(msg, "tool_call_id", "")
                    tool_name = getattr(msg, "name", "")
                    tool_content = getattr(msg, "content", "")
                    is_tool_message = True
                    logger.debug(f"[STREAM] 工具消息检测: type='{msg.type}'")

                # 统一处理工具消息 - 确保每个 tool_call_id 只处理一次
                # 使用工具消息的唯一标识：优先使用 tool_call_id，如果没有则使用消息对象的 id
                if is_tool_message:
                    # 生成唯一标识：tool_call_id + tool_name + content_hash
                    content_hash = hash(tool_content) & 0xFFFFFFFF
                    unique_id = f"{tool_call_id}:{tool_name}:{content_hash}" if tool_call_id else f"{tool_name}:{content_hash}"

                    if unique_id in tool_call_end_sent:
                        # 已经处理过，只标记跳过 content
                        logger.debug(f"[STREAM] 工具消息已处理过，跳过 | tool_call_id={tool_call_id} | unique_id={unique_id}")
                        skip_content = True
                    else:
                        logger.info(
                            f"[STREAM] 检测到工具调用结果 | "
                            f"tool_name={tool_name} | "
                            f"tool_call_id={tool_call_id}"
                        )
                        # 标记工具执行已完成
                        tool_execution_completed = True

                        # 解析工具执行结果
                        status = "completed"
                        error = None
                        result = None

                        if "执行失败" in tool_content or "执行异常" in tool_content:
                            status = "failed"
                            error = tool_content
                        else:
                            # 尝试从数据库获取纯净的工具结果
                            result = await self._get_pure_tool_result(
                                thread_id, tool_call_id, tool_content
                            )

                        # 发送工具调用结束消息
                        await hub.on_tool_end(
                            message_id,
                            tool_call_id,
                            status,
                            result,
                            error,
                        )

                        # 更新工具调用信息
                        for tc in tool_calls:
                            tc_id = tc.get("id") or tc.get("call_id", "")
                            if tc_id == tool_call_id:
                                tc["status"] = status
                                tc["result"] = result
                                tc["error"] = error
                                tc["completed_at"] = time.time()
                                if "started_at" in tc:
                                    tc["duration_ms"] = int(
                                        (tc["completed_at"] - tc["started_at"]) * 1000
                                    )
                                break

                        # 记录已处理的工具消息唯一标识
                        tool_call_end_sent.add(unique_id)

                        # 标记工具消息的 content 不应该添加到 final_content
                        skip_content = True

                # 处理思考内容
                reasoning_text = None
                if enable_thinking:
                    reasoning_text = self._extract_reasoning_content(msg)
                    logger.debug(
                        f"[STREAM] 提取思考内容 | "
                        f"reasoning_text={reasoning_text[:50] if reasoning_text else None}... | "
                        f"reasoning_text_len={len(reasoning_text) if reasoning_text else 0}"
                    )
                    if reasoning_text and reasoning_text.strip():
                        logger.info(
                            f"[STREAM] 收到思考内容 | "
                            f"len={len(reasoning_text)} | "
                            f"preview={reasoning_text[:50]}... | "
                            f"current_thinking_len={len(thinking_content)}"
                        )
                        # 只发送新增的思考内容（增量发送）
                        if reasoning_text not in thinking_content:
                            # 新内容，追加并发送
                            thinking_content += reasoning_text
                            logger.info(
                                f"[STREAM] 发送思考内容到前端 | total_len={len(thinking_content)}"
                            )
                            await hub.on_thinking_chunk(message_id, reasoning_text)
                        else:
                            # 内容已存在，可能是累积返回，跳过
                            logger.debug(
                                f"[STREAM] 思考内容已存在，跳过 | len={len(reasoning_text)}"
                            )

                # 处理普通内容（跳过工具消息和系统消息的 content）
                # 注意：工具调用后的 LLM 回复（AIMessage）会在这里处理
                # 注意：正文处理与思考内容处理独立，确保正文总是被处理
                if hasattr(msg, "content") and not skip_content:
                    # 检查消息类型，只处理 AI 消息的内容
                    msg_type = type(msg).__name__
                    if msg_type not in ["AIMessage", "AIMessageChunk"]:
                        logger.debug(
                            f"[STREAM] 跳过非 AI 消息 | type={msg_type} | "
                            f"content_preview={str(msg.content)[:50]}..."
                        )
                        continue

                    text = msg.content
                    if isinstance(text, str) and text:
                        # 如果启用了思考模式，需要从内容中移除思考部分
                        if enable_thinking and reasoning_text:
                            if reasoning_text in text:
                                text = text.replace(reasoning_text, "").strip()
                                logger.debug(
                                    f"[STREAM] 从正文中移除思考内容 | "
                                    f"original_len={len(msg.content)} | "
                                    f"cleaned_len={len(text)}"
                                )
                            # 注意：如果 reasoning_text 不在 text 中（如智谱API分开返回），
                            # 则保留原始 text 内容，不做修改

                        # 检测是否是工具调用后的第二条AI消息
                        # 条件：1. 发生过工具调用 2. 工具执行已完成 3. 当前消息没有tool_calls
                        msg_has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls
                        is_second_ai_message = (
                            has_tool_calls and
                            tool_execution_completed and
                            not msg_has_tool_calls and
                            second_ai_message_id is None
                        )

                        if is_second_ai_message:
                            # 这是工具调用后的第二条AI消息，需要生成新ID
                            # 先生成新ID，然后更新流式状态
                            from src.db.connection import get_async_session
                            from src.utils.message_id_helper import generate_execution_record_id

                            async for db_session in get_async_session():
                                second_ai_message_id = await generate_execution_record_id(
                                    db_session, thread_id
                                )
                                break

                            logger.info(
                                f"[STREAM] 检测到第二条AI消息（工具调用后回复），生成新ID | "
                                f"first_id={first_ai_message_id} | "
                                f"second_id={second_ai_message_id} | "
                                f"content_preview={text[:50]}..."
                            )

                            # 将第二条AI消息ID传递给Agent循环
                            # 这样 call_model_node 在创建执行记录时会使用这个ID
                            if agent_loop and hasattr(agent_loop, "second_ai_message_id"):
                                agent_loop.second_ai_message_id = second_ai_message_id
                                logger.info(
                                    f"[STREAM] 已将 second_ai_message_id 设置到 agent_loop | "
                                    f"second_id={second_ai_message_id}"
                                )

                            # 发送流式结束事件给第一条消息
                            await hub.on_stream_end(first_ai_message_id)

                            # 保存第一条AI消息的内容
                            first_ai_message_content = final_content

                            # 重置流式状态，使用新ID开始新的流式输出
                            await hub.on_stream_start(second_ai_message_id, thread_id)

                            # 更新message_id为第二条消息的ID，后续内容都发送给第二条消息
                            message_id = second_ai_message_id
                            final_content = ""  # 重置内容，开始累积第二条消息的内容

                        # 检测重复：LangGraph会在流式输出结束后发送完整的响应
                        # 如果text已经完全包含在final_content中，跳过这次发送
                        if text and text in final_content:
                            logger.debug(
                                f"[STREAM] 检测到重复内容，跳过发送 | "
                                f"content_len={len(text)} | event_count={event_count}"
                            )
                        else:
                            # 记录消息类型信息，帮助调试工具调用后的 LLM 回复
                            msg_type = type(msg).__name__
                            msg_has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls
                            logger.info(
                                f"[STREAM] 发送内容 chunk | chunk_count={chunk_count + 1} | "
                                f"content_len={len(text)} | msg_type={msg_type} | "
                                f"has_tool_calls={msg_has_tool_calls} | "
                                f"message_id={message_id} | "
                                f"content_preview={text[:50]}..."
                            )
                            final_content += text
                            chunk_count += 1
                            await hub.on_llm_chunk(message_id, text)
                    else:
                        # content 为空字符串，记录日志帮助调试
                        msg_type = type(msg).__name__
                        msg_has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls
                        if msg_has_tool_calls:
                            logger.debug(
                                f"[STREAM] AIMessage content 为空但有 tool_calls | "
                                f"msg_type={msg_type} | tool_calls_count={len(msg.tool_calls)}"
                            )
                        elif not skip_content:
                            logger.debug(
                                f"[STREAM] 消息 content 为空 | msg_type={msg_type} | "
                                f"skip_content={skip_content}"
                            )

            # 发送思考结束消息
            if enable_thinking and thinking_start_time:
                await hub.on_thinking_end(message_id)
                logger.info(
                    f"思考模式完成 | thread_id={thread_id} | "
                    f"thinking_len={len(thinking_content)}"
                )

        except TimeoutError as e:
            has_error = True
            error_detail = "Agent 执行超时"
            logger.error(
                f"[STREAM] 执行超时 | thread_id={thread_id} | message_id={message_id}"
            )
            await hub.on_stream_error(message_id, e)
            final_content = f"[错误] {error_detail}"

        except Exception as e:
            has_error = True
            error_detail = str(e)
            logger.error(
                f"[STREAM] 执行异常 | thread_id={thread_id} | message_id={message_id} | error={e}",
                exc_info=True,
            )
            await hub.on_stream_error(message_id, e)

        # 发送流式结束事件
        if not has_error:
            await hub.on_stream_end(message_id)

        logger.info(
            f"[STREAM] Agent 执行完成 | thread_id={thread_id} | message_id={message_id} | "
            f"total_chunks={chunk_count} | total_chars={len(final_content)} | has_error={has_error} | "
            f"has_tool_calls={has_tool_calls} | second_ai_message_id={second_ai_message_id}"
        )

        return StreamResult(
            message_id=first_ai_message_id,  # 第一条AI消息的ID（包含tool_calls的）
            final_content=final_content,
            thinking_content=thinking_content,
            has_error=has_error,
            error_detail=error_detail,
            tool_calls=tool_calls,
            second_ai_message_id=second_ai_message_id,  # 第二条AI消息的ID（工具调用后的回复）
            has_tool_calls=has_tool_calls,
            first_ai_message_content=first_ai_message_content,  # 第一条AI消息的内容
        )

    def __init__(self):
        """初始化流式处理器"""
        # 用于跟踪是否找到过思考内容，避免重复警告
        self._has_found_thinking = False

    def _extract_reasoning_content(self, msg: Any) -> str | None:
        """
        从消息中提取思考内容

        Args:
            msg: LangChain 消息对象

        Returns:
            Optional[str]: 思考内容
        """
        reasoning_text = None

        # 调试日志：打印消息类型和属性
        logger.debug(
            f"[_extract_reasoning_content] 消息类型={type(msg).__name__} | "
            f"has_additional_kwargs={hasattr(msg, 'additional_kwargs')} | "
            f"has_reasoning_content={hasattr(msg, 'reasoning_content')}"
        )

        # 详细调试：打印消息的所有属性
        if hasattr(msg, "__dict__"):
            msg_attrs = [k for k in msg.__dict__.keys() if not k.startswith("_")]
            logger.debug(f"[_extract_reasoning_content] 消息属性: {msg_attrs}")

        # 方式1: 检查 additional_kwargs 中的 thinking 或 reasoning_content
        if hasattr(msg, "additional_kwargs"):
            additional_kwargs = msg.additional_kwargs
            logger.debug(
                f"[_extract_reasoning_content] additional_kwargs keys={list(additional_kwargs.keys())}"
            )

            thinking = additional_kwargs.get("thinking")
            if thinking:
                reasoning_text = (
                    thinking.get("content", "")
                    if isinstance(thinking, dict)
                    else str(thinking)
                )
                logger.debug(
                    f"[_extract_reasoning_content] 从 thinking 提取 | len={len(reasoning_text) if reasoning_text else 0}"
                )

            # 智谱 API 可能返回 reasoning_content
            if not reasoning_text:
                reasoning_text = additional_kwargs.get("reasoning_content", "")
                if reasoning_text:
                    logger.debug(
                        f"[_extract_reasoning_content] 从 reasoning_content 提取 | len={len(reasoning_text)}"
                    )

        # 方式2: 直接检查 reasoning_content 属性（智谱原生格式）
        if not reasoning_text and hasattr(msg, "reasoning_content"):
            reasoning_text = msg.reasoning_content
            if reasoning_text:
                logger.debug(
                    f"[_extract_reasoning_content] 从属性 reasoning_content 提取 | len={len(reasoning_text)}"
                )

        # 方式3: 检查 response_metadata
        if not reasoning_text and hasattr(msg, "response_metadata"):
            reasoning_text = msg.response_metadata.get("reasoning_content", "")
            if reasoning_text:
                logger.debug(
                    f"[_extract_reasoning_content] 从 response_metadata 提取 | len={len(reasoning_text)}"
                )

        # 更新思考内容找到状态
        if reasoning_text:
            self._has_found_thinking = True
            logger.info(
                f"[_extract_reasoning_content] 成功提取思考内容 | len={len(reasoning_text)}"
            )
        else:
            # 只在调试模式下输出详细日志，不再使用 WARNING
            logger.debug(
                "[_extract_reasoning_content] 此消息块无思考内容（这是正常现象，"
                "思考内容通常只在特定消息块中出现）"
            )

        return reasoning_text if reasoning_text else None

    async def _get_pure_tool_result(
        self, thread_id: str, tool_call_id: str, fallback_content: str
    ) -> str:
        """
        从数据库获取纯净的工具执行结果

        工具消息在传给 LLM 时会被包装（添加提示词），但前端需要显示纯净的结果。
        这个方法从数据库查询 ExecutionRecord 获取原始的 output.result。

        Args:
            thread_id: 线程ID
            tool_call_id: 工具调用ID
            fallback_content: 如果数据库查询失败，返回的备用内容

        Returns:
            str: 纯净的工具结果（JSON字符串格式）
        """
        logger.info(
            f"[_get_pure_tool_result] 开始获取纯净结果 | "
            f"thread_id={thread_id} | "
            f"tool_call_id={tool_call_id} | "
            f"fallback_content_preview={fallback_content[:200] if fallback_content else 'None'}..."
        )
        try:
            from src.db.repositories.execution_record_repo import (
                ExecutionRecordRepository,
            )
            from src.db.session import get_async_session

            async for db in get_async_session():
                try:
                    repo = ExecutionRecordRepository(db)
                    # 通过 tool_call_id 查找对应的执行记录
                    # 注意：tool_call_id 可能对应多个记录（如果有重试），取最新的
                    records = await repo.get_execution_records(
                        session_id=thread_id,
                        limit=50,  # 获取足够多的记录
                    )

                    # 查找匹配 tool_call_id 的记录
                    for record in records:
                        msg_data = record.message_data or {}
                        record_tool_call_id = msg_data.get("tool_call_id", "")
                        if record_tool_call_id == tool_call_id:
                            # 找到了匹配的记录，获取纯净的 output.result
                            output = msg_data.get("output", {})
                            if isinstance(output, dict):
                                result = output.get("result")
                                if result is not None:
                                    logger.info(
                                        f"[_get_pure_tool_result] 从数据库获取纯净结果 | "
                                        f"tool_call_id={tool_call_id} | "
                                        f"result_type={type(result).__name__}"
                                    )
                                    # 将结果序列化为 JSON 字符串，保持数据结构
                                    import json
                                    if isinstance(result, (dict, list)):
                                        return json.dumps(result, ensure_ascii=False)
                                    else:
                                        return str(result)
                            # 如果没有 result，尝试返回整个 output
                            if output:
                                import json
                                return json.dumps(output, ensure_ascii=False)

                    # 没有找到匹配的记录，尝试从 fallback_content 解析纯净结果
                    logger.debug(
                        f"[_get_pure_tool_result] 未找到匹配记录，尝试解析 fallback_content | "
                        f"tool_call_id={tool_call_id}"
                    )
                    # fallback_content 可能是包装后的提示词，尝试提取其中的 JSON 结果
                    return self._extract_result_from_fallback(fallback_content)

                except Exception as e:
                    logger.warning(
                        f"[_get_pure_tool_result] 查询数据库失败: {e} | "
                        f"tool_call_id={tool_call_id}"
                    )
                    # 尝试从 fallback_content 解析纯净结果
                    return self._extract_result_from_fallback(fallback_content)

        except Exception as e:
            logger.warning(
                f"[_get_pure_tool_result] 获取纯净结果失败: {e} | "
                f"tool_call_id={tool_call_id}"
            )
            # 尝试从 fallback_content 解析纯净结果
            return self._extract_result_from_fallback(fallback_content)

    def _extract_result_from_fallback(self, fallback_content: str) -> str:
        """
        从 fallback_content（可能是包装后的提示词）中提取纯净的结果

        fallback_content 格式示例：
        "工具 resource_search 执行成功\n返回结果:\n{...}\n\n✅ 操作已完成，无需再次调用。"

        Args:
            fallback_content: 包装后的提示词内容

        Returns:
            str: 提取的纯净结果
        """
        if not fallback_content:
            return ""

        try:
            # 尝试找到 JSON 部分
            # 格式通常是："工具 xxx 执行成功\n返回结果:\n{JSON}\n\n✅ 操作已完成..."
            import json
            import re

            # 尝试匹配 "返回结果:" 后面的 JSON
            match = re.search(r'返回结果:\s*(\{.*?\}|\[.*?\])', fallback_content, re.DOTALL)
            if match:
                json_str = match.group(1)
                # 验证是否是有效的 JSON
                parsed = json.loads(json_str)
                return json.dumps(parsed, ensure_ascii=False)

            # 如果没有匹配到，尝试直接找 JSON 对象或数组
            match = re.search(r'(\{.*?\}|\[.*?\])', fallback_content, re.DOTALL)
            if match:
                json_str = match.group(1)
                try:
                    parsed = json.loads(json_str)
                    return json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass

            # 如果都无法解析，返回原始内容（但去除明显的提示词）
            # 去除 "工具 xxx 执行成功"、"返回结果:"、"✅ 操作已完成" 等提示词
            cleaned = fallback_content
            cleaned = re.sub(r'工具 \w+ 执行成功\s*', '', cleaned)
            cleaned = re.sub(r'返回结果:\s*', '', cleaned)
            cleaned = re.sub(r'✅\s*操作已完成.*', '', cleaned)
            cleaned = cleaned.strip()

            return cleaned if cleaned else fallback_content

        except Exception as e:
            logger.warning(f"[_extract_result_from_fallback] 解析失败: {e}")
            return fallback_content
