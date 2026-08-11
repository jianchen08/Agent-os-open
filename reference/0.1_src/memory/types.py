"""记忆模块类型定义。

从旧代码 src/memory/types.py 搬迁，将 Pydantic BaseModel 替换为 dataclass。
移除 ORM/Pydantic 依赖，保持核心数据结构不变。

暴露接口：
- MemoryType: 记忆类型枚举
- InjectType: 注入方式枚举
- RetrievalMethod: 检索方法枚举
- ContextType: 上下文类型枚举
- Episode: 情景记忆数据类
- Knowledge: 语义记忆/知识数据类
- ToolInfo: 工具信息数据类
- ContextRequest: 上下文请求数据类
- Context: 上下文数据类
- SearchResult: 搜索结果数据类
- RetrievalConfig: 检索配置数据类
- TagInfo: Tag 信息数据类
- CooccurrenceEntry: 共现矩阵条目数据类
- TagBoostResult: Tag 增强结果数据类
- ChunkData: 压缩块数据类
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """记忆类型。"""

    EPISODE = "episode"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class InjectType(str, Enum):
    """注入方式（第二层决策）。"""

    FULL = "full"
    RETRIEVAL = "retrieval"
    SUMMARY = "summary"


class RetrievalMethod(str, Enum):
    """检索方法（第三层决策，仅 retrieval 注入方式时使用）。"""

    VECTOR = "vector"
    KEYWORD = "keyword"
    TAGWAVE = "tagwave"


class ContextType(str, Enum):
    """上下文类型。

    对应 context_window_config.yaml 中的 layer_order 配置。
    """

    # 固定层
    SYSTEM_PROMPT = "system_prompt"
    TOOL_DESCRIPTIONS = "tool_descriptions"
    DYNAMIC_VARIABLES = "dynamic_variables"

    # 记忆层（分层压缩）
    MEMORY_L2 = "memory_l2"
    MEMORY_L1 = "memory_l1"

    # 动态层
    RETRIEVAL = "retrieval"
    RECENT_MESSAGES = "recent_messages"
    USER_MESSAGE = "user_message"

    # 其他上下文类型
    USER_INTENT = "user_intent"
    AGENT_DEFINITION = "agent_definition"
    DOMAIN_KNOWLEDGE = "domain_knowledge"
    EXECUTION_HISTORY = "execution_history"
    USER_PREFERENCES = "user_preferences"
    ERROR_CONTEXT = "error_context"


@dataclass
class Episode:
    """情景记忆。

    记录用户意图、执行计划和结果评估。

    Attributes:
        id: 记忆唯一标识
        user_id: 用户 ID
        session_id: 会话 ID
        intent_text: 意图文本
        intent_vector: 意图向量嵌入
        plan_dag: 执行计划 DAG
        execution_summary: 执行摘要
        evaluation_report: 评估报告
        final_score: 最终得分 (0-1)
        tags: 标签列表
        created_at: 创建时间
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    session_id: str | None = None
    intent_text: str = ""
    intent_vector: list[float] | None = None
    plan_dag: dict[str, Any] | None = None
    execution_summary: str | None = None
    evaluation_report: dict[str, Any] | None = None
    final_score: float | None = None
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "intent_text": self.intent_text,
            "plan_dag": self.plan_dag,
            "execution_summary": self.execution_summary,
            "final_score": self.final_score,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Knowledge:
    """语义记忆/知识。

    Attributes:
        id: 知识唯一标识
        user_id: 用户 ID
        source_type: 来源类型
        source_id: 来源标识
        content: 知识内容
        embedding: 向量嵌入
        extra_data: 额外数据
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    source_type: str = ""
    source_id: str | None = None
    content: str = ""
    embedding: list[float] | None = None
    extra_data: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_type": self.source_type,
            "content": self.content,
            "extra_data": self.extra_data,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ToolInfo:
    """工具信息（程序性记忆）。

    Attributes:
        id: 工具唯一标识
        name: 工具名称
        description: 工具描述
        args_schema: 参数 Schema
        return_schema: 返回 Schema
        source_type: 来源类型
        requires_approval: 是否需要审批
        success_count: 成功次数
        last_used_at: 最后使用时间
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    source_type: str = "code"
    requires_approval: bool = False
    success_count: int = 0
    last_used_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "source_type": self.source_type,
            "requires_approval": self.requires_approval,
        }


