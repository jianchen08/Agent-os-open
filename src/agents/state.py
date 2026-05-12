"""
Agent 状态定义

基于 LangGraph 的 Agent 状态管理
"""

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.messages import (
    BaseMessageChunk,
    RemoveMessage,
    convert_to_messages,
    message_chunk_to_message,
)
from typing_extensions import TypedDict

from src.core.states import ExecutionStatus

# 日志
logger = logging.getLogger(__name__)


def safe_add_messages(
    left: list[Any],
    right: list[Any],
) -> list[Any]:
    """
    安全的消息合并函数

    处理消息 ID 为字典类型的情况，避免 unhashable type: 'dict' 错误

    Args:
        left: 基础消息列表
        right: 要合并的消息列表

    Returns:
        合并后的消息列表
    """
    # 转换为列表
    if not isinstance(left, list):
        left = [left] if left else []
    if not isinstance(right, list):
        right = [right] if right else []

    # 如果都为空，直接返回
    if not left and not right:
        return []

    # 转换消息格式
    try:
        left = [
            message_chunk_to_message(m) if isinstance(m, BaseMessageChunk) else m
            for m in convert_to_messages(left)
            if left
        ]
    except Exception:
        # 消息格式转换失败时保持原样
        pass

    try:
        right = [
            message_chunk_to_message(m) if isinstance(m, BaseMessageChunk) else m
            for m in convert_to_messages(right)
            if right
        ]
    except Exception:
        # 消息格式转换失败时保持原样
        pass

    def get_safe_id(msg: Any) -> str:
        """获取安全的消息 ID（字符串格式）"""
        msg_id = getattr(msg, "id", None)
        if msg_id is None:
            return str(uuid.uuid4())
        if isinstance(msg_id, dict):
            # 将字典转为 JSON 字符串作为 ID
            return json.dumps(msg_id, sort_keys=True)
        return str(msg_id)

    def set_safe_id(msg: Any) -> None:
        """确保消息有字符串类型的 ID"""
        if hasattr(msg, "id"):
            if msg.id is None:
                msg.id = str(uuid.uuid4())
            elif isinstance(msg.id, dict):
                # 将字典 ID 转为字符串
                msg.id = json.dumps(msg.id, sort_keys=True)

    # 为所有消息设置安全 ID
    for m in left:
        set_safe_id(m)
    for m in right:
        set_safe_id(m)

    # 合并消息
    merged = list(left)
    merged_by_id: dict[str, int] = {get_safe_id(m): i for i, m in enumerate(merged)}
    ids_to_remove: set = set()

    for m in right:
        msg_id = get_safe_id(m)
        existing_idx = merged_by_id.get(msg_id)

        if existing_idx is not None:
            if isinstance(m, RemoveMessage):
                ids_to_remove.add(msg_id)
            else:
                ids_to_remove.discard(msg_id)
                merged[existing_idx] = m
        else:
            if isinstance(m, RemoveMessage):
                # 尝试删除不存在的消息，跳过
                continue
            merged_by_id[msg_id] = len(merged)
            merged.append(m)

    # 移除标记删除的消息
    merged = [m for m in merged if get_safe_id(m) not in ids_to_remove]

    return merged


class AgentState(TypedDict):
    """
    Agent 状态

    注意: llm_client, tool_executor, agent_config, layered_context_store,
    thinking_callback 这些字段不参与 LangGraph 的 checkpoint 序列化,
    它们在运行时动态注入。
    """

    # 执行状态（使用统一 ExecutionStatus）
    status: ExecutionStatus

    # 当前迭代次数
    iteration: int

    # 消息列表（包含用户输入和对话历史）
    messages: list[Any]

    # 待执行的工具调用
    pending_tool_calls: list[dict[str, Any]]

    # 已执行的工具调用记录
    tool_calls: list[dict[str, Any]]

    # LLM 客户端（运行时注入，不序列化）
    # 注意: 此字段在 checkpoint 时会被跳过
    llm_client: Any | None

    # 可用工具列表（运行时注入，不序列化）
    tools: list[Any]

    # 工具执行器（运行时注入，不序列化）
    tool_executor: Any | None

    # 是否需要人工审批
    requires_approval: bool

    # 执行上下文
    context: dict[str, Any]

    # 最终输出
    final_output: str | None

    # 错误信息
    error: str | None

    # 是否停止
    should_stop: bool

    # 是否启用思考模式
    enable_thinking: bool

    # 输出 Schema（用于结构化输出）
    output_schema: dict[str, Any] | None

    # Agent 配置（运行时注入，不序列化）
    agent_config: Any | None

    # 分层上下文存储（运行时注入，不序列化）
    layered_context_store: Any | None

    # 思考内容回调函数（运行时注入，不序列化）
    thinking_callback: Callable | None

    # 评估提醒次数（防止无限循环）
    evaluate_reminder_count: int

    # 状态版本控制
    state_version: int

    # 状态变更时间戳
    last_updated: str

    # 状态变更来源
    last_updated_by: str | None

    # 状态变更原因
    last_updated_reason: str | None

    # 状态一致性标记
    consistency_hash: str | None


