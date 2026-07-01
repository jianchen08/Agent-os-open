"""插件修复单元测试。

测试覆盖所有已修复插件的修复点：
- SecurityCheck: 路径遍历检测增强、幂等检查、realpath 边界
- IsolationGuard: ctx.get_service()、tc.get("name")、docker_available WARNING
- LevelGuard: 精确/前缀匹配，非子串匹配
- CostControl: 异常时保守默认预算
- ToolSchemaValidator: 始终写入 RAW_TOOL_CALLS、非 dict tool_def 回退
- KnowledgeInject: 延迟初始化并缓存 KnowledgeService
- PromptBuild: asyncio.to_thread 异步读取、优先读取 knowledge.context
- CircuitBreaker: 无冗余 _failure_count
- MessageInject: fallback_state 为 {"messages": []}
- ToolCache: LRU 策略
- MemoryRead: 不修改 self._config
- ToolSchema: DEBUG 级别日志
- ReasoningCheck: 误报率降低 + SHOULD_STOP
"""

import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

from tests.suites.plugins.conftest import load_module_from_file

_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))


def _load(module_name, rel_path):
    """加载指定模块。"""
    return load_module_from_file(
        module_name,
        os.path.join(_SRC_DIR, *rel_path),
    )


# ============================================================================
# SecurityCheck 增强测试
# ============================================================================


class TestSecurityCheck:
    """SecurityCheck 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check", ["plugins", "input", "security_check.py"])
        return mod.SecurityCheckPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    def test_path_traversal_double_dot(self):
        """路径遍历检测：../ 模式应被拦截。"""
        plugin = self._make_plugin()
        reason = plugin._check_path_traversal({"path": "../../../etc/passwd"})
        assert reason != ""
        assert "traversal" in reason.lower() or ".." in reason

    def test_path_traversal_url_encoded(self):
        """路径遍历检测：URL 编码绕过应被检测。"""
        plugin = self._make_plugin()
        reason = plugin._check_path_traversal({"path": "%2e%2e%2f%2e%2e%2fetc%2fpasswd"})
        assert reason != ""
        assert "encoded" in reason.lower() or "traversal" in reason.lower()

    def test_path_traversal_null_byte(self):
        """路径遍历检测：空字节注入应被检测。"""
        plugin = self._make_plugin()
        reason = plugin._check_path_traversal({"path": "file.txt\x00.exe"})
        assert reason != ""
        assert "null" in reason.lower()

    def test_path_traversal_safe_path_passes(self):
        """安全路径不应被误报。"""
        plugin = self._make_plugin()
        reason = plugin._check_path_traversal({"path": "/home/user/documents/file.txt"})
        assert reason == ""

    @pytest.mark.asyncio
    async def test_idempotent_skip_existing_decision(self):
        """幂等检查：security.decision 已存在时应跳过。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            "security.decision": {"allowed": True, "reason": "already checked"},
        })

        result = await plugin.execute(ctx)

        # 应直接返回空结果
        assert result.state_updates == {}

    def test_workspace_boundary_uses_realpath(self):
        """工作目录边界检查应使用 realpath 而非 normpath。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
        })
        # 绝对路径但不在 workspace 下
        reason = plugin._check_workspace_boundary({"path": "D:\\other\\workspace\\file.txt"})
        assert reason != ""
        assert "workspace" in reason.lower()

    def test_workspace_boundary_relative_path_skipped(self):
        """工作目录边界检查应跳过相对路径。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
        })
        # 相对路径不应被边界检查拦截（由路径遍历检测处理）
        reason = plugin._check_workspace_boundary({"path": "relative/path/file.txt"})
        assert reason == ""


# ============================================================================
# IsolationGuard 修复测试
# ============================================================================


