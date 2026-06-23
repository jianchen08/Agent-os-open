"""
Round 2 工具系统模块测试缺口补充。

与 round1 独立，以新视角审查工具系统，补充以下验收标准的测试覆盖：

- AC-TOOL-04 / F-TOOL-10 / F-TOOL-11: 动态 Schema 注入（image_generate enum 注入 Provider 列表）
- AC-TOOL-05: 工具超时返回 timeout 信息（handler 模式端到端）
- AC-TOOL-06: 工具异常返回 error 信息（handler 模式端到端）
- AC-TOOL-09: 工具结果缓存生效（通过 ToolExecutor 端到端缓存命中）
- AC-TOOL-10: 嵌套工具调用链追踪（NestedRecordManager）
- F-TOOL-12 ~ F-TOOL-15: 插件错误策略 ABORT/SKIP/FALLBACK/RETRY
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exceptions import ToolExecutionError
from pipeline.chain import PluginChain
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy
from tools.executor import ExecutionContext, ToolExecutor
from tools.registry import ToolRegistry
from tools.tool_cache import ToolCache, ToolCacheConfig
from tools.types import Tool, ToolSource, create_success_result


# ════════════════════════════════════════════════════════════════
# AC-TOOL-04 / F-TOOL-10 / F-TOOL-11: 动态 Schema 注入
# ════════════════════════════════════════════════════════════════


class TestDynamicSchemaInjection:
    """动态 Schema 注入测试：image_generate 的 enum 在运行时注入可用 Provider 列表。"""

    def _make_mock_registry(self, provider_names: list[str]) -> MagicMock:
        """创建含指定 Provider 名称的 mock MediaProviderRegistry。"""
        mock_providers = [SimpleNamespace(provider_name=n) for n in provider_names]
        mock_registry = MagicMock()
        mock_registry.list_by_type.return_value = mock_providers
        return mock_registry

    def test_enrich_injects_provider_enum(self):
        """AC-TOOL-04: enricher 将可用 Provider 名称注入到 enum 字段。

        F-TOOL-10: image_generate 的 Schema 在运行时动态注入 Provider 列表。
        F-TOOL-11: LLM 看到的总是"现在能用的服务"。
        """
        from tools.builtin.image_generate.tool import _enrich_image_schema
        from tools.builtin.image_generate.tool import ImageGenerateTool

        tool = ImageGenerateTool.get_tool_definition()
        mock_registry = self._make_mock_registry(["comfyui", "minimax"])

        enriched = _enrich_image_schema(tool, {"media_provider_registry": mock_registry})

        provider_prop = enriched.input_schema["properties"]["provider"]
        assert "enum" in provider_prop, "enricher 应注入 enum 字段"
        assert "comfyui" in provider_prop["enum"]
        assert "minimax" in provider_prop["enum"]
        assert "auto" in provider_prop["enum"], "enum 应包含 'auto' 选项"

    def test_enrich_no_registry_returns_original(self):
        """enricher 在无 media_provider_registry 时返回原始工具。"""
        from tools.builtin.image_generate.tool import _enrich_image_schema
        from tools.builtin.image_generate.tool import ImageGenerateTool

        tool = ImageGenerateTool.get_tool_definition()
        result = _enrich_image_schema(tool, {})
        assert result is tool

    def test_enrich_no_providers_returns_original(self):
        """enricher 在无可用 Provider 时返回原始工具。"""
        from tools.builtin.image_generate.tool import _enrich_image_schema
        from tools.builtin.image_generate.tool import ImageGenerateTool

        tool = ImageGenerateTool.get_tool_definition()
        mock_registry = MagicMock()
        mock_registry.list_by_type.return_value = []
        result = _enrich_image_schema(tool, {"media_provider_registry": mock_registry})
        assert result is tool

    def test_enrich_deepcopy_preserves_original(self):
        """enricher 使用 deepcopy，不修改原始工具定义。"""
        from tools.builtin.image_generate.tool import _enrich_image_schema
        from tools.builtin.image_generate.tool import ImageGenerateTool

        tool = ImageGenerateTool.get_tool_definition()
        original_provider = tool.input_schema["properties"]["provider"]
        assert "enum" not in original_provider

        mock_registry = self._make_mock_registry(["comfyui"])
        _enrich_image_schema(tool, {"media_provider_registry": mock_registry})

        # 原始工具未被修改
        assert "enum" not in tool.input_schema["properties"]["provider"]

    @pytest.mark.asyncio
    async def test_tool_schema_plugin_end_to_end_enrichment(self):
        """AC-TOOL-04: ToolSchemaPlugin 端到端验证 Schema 注入到 LLM 格式。

        模拟完整流程：ToolRegistry 注册工具 + enricher → ToolSchemaPlugin 执行 →
        生成的 tool_schemas 中 image_generate 的 provider 参数含动态 enum。
        """
        from plugins.input.tool_schema.plugin import ToolSchemaPlugin
        from tools.builtin.image_generate.tool import (
            ImageGenerateTool,
            _enrich_image_schema,
        )

        # 准备工具注册表
        registry = ToolRegistry(lazy_load=False)
        tool = ImageGenerateTool.get_tool_definition()
        registry.register(tool)
        registry.register_schema_enricher("image_generate", _enrich_image_schema)

        # 准备 mock MediaProviderRegistry
        mock_media_registry = self._make_mock_registry(["comfyui", "minimax"])

        # 构建 PluginContext
        ctx = PluginContext(
            state={},
            _services={
                "tool_registry": registry,
                "media_provider_registry": mock_media_registry,
            },
        )

        plugin = ToolSchemaPlugin(
            config={"enabled": True, "tool_ids": ["image_generate"]}
        )
        result = await plugin.execute(ctx)

        schemas = result.state_updates["tool_schemas"]
        assert len(schemas) == 1

        img_schema = schemas[0]
        assert img_schema["type"] == "function"
        assert img_schema["function"]["name"] == "image_generate"

        provider_prop = img_schema["function"]["parameters"]["properties"]["provider"]
        assert "enum" in provider_prop
        assert "comfyui" in provider_prop["enum"]
        assert "minimax" in provider_prop["enum"]
        assert "auto" in provider_prop["enum"]


# ════════════════════════════════════════════════════════════════
# AC-TOOL-05: 工具超时返回 timeout 信息（handler 模式）
# ════════════════════════════════════════════════════════════════


class TestHandlerModeTimeout:
    """handler 模式下的超时行为测试。

    现有测试通过 runnable 模式验证超时（返回 failure result），
    本测试补充 handler 模式的超时行为（抛出 ToolExecutionError）。
    """

    @pytest.mark.asyncio
    async def test_handler_mode_timeout_raises_tool_execution_error(self):
        """AC-TOOL-05: handler 模式超时抛出含 timeout 信息的 ToolExecutionError。

        handler 模式下，executor 通过 self._handlers 查找处理函数。
        需要使用 executor.register_handler() 注册到 executor 自身的 handler 字典中。
        """
        registry = ToolRegistry(lazy_load=False)

        async def slow_handler(args: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"data": "unreachable"}

        tool = Tool(
            name="slow_handler_tool",
            description="慢工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register(tool)

        executor = ToolExecutor(registry)
        executor.set_runnable_first(False)
        executor.register_handler("slow_handler_tool", slow_handler)
        ctx = ExecutionContext(session_id="test-session")

        with pytest.raises(ToolExecutionError) as exc_info:
            await executor.execute("slow_handler_tool", {}, ctx, timeout=0.1)

        assert "超时" in str(exc_info.value) or "timeout" in str(exc_info.value).lower()


# ════════════════════════════════════════════════════════════════
# AC-TOOL-06: 工具异常返回 error 信息（handler 模式）
# ════════════════════════════════════════════════════════════════


class TestHandlerModeException:
    """handler 模式下的异常行为测试。

    现有测试通过 runnable 模式验证异常（返回 failure result），
    本测试补充 handler 模式的异常行为（抛出 ToolExecutionError）。
    """

    @pytest.mark.asyncio
    async def test_handler_mode_exception_raises_with_original_message(self):
        """AC-TOOL-06: handler 模式异常抛出含原始错误信息的 ToolExecutionError。

        handler 模式下，executor 通过 self._handlers 查找处理函数。
        需要使用 executor.register_handler() 注册到 executor 自身的 handler 字典中。
        """
        registry = ToolRegistry(lazy_load=False)

        async def failing_handler(args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("handler 内部严重错误")

        tool = Tool(
            name="failing_handler_tool",
            description="失败工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register(tool)

        executor = ToolExecutor(registry)
        executor.set_runnable_first(False)
        executor.register_handler("failing_handler_tool", failing_handler)
        ctx = ExecutionContext(session_id="test-session")

        with pytest.raises(ToolExecutionError) as exc_info:
            await executor.execute("failing_handler_tool", {}, ctx)

        assert "handler 内部严重错误" in str(exc_info.value)


# ════════════════════════════════════════════════════════════════
# AC-TOOL-09: 工具结果缓存生效
# ════════════════════════════════════════════════════════════════


class TestCacheHitFlow:
    """通过 ToolExecutor 端到端验证缓存命中流程。

    现有测试验证了缓存配置/键生成/敏感信息检测，
    本测试补充通过 executor.execute() 的完整缓存命中和写入流程。
    """

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result_with_metadata(self):
        """AC-TOOL-09: 缓存命中时 executor 返回缓存结果并标记 from_cache。"""
        registry = ToolRegistry(lazy_load=False)

        call_count = 0

        async def echo_handler(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"echo": args.get("msg", ""), "call": call_count}

        tool = Tool(
            name="cached_echo",
            description="缓存回显",
            input_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, echo_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        # Mock 缓存命中
        cached_result = create_success_result(data={"echo": "cached", "call": 0})
        executor._tool_cache.should_cache = MagicMock(return_value=True)
        executor._tool_cache.get_cached_result = AsyncMock(return_value=cached_result)

        result = await executor.execute("cached_echo", {"msg": "hello"}, ctx)

        assert result.success
        assert result.output["echo"] == "cached"
        assert call_count == 0, "缓存命中时 handler 不应被调用"
        assert result.metadata is not None
        assert result.metadata.get("from_cache") is True
        assert result.metadata.get("duration_ms") == 0

    @pytest.mark.asyncio
    async def test_cache_miss_executes_and_caches_result(self):
        """AC-TOOL-09: 缓存未命中时执行工具并写入缓存。"""
        registry = ToolRegistry(lazy_load=False)

        async def echo_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args.get("msg", "")}

        tool = Tool(
            name="cache_miss_tool",
            description="缓存未命中工具",
            input_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, echo_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        # Mock 缓存未命中
        executor._tool_cache.should_cache = MagicMock(return_value=True)
        executor._tool_cache.get_cached_result = AsyncMock(return_value=None)
        executor._tool_cache.set_cached_result = AsyncMock()

        result = await executor.execute("cache_miss_tool", {"msg": "fresh"}, ctx)

        assert result.success
        assert result.output["echo"] == "fresh"
        # 验证结果被写入缓存
        executor._tool_cache.set_cached_result.assert_called_once()
        call_args = executor._tool_cache.set_cached_result.call_args
        assert call_args.args[0] == "cache_miss_tool"

    def test_tool_cache_should_cache_respects_config(self):
        """AC-TOOL-09: ToolCache.should_cache 遵循工具级配置。"""
        config = ToolCacheConfig(
            enabled=True,
            tools={"cacheable_tool": {"enabled": True}, "disabled_tool": {"enabled": False}},
        )
        cache = ToolCache(config)

        assert cache.should_cache("cacheable_tool", {"q": "test"})
        assert not cache.should_cache("disabled_tool", {"q": "test"})
        assert not cache.should_cache("unconfigured_tool", {"q": "test"})


# ════════════════════════════════════════════════════════════════
# AC-TOOL-10: 嵌套工具调用链追踪
# ════════════════════════════════════════════════════════════════


class TestNestedRecordManager:
    """NestedRecordManager 嵌套工具调用链追踪测试。

    验证嵌套执行记录的创建和更新逻辑，以及错误处理。
    """

    @pytest.mark.asyncio
    async def test_create_nested_record_no_db_returns_none(self):
        """AC-TOOL-10: 无 db_session 时优雅降级返回 None。"""
        from tools.nested_record_manager import NestedRecordManager

        mgr = NestedRecordManager(db_session=None)
        result = await mgr.create_nested_execution_record(
            parent_record_id="parent_123",
            session_id="session_123",
            tool_name="test_tool",
            tool_args={"key": "value"},
        )
        # 无数据库可用时应优雅降级
        assert result is None

    @pytest.mark.asyncio
    async def test_update_nested_record_error_no_crash(self):
        """AC-TOOL-10: 更新记录失败时不崩溃。"""
        from tools.nested_record_manager import NestedRecordManager

        mgr = NestedRecordManager(db_session=None)
        # 不应抛出异常
        await mgr.update_nested_execution_record(
            record_id="nonexistent_record",
            success=True,
            output={"result": "ok"},
        )

    @pytest.mark.asyncio
    async def test_create_nested_record_with_mock_db(self):
        """AC-TOOL-10: 有 mock db 时正确创建嵌套记录。"""
        from tools.nested_record_manager import NestedRecordManager

        # 构建 mock db 会话
        mock_db = AsyncMock()
        mock_scalar_result = MagicMock()
        mock_parent = MagicMock()
        mock_parent.session_id = "parent_session_id"
        mock_scalar_result.scalar_one_or_none.return_value = mock_parent
        mock_db.execute.return_value = mock_scalar_result

        mgr = NestedRecordManager(db_session=mock_db)

        # Mock 内部方法避免真实 DB 操作
        original_method = mgr._create_nested_record_in_session
        mgr._create_nested_record_in_session = AsyncMock(return_value="nested_record_456")

        result = await mgr.create_nested_execution_record(
            parent_record_id="parent_123",
            session_id="session_123",
            tool_name="nested_tool",
            tool_args={"param": "value"},
            tool_call_id="call_001",
        )

        assert result == "nested_record_456"
        mgr._create_nested_record_in_session.assert_called_once()
        call_kwargs = mgr._create_nested_record_in_session.call_args
        assert call_kwargs.args[1] == "parent_123"

    @pytest.mark.asyncio
    async def test_update_nested_record_with_mock_db(self):
        """AC-TOOL-10: 有 mock db 时正确更新嵌套记录状态。"""
        from tools.nested_record_manager import NestedRecordManager

        mock_db = AsyncMock()
        mgr = NestedRecordManager(db_session=mock_db)

        mgr._update_nested_record_in_session = AsyncMock()

        await mgr.update_nested_execution_record(
            record_id="record_456",
            success=True,
            output={"computed": 42},
            duration_ms=150,
        )

        mgr._update_nested_record_in_session.assert_called_once()


# ════════════════════════════════════════════════════════════════
# F-TOOL-12 ~ F-TOOL-15: 插件错误策略
# ════════════════════════════════════════════════════════════════


class _FailingPlugin(IInputPlugin):
    """测试用：总是失败的插件。"""

    def __init__(
        self,
        name: str = "failing",
        priority: int = 10,
        error_policy: ErrorPolicy = ErrorPolicy.ABORT,
        fail_message: str = "plugin failed",
        fallback_state: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self.error_policy = error_policy
        self._fail_message = fail_message
        self.fallback_state = fallback_state or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        raise RuntimeError(self._fail_message)


class _SuccessPlugin(IInputPlugin):
    """测试用：总是成功的插件，记录执行。"""

    def __init__(self, name: str = "success", priority: int = 20) -> None:
        self._name = name
        self._priority = priority
        self.executed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        self.executed = True
        return PluginResult(state_updates={f"{self._name}_ran": True})


class _RetryablePlugin(IInputPlugin):
    """测试用：前 N 次失败后成功的插件。"""

    def __init__(self, fail_times: int = 2, max_retries: int = 3) -> None:
        self._call_count = 0
        self._fail_times = fail_times
        self.error_policy = ErrorPolicy.RETRY
        self.max_retries = max_retries

    @property
    def name(self) -> str:
        return "retryable"

    @property
    def priority(self) -> int:
        return 10

    async def execute(self, ctx: PluginContext) -> PluginResult:
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise ValueError(f"attempt {self._call_count} failed")
        return PluginResult(state_updates={"retry_succeeded": True})

    @property
    def call_count(self) -> int:
        return self._call_count


class _AlwaysFailRetryPlugin(IInputPlugin):
    """测试用：始终失败的重试插件。"""

    def __init__(self, max_retries: int = 3) -> None:
        self._call_count = 0
        self.error_policy = ErrorPolicy.RETRY
        self.max_retries = max_retries

    @property
    def name(self) -> str:
        return "always_fail_retry"

    @property
    def priority(self) -> int:
        return 10

    async def execute(self, ctx: PluginContext) -> PluginResult:
        self._call_count += 1
        raise RuntimeError(f"always fails (attempt {self._call_count})")

    @property
    def call_count(self) -> int:
        return self._call_count


class TestPluginErrorPolicies:
    """F-TOOL-12 ~ F-TOOL-15: 插件错误策略测试。

    验证 PluginChain 对四种错误策略的处理：
    - ABORT (F-TOOL-12): 遇到错误立即终止后续插件
    - SKIP (F-TOOL-13): 记录警告继续执行
    - FALLBACK (F-TOOL-14): 用兜底结果替代
    - RETRY (F-TOOL-15): 由调用方实现重试循环
    """

    @pytest.mark.asyncio
    async def test_abort_policy_stops_remaining_plugins(self):
        """F-TOOL-12: ABORT 策略下，失败插件终止后续所有插件。"""
        failing = _FailingPlugin(
            name="abort_plugin",
            priority=10,
            error_policy=ErrorPolicy.ABORT,
        )
        success = _SuccessPlugin(name="after_abort", priority=20)

        chain = PluginChain([failing, success])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        # ABORT 应标记 skip_remaining
        assert any(r.error is not None for r in results)
        assert not success.executed, "ABORT 后后续插件不应执行"

    @pytest.mark.asyncio
    async def test_skip_policy_continues_remaining_plugins(self):
        """F-TOOL-13: SKIP 策略下，失败插件记录警告后继续执行后续。"""
        failing = _FailingPlugin(
            name="skip_plugin",
            priority=10,
            error_policy=ErrorPolicy.SKIP,
        )
        success = _SuccessPlugin(name="after_skip", priority=20)

        chain = PluginChain([failing, success])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert success.executed, "SKIP 后后续插件应继续执行"

    @pytest.mark.asyncio
    async def test_fallback_policy_uses_fallback_state(self):
        """F-TOOL-14: FALLBACK 策略使用 fallback_state 作为状态更新。"""
        fallback_data = {"fallback_value": 42, "mode": "degraded"}
        failing = _FailingPlugin(
            name="fallback_plugin",
            priority=10,
            error_policy=ErrorPolicy.FALLBACK,
            fallback_state=fallback_data,
        )
        success = _SuccessPlugin(name="after_fallback", priority=20)

        chain = PluginChain([failing, success])
        ctx = PluginContext(state={})
        await chain.execute(ctx)

        assert success.executed, "FALLBACK 后后续插件应继续执行"
        assert ctx.state.get("fallback_value") == 42
        assert ctx.state.get("mode") == "degraded"

    @pytest.mark.asyncio
    async def test_retry_policy_succeeds_after_failures(self):
        """F-TOOL-15: RETRY 策略重试后成功。"""
        retryable = _RetryablePlugin(fail_times=2, max_retries=3)
        success = _SuccessPlugin(name="after_retry", priority=20)

        chain = PluginChain([retryable, success])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert retryable.call_count == 3, "应重试到第 3 次成功"
        assert success.executed, "RETRY 成功后后续插件应执行"
        assert ctx.state.get("retry_succeeded") is True

    @pytest.mark.asyncio
    async def test_retry_exhausted_aborts_chain(self):
        """F-TOOL-15: RETRY 重试耗尽后终止插件链。"""
        always_fail = _AlwaysFailRetryPlugin(max_retries=2)
        success = _SuccessPlugin(name="after_exhausted", priority=20)

        chain = PluginChain([always_fail, success])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        # max_retries=2 意味着总共调用 1(initial) + 2(retries) = 3 次
        assert always_fail.call_count == 3
        assert not success.executed, "RETRY 耗尽后后续插件不应执行"
        assert any(r.error is not None and r.skip_remaining for r in results)