def create_initial_state(
    user_input: str,
    system_prompt: str,
    llm_client: Any | None = None,
    tools: list[Any] | None = None,
    tool_executor: Any | None = None,
    context: dict[str, Any] | None = None,
    enable_thinking: bool | None = None,  # 改为 Optional[bool]
    agent_config: Any | None = None,  # Agent 配置参数
    layered_context_store: Any | None = None,  # 分层上下文存储
    thinking_callback: Callable | None = None,  # 思考内容回调函数
) -> AgentState:
    """
    创建初始 Agent 状态

    Args:
        user_input: 用户输入
        system_prompt: 系统提示词
        llm_client: LLM 客户端
        tools: 可用工具列表
        tool_executor: 工具执行器
        context: 执行上下文
        enable_thinking: 是否启用思考模式（None=使用Agent配置，True/False=用户明确指定）
        agent_config: Agent 配置对象
        layered_context_store: 分层上下文存储（可选，用于增强的上下文管理）

    Returns:
        初始化的 AgentState
    """
    import hashlib
    import json
    from datetime import datetime

    # 解析思考模式设置（优先级：用户明确指定 > Agent 配置）
    final_enable_thinking = False

    # 检查 Agent 配置是否支持思考模式
    has_thinking_config = (
        agent_config
        and hasattr(agent_config, "can_use_thinking_mode")
        and agent_config.can_use_thinking_mode()
    )

    if has_thinking_config:
        # Agent 配置支持思考模式
        if enable_thinking is not None:
            # 用户明确指定，优先级最高
            final_enable_thinking = enable_thinking
            logger.debug(
                f"[Agent State] 用户明确指定思考模式: {enable_thinking} (Agent支持思考模式)"
            )
        else:
            # 用户未指定，检查 Agent 配置中的默认设置
            agent_model_params = getattr(agent_config, "model_params", {}) or {}
            agent_thinking_mode = agent_model_params.get("thinking_mode", False)
            final_enable_thinking = agent_thinking_mode
            if agent_thinking_mode:
                logger.debug(
                    f"[Agent State] 使用 Agent 配置的思考模式: {agent_thinking_mode}"
                )
    else:
        # Agent 配置不支持思考模式
        final_enable_thinking = (
            enable_thinking if enable_thinking is not None else False
        )
        if enable_thinking is not None:
            logger.debug(
                f"[Agent State] 用户指定思考模式: {enable_thinking} (Agent不支持思考模式)"
            )

    # 提取 output_schema（从 agent_config 中）
    output_schema = None
    if agent_config and hasattr(agent_config, "output_schema"):
        schema = agent_config.output_schema
        # 检查 schema 是否有效（非空且不是空对象）
        if schema and isinstance(schema, dict):
            props = schema.get("properties", {})
            if props:  # 只有有属性时才启用结构化输出
                output_schema = schema
                logger.debug(
                    f"[Agent State] 启用结构化输出 | schema={list(props.keys())}"
                )

    # 创建消息列表，包含系统提示和用户输入
    from langchain_core.messages import SystemMessage, HumanMessage
    
    messages: list[Any] = []
    
    # 添加系统提示
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    
    # 添加用户输入（任务目标）- 这是关键！
    if user_input:
        messages.append(HumanMessage(content=user_input))
        logger.debug(f"[Agent State] 添加用户输入到消息列表 | length={len(user_input)}")
    
    # 计算初始状态的一致性哈希
    initial_state_data = {
        "status": ExecutionStatus.PENDING.value,
        "iteration": 0,
        "messages": [str(m) for m in messages],  # 简化消息用于哈希计算
        "pending_tool_calls": [],
        "tool_calls": [],
        "requires_approval": False,
        "context": context or {},
        "final_output": None,
        "error": None,
        "should_stop": False,
        "enable_thinking": final_enable_thinking,
        "output_schema": output_schema,
        "evaluate_reminder_count": 0,
    }

    # 生成一致性哈希
    consistency_hash = hashlib.sha256(
        json.dumps(initial_state_data, sort_keys=True, default=str).encode()
    ).hexdigest()

    return AgentState(
        status=ExecutionStatus.PENDING,
        iteration=0,
        messages=messages,  # 保存消息列表
        pending_tool_calls=[],
        tool_calls=[],
        llm_client=llm_client,
        tools=tools or [],
        tool_executor=tool_executor,
        requires_approval=False,
        context=context or {},
        final_output=None,
        error=None,
        should_stop=False,
        enable_thinking=final_enable_thinking,
        output_schema=output_schema,
        agent_config=agent_config,
        layered_context_store=layered_context_store,
        thinking_callback=thinking_callback,
        evaluate_reminder_count=0,
        state_version=1,
        last_updated=datetime.now().isoformat(),
        last_updated_by="system",
        last_updated_reason="initialization",
        consistency_hash=consistency_hash,
    )


