"""
记忆系统模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base

VECTOR_DIMENSION = 1536


class EpisodesMemory(Base):
    """情景记忆"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id", "")
        self.session_id = kwargs.get("session_id")
        self.task_id = kwargs.get("task_id")
        self.intent_text = kwargs.get("intent_text", "")
        self.intent_vector = kwargs.get("intent_vector")
        self.plan_dag = kwargs.get("plan_dag")
        self.execution_summary = kwargs.get("execution_summary")
        self.evaluation_report = kwargs.get("evaluation_report")
        self.final_score = kwargs.get("final_score")
        self.tags = kwargs.get("tags")
        self.created_at = kwargs.get("created_at", datetime.now())


class SemanticMemory(Base):
    """语义记忆"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id", "")
        self.source_type = kwargs.get("source_type", "")
        self.source_id = kwargs.get("source_id")
        self.content = kwargs.get("content", "")
        self.embedding = kwargs.get("embedding")
        self.memory_metadata = kwargs.get("memory_metadata")
        self.tags = kwargs.get("tags", [])
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")


class KnowledgeBase(Base):
    """外部知识库"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id", "")
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.type = kwargs.get("type", "document")
        self.source_url = kwargs.get("source_url")
        self.status = kwargs.get("status", "processing")
        self.doc_count = kwargs.get("doc_count", 0)
        self.tags = kwargs.get("tags", [])
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")


class Tag(Base):
    """Tag 注册表"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id")
        self.name = kwargs.get("name", "")
        self.vector = kwargs.get("vector")
        self.tag_type = kwargs.get("tag_type", "auto")
        self.frequency = kwargs.get("frequency", 0)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.memory_tags = kwargs.get("memory_tags", [])


class MemoryTag(Base):
    """记忆-Tag 关联"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id")
        self.memory_id = kwargs.get("memory_id", "")
        self.memory_type = kwargs.get("memory_type", "")
        self.tag_id = kwargs.get("tag_id")
        self.weight = kwargs.get("weight", 1.0)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.tag = kwargs.get("tag")


class TagCooccurrence(Base):
    """Tag 共现关系"""

    def __init__(self, **kwargs):
        self.tag1_id = kwargs.get("tag1_id")
        self.tag2_id = kwargs.get("tag2_id")
        self.cooccurrence_count = kwargs.get("cooccurrence_count", 1)
        self.last_updated = kwargs.get("last_updated", datetime.now())


class MemoryChunk(Base):
    """记忆分块"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id", "")
        self.session_id = kwargs.get("session_id")
        self.executor_type = kwargs.get("executor_type")
        self.executor_id = kwargs.get("executor_id")
        self.executor_name = kwargs.get("executor_name")
        self.layer = kwargs.get("layer", "")
        self.content = kwargs.get("content", "")
        self.embedding = kwargs.get("embedding")
        self.token_count = kwargs.get("token_count", 0)
        self.start_time = kwargs.get("start_time")
        self.end_time = kwargs.get("end_time")
        self.message_count = kwargs.get("message_count", 0)
        self.graduated = kwargs.get("graduated", False)
        self.episode_id = kwargs.get("episode_id")
        self.chunk_metadata = kwargs.get("chunk_metadata")
        self.created_at = kwargs.get("created_at", datetime.now())
