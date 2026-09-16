# @feature: FP-0.2.二 | @vision: V2 安全
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

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import logging
from unittest.mock import MagicMock

import pytest
from pipeline.plugin import PluginContext

pytestmark = pytest.mark.unit


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

    契约：拒绝反馈必须配对写回 messages（否则模型收不到反馈）。
    """

    @pytest.mark.asyncio
    async def test_soft_block_appends_tool_message_with_call_id(self):
        """路径遍历被拦 → messages 末尾必须有 role=tool 且 tool_call_id 配对。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

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
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

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
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = _ctx_with_traversal_path()

        result = await plugin.execute(ctx)
        assert result.state_updates.get("raw_tool_calls") == []


# ═══════════════════════════════════════════════════════════════
# P2：同一请求连续被拦超阈值 → 终止管道
# ═══════════════════════════════════════════════════════════════


class TestSoftBlockRepeatThreshold:
    """P2 契约：同一工具签名连续被拦超阈值 → 设 ENDED 终止管道。

    契约：连续被拦必须有上限（不得无限重试同一被拦请求）。
    """

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_end(self):
        """连续被拦 < 阈值（3）→ 不终止，仍走 soft_block 反馈（让模型改正）。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

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
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

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
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

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
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(
            config={"enabled": True, "rules": [], "reject_threshold": 2},
        )
        result = None
        for _ in range(2):
            ctx = _ctx_with_traversal_path()
            result = await plugin.execute(ctx)
        assert result.state_updates.get("ended") is True, "阈值配为 2 时，第 2 次即应终止"


# ═══════════════════════════════════════════════════════════════
# 2026-09-13 覆盖率补测：内置底线软拦截 / 字符串参数容错 / 降级提示发射失败
# ═══════════════════════════════════════════════════════════════


def _ctx_with_args(
    tool_name: str,
    args: dict,
    *,
    call_id: str = "call-args-1",
) -> PluginContext:
    """构造单工具调用的 PluginContext（host、非隔离），args 形状自定义。"""
    return PluginContext(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": tool_name, "id": call_id, "args": args}],
            "execution_contexts": [{"provider": "host", "tool_name": tool_name}],
            "messages": [],
        },
        _services={},
    )


class TestBaseScanHardFloor:
    """内置底线（任何模式都强制执行）命中即软拦截：敏感目录 / nul 重定向 / 非法路径。"""

    @pytest.mark.asyncio
    async def test_sensitive_path_hit_soft_blocks(self, monkeypatch: pytest.MonkeyPatch):
        """敏感目录判定命中 → 软拦截；未命中 → 正常放行（判定器为注入边界）。"""
        add_plugin_dir("input", "security_check")
        import plugin as sc_mod
        from plugin import SecurityCheckPlugin

        def fake_is_sensitive(path: str) -> tuple[bool, str]:
            return (True, "forbidden/prefix") if "sysroot" in path else (False, "")

        monkeypatch.setattr(sc_mod, "is_sensitive_path", fake_is_sensitive)
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        blocked = await plugin.execute(
            _ctx_with_traversal_path(path="/data/sysroot/secret", call_id="call-sens")
        )
        decision = blocked.state_updates["security.decision"]
        assert "敏感系统目录被拦截" in decision.get("reason", "")
        assert "forbidden/prefix" in decision.get("reason", "")
        tool_msgs = [
            m for m in blocked.state_updates["messages"] if m.get("role") == "tool"
        ]
        assert [m["tool_call_id"] for m in tool_msgs] == ["call-sens"]
        assert blocked.state_updates["raw_tool_calls"] == []

        passed = await plugin.execute(
            _ctx_with_traversal_path(path="/data/normal/file.txt")
        )
        assert passed.state_updates["security.decision"]["allowed"] is True
        assert "soft_block" not in passed.state_updates["security.decision"].get("reason", "")

    @pytest.mark.parametrize(
        "command",
        [
            "dir >nul",
            "type x 2>nul",
            "echo hi >>nul",
            "copy a b >&nul",
            "ping host > NUL",
        ],
    )
    @pytest.mark.asyncio
    async def test_nul_redirect_hit_soft_blocks(self, command: str):
        """CMD 风格 nul 重定向变体（>nul/2>nul/>>nul/>&nul，大小写不敏感）→ 软拦截。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = await plugin.execute(_ctx_with_args("bash_execute", {"command": command}))
        reason = result.state_updates["security.decision"].get("reason", "")
        assert "CMD 风格重定向被拦截" in reason, f"{command!r} 应命中 nul 重定向拦截"
        assert "2>/dev/null" in reason, "拦截反馈应提示改用 2>/dev/null"
        assert result.state_updates["raw_tool_calls"] == []
        tool_msgs = [m for m in result.state_updates["messages"] if m.get("role") == "tool"]
        assert tool_msgs, "拦截原因必须写回 messages 反馈给 LLM"

    @pytest.mark.parametrize(
        "command",
        [
            "dir > /dev/null",
            "echo nullable-value",
            "wc -l nulless",
        ],
    )
    @pytest.mark.asyncio
    async def test_nul_redirect_miss_passes(self, command: str):
        """/dev/null 重定向与 null* 标识符不误伤 → 正常放行。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = await plugin.execute(_ctx_with_args("bash_execute", {"command": command}))
        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True
        assert "soft_block" not in decision.get("reason", "")
        assert "tool_results" not in result.state_updates

    @pytest.mark.asyncio
    async def test_null_byte_path_fail_closed(self):
        """含空字节路径必被 fail-closed 拦截（Invalid path / Null byte 二检其一命中）。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = await plugin.execute(
            _ctx_with_traversal_path(path="C:/tmp/x\x00y", call_id="call-null")
        )
        reason = result.state_updates["security.decision"].get("reason", "")
        assert "路径遍历攻击被拦截" in reason
        # resolve 是否对空字节抛 ValueError 随 Python 版本/平台而异：
        # 抛则走 Invalid path，不抛则落显式空字节检查——两条路都必须拦截
        assert ("Invalid path" in reason) or ("Null byte injection" in reason)
        assert result.state_updates["raw_tool_calls"] == []

    @pytest.mark.asyncio
    async def test_null_byte_path_reports_precise_reason(self):
        """空字节检查先于 resolve：命中精确 Null byte 文案（不落泛化 Invalid path）。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = await plugin.execute(
            _ctx_with_traversal_path(path="C:/tmp/x\x00y", call_id="call-null-precise")
        )
        reason = result.state_updates["security.decision"].get("reason", "")
        assert "Null byte injection detected" in reason
        assert "Invalid path" not in reason
        assert result.state_updates["raw_tool_calls"] == []

    @pytest.mark.asyncio
    async def test_resolve_failure_falls_to_invalid_path_fail_closed(self, monkeypatch):
        """resolve 异常（非 NUL 的 OS 层故障）→ 泛化 Invalid path 拦截，fail-closed。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        def _boom(self, **kwargs):
            raise OSError(123, "injected resolve failure")

        monkeypatch.setattr("pathlib.Path.resolve", _boom)
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        result = await plugin.execute(
            _ctx_with_traversal_path(path="C:/tmp/ok-name", call_id="call-invalid")
        )
        reason = result.state_updates["security.decision"].get("reason", "")
        assert "Invalid path" in reason and "injected resolve failure" in reason
        assert result.state_updates["raw_tool_calls"] == []


