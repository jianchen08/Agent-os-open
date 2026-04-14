"""记忆模块数据模型测试。

测试 types.py 中所有 dataclass 的创建、转换和验证逻辑。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from memory.types import (
    CooccurrenceEntry,
    Context,
    ContextRequest,
    ContextType,
    Episode,
    InjectType,
    Knowledge,
    MemoryType,
    RetrievalConfig,
    RetrievalMethod,
    SearchResult,
    TagBoostResult,
    TagInfo,
    ToolInfo,
)


class TestMemoryType:
    """MemoryType 枚举测试。"""

    def test_values(self) -> None:
        assert MemoryType.EPISODE.value == "episode"
        assert MemoryType.SEMANTIC.value == "semantic"
        assert MemoryType.PROCEDURAL.value == "procedural"

    def test_from_value(self) -> None:
        assert MemoryType("episode") == MemoryType.EPISODE
        assert MemoryType("semantic") == MemoryType.SEMANTIC


class TestInjectType:
    """InjectType 枚举测试。"""

    def test_values(self) -> None:
        assert InjectType.FULL.value == "full"
        assert InjectType.RETRIEVAL.value == "retrieval"
        assert InjectType.SUMMARY.value == "summary"


class TestRetrievalMethod:
    """RetrievalMethod 枚举测试。"""

    def test_values(self) -> None:
        assert RetrievalMethod.VECTOR.value == "vector"
        assert RetrievalMethod.KEYWORD.value == "keyword"
        assert RetrievalMethod.TAGWAVE.value == "tagwave"


class TestEpisode:
    """Episode 数据类测试。"""

    def test_default_values(self) -> None:
        ep = Episode()
        assert ep.id  # 非空字符串
        assert ep.user_id == ""
        assert ep.session_id is None
        assert ep.intent_text == ""
        assert ep.tags == []
        assert ep.final_score is None
        assert isinstance(ep.created_at, datetime)

    def test_custom_values(self) -> None:
        now = datetime.now(UTC)
        ep = Episode(
            id="test-id",
            user_id="user-1",
            session_id="session-1",
            intent_text="测试意图",
            plan_dag={"step1": "action1"},
            execution_summary="执行摘要",
            final_score=0.85,
            tags=["tag1", "tag2"],
            created_at=now,
        )
        assert ep.id == "test-id"
        assert ep.user_id == "user-1"
        assert ep.intent_text == "测试意图"
        assert ep.final_score == 0.85
        assert len(ep.tags) == 2

    def test_to_dict(self) -> None:
        ep = Episode(id="ep-1", user_id="user-1", intent_text="测试")
        d = ep.to_dict()
        assert d["id"] == "ep-1"
        assert d["user_id"] == "user-1"
        assert d["intent_text"] == "测试"
        assert "created_at" in d
        assert isinstance(d["tags"], list)

    def test_unique_ids(self) -> None:
        ep1 = Episode()
        ep2 = Episode()
        assert ep1.id != ep2.id


class TestKnowledge:
    """Knowledge 数据类测试。"""

    def test_default_values(self) -> None:
        kn = Knowledge()
        assert kn.id
        assert kn.user_id == ""
        assert kn.source_type == ""
        assert kn.content == ""
        assert kn.embedding is None
        assert kn.extra_data is None

    def test_custom_values(self) -> None:
        kn = Knowledge(
            user_id="user-1",
            source_type="file",
            content="知识内容",
            embedding=[0.1, 0.2, 0.3],
            extra_data={"key": "value"},
        )
        assert kn.user_id == "user-1"
        assert kn.embedding == [0.1, 0.2, 0.3]

    def test_to_dict(self) -> None:
        kn = Knowledge(id="kn-1", user_id="user-1", content="测试知识")
        d = kn.to_dict()
        assert d["id"] == "kn-1"
        assert d["content"] == "测试知识"


class TestContext:
    """Context 数据类测试。"""

    def test_default_values(self) -> None:
        ctx = Context()
        assert ctx.system_prompt is None
        assert ctx.tool_descriptions is None
        assert ctx.total_tokens == 0
        assert ctx.extra == {}

    def test_set_get_layer_data(self) -> None:
        ctx = Context()
        # 标准字段
        ctx.set_layer_data("user_message", "你好")
        assert ctx.get_layer_data("user_message") == "你好"

        # 动态字段
        ctx.set_layer_data("custom_field", "自定义值")
        assert ctx.get_layer_data("custom_field") == "自定义值"

        # 不存在的字段
        assert ctx.get_layer_data("nonexistent") is None

    def test_to_prompt(self) -> None:
        ctx = Context(
            system_prompt="你是助手",
            user_message="你好",
            domain_knowledge=["知识1", "知识2"],
        )
        prompt = ctx.to_prompt()
        assert "你是助手" in prompt
        assert "你好" in prompt
        assert "知识1" in prompt

    def test_to_prompt_empty(self) -> None:
        ctx = Context()
        assert ctx.to_prompt() == ""


class TestSearchResult:
    """SearchResult 数据类测试。"""

    def test_default_values(self) -> None:
        sr = SearchResult()
        assert sr.id == ""
        assert sr.content == ""
        assert sr.score == 0.0
        assert sr.memory_type == MemoryType.SEMANTIC

    def test_to_dict(self) -> None:
        sr = SearchResult(
            id="sr-1",
            content="测试结果",
            score=0.9,
            memory_type=MemoryType.EPISODE,
        )
        d = sr.to_dict()
        assert d["id"] == "sr-1"
        assert d["score"] == 0.9
        assert d["memory_type"] == "episode"


class TestRetrievalConfig:
    """RetrievalConfig 数据类测试。"""

    def test_default_values(self) -> None:
        rc = RetrievalConfig()
        assert rc.inject_type == InjectType.RETRIEVAL
        assert rc.retrieval_method == RetrievalMethod.VECTOR
        assert rc.top_k == 10
        assert rc.min_score == 0.5


class TestTagInfo:
    """TagInfo 数据类测试。"""

    def test_to_dict(self) -> None:
        ti = TagInfo(id=1, name="python", frequency=42)
        d = ti.to_dict()
        assert d["id"] == 1
        assert d["name"] == "python"
        assert d["frequency"] == 42


class TestTagBoostResult:
    """TagBoostResult 数据类测试。"""

    def test_to_dict(self) -> None:
        tbr = TagBoostResult(
            vector=[0.1, 0.2],
            matched_tags=["tag1", "tag2"],
            boost_factor=0.3,
            spike_count=5,
            total_spike_score=1.5,
        )
        d = tbr.to_dict()
        assert d["matched_tags"] == ["tag1", "tag2"]
        assert d["boost_factor"] == 0.3
        assert d["spike_count"] == 5


class TestToolInfo:
    """ToolInfo 数据类测试。"""

    def test_to_dict(self) -> None:
        ti = ToolInfo(name="test_tool", description="测试工具")
        d = ti.to_dict()
        assert d["name"] == "test_tool"
        assert d["description"] == "测试工具"


class TestContextRequest:
    """ContextRequest 数据类测试。"""

    def test_default_values(self) -> None:
        cr = ContextRequest()
        assert cr.required_memories == []
        assert cr.max_context_tokens == 128000

    def test_custom_values(self) -> None:
        cr = ContextRequest(
            required_memories=[ContextType.USER_MESSAGE],
            max_context_tokens=8000,
        )
        assert ContextType.USER_MESSAGE in cr.required_memories
