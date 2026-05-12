"""
Agent Graph 定义

基于 LangGraph StateGraph 的 Agent 图构建
"""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.agents.langgraph_checkpoint import SerializableCheckpointer
from src.agents.nodes import (
    call_model_node,
    evaluate_reminder_node,
    execute_tools_node,
    human_approval_node,
    should_continue,
)
from src.agents.state import AgentState


class AgentGraphBuilder:
    """
    Agent 图构建器

    提供灵活的图构建接口
    """

    def __init__(self):
        """初始化构建器"""
        self._graph_builder = StateGraph(AgentState)
        self._checkpointer = None
        self._enable_approval = False

    def with_checkpointer(self, checkpointer: Any | None = None) -> "AgentGraphBuilder":
        """
        配置检查点

        Args:
            checkpointer: 检查点保存器，None 则使用内存保存器

        Returns:
            self
        """
        base_checkpointer = checkpointer or MemorySaver()
        # 使用包装器过滤不可序列化的字段
        self._checkpointer = SerializableCheckpointer(base_checkpointer)
        return self

    def with_human_approval(self, enable: bool = True) -> "AgentGraphBuilder":
        """
        启用人工审批

        Args:
            enable: 是否启用

        Returns:
            self
        """
        self._enable_approval = enable
        return self

    def build(self) -> Any:
        """
        构建并编译图

        Returns:
            编译后的图
        """
        # 添加节点
        self._graph_builder.add_node("call_model", call_model_node)
        self._graph_builder.add_node("execute_tools", execute_tools_node)
        self._graph_builder.add_node("evaluate_reminder", evaluate_reminder_node)

        if self._enable_approval:
            self._graph_builder.add_node("human_approval", human_approval_node)

        # 添加边
        self._graph_builder.add_edge(START, "call_model")

        # 条件边：根据 should_continue 决定下一步
        if self._enable_approval:
            # 启用审批时的路由
            self._graph_builder.add_conditional_edges(
                "call_model",
                should_continue,
                {
                    "tools": "human_approval",
                    "evaluate_reminder": "evaluate_reminder",
                    "end": END,
                },
            )
            self._graph_builder.add_edge("human_approval", "execute_tools")
        else:
            # 不启用审批时的路由
            self._graph_builder.add_conditional_edges(
                "call_model",
                should_continue,
                {
                    "tools": "execute_tools",
                    "evaluate_reminder": "evaluate_reminder",
                    "end": END,
                },
            )

        # 工具执行后返回模型
        self._graph_builder.add_edge("execute_tools", "call_model")

        # 评估提醒后返回模型
        self._graph_builder.add_edge("evaluate_reminder", "call_model")

        # 编译图 - 只有显式设置了checkpointer才使用
        compile_kwargs = {}
        if self._checkpointer is not None:
            compile_kwargs["checkpointer"] = self._checkpointer

        return self._graph_builder.compile(**compile_kwargs)


def create_agent_graph(
    checkpointer: Any | None = None,
    enable_approval: bool = False,
) -> Any:
    """
    创建 Agent 图

    Args:
        checkpointer: 检查点保存器
        enable_approval: 是否启用人工审批

    Returns:
        编译后的 Agent 图
    """
    builder = AgentGraphBuilder()

    if checkpointer:
        builder.with_checkpointer(checkpointer)

    if enable_approval:
        builder.with_human_approval(True)

    return builder.build()


def create_agent_graph_with_memory() -> Any:
    """
    创建带内存检查点的 Agent 图

    Returns:
        编译后的 Agent 图
    """
    return create_agent_graph(checkpointer=MemorySaver())
