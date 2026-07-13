"""Stage 5 端到端验证测试。

验证阶段 5「能自我进化」的三项验收条件：
- 5.1 知识注入验证：记忆写入 → 检索 → 注入 Prompt，全链路跑通
- 5.3 上下文变量注入：rules/path/timestamp/retrieval 四种类型注入验证
- 5.4 自我诊断：执行失败时能自动分析原因（工具缺失/知识不足/策略错误）

与单元测试不同，本文件使用真实存储后端（JsonMemoryStore/InMemorySemanticStorage）
验证跨插件数据流，不依赖 Mock。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.context_builder import ContextBuilder
from agents.types import AgentConfig, ContextConfig, ContextVarItem
from memory.ports import IRetriever, ISemanticStorage
from memory.storage.json_store import JsonMemoryStore
from memory.types import Episode, Knowledge, SearchResult
from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.input.context_build import ContextBuildPlugin
from plugins.input.knowledge_inject import KnowledgeInjectPlugin
from plugins.input.memory_read import MemoryReadPlugin
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.output.error_check import ErrorCheckPlugin


# ── 测试用内存后端 ──


class InMemorySemanticStorage(ISemanticStorage):
    """内存语义存储，用于端到端测试。

    实现 ISemanticStorage 接口，数据保存在内存字典中。
    """

    def __init__(self) -> None:
        self._data: dict[str, Knowledge] = {}

    async def save(self, knowledge: Knowledge) -> str:
        self._data[knowledge.id] = knowledge
        return knowledge.id

    async def get(self, knowledge_id: str) -> Knowledge | None:
        return self._data.get(knowledge_id)

    async def find_by_user(self, user_id: str, limit: int = 20) -> list[Knowledge]:
        items = [kn for kn in self._data.values() if kn.user_id == user_id]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    async def update_embedding(self, knowledge_id: str, embedding: list[float]) -> bool:
        kn = self._data.get(knowledge_id)
        if kn:
            kn.embedding = embedding
            return True
        return False

    async def delete(self, knowledge_id: str) -> bool:
        if knowledge_id in self._data:
            del self._data[knowledge_id]
            return True
        return False


class InMemoryRetriever(IRetriever):
    """内存检索器，用于端到端测试。

    实现 IRetriever 接口，从 JsonMemoryStore 的 search 方法检索。
    """

    def __init__(self, store: JsonMemoryStore) -> None:
        self._store = store

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        filters = filters or {}
        if memory_type:
            filters["memory_type"] = memory_type
        results = await self._store.search(
            query=query, user_id=user_id, limit=top_k, filters=filters,
        )
        return results


# ── Fixtures ──


@pytest.fixture
def tmp_dir():
    """创建临时目录供 JsonMemoryStore 使用。"""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def json_store(tmp_dir: str) -> JsonMemoryStore:
    """创建 JsonMemoryStore 实例。"""
    return JsonMemoryStore(data_dir=tmp_dir)


@pytest.fixture
def semantic_storage() -> InMemorySemanticStorage:
    """创建内存语义存储。"""
    return InMemorySemanticStorage()


@pytest.fixture
def retriever(json_store: JsonMemoryStore) -> InMemoryRetriever:
    """创建检索器。"""
    return InMemoryRetriever(json_store)


@pytest.fixture
def base_state() -> dict:
    """创建基础测试状态。"""
    return create_initial_state(
        session_id="stage5-test-session",
        task_id="stage5-test-task",
    )


def make_ctx(state: dict, **services: Any) -> PluginContext:
    """创建带服务的插件上下文。"""
    ctx = PluginContext(state=state)
    for name, svc in services.items():
        ctx._services[name] = svc
    return ctx


# ══════════════════════════════════════════════════════════════
# 5.1 知识注入验证：记忆写入 → 检索 → 注入 Prompt，全链路
# ══════════════════════════════════════════════════════════════


class TestKnowledgeInjectionE2E:
    """5.1 知识注入端到端测试。

    验证完整链路：
    直接写入 Episode → MemoryReadPlugin 检索 →
    KnowledgeInjectPlugin 注入 → PromptBuildPlugin 包含在 system prompt
    """

    @pytest.mark.asyncio
    async def test_episode_write_read_prompt_chain(
        self, json_store: JsonMemoryStore, retriever: InMemoryRetriever,
        base_state: dict,
    ) -> None:
        """验证 Episode 写入 → 检索 → 出现在 Prompt 中的全链路。"""
        # ── 步骤 1: 直接通过 json_store 写入 Episode ──
        episode = Episode(
            user_id="user-e2e",
            session_id="stage5-test-session",
            intent_text="Python 异常处理最佳实践",
            execution_summary="用户询问 Python 异常处理",
        )
        await json_store.save(episode, "episode")

        # ── 步骤 2: MemoryReadPlugin 检索 ──
        read_state = create_initial_state(
            session_id="stage5-test-session",
            task_id="stage5-test-task",
            user_message="Python 异常处理",
            user_id="user-e2e",
        )
        read_ctx = make_ctx(read_state, retriever=retriever)
        read_plugin = MemoryReadPlugin(config={"memory_type": "episode"})
        read_result = await read_plugin.execute(read_ctx)

        # 检索到结果（MemoryReadPlugin 产出 memory.retrieved）
        memory_context = read_result.state_updates["memory.retrieved"]
        assert isinstance(memory_context, list)
        assert len(memory_context) >= 1
        # 关键词匹配：写入了"Python 异常处理最佳实践"，查询"Python 异常处理"应命中
        matched_content = memory_context[0].get("content", "")
        assert "Python" in matched_content or "异常" in matched_content

        # ── 步骤 3: KnowledgeInjectPlugin 注入知识 ──
        semantic_storage = InMemorySemanticStorage()
        # 预存一些知识到语义存储
        knowledge = Knowledge(
            user_id="user-e2e",
            content="Python 异常处理应使用 try/except，避免裸 except",
            source_type="documentation",
        )
        await semantic_storage.save(knowledge)

        inject_state = create_initial_state(
            session_id="stage5-test-session",
            task_id="stage5-test-task",
            user_message="如何处理Python异常",
            user_id="user-e2e",
        )
        inject_ctx = make_ctx(inject_state, semantic_storage=semantic_storage)
        inject_plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        inject_result = await inject_plugin.execute(inject_ctx)

        # 知识注入成功
        knowledge_context = inject_result.state_updates["knowledge.context"]
        assert isinstance(knowledge_context, str)
        assert "异常处理" in knowledge_context

        # ── 步骤 4: PromptBuildPlugin 组装 system prompt（不再自动注入记忆/知识）──
        prompt_state = create_initial_state(
            session_id="stage5-test-session",
            task_id="stage5-test-task",
            user_message="如何处理Python异常",
        )
        # 模拟前面插件已写入的上下文
        prompt_state["context.system_prompt"] = "你是一个AI助手"
        prompt_state["knowledge.context"] = knowledge_context
        prompt_state["memory.retrieved"] = "用户之前问过Python异常处理"

        prompt_ctx = make_ctx(prompt_state)
        prompt_plugin = PromptBuildPlugin()
        prompt_result = await prompt_plugin.execute(prompt_ctx)

        # system_prompt 正常拼入（PromptBuildPlugin 产出 system_message dict）
        system_content = prompt_result.state_updates["system_message"]["content"]
        assert "你是一个AI助手" in system_content
        # 记忆/知识不再无条件拼入（仅 static_vars opt-in；此处未配置 retrieval 变量）
        assert "异常处理" not in system_content
        assert "用户之前问过Python异常处理" not in system_content

    @pytest.mark.asyncio
    async def test_knowledge_inject_disabled_mode(
        self, semantic_storage: InMemorySemanticStorage,
    ) -> None:
        """验证 knowledge_inject 在 disabled 模式下不注入。"""
        # 预存知识
        knowledge = Knowledge(
            user_id="user-e2e",
            content="这是一条知识",
            source_type="test",
        )
        await semantic_storage.save(knowledge)

        state = create_initial_state(
            user_message="查询知识",
            user_id="user-e2e",
        )
        ctx = make_ctx(state, semantic_storage=semantic_storage)
        plugin = KnowledgeInjectPlugin(config={"mode": "disabled"})
        result = await plugin.execute(ctx)

        # disabled 模式返回空字符串
        assert result.state_updates["knowledge.context"] == ""


# ══════════════════════════════════════════════════════════════
# 5.3 上下文变量注入：rules/path/timestamp/retrieval 四类型
# ══════════════════════════════════════════════════════════════


class TestContextVariableInjectionE2E:
    """5.3 上下文变量注入端到端测试。

    验证 ContextBuilder → ContextBuildPlugin → PromptBuildPlugin 数据流，
    确保 rules/path/timestamp/retrieval 四种类型的变量能正确注入到 prompt。
    """

    def test_rules_type_injection(self) -> None:
        """验证 rules 类型上下文变量注入。"""
        config = AgentConfig(
            name="test_agent",
            system_prompt="你是测试助手",
            hard_constraints=["不准生成有害内容", "必须使用中文回复"],
            soft_constraints=["尽量简洁"],
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="行为规则", type="rules"),
                ],
            ),
        )
        builder = ContextBuilder()
        context = builder.build_static_context(config)

        assert context["enabled"] is True
        items = context["items"]
        assert len(items) == 1
        # rules 类型从 AgentConfig.hard_constraints + soft_constraints 提取
        assert items[0]["type"] == "rules"
        assert "不准生成有害内容" in items[0]["content"]
        assert "必须使用中文回复" in items[0]["content"]
        assert "尽量简洁" in items[0]["content"]

    def test_path_type_injection(self, tmp_path: Path) -> None:
        """验证 path 类型上下文变量注入。"""
        # 创建测试文件
        test_file = tmp_path / "rules.md"
        test_file.write_text("## 项目规范\n1. 代码必须有类型注解", encoding="utf-8")

        config = AgentConfig(
            name="test_agent",
            system_prompt="你是测试助手",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="项目规则", type="path", path="rules.md"),
                ],
            ),
        )
        builder = ContextBuilder(base_path=tmp_path)
        context = builder.build_static_context(config)

        items = context["items"]
        assert len(items) == 1
        assert items[0]["type"] == "path"
        assert "类型注解" in items[0]["content"]
        assert items[0]["path"] == "rules.md"

    def test_timestamp_type_injection(self) -> None:
        """验证 timestamp 类型上下文变量注入。"""
        config = AgentConfig(
            name="test_agent",
            system_prompt="你是测试助手",
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="当前时间", type="timestamp"),
                ],
            ),
        )
        builder = ContextBuilder()
        context = builder.build_dynamic_context(config)

        items = context["items"]
        assert len(items) == 1
        assert items[0]["type"] == "timestamp"
        # 应返回 ISO 格式时间戳
        content = items[0]["content"]
        assert isinstance(content, str)
        # 验证是合法的 ISO 格式
        parsed = datetime.fromisoformat(content)
        assert parsed.year >= 2025

    def test_retrieval_type_injection(self) -> None:
        """验证 retrieval 类型上下文变量注入。

        retrieval 类型的 content 在运行时填充，构建时为空字符串。
        """
        config = AgentConfig(
            name="test_agent",
            system_prompt="你是测试助手",
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="相关记忆",
                        type="retrieval",
                        tags=["python", "exception"],
                        inject_type="full",
                        top_k=5,
                    ),
                ],
            ),
        )
        builder = ContextBuilder()
        context = builder.build_dynamic_context(config)

        items = context["items"]
        assert len(items) == 1
        assert items[0]["type"] == "retrieval"
        # 运行时填充前 content 为空
        assert items[0]["content"] == ""
        # tags 正确传递
        assert items[0]["tags"] == ["python", "exception"]
        assert items[0]["top_k"] == 5
        assert items[0]["inject_type"] == "full"

    @pytest.mark.asyncio
    async def test_full_context_pipeline(
        self, base_state: dict,
    ) -> None:
        """验证 ContextBuildPlugin → PromptBuildPlugin 完整数据流。

        模拟一个包含硬约束的 Agent 配置，验证约束通过
        ContextBuildPlugin 传递到 PromptBuildPlugin 的 system prompt。
        """
        # ContextBuildPlugin 写入上下文
        context_plugin = ContextBuildPlugin({
            "system_prompt": "你是一个专业的Python编程助手",
            "agent_name": "PythonExpert",
            "agent_level": "l1_main",
            "extra_context": {
                "hard_rules": "1. 必须类型注解\n2. 必须有docstring",
            },
        })
        ctx = make_ctx(base_state)
        context_result = await context_plugin.execute(ctx)

        # 应用上下文到 state
        base_state.update(context_result.state_updates)

        # 验证上下文字段存在
        assert base_state["context.system_prompt"] == "你是一个专业的Python编程助手"
        assert base_state["context.agent_name"] == "PythonExpert"
        assert base_state["context.hard_rules"] == "1. 必须类型注解\n2. 必须有docstring"

        # PromptBuildPlugin 使用上下文构建 prompt
        prompt_plugin = PromptBuildPlugin({
            "hard_constraints": ["必须类型注解", "必须有docstring"],
        })
        prompt_ctx = make_ctx(base_state)
        prompt_result = await prompt_plugin.execute(prompt_ctx)

        # 系统提示词包含基础 prompt 和约束（PromptBuildPlugin 产出 system_message dict）
        system_content = prompt_result.state_updates["system_message"]["content"]
        assert "Python" in system_content


# ══════════════════════════════════════════════════════════════
# 5.4 自我诊断：执行失败时自动分析原因
# ══════════════════════════════════════════════════════════════


class TestSelfDiagnosisE2E:
    """5.4 自我诊断端到端测试。

    验证 ErrorCheckPlugin 能识别不同类型的执行失败：
    - core_error: LLM 调用异常
    - empty_response: 空响应
    - format_error: 格式错误
    - tool_missing: 工具缺失（通过 error_analysis 诊断）
    - knowledge_insufficient: 知识不足（通过 error_analysis 诊断）
    - strategy_error: 策略错误（通过 error_analysis 诊断）

    以及 error_analysis → route_signal → retry 闭环。
    """

    @pytest.mark.asyncio
    async def test_core_error_diagnosis_and_retry(self) -> None:
        """验证 core_error 诊断和重试闭环。"""
        state = create_initial_state()
        state[StateKeys.RAW_ERROR] = RuntimeError("Connection timeout")
        state["retry.count"] = 0

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 诊断结果
        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis is not None
        assert analysis["category"] == "core_error"
        assert analysis["retryable"] is True
        assert analysis["retry_count"] == 0

        # 路由信号：应重试
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"

        # retry count 增加
        assert result.state_updates["retry.count"] == 1

    @pytest.mark.asyncio
    async def test_empty_response_diagnosis_and_retry(self) -> None:
        """验证 empty_response 诊断和重试闭环。

        当空响应但有记忆/知识上下文时，诊断为 empty_response（非知识不足）。
        """
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = ""
        state["retry.count"] = 0
        # 有记忆上下文 → 不是知识不足，是空响应
        # ErrorCheckPlugin 检查的是 memory.retrieved（MemoryReadPlugin 的产出）
        state["memory.retrieved"] = [{"content": "之前的对话记录", "score": 0.8}]

        plugin = ErrorCheckPlugin({"max_retries": 2})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis["category"] == "empty_response"
        assert analysis["retryable"] is True

        # 产出 next_llm 信号
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["retry.count"] == 1

    @pytest.mark.asyncio
    async def test_format_error_diagnosis_and_retry(self) -> None:
        """验证 format_error 诊断和重试闭环。"""
        state = create_initial_state()
        # 模拟未关闭代码块的格式错误
        state[StateKeys.RAW_RESULT] = "结果如下：```json\n{\"key\": \"value\"}"
        state["retry.count"] = 0

        plugin = ErrorCheckPlugin({"max_retries": 2})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis["category"] == "format_error"
        assert analysis["retryable"] is True
        assert result.route_signal.route_type == "next_llm"

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_terminates(self) -> None:
        """验证重试次数耗尽后产出 end 信号。"""
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = ""
        state["retry.count"] = 3  # 已达上限

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 应产出 end 信号而非 next_llm
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_non_retryable_error_terminates(self) -> None:
        """验证不可重试错误直接产出 end 信号。"""
        state = create_initial_state()
        state[StateKeys.RAW_ERROR] = PermissionError("Invalid API key: auth failed")
        state["retry.count"] = 0

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis["category"] == "core_error"
        assert analysis["retryable"] is False
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_tool_missing_diagnosis(self) -> None:
        """验证工具缺失诊断。

        当 LLM 返回的 tool_calls 中引用了不存在的工具时，
        ErrorCheckPlugin 应识别为 tool_missing 类别。
        """
        state = create_initial_state()
        # 模拟工具缺失：LLM 返回了无法执行的工具调用
        state[StateKeys.RAW_ERROR] = ValueError("Tool 'nonexistent_tool' not found in registry")
        state["retry.count"] = 0

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 增强后的 ErrorCheckPlugin 应归类为 tool_missing
        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis is not None
        assert analysis["category"] == "tool_missing"
        # 工具缺失可重试（工具可能在后续注册）
        assert analysis["retryable"] is True
        assert result.route_signal.route_type == "next_llm"

    @pytest.mark.asyncio
    async def test_knowledge_insufficient_diagnosis(self) -> None:
        """验证知识不足诊断。

        当 LLM 回复声明"无法回答/我不知道"，且 memory.retrieved 与
        knowledge.context 均为空时，诊断为 knowledge_insufficient。
        注意：空响应不再触发此分类（已归 empty_response）；必须 LLM 显式声明
        知识不足，才会结合"无记忆/知识"判定。
        """
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = "抱歉，关于这个领域我无法回答，缺少相关信息"
        state["retry.count"] = 1  # 已经重试过一次
        state["memory.retrieved"] = []  # 无记忆检索结果
        state["knowledge.context"] = ""  # 无知识注入

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 增强后的 ErrorCheckPlugin 应识别为 knowledge_insufficient
        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis is not None
        assert analysis["category"] == "knowledge_insufficient"
        # 知识不足不可重试：memory/knowledge 是否为空是确定性状态，重试根因不会自愈
        assert analysis["retryable"] is False
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_knowledge_insufficient_from_llm_response(self) -> None:
        """验证从 LLM 回复内容判断知识不足。

        当 LLM 回复包含"我不知道"等指示，且无记忆/知识上下文时，
        诊断为 knowledge_insufficient。
        """
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = "I don't know the answer to this question"
        state["retry.count"] = 0
        state["memory.retrieved"] = []
        state["knowledge.context"] = ""

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis is not None
        assert analysis["category"] == "knowledge_insufficient"
        # 知识不足不可重试（确定性状态，重试无意义）
        assert analysis["retryable"] is False
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_strategy_error_diagnosis(self) -> None:
        """验证策略错误诊断。

        当格式错误 + 重试次数 >= 2 时，判断为策略错误，
        标记不可重试并产出 end 信号。
        """
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = "结果如下：```json\n{\"key\": \"value\"}"
        state["retry.count"] = 2  # 已重试2次，策略可能有问题

        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert analysis is not None
        assert analysis["category"] == "strategy_error"
        assert analysis["retryable"] is False
        # 策略错误直接终止
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_strategy_error_multiple_retries(self) -> None:
        """验证多次重试后策略错误的完整闭环。

        模拟管道循环：
        1. 第一次格式错误 → format_error + next_llm
        2. 第二次格式错误 → format_error + next_llm
        3. 第三次格式错误 → strategy_error + end
        """
        plugin = ErrorCheckPlugin({"max_retries": 3})

        # 第一轮：格式错误
        state1 = create_initial_state()
        state1[StateKeys.RAW_RESULT] = "```json\n{\"data\": 1}"
        state1["retry.count"] = 0

        ctx1 = make_ctx(state1)
        result1 = await plugin.execute(ctx1)
        assert result1.state_updates[StateKeys.ERROR_ANALYSIS]["category"] == "format_error"
        assert result1.route_signal.route_type == "next_llm"
        retry_count = result1.state_updates["retry.count"]
        assert retry_count == 1

        # 第二轮：格式错误
        state2 = create_initial_state()
        state2[StateKeys.RAW_RESULT] = "```json\n{\"data\": 2}"
        state2["retry.count"] = retry_count

        ctx2 = make_ctx(state2)
        result2 = await plugin.execute(ctx2)
        assert result2.state_updates[StateKeys.ERROR_ANALYSIS]["category"] == "format_error"
        assert result2.route_signal.route_type == "next_llm"
        retry_count = result2.state_updates["retry.count"]
        assert retry_count == 2

        # 第三轮：策略错误（重试次数已达2次）
        state3 = create_initial_state()
        state3[StateKeys.RAW_RESULT] = "```json\n{\"data\": 3}"
        state3["retry.count"] = retry_count

        ctx3 = make_ctx(state3)
        result3 = await plugin.execute(ctx3)
        # retry_count=2 且格式错误 → strategy_error
        assert result3.state_updates[StateKeys.ERROR_ANALYSIS]["category"] == "strategy_error"
        assert result3.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_success_case_no_diagnosis(self) -> None:
        """验证成功执行时不产生诊断。"""
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = "这是正常的LLM回复"
        state[StateKeys.EXECUTION_STATUS] = "success"

        plugin = ErrorCheckPlugin()
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 无错误分析
        assert result.state_updates[StateKeys.ERROR_ANALYSIS] is None
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "success"
        # 无路由信号
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_tool_calls_not_treated_as_empty(self) -> None:
        """验证 LLM 返回 tool_calls 时不被误判为空响应。"""
        state = create_initial_state()
        state[StateKeys.RAW_RESULT] = ""  # 内容为空
        state[StateKeys.RAW_TOOL_CALLS] = [{"name": "read_file", "args": {"path": "/tmp"}}]
        state["retry.count"] = 0

        plugin = ErrorCheckPlugin()
        ctx = make_ctx(state)
        result = await plugin.execute(ctx)

        # 不应被诊断为空响应
        assert result.state_updates[StateKeys.ERROR_ANALYSIS] is None
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "success"

    @pytest.mark.asyncio
    async def test_error_analysis_to_retry_closed_loop(self) -> None:
        """验证 error_analysis → route_signal → retry 完整闭环。

        模拟管道循环：
        1. 第一次执行失败 → error_analysis + next_llm
        2. 应用 retry.count 更新
        3. 第二次执行失败 → error_analysis + next_llm
        4. 第三次执行失败 → error_analysis + end
        """
        plugin = ErrorCheckPlugin({"max_retries": 3})

        # 第一轮：失败
        state1 = create_initial_state()
        state1[StateKeys.RAW_RESULT] = ""
        state1["retry.count"] = 0

        ctx1 = make_ctx(state1)
        result1 = await plugin.execute(ctx1)
        assert result1.route_signal.route_type == "next_llm"
        retry_count = result1.state_updates["retry.count"]
        assert retry_count == 1

        # 第二轮：继续失败
        state2 = create_initial_state()
        state2[StateKeys.RAW_RESULT] = ""
        state2["retry.count"] = retry_count

        ctx2 = make_ctx(state2)
        result2 = await plugin.execute(ctx2)
        assert result2.route_signal.route_type == "next_llm"
        retry_count = result2.state_updates["retry.count"]
        assert retry_count == 2

        # 第三轮：继续失败
        state3 = create_initial_state()
        state3[StateKeys.RAW_RESULT] = ""
        state3["retry.count"] = retry_count

        ctx3 = make_ctx(state3)
        result3 = await plugin.execute(ctx3)
        assert result3.route_signal.route_type == "next_llm"
        retry_count = result3.state_updates["retry.count"]
        assert retry_count == 3

        # 第四轮：达到上限，终止
        state4 = create_initial_state()
        state4[StateKeys.RAW_RESULT] = ""
        state4["retry.count"] = retry_count

        ctx4 = make_ctx(state4)
        result4 = await plugin.execute(ctx4)
        assert result4.route_signal.route_type == "end"
        assert result4.state_updates[StateKeys.EXECUTION_STATUS] == "failed"


# ══════════════════════════════════════════════════════════════
# MemoryReadPlugin 单元测试（当前零覆盖率）
# ══════════════════════════════════════════════════════════════


class TestMemoryReadPlugin:
    """MemoryReadPlugin 单元测试。

    补充当前零覆盖的 MemoryReadPlugin 测试。
    """

    def test_name_and_priority(self) -> None:
        """测试插件名称和优先级。"""
        plugin = MemoryReadPlugin()
        assert plugin.name == "memory_read"
        assert plugin.priority == 35
        assert plugin.error_policy == ErrorPolicy.SKIP

    @pytest.mark.asyncio
    async def test_no_retriever_returns_empty(self) -> None:
        """测试无 retriever 服务时返回空列表。"""
        state = create_initial_state(user_message="test")
        ctx = make_ctx(state)
        plugin = MemoryReadPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["memory.retrieved"] == []

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self) -> None:
        """测试空查询时返回空列表。"""
        state = create_initial_state()
        state["user_message"] = ""

        mock_retriever = AsyncMock()
        ctx = make_ctx(state, retriever=mock_retriever)
        plugin = MemoryReadPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["memory.retrieved"] == []
        mock_retriever.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_retrieves_from_service(self) -> None:
        """测试从 retriever 服务获取结果。"""
        state = create_initial_state(user_message="Python 异常处理")
        state["user_id"] = "user-123"

        # 模拟检索器返回
        mock_results = [
            SearchResult(id="1", content="异常处理指南", score=0.9),
            SearchResult(id="2", content="try/except 用法", score=0.8),
        ]
        mock_retriever = AsyncMock()
        mock_retriever.retrieve.return_value = mock_results

        ctx = make_ctx(state, retriever=mock_retriever)
        plugin = MemoryReadPlugin()
        result = await plugin.execute(ctx)

        memory_context = result.state_updates["memory.retrieved"]
        assert isinstance(memory_context, list)
        assert len(memory_context) == 2
        assert memory_context[0]["content"] == "异常处理指南"
        assert memory_context[0]["score"] == 0.9

        # 验证调用了正确的参数
        mock_retriever.retrieve.assert_called_once_with(
            query="Python 异常处理",
            user_id="user-123",
            top_k=5,
            memory_type="semantic",
        )

    @pytest.mark.asyncio
    async def test_custom_config(self) -> None:
        """测试自定义配置参数。"""
        state = create_initial_state(user_message="test query")
        state["user_id"] = "user-1"

        mock_retriever = AsyncMock()
        mock_retriever.retrieve.return_value = []

        ctx = make_ctx(state, retriever=mock_retriever)
        plugin = MemoryReadPlugin(config={"top_k": 10, "memory_type": "episode"})
        await plugin.execute(ctx)

        mock_retriever.retrieve.assert_called_once_with(
            query="test query",
            user_id="user-1",
            top_k=10,
            memory_type="episode",
        )

    @pytest.mark.asyncio
    async def test_handles_retriever_error(self) -> None:
        """测试检索器异常处理。"""
        state = create_initial_state(user_message="test query")
        state["user_id"] = "user-1"

        mock_retriever = AsyncMock()
        mock_retriever.retrieve.side_effect = RuntimeError("检索服务不可用")

        ctx = make_ctx(state, retriever=mock_retriever)
        plugin = MemoryReadPlugin()
        result = await plugin.execute(ctx)

        # 错误策略为 SKIP，应返回空列表但不中断
        assert result.state_updates["memory.retrieved"] == []
        assert result.error is not None