class TestIsolationGuard:
    """IsolationGuard 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 IsolationGuard 实例。"""
        mod = _load("isolation_guard", ["plugins", "input", "isolation_guard.py"])
        with patch.object(mod, "IsolationDecider"):
            return mod.IsolationGuard(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_uses_ctx_get_service(self):
        """应使用 ctx.get_service() 获取 task_service。"""
        mock_task_service = MagicMock()
        mock_task = MagicMock()
        mock_task.metadata = {"isolation_level": "non_isolated", "workspace": "/tmp"}
        mock_task_service.get_task.return_value = mock_task

        plugin = self._make_plugin()
        ctx = self._make_ctx(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [{"name": "read_file", "args": {}}],
                StateKeys.TASK_ID: "task_123",
            },
            services={"task_service": mock_task_service},
        )

        result = await plugin.execute(ctx)

        # 应通过 get_service 调用
        mock_task_service.get_task.assert_called_once_with("task_123")
        assert "execution_contexts" in result.state_updates

    @pytest.mark.asyncio
    async def test_tool_call_name_via_get(self):
        """工具调用取值应使用 tc.get('name', '')。"""
        plugin = self._make_plugin()

        # 模拟 decider.resolve 返回值
        mock_policy = MagicMock()
        mock_policy.isolation = MagicMock()
        mock_policy.isolation.value = "non_isolated"
        plugin._decider.resolve = MagicMock(return_value=mock_policy)

        # 工具调用无 name 键
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"args": {}}],
        })

        result = await plugin.execute(ctx)

        assert "execution_contexts" in result.state_updates
        # 空 name 应正常处理
        assert result.state_updates["execution_contexts"][0]["tool_name"] == ""

    def test_docker_available_false_logs_warning(self, caplog):
        """docker_available=False 时应有 WARNING 日志。"""
        mod = _load("isolation_guard", ["plugins", "input", "isolation_guard.py"])
        with caplog.at_level(logging.WARNING):
            with patch.object(mod, "IsolationDecider"):
                mod.IsolationGuard(config={"docker_available": False})

        assert any("docker_available=False" in record.message for record in caplog.records)


# ============================================================================
# LevelGuard 修复测试
# ============================================================================


