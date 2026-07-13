"""复盘 B 路径隔离层 e2e 测试。

真实组件（不连 LLM）验证改造的正确性：
1. read_execution_detail 的 L1 摘要真实读 ExecutionRecordStorage，返回 user_inputs
   —— 验证"复盘看语义时必须能看到用户输入和人类交互"这一改造。
2. 注册管道时 tags.agent_id 写入注册表，能被 EngineRegistry 读回
   —— 验证"agent 身份走 tags"这一改造的注册侧。
3. _resolve_trigger_origin 从父管道 tags 反查触发来源
   —— 验证来源溯源逻辑。

注意：本测试不连真实 LLM，不验证 review_agent 的 LLM 输出。
全链路（连 LLM）的 e2e 在另一组测试/脚本。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
)
from memory.maintenance.service import MemoryMaintenanceService
from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool


# ============================================================
# 点 3：read_execution_detail L1 必须返回 user_inputs
# ============================================================


def _storage_with_user_and_ai(pipeline_run_id: str = "pipe-review-001") -> ExecutionRecordStorage:
    """构造含 user 指令 + ai 回复 + tool 调用 + 错误的真实执行记录。

    模拟一个典型管道：用户下达任务 → agent 执行工具 → 报错。
    """
    storage = ExecutionRecordStorage()  # 纯内存
    records = [
        ExecutionRecordData(
            pipeline_run_id=pipeline_run_id, type="user", role="user",
            name="user", content="请帮我重构这个模块，确保所有测试通过",
            sequence=1, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=pipeline_run_id, type="ai", role="assistant",
            name="assistant", content="我来分析并重构",
            thinking_content="先看现有结构",
            sequence=2, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=pipeline_run_id, type="tool", role="tool",
            name="file_read", content="读取了模块文件",
            sequence=3, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=pipeline_run_id, type="ai", role="assistant",
            name="assistant", content="", error="ImportError: No module named 'core'",
            sequence=4, iteration=1,
        ),
    ]
    for r in records:
        storage.save(r)
    return storage


class TestReadExecutionDetailL1IncludesUserInputs:
    """L1 语义摘要必须包含用户输入/人类交互段。

    这是 B 路径复盘的关键：脱离用户意图的根因分析没有意义。
    修复前 _get_l1_block 只取 type=ai/tool，完全看不到用户指令。
    """

    def test_l1_summary_has_user_inputs_section(self):
        """L1 摘要必须包含 user_inputs 段。"""
        storage = _storage_with_user_and_ai()
        tool = ReadExecutionDetailTool(storage=storage)

        result = tool._get_l1_block(storage, {"iteration": 1}, "pipe-review-001")

        assert result.success, "L1 查询应成功"
        summary = result.output["summary"]
        assert "user_inputs" in summary, "L1 摘要必须含 user_inputs 段"

    def test_l1_user_inputs_contains_user_message(self):
        """user_inputs 段必须包含真实的用户指令文本。"""
        storage = _storage_with_user_and_ai()
        tool = ReadExecutionDetailTool(storage=storage)

        result = tool._get_l1_block(storage, {"iteration": 1}, "pipe-review-001")

        summary = result.output["summary"]
        user_inputs = summary["user_inputs"]
        assert len(user_inputs) >= 1, "应至少有 1 条用户输入"
        # 用户指令原文必须出现在摘要里
        assert "重构这个模块" in user_inputs[0]["content_preview"]

    def test_l1_user_inputs_appears_before_ai_actions(self):
        """user_inputs 应排在 ai_actions 之前（复盘先理解意图再看动作）。"""
        storage = _storage_with_user_and_ai()
        tool = ReadExecutionDetailTool(storage=storage)

        result = tool._get_l1_block(storage, {"iteration": 1}, "pipe-review-001")

        summary = result.output["summary"]
        keys = list(summary.keys())
        # iteration 是第一段，user_inputs 应紧随其后，先于 ai_actions
        assert keys.index("user_inputs") < keys.index("ai_actions")

    def test_l1_includes_human_interaction_type(self):
        """type=human 的人类交互记录也要进 user_inputs。"""
        storage = ExecutionRecordStorage()
        storage.save(ExecutionRecordData(
            pipeline_run_id="pipe-h", type="human", role="user",
            content="这里不对，换个方案", sequence=1, iteration=1,
        ))
        tool = ReadExecutionDetailTool(storage=storage)

        result = tool._get_l1_block(storage, {"iteration": 1}, "pipe-h")

        summary = result.output["summary"]
        assert len(summary["user_inputs"]) == 1
        assert "换个方案" in summary["user_inputs"][0]["content_preview"]

    def test_l1_still_includes_ai_and_errors(self):
        """补 user_inputs 后，原有的 ai_actions/errors 段仍要保留。"""
        storage = _storage_with_user_and_ai()
        tool = ReadExecutionDetailTool(storage=storage)

        result = tool._get_l1_block(storage, {"iteration": 1}, "pipe-review-001")

        summary = result.output["summary"]
        assert len(summary["ai_actions"]) >= 1, "ai_actions 仍要有"
        assert len(summary["errors"]) >= 1, "errors 仍要有"
        assert "ImportError" in summary["errors"][0]["error"]


# ============================================================
# 点 2：_resolve_trigger_origin 来源反查
# ============================================================


class TestResolveTriggerOrigin:
    """从父管道 tags 反查触发来源（agent / 会话）。"""

    def test_returns_empty_when_no_parent(self):
        """parent_pipeline_id 为空时返回空来源，不崩。"""
        service = MemoryMaintenanceService(
            storage=ExecutionRecordStorage(),
            chunk_db=None,
            knowledge_service=None,
        )
        origin = service._resolve_trigger_origin("")

        assert origin["trigger_agent"] == ""
        assert origin["trigger_session"] == ""
        assert origin["trigger_tool"] == "trigger_review"

    def test_returns_empty_when_parent_not_in_registry(self):
        """父管道不在注册表时返回空，不阻断复盘。"""
        service = MemoryMaintenanceService(
            storage=ExecutionRecordStorage(),
            chunk_db=None,
            knowledge_service=None,
        )
        # 这个 pipeline_id 不在注册表里
        origin = service._resolve_trigger_origin("nonexistent-pipeline-xyz")

        assert origin["trigger_agent"] == ""
        assert origin["trigger_session"] == ""


# ============================================================
# 点 1：tags.agent_id 注册链路（真实 PipelineEngine + 真实 EngineRegistry）
# ============================================================


def _make_engine():
    """构造真实可 run 的 PipelineEngine（用假 core 绕过 LLM）。

    范式摘自 tests/suites/core/test_engine_registry_e2e.py 的 _make_engine。
    """
    from pipeline.engine import PipelineEngine
    from pipeline.plugin import (
        ErrorPolicy, ICorePlugin, IOutputPlugin, OutputResult, PluginContext,
    )
    from pipeline.registry import PluginRegistry
    from pipeline.route import (
        InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable,
    )
    from pipeline.types import RouteSignal

    class _FakeLLMCore(ICorePlugin):
        """假 LLM core：不连真实模型，直接产出固定回复并结束。"""
        error_policy = ErrorPolicy.ABORT
        fallback_state = {"raw_result": "fallback"}

        @property
        def name(self): return "fake_llm_core"
        @property
        def priority(self): return 0

        async def execute(self, ctx: PluginContext) -> dict:
            return {"raw_result": "复盘报告（假LLM）", "task_complete": True}

    class _AutoEndOutput(IOutputPlugin):
        error_policy = ErrorPolicy.ABORT
        @property
        def name(self): return "auto_end_output"
        @property
        def priority(self): return 0
        @property
        def route_signals(self): return []
        async def execute(self, ctx: PluginContext) -> OutputResult:
            task_complete = ctx.state.get("task_complete", False)
            sig = RouteSignal(route_type="end" if task_complete else "next_llm", reason="auto")
            return OutputResult(route_signal=sig)

    input_table = InputRouteTable([
        InputRouteEntry(name="default", condition="", target="core", plugins=[], priority=0),
    ])
    output_table = OutputRouteTable([
        OutputRouteEntry(route_type="next_llm", condition="", priority=0),
        OutputRouteEntry(route_type="end", condition="", priority=1),
    ])
    reg = PluginRegistry()
    reg.register_core("llm_call", _FakeLLMCore())
    reg.register(_AutoEndOutput())
    return PipelineEngine(input_table, output_table, reg)


class TestTagsAgentIdRegistration:
    """验证复盘管道 tags（agent_id/source/parent_pipeline/session_id）写入注册表。

    这是点 1 改造的核心：agent 身份走 tags，引擎启动时由 _start_idle_engine
    从 tags.agent_id 反查。本测试验证注册侧 tags 真实可读回。
    """

    def test_review_pipeline_tags_persisted_in_registry(self):
        """注册带完整来源 tags 的管道，注册表能读回全部字段。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_engine()
        review_tags = {
            "agent_id": "review_agent",
            "source": "tool_review",
            "parent_pipeline": "parent-pipe-001",
            "session_id": "session-xyz",
        }
        try:
            entry = registry.register(
                engine.pipeline_id, engine,
                thread_id="session-xyz",
                tags=review_tags,
            )
            assert entry is not None

            # 读回：注册表必须保留全部来源 tags
            got = registry.get(engine.pipeline_id)
            assert got is not None
            assert got.tags["agent_id"] == "review_agent"
            assert got.tags["source"] == "tool_review"
            assert got.tags["parent_pipeline"] == "parent-pipe-001"
            assert got.tags["session_id"] == "session-xyz"
        finally:
            # 清理：避免污染全局注册表
            try:
                registry.unregister(engine.pipeline_id)
            except Exception:
                pass

    def test_find_review_pipeline_by_source_tag(self):
        """能按 source=tool_review 反查到复盘管道（用于防自循环 _is_review_pipeline）。"""
        from pipeline.registry import get_engine_registry

        registry = get_engine_registry()
        engine = _make_engine()
        try:
            registry.register(
                engine.pipeline_id, engine,
                tags={"agent_id": "review_agent", "source": "tool_review"},
            )
            # find_by_tag 用 key/value 对查询
            found = registry.find_by_tag("source", "tool_review")
            assert any(e.engine.pipeline_id == engine.pipeline_id for e in found)
        finally:
            try:
                registry.unregister(engine.pipeline_id)
            except Exception:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
