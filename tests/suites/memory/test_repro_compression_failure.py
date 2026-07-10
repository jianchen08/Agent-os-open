"""端到端复现测试：压缩 0.000s 失败 + glm messages 参数非法。

针对线上日志两个现象：
1. context_window_guard 调 compress_messages 后 0.000s success、
   skip_remaining=True，但 MemoryContextService 内部 INFO 日志一条没有 →
   说明 _compress_messages_impl 根本没进入，异常被顶层 except 吞成 None。
2. 主模型（glm-5.2 / yichengc）抛 BadRequestError "messages 参数非法"，
   怀疑是 history 段残留非首位 system 消息，而 normalizer 对非 minimax
   provider 不做 system→user 转换。

本测试用真实 plugin.execute(ctx) 链路（非直接调 service）复现上述路径。
"""

import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src",
)))

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _fake_llm_response(l1: str = "压缩摘要内容") -> str:
    """构造 compressor 需要的假 LLM JSON 响应。"""
    return json.dumps({
        "l1": l1,
        "l2": "",
        "keywords": ["关键词1"],
        "state_snapshot": {"current_state": "测试中"},
        "memory_items": {},
    }, ensure_ascii=False)


def _make_big_messages(count: int = 100, chars_each: int = 2000) -> list[dict]:
    """生成足够大、超过触发线的消息（len//2 估算）。"""
    msgs = [{"role": "system", "content": "你是一个 AI 助手"}]
    for i in range(count):
        msgs.append({"role": "user", "content": f"消息 {i}: " + "x" * chars_each})
        msgs.append({"role": "assistant", "content": f"回复 {i}: " + "y" * chars_each})
    return msgs


def _build_real_ctx(
    messages: list[dict],
    *,
    context_window: int = 200000,
    trigger_ratio: float = 0.5,
    service: object = None,
    chunk_service: object = None,
) -> "MagicMock":
    """构造贴近真实运行的 PluginContext。

    关键：service 从 ctx.get_service("context_service") 拿（复现 service 复用路径），
    而非测试里直接 new。prev_input + tracked 模拟日志里的"无增量"场景。
    """
    ctx = MagicMock()
    ctx.state = {
        "context_window": context_window,
        "messages": messages,
        "pipeline_id": "test-pipeline-repro",
        # 模拟日志：prev_input=75584, tracked=20, current=20 → 走"无增量"分支
        "llm_usage": {"input_tokens": context_window},  # 超过 trigger 模拟已超阈值
        "_tracked_msg_count": sum(1 for m in messages if m.get("role") != "system"),
        StateKeys_PIPELINE_ID: "test-pipeline-repro",
    }
    services = {}
    if service is not None:
        services["context_service"] = service
    if chunk_service is not None:
        services["chunk_service"] = chunk_service
    ctx._services = services
    ctx.get_service = lambda name: services[name] if name in services else (_ for _ in ()).throw(KeyError(name))
    return ctx


# pipeline.types.StateKeys.PIPELINE_ID 的字符串值，避免循环导入硬编码
StateKeys_PIPELINE_ID = "pipeline_id"


