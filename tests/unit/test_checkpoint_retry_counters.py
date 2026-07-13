"""检查点 retry 计数持久化回归测试。

BUG-FIX-fix_20260702_transient_count_lost_on_suspend:
_DYNAMIC_STATE_KEYS 原不含 retry.transient_count / retry.count /
error_check.* → suspend/resume 周期中计数不持久化 → 恢复后归零 →
error_check 的 transient_max_retries=10 安全阀永远触发不了 →
上游持续 timeout 时管道无限重试无法 failed。

修复：将上述 4 个键加入 _DYNAMIC_STATE_KEYS。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrastructure.checkpoint.pipeline_checkpoint import (
    PipelineCheckpointManager,
    _DYNAMIC_STATE_KEYS,
)


@pytest.fixture
def manager(tmp_path: Path) -> PipelineCheckpointManager:
    return PipelineCheckpointManager(store_dir=str(tmp_path))


class TestRetryCountersPersisted:
    """retry/error_check 计数必须在 checkpoint round-trip 中保留。"""

    def test_keys_in_dynamic_state_keys(self) -> None:
        """修复 B 核心：4 个键必须在持久化白名单内。"""
        assert "retry.count" in _DYNAMIC_STATE_KEYS
        assert "retry.transient_count" in _DYNAMIC_STATE_KEYS
        assert "error_check.last_error_type" in _DYNAMIC_STATE_KEYS
        assert "error_check.consecutive_same_type" in _DYNAMIC_STATE_KEYS

    @pytest.mark.asyncio
    async def test_transient_count_survives_round_trip(self, manager: PipelineCheckpointManager) -> None:
        """transient_count=5 → save → load → 仍为 5（不为 0）。"""
        state = {
            "iteration": 3,
            "ended": False,
            "user_input": "",
            "messages": [],
            "pipeline_id": "pipe-test",
            "core_type": "llm_call",
            "agent_config_id": "test_agent",
            "retry.count": 2,
            "retry.transient_count": 5,
            "error_check.last_error_type": "core_error",
            "error_check.consecutive_same_type": 5,
        }

        checkpoint_id = await manager.save("pipe-test", state, phase="suspended")
        loaded = await manager.load(checkpoint_id)

        assert loaded is not None
        restored = loaded["state"]
        assert restored["retry.transient_count"] == 5, (
            "transient_count 必须 round-trip 保留，否则 suspend 后安全阀归零"
        )
        assert restored["retry.count"] == 2
        assert restored["error_check.last_error_type"] == "core_error"
        assert restored["error_check.consecutive_same_type"] == 5

    @pytest.mark.asyncio
    async def test_transient_count_zero_also_persisted(self, manager: PipelineCheckpointManager) -> None:
        """计数为 0 时也应正常持久化（区分"未设置"和"显式 0"）。"""
        state = {
            "pipeline_id": "p",
            "retry.transient_count": 0,
            "retry.count": 0,
        }

        checkpoint_id = await manager.save("p", state, phase="auto")
        loaded = await manager.load(checkpoint_id)

        assert loaded is not None
        assert loaded["state"]["retry.transient_count"] == 0
        assert loaded["state"]["retry.count"] == 0

    @pytest.mark.asyncio
    async def test_counter_not_persisted_when_absent_from_state(self, manager: PipelineCheckpointManager) -> None:
        """state 里没有这些键时，checkpoint 也不应凭空生成（保持稀疏）。"""
        state = {"pipeline_id": "p", "iteration": 1}

        checkpoint_id = await manager.save("p", state, phase="auto")
        loaded = await manager.load(checkpoint_id)

        assert loaded is not None
        # 没有设置就不应该出现
        assert "retry.transient_count" not in loaded["state"]

    @pytest.mark.asyncio
    async def test_numeric_types_preserved(self, manager: PipelineCheckpointManager) -> None:
        """计数 round-trip 后类型仍为 int（非 str），error_check.last_error_type 仍为 str。"""
        state = {
            "pipeline_id": "p",
            "retry.transient_count": 10,
            "error_check.last_error_type": "format_error",
        }

        checkpoint_id = await manager.save("p", state, phase="suspended")
        loaded = await manager.load(checkpoint_id)

        assert loaded is not None
        restored = loaded["state"]
        assert isinstance(restored["retry.transient_count"], int)
        assert isinstance(restored["error_check.last_error_type"], str)

    @pytest.mark.asyncio
    async def test_raw_checkpoint_file_contains_counters(self, manager: PipelineCheckpointManager, tmp_path: Path) -> None:
        """直接读 JSON 文件，确认计数确实被写入磁盘。"""
        state = {"pipeline_id": "p", "retry.transient_count": 7}

        checkpoint_id = await manager.save("p", state, phase="suspended")
        raw = json.loads((tmp_path / f"{checkpoint_id}.json").read_text(encoding="utf-8"))

        assert raw["state"]["retry.transient_count"] == 7
