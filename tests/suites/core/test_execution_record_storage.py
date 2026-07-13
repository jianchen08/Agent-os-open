"""M12c 执行记录存储 + TrackPlugin 增强测试。

测试 ExecutionRecordStorage 的 CRUD 操作和 TrackPlugin
增加执行记录持久化写入后的行为（有/无 execution_record_storage 服务）。
"""

from __future__ import annotations

import json
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

    def test_delete_by_session_lazy_loaded_disk_files(self, tmp_path: Path):
        """回归测试：懒加载/服务重启场景下 delete_by_session 必须删除磁盘文件。

        BUG-FIX-fix_20260627_delete_lazy_loaded:
        问题根因: delete_by_session 缺少 _ensure_loaded 调用。ExecutionRecordStorage
                  采用懒加载（构造函数不调 _load_all），self._records 仅在访问时
                  由 _ensure_loaded 填充。服务重启后或会话消息从未被读取时，
                  self._records 对该 pipeline 为空，导致文件清理守卫
                  `if to_delete` 失败，磁盘 YAML、子目录、_pipeline_root_map
                  全部残留——表现为"前端删除会话后后端文件从未删除"。
        复现方式: 写入磁盘文件后，新建一个指向同目录的 storage 实例模拟服务
                  重启（此时新实例 self._records 为空），调用 delete_by_session。
        修复方案: 在 delete_by_session 顶部调用 _ensure_loaded(session_id)，
                  与 list_by_session/count_by_session 等所有兄弟访问器保持一致。
        """
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))
        for i in range(5):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i,
                iteration=i,
            ))
        yaml_file = data_dir / "run-001.yaml"
        assert yaml_file.exists()

        # 模拟服务重启：新实例的 self._records 为空（懒加载未触发）
        restarted = ExecutionRecordStorage(data_dir=str(data_dir))
        assert len(restarted._records) == 0  # noqa: SLF001 — 验证懒加载前提成立

        deleted = restarted.delete_by_session("run-001")

        # 返回值反映磁盘真实记录数（而非空的内存缓存）
        assert deleted == 5
        # 磁盘文件必须被实际删除
        assert not yaml_file.exists()

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

    def test_tail_read_large_file_supplements_window_to_fill_limit(self, tmp_path: Path):
        """回归测试：大文件 + 偏大 record，补充循环扩展窗口凑够 limit。

        BUG（fix tail-read-shortfall）:
            首次加载走尾部固定窗口读取。原实现窗口上限 128KB 且只重试一次，
            当 record 偏大（长 AI 回复 / 大 tool 输出，>2.6KB/条）时，
            128KB 装不下前端要的 50 条，循环会提前 break，返回不足 50 条；
            又因 records 非空不触发 fallback，前端首屏显示条数偏少。

        修复方案: _extract_tail_blocks 改为补充循环，起始 128KB，不够每次
                  再向前扩 128KB，直到凑够 n 个起点或覆盖整个文件。
        修复日期: 2026-06-29
        """
        data_dir = tmp_path / "pipelines"
        data_dir.mkdir()
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 80 条 record，每条 content ~4KB → 单条 record YAML 文本 > 4KB，
        # 128KB 起始窗口仅能装 ~30 条，远不够 limit=50，必须扩展窗口才能凑够。
        padding = "x" * 4000
        big_run = data_dir / "run-big.yaml"
        big_run.write_text(
            "summary: null\nrecords:\n"
            + "\n".join(
                f"- record_id: r{i:03d}\n  pipeline_run_id: run-big\n  type: ai\n"
                f"  sequence: {i}\n  iteration: 0\n  role: assistant\n"
                f"  content: {padding}-{i}\n"
                for i in range(1, 81)
            ),
            encoding="utf-8",
        )
        # 文件足够大（>256KB），单次 128KB 窗口必装不下 50 条
        assert big_run.stat().st_size > 256 * 1024

        # _extract_tail_blocks 直接调用：n=50 时应扩展窗口凑够 50 个块
        blocks = storage._extract_tail_blocks(big_run, n=50)
        assert len(blocks) == 50
        assert blocks[-1].startswith("- record_id: r080")

        # limit=50：补充循环应扩展窗口直到凑够 50 条（sequence 31..80）
        records, has_more = storage.list_by_pipeline("run-big", limit=50)
        assert len(records) == 50
        assert [r.sequence for r in records] == list(range(31, 81))
        # 还有更早记录（1..30），故 has_more=True
        assert has_more is True

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


