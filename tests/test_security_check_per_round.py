"""security_check 审批闸门回归测试。

覆盖两类契约：

1. 根因回归（bug 修复）：
   原 execute() 开头有一段"已有 security.decision 就跳过"的幂等检查，
   因 state 跨轮复用，导致第一轮审批通过后，后续每轮工具调用（含路径遍历、
   敏感目录等硬底线）全部被跳过，安全闸门失效。
   删除后必须保证：同一插件实例 + 同一 state 字典，换不同危险命令时
   每轮都独立触发审批。

2. 指纹记忆（体验增强）：
   用户选 "approved_remember" 后，本管道内"同工具+同指纹"命令免审批；
   但精确匹配——路径/命令不同（哪怕只差一个参数）仍要重新审批。

这是对象生命周期/状态残留类 bug，回归测试验证"审批触发次数"（行为身份），
不仅是返回值。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from pipeline.plugin import PluginContext


def _approval_svc(sequence: list[dict]):
    """构造审批服务 mock，按 sequence 依次返回审批结果。

    每次 wait_for_choice 消费 sequence 中一项，用于模拟多轮审批。

    Args:
        sequence: 审批结果列表，每项形如 {"selected_option": "approved_once"}

    Returns:
        (svc, create_call_count) —— svc 为 mock 服务，
        create_call_count 为 create_choice_request 被调用次数的可观测计数器。
    """
    svc = MagicMock()
    it = iter(sequence)

    # 用 list 容器承载计数器，避免闭包对不可变值赋值需要 nonlocal
    counter = [0]

    async def _create(**kwargs):
        counter[0] += 1
        return f"req-{counter[0]}"

    async def _wait(request_id):
        try:
            return next(it)
        except StopIteration as e:
            raise AssertionError("审批被发起次数超出预期 sequence") from e

    svc.create_choice_request = _create
    svc.wait_for_choice = _wait

    # 返回一个轻量可观测对象，.calls 反映 create 调用次数
    class _Counter:
        @property
        def calls(self) -> int:
            return counter[0]

    return svc, _Counter()


def _ctx_for(tool_name: str, command: str, *, provider: str = "host") -> PluginContext:
    """构造一个危险工具执行的 PluginContext（host 模式、非白名单命令）。"""
    return PluginContext(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": tool_name, "args": {"command": command}}],
            "execution_contexts": [{"provider": provider, "tool_name": tool_name}],
        },
        _services={},
    )


# ═══════════════════════════════════════════════════════════════
# 契约 1：根因回归 —— 每轮独立审批，不因前轮残留而跳过
# ═══════════════════════════════════════════════════════════════


class TestPerRoundApproval:
    """bug 修复核心契约：同一插件实例跨轮调用，每轮危险命令都要审批。"""

    @pytest.mark.asyncio
    async def test_second_round_still_triggers_approval(self, monkeypatch):
        """第一轮审批通过后，第二轮换不同危险命令 → 必须再次审批。

        修复前：第一轮写入 security.decision 后永久驻留 state，
        第二轮 execute() 开头幂等检查命中 → 跳过 → 审批只发起 1 次。
        修复后：每轮独立检查 → 审批发起 2 次。
        """
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([
            {"selected_option": "approved_once"},
            {"selected_option": "approved_once"},
        ])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        # 第一轮：危险命令 A
        ctx1 = _ctx_for("bash_execute", "rm -rf /tmp/a")
        r1 = await plugin.execute(ctx1)
        assert r1.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 1

        # 第二轮：危险命令 B（不同路径）—— 关键：必须再次审批
        ctx2 = _ctx_for("bash_execute", "rm -rf /tmp/b")
        r2 = await plugin.execute(ctx2)
        assert create.calls == 2, (
            f"第二轮危险命令必须再次触发审批，但 create_choice_request 只被调用 {create.calls} 次"
        )

    @pytest.mark.asyncio
    async def test_residual_decision_does_not_skip(self, monkeypatch):
        """state 中预先残留 security.decision 也不应跳过检查。

        直接验证幂等检查已被删除：手动注入 allowed=True 的旧决策，
        execute() 仍应正常执行本轮检查（触发审批）。
        """
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([{"selected_option": "approved_once"}])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = _ctx_for("bash_execute", "curl http://evil.example")
        # 模拟上一轮残留的决策
        ctx.state["security"] = {"decision": {"allowed": True, "reason": "approved"}}

        await plugin.execute(ctx)
        assert create.calls == 1, "残留 security.decision 不应导致跳过本轮审批"


# ═══════════════════════════════════════════════════════════════
# 契约 2：指纹记忆 —— 同命令免批，不同命令重审
# ═══════════════════════════════════════════════════════════════


class TestSignatureMemory:
    """体验增强契约：approved_remember 记忆精确指纹，本管道内同命令免批。"""

    @pytest.mark.asyncio
    async def test_remembered_command_skips_approval(self, monkeypatch):
        """approved_remember 后，同工具+同命令再次调用 → 不再审批（放行）。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([{"selected_option": "approved_remember"}])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        # 第一次：危险命令，用户选"同命令免批"
        ctx1 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx1)
        assert create.calls == 1
        assert plugin._approved_signatures, "指纹应被记忆"

        # 第二次：完全相同命令 → 免审批
        ctx2 = _ctx_for("bash_execute", "curl http://api.example")
        r2 = await plugin.execute(ctx2)
        assert create.calls == 1, "同命令已记忆，不应再次发起审批"
        assert r2.state_updates.get("security.decision", {}).get("allowed") is True

    @pytest.mark.asyncio
    async def test_approved_once_does_not_remember(self, monkeypatch):
        """approved_once 不记忆 → 同命令再次调用仍要审批。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([
            {"selected_option": "approved_once"},
            {"selected_option": "approved_once"},
        ])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        ctx1 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx1)
        assert create.calls == 1
        assert not plugin._approved_signatures, "approved_once 不应记忆指纹"

        # 同命令再次调用 → 仍要审批
        ctx2 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx2)
        assert create.calls == 2, "approved_once 未记忆，同命令仍应审批"

    @pytest.mark.asyncio
    async def test_different_path_still_requires_approval(self, monkeypatch):
        """记忆命令 A 后，命令 B（路径不同）→ 仍要审批（精确匹配，不模糊）。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([
            {"selected_option": "approved_remember"},
            {"selected_option": "approved_once"},
        ])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        # 记忆 rm -rf /tmp/a
        ctx1 = _ctx_for("bash_execute", "rm -rf /tmp/a")
        await plugin.execute(ctx1)
        assert create.calls == 1

        # rm -rf /tmp/b —— 路径不同，必须重审（不能因记了 /tmp/a 就放行 /tmp/b）
        ctx2 = _ctx_for("bash_execute", "rm -rf /tmp/b")
        await plugin.execute(ctx2)
        assert create.calls == 2, "不同路径的命令必须重新审批"

    @pytest.mark.asyncio
    async def test_whitespace_normalized_to_same_signature(self, monkeypatch):
        """命令多余空白归一化为同一指纹 → 免审批。

        "curl   http://x"（多空格）与 "curl http://x"（单空格）视为同命令。
        """
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc([{"selected_option": "approved_remember"}])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        ctx1 = _ctx_for("bash_execute", "curl    http://x")
        await plugin.execute(ctx1)
        assert create.calls == 1

        # 单空格版本 → 同指纹 → 免审批
        ctx2 = _ctx_for("bash_execute", "curl http://x")
        r2 = await plugin.execute(ctx2)
        assert create.calls == 1, "空白归一化后应视为同命令，免审批"
        assert r2.state_updates.get("security.decision", {}).get("allowed") is True


