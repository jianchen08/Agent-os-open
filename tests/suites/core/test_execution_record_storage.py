"""M12c 执行记录存储 + TrackPlugin 增强测试。

测试 ExecutionRecordStorage 的 CRUD 操作和 TrackPlugin
增加执行记录持久化写入后的行为（有/无 execution_record_storage 服务）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
    PipelineRunSummary,
    summarize_text,
)
from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from plugins.output.track import TrackPlugin


# ── Fixtures ──


@pytest.fixture
def storage() -> ExecutionRecordStorage:
    """创建纯内存存储实例。"""
    return ExecutionRecordStorage()


@pytest.fixture
def sample_record() -> ExecutionRecordData:
    """创建样本执行记录。"""
    return ExecutionRecordData(
        pipeline_run_id="run-001",
        type="ai",
        sequence=1,
        iteration=1,
        role="assistant",
        content="Hello world",
    )


@pytest.fixture
def base_state() -> dict[str, Any]:
    """创建基础管道状态。"""
    return {
        "pipeline_id": "run-001",
        "iteration": 1,
        "ended": False,
    }


@pytest.fixture
def ctx(base_state: dict[str, Any]) -> PluginContext:
    """创建无服务的插件上下文。"""
    return PluginContext(state=base_state)


@pytest.fixture
def ctx_with_storage(base_state: dict[str, Any]) -> PluginContext:
    """创建包含 execution_record_storage 服务的插件上下文。"""
    storage = ExecutionRecordStorage()
    return PluginContext(
        state=base_state,
        _services={"execution_record_storage": storage},
    )


# ── ExecutionRecordData Tests ──


class TestExecutionRecordData:
    """执行记录数据类测试。"""

    def test_auto_generate_record_id(self):
        """测试自动生成 record_id。"""
        record = ExecutionRecordData()
        assert record.record_id != ""
        assert len(record.record_id) == 12

    def test_auto_generate_created_at(self):
        """测试自动生成 created_at。"""
        record = ExecutionRecordData()
        assert record.created_at != ""
        # 应该是 ISO 8601 格式
        assert "T" in record.created_at or "-" in record.created_at

    def test_preserve_explicit_values(self):
        """测试保留显式设置的值。"""
        record = ExecutionRecordData(
            record_id="custom-id",
            created_at="2026-01-01T00:00:00",
            pipeline_run_id="s1",
            iteration=5,
        )
        assert record.record_id == "custom-id"
        assert record.created_at == "2026-01-01T00:00:00"
        assert record.pipeline_run_id == "s1"
        assert record.iteration == 5

    def test_default_values(self):
        """测试默认值。"""
        record = ExecutionRecordData()
        assert record.pipeline_run_id == ""
        assert record.type == "ai"
        assert record.role == ""
        assert record.content == ""
        assert record.sequence == 0
        assert record.error is None


# ── ExecutionRecordStorage Tests ──


class TestExecutionRecordStorage:
    """执行记录存储测试。"""

    def test_save_returns_record_id(self, storage: ExecutionRecordStorage, sample_record: ExecutionRecordData):
        """测试 save 返回 record_id。"""
        record_id = storage.save(sample_record)
        assert record_id == sample_record.record_id
        assert record_id != ""

    def test_save_auto_generates_id(self, storage: ExecutionRecordStorage):
        """测试 save 自动生成 ID（当 record_id 为空时）。"""
        record = ExecutionRecordData(pipeline_run_id="s1", iteration=1)
        record_id = storage.save(record)
        assert record_id != ""
        assert record.record_id == record_id

    def test_get_existing_record(self, storage: ExecutionRecordStorage, sample_record: ExecutionRecordData):
        """测试获取存在的记录。"""
        record_id = storage.save(sample_record)
        result = storage.get(record_id)
        assert result is not None
        assert result.pipeline_run_id == "run-001"
        assert result.type == "ai"
        assert result.content == "Hello world"
        assert result.role == "assistant"
        assert result.iteration == 1

    def test_get_nonexistent_record(self, storage: ExecutionRecordStorage):
        """测试获取不存在的记录返回 None。"""
        assert storage.get("nonexistent") is None

    def test_list_by_pipeline(self, storage: ExecutionRecordStorage):
        """测试按管道运行 ID 列出记录。"""
        for i in range(5):
            storage.save(ExecutionRecordData(pipeline_run_id="run-001", sequence=i, iteration=i))
        for i in range(3):
            storage.save(ExecutionRecordData(pipeline_run_id="run-002", sequence=i, iteration=i))

        records = storage.list_by_pipeline("run-001")
        assert len(records) == 5
        # 按 sequence 升序排列
        assert [r.sequence for r in records] == [0, 1, 2, 3, 4]

        records_2 = storage.list_by_pipeline("run-002")
        assert len(records_2) == 3

    def test_list_by_pipeline_empty(self, storage: ExecutionRecordStorage):
        """测试列出不存在管道运行的记录返回空列表。"""
        records = storage.list_by_pipeline("nonexistent")
        assert records == []

    def test_delete_by_session(self, storage: ExecutionRecordStorage):
        """测试按会话删除记录（兼容接口，内部匹配 pipeline_run_id）。"""
        for i in range(5):
            storage.save(ExecutionRecordData(pipeline_run_id="run-001", sequence=i, iteration=i))
        for i in range(3):
            storage.save(ExecutionRecordData(pipeline_run_id="run-002", sequence=i, iteration=i))

        deleted = storage.delete_by_session("run-001")
        assert deleted == 5
        assert len(storage.list_by_pipeline("run-001")) == 0
        assert len(storage.list_by_pipeline("run-002")) == 3

    def test_delete_by_session_nonexistent(self, storage: ExecutionRecordStorage):
        """测试删除不存在会话的记录返回 0。"""
        deleted = storage.delete_by_session("nonexistent")
        assert deleted == 0

    def test_yaml_persistence(self, tmp_path: Path):
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        storage.save(ExecutionRecordData(
            pipeline_run_id="run-001",
            type="ai",
            sequence=1,
            iteration=1,
            role="assistant",
            content="test output",
        ))

        yaml_file = data_dir / "run-001.yaml"
        assert yaml_file.exists()
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert len(data["records"]) == 1

        storage2 = ExecutionRecordStorage(data_dir=str(data_dir))
        records = storage2.list_by_pipeline("run-001")
        assert len(records) == 1
        assert records[0].content == "test output"
        assert records[0].type == "ai"

    def test_memory_only_storage(self, storage: ExecutionRecordStorage):
        storage.save(ExecutionRecordData(pipeline_run_id="s1", sequence=1, iteration=1))
        assert len(storage.list_by_pipeline("s1")) == 1
        assert storage._data_dir is None

    def test_load_corrupted_file(self, tmp_path: Path):
        data_dir = tmp_path / "pipelines"
        data_dir.mkdir()
        (data_dir / "corrupted.yaml").write_text("not: valid: yaml: {{{", encoding="utf-8")

        storage = ExecutionRecordStorage(data_dir=str(data_dir))
        assert len(storage.list_by_pipeline("any")) == 0


# ── summarize_text Tests ──


class TestSummarizeText:
    """摘要截断函数测试。"""

    def test_none_returns_empty(self):
        """测试 None 返回空字符串。"""
        assert summarize_text(None) == ""

    def test_short_text_unchanged(self):
        """测试短文本不变。"""
        assert summarize_text("hello") == "hello"

    def test_long_text_truncated(self):
        """测试长文本截断。"""
        long_text = "a" * 600
        result = summarize_text(long_text)
        assert len(result) == 500 + len("...(truncated)")
        assert result.endswith("...(truncated)")

    def test_exact_length_not_truncated(self):
        """测试恰好长度的文本不截断。"""
        text = "a" * 500
        result = summarize_text(text)
        assert result == text

    def test_custom_max_len(self):
        """测试自定义最大长度。"""
        result = summarize_text("hello world", max_len=5)
        assert result == "hello...(truncated)"

    def test_non_string_input(self):
        """测试非字符串输入。"""
        assert summarize_text(123) == "123"
        assert summarize_text([1, 2]) == "[1, 2]"


# ── TrackPlugin Enhancement Tests ──


class TestTrackPluginWithExecutionRecord:
    """TrackPlugin 增强后的测试（执行记录持久化）。"""

    @pytest.mark.asyncio
    async def test_existing_state_logic_unchanged(
        self, ctx: PluginContext, base_state: dict[str, Any]
    ):
        """测试无服务时现有 state 逻辑不变。"""
        base_state["llm_usage"] = {"input_tokens": 100, "output_tokens": 50}
        base_state[StateKeys.ITERATION] = 2
        plugin = TrackPlugin()
        result = await plugin.execute(ctx)

        # 原有 state 更新仍然存在
        assert "track.llm_usage" in result.state_updates
        assert "track.execution_stats" in result.state_updates
        assert result.state_updates["track.llm_usage"]["total_input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_no_service_skips_persist(self, ctx: PluginContext):
        """测试无 execution_record_storage 服务时跳过持久化。"""
        plugin = TrackPlugin()
        # 不应抛出异常
        result = await plugin.execute(ctx)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_with_service_persists_record(
        self, ctx_with_storage: PluginContext, base_state: dict[str, Any]
    ):
        """测试有服务时持久化执行记录。"""
        storage: ExecutionRecordStorage = ctx_with_storage.get_service(
            "execution_record_storage"
        )
        base_state["llm_usage"] = {"input_tokens": 200, "output_tokens": 80}
        base_state[StateKeys.ITERATION] = 3
        base_state[StateKeys.PIPELINE_ID] = "run-persist"
        base_state[StateKeys.RAW_RESULT] = "LLM response content"

        plugin = TrackPlugin()
        result = await plugin.execute(ctx_with_storage)

        # 原有 state 更新仍然存在
        assert "track.llm_usage" in result.state_updates
        assert "track.execution_stats" in result.state_updates

        # 持久化记录已写入
        records = storage.list_by_pipeline("run-persist")
        assert len(records) == 1
        record = records[0]
        assert record.iteration == 3
        assert record.pipeline_run_id == "run-persist"
        assert record.type == "ai"
        assert record.role == "assistant"
        assert record.content == "LLM response content"
        assert record.error is None

    @pytest.mark.asyncio
    async def test_persist_with_error(
        self, ctx_with_storage: PluginContext, base_state: dict[str, Any]
    ):
        """测试持久化包含错误信息的记录。"""
        storage: ExecutionRecordStorage = ctx_with_storage.get_service(
            "execution_record_storage"
        )
        base_state[StateKeys.RAW_ERROR] = "API rate limit exceeded"
        base_state[StateKeys.PIPELINE_ID] = "run-err"
        base_state[StateKeys.RAW_RESULT] = "error response"
        base_state[StateKeys.ITERATION] = 1
        base_state[StateKeys.ENDED] = True

        plugin = TrackPlugin()
        await plugin.execute(ctx_with_storage)

        records = storage.list_by_pipeline("run-err")
        assert len(records) == 1
        assert records[0].type == "ai"
        # 错误信息保存在摘要中
        summary = storage.get_summary("run-err")
        assert summary is not None
        assert summary.error == "API rate limit exceeded"

    @pytest.mark.asyncio
    async def test_persist_large_content_not_truncated(
        self, ctx_with_storage: PluginContext, base_state: dict[str, Any]
    ):
        """测试大 content 完整保存（不截断）。"""
        storage: ExecutionRecordStorage = ctx_with_storage.get_service(
            "execution_record_storage"
        )
        large_content = "x" * 2000
        base_state[StateKeys.RAW_RESULT] = large_content
        base_state[StateKeys.PIPELINE_ID] = "run-trunc"
        base_state[StateKeys.ITERATION] = 1

        plugin = TrackPlugin()
        await plugin.execute(ctx_with_storage)

        records = storage.list_by_pipeline("run-trunc")
        assert len(records) == 1
        # content 应完整保存，不截断
        assert records[0].content == large_content

    @pytest.mark.asyncio
    async def test_persist_tool_results(
        self, ctx_with_storage: PluginContext, base_state: dict[str, Any]
    ):
        """测试持久化工具调用记录。"""
        storage: ExecutionRecordStorage = ctx_with_storage.get_service(
            "execution_record_storage"
        )
        base_state[StateKeys.PIPELINE_ID] = "run-tools"
        base_state[StateKeys.ITERATION] = 2
        base_state[StateKeys.RAW_RESULT] = "AI response with tool calls"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {
                "id": "tc-001",
                "function": {"name": "search", "arguments": '{"query": "test"}'},
            },
            {
                "id": "tc-002",
                "function": {"name": "calculate", "arguments": '{"expr": "1+1"}'},
            },
        ]
        base_state[StateKeys.TOOL_RESULTS] = [
            {"tool": "search", "result": "found something"},
            {"tool": "calculate", "result": 42},
        ]

        plugin = TrackPlugin()
        await plugin.execute(ctx_with_storage)

        records = storage.list_by_pipeline("run-tools")
        # 应有 1 条 AI 记录 + 2 条工具记录 = 3 条
        assert len(records) == 3
        ai_records = [r for r in records if r.type == "ai"]
        tool_records = [r for r in records if r.type == "tool"]
        assert len(ai_records) == 1
        assert len(tool_records) == 2
        assert ai_records[0].content == "AI response with tool calls"
        tool_names = {r.name for r in tool_records}
        assert "search" in tool_names
        assert "calculate" in tool_names

    @pytest.mark.asyncio
    async def test_wrong_service_type_skips(
        self, base_state: dict[str, Any]
    ):
        """测试 execution_record_storage 服务类型不匹配时跳过。"""
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": "not_a_storage_instance"},
        )
        base_state[StateKeys.ITERATION] = 1

        plugin = TrackPlugin()
        # 不应抛出异常
        result = await plugin.execute(ctx)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_storage_save_failure_does_not_break_plugin(
        self, base_state: dict[str, Any]
    ):
        """测试存储 save 失败不破坏插件主流程。"""
        import unittest.mock

        storage = ExecutionRecordStorage()
        # Mock save 方法抛出异常
        storage.save = unittest.mock.MagicMock(side_effect=RuntimeError("disk full"))
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": storage},
        )
        base_state["llm_usage"] = {"input_tokens": 10, "output_tokens": 5}
        base_state[StateKeys.ITERATION] = 1
        base_state[StateKeys.PIPELINE_ID] = "run-fail"

        plugin = TrackPlugin()
        result = await plugin.execute(ctx)

        # 插件不应崩溃，原有逻辑应正常返回
        assert result.error is None
        assert "track.llm_usage" in result.state_updates
        assert "track.execution_stats" in result.state_updates

    @pytest.mark.asyncio
    async def test_multiple_iterations_create_multiple_records(
        self, ctx_with_storage: PluginContext, base_state: dict[str, Any]
    ):
        """测试多次迭代创建多条记录。"""
        storage: ExecutionRecordStorage = ctx_with_storage.get_service(
            "execution_record_storage"
        )
        base_state[StateKeys.PIPELINE_ID] = "run-multi"

        plugin = TrackPlugin()
        for i in range(4):
            base_state[StateKeys.ITERATION] = i
            base_state[StateKeys.RAW_RESULT] = f"response-{i}"
            await plugin.execute(ctx_with_storage)

        records = storage.list_by_pipeline("run-multi")
        assert len(records) == 4


# ── PipelineRunSummary Tests ──


class TestPipelineRunSummary:
    """PipelineRunSummary 测试。"""

    def test_auto_fill_created_at(self):
        """测试自动填充 created_at。"""
        s = PipelineRunSummary(run_id="r1")
        assert s.created_at != ""

    def test_save_and_get_summary(self, storage: ExecutionRecordStorage):
        """测试保存和获取摘要。"""
        s = PipelineRunSummary(run_id="r1", total_iterations=5, total_records=10, status="completed")
        storage.save_summary(s)
        got = storage.get_summary("r1")
        assert got is not None
        assert got.run_id == "r1"
        assert got.total_iterations == 5
        assert got.total_records == 10

    def test_list_summaries(self, storage: ExecutionRecordStorage):
        """测试列出摘要。"""
        for i in range(3):
            storage.save_summary(PipelineRunSummary(run_id=f"r{i}", total_records=i))
        summaries = storage.list_summaries()
        assert len(summaries) == 3

    def test_get_total_tokens(self, storage: ExecutionRecordStorage):
        """测试汇总 token 用量。"""
        storage.save_summary(PipelineRunSummary(
            run_id="r1",
            total_tokens={"input_tokens": 100, "output_tokens": 50},
        ))
        storage.save_summary(PipelineRunSummary(
            run_id="r2",
            total_tokens={"input_tokens": 200, "output_tokens": 100},
        ))
        totals = storage.get_total_tokens()
        assert totals["input_tokens"] == 300
        assert totals["output_tokens"] == 150
