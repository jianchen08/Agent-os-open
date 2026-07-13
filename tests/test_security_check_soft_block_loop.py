"""security_check 软拦截死循环回归测试。

历史 Bug（任务 d6ac591c393c，87 轮空转）：
  模型用相对路径 `../../../docs/working/xxx.md` 调 file_write，被
  _check_path_traversal（含 `..`）判为路径遍历，走 _soft_block 软拦截。
  _soft_block 清空了 raw_tool_calls、写了 TOOL_RESULTS，但拒绝结果
  【没有同步 append 成 role=tool 消息到 messages】。导致：
    1. messages 末尾留下无配对 tool 结果的孤儿 assistant(file_write)
    2. 下一轮 normalize Phase B 把这条孤儿 assistant 整条删除
    3. 模型收不到"路径被拦"的反馈，历史回退到更早的 mkdir
    4. 模型重新生成同一 file_write → 死循环（87 轮，无上限提示）

修复（两道防线）：
  P0：_soft_block 把拒绝结果 append 成 role=tool 消息（tool_call_id 配对），
      让 assistant 有了配对结果，normalize 不再删除，模型收到反馈可改正。
  P2：同一工具签名连续被拦超阈值（默认 3）→ 终止管道并上报，
      作为"模型无视反馈反复重试"的最终上限防线。

本测试锁定该契约。参考 test_security_check_per_round.py 的 PluginContext 风格。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext


def _ctx_with_traversal_path(
    tool_name: str = "file_write",
    path: str = "../../../docs/working/Python入门要点.md",
    *,
    call_id: str = "call_abc123",
    messages: list[dict] | None = None,
) -> PluginContext:
    """构造一个命中路径遍历检测的 PluginContext。

    path 含 `..` → _check_path_traversal 命中 → _soft_block。
    工具为 file_write（dangerous_operations），host 模式非隔离，
    但路径遍历检测在第一道，先于审批命中。
    """
    return PluginContext(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [
                {"name": tool_name, "id": call_id, "args": {"path": path, "content": "# x"}},
            ],
            "execution_contexts": [{"provider": "host", "tool_name": tool_name}],
            "messages": list(messages) if messages else [],
        },
        _services={},
    )


# ═══════════════════════════════════════════════════════════════
# P0：拒绝结果必须配对写回 messages
# ═══════════════════════════════════════════════════════════════


class TestSoftBlockWritesPairedToolMessage:
    """P0 契约：_soft_block 必须把拒绝结果 append 成配对的 role=tool 消息。

    修复前 TOOL_RESULTS 写了但没人读，messages 里留下孤儿 assistant →
    被 normalize 删除 → 模型收不到反馈 → 死循环。
    """

    @pytest.mark.asyncio
    async def test_soft_block_appends_tool_message_with_call_id(self):
        """路径遍历被拦 → messages 末尾必须有 role=tool 且 tool_call_id 配对。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = _ctx_with_traversal_path(call_id="call_fix_1")

        result = await plugin.execute(ctx)

        messages = result.state_updates.get("messages", [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]

        assert len(tool_msgs) == 1, "拒绝结果必须 append 为 role=tool 消息"
        assert tool_msgs[0].get("tool_call_id") == "call_fix_1", (
            "tool 消息的 tool_call_id 必须与被拒 assistant 的 id 配对，"
            "否则 normalize Phase B 会把 assistant 当孤儿删除"
        )
        assert "路径遍历" in tool_msgs[0].get("content", "") or "traversal" in tool_msgs[0].get("content", "")

    @pytest.mark.asyncio
    async def test_soft_block_preserves_existing_messages(self):
        """_soft_block 在已有 messages 基础上追加，不破坏历史。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        existing = [
            {"role": "user", "content": "写文档"},
            {"role": "assistant", "tool_calls": [{"id": "call_fix_1", "function": {"name": "file_write"}}]},
        ]
        ctx = _ctx_with_traversal_path(call_id="call_fix_1", messages=existing)

        result = await plugin.execute(ctx)
        messages = result.state_updates.get("messages", [])

        # 原有 2 条 + 新增 1 条 tool = 3 条
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_soft_block_clears_raw_tool_calls(self):
        """软拦截后 raw_tool_calls 必须清空（防止 tool_core 重复执行）。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = _ctx_with_traversal_path()

        result = await plugin.execute(ctx)
        assert result.state_updates.get("raw_tool_calls") == []


# ═══════════════════════════════════════════════════════════════
# P2：同一请求连续被拦超阈值 → 终止管道
# ═══════════════════════════════════════════════════════════════


class TestSoftBlockRepeatThreshold:
    """P2 契约：同一工具签名连续被拦超阈值 → 设 ENDED 终止管道。

    修复前没有任何上限，模型可无限重试同一被拦请求（实际 87 轮空转）。
    """

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_end(self):
        """连续被拦 < 阈值（3）→ 不终止，仍走 soft_block 反馈（让模型改正）。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        # 同一签名连拦 2 次（< 阈值 3）
        for _ in range(2):
            ctx = _ctx_with_traversal_path()
            result = await plugin.execute(ctx)
            assert not result.state_updates.get("ended", False), (
                "未达阈值时不应终止管道，应反馈给模型让其改正路径"
            )

    @pytest.mark.asyncio
    async def test_at_threshold_ends_pipeline(self):
        """同一签名连续被拦达阈值（3）→ 设 ENDED 终止管道并上报。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = None
        for _ in range(3):
            ctx = _ctx_with_traversal_path()
            result = await plugin.execute(ctx)

        assert result is not None
        assert result.state_updates.get("ended") is True, (
            "同一被拦请求连续达阈值必须终止管道，这是死循环的最终上限防线"
        )
        err = result.state_updates.get("raw_error", "")
        assert "连续被" in err and "次" in err, f"终止时应明确上报原因，得到: {err}"

    @pytest.mark.asyncio
    async def test_different_signature_resets_count(self):
        """模型换了请求（签名变化）→ 计数重置，不算死循环。

        模型从路径遍历 A 改成路径遍历 B（不同路径）是"在尝试改正"，
        不应被上限误杀。
        """
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        # 路径 A 拦 2 次
        for _ in range(2):
            await plugin.execute(_ctx_with_traversal_path(path="../../../a.md"))
        # 换路径 B 再拦 2 次（< 阈值，且是新签名）
        ended = False
        for _ in range(2):
            ctx = _ctx_with_traversal_path(path="../../../b.md")
            r = await plugin.execute(ctx)
            if r.state_updates.get("ended"):
                ended = True
        assert not ended, "换路径（新签名）不应累加旧计数，不应被上限误杀"

    @pytest.mark.asyncio
    async def test_threshold_configurable(self):
        """reject_threshold 可通过 config 配置。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(
            config={"enabled": True, "rules": [], "reject_threshold": 2},
        )
        result = None
        for _ in range(2):
            ctx = _ctx_with_traversal_path()
            result = await plugin.execute(ctx)
        assert result.state_updates.get("ended") is True, "阈值配为 2 时，第 2 次即应终止"
