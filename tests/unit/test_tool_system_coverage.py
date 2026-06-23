"""
工具系统模块补充测试。

覆盖现有 test_tool_system_core.py 未覆盖的区域：
- ToolExecutor: handler/runnable 执行、输入验证、输出截断、批量/管道执行
- Tool: 注入参数过滤、层级限制、格式转换（YAML/MCP/checksum）
- ToolRegistry: handler 绑定、清理、MCP/YAML 格式输出
- ToolCache: 缓存判断、敏感信息检测、缓存键生成
- InputNormalizer: 类型转换、task_submit 输入修复
- FormatManager: 序列化（JSON/YAML/XML）
- Bug 修复验证: exceptions 导入路径、_get_llm_schema 死代码
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.results import ToolExecutionResult
from tools.executor import ExecutionContext, ToolExecutor
from tools.format_manager import FormatManager, ToolFormat, get_format_manager
from tools.input_normalizer import (
    fix_object_field,
    fix_task_submit_inputs,
    normalize_input_types,
    try_parse_json_string,
)
from tools.registry import ToolRegistry
from tools.tool_cache import ToolCache, ToolCacheConfig, _contains_sensitive_info
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_success_result,
)


# ════════════════════════════════════════════════════════════════
# ToolExecutor Handler 执行测试
# ════════════════════════════════════════════════════════════════


class TestToolExecutorHandler:
    """ToolExecutor handler 模式执行测试。"""

    @pytest.fixture
    def registry_with_tool(self) -> ToolRegistry:
        """创建注册了 echo 工具的注册表（含 runnable）。"""
        registry = ToolRegistry(lazy_load=False)

        async def echo_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args.get("message", "")}

        tool = Tool(
            name="echo",
            description="回显工具",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "消息"},
                },
                "required": ["message"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, echo_handler)
        return registry

    @pytest.mark.asyncio
    async def test_execute_handler_success(self, registry_with_tool):
        """测试: 默认 runnable 模式成功执行返回正确结果。"""
        executor = ToolExecutor(registry_with_tool)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute("echo", {"message": "hello"}, ctx)

        assert result.success
        assert result.output["echo"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self, registry_with_tool):
        """测试: 执行不存在的工具抛出 ToolNotFoundError。"""
        from core.exceptions import ToolNotFoundError

        executor = ToolExecutor(registry_with_tool)
        ctx = ExecutionContext(session_id="test-session")

        with pytest.raises(ToolNotFoundError):
            await executor.execute("nonexistent_tool_xyz", {}, ctx)

    @pytest.mark.asyncio
    async def test_execute_no_handler_raises(self, registry_with_tool):
        """测试: 工具已注册但无 handler/runnable 时抛出 ToolExecutionError。"""
        from core.exceptions import ToolExecutionError

        registry = registry_with_tool
        registry.register(Tool(
            name="no_handler_tool",
            description="无处理函数的工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))

        executor = ToolExecutor(registry)
        executor.set_runnable_first(False)
        ctx = ExecutionContext(session_id="test-session")

        with pytest.raises(ToolExecutionError):
            await executor.execute("no_handler_tool", {}, ctx)

    @pytest.mark.asyncio
    async def test_execute_handler_timeout(self):
        """AC-TOOL-05: 工具超时返回 timeout 信息。"""
        registry = ToolRegistry(lazy_load=False)

        async def slow_handler(args: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"data": "unreachable"}

        tool = Tool(
            name="slow_tool",
            description="慢工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, slow_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        # Runnable 模式下超时返回失败结果（不抛异常）
        result = await executor.execute("slow_tool", {}, ctx, timeout=0.1)

        assert not result.success
        assert "超时" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_handler_exception(self):
        """AC-TOOL-06: 工具异常返回 error 信息。"""
        registry = ToolRegistry(lazy_load=False)

        async def failing_handler(args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("工具内部错误")

        tool = Tool(
            name="failing_tool",
            description="失败工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, failing_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        # Runnable 模式下异常被捕获并返回失败结果
        result = await executor.execute("failing_tool", {}, ctx)

        assert not result.success
        assert "工具内部错误" in (result.error or "")


# ════════════════════════════════════════════════════════════════
# ToolExecutor Runnable 执行测试
# ════════════════════════════════════════════════════════════════


class TestToolExecutorRunnable:
    """ToolExecutor runnable 模式执行测试。"""

    @pytest.mark.asyncio
    async def test_execute_runnable_success(self):
        """测试: runnable 模式成功执行。"""
        registry = ToolRegistry(lazy_load=False)

        async def add_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"sum": args.get("a", 0) + args.get("b", 0)}

        tool = Tool(
            name="add",
            description="加法工具",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "数字A"},
                    "b": {"type": "integer", "description": "数字B"},
                },
                "required": ["a", "b"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, add_handler)

        executor = ToolExecutor(registry)
        executor.set_runnable_first(True)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute("add", {"a": 3, "b": 5}, ctx)

        assert result.success
        assert result.output["sum"] == 8

    @pytest.mark.asyncio
    async def test_execute_runnable_timeout(self):
        """测试: runnable 模式超时返回失败结果。"""
        registry = ToolRegistry(lazy_load=False)

        async def slow_handler(args: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {}

        tool = Tool(
            name="slow_runnable",
            description="慢工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, slow_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute("slow_runnable", {}, ctx, timeout=0.1)

        assert not result.success
        assert "超时" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_runnable_returns_execution_result(self):
        """测试: runnable 返回 ToolExecutionResult 时直接使用。"""
        registry = ToolRegistry(lazy_load=False)

        async def result_handler(args: dict[str, Any]) -> ToolExecutionResult:
            return create_success_result(data={"computed": 42})

        tool = Tool(
            name="result_tool",
            description="返回执行结果工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, result_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute("result_tool", {}, ctx)

        assert result.success
        assert result.output["computed"] == 42


# ════════════════════════════════════════════════════════════════
# ToolExecutor 输入验证
# ════════════════════════════════════════════════════════════════


class TestToolExecutorValidation:
    """ToolExecutor 输入验证与类型规范化测试。"""

    @pytest.mark.asyncio
    async def test_input_type_normalization_boolean(self):
        """测试: LLM 返回字符串布尔值被自动转换。"""
        registry = ToolRegistry(lazy_load=False)
        captured: dict[str, Any] = {}

        async def capture_handler(args: dict[str, Any]) -> dict[str, Any]:
            captured.update(args)
            return {"ok": True}

        tool = Tool(
            name="bool_tool",
            description="布尔参数工具",
            input_schema={
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                },
                "required": ["enabled"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, capture_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        await executor.execute("bool_tool", {"enabled": "true"}, ctx)

        assert captured["enabled"] is True

    @pytest.mark.asyncio
    async def test_input_type_normalization_integer(self):
        """测试: LLM 返回字符串整数被自动转换。"""
        registry = ToolRegistry(lazy_load=False)
        captured: dict[str, Any] = {}

        async def capture_handler(args: dict[str, Any]) -> dict[str, Any]:
            captured.update(args)
            return {"ok": True}

        tool = Tool(
            name="int_tool",
            description="整数参数工具",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, capture_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        await executor.execute("int_tool", {"count": "42"}, ctx)

        assert captured["count"] == 42

    @pytest.mark.asyncio
    async def test_schema_defaults_filling(self):
        """测试: 未传参数但有默认值时自动填充。"""
        registry = ToolRegistry(lazy_load=False)
        captured: dict[str, Any] = {}

        async def capture_handler(args: dict[str, Any]) -> dict[str, Any]:
            captured.update(args)
            return {"ok": True}

        tool = Tool(
            name="default_tool",
            description="默认值工具",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["path"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, capture_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        await executor.execute("default_tool", {"path": "/tmp"}, ctx)

        assert captured["path"] == "/tmp"
        assert captured["limit"] == 10

    @pytest.mark.asyncio
    async def test_input_validation_failure(self):
        """测试: 输入验证失败抛出 ToolValidationError。"""
        from core.exceptions import ToolValidationError

        registry = ToolRegistry(lazy_load=False)

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        tool = Tool(
            name="required_tool",
            description="必填参数工具",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        with pytest.raises(ToolValidationError):
            await executor.execute("required_tool", {}, ctx)


# ════════════════════════════════════════════════════════════════
# ToolExecutor 输出截断
# ════════════════════════════════════════════════════════════════


class TestToolExecutorOutputTruncation:
    """ToolExecutor 输出截断测试。"""

    def test_truncate_long_string_output(self):
        """测试: 超长字符串输出被截断。"""
        long_text = "x" * (ToolExecutor.MAX_TOOL_OUTPUT_LENGTH + 1000)

        registry = ToolRegistry(lazy_load=False)
        executor = ToolExecutor(registry)

        truncated = executor._truncate_output(long_text)

        assert len(truncated) <= ToolExecutor.MAX_TOOL_OUTPUT_LENGTH + 200
        assert "截断" in truncated

    def test_normal_output_not_truncated(self):
        """测试: 正常长度输出不被截断。"""
        registry = ToolRegistry(lazy_load=False)
        executor = ToolExecutor(registry)

        result = executor._truncate_output("short output")

        assert result == "short output"

    def test_dict_output_not_truncated(self):
        """测试: 字典输出不会被截断（截断仅作用于顶层字符串）。"""
        registry = ToolRegistry(lazy_load=False)
        executor = ToolExecutor(registry)

        data = {"key": "x" * (ToolExecutor.MAX_TOOL_OUTPUT_LENGTH + 100)}
        result = executor._truncate_output(data)

        assert isinstance(result, dict)
        # 字典内的长字符串不会被截断
        assert len(result["key"]) > ToolExecutor.MAX_TOOL_OUTPUT_LENGTH


# ════════════════════════════════════════════════════════════════
# ToolExecutor 批量与管道执行
# ════════════════════════════════════════════════════════════════


class TestToolExecutorBatchPipeline:
    """ToolExecutor 批量执行与管道执行测试。"""

    @pytest.mark.asyncio
    async def test_batch_execute(self):
        """测试: 批量执行多个工具调用。"""
        registry = ToolRegistry(lazy_load=False)

        async def echo_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args.get("msg", "")}

        tool = Tool(
            name="batch_echo",
            description="批量回显",
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

        calls = [
            {"tool_name": "batch_echo", "inputs": {"msg": "first"}},
            {"tool_name": "batch_echo", "inputs": {"msg": "second"}},
            {"tool_name": "batch_echo", "inputs": {"msg": "third"}},
        ]

        results = await executor.batch_execute(calls, ctx)

        assert len(results) == 3
        assert results[0].output["echo"] == "first"
        assert results[1].output["echo"] == "second"
        assert results[2].output["echo"] == "third"

    @pytest.mark.asyncio
    async def test_execute_pipeline_success(self):
        """测试: 管道顺序执行，前一个输出作为后一个输入。"""
        registry = ToolRegistry(lazy_load=False)

        async def step1(args: dict[str, Any]) -> dict[str, Any]:
            return {"value": args.get("value", 0) + 1}

        async def step2(args: dict[str, Any]) -> dict[str, Any]:
            return {"value": args.get("value", 0) * 2}

        tool1 = Tool(
            name="pipe_add",
            description="加1",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            source=ToolSource.BUILTIN,
        )
        tool2 = Tool(
            name="pipe_double",
            description="乘2",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool1, step1)
        registry.register_with_handler(tool2, step2)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute_pipeline(
            ["pipe_add", "pipe_double"], {"value": 5}, ctx
        )

        assert result.success
        assert result.output["value"] == 12  # (5+1)*2

    @pytest.mark.asyncio
    async def test_execute_pipeline_failure_stops(self):
        """测试: 管道中某步失败则停止并返回失败结果。"""
        registry = ToolRegistry(lazy_load=False)

        async def ok_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"value": 1}

        async def fail_handler(args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("管道步骤失败")

        tool1 = Tool(
            name="pipe_ok",
            description="成功步骤",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        tool2 = Tool(
            name="pipe_fail",
            description="失败步骤",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool1, ok_handler)
        registry.register_with_handler(tool2, fail_handler)

        executor = ToolExecutor(registry)
        ctx = ExecutionContext(session_id="test-session")

        result = await executor.execute_pipeline(["pipe_ok", "pipe_fail"], {}, ctx)

        # 管道中第二步失败，返回失败结果
        assert not result.success
        assert "管道步骤失败" in (result.error or "")


# ════════════════════════════════════════════════════════════════
# Tool 注入参数与层级限制
# ════════════════════════════════════════════════════════════════


class TestToolInjectedParams:
    """Tool 注入参数过滤测试。"""

    def test_injected_params_removed_from_llm_schema(self):
        """测试: 注入参数从 LLM schema 中移除。"""
        tool = Tool(
            name="tool_with_injection",
            description="带注入参数的工具",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询"},
                    "session_id": {"type": "string"},
                    "user_id": {"type": "string"},
                },
                "required": ["query", "session_id"],
            },
            injected_params=["session_id", "user_id"],
            source=ToolSource.BUILTIN,
        )

        llm_schema = tool.get_tool_call_schema()

        assert "session_id" not in llm_schema["properties"]
        assert "user_id" not in llm_schema["properties"]
        assert "query" in llm_schema["properties"]
        assert "session_id" not in llm_schema.get("required", [])

    def test_injected_params_empty_required_after_removal(self):
        """测试: 注入参数移除后 required 为空时删除该字段。"""
        tool = Tool(
            name="only_injected",
            description="只有注入参数",
            input_schema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            injected_params=["session_id"],
            source=ToolSource.BUILTIN,
        )

        llm_schema = tool.get_tool_call_schema()

        assert "required" not in llm_schema or "session_id" not in llm_schema.get("required", [])

    def test_no_injected_params_unchanged(self):
        """测试: 无注入参数时 schema 不变。"""
        tool = Tool(
            name="plain_tool",
            description="普通工具",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=ToolSource.BUILTIN,
        )

        llm_schema = tool.get_tool_call_schema()

        assert "name" in llm_schema["properties"]
        assert "name" in llm_schema.get("required", [])


class TestToolLevelRestrictions:
    """Tool 参数层级限制测试。"""

    def test_max_visible_level_hides_param(self):
        """测试: max_visible_level 对超过该层级的 Agent 隐藏参数。"""
        tool = Tool(
            name="level_restricted",
            description="层级限制工具",
            input_schema={
                "type": "object",
                "properties": {
                    "normal_param": {"type": "string"},
                    "admin_only": {"type": "string"},
                },
                "required": ["normal_param"],
            },
            param_level_restrictions={
                "admin_only": {"max_visible_level": 1},
            },
            source=ToolSource.BUILTIN,
        )

        # L1 (level=1) 能看到 admin_only
        schema_l1 = tool.to_llm_format(agent_level=1)
        assert "admin_only" in schema_l1["function"]["parameters"]["properties"]

        # L3 (level=3) 看不到 admin_only
        schema_l3 = tool.to_llm_format(agent_level=3)
        assert "admin_only" not in schema_l3["function"]["parameters"]["properties"]

    def test_enum_restrictions_filter_values(self):
        """测试: enum_restrictions 按层级过滤枚举值。"""
        tool = Tool(
            name="enum_restricted",
            description="枚举限制工具",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "delete", "admin"],
                    },
                },
                "required": ["action"],
            },
            param_level_restrictions={
                "action": {
                    "enum_restrictions": {
                        "read": 0,
                        "write": 2,
                        "delete": 1,
                        "admin": 1,
                    }
                },
            },
            source=ToolSource.BUILTIN,
        )

        # L1 (level=1): 可以看到所有 max_level >= 1 的值
        schema_l1 = tool.to_llm_format(agent_level=1)
        allowed = schema_l1["function"]["parameters"]["properties"]["action"]["enum"]
        assert "read" in allowed
        assert "write" in allowed
        assert "delete" in allowed
        assert "admin" in allowed

        # L3 (level=3): 只能看到 max_level=0 的值 (所有层级可见)
        schema_l3 = tool.to_llm_format(agent_level=3)
        allowed_l3 = schema_l3["function"]["parameters"]["properties"]["action"]["enum"]
        assert "read" in allowed_l3
        assert "write" not in allowed_l3
        assert "delete" not in allowed_l3
        assert "admin" not in allowed_l3

    def test_no_level_filter_when_agent_level_none(self):
        """测试: agent_level=None 时不做层级过滤。"""
        tool = Tool(
            name="no_filter_tool",
            description="不过滤工具",
            input_schema={
                "type": "object",
                "properties": {
                    "param": {"type": "string", "enum": ["a", "b"]},
                },
            },
            param_level_restrictions={
                "param": {"enum_restrictions": {"a": 1, "b": 0}},
            },
            source=ToolSource.BUILTIN,
        )

        schema = tool.to_llm_format(agent_level=None)
        enum_values = schema["function"]["parameters"]["properties"]["param"]["enum"]
        assert "a" in enum_values
        assert "b" in enum_values


# ════════════════════════════════════════════════════════════════
# Tool 格式转换
# ════════════════════════════════════════════════════════════════


class TestToolFormats:
    """Tool 多格式转换测试。"""

    @pytest.fixture
    def sample_tool(self) -> Tool:
        return Tool(
            name="format_test",
            description="格式测试工具",
            when_to_use=["需要格式化时"],
            caveats=["注意边界"],
            input_schema={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.FILE,
        )

    def test_build_full_description(self, sample_tool):
        """测试: build_full_description 包含增强信息。"""
        desc = sample_tool.build_full_description()

        assert "格式测试工具" in desc
        assert "适用场景" in desc
        assert "注意事项" in desc

    def test_to_llm_yaml_format(self, sample_tool):
        """测试: to_llm_yaml_format 输出有效 YAML。"""
        import yaml

        yaml_str = sample_tool.to_llm_yaml_format()
        parsed = yaml.safe_load(yaml_str)

        assert parsed["name"] == "format_test"
        assert "props" in parsed["params"]

    def test_to_mcp_format(self, sample_tool):
        """测试: to_mcp_format 输出正确的 MCP 结构。"""
        mcp = sample_tool.to_mcp_format()

        assert mcp["name"] == "format_test"
        assert "description" in mcp
        assert "inputSchema" in mcp
        assert mcp["inputSchema"]["type"] == "object"

    def test_compute_checksum(self, sample_tool):
        """测试: compute_checksum 返回确定性校验和。"""
        checksum1 = sample_tool.compute_checksum()
        checksum2 = sample_tool.compute_checksum()

        assert checksum1 == checksum2
        assert len(checksum1) == 16

    def test_checksum_changes_on_modification(self):
        """测试: 工具定义变更时校验和改变。"""
        tool1 = Tool(
            name="checksum_tool",
            description="描述A",
            input_schema={"type": "object"},
            source=ToolSource.BUILTIN,
        )
        tool2 = Tool(
            name="checksum_tool",
            description="描述B",
            input_schema={"type": "object"},
            source=ToolSource.BUILTIN,
        )

        assert tool1.compute_checksum() != tool2.compute_checksum()

    def test_model_dump_yaml(self, sample_tool):
        """测试: model_dump_yaml 输出 YAML 友好字典。"""
        dumped = sample_tool.model_dump_yaml()

        assert dumped["name"] == "format_test"
        assert dumped["description"] == "格式测试工具"
        assert dumped["source"] == ToolSource.BUILTIN


# ════════════════════════════════════════════════════════════════
# ToolRegistry 扩展方法
# ════════════════════════════════════════════════════════════════


class TestToolRegistryExtended:
    """ToolRegistry 扩展方法测试。"""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        return ToolRegistry(lazy_load=False)

    def test_register_with_handler(self, registry):
        """测试: register_with_handler 同时注册工具和 handler。"""
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        tool = Tool(
            name="handler_tool",
            description="带handler工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        name = registry.register_with_handler(tool, handler)

        assert name == "handler_tool"
        assert registry.has_handler("handler_tool")
        assert registry.has_runnable("handler_tool")

    def test_bind_handler(self, registry):
        """测试: bind_handler 为已注册工具绑定处理函数。"""
        tool = Tool(
            name="bind_tool",
            description="待绑定工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register(tool)
        assert not registry.has_handler("bind_tool")

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        registry.bind_handler("bind_tool", handler)

        assert registry.has_handler("bind_tool")
        assert registry.has_runnable("bind_tool")

    def test_bind_handler_nonexistent_raises(self, registry):
        """测试: 为不存在的工具绑定 handler 抛出异常。"""
        from core.exceptions import ToolNotFoundError

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {}

        with pytest.raises(ToolNotFoundError):
            registry.bind_handler("nonexistent", handler)

    def test_get_handler(self, registry):
        """测试: get_handler 返回绑定的处理函数。"""
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"data": "handled"}

        tool = Tool(
            name="get_handler_tool",
            description="获取handler工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, handler)

        retrieved = registry.get_handler("get_handler_tool")
        assert retrieved is not None
        assert callable(retrieved)

    def test_get_handler_nonexistent(self, registry):
        """测试: 获取不存在工具的 handler 返回 None。"""
        assert registry.get_handler("nonexistent_handler") is None

    def test_get_optional_nonexistent(self, registry):
        """测试: get_optional 返回 None 而非抛出异常。"""
        assert registry.get_optional("nonexistent_optional") is None

    def test_get_optional_existing(self, registry):
        """测试: get_optional 返回已注册工具。"""
        tool = Tool(
            name="optional_tool",
            description="可选工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register(tool)

        result = registry.get_optional("optional_tool")
        assert result is not None
        assert result.name == "optional_tool"

    def test_unregister_cleans_handlers(self, registry):
        """测试: 注销工具时清理 handler/runnable/enricher。"""
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {}

        tool = Tool(
            name="cleanup_tool",
            description="清理工具",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, handler)
        registry.register_schema_enricher("cleanup_tool", lambda t, s: t)
        registry.mark_dynamic("cleanup_tool")

        assert registry.has("cleanup_tool")
        assert registry.has_handler("cleanup_tool")

        registry.unregister("cleanup_tool")

        assert not registry.has("cleanup_tool")
        assert not registry.has_handler("cleanup_tool")
        assert not registry.has_runnable("cleanup_tool")
        assert "cleanup_tool" not in registry.get_dynamic_tool_names()

    def test_list_by_source(self, registry):
        """测试: 按来源筛选工具。"""
        registry.register(Tool(
            name="builtin_tool",
            description="内置",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))
        registry.register(Tool(
            name="code_tool",
            description="代码",
            input_schema={},
            source=ToolSource.CODE,
        ))

        builtin_tools = registry.list_by_source(ToolSource.BUILTIN)
        assert len(builtin_tools) == 1
        assert builtin_tools[0].name == "builtin_tool"

    def test_get_tools_for_llm_yaml(self, registry):
        """测试: get_tools_for_llm_yaml 输出有效 YAML。"""
        import yaml

        registry.register(Tool(
            name="yaml_tool",
            description="YAML工具",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
            source=ToolSource.BUILTIN,
        ))

        yaml_str = registry.get_tools_for_llm_yaml()
        parsed = yaml.safe_load(yaml_str)

        assert "tools" in parsed
        assert len(parsed["tools"]) == 1
        assert parsed["tools"][0]["name"] == "yaml_tool"

    def test_get_tools_for_mcp(self, registry):
        """测试: get_tools_for_mcp 输出 MCP 格式。"""
        registry.register(Tool(
            name="mcp_tool",
            description="MCP工具",
            input_schema={"type": "object"},
            source=ToolSource.BUILTIN,
        ))

        mcp_tools = registry.get_tools_for_mcp()

        assert len(mcp_tools) == 1
        assert mcp_tools[0]["name"] == "mcp_tool"
        assert "inputSchema" in mcp_tools[0]

    def test_clear(self, registry):
        """测试: clear 清空所有数据和附加结构。"""
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return {}

        tool = Tool(
            name="clear_tool",
            description="清理测试",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register_with_handler(tool, handler)
        registry.mark_dynamic("clear_tool")

        registry.clear()

        assert registry.count() == 0
        assert not registry.has_handler("clear_tool")
        assert len(registry.get_dynamic_tool_names()) == 0


# ════════════════════════════════════════════════════════════════
# ToolCache 逻辑测试
# ════════════════════════════════════════════════════════════════


class TestToolCacheLogic:
    """ToolCache 缓存判断与敏感信息检测测试。"""

    def test_should_cache_excludes_task_submit(self):
        """测试: task_submit 永不缓存。"""
        config = ToolCacheConfig(
            enabled=True,
            tools={"task_submit": {"enabled": True}},
        )
        cache = ToolCache(config)

        assert not cache.should_cache("task_submit", {})

    def test_should_cache_excludes_sensitive_info(self):
        """测试: 包含敏感信息的输入不缓存。"""
        config = ToolCacheConfig(
            enabled=True,
            tools={"safe_tool": {"enabled": True}},
        )
        cache = ToolCache(config)

        assert not cache.should_cache("safe_tool", {"password": "secret123"})

    def test_should_cache_normal_input(self):
        """测试: 正常输入允许缓存。"""
        config = ToolCacheConfig(
            enabled=True,
            tools={"normal_tool": {"enabled": True}},
        )
        cache = ToolCache(config)

        assert cache.should_cache("normal_tool", {"query": "hello"})

    def test_should_cache_disabled_config(self):
        """测试: 配置禁用时不缓存。"""
        config = ToolCacheConfig(enabled=False)
        cache = ToolCache(config)

        assert not cache.should_cache("any_tool", {"data": "test"})

    def test_generate_cache_key_deterministic(self):
        """测试: 相同输入生成相同缓存键。"""
        config = ToolCacheConfig()
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool_a", {"param": "value"})
        key2 = cache.generate_cache_key("tool_a", {"param": "value"})

        assert key1 == key2
        assert key1.startswith("tool:tool_a:")

    def test_generate_cache_key_different_tools(self):
        """测试: 不同工具生成不同缓存键。"""
        config = ToolCacheConfig()
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool_a", {"param": "value"})
        key2 = cache.generate_cache_key("tool_b", {"param": "value"})

        assert key1 != key2

    def test_generate_cache_key_ignores_session_info(self):
        """测试: 缓存键忽略 session_id/user_id 等无关字段。"""
        config = ToolCacheConfig()
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool", {
            "query": "test", "session_id": "s1", "user_id": "u1",
        })
        key2 = cache.generate_cache_key("tool", {
            "query": "test", "session_id": "s2", "user_id": "u2",
        })

        assert key1 == key2

    def test_get_cache_stats_initial(self):
        """测试: 初始缓存统计全为零。"""
        config = ToolCacheConfig()
        cache = ToolCache(config)

        stats = cache.get_cache_stats()

        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["total"] == 0
        assert stats["hit_rate"] == 0

    def test_contains_sensitive_info_password(self):
        """测试: _contains_sensitive_info 检测密码字段。"""
        assert _contains_sensitive_info({"password": "secret"})
        assert _contains_sensitive_info({"user_token": "abc"})
        assert _contains_sensitive_info({"api_key": "xyz"})
        assert _contains_sensitive_info({"credential": "data"})

    def test_contains_sensitive_info_nested(self):
        """测试: _contains_sensitive_info 检测嵌套结构中的敏感信息。"""
        assert _contains_sensitive_info({
            "data": {"config": {"secret_value": "hidden"}},
        })

    def test_contains_sensitive_info_clean(self):
        """测试: _contains_sensitive_info 对干净输入返回 False。"""
        assert not _contains_sensitive_info({"query": "hello"})
        assert not _contains_sensitive_info({"path": "/tmp/file"})


# ════════════════════════════════════════════════════════════════
# InputNormalizer 测试
# ════════════════════════════════════════════════════════════════


class TestInputNormalizer:
    """输入规范化器测试。"""

    def test_normalize_boolean_string_true(self):
        """测试: 字符串 'true' 转换为布尔 True。"""
        schema = {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
        }
        result = normalize_input_types({"enabled": "true"}, schema)
        assert result["enabled"] is True

    def test_normalize_boolean_string_false(self):
        """测试: 字符串 'false' 转换为布尔 False。"""
        schema = {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
        }
        result = normalize_input_types({"enabled": "false"}, schema)
        assert result["enabled"] is False

    def test_normalize_integer_string(self):
        """测试: 字符串整数转换为 int。"""
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }
        result = normalize_input_types({"count": "123"}, schema)
        assert result["count"] == 123

    def test_normalize_number_string(self):
        """测试: 字符串浮点数转换为 float。"""
        schema = {
            "type": "object",
            "properties": {"price": {"type": "number"}},
        }
        result = normalize_input_types({"price": "3.14"}, schema)
        assert result["price"] == 3.14

    def test_normalize_object_from_json_string(self):
        """测试: JSON 字符串转换为对象。"""
        schema = {
            "type": "object",
            "properties": {"config": {"type": "object"}},
        }
        result = normalize_input_types(
            {"config": '{"key": "value"}'}, schema
        )
        assert result["config"] == {"key": "value"}

    def test_normalize_no_schema_property_passthrough(self):
        """测试: schema 中不存在的属性原样保留。"""
        schema = {"type": "object", "properties": {}}
        result = normalize_input_types({"extra": "value"}, schema)
        assert result["extra"] == "value"

    def test_try_parse_json_string_valid(self):
        """测试: 有效 JSON 字符串解析成功。"""
        result = try_parse_json_string('{"key": "value"}')
        assert result == {"key": "value"}

    def test_try_parse_json_string_invalid(self):
        """测试: 无效 JSON 字符串返回 None。"""
        assert try_parse_json_string("not json") is None
        assert try_parse_json_string("") is None
        assert try_parse_json_string("[1, 2]") is None  # 非 dict

    def test_fix_object_field_from_json_string(self):
        """测试: fix_object_field 从 JSON 字符串修复对象字段。"""
        inputs: dict[str, Any] = {"data": '{"key": "val"}'}
        fix_object_field(inputs, "data")
        assert inputs["data"] == {"key": "val"}

    def test_fix_object_field_empty_string_removed(self):
        """测试: fix_object_field 移除空字符串字段。"""
        inputs: dict[str, Any] = {"data": "  "}
        fix_object_field(inputs, "data")
        assert "data" not in inputs

    def test_fix_object_field_already_dict(self):
        """测试: fix_object_field 不修改已经是 dict 的值。"""
        original = {"key": "val"}
        inputs: dict[str, Any] = {"data": original}
        fix_object_field(inputs, "data")
        assert inputs["data"] is original

    def test_fix_task_submit_inputs_adds_default_ac(self):
        """测试: fix_task_submit_inputs 为缺失 AC 的非容器任务添加默认 file_check。"""
        inputs: dict[str, Any] = {
            "task_scope": "non_container",
            "target_type": "agent",
            "target_id": "test_agent",
        }
        fix_task_submit_inputs(inputs)

        assert "acceptance_criteria" in inputs
        ac = inputs["acceptance_criteria"]
        assert "file_check" in ac


# ════════════════════════════════════════════════════════════════
# FormatManager 测试
# ════════════════════════════════════════════════════════════════


class TestFormatManager:
    """FormatManager 序列化测试。"""

    def test_serialize_json(self):
        """测试: JSON 格式序列化。"""
        mgr = FormatManager(default_format=ToolFormat.JSON)
        result = mgr.serialize({"key": "value"})

        import json
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_serialize_yaml(self):
        """测试: YAML 格式序列化。"""
        mgr = FormatManager(default_format=ToolFormat.YAML)
        result = mgr.serialize({"key": "value"})

        import yaml
        parsed = yaml.safe_load(result)
        assert parsed["key"] == "value"

    def test_serialize_xml(self):
        """测试: XML 格式序列化。"""
        mgr = FormatManager(default_format=ToolFormat.XML)
        result = mgr.serialize({"key": "value"})

        assert "<key>value</key>" in result

    def test_get_format_manager_singleton(self):
        """测试: get_format_manager 返回全局单例。"""
        mgr1 = get_format_manager()
        mgr2 = get_format_manager()

        assert mgr1 is mgr2

    def test_set_default_format(self):
        """测试: set_default_format 修改默认格式。"""
        mgr = FormatManager(default_format=ToolFormat.JSON)
        mgr.set_default_format(ToolFormat.YAML)

        assert mgr.default_format == ToolFormat.YAML


# ════════════════════════════════════════════════════════════════
# Bug 修复验证: tools/exceptions.py 导入路径
# ════════════════════════════════════════════════════════════════


class TestToolsExceptionsImport:
    """验证 tools/exceptions.py 的导入路径修复。

    Bug: src/tools/exceptions.py 使用了 ``from src.core.exceptions import DomainException``，
    但项目 sys.path 配置使 src 不在路径中，应使用 ``from core.exceptions.base import DomainException``。
    同时该文件缺少类型注解（dict = None 应为 dict | None = None）。
    """

    def test_import_tools_exceptions_module(self):
        """测试: tools.exceptions 模块可正确导入（Bug 修复后）。"""
        import importlib

        module = importlib.import_module("tools.exceptions")
        assert hasattr(module, "ToolNotFoundError")
        assert hasattr(module, "ToolExecutionError")
        assert hasattr(module, "ToolAlreadyExistsError")
        assert hasattr(module, "ToolValidationError")

    def test_tools_exceptions_type_annotations(self):
        """测试: tools.exceptions 异常类构造函数有正确的类型注解。"""
        import inspect

        from tools.exceptions import ToolNotFoundError

        sig = inspect.signature(ToolNotFoundError.__init__)
        details_param = sig.parameters.get("details")

        assert details_param is not None
        assert details_param.default is None
        # 修复后应使用 dict | None 而非 bare dict = None
        assert details_param.annotation is not inspect.Parameter.empty


# ════════════════════════════════════════════════════════════════
# Bug 修复验证: _get_llm_schema 死代码
# ════════════════════════════════════════════════════════════════


class TestToolSchemaDeadCodeFix:
    """验证 Tool._get_llm_schema 死代码修复。

    Bug: 原代码第305-308行，在外层 ``not self.param_level_restrictions`` 为 True 的分支中，
    内部又检查 ``self.param_level_restrictions``，此条件永远为 False，是死代码。
    """

    def test_no_injected_params_no_restrictions_returns_original(self):
        """测试: 无注入参数和层级限制时返回原始 schema。"""
        tool = Tool(
            name="simple_tool",
            description="简单工具",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            source=ToolSource.BUILTIN,
        )

        schema = tool.get_tool_call_schema()

        assert schema["properties"]["x"]["type"] == "string"
        assert "x" in schema["required"]

    def test_schema_with_agent_level_no_restrictions(self):
        """测试: 有 agent_level 但无层级限制时不报错。"""
        tool = Tool(
            name="level_tool",
            description="层级工具",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
            source=ToolSource.BUILTIN,
        )

        # agent_level=None + 无 param_level_restrictions
        # 不应触发死代码路径
        schema = tool._get_llm_schema(agent_level=2)
        assert "x" in schema["properties"]
