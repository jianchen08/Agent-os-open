"""ToolCallGuard 和 OutputRepetitionGuard 新插件单元测试。

测试覆盖：
- ToolCallGuard：首次调用通过、1-2次重复添加提示、3次重复清空并路由、超阈值decision路由
- OutputRepetitionGuard：首次输出通过、1-2次相似输出路由、3次重度提示、超阈值decision路由
"""

import hashlib
import os

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

# 直接加载模块，绕过 __init__.py 导入链
from tests.suites.plugins.conftest import load_module_from_file

_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))


def _import_tool_call_guard():
    """加载 tool_call_guard 模块。"""
    return load_module_from_file(
        "tool_call_guard",
        os.path.join(_SRC_DIR, "plugins", "input", "tool_call_guard.py"),
    )


def _import_output_repetition_guard():
    """加载 output_repetition_guard 模块。"""
    return load_module_from_file(
        "output_repetition_guard",
        os.path.join(_SRC_DIR, "plugins", "output", "output_repetition_guard.py"),
    )


# ============================================================================
# ToolCallGuard 测试
# ============================================================================


class TestToolCallGuard:
    """ToolCallGuard 插件测试套件。"""

    def _make_plugin(self, config=None):
        """创建 ToolCallGuard 实例。"""
        mod = _import_tool_call_guard()
        return mod.ToolCallGuard(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_first_call_no_repeat_passes_through(self):
        """首次调用（无重复）应正常通过，不修改工具调用。"""
        plugin = self._make_plugin()
        tool_calls = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tool_calls,
        })

        result = await plugin.execute(ctx)

        # 不应有 route_signal
        assert result.route_signal is None
        # 不应修改工具调用
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates
        # 应记录签名和重复计数
        assert "tool_call.last_signature" in result.state_updates
        assert result.state_updates["tool_call.repeat_count"] == 0

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_empty(self):
        """无工具调用时返回空结果。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({StateKeys.RAW_TOOL_CALLS: []})

        result = await plugin.execute(ctx)

        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_one_repeat_adds_prompt_no_block(self):
        """1-2次重复应添加系统提示但不阻止工具调用。"""
        plugin = self._make_plugin()
        tc = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        sig = plugin._generate_signature(tc)

        # 模拟第一次已执行，当前为第二次调用（签名相同）
        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 0,
        })

        result = await plugin.execute(ctx)

        # 应添加系统提示
        assert "messages" in result.state_updates
        messages = result.state_updates["messages"]
        assert any("[ToolCallGuard]" in m.get("content", "") for m in messages)
        # 不应清空工具调用
        assert StateKeys.RAW_TOOL_CALLS not in result.state_updates or \
               result.state_updates.get(StateKeys.RAW_TOOL_CALLS) != []
        # 不应有路由信号
        assert result.route_signal is None
        # repeat_count 应为 1
        assert result.state_updates["tool_call.repeat_count"] == 1

    @pytest.mark.asyncio
    async def test_two_repeats_adds_prompt_no_block(self):
        """2次重复（repeat_count=2）应添加系统提示但不阻止。"""
        plugin = self._make_plugin()
        tc = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        sig = plugin._generate_signature(tc)

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 1,
        })

        result = await plugin.execute(ctx)

        assert "messages" in result.state_updates
        assert result.route_signal is None
        assert result.state_updates["tool_call.repeat_count"] == 2

    @pytest.mark.asyncio
    async def test_three_repeats_clears_tool_calls_routes_next_llm(self):
        """3次重复应清空工具调用并路由 next_llm。"""
        plugin = self._make_plugin()
        tc = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        sig = plugin._generate_signature(tc)

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 2,
        })

        result = await plugin.execute(ctx)

        # 应清空工具调用
        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS) == []
        assert result.state_updates.get("tool_call.blocked") is True
        # 应有 next_llm 路由信号
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        # 应添加系统提示
        assert "messages" in result.state_updates

    @pytest.mark.asyncio
    async def test_exceeds_max_retries_produces_decision_route(self):
        """超过阈值（默认3次）应产出 decision 路由信号。"""
        plugin = self._make_plugin()
        tc = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        sig = plugin._generate_signature(tc)

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 3,  # 超过 max_retries=3
        })

        result = await plugin.execute(ctx)

        # 应有 decision 路由信号
        assert result.route_signal is not None
        assert result.route_signal.route_type == "decision"
        assert result.route_signal.payload is not None
        assert result.route_signal.payload["decision_type"] == "agent"
        assert result.route_signal.payload["repeat_count"] == 4

    @pytest.mark.asyncio
    async def test_custom_max_retries(self):
        """自定义 max_retries 应正确生效。"""
        plugin = self._make_plugin(config={"max_retries": 5})
        tc = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        sig = plugin._generate_signature(tc)

        # repeat_count=2 在 max_retries=5 内，应走 next_llm
        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 2,
        })

        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"

        # repeat_count=5 超过 max_retries=5，应走 decision
        ctx2 = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: tc,
            "tool_call.last_signature": sig,
            "tool_call.repeat_count": 5,
        })

        result2 = await plugin.execute(ctx2)
        assert result2.route_signal is not None
        assert result2.route_signal.route_type == "decision"

    def test_signature_reflects_tool_name_and_args(self):
        """签名生成应正确反映工具名和参数。"""
        plugin = self._make_plugin()

        tc1 = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]
        tc2 = [{"name": "read_file", "args": {"path": "/tmp/b.txt"}}]
        tc3 = [{"name": "write_file", "args": {"path": "/tmp/a.txt"}}]
        tc4 = [{"name": "read_file", "args": {"path": "/tmp/a.txt"}}]

        sig1 = plugin._generate_signature(tc1)
        sig2 = plugin._generate_signature(tc2)
        sig3 = plugin._generate_signature(tc3)
        sig4 = plugin._generate_signature(tc4)

        # 相同工具名和参数应生成相同签名
        assert sig1 == sig4
        # 不同参数应生成不同签名
        assert sig1 != sig2
        # 不同工具名应生成不同签名
        assert sig1 != sig3

    def test_signature_multiple_tool_calls(self):
        """多工具调用签名应为逗号分隔的哈希。"""
        plugin = self._make_plugin()

        tc = [
            {"name": "read_file", "args": {"path": "/tmp/a.txt"}},
            {"name": "write_file", "args": {"path": "/tmp/b.txt"}},
        ]
        sig = plugin._generate_signature(tc)

        # 签名应包含两个哈希，逗号分隔
        parts = sig.split(",")
        assert len(parts) == 2

    @pytest.mark.asyncio
    async def test_different_signature_resets_repeat_count(self):
        """不同签名应重置重复计数。"""
        plugin = self._make_plugin()

        ctx = self._make_ctx({
            StateKeys.RAW_TOOL_CALLS: [{"name": "write_file", "args": {"path": "/new.txt"}}],
            "tool_call.last_signature": "old_signature_xyz",
            "tool_call.repeat_count": 5,
        })

        result = await plugin.execute(ctx)

        assert result.state_updates["tool_call.repeat_count"] == 0
        assert result.route_signal is None

    def test_name_and_priority(self):
        """插件名称和优先级应正确。"""
        plugin = self._make_plugin()
        assert plugin.name == "tool_call_guard"
        assert plugin.priority == 15

        plugin_custom = self._make_plugin(config={"priority": 20})
        assert plugin_custom.priority == 20


# ============================================================================
# OutputRepetitionGuard 测试
# ============================================================================


class TestOutputRepetitionGuard:
    """OutputRepetitionGuard 插件测试套件。"""

    def _make_plugin(self, config=None):
        """创建 OutputRepetitionGuard 实例。"""
        mod = _import_output_repetition_guard()
        return mod.OutputRepetitionGuard(config=config)

    def _make_ctx(self, state=None, services=None):
        """创建 Mock PluginContext。"""
        return PluginContext(
            state=state or {},
            config={},
            _services=services or {},
        )

    @pytest.mark.asyncio
    async def test_first_output_no_repeat_passes(self):
        """首次输出（无重复）应正常通过。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: "Hello, this is the first output.",
        })

        result = await plugin.execute(ctx)

        # 不应有路由信号
        assert result.route_signal is None
        # 不应清空输出
        assert StateKeys.RAW_RESULT not in result.state_updates
        # 应记录哈希和重复计数
        assert "output.last_hash" in result.state_updates
        assert result.state_updates["output.repeat_count"] == 0

    @pytest.mark.asyncio
    async def test_no_raw_result_returns_empty(self):
        """无 RAW_RESULT 时返回空结果。"""
        plugin = self._make_plugin()
        ctx = self._make_ctx({})

        result = await plugin.execute(ctx)

        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_one_repeat_routes_next_llm_with_light_prompt(self):
        """1次重复应清空输出并路由 next_llm（轻度提示）。"""
        plugin = self._make_plugin()
        text = "This is the repeated output."

        text_hash = hashlib.md5(text[:500].encode()).hexdigest()[:8]

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: text,
            "output.last_hash": text_hash,
            "output.last_text": text,
            "output.repeat_count": 0,
        })

        result = await plugin.execute(ctx)

        # 应清空输出
        assert result.state_updates.get(StateKeys.RAW_RESULT) == ""
        # 应有 next_llm 路由信号
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        # 应添加系统提示
        assert "messages" in result.state_updates
        messages = result.state_updates["messages"]
        assert any("[OutputRepetitionGuard]" in m.get("content", "") for m in messages)
        # repeat_count 应为 1
        assert result.state_updates["output.repeat_count"] == 1

    @pytest.mark.asyncio
    async def test_two_repeats_routes_next_llm_with_light_prompt(self):
        """2次重复应清空输出并路由 next_llm（轻度提示）。"""
        plugin = self._make_plugin()
        text = "Repeated output again."

        text_hash = hashlib.md5(text[:500].encode()).hexdigest()[:8]

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: text,
            "output.last_hash": text_hash,
            "output.last_text": text,
            "output.repeat_count": 1,
        })

        result = await plugin.execute(ctx)

        assert result.state_updates.get(StateKeys.RAW_RESULT) == ""
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["output.repeat_count"] == 2

    @pytest.mark.asyncio
    async def test_three_repeats_routes_next_llm_with_strong_prompt(self):
        """3次重复应使用重度提示并路由 next_llm。"""
        plugin = self._make_plugin()
        text = "Still repeating."

        text_hash = hashlib.md5(text[:500].encode()).hexdigest()[:8]

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: text,
            "output.last_hash": text_hash,
            "output.last_text": text,
            "output.repeat_count": 2,
        })

        result = await plugin.execute(ctx)

        assert result.state_updates.get(StateKeys.RAW_RESULT) == ""
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        # 验证使用了重度提示
        messages = result.state_updates["messages"]
        assert any("[OutputRepetitionGuard]" in m.get("content", "") for m in messages)

    @pytest.mark.asyncio
    async def test_exceeds_max_retries_produces_decision_route(self):
        """超过阈值应产出 decision 路由信号。"""
        plugin = self._make_plugin()
        text = "Over the limit."

        text_hash = hashlib.md5(text[:500].encode()).hexdigest()[:8]

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: text,
            "output.last_hash": text_hash,
            "output.last_text": text,
            "output.repeat_count": 3,
        })

        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "decision"
        assert result.route_signal.payload is not None
        assert result.route_signal.payload["decision_type"] == "agent"

    @pytest.mark.asyncio
    async def test_similar_text_detected_as_repeat(self):
        """高相似度文本应被检测为重复。"""
        plugin = self._make_plugin(config={"similarity_threshold": 0.85})

        # 上一段文本
        last_text = "The quick brown fox jumps over the lazy dog."
        # 当前文本（稍有变化但高度相似）
        current_text = "The quick brown fox jumps over the lazy dog"

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: current_text,
            "output.last_hash": "different_hash",
            "output.last_text": last_text,
            "output.repeat_count": 0,
        })

        result = await plugin.execute(ctx)

        # 由于文本高度相似，应被检测为重复
        assert result.route_signal is not None
        assert result.state_updates["output.repeat_count"] == 1

    @pytest.mark.asyncio
    async def test_dissimilar_text_not_detected_as_repeat(self):
        """不相似文本不应被检测为重复。"""
        plugin = self._make_plugin()

        last_text = "This is about machine learning algorithms."
        current_text = "Today the weather is sunny and warm."

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: current_text,
            "output.last_hash": "different_hash",
            "output.last_text": last_text,
            "output.repeat_count": 0,
        })

        result = await plugin.execute(ctx)

        # 不相似的文本不应被检测为重复
        assert result.route_signal is None
        assert result.state_updates["output.repeat_count"] == 0

    def test_compute_similarity_correct(self):
        """文本相似度计算应正确。"""
        plugin = self._make_plugin()

        # 完全相同
        assert plugin._compute_similarity("hello world", "hello world") == 1.0

        # 完全不同
        assert plugin._compute_similarity("abc", "xyz") < 0.3

        # 空文本
        assert plugin._compute_similarity("", "hello") == 0.0
        assert plugin._compute_similarity("hello", "") == 0.0

        # 部分相似
        sim = plugin._compute_similarity(
            "The quick brown fox",
            "The quick brown cat",
        )
        assert 0.5 < sim < 1.0

    @pytest.mark.asyncio
    async def test_different_output_resets_repeat_count(self):
        """不同的输出应重置重复计数。"""
        plugin = self._make_plugin()

        ctx = self._make_ctx({
            StateKeys.RAW_RESULT: "Completely new and different output content here.",
            "output.last_hash": "old_hash_xyz",
            "output.last_text": "Old content that is very different from the new one.",
            "output.repeat_count": 5,
        })

        result = await plugin.execute(ctx)

        assert result.state_updates["output.repeat_count"] == 0
        assert result.route_signal is None

    def test_name_and_priority(self):
        """插件名称和优先级应正确。"""
        plugin = self._make_plugin()
        assert plugin.name == "output_repetition_guard"
        assert plugin.priority == 12

        plugin_custom = self._make_plugin(config={"priority": 20})
        assert plugin_custom.priority == 20

    def test_route_signals(self):
        """插件应声明 decision 和 next_llm 路由信号。"""
        plugin = self._make_plugin()
        assert "decision" in plugin.route_signals
        assert "next_llm" in plugin.route_signals