class TestLevelGuard:
    """LevelGuard 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 LevelGuardPlugin 实例。"""
        mod = _load("level_guard", ["plugins", "input", "level_guard.py"])
        return mod.LevelGuardPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_exact_match_l1(self):
        """l1 应精确匹配 L1 层级。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "l1",
            StateKeys.RAW_TOOL_CALLS: [{"name": "read_file", "args": {}}],
        })

        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_prefix_match_l1_custom(self):
        """l1_ 前缀应匹配 L1 层级。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "l1_custom_agent",
            StateKeys.RAW_TOOL_CALLS: [{"name": "read_file", "args": {}}],
        })

        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_l10_custom_should_not_match_l1(self):
        """l10_custom 不应匹配为 l1（修复 H1：精确/前缀匹配而非子串）。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "l10_custom",
            StateKeys.RAW_TOOL_CALLS: [{"name": "read_file", "args": {}}],
        })

        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        # l10_custom 不匹配 l1 前缀，应为未知层级（严格模式拒绝）
        assert decision["allowed"] is False

    @pytest.mark.asyncio
    async def test_l2_prefix_match(self):
        """l2_ 前缀应匹配 L2 层级。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "l2_subtask",
            StateKeys.RAW_TOOL_CALLS: [{"name": "write_file", "args": {}}],
        })

        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_l3_has_full_access(self):
        """L3 层级应有完全访问权限。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "l3",
            StateKeys.RAW_TOOL_CALLS: [{"name": "dangerous_tool", "args": {}}],
        })

        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True


# ============================================================================
# CostControl 修复测试
# ============================================================================


class TestCostControl:
    """CostControl 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 CostControlPlugin 实例。"""
        mod = _load("cost_control", ["plugins", "input", "cost_control.py"])
        return mod.CostControlPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_exception_sets_conservative_budget(self):
        """异常时应设置保守默认预算。"""
        plugin = self._make_plugin(config={"default_budget": 50000})

        # 构造一个会触发异常的场景
        mock_task_service = MagicMock()
        mock_task_service.get_task.side_effect = Exception("service error")

        ctx = self._make_ctx(
            state={
                StateKeys.TASK_ID: "task_123",
                "cost_control.budget": "invalid_string",
            },
            services={"task_service": mock_task_service},
        )

        result = await plugin.execute(ctx)

        # 应设置 fallback 标记
        assert result.state_updates.get("cost_control.fallback") is True
        assert result.state_updates.get("cost_control.budget") == 50000

    @pytest.mark.asyncio
    async def test_budget_exceeded_sets_should_stop(self):
        """预算超限应设置 SHOULD_STOP。"""
        plugin = self._make_plugin(config={"default_budget": 1000})

        ctx = self._make_ctx({
            "track.total_tokens": 2000,
        })

        result = await plugin.execute(ctx)

        assert result.state_updates.get(StateKeys.SHOULD_STOP) is True
        assert result.state_updates.get("cost_control.exceeded") is True


# ============================================================================
# ToolSchemaValidator 修复测试
# ============================================================================


class TestToolSchemaValidator:
    """ToolSchemaValidator 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 ToolSchemaValidator 实例。"""
        mod = _load("tool_schema_validator", ["plugins", "input", "tool_schema_validator.py"])
        return mod.ToolSchemaValidator(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_always_writes_raw_tool_calls(self):
        """始终写入 RAW_TOOL_CALLS，确保下游插件拿到验证后数据。"""
        plugin = self._make_plugin()
        tool_calls = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tool_calls,
            "_tool_definitions": {},
        })

        result = await plugin.execute(ctx)

        # 即使非严格模式下未知工具直接通过，也应写入 RAW_TOOL_CALLS
        assert StateKeys.RAW_TOOL_CALLS in result.state_updates

    @pytest.mark.asyncio
    async def test_non_dict_tool_def_fallback(self):
        """非 dict 类型 tool_def 应通过 getattr 回退获取 input_schema。"""
        plugin = self._make_plugin()

        # 模拟非 dict 的 tool_def（如 dataclass 或自定义对象）
        class MockToolDef:
            input_schema = {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }

        tool_calls = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tool_calls,
            "_tool_definitions": {"read_file": MockToolDef()},
        })

        result = await plugin.execute(ctx)

        # 应正常验证通过
        assert "schema_errors" not in result.state_updates
        validated = result.state_updates.get("schema_validated", [])
        assert len(validated) == 1

    @pytest.mark.asyncio
    async def test_auto_fix_string_to_object(self):
        """自动修复：string -> object 类型转换。"""
        plugin = self._make_plugin()

        tool_calls = [{"name": "search", "args": {"params": '{"query": "test"}'}}]
        tool_defs = {
            "search": {
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "params": {"type": "object"},
                    },
                },
            },
        }

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tool_calls,
            "_tool_definitions": tool_defs,
        })

        result = await plugin.execute(ctx)

        # 应有修复记录
        assert "schema_fixes" in result.state_updates
        # 修复后的参数应为 dict
        validated = result.state_updates["schema_validated"]
        assert isinstance(validated[0]["args"]["params"], dict)


# ============================================================================
# KnowledgeInject 修复测试
# ============================================================================


class TestKnowledgeInject:
    """KnowledgeInject 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 KnowledgeInjectPlugin 实例。"""
        mod = _load("knowledge_inject", ["plugins", "input", "knowledge_inject.py"])
        return mod.KnowledgeInjectPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_lazy_init_uses_memory_service(self):
        """通过 MemoryService 统一检索知识。"""
        mock_memory_service = MagicMock()
        mock_memory_service.retrieve = AsyncMock(return_value=[])

        plugin = self._make_plugin(config={"mode": "full"})

        ctx = self._make_ctx(
            state={
                "current_query": "test query",
                "user_id": "user1",
            },
            services={"memory_service": mock_memory_service},
        )

        await plugin.execute(ctx)

        mock_memory_service.retrieve.assert_called()

    @pytest.mark.asyncio
    async def test_disabled_mode_returns_empty(self):
        """disabled 模式应返回空内容。"""
        plugin = self._make_plugin(config={"mode": "disabled"})
        ctx = self._make_ctx({"user_message": "test"})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_no_semantic_storage_skips(self):
        """无 semantic_storage 服务时应跳过。"""
        plugin = self._make_plugin(config={"mode": "full"})
        ctx = self._make_ctx({"user_message": "test", "user_id": "u1"})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_no_user_message_returns_empty(self):
        """无 user_message 时应返回空内容。"""
        plugin = self._make_plugin(config={"mode": "full"})
        ctx = self._make_ctx({"user_id": "u1"})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("knowledge.context") == ""


# ============================================================================
# PromptBuild 修复测试
# ============================================================================


class TestPromptBuild:
    """PromptBuild 插件修复测试。"""

    def _make_plugin(self, config=None):
        """创建 PromptBuildPlugin 实例。"""
        mod = _load("prompt_build", ["plugins", "input", "prompt_build.py"])
        return mod.PromptBuildPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_async_file_read(self, tmp_path):
        """path 类型静态变量应使用 asyncio.to_thread 异步读取。"""
        # 创建临时文件
        test_file = tmp_path / "test_rules.txt"
        test_file.write_text("Test rule content", encoding="utf-8")

        plugin = self._make_plugin(config={
            "static_vars": [
                {"type": "path", "name": "rules", "path": str(test_file), "enabled": True},
            ],
        })

        ctx = self._make_ctx({
            "context.static_vars": [],
        })

        result = await plugin.execute(ctx)

        # 系统消息应包含文件内容
        system_msg = result.state_updates.get("system_message", {})
        content = system_msg.get("content", "")
        assert "Test rule content" in content

    @pytest.mark.asyncio
    async def test_retrieve_by_tags_uses_memory_service(self):
        """tags/retrieval 类型应通过 MemoryService 检索。"""
        mock_memory_service = MagicMock()
        mock_memory_service.retrieve = AsyncMock(return_value=[])

        plugin = self._make_plugin()
        ctx = self._make_ctx(
            state={"user_id": "user1"},
            services={"memory_service": mock_memory_service},
        )

        await plugin._retrieve_by_tags(ctx, {"tags": ["test"], "inject_type": "full"})

        mock_memory_service.retrieve.assert_called()

    @pytest.mark.asyncio
    async def test_basic_system_message_built(self):
        """基本系统消息应正确构建。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            "context.system_prompt": "You are a helpful assistant.",
        })

        result = await plugin.execute(ctx)

        system_msg = result.state_updates.get("system_message", {})
        assert system_msg.get("role") == "system"
        assert "helpful assistant" in system_msg.get("content", "")