@dataclass
class ContextRequest:
    """上下文请求。

    Attributes:
        required_memories: 必需上下文类型列表
        optional_memories: 可选上下文类型列表
        excluded_memories: 排除上下文类型列表
        domain_scope: 领域范围
        history_scope: 历史范围
        max_context_tokens: 最大 Token 数
    """

    required_memories: list[ContextType] = field(default_factory=list)
    optional_memories: list[ContextType] = field(default_factory=list)
    excluded_memories: list[ContextType] = field(default_factory=list)
    domain_scope: dict[str, Any] | None = None
    history_scope: dict[str, Any] | None = None
    max_context_tokens: int = 128000


@dataclass
class Context:
    """上下文数据类。

    包含上下文构建所需的各层数据，支持动态扩展。

    Attributes:
        system_prompt: 系统提示
        tool_descriptions: 工具描述
        dynamic_variables: 动态变量
        memory_l2: L2 三元组层
        memory_l1: L1 八段摘要层
        retrieval: 检索召回结果
        recent_messages: 最近对话
        user_message: 当前用户消息
        user_intent: 用户意图
        agent_definition: Agent 定义
        domain_knowledge: 领域知识
        execution_history: 执行历史
        user_preferences: 用户偏好
        error_context: 错误上下文
        total_tokens: 总 Token 数
        layer_tokens: 各层 Token 使用量
        extra: 动态扩展字段
    """

    # 固定层
    system_prompt: str | None = None
    tool_descriptions: list[dict[str, Any]] | None = None

    # 动态变量
    dynamic_variables: dict[str, str] | None = None

    # 分层记忆
    memory_l2: list[dict[str, Any]] | None = None
    memory_l1: list[dict[str, Any]] | None = None

    # 动态层
    retrieval: list[dict[str, Any]] | None = None
    recent_messages: list[dict[str, Any]] | None = None
    user_message: str | None = None

    # 其他上下文类型
    user_intent: str | None = None
    agent_definition: dict[str, Any] | None = None
    domain_knowledge: list[str] | None = None
    execution_history: list[dict[str, Any]] | None = None
    user_preferences: dict[str, Any] | None = None
    error_context: dict[str, Any] | None = None

    # Token 统计
    total_tokens: int = 0
    layer_tokens: dict[str, int] = field(default_factory=dict)

    # 动态扩展字段
    extra: dict[str, Any] = field(default_factory=dict)

    def get_layer_data(self, layer_id: str) -> Any:
        """获取层级数据（支持标准层级和动态层级）。

        Args:
            layer_id: 层级标识

        Returns:
            层级数据，不存在则返回 None
        """
        if hasattr(self, layer_id):
            val = getattr(self, layer_id)
            if val is not None:
                return val
        return self.extra.get(layer_id)

    def set_layer_data(self, layer_id: str, data: Any) -> None:
        """设置层级数据（支持标准层级和动态层级）。

        Args:
            layer_id: 层级标识
            data: 层级数据
        """
        if hasattr(self, layer_id) and layer_id != "extra":
            setattr(self, layer_id, data)
        else:
            self.extra[layer_id] = data

    def to_prompt(self) -> str:  # noqa: PLR0912
        """转换为提示词（按稳定性排序：稳定→动态）。

        Returns:
            按层级排序拼接的提示词字符串
        """
        parts: list[str] = []

        # 固定层
        if self.system_prompt:
            parts.append(self.system_prompt)

        if self.tool_descriptions:
            tools_text = "\n".join(
                f"- {t.get('name', 'unknown')}: {t.get('description', '')}" for t in self.tool_descriptions
            )
            parts.append(f"## 可用工具\n\n{tools_text}")

        # 动态变量
        if self.dynamic_variables:
            vars_text = "\n".join(f"- {k}: {v}" for k, v in self.dynamic_variables.items())
            parts.append(f"### 当前状态\n{vars_text}")

        # 分层记忆
        if self.memory_l2:
            summaries = "\n".join(str(item) for item in self.memory_l2)
            parts.append(f"## 历史摘要\n\n{summaries}")

        if self.memory_l1:
            details = "\n".join(str(item) for item in self.memory_l1)
            parts.append(f"## 详细历史\n\n{details}")

        # 动态层
        if self.recent_messages:
            messages = "\n".join(str(msg) for msg in self.recent_messages)
            parts.append(messages)

        if self.retrieval:
            results = "\n".join(str(item) for item in self.retrieval)
            parts.append(f"## 相关信息\n\n{results}")

        if self.user_message:
            parts.append(f"用户: {self.user_message}")

        # 向后兼容
        if self.user_intent:
            parts.append(f"## 用户意图\n{self.user_intent}")

        if self.agent_definition:
            parts.append(f"## Agent 定义\n{self.agent_definition}")

        if self.domain_knowledge:
            knowledge_text = "\n".join(f"- {k}" for k in self.domain_knowledge)
            parts.append(f"## 领域知识\n{knowledge_text}")

        if self.execution_history:
            history_text = "\n".join(str(h) for h in self.execution_history)
            parts.append(f"## 执行历史\n{history_text}")

        if self.user_preferences:
            parts.append(f"## 用户偏好\n{self.user_preferences}")

        if self.error_context:
            parts.append(f"## 错误上下文\n{self.error_context}")

        return "\n\n".join(parts)