def state_to_dict(state: AgentState) -> dict[str, Any]:
    """
    将状态转换为可序列化的字典

    Args:
        state: Agent 状态

    Returns:
        可序列化的字典
    """
    status = state.get("status")
    result = {
        "status": status.value if status else None,
        "iteration": state.get("iteration", 0),
        "tool_calls": state.get("tool_calls", []),
        "final_output": state.get("final_output"),
        "error": state.get("error"),
        "state_version": state.get("state_version", 1),
        "last_updated": state.get("last_updated"),
        "consistency_hash": state.get("consistency_hash"),
    }

    # 添加分层上下文存储信息（如果有）
    layered_context_store = state.get("layered_context_store")
    if layered_context_store:
        result["layered_context_store"] = {
            "type": type(layered_context_store).__name__,
            "available": True,
        }

    return result


def update_state(
    state: AgentState,
    updates: dict[str, Any],
    updated_by: str = "system",
    reason: str = "state_update",
) -> AgentState:
    """
    更新 Agent 状态，自动处理版本控制和一致性哈希

    Args:
        state: 当前 Agent 状态
        updates: 要更新的字段和值
        updated_by: 更新来源
        reason: 更新原因

    Returns:
        更新后的 Agent 状态
    """
    import hashlib
    import json
    from datetime import datetime

    # 创建状态副本
    updated_state = state.copy()

    # 应用更新
    for key, value in updates.items():
        if key in updated_state:
            updated_state[key] = value

    # 增加版本号
    updated_state["state_version"] = updated_state.get("state_version", 0) + 1

    # 更新时间戳和来源
    updated_state["last_updated"] = datetime.now().isoformat()
    updated_state["last_updated_by"] = updated_by
    updated_state["last_updated_reason"] = reason

    # 计算新的一致性哈希
    state_data = {
        "status": updated_state.get("status", ExecutionStatus.PENDING).value
        if updated_state.get("status")
        else None,
        "iteration": updated_state.get("iteration", 0),
        "pending_tool_calls": updated_state.get("pending_tool_calls", []),
        "tool_calls": updated_state.get("tool_calls", []),
        "requires_approval": updated_state.get("requires_approval", False),
        "context": updated_state.get("context", {}),
        "final_output": updated_state.get("final_output"),
        "error": updated_state.get("error"),
        "should_stop": updated_state.get("should_stop", False),
        "enable_thinking": updated_state.get("enable_thinking", False),
        "output_schema": updated_state.get("output_schema"),
        "evaluate_reminder_count": updated_state.get("evaluate_reminder_count", 0),
    }

    consistency_hash = hashlib.sha256(
        json.dumps(state_data, sort_keys=True, default=str).encode()
    ).hexdigest()

    updated_state["consistency_hash"] = consistency_hash

    logger.debug(
        f"状态已更新 | 版本: {updated_state['state_version']} "
        f"| 来源: {updated_by} | 原因: {reason} "
        f"| 一致性哈希: {consistency_hash[:8]}..."
    )

    return updated_state


def validate_state_consistency(state: AgentState) -> bool:
    """
    验证状态一致性

    Args:
        state: 要验证的 Agent 状态

    Returns:
        状态是否一致
    """
    import hashlib
    import json

    try:
        # 计算当前状态的一致性哈希
        state_data = {
            "status": state.get("status", ExecutionStatus.PENDING).value
            if state.get("status")
            else None,
            "iteration": state.get("iteration", 0),
            "pending_tool_calls": state.get("pending_tool_calls", []),
            "tool_calls": state.get("tool_calls", []),
            "requires_approval": state.get("requires_approval", False),
            "context": state.get("context", {}),
            "final_output": state.get("final_output"),
            "error": state.get("error"),
            "should_stop": state.get("should_stop", False),
            "enable_thinking": state.get("enable_thinking", False),
            "output_schema": state.get("output_schema"),
            "evaluate_reminder_count": state.get("evaluate_reminder_count", 0),
        }

        calculated_hash = hashlib.sha256(
            json.dumps(state_data, sort_keys=True, default=str).encode()
        ).hexdigest()

        # 比较计算的哈希和存储的哈希
        stored_hash = state.get("consistency_hash")
        if not stored_hash:
            logger.warning("状态缺少一致性哈希")
            return False

        if calculated_hash != stored_hash:
            logger.warning(
                f"状态一致性检查失败 | 计算的哈希: {calculated_hash[:8]}... "
                f"| 存储的哈希: {stored_hash[:8]}..."
            )
            return False

        return True
    except Exception as e:
        logger.error(f"验证状态一致性时出错: {e}")
        return False
