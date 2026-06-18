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
from pipeline.types import StateKeys
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

        records = storage.list_by_pipeline("run-001")[0]
        assert len(records) == 5
        # 按 sequence 升序排列
        assert [r.sequence for r in records] == [0, 1, 2, 3, 4]

        records_2 = storage.list_by_pipeline("run-002")[0]
        assert len(records_2) == 3

    def test_list_by_pipeline_empty(self, storage: ExecutionRecordStorage):
        """测试列出不存在管道运行的记录返回空列表。"""
        records, _has_more = storage.list_by_pipeline("nonexistent")
        assert records == []

    def test_delete_by_session(self, storage: ExecutionRecordStorage):
        """测试按会话删除记录（兼容接口，内部匹配 pipeline_run_id）。"""
        for i in range(5):
            storage.save(ExecutionRecordData(pipeline_run_id="run-001", sequence=i, iteration=i))
        for i in range(3):
            storage.save(ExecutionRecordData(pipeline_run_id="run-002", sequence=i, iteration=i))

        deleted = storage.delete_by_session("run-001")
        assert deleted == 5
        assert len(storage.list_by_pipeline("run-001")[0]) == 0
        assert len(storage.list_by_pipeline("run-002")[0]) == 3

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
        records = storage2.list_by_pipeline("run-001")[0]
        assert len(records) == 1
        assert records[0].content == "test output"
        assert records[0].type == "ai"

    def test_memory_only_storage(self, storage: ExecutionRecordStorage):
        storage.save(ExecutionRecordData(pipeline_run_id="s1", sequence=1, iteration=1))
        assert len(storage.list_by_pipeline("s1")[0]) == 1

    def test_tail_read_small_chunked_file(self, tmp_path: Path):
        """回归测试：小文件（<64KB）的 chunked 分片也能被尾部读取。

        BUG-FIX-fix_20260606_chunked_small_file_read:
        问题根因: _extract_tail_blocks 在第一次尝试窗口小于文件大小时，
                  即使读到整个文件也找不到 n 个 record 起点，于是进入第二次
                  尝试用 _TAIL_READ_BYTES_MAX（128KB）作为窗口，
                  f.seek(file_size - 128KB) 在小文件下越过文件起始位置，
                  触发 OSError 后静默返回空列表，导致 chunk 文件中的
                  记录（最关键的最新记录）完全不返回，前端无法渲染。
        修复方案: 第二次窗口也用 min(MAX, file_size) 限制，并新增
                  "本次窗口已覆盖整个文件" 的提前返回条件。
        影响范围: 所有切片的 pipeline 文件 + list_messages API 分页加载。
        修复日期: 2026-06-06
        """
        data_dir = tmp_path / "pipelines"
        data_dir.mkdir()
        storage = ExecutionRecordStorage(data_dir=str(data_dir))
        # 模拟 chunked 场景：手写一个 {run_id}_002.yaml（小文件），
        # 内容包含 N 条 record 且总大小 < 64KB。
        chunk_file = data_dir / "run-chunked_002.yaml"
        chunk_file.write_text(
            "summary: null\nrecords:\n"
            + "\n".join(
                f"- record_id: r{i:03d}\n  pipeline_run_id: run-chunked\n  type: ai\n  sequence: {i}\n  iteration: 0\n  role: assistant\n  content: msg-{i}\n"
                for i in range(1, 17)
            ),
            encoding="utf-8",
        )
        assert chunk_file.stat().st_size < 64 * 1024

        # 触发 _extract_tail_blocks 的两条路径：limit 大于文件 record 总数
        # 也能完整返回所有块（修复前会因 OSError 返回空列表）。
        blocks = storage._extract_tail_blocks(chunk_file, n=20)
        assert len(blocks) == 16
        assert blocks[-1].startswith("- record_id: r016")

        # list_by_pipeline 应能读到全部 16 条 record
        records, _has_more = storage.list_by_pipeline("run-chunked", limit=20)
        assert len(records) == 16
        assert records[-1].record_id == "r016"
        assert records[-1].sequence == 16

    def test_load_corrupted_file(self, tmp_path: Path):
        data_dir = tmp_path / "pipelines"
        data_dir.mkdir()
        (data_dir / "corrupted.yaml").write_text("not: valid: yaml: {{{", encoding="utf-8")

        storage = ExecutionRecordStorage(data_dir=str(data_dir))
        assert len(storage.list_by_pipeline("any")[0]) == 0


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
        records = storage.list_by_pipeline("run-persist")[0]
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

        records = storage.list_by_pipeline("run-err")[0]
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

        records = storage.list_by_pipeline("run-trunc")[0]
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

        records = storage.list_by_pipeline("run-multi")[0]
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


# ── 继承历史落盘测试（pipe 继承场景）──