class TestSoftBlockToleratesStringArgs:
    """软拦截对字符串形状的工具参数不崩溃（容错契约）。"""

    @pytest.mark.asyncio
    async def test_string_args_tool_call_rejected_with_paired_messages(self):
        """同名工具首个调用 args 为字符串：全部拒绝写回且消息配对，不抛异常。"""
        add_plugin_dir("input", "security_check")
        from plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [
                    {"name": "bash_execute", "id": "c-str", "args": "plain args string"},
                    {"name": "bash_execute", "id": "c-dict", "args": {"path": "../../x"}},
                ],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute"}],
                "messages": [],
            },
            _services={},
        )
        result = await plugin.execute(ctx)

        assert result.state_updates["raw_tool_calls"] == []
        tool_msgs = [m for m in result.state_updates["messages"] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c-str", "c-dict"]
        assert all(m["content"].startswith("Error: [审批拒绝]") for m in tool_msgs), (
            f"同名调用都按审批拒绝标记，实际 {[m['content'] for m in tool_msgs]}"
        )


class TestDegradedNoticeEmitFailure:
    """降级提示发射失败只留日志，绝不阻断安全检查（通知是增强能力）。"""

    @pytest.mark.asyncio
    async def test_emit_failure_does_not_block_check(self, caplog: pytest.LogCaptureFixture):
        add_plugin_dir("input", "security_check")
        import plugin as sc_mod
        from plugin import SecurityCheckPlugin

        async def _boom(event: str, payload: dict, thread_id: str) -> None:
            raise RuntimeError("frontend channel down")

        sc_mod.set_frontend_emit(_boom)
        try:
            plugin = SecurityCheckPlugin(
                config={"enabled": True, "security_rules": {"mode": "blacklist"}}
            )
            assert plugin._rules_degraded is True
            with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
                result = await plugin.execute(
                    _ctx_with_args("file_write", {"path": "普通非遍历.txt", "content": "# x"})
                )
        finally:
            sc_mod.set_frontend_emit(None)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True, "提示失败不得阻断安全检查"
        assert any("事件推送失败" in r.getMessage() for r in caplog.records)
