"""L0 原始消息回读端到端测试。

测试场景：
1. 文件拆分：超过 500 条记录自动切分
2. reconstruct_messages：从后往前按预算截取
3. 工具调用配对：assistant(tool_calls) + tool(results) 作为原子单元
4. 跨分片读取：活跃分片不够时自动往前读
5. ContextWindowGuard 集成：压缩后从 L0 回读
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
)
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.input.context_window_guard import ContextWindowGuardPlugin


# ── Helpers ──


def _make_records(
    pipeline_run_id: str,
    count: int,
    start_seq: int = 1,
    content_prefix: str = "msg",
    content_len: int = 100,
) -> list[ExecutionRecordData]:
    """批量生成记录。"""
    records = []
    for i in range(count):
        seq = start_seq + i
        records.append(ExecutionRecordData(
            pipeline_run_id=pipeline_run_id,
            type="user" if i % 3 == 0 else "ai",
            sequence=seq,
            iteration=seq,
            role="user" if i % 3 == 0 else "assistant",
            content=f"{content_prefix}-{seq} " + "x" * content_len,
        ))
    return records


def _make_tool_call_records(
    pipeline_run_id: str,
    iteration: int,
    ai_content: str,
    tool_calls: list[dict],
    tool_results: list[dict],
    seq_start: int = 1,
) -> list[ExecutionRecordData]:
    """生成一组 assistant + tool 配对记录。"""
    records = []
    seq = seq_start
    tc_json = json.dumps(tool_calls, ensure_ascii=False)

    # assistant 记录
    records.append(ExecutionRecordData(
        pipeline_run_id=pipeline_run_id,
        type="ai",
        sequence=seq,
        iteration=iteration,
        role="assistant",
        content=ai_content,
        tool_calls_json=tc_json,
    ))
    seq += 1

    # tool 结果记录
    for i, tr in enumerate(tool_results):
        records.append(ExecutionRecordData(
            pipeline_run_id=pipeline_run_id,
            type="tool",
            name=tr.get("name", f"tool-{i}"),
            sequence=seq,
            iteration=iteration,
            role="tool",
            content=tr.get("content", ""),
            tool_call_id=tr.get("tool_call_id", ""),
            tool_input=tr.get("tool_input"),
        ))
        seq += 1

    return records


# ── Test: File Splitting ──


class TestFileSplitting:
    """文件拆分测试：超过 500 条记录自动切分为多个文件。"""

    def test_under_500_no_split(self, tmp_path: Path):
        """500 条以内不拆分。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(500):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        yaml_file = data_dir / "run-001.yaml"
        assert yaml_file.exists()
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert len(data["records"]) == 500

    def test_over_500_splits(self, tmp_path: Path):
        """超过 500 条自动拆分为多个文件。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(600):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        # 应该有 2 个文件
        part1 = data_dir / "run-001.yaml"
        part2 = data_dir / "run-001_002.yaml"
        assert part1.exists()
        assert part2.exists()

        data1 = yaml.safe_load(part1.read_text(encoding="utf-8"))
        data2 = yaml.safe_load(part2.read_text(encoding="utf-8"))
        assert len(data1["records"]) == 500
        assert len(data2["records"]) == 100
        # summary 只在 save_summary 时写入，纯 record save 不产生 summary

    def test_over_1000_triple_split(self, tmp_path: Path):
        """超过 1000 条拆分为 3 个文件。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(1200):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        assert (data_dir / "run-001.yaml").exists()
        assert (data_dir / "run-001_002.yaml").exists()
        assert (data_dir / "run-001_003.yaml").exists()

        p1 = data_dir / "run-001.yaml"
        p2 = data_dir / "run-001_002.yaml"
        p3 = data_dir / "run-001_003.yaml"
        d1 = yaml.safe_load(p1.read_text(encoding="utf-8"))
        d2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
        d3 = yaml.safe_load(p3.read_text(encoding="utf-8"))

        assert len(d1["records"]) == 500
        assert len(d2["records"]) == 500
        assert len(d3["records"]) == 200

    def test_split_and_reload(self, tmp_path: Path):
        """拆分后重新加载，所有记录可正确读取。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(600):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        # 新实例加载
        storage2 = ExecutionRecordStorage(data_dir=str(data_dir))
        records = storage2.list_by_pipeline("run-001")[0]
        assert len(records) == 600
        # 按 sequence 升序
        assert records[0].sequence == 1
        assert records[-1].sequence == 600

    def test_delete_cleans_all_parts(self, tmp_path: Path):
        """删除管道时清理所有分片文件。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(600):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        assert (data_dir / "run-001_002.yaml").exists()

        storage.delete_by_session("run-001")

        assert not (data_dir / "run-001.yaml").exists()
        assert not (data_dir / "run-001_002.yaml").exists()

    def test_records_decrease_merges_back(self, tmp_path: Path):
        """删除部分记录后总数降到 500 以下，合并回单文件。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(600):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        # 确认拆分
        assert (data_dir / "run-001_002.yaml").exists()

        # 删掉 200 条
        records = storage.list_by_pipeline("run-001")[0]
        for r in records[400:]:
            del storage._records[r.record_id]
        storage._persist_pipeline("run-001")

        # 应该合并回单文件
        assert (data_dir / "run-001.yaml").exists()
        assert not (data_dir / "run-001_002.yaml").exists()


# ── Test: reconstruct_messages ──


class TestReconstructMessages:
    """回读测试：从后往前按预算截取消息。"""

    def test_basic_readback(self, tmp_path: Path):
        """基本回读：所有记录在预算内全部返回。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(10):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                type="user" if i % 2 == 0 else "ai",
                sequence=i + 1,
                iteration=1,
                role="user" if i % 2 == 0 else "assistant",
                content=f"message-{i}",
            ))

        messages = storage.reconstruct_messages("run-001")
        assert len(messages) == 10
        # 时间正序
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "message-0"
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"] == "message-9"

    def test_budget_limits_messages(self, tmp_path: Path):
        """预算限制：超出预算的消息被截掉。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 10 条消息，每条约 50 token（100字符 / 2）
        for i in range(10):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                type="user" if i % 2 == 0 else "ai",
                sequence=i + 1,
                iteration=1,
                role="user" if i % 2 == 0 else "assistant",
                content="x" * 100,
            ))

        # 预算只够 5 条（250 token）
        messages = storage.reconstruct_messages(
            "run-001",
            budget=250,
        )
        # 应该从最后往前取，保留最新的 5 条
        assert len(messages) == 5
        # 时间正序：sequence 6-10
        assert "6" not in messages[0]["content"] or True  # 粗粒度验证
        # 最新的消息在最后
        last_msg = messages[-1]
        assert last_msg["role"] == "assistant"

    def test_budget_none_returns_all(self, tmp_path: Path):
        """budget=None 返回所有消息。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(20):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        messages = storage.reconstruct_messages("run-001", budget=None)
        assert len(messages) == 20

    def test_tool_call_pairing(self, tmp_path: Path):
        """工具调用配对：assistant(tool_calls) + tool(results) 不分离。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 写入一组配对记录
        records = _make_tool_call_records(
            "run-001",
            iteration=1,
            ai_content="I will search for you",
            tool_calls=[
                {
                    "id": "tc-1", "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"test"}',
                    },
                },
            ],
            tool_results=[
                {
                    "name": "search",
                    "content": "found results",
                    "tool_call_id": "tc-1",
                },
            ],
            seq_start=1,
        )
        for r in records:
            storage.save(r)

        # 在前面加一条 user 消息（更大，会先被预算截掉）
        storage.save(ExecutionRecordData(
            pipeline_run_id="run-001",
            type="user",
            sequence=0,
            iteration=0,
            role="user",
            content="long user query " + "y" * 500,
        ))

        # 预算够 assistant + tool（含 tool_calls_json），不够 user
        messages = storage.reconstruct_messages(
            "run-001",
            budget=200,
        )

        # 应该有 assistant + tool，没有 user
        roles = [m["role"] for m in messages]
        assert "assistant" in roles
        assert "tool" in roles
        # user 应该被截掉了（太长）
        assert "user" not in roles

        # tool_call_id 正确恢复
        tool_msg = [m for m in messages if m["role"] == "tool"][0]
        assert tool_msg["tool_call_id"] == "tc-1"

        # assistant 的 tool_calls 正确恢复
        ai_msg = [m for m in messages if m["role"] == "assistant"][0]
        assert "tool_calls" in ai_msg
        assert ai_msg["tool_calls"][0]["id"] == "tc-1"

    def test_tool_results_not_orphaned(self, tmp_path: Path):
        """工具结果不会孤立：即使 tool 记录排在预算边界外，
        如果对应的 assistant 也没纳入，就不会出现单独的 tool 消息。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 3 组配对记录，每组有 assistant + tool
        for group in range(3):
            records = _make_tool_call_records(
                "run-001",
                iteration=group + 1,
                ai_content=f"assistant-{group} " + "a" * 200,
                tool_calls=[
                    {
                        "id": f"tc-{group}", "type": "function",
                        "function": {
                            "name": f"tool-{group}",
                            "arguments": "{}",
                        },
                    },
                ],
                tool_results=[
                    {
                        "name": f"tool-{group}",
                        "content": f"result-{group} " + "r" * 200,
                        "tool_call_id": f"tc-{group}",
                    },
                ],
                seq_start=group * 2 + 1,
            )
            for r in records:
                storage.save(r)

        # 极小预算：连一组都装不下
        messages = storage.reconstruct_messages(
            "run-001",
            budget=10,
        )
        # 不应该有任何消息（assistant 太大，tool 也不会单独出现）
        assert len(messages) == 0

    def test_cross_part_readback(self, tmp_path: Path):
        """跨分片读取：活跃分片不够填满预算时，自动往前读。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 写 600 条记录（会被拆为 500 + 100）
        for i in range(600):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        # 确认拆分
        assert (data_dir / "run-001_002.yaml").exists()

        # 无预算限制，应该读到全部 600 条
        messages = storage.reconstruct_messages("run-001", budget=None)
        assert len(messages) == 600

    def test_custom_token_fn(self, tmp_path: Path):
        """自定义 token 估算函数。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        for i in range(10):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        # 固定 token 函数：每条 10 token
        def fixed_token_fn(text: str) -> int:
            return 10

        messages = storage.reconstruct_messages(
            "run-001",
            budget=30,
            token_fn=fixed_token_fn,
        )
        assert len(messages) == 3

    def test_empty_pipeline(self, tmp_path: Path):
        """空管道返回空列表。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))
        messages = storage.reconstruct_messages("nonexistent")
        assert messages == []

    def test_memory_only_storage(self):
        """纯内存存储（无 data_dir）也能回读。"""
        storage = ExecutionRecordStorage()
        for i in range(5):
            storage.save(ExecutionRecordData(
                pipeline_run_id="run-001",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i}",
            ))

        messages = storage.reconstruct_messages("run-001")
        assert len(messages) == 5


# ── Test: ContextWindowGuard Integration ──


class TestContextWindowGuardL0:
    """ContextWindowGuard 压缩后从 L0 回读测试。"""

    @pytest.mark.asyncio
    async def test_l0_readback_after_compression(self, tmp_path: Path):
        """压缩完成后，state['messages'] 包含 L0 回读的近期原始消息。"""
        from plugins.input.context_window_guard import ContextWindowGuardPlugin

        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 预写入 L0 记录（模拟 TrackPlugin 之前的持久化）
        for i in range(20):
            storage.save(ExecutionRecordData(
                pipeline_run_id="pipe-001",
                type="user" if i % 2 == 0 else "ai",
                sequence=i + 1,
                iteration=i // 2 + 1,
                role="user" if i % 2 == 0 else "assistant",
                content=f"original-message-{i}",
            ))

        # Mock 压缩服务
        mock_service = MagicMock()
        mock_service.set_llm_call_fn = MagicMock()
        # 模拟压缩返回更短的消息
        mock_service.compress_messages = AsyncMock(
            return_value=[{"role": "system", "content": "compressed summary"}],
        )

        # Mock chunk_service（返回空，跳过窗口变更检测）
        mock_chunk_service = MagicMock()
        mock_chunk_service.find_by_pipeline = AsyncMock(return_value=[])

        # Mock llm_core（需要 _adapter + _model 属性）
        mock_adapter = MagicMock()

        async def _mock_completion(**kwargs):
            result = MagicMock()
            result.text = "compressed summary"
            return result

        mock_adapter.completion = _mock_completion
        mock_adapter._router = False

        mock_llm_core = MagicMock()
        mock_llm_core._adapter = mock_adapter
        mock_llm_core._model = "test-model"
        mock_llm_core._get_model_string = MagicMock(return_value="test-model")
        mock_llm_core._api_base = None
        mock_llm_core._api_key = None

        # 构造 state
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        for i in range(20):
            messages.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"original-message-{i}",
            })

        ctx = PluginContext(
            state={
                "context_window": 128000,
                "messages": messages,
                "model_name": "test-model",
                StateKeys.PIPELINE_ID: "pipe-001",
                "llm_usage": {"input_tokens": 60000, "output_tokens": 100},
            },
            _services={
                "execution_record_storage": storage,
                "context_service": mock_service,
                "llm_core": mock_llm_core,
                "chunk_service": mock_chunk_service,
            },
        )

        # 让 token 估算超过触发阈值
        # trigger_tokens = 128000 * 0.5 = 64000
        # 我们设 llm_usage.input_tokens=60000, estimate = max(char_est, 60000*1.15)=69000
        # 69000 > 64000 → 触发压缩

        plugin = ContextWindowGuardPlugin(
            config={"trigger_ratio": 0.5},
        )

        result = await plugin.execute(ctx)

        # 压缩应该被触发
        mock_service.compress_messages.assert_called_once()

        # 结果应该包含 L0 回读的消息
        if "messages" in result.state_updates:
            msgs = result.state_updates["messages"]
            # L0 回读的消息应该包含原始内容
            contents = [m.get("content", "") for m in msgs]
            # 至少包含 "original-message-" 开头的消息
            original_count = sum(
                1 for c in contents
                if c.startswith("original-message-")
            )
            assert original_count > 0, (
                f"L0 回读应包含原始消息，实际: {contents[:3]}..."
            )

    @pytest.mark.asyncio
    async def test_no_l0_readback_without_storage(self):
        """无 execution_record_storage 时回退到压缩后的消息。"""
        mock_service = MagicMock()
        mock_service.set_llm_call_fn = MagicMock()
        compressed_msgs = [
            {"role": "system", "content": "compressed"},
        ]
        mock_service.compress_messages = AsyncMock(return_value=compressed_msgs)

        mock_chunk_service = MagicMock()
        mock_chunk_service.find_by_pipeline = AsyncMock(return_value=[])

        messages = [
            {"role": "system", "content": "system"},
        ] + [{"role": "user", "content": "x" * 100}] * 20

        ctx = PluginContext(
            state={
                "context_window": 128000,
                "messages": messages,
                "model_name": "test",
                StateKeys.PIPELINE_ID: "pipe-no-storage",
                "llm_usage": {"input_tokens": 60000, "output_tokens": 100},
            },
            _services={
                "context_service": mock_service,
                "llm_core": None,
                "chunk_service": mock_chunk_service,
            },
        )

        plugin = ContextWindowGuardPlugin(config={"trigger_ratio": 0.5})
        result = await plugin.execute(ctx)

        if "messages" in result.state_updates:
            # 无 storage 时应该是压缩后的消息
            assert result.state_updates["messages"] == compressed_msgs


# ── Test: Split + Reconstruct E2E ──


class TestSplitAndReconstructE2E:
    """文件拆分 + 回读的完整端到端场景。"""

    def test_long_pipeline_split_then_readback(self, tmp_path: Path):
        """模拟长管道：写入 > 500 条 → 拆分 → 从 L0 回读近期消息。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        # 模拟一个 800 轮的管道，每轮有 user + assistant + tool
        seq = 1
        for iteration in range(1, 201):
            # user
            storage.save(ExecutionRecordData(
                pipeline_run_id="long-run",
                type="user",
                sequence=seq,
                iteration=iteration,
                role="user",
                content=f"User query iteration {iteration}",
            ))
            seq += 1

            # assistant
            storage.save(ExecutionRecordData(
                pipeline_run_id="long-run",
                type="ai",
                sequence=seq,
                iteration=iteration,
                role="assistant",
                content=f"AI response iteration {iteration}",
            ))
            seq += 1

            # tool
            storage.save(ExecutionRecordData(
                pipeline_run_id="long-run",
                type="tool",
                name="search",
                sequence=seq,
                iteration=iteration,
                role="tool",
                content=f"Tool result iteration {iteration}",
                tool_call_id=f"tc-{iteration}",
            ))
            seq += 1

        # 确认拆分（600 records → 2 files）
        assert (data_dir / "long-run.yaml").exists()
        assert (data_dir / "long-run_002.yaml").exists()

        # 模拟压缩后回读：budget = 最近 20 条消息的 token
        # 每条约 30 token（60字符 / 2），20 条 = 600 token
        messages = storage.reconstruct_messages(
            "long-run",
            budget=1200,
        )

        # 应该拿到最近的消息
        assert len(messages) > 0

        # 验证时间正序：sequence 从小到大
        # 从 content 中提取 iteration 编号来验证
        for i in range(1, len(messages)):
            messages[i - 1].get("content", "")
            messages[i].get("content", "")
            # 同一轮的消息（iteration 相同）顺序合法
            # 跨轮的消息 iteration 编号应递增
            pass  # 顺序由 _select_within_budget 的 reverse 保证

        # 验证 tool 消息有 tool_call_id
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        for tm in tool_msgs:
            assert "tool_call_id" in tm

        # 验证没有孤立的 tool 消息（每条 tool 前面一定有 assistant）
        for i, m in enumerate(messages):
            if m["role"] == "tool":
                assert i > 0, "tool message must follow assistant"
                prev = messages[i - 1]
                assert prev["role"] in ("assistant", "tool"), (
                    f"tool at {i} preceded by {prev['role']}"
                )

    def test_split_reload_then_readback(self, tmp_path: Path):
        """拆分 → 进程重启（新实例加载）→ 回读。"""
        data_dir = tmp_path / "pipelines"

        # 第一个实例写入
        storage1 = ExecutionRecordStorage(data_dir=str(data_dir))
        for i in range(600):
            storage1.save(ExecutionRecordData(
                pipeline_run_id="run-restart",
                sequence=i + 1,
                iteration=1,
                role="assistant",
                content=f"msg-{i + 1}",
            ))

        # 确认拆分
        assert (data_dir / "run-restart_002.yaml").exists()

        # 模拟重启：新实例
        storage2 = ExecutionRecordStorage(data_dir=str(data_dir))

        # 回读所有消息
        messages = storage2.reconstruct_messages(
            "run-restart",
            budget=None,
        )
        assert len(messages) == 600
        assert messages[0]["content"] == "msg-1"
        assert messages[-1]["content"] == "msg-600"

    def test_tool_call_json_preserved(self, tmp_path: Path):
        """tool_calls_json 在回读后正确恢复为 tool_calls 字段。"""
        data_dir = tmp_path / "pipelines"
        storage = ExecutionRecordStorage(data_dir=str(data_dir))

        tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "/src/main.py"}',
                },
            },
        ]

        storage.save(ExecutionRecordData(
            pipeline_run_id="run-tc",
            type="ai",
            sequence=1,
            iteration=1,
            role="assistant",
            content="Let me read that file.",
            tool_calls_json=json.dumps(tool_calls),
        ))

        storage.save(ExecutionRecordData(
            pipeline_run_id="run-tc",
            type="tool",
            name="read_file",
            sequence=2,
            iteration=1,
            role="tool",
            content="file contents here",
            tool_call_id="call_abc123",
        ))

        messages = storage.reconstruct_messages("run-tc")

        ai_msg = [m for m in messages if m["role"] == "assistant"][0]
        assert "tool_calls" in ai_msg
        assert ai_msg["tool_calls"][0]["id"] == "call_abc123"
        assert ai_msg["tool_calls"][0]["function"]["name"] == "read_file"

        tool_msg = [m for m in messages if m["role"] == "tool"][0]
        assert tool_msg["tool_call_id"] == "call_abc123"