# ═══════════════════════════════════════════════════════════════
# 契约 3：label 反查 —— 前端提交 label 文本时分支判断仍正确
#
# 阶段 2 改动：消费处把 selected_option（可能是 label 也可能是 id）
# 归一到 stable id 再做分支。前端实际优先传 label，此契约保障该路径。
# ═══════════════════════════════════════════════════════════════


class TestLabelBasedSelection:
    """前端提交选项 label 文本（非 id）时，审批分支判断仍正确。

    前端 InteractionPanel.respondChoice 优先传 optionLabel，故 wait_for_choice
    返回的 selected_option 通常是 label。插件需先按 label 反查 id 再判分支。
    """

    @pytest.mark.asyncio
    async def test_label_remember_triggers_signature_memory(self, monkeypatch):
        """提交 label '本管道内同命令免批' → 等价 approved_remember → 记忆指纹。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc(
            [{"selected_option": "本管道内同命令免批"}]
        )
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        ctx1 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx1)
        assert create.calls == 1
        assert plugin._approved_signatures, "label 路径下 approved_remember 应记忆指纹"

        # 同命令再次调用 → 免审批（指纹已记）
        ctx2 = _ctx_for("bash_execute", "curl http://api.example")
        r2 = await plugin.execute(ctx2)
        assert create.calls == 1, "label 路径记忆后，同命令应免审批"
        assert r2.state_updates.get("security.decision", {}).get("allowed") is True

    @pytest.mark.asyncio
    async def test_label_once_does_not_remember(self, monkeypatch):
        """提交 label '仅本次执行' → 等价 approved_once → 不记忆指纹。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, create = _approval_svc(
            [{"selected_option": "仅本次执行"}, {"selected_option": "仅本次执行"}]
        )
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        ctx1 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx1)
        assert create.calls == 1
        assert not plugin._approved_signatures, "label 路径下 approved_once 不应记忆"

        # 同命令再次调用 → 仍要审批
        ctx2 = _ctx_for("bash_execute", "curl http://api.example")
        await plugin.execute(ctx2)
        assert create.calls == 2, "label 路径 approved_once 未记忆，同命令仍应审批"

    @pytest.mark.asyncio
    async def test_label_denied_soft_blocks(self, monkeypatch):
        """提交 label '拒绝执行' → 等价 denied → 软拦截，不放行。"""
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        svc, _create = _approval_svc([{"selected_option": "拒绝执行"}])
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: svc,
        )

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        ctx = _ctx_for("bash_execute", "rm -rf /tmp/x")
        r = await plugin.execute(ctx)
        decision = r.state_updates.get("security.decision", {})
        # _soft_block 返回 allowed=True（软拦截：拒绝结果反馈给 LLM，
        # 不结束管道），靠 reason 标记区分是否被拒。
        assert "soft_block" in decision.get("reason", ""), (
            "label 路径 denied 应走 soft_block，reason 须带 soft_block 标记"
        )
        assert "用户拒绝" in decision.get("reason", ""), "reason 应体现用户拒绝"
        assert not plugin._approved_signatures, "denied 绝不记忆指纹"
