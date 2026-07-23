"""记忆系统数据模型。

从 src/memory/types.py 精简搬迁：仅保留 sidecar 检索/注入所需的数据类与枚举，
移除 ORM/Pydantic 依赖，import 改为本地相对引用。

暴露接口：
- MemoryType / InjectType / RetrievalMethod: 枚举
- SearchResult: 搜索结果数据类
- TagBoostResult / TagInfo / CooccurrenceEntry: Tag 网络相关数据类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """记忆类型。"""

    EPISODE = "episode"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class InjectType(str, Enum):
    """注入方式（记忆→上下文的第二层决策）。"""

    FULL = "full"
    RETRIEVAL = "retrieval"
    SUMMARY = "summary"


class RetrievalMethod(str, Enum):
    """检索方法（仅 retrieval 注入方式时使用）。"""

    VECTOR = "vector"
    KEYWORD = "keyword"
    TAGWAVE = "tagwave"


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
        return {"id": self.id, "name": self.name, "frequency": self.frequency}


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