@dataclass
class SearchResult:
    """搜索结果。

    Attributes:
        id: 记录 ID
        content: 内容
        score: 相关性得分 (0-1)
        memory_type: 记忆类型
        metadata: 元数据
        highlight: 高亮片段
    """

    id: str = ""
    content: str = ""
    score: float = 0.0
    memory_type: MemoryType = MemoryType.SEMANTIC
    metadata: dict[str, Any] | None = None
    highlight: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "content": self.content,
            "score": self.score,
            "memory_type": self.memory_type.value,
            "metadata": self.metadata,
            "highlight": self.highlight,
        }


@dataclass
class RetrievalConfig:
    """检索配置。

    Attributes:
        inject_type: 注入方式
        retrieval_method: 检索方法
        top_k: 返回数量
        min_score: 最小得分阈值
    """

    inject_type: InjectType = InjectType.RETRIEVAL
    retrieval_method: RetrievalMethod = RetrievalMethod.VECTOR
    top_k: int = 10
    min_score: float = 0.5


# ========== Tag 网络相关类型 ==========


@dataclass
class TagInfo:
    """Tag 信息。

    Attributes:
        id: Tag ID
        name: Tag 名称
        vector: Tag 向量
        frequency: 全局出现频率
    """

    id: int = 0
    name: str = ""
    vector: list[float] | None = None
    frequency: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "frequency": self.frequency,
        }


@dataclass
class CooccurrenceEntry:
    """共现矩阵条目。

    Attributes:
        tag1_id: Tag 1 ID
        tag2_id: Tag 2 ID
        weight: 共现次数
    """

    tag1_id: int = 0
    tag2_id: int = 0
    weight: int = 0


@dataclass
class TagBoostResult:
    """Tag 增强结果。

    Attributes:
        vector: 增强后的向量
        matched_tags: 匹配的 Tag 名称
        boost_factor: 增强因子
        spike_count: 扩展 Tag 数量
        total_spike_score: 总增强得分
    """

    vector: list[float] = field(default_factory=list)
    matched_tags: list[str] = field(default_factory=list)
    boost_factor: float = 0.0
    spike_count: int = 0
    total_spike_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "matched_tags": self.matched_tags,
            "boost_factor": self.boost_factor,
            "spike_count": self.spike_count,
            "total_spike_score": self.total_spike_score,
        }


@dataclass
class ChunkData:
    """压缩块数据。

    通过 pipeline_run_id 关联到具体管道运行，
    sequence 范围对应 ExecutionRecord 的 sequence 范围。
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pipeline_run_id: str = ""
    session_id: str = ""
    layer: str = "L1"
    content: str = ""
    l2_content: str = ""
    token_count: int = 0
    message_count: int = 0
    sequence_start: int = 0
    sequence_end: int = 0
    keywords: list[str] = field(default_factory=list)
    graduated: bool = False
    context_window: int = 0
    episode_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "pipeline_run_id": self.pipeline_run_id,
            "session_id": self.session_id,
            "layer": self.layer,
            "content": self.content,
            "l2_content": self.l2_content,
            "token_count": self.token_count,
            "message_count": self.message_count,
            "sequence_start": self.sequence_start,
            "sequence_end": self.sequence_end,
            "keywords": self.keywords,
            "graduated": self.graduated,
            "episode_id": self.episode_id,
            "context_window": self.context_window,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkData:
        """从字典创建实例。"""
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "created_at" in filtered and isinstance(filtered["created_at"], str):
            filtered["created_at"] = datetime.fromisoformat(filtered["created_at"])
        return cls(**filtered)