class TestClonePipelineRecords:
    """验证 pipe 继承的物理拷贝路径（clone_pipeline_records）。

    取代了原 _persist_inherited_history 机制：继承不再走
    「messages → 落盘」往返，而是直接把源管道 records 物理拷贝到目标管道，
    文本行替换 pipeline_run_id / container_task_id。
    """

    @staticmethod
    def _make_src_storage(
        tmp_path: Path, src_pid: str,
        num_records: int = 3,
        with_tool_calls_json: bool = False,
    ) -> tuple[ExecutionRecordStorage, list[ExecutionRecordData]]:
        """用 tmp_path 创建含源记录的 storage 实例。"""
        storage = ExecutionRecordStorage(data_dir=str(tmp_path))
        storage.register_pipeline(src_pid, src_pid)
        created: list[ExecutionRecordData] = []
        for i in range(num_records):
            rec = ExecutionRecordData(
                record_id=f"rec_{i:04d}",
                pipeline_run_id=src_pid,
                type="ai",
                sequence=i + 1,
                iteration=i,
                role="assistant",
                content=f"msg_{i}",
                container_task_id=f"old_task_{i}",
            )
            if with_tool_calls_json and i == 0:
                rec.tool_calls_json = json.dumps([
                    {"id": "call_1", "name": "file_read", "arguments": '{"path": "a.py"}'},
                ])
            storage.save(rec)
            created.append(rec)
        return storage, created

    def test_clone_replaces_two_fields_and_keeps_record_id(
        self, tmp_path,
    ):
        """克隆后 pipeline_run_id / container_task_id 全部替换，
        record_id 保留不动（不同管道间无全局唯一性约束）。"""
        src_pid, dst_pid, dst_ctid = "src-run", "dst-run", "new-task-001"
        storage, src_records = self._make_src_storage(tmp_path, src_pid)

        count = storage.clone_pipeline_records(src_pid, dst_pid, dst_ctid)

        dst_records, _ = storage.list_by_pipeline(dst_pid)
        assert count == len(dst_records) == len(src_records)
        assert all(r.pipeline_run_id == dst_pid for r in dst_records)
        assert all(r.container_task_id == dst_ctid for r in dst_records)
        # record_id 保留不动
        assert {r.record_id for r in dst_records} == {r.record_id for r in src_records}

    def test_clone_source_not_modified(
        self, tmp_path,
    ):
        """克隆不修改源管道的任何记录（数据安全红线）。"""
        src_pid = "src-run"
        storage, src_records = self._make_src_storage(tmp_path, src_pid)
        src_before = [(r.record_id, r.pipeline_run_id, r.container_task_id) for r in src_records]

        storage.clone_pipeline_records(src_pid, "dst-run", "new-task-001")

        src_after_records, _ = storage.list_by_pipeline(src_pid)
        src_after = [(r.record_id, r.pipeline_run_id, r.container_task_id) for r in src_after_records]
        assert src_before == src_after

    def test_clone_tool_calls_json_format_preserved(
        self, tmp_path,
    ):
        """克隆保持 tool_calls_json 的原始格式（扁平结构不变形）。

        这是本重构的核心目的：消除「messages 往返导致 tool_calls 格式
        从扁平变嵌套」的根因。源是什么格式，克隆后就是什么格式。
        """
        src_pid = "src-run"
        storage, _ = self._make_src_storage(tmp_path, src_pid, with_tool_calls_json=True)

        storage.clone_pipeline_records(src_pid, "dst-run", "new-task-001")

        dst_records, _ = storage.list_by_pipeline("dst-run")
        ai_with_tc = [r for r in dst_records if r.type == "ai" and r.tool_calls_json]
        assert len(ai_with_tc) >= 1
        for r in ai_with_tc:
            parsed = json.loads(r.tool_calls_json)
            assert parsed, "tool_calls_json 解析为空"
            assert "name" in parsed[0], (
                f"克隆后 tool_calls_json 顶层缺 name（格式变形）: {parsed[0]}"
            )

    def test_clone_empty_source_raises(self, tmp_path):
        """源管道无记录时克隆应报错（而非静默成功）。"""
        storage = ExecutionRecordStorage(data_dir=str(tmp_path))
        storage.register_pipeline("empty-src", "empty-src")
        with pytest.raises(ValueError, match="无执行记录"):
            storage.clone_pipeline_records("empty-src", "dst-run", "new-task-001")

    def test_clone_round_trip_preserves_all_fields(
        self, tmp_path,
    ):
        """解析→修改→序列化 round-trip 后所有 record 字段保留完整。"""
        src_pid = "src-run"
        storage, src_records = self._make_src_storage(tmp_path, src_pid, num_records=5)

        storage.clone_pipeline_records(src_pid, "dst-run", "new-task-001")

        dst_records, _ = storage.list_by_pipeline("dst-run")
        assert len(dst_records) == 5
        # 与源相比，只有 pipeline_run_id / container_task_id 变化，其余字段不变
        for src, dst in zip(src_records, dst_records):
            assert src.record_id == dst.record_id
            assert src.type == dst.type
            assert src.sequence == dst.sequence
            assert src.role == dst.role
            assert src.content == dst.content

    def test_clone_failure_rolls_back_all_residue(
        self, tmp_path, monkeypatch,
    ):
        """clone 验证失败时回滚：删除目标文件、目录、root_map 映射。

        失败后不留垃圾，确保后续重试/继承从干净状态开始。
        """
        src_pid = "src-run"
        storage, _ = self._make_src_storage(tmp_path, src_pid, num_records=3)
        dst_pid = "dst-run"

        # 篡改序列化结果，使第一条记录的 pipeline_run_id 仍是源值 → 验证失败
        import infrastructure.execution_record_storage as ers_mod
        orig_dump = ers_mod.yaml.safe_dump

        def broken_dump(data, **kw):
            text = orig_dump(data, **kw)
            # 把第一条的 pipeline_run_id 改回源值，制造验证失败
            return text.replace(
                f"pipeline_run_id: {dst_pid}",
                f"pipeline_run_id: {src_pid}",
                1,
            )

        monkeypatch.setattr(ers_mod.yaml, "safe_dump", broken_dump)

        with pytest.raises(ValueError, match="pipeline_run_id 仍为"):
            storage.clone_pipeline_records(src_pid, dst_pid, "new-task-001")

        # 验证回滚干净：目标目录不存在或为空
        dst_dir = tmp_path / dst_pid
        if dst_dir.exists():
            assert not any(dst_dir.iterdir()), "目标目录仍有残留文件"
        # root_map 不含目标映射
        assert dst_pid not in storage._pipeline_root_map
        # 内存分片状态已清理
        assert dst_pid not in storage._active_part
        assert dst_pid not in storage._records_in_active_file
        # 源未受影响
        src_records, _ = storage.list_by_pipeline(src_pid)
        assert len(src_records) == 3

    def test_clone_root_task_id_matches_engine_registration(
        self, tmp_path,
    ):
        """clone 用 root_task_id 作目录，与引擎后续 register_pipeline 一致。

        回归 BUG-fix_20260625：clone 写到 {target_pipeline_id}/ 目录，
        但 _bind_pipeline_run 又 register_pipeline(pipeline_id, root_task_id)
        触发文件迁移，导致 clone 文件和引擎读取文件分裂、继承历史丢失。
        修复：clone 时直接用 root_task_id 作目录 root。
        """
        src_pid, dst_pid, dst_ctid = "src-run", "dst-run", "new-task-001"
        root_task_id = "root-task-xyz"
        storage, _ = self._make_src_storage(tmp_path, src_pid, num_records=4)

        # clone 时用 root_task_id（模拟 task_submit 传入）
        storage.clone_pipeline_records(
            src_pid, dst_pid, dst_ctid, root_task_id=root_task_id,
        )

        # clone 后文件应在 {root_task_id}/ 目录下
        dst_dir = tmp_path / root_task_id
        assert dst_dir.exists(), f"clone 文件应在 {root_task_id}/ 目录下"
        dst_files = list(dst_dir.glob(f"{dst_pid}*.yaml"))
        assert dst_files, "目标文件应存在于 root_task_id 目录"

        # 模拟引擎注册：_bind_pipeline_run 调 register_pipeline(pipeline_id, root_task_id)
        # 应幂等（root 相同），不触发文件迁移
        storage.register_pipeline(dst_pid, root_task_id)

        # 验证：文件仍在原位，能正确读出全部克隆记录
        dst_records, _ = storage.list_by_pipeline(dst_pid)
        assert len(dst_records) == 4, (
            f"register_pipeline 后记录应完整，实际 {len(dst_records)} 条"
        )
        assert all(r.pipeline_run_id == dst_pid for r in dst_records)
        assert all(r.container_task_id == dst_ctid for r in dst_records)

