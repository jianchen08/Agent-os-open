"""TrackPlugin AI 记录 record_id 契约测试（id 一致性）。

背景 BUG（fix_20260625_ai_record_id_duplicate 修订）：
原方案给多轮迭代（iteration>1）的 ai 记录 record_id 追加 #iteration 后缀，
导致 record_id 与前端 stream_start 下发的裸 bridge message_id 不一致，
前端 initFromAPI 精确 id 对账失败，乐观占位符残留。

修订后：record_id 始终等于 bridge 的裸 message_id（id 契约），同 record_id
多轮记录的覆盖问题由 ExecutionRecordStorage 的组合 key 解决。

本测试锁定核心契约：
1. _resolve_ai_record_id 始终返回 bridge 的裸 message_id，不分轮次、不加后缀。
2. bridge 不可用时回退到 preset_record_id（调用方传入）。
3. 端到端：多轮迭代落盘后，storage 中 ai 记录 record_id 全是裸 hex、无 #。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.track.plugin import TrackPlugin


def _make_plugin() -> TrackPlugin:
    return TrackPlugin(config={"enabled": True, "track_token_usage": False, "track_execution_time": False})


class TestResolveAiRecordId:
    """_resolve_ai_record_id 的 id 契约（改动 2 的核心保证）。"""

    def test_bridge_available_returns_bare_message_id(self):
        """bridge 可用时，返回 bridge.message_id（裸 hex），不加任何后缀。"""
        plugin = _make_plugin()
        bridge_msg_id = "abc123def456"  # 裸 hex，无后缀

        mock_entry = MagicMock()
        mock_entry.bridge = MagicMock()
        mock_entry.bridge.message_id = bridge_msg_id
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry

        with patch("pipeline.registry.get_engine_registry", return_value=mock_registry):
            result = plugin._resolve_ai_record_id("pipe-001", "preset_fallback_id")

        assert result == bridge_msg_id
        assert "#" not in result  # 绝无 #iteration 后缀

    def test_bridge_available_ignores_iteration_in_caller(self):
        """调用方不再因 iteration 给 record_id 加后缀（改动 2 删除了该逻辑）。

        通过源码静态检查确认：track/plugin.py 中不再存在 '#{' + iteration 的拼接。
        """
        import inspect

        from plugins.output.track import plugin as track_module

        source = inspect.getsource(track_module)
        # 不应存在给 ai_record_id 追加 #iteration 后缀的代码
        assert 'ai_record_id}#{iteration}' not in source, (
            "track/plugin.py 仍存在 ai_record_id 加 #iteration 后缀的逻辑，"
            "会破坏 id 契约（与 WS message_id 不一致）"
        )

    def test_bridge_unavailable_falls_back_to_preset(self):
        """bridge 不可用时，回退到调用方传入的 preset_record_id。"""
        plugin = _make_plugin()
        mock_registry = MagicMock()
        mock_registry.get.return_value = None  # entry 不存在
        with patch("pipeline.registry.get_engine_registry", return_value=mock_registry):
            result = plugin._resolve_ai_record_id("pipe-001", "preset_bare_id")
        assert result == "preset_bare_id"


class TestEndToEndRecordIdContract:
    """端到端：多轮迭代落盘后 record_id 全是裸 hex。

    通过 _try_persist_record 验证：模拟同一 bridge message_id 的多轮 LLM 输出，
    storage 中所有 ai 记录的 record_id 都等于裸 message_id，无 #iteration 后缀。
    """

    @pytest.mark.asyncio
    async def test_multi_iteration_ai_records_keep_bare_record_id(self, tmp_path):
        from infrastructure.execution_record_storage import (
            ExecutionRecordData,
            ExecutionRecordStorage,
        )

        storage = ExecutionRecordStorage(data_dir=str(tmp_path))
        pipeline_run_id = "pipe-e2e-001"
        bridge_msg_id = "e2e_bare_id01"

        # mock registry：bridge 返回固定裸 message_id，entry.next_sequence 递增
        seq_counter = {"n": 0}

        def fake_next_sequence():
            seq_counter["n"] += 1
            return seq_counter["n"]

        mock_entry = MagicMock()
        mock_entry.bridge = MagicMock()
        mock_entry.bridge.message_id = bridge_msg_id
        mock_entry.next_sequence = fake_next_sequence
        mock_entry.msg_sequence = seq_counter["n"]
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry

        plugin = _make_plugin()

        with patch("pipeline.registry.get_engine_registry", return_value=mock_registry):
            # 模拟 3 轮迭代，每轮都有 LLM 输出
            for iteration in (1, 2, 3):
                ctx = PluginContext(
                    state={
                        StateKeys.PIPELINE_ID: pipeline_run_id,
                        StateKeys.CORE_TYPE: "llm_call",
                        StateKeys.ITERATION: iteration,
                        StateKeys.RAW_RESULT: f"reply iter {iteration}",
                        StateKeys.EXECUTION_STATUS: "completed",
                        "preset_ai_record_id": bridge_msg_id,
                    },
                    config={},
                    _services={"execution_record_storage": storage},
                )
                await plugin._try_persist_record(ctx, elapsed=0.1)

        # 取出所有 ai 记录
        records, _ = storage.list_by_pipeline(pipeline_run_id, limit=None)
        ai_records = [r for r in records if r.type == "ai"]

        # 核心断言：3 轮迭代各落一条 ai 记录（组合 key 不覆盖）
        assert len(ai_records) == 3, f"期望 3 条 ai 记录，实际 {len(ai_records)}"

        # 所有 record_id 都是裸 bridge message_id，无 # 后缀（id 契约恢复）
        for r in ai_records:
            assert r.record_id == bridge_msg_id, (
                f"record_id 应为裸 {bridge_msg_id}，实际 {r.record_id}"
            )
            assert "#" not in r.record_id

        # sequence 各不相同（3 轮递增）
        seqs = [r.sequence for r in ai_records]
        assert seqs == [1, 2, 3]
