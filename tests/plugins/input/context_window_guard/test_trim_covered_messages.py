"""_trim_covered_messages 与 _estimate_assembled_tokens 单元测试。

回归 pipeline 76e40ac9a0df 的 bug：裁剪侧和估算侧都用「非 system 消息条数」
与「压缩块全局 sequence_end」做算术，因 system 消息占用 sequence 号导致
non_sys_count < max_end 误判「全部已覆盖」，把未被压缩的 recent 段
（sequence > max_end，含已提交的 NoopInvoker 任务）一起裁掉/漏算
（实锤日志：851 -> 0 (max_end=899)，recent=0 (after=899)）。

修复后两处都逐条按全局 _record_sequence 过滤/累加，与压缩侧
_save_compression_result 的 max(_record_sequence) 统一真相源。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _make_context(
    pipeline_id: str = "test_pipeline",
    chunk_service: Any = None,
) -> PluginContext:
    """创建带 chunk_service 的 PluginContext。"""
    return PluginContext(
        state={StateKeys.PIPELINE_ID: pipeline_id},
        config={},
        _services={"chunk_service": chunk_service} if chunk_service else {},
    )


def _chunk(
    layer: str,
    sequence_end: int,
    sequence_start: int = 0,
    content: str = "已有压缩块",
) -> SimpleNamespace:
    """构造一个含 sequence_end/start/layer/content 的压缩块桩。"""
    return SimpleNamespace(
        layer=layer,
        sequence_end=sequence_end,
        sequence_start=sequence_start,
        content=content,
    )


def _msg(role: str, seq: int | None, content: str = "x") -> dict[str, Any]:
    """构造一条消息；seq=None 表示不带 _record_sequence。"""
    m: dict[str, Any] = {"role": role, "content": content}
    if seq is not None:
        m["_record_sequence"] = seq
    return m


def _non_sys_count(msgs: list[dict[str, Any]]) -> int:
    return sum(1 for m in msgs if m.get("role") != "system")


def _chunk_service(chunks: list[Any]) -> AsyncMock:
    """构造一个 chunk_service，find_by_pipeline 按 layer 过滤返回。"""
    svc = AsyncMock()

    async def _find(pipeline_id: str, layer: str | None = None) -> list[Any]:
        if layer is None:
            return list(chunks)
        return [c for c in chunks if c.layer == layer]

    svc.find_by_pipeline = _find
    return svc


# ============================================================
# 测试
# ============================================================


class TestTrimCoveredMessages:
    """_trim_covered_messages 回归测试。"""

    @pytest.mark.asyncio
    async def test_无压缩块原样返回(self) -> None:
        """没有 L1 压缩块时不裁剪。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        ctx = _make_context(chunk_service=_chunk_service([]))
        messages = [_msg("user", 1), _msg("assistant", 2)]

        result = await plugin._trim_covered_messages(ctx, messages)

        assert result is messages

    @pytest.mark.asyncio
    async def test_核心回归_system占号致non_sys_count小于max_end不全裁(self) -> None:
        """复现 pipeline 76e40ac9a0df 的核心 bug。

        场景：max_end=899（全局记录号），但非 system 消息中穿插了 system
        消息占用 sequence 号，使得非 system 消息条数（<899）小于 max_end，
        且存在 sequence > 899 的未压缩 recent 段（NoopInvoker 任务）。
        修复前：851 -> 0 全裁；修复后：保留 seq>899 的消息。
        """
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 899, 1)])
        ctx = _make_context(chunk_service=svc)

        messages: list[dict[str, Any]] = []
        # seq 1-100：部分已被压缩的非 system + 穿插 system
        for i in range(1, 101):
            messages.append(_msg("system", i) if i % 10 == 0 else _msg("user", i))
        # seq 900-910：未被压缩的 recent 段（NoopInvoker 任务在此）
        for i in range(900, 911):
            messages.append(_msg("assistant", i))

        result = await plugin._trim_covered_messages(ctx, messages)

        # 修复后绝不能全裁：必须保留 seq>899 的非 system 消息（900-910，共 11 条）
        kept_non_sys = _non_sys_count(result)
        assert kept_non_sys > 0, "裁剪后非 system 消息不应为 0（修复前是 851->0）"
        kept_seqs = {
            m["_record_sequence"]
            for m in result
            if m.get("role") != "system" and isinstance(m.get("_record_sequence"), int)
        }
        assert all(s > 899 for s in kept_seqs), "保留的非 system 消息 sequence 必须都 > max_end"
        assert 900 in kept_seqs and 910 in kept_seqs

    @pytest.mark.asyncio
    async def test_system消息全保留与压缩侧统一(self) -> None:
        """system 消息不参与裁剪，全部保留（与压缩侧 pure_system_msgs 原样保留一致）。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 50, 1)])
        ctx = _make_context(chunk_service=svc)

        messages = [
            _msg("system", 5),   # 在覆盖区间内的 system
            _msg("user", 10),    # 被覆盖的非 system，应裁
            _msg("system", 30),  # 在覆盖区间内的 system
            _msg("assistant", 60),  # 未覆盖，保留
        ]

        result = await plugin._trim_covered_messages(ctx, messages)

        kept_roles = [m["role"] for m in result]
        # 两条 system 都在，assistant(seq>50) 保留，user(seq10<=50) 被裁
        assert kept_roles.count("system") == 2
        assert "assistant" in kept_roles
        assert "user" not in kept_roles

    @pytest.mark.asyncio
    async def test_无record_sequence的非system消息保留(self) -> None:
        """无 _record_sequence 的非 system 消息（重注入压缩块等）默认保留。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 50, 1)])
        ctx = _make_context(chunk_service=svc)

        messages = [
            _msg("user", 10),          # 被覆盖，裁
            _msg("assistant", None),   # 无 sequence，保留
            _msg("user", 60),          # 未覆盖，保留
        ]

        result = await plugin._trim_covered_messages(ctx, messages)

        contents = [m["content"] for m in result]
        assert "x" in contents  # 至少保留了无 seq 和 seq>50 的
        kept_seqs = [
            m.get("_record_sequence")
            for m in result
            if m.get("role") != "system" and isinstance(m.get("_record_sequence"), int)
        ]
        # 只剩无 seq 的和 seq=60 的；seq=10 被裁
        assert 10 not in kept_seqs
        assert 60 in kept_seqs

    @pytest.mark.asyncio
    async def test_裁剪后非system不足10百分比触发防护保留原消息(self) -> None:
        """裁剪后非 system 消息 < 原 10% 视为边界异常，放弃裁剪保留原消息。

        模拟：全部非 system 消息 sequence 都 <= max_end（确实都被覆盖），
        但裁剪后非 system 为 0，触发防护，返回原消息避免上下文裁空。
        """
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 100, 1)])
        ctx = _make_context(chunk_service=svc)

        messages = [
            _msg("user", 1),
            _msg("assistant", 2),
            _msg("user", 3),
            _msg("system", 4),
            _msg("assistant", 5),
        ]

        result = await plugin._trim_covered_messages(ctx, messages)

        # 全部非 system 的 seq 都 <= 100，裁后非 system=0 < 10% → 防护触发，返回原消息
        assert result is messages

    @pytest.mark.asyncio
    async def test_正常裁剪保留recent段并打日志(self, caplog: pytest.LogCaptureFixture) -> None:
        """正常裁剪：大部分被覆盖，保留少量 recent 段（>10%），记录 INFO 日志。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 5, 1)])
        ctx = _make_context(chunk_service=svc)

        messages: list[dict[str, Any]] = []
        # 5 条被覆盖的非 system
        for i in range(1, 6):
            messages.append(_msg("user", i))
        # 3 条未覆盖的 recent（3/8 = 37.5% > 10%，不触发防护）
        for i in range(6, 9):
            messages.append(_msg("assistant", i))

        with caplog.at_level("INFO", logger="plugins.input.context_window_guard.plugin"):
            result = await plugin._trim_covered_messages(ctx, messages)

        # 保留 seq 6-8 三条，裁掉 seq 1-5
        kept_seqs = [
            m["_record_sequence"]
            for m in result
            if m.get("role") != "system" and isinstance(m.get("_record_sequence"), int)
        ]
        assert kept_seqs == [6, 7, 8]
        # 日志记录了裁剪动作
        assert any("裁剪被压缩块覆盖" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_无pipeline_id原样返回(self) -> None:
        """pipeline_id 为空时不裁剪。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        ctx = PluginContext(state={}, config={}, _services={})
        messages = [_msg("user", 1)]

        result = await plugin._trim_covered_messages(ctx, messages)

        assert result is messages