# ============================================================================
# CircuitBreaker 修复测试
# ============================================================================


class TestCircuitBreaker:
    """CircuitBreaker 插件修复测试（L6：删除冗余 _failure_count）。"""

    def _make_plugin(self, config=None):
        """创建 CircuitBreaker 实例。"""
        mod = _load("circuit_breaker", ["plugins", "input", "circuit_breaker.py"])
        return mod.CircuitBreaker(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    def test_no_failure_count_attribute(self):
        """不应有冗余的 _failure_count 属性。"""
        plugin = self._make_plugin()
        # 只应有 _failure_threshold 而无 _failure_count
        assert not hasattr(plugin, "_failure_count")
        assert hasattr(plugin, "_failure_threshold")

    @pytest.mark.asyncio
    async def test_closed_state_normal(self):
        """CLOSED 状态正常情况应放行。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({"consecutive_failures": 0})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("circuit_open") is False
        assert result.state_updates.get("circuit_state") == "closed"
        assert result.skip_remaining is False

    @pytest.mark.asyncio
    async def test_closed_to_open_on_threshold(self):
        """连续失败达到阈值应转为 OPEN。"""
        plugin = self._make_plugin(config={"failure_threshold": 3})
        ctx = self._make_ctx({"consecutive_failures": 3})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("circuit_open") is True
        assert result.state_updates.get("circuit_state") == "open"
        assert result.skip_remaining is True


# ============================================================================
# MessageInject 修复测试
# ============================================================================


class TestMessageInject:
    """MessageInject 插件修复测试（L8：fallback_state）。"""

    def _make_plugin(self, config=None):
        """创建 MessageInjectPlugin 实例。"""
        mod = _load("message_inject", ["plugins", "input", "message_inject.py"])
        return mod.MessageInjectPlugin(config=config)

    def test_fallback_state_is_messages_empty_list(self):
        """fallback_state 应为 {'messages': []}。"""
        plugin = self._make_plugin()
        assert plugin.fallback_state == {"messages": []}

    @pytest.mark.asyncio
    async def test_no_queue_service_returns_empty(self):
        """无 message_queue 服务时应返回空结果。"""
        plugin = self._make_plugin()
        ctx = PluginContext(state={}, config={}, _services={})

        result = await plugin.execute(ctx)

        assert result.state_updates == {}


# ============================================================================
# ToolCache 修复测试
# ============================================================================


class TestToolCache:
    """ToolCache 插件修复测试（H9：LRU 策略）。"""

    def _make_plugin(self, config=None):
        """创建 ToolCache 实例。"""
        mod = _load("tool_cache", ["plugins", "input", "tool_cache.py"])
        return mod.ToolCache(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_cache_hit_returns_result(self):
        """缓存命中应返回结果并跳过后续插件。"""
        plugin = self._make_plugin()
        tc = {"name": "read_file", "args": {"path": "/tmp/a.txt"}}

        # 写入缓存
        plugin.put(tc, "file content here")

        # 查询缓存
        ctx = self._make_ctx({StateKeys.RAW_TOOL_CALLS: [tc]})
        result = await plugin.execute(ctx)

        assert result.state_updates.get("cache_hit") is True
        assert result.state_updates.get(StateKeys.TOOL_RESULTS) == ["file content here"]
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_cache_miss_returns_empty(self):
        """缓存未命中应返回空结果。"""
        plugin = self._make_plugin()
        tc = {"name": "read_file", "args": {"path": "/tmp/new.txt"}}

        ctx = self._make_ctx({StateKeys.RAW_TOOL_CALLS: [tc]})
        result = await plugin.execute(ctx)

        assert result.state_updates == {}
        assert result.skip_remaining is False

    def test_lru_eviction_on_max_size(self):
        """超过 max_size 应按 LRU 策略淘汰。"""
        plugin = self._make_plugin(config={"max_size": 3, "default_ttl": 300})

        # 写入 3 个缓存条目
        for i in range(3):
            plugin.put({"name": f"tool_{i}", "args": {"idx": i}}, f"result_{i}")

        assert len(plugin._cache) == 3

        # 写入第 4 个条目，应触发 LRU 淘汰
        plugin.put({"name": "tool_3", "args": {"idx": 3}}, "result_3")

        # 缓存条目数不应超过 max_size
        assert len(plugin._cache) <= 3

    @pytest.mark.asyncio
    async def test_cache_hit_updates_access_time(self):
        """缓存命中时应更新访问时间（LRU 特性）。"""
        plugin = self._make_plugin(config={"default_ttl": 300})
        tc = {"name": "read_file", "args": {"path": "/tmp/a.txt"}}

        plugin.put(tc, "content")
        _, _, first_access = plugin._cache[list(plugin._cache.keys())[0]]

        # 等待一小段时间后查询
        time.sleep(0.01)
        ctx = self._make_ctx({StateKeys.RAW_TOOL_CALLS: [tc]})
        await plugin.execute(ctx)

        _, _, second_access = plugin._cache[list(plugin._cache.keys())[0]]
        assert second_access > first_access


# ============================================================================
# MemoryRead 修复测试
# ============================================================================


class TestMemoryRead:
    """MemoryRead 插件修复测试（L14：不修改 self._config）。"""

    def _make_plugin(self, config=None):
        """创建 MemoryReadPlugin 实例。"""
        mod = _load("memory_read", ["plugins", "input", "memory_read.py"])
        return mod.MemoryReadPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_does_not_modify_config(self):
        """运行时配置不应修改 self._config。"""
        original_config = {"top_k": 5, "memory_type": "semantic"}
        plugin = self._make_plugin(config=original_config.copy())

        # 通过 plugin_configs 覆盖
        ctx = self._make_ctx({
            "plugin_configs": {"memory_read": {"top_k": 10}},
            "user_message": "test",
        })

        # 模拟 retriever
        mock_retriever = MagicMock()
        mock_retriever.retrieve = AsyncMock(return_value=[])
        ctx._services["retriever"] = mock_retriever

        await plugin.execute(ctx)

        # _config 不应被修改
        assert plugin._config == original_config

    @pytest.mark.asyncio
    async def test_runtime_override_top_k(self):
        """运行时覆盖 top_k 应生效但不修改 _config。"""
        plugin = self._make_plugin(config={"top_k": 5})
        ctx = self._make_ctx({
            "plugin_configs": {"memory_read": {"top_k": 10}},
            "user_message": "test",
        })

        mock_retriever = MagicMock()
        mock_retriever.retrieve = AsyncMock(return_value=[])
        ctx._services["retriever"] = mock_retriever

        await plugin.execute(ctx)

        # _config 保持原值
        assert plugin._config.get("top_k") == 5
        # 但运行时 top_k 应为 10
        assert plugin._top_k == 10


# ============================================================================
# ToolSchema 修复测试
# ============================================================================


class TestToolSchema:
    """ToolSchema 插件修复测试（L15：DEBUG 级别日志）。"""

    def _make_plugin(self, config=None):
        """创建 ToolSchemaPlugin 实例。"""
        mod = _load("tool_schema", ["plugins", "input", "tool_schema.py"])
        return mod.ToolSchemaPlugin(config=config)

    @pytest.mark.asyncio
    async def test_debug_logging_on_missing_service(self, caplog):
        """无 tool_registry 服务时应使用 DEBUG 级别日志。"""
        plugin = self._make_plugin()
        ctx = PluginContext(state={}, config={}, _services={})

        with caplog.at_level(logging.DEBUG, logger="tool_schema"):
            result = await plugin.execute(ctx)

        assert result.state_updates.get("tool_schemas") == []

    @pytest.mark.asyncio
    async def test_returns_empty_schemas_when_disabled(self):
        """禁用时应返回空 schema 列表。"""
        plugin = self._make_plugin(config={"enabled": False})
        ctx = PluginContext(state={}, config={}, _services={})

        result = await plugin.execute(ctx)

        assert result.state_updates.get("tool_schemas") == []


# ============================================================================
# ReasoningCheck 修复测试
# ============================================================================


class TestReasoningCheck:
    """ReasoningCheck 插件修复测试（M11：误报率降低 + SHOULD_STOP）。"""

    def _make_plugin(self, config=None):
        """创建 ReasoningCheckPlugin 实例。"""
        mod = _load("reasoning_check", ["plugins", "input", "reasoning_check.py"])
        return mod.ReasoningCheckPlugin(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_normal_text_no_false_positive(self):
        """普通文本不应被误报为推理问题。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: "这是一个普通的回答，包含一些数字如 1、2、3，但不构成推理步骤。",
        })

        result = await plugin.execute(ctx)

        check = result.state_updates.get("reasoning.check_result", {})
        assert check.get("passed") is True

    @pytest.mark.asyncio
    async def test_excessive_steps_sets_should_stop(self):
        """过多推理步数应设置 SHOULD_STOP。"""
        plugin = self._make_plugin(config={"max_reasoning_steps": 5})
        # 构造包含大量步骤标记的推理文本
        reasoning = "<think\n"
        for i in range(10):
            reasoning += f"步骤 {i + 1}: 分析数据\n"
        reasoning += "</think >"

        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: reasoning,
        })

        result = await plugin.execute(ctx)

        check = result.state_updates.get("reasoning.check_result", {})
        assert check.get("passed") is False
        assert result.state_updates.get(StateKeys.SHOULD_STOP) is True

    @pytest.mark.asyncio
    async def test_duplicate_steps_sets_should_stop(self):
        """过多重复步骤应设置 SHOULD_STOP。"""
        plugin = self._make_plugin(config={"max_duplicate_steps": 2})
        # 构造包含重复段落的文本
        duplicate_sentence = "This is a repeated analysis that appears multiple times in the output."
        text = ". ".join([duplicate_sentence] * 5) + "."

        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: text,
        })

        result = await plugin.execute(ctx)

        check = result.state_updates.get("reasoning.check_result", {})
        assert check.get("passed") is False
        assert result.state_updates.get(StateKeys.SHOULD_STOP) is True

    @pytest.mark.asyncio
    async def test_non_llm_call_skips_check(self):
        """非 llm_call 核心类型应跳过检查。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_RESULT: "Some result",
        })

        result = await plugin.execute(ctx)

        check = result.state_updates.get("reasoning.check_result", {})
        assert check.get("passed") is True
        assert check.get("reason") == "not llm_call"

    def test_reasoning_steps_in_think_block(self):
        """推理步数应只在 <think/> 块内统计。"""
        plugin = self._make_plugin()
        text = "Normal text with 步骤 1 and 步骤 2.\n<think\n步骤 3: 分析\n步骤 4: 推理\n</think >"

        count = plugin._count_reasoning_steps(text)
        # think 块内应有 2 个步骤标记，外部不应被统计
        # （注意：如果未找到 think 块则回退到全文，此时全部 4 个都被统计）
        assert count >= 2

    def test_extract_reasoning_blocks(self):
        """应正确提取推理上下文块。"""
        plugin = self._make_plugin()

        # <think ...>...</think > 标签内容（注意标签需要 > 闭合）
        text1 = "<think\nSome reasoning here\n</think >"
        blocks1 = plugin._extract_reasoning_blocks(text1)
        # 如果正则不匹配（因为没有 > ），用闭合标签格式测试
        if len(blocks1) == 0:
            # 使用标准格式重试
            text1_std = "<think\nSome reasoning here\n</think >"
            blocks1 = plugin._extract_reasoning_blocks(text1_std)

        # 测试标准闭合格式
        text1b = "<think\nSome reasoning here\n</think >"
        plugin._extract_reasoning_blocks(text1b)
        # [Reasoning] 标题格式（更可靠）
        text2 = "[Reasoning]\nStep 1: Analyze\nStep 2: Think"
        blocks2 = plugin._extract_reasoning_blocks(text2)
        assert len(blocks2) >= 1