class TestInheritedHistoryPersist:
    """验证 pipe 继承来的历史对话在新 pipeline 首次执行时被落盘。

    覆盖 _persist_inherited_history 的核心场景：
    1. 触发：_inherited_history=True + records 为空 → 历史落盘
    2. 幂等：第二次执行不重复落盘（_initialized_pipeline_ids 守卫）
    3. sequence 连续：历史落盘后事件保存续号
    4. 不触发：无 _inherited_history 标志（workspace-only 继承）不落盘
    """

    @pytest.mark.asyncio
    async def test_inherited_history_gets_persisted(
        self, base_state: dict[str, Any]
    ):
        """新 pipeline 携带继承历史时，历史消息应落盘到新 pipeline 文件。"""
        storage = ExecutionRecordStorage()
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": storage},
        )
        # 模拟 build_initial_state 装载的继承历史（3 条）+ 新 user_input（1 条）
        base_state[StateKeys.PIPELINE_ID] = "run-inherit"
        base_state[StateKeys.ITERATION] = 1
        base_state["_inherited_history"] = True
        base_state["messages"] = [
            {"role": "user", "content": "原始用户提问"},
            {"role": "assistant", "content": "原始 AI 回复"},
            {"role": "user", "content": "继承后的新输入"},
        ]
        base_state["user_input"] = "继承后的新输入"

        plugin = TrackPlugin()
        await plugin.execute(ctx)

        records = storage.list_by_pipeline("run-inherit")[0]
        # 3 条继承历史全部落盘，新 user_input 因去重不重复存
        assert len(records) == 3
        roles = [r.role for r in records]
        assert roles == ["user", "assistant", "user"]
        # iteration=0 标记为继承历史
        assert all(r.iteration == 0 for r in records)

    @pytest.mark.asyncio
    async def test_idempotent_on_second_execute(
        self, base_state: dict[str, Any]
    ):
        """同一 pipeline 第二次执行不重复落盘继承历史。"""
        storage = ExecutionRecordStorage()
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": storage},
        )
        base_state[StateKeys.PIPELINE_ID] = "run-idem"
        base_state["_inherited_history"] = True
        base_state["messages"] = [
            {"role": "user", "content": "历史消息"},
        ]

        plugin = TrackPlugin()
        await plugin.execute(ctx)  # 第一次：落盘
        base_state[StateKeys.ITERATION] = 2
        await plugin.execute(ctx)  # 第二次：应跳过（_initialized_pipeline_ids 守卫）

        records = storage.list_by_pipeline("run-idem")[0]
        # 只有 1 条历史，不会因第二次执行翻倍
        history_records = [r for r in records if r.iteration == 0]
        assert len(history_records) == 1

    @pytest.mark.asyncio
    async def test_sequence_continuous_after_history(
        self, base_state: dict[str, Any]
    ):
        """历史落盘后，本轮事件保存的 sequence 应续在历史号之后。"""
        storage = ExecutionRecordStorage()
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": storage},
        )
        base_state[StateKeys.PIPELINE_ID] = "run-seq"
        base_state[StateKeys.ITERATION] = 1
        base_state["_inherited_history"] = True
        base_state["messages"] = [
            {"role": "user", "content": "历史1"},
            {"role": "assistant", "content": "历史2"},
        ]
        # 本轮 LLM 事件
        base_state[StateKeys.RAW_RESULT] = "本轮 AI 回复"

        plugin = TrackPlugin()
        await plugin.execute(ctx)

        records = storage.list_by_pipeline("run-seq")[0]
        # 2 条历史（seq 1,2）+ 1 条本轮 AI（seq 3）
        assert len(records) == 3
        seqs = [r.sequence for r in records]
        assert seqs == [1, 2, 3]
        # 本轮事件的 iteration=1，历史 iteration=0
        assert records[-1].iteration == 1
        assert records[-1].type == "ai"

    @pytest.mark.asyncio
    async def test_no_inherited_history_flag_skips_persist(
        self, base_state: dict[str, Any]
    ):
        """无 _inherited_history 标志（如 workspace-only 继承）不触发历史落盘。

        判别依据：落盘的记录数应等于本轮事件产生的记录数，
        不会把 messages 列表里的内容额外落盘。
        （注意：现有 user 消息保存逻辑本身用 iteration=0，属正常约定，
        故不能用 iteration=0 区分历史；改用记录数对账。）
        """
        storage = ExecutionRecordStorage()
        ctx = PluginContext(
            state=base_state,
            _services={"execution_record_storage": storage},
        )
        base_state[StateKeys.PIPELINE_ID] = "run-ws-only"
        base_state[StateKeys.ITERATION] = 1
        # messages 里有 1 条，但没有 _inherited_history 标志
        base_state["messages"] = [{"role": "user", "content": "历史消息(不应被落盘)"}]
        base_state["user_input"] = "本轮真实输入"

        plugin = TrackPlugin()
        await plugin.execute(ctx)

        records = storage.list_by_pipeline("run-ws-only")[0]
        # 只应落盘本轮 user_input（iteration==1 的 user 保存逻辑），
        # messages 里的"历史消息(不应被落盘)"不应出现
        contents = [r.content for r in records]
        assert "本轮真实输入" in contents
        assert "历史消息(不应被落盘)" not in contents