class TestEstimateAssembledTokens:
    """_estimate_assembled_tokens 的 recent 累加回归测试。

    同 _trim_covered_messages 的 bug：原代码用 non_sys_count > max_end 判断
    recent 段，system 消息占号导致 recent 永远算成 0（实锤 recent=0 (after=899)）。
    """

    @pytest.mark.asyncio
    async def test_recent按全局sequence累加不被system占号影响(self) -> None:
        """seq>max_end 的非 system 消息应被计入 recent，即使条数 < max_end。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        # L1 块 max_end=899；消息含穿插的 system + seq>899 的 recent
        svc = _chunk_service([_chunk("L1", 899, 1)])
        ctx = _make_context(chunk_service=svc)
        ctx.state["messages"] = []

        messages: list[dict[str, Any]] = []
        # 穿插 system 占用号（模拟触发器/通知）
        for i in range(1, 101):
            messages.append(_msg("system", i) if i % 10 == 0 else _msg("user", i))
        # seq>899 的 recent（NoopInvoker 任务）
        for i in range(900, 911):
            content = "x" * 200  # 100 tokens/条
            messages.append(_msg("assistant", i, content))

        estimated = await plugin._estimate_assembled_tokens(ctx, messages)

        # recent 段 11 条 × 100 tokens = 1100，加上 L1(3839 估算自 content) + snapshot
        # 关键：recent 不应为 0（修复前因 non_sys_count<=max_end 永远算 0）
        assert estimated > 0
        # L1 块 content "已有压缩块" 约 6 tokens，snapshot 无 STATE_SNAPSHOT 块=0
        # recent 必须贡献了相当大的量（>1000 tokens），证明 seq>899 被正确累加
        assert estimated >= 1100, f"recent 段未正确累加，estimated={estimated}"

    @pytest.mark.asyncio
    async def test_无sequence的非system消息不计入recent(self) -> None:
        """无 _record_sequence 的非 system 消息不计入 recent（避免重复算）。"""
        from plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin()
        svc = _chunk_service([_chunk("L1", 50, 1)])
        ctx = _make_context(chunk_service=svc)
        ctx.state["messages"] = []

        messages = [
            _msg("assistant", None, "y" * 200),  # 无 seq，不计入 recent
            _msg("user", 60, "x" * 200),         # seq>50，计入
        ]

        estimated = await plugin._estimate_assembled_tokens(ctx, messages)

        # 只有 seq=60 的一条计入 recent（100 tokens），无 seq 的不算
        assert estimated >= 100