class TestReproCompressionFailure:
    """复现压缩 0.000s 失败 + 异常被吞场景。"""

    @pytest.mark.asyncio
    async def test_compression_swallows_exception_when_llm_fn_unavailable(self, caplog):
        """复现：service 走真实 _build_llm_call_fn 但无法构建 → 返回 None → 终止管线。

        线上日志现象：0.000s success + skip_remaining=True，无内部 INFO 日志。
        本测试不预注入 llm_call_fn，让 _build_llm_call_fn 走真实 router_factory 路径，
        mock 掉 router 使其返回 None → 验证失败分支行为。
        """
        from memory.memory_context_service import MemoryContextService
        from pipeline.plugin import PluginContext
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin
        from pipeline.types import StateKeys

        context_window = 200000
        trigger_ratio = 0.5
        messages = _make_big_messages(100, 2000)

        # 真实 service（不预注入 llm_call_fn，逼 _build_llm_call_fn 走真实路径）
        service = MemoryContextService(
            config={"context_window": context_window, "compress_trigger_ratio": trigger_ratio},
        )
        service.setup(
            pipeline_id="test-pipeline-repro",
            session_id="test-session",
            context_window=context_window,
            compression_model_id="minimax-m3-guangfang",
        )

        plugin = ContextWindowGuardPlugin(config={
            "trigger_ratio": trigger_ratio,
            "compression_model": "minimax-m3-guangfang",
        })

        # 构造真实 PluginContext（非 MagicMock），更贴近运行时
        ctx = PluginContext(
            state={
                "context_window": context_window,
                "messages": messages,
                StateKeys.PIPELINE_ID: "test-pipeline-repro",
                # 模拟日志"无增量"场景：prev_input 大 + tracked=current
                "llm_usage": {"input_tokens": context_window + 10000},
                "_tracked_msg_count": sum(1 for m in messages if m.get("role") != "system"),
            },
            _services={"context_service": service},
        )

        # mock router_factory.get_or_create_adapter 抛异常 → _build_llm_call_fn 返回 None
        with patch(
            "llm.router_factory.get_or_create_adapter",
            side_effect=RuntimeError("mock: router 构建失败（模拟压缩链路异常）"),
        ):
            with caplog.at_level(logging.DEBUG):
                result = await plugin.execute(ctx)

        # 打印所有 memory/ContextCompressor 相关日志，看清楚 _compress_messages_impl 走到哪
        relevant = [
            r for r in caplog.records
            if "memory" in r.name.lower() or "compressor" in r.name.lower()
            or "context_window_guard" in r.name.lower()
        ]
        print(f"\n[场景A] 全部相关日志 ({len(relevant)} 条):")
        for r in relevant:
            print(f"  [{r.name}] {r.getMessage()[:160]}")

        # 断言：失败分支应终止管线（对应日志 skip_remaining=True）
        assert result.skip_remaining is True, "压缩失败应返回 skip_remaining=True"
        assert ctx.state.get(StateKeys.ENDED) is True, "压缩失败应设 ENDED=True"
        print(f"\n[场景A] 压缩失败正确触发终止: skip_remaining={result.skip_remaining}, ENDED={ctx.state.get(StateKeys.ENDED)}")

        # 关键诊断：捕获 service 内部日志，确认是否进入 _compress_messages_impl
        impl_logs = [r for r in caplog.records if "_compress_messages_impl 开始执行" in r.getMessage()]
        top_exc_logs = [r for r in caplog.records if "compress_messages 顶层异常" in r.getMessage()]
        skip_logs = [r for r in caplog.records if "跳过压缩" in r.getMessage() or "无法构建" in r.getMessage()]
        print(f"[场景A] service 内部日志: impl={len(impl_logs)} 顶层异常={len(top_exc_logs)} 跳过/无法构建={len(skip_logs)}")

        # 这是核心证据点：如果三条都没有，就复现了线上"无内部日志"的反常现象
        if not impl_logs and not top_exc_logs and not skip_logs:
            print("[场景A] ⚠️ 复现线上现象：compress_messages 返回 None 但无任何内部日志！")
            print("[场景A]    → 异常被吞在比 _compress_messages_impl 更早的位置")

    @pytest.mark.asyncio
    async def test_compression_swallows_exception_in_load_background(self, caplog):
        """复现：_load_background 抛异常被顶层 except 吞 → 0.000s None。

        更精准模拟：service 有 chunk_service，但 _load_background 抛异常。
        """
        from memory.memory_context_service import MemoryContextService
        from pipeline.plugin import PluginContext
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin
        from pipeline.types import StateKeys

        context_window = 200000
        messages = _make_big_messages(100, 2000)

        service = MemoryContextService(
            config={"context_window": context_window},
        )

        # 坏的 chunk_service：find_by_pipeline 抛异常
        bad_chunk = MagicMock()
        bad_chunk.find_by_pipeline = AsyncMock(side_effect=RuntimeError("mock: chunk_service 损坏"))
        service.setup(
            pipeline_id="test-bg",
            context_window=context_window,
            compression_model_id="minimax-m3-guangfang",
            chunk_service=bad_chunk,
        )

        plugin = ContextWindowGuardPlugin(config={
            "trigger_ratio": 0.5,
            "compression_model": "minimax-m3-guangfang",
        })

        ctx = PluginContext(
            state={
                "context_window": context_window,
                "messages": messages,
                StateKeys.PIPELINE_ID: "test-bg",
                "llm_usage": {"input_tokens": context_window + 10000},
                "_tracked_msg_count": sum(1 for m in messages if m.get("role") != "system"),
            },
            _services={"context_service": service, "chunk_service": bad_chunk},
        )

        with patch(
            "llm.router_factory.get_or_create_adapter",
            side_effect=RuntimeError("mock: router 构建失败"),
        ):
            with caplog.at_level(logging.DEBUG, logger="memory.memory_context_service"):
                result = await plugin.execute(ctx)

        assert result.skip_remaining is True
        # 此时应该能看到 _load_background 的 warning 或顶层异常日志
        top_exc = [r for r in caplog.records if "顶层异常" in r.getMessage()]
        bg_logs = [r for r in caplog.records if "加载" in r.getMessage() and "失败" in r.getMessage()]
        print(f"\n[场景A2] chunk_service 损坏: 顶层异常={len(top_exc)} 加载失败={len(bg_logs)} skip={result.skip_remaining}")


