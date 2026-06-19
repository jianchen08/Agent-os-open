"""管道状态构建器。

从 PipelineEngine 中迁出的状态构建相关逻辑，负责：
1. 构建管道初始 state 字典
2. 解析/恢复对话历史
3. 加载系统默认 Agent 配置

所有函数均为模块级公开函数，不依赖 PipelineEngine 实例，
通过参数显式传入所需依赖（services、pipeline_id、agent_registry 等）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.types import StateKeys, TargetType

logger = logging.getLogger(__name__)


def build_initial_state(
    user_input: str,
    agent_config: Any | None,
    conversation_history: list[dict[str, Any]] | None,
    pipeline_id: str,
    services: dict[str, Any],
    extra_state: dict[str, Any],
) -> dict[str, Any]:
    """构建管道初始状态字典。

    将用户输入、Agent 配置、对话历史和额外状态合并为管道 state。

    Args:
        user_input: 用户输入文本
        agent_config: Agent 配置实例
        conversation_history: 对话历史
        pipeline_id: 管道唯一标识，用于恢复历史记录
        services: 服务字典（包含 execution_record_storage 等）
        extra_state: 额外状态键值对

    Returns:
        管道初始状态字典
    """
    resolved_history = resolve_conversation_history(
        conversation_history, pipeline_id, services,
    )

    state: dict[str, Any] = {
        StateKeys.ITERATION: 0,
        StateKeys.CORE_TYPE: TargetType.LLM_CALL.value,
        StateKeys.ENDED: False,
        "user_input": user_input,
        "messages": resolved_history,
    }

    # 标记本次管道是否携带继承来的历史对话（pipe 继承）。
    # 判据：调用方显式传入了非空 conversation_history。
    # workspace 继承不触碰消息，不会置真；两者组合继承时只要走了 pipe 就为真。
    # engine._persist_inherited_history 据此把装载的历史落盘到新 pipeline 文件，
    # 避免继承历史只活在内存里、管道结束后丢失（且使新管道可二次重试）。
    state["_inherited_history"] = bool(conversation_history)

    if user_input:
        _last = resolved_history[-1] if resolved_history else {}
        if not (_last.get("role") == "user" and _last.get("content") == user_input):
            state["messages"].append({"role": "user", "content": user_input})
            logger.info("[StateBuilder] appended user_input to messages (dedup skipped)")
        else:
            logger.info("[StateBuilder] dedup: skipped appending user_input (last msg is same)")

    if agent_config and hasattr(agent_config, "to_state"):
        agent_state = agent_config.to_state()
        state.update(agent_state)

    state.update(extra_state)

    return state


def resolve_conversation_history(
    conversation_history: list[dict[str, Any]] | None,
    pipeline_id: str,
    services: dict[str, Any],
) -> list[dict[str, Any]]:
    """解析对话历史，当调用方未传入历史时自动从 ExecutionRecordStorage 恢复。

    当 conversation_history 非空时直接使用调用方传入的历史；
    当为空但当前 pipeline_id 已有执行记录时，从持久化存储恢复完整历史。

    Args:
        conversation_history: 调用方传入的对话历史（可能为 None 或空列表）
        pipeline_id: 管道唯一标识，用于查询执行记录
        services: 服务字典（包含 execution_record_storage 等）

    Returns:
        解析后的对话历史列表
    """
    if conversation_history:
        return list(conversation_history)

    exec_storage = services.get("execution_record_storage")
    if not exec_storage:
        return []

    try:
        records = exec_storage.list_by_pipeline(pipeline_id)[0]
    except Exception:
        return []

    if not records:
        return []

    history: list[dict[str, Any]] = []
    _type_to_role = {"user": "user", "ai": "assistant", "tool": "tool", "system": "system"}
    for r in records:
        role = r.role or _type_to_role.get(r.type, "user")
        msg: dict[str, Any] = {"role": role, "content": r.content}
        # 保留执行记录的 sequence，用于压缩块记录实际消息范围
        if getattr(r, "sequence", 0) > 0:
            msg["_record_sequence"] = r.sequence
        if getattr(r, "name", None):
            msg["name"] = r.name
        if getattr(r, "tool_call_id", None):
            msg["tool_call_id"] = r.tool_call_id
        if getattr(r, "tool_input", None):
            msg["tool_input"] = r.tool_input
        if getattr(r, "tool_calls_json", None):
            try:
                import json as _json

                msg["tool_calls"] = _json.loads(r.tool_calls_json)
            except (ValueError, TypeError):
                pass
        history.append(msg)

    from infrastructure.task_worker import _reconstruct_tool_calls

    _reconstruct_tool_calls(history)

    logger.info(
        "[StateBuilder] 从持久化存储恢复 %d 条历史记录 (pipeline=%s)",
        len(history),
        pipeline_id,
    )
    return history