class TestReproGlmMessagesInvalid:
    """复现 glm provider 收到非首位 system 消息 → messages 参数非法。

    关键代码证据：_message_normalizer.py:639
        if provider != "minimax":
            return messages
    即对 glm/yichengc 不做非首位 system→user 转换。
    而 compression_messages 注入的全是 role=system（prompt_build 886-944 行）。
    """

    def test_glm_keeps_non_first_system_messages_unchanged(self):
        """验证：glm provider 下，非首位 system 消息原样保留（潜在 BadRequestError 来源）。"""
        from plugins.core.llm_core._message_normalizer import normalize_messages_for_provider

        # 模拟 _build_messages 的输出：
        # [system_message] + compression_messages(system) + history(含中间system) + dynamic_vars(system)
        messages = [
            {"role": "system", "content": "主系统提示词"},  # 首位 system（合法）
            {"role": "system", "name": "compressed", "content": "<compressed level=\"L1\">...摘要...</compressed>"},  # 非首位 system
            {"role": "system", "name": "state_snapshot", "content": "<current_state>...</current_state>"},  # 非首位 system
            {"role": "user", "content": "用户问题"},
            {"role": "assistant", "content": "助手回复"},
            {"role": "system", "name": "dynamic_context", "content": "<dynamic_vars>...</dynamic_vars>"},  # 末尾 system
        ]

        # glm 走默认 provider（zai/openai），不是 minimax
        normalized = normalize_messages_for_provider(
            messages, provider="zai", name="test", pipeline_id="glm-test",
        )

        # 统计非首位 system 消息
        non_first_systems = [m for i, m in enumerate(normalized) if m.get("role") == "system" and i > 0]
        print(f"\n[场景B] glm normalize 后非首位 system 消息数: {len(non_first_systems)}")
        for i, m in enumerate(normalized):
            print(f"  MSG-{i} role={m.get('role')} name={m.get('name', '-')}")

        # 这是诊断核心：glm 路径下非首位 system 被原样保留
        # 如果上游 yichengc/zhipu 严格限制 system 位置 → 报 messages 参数非法
        assert len(non_first_systems) >= 3, "应至少保留 3 条非首位 system（压缩块+快照+dynamic）"
        print("[场景B] ⚠️ 确认：glm 路径不转换非首位 system，这些消息原样发给上游")

    def test_minimax_converts_non_first_system_to_user(self):
        """对照组：minimax provider 会把非首位 system 转成 user。"""
        from plugins.core.llm_core._message_normalizer import normalize_messages_for_provider

        messages = [
            {"role": "system", "content": "主系统提示词"},
            {"role": "system", "name": "compressed", "content": "<compressed>...</compressed>"},
            {"role": "user", "content": "用户问题"},
            {"role": "system", "name": "dynamic_context", "content": "<dynamic_vars>...</dynamic_vars>"},
        ]

        normalized = normalize_messages_for_provider(
            messages, provider="minimax", name="test", pipeline_id="mm-test",
        )

        non_first_systems = [m for i, m in enumerate(normalized) if m.get("role") == "system" and i > 0]
        converted_to_user = [m for m in normalized if m.get("role") == "user" and m.get("content", "").startswith("<")]
        print(f"\n[场景B对照] minimax normalize 后非首位 system: {len(non_first_systems)}")
        for i, m in enumerate(normalized):
            print(f"  MSG-{i} role={m.get('role')} name={m.get('name', '-')}")
        assert len(non_first_systems) == 0, "minimax 应把所有非首位 system 转成 user"
        print("[场景B对照] minimax 正确转换了非首位 system")

    def test_glm_with_tool_call_pairing_and_systems(self):
        """复现：压缩写回后 history 含 tool_call 配对 + 中间 system，过 normalizer。"""
        from plugins.core.llm_core._message_normalizer import normalize_messages_for_provider

        # 模拟压缩后写回的 messages（pure_system + recent），
        # 再经 _build_messages 拼成完整请求
        messages = [
            {"role": "system", "content": "主提示词"},
            {"role": "system", "name": "compressed", "content": "<compressed level=\"L1\">历史摘要</compressed>"},
            # recent 段（压缩保留的最近消息）
            {"role": "user", "content": "读文件"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_abc123", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_abc123", "name": "read_file", "content": "文件内容"},
            {"role": "assistant", "content": "文件内容是..."},
            {"role": "system", "name": "dynamic_context", "content": "<dynamic_vars>时间</dynamic_vars>"},
        ]

        normalized = normalize_messages_for_provider(
            messages, provider="zai", name="test", pipeline_id="glm-tool-test",
        )

        print(f"\n[场景B-tool] glm normalize 结果 ({len(normalized)} 条):")
        for i, m in enumerate(normalized):
            tc = m.get("tool_calls")
            print(f"  MSG-{i} role={m.get('role')} name={m.get('name', '-')} "
                  f"{'tool_calls=' + str(len(tc)) if tc else ''}")

        # tool_call 配对应保持完整（配对校验对所有 provider 生效）
        assistant_tc = [m for m in normalized if m.get("tool_calls")]
        tool_results = [m for m in normalized if m.get("role") == "tool"]
        assert len(assistant_tc) == 1 and len(tool_results) == 1, "tool_call 配对应保留"
        # 非首位 system 仍保留（glm 不转换）
        non_first_sys = [m for i, m in enumerate(normalized) if m.get("role") == "system" and i > 0]
        print(f"[场景B-tool] 非首位 system 保留 {len(non_first_sys)} 条（潜在非法来源）")
