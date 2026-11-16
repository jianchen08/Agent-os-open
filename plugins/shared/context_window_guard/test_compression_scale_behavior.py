# @feature: FP-0.2.〇 规模化压缩行为 | @ci: python-coverage
"""G2 形态规模化压缩行为测试（2026-10-24 口径统一刀随刀验证）。

用户验收质询的三问在此作答（断行为不断实现）：
1. 大数据量：G2 事故量级复现（百余轮 × 每轮大块思考，思考占消息本体
   85%+），压缩必须真发生且吃完被压区思考；
2. 多种类 message：user / assistant（思考 / 空正文 / 多模态 list content）/
   tool 配对 / 纯 system / 畸形 tool_calls=None 混排不炸；
3. 断言正确性：断压缩的行为结果（思考消失量、块存在、recent 保留、
   配对完整、端到端估算达标、压缩模型看得见思考），不断内部步骤。

场景锚：71c1768d578f 实测（正文 42.7 万字符 vs 思考 286.6 万字符，
input 滚到窗口 101.46% 后 MiniMax 400 拒绝；压缩 40 次触发 33 次空转）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_plugin_module() -> Any:
    """动态加载 plugin.py（file-location 精确加载，与 test_plugin_branches
    同款——平铺布局下裸名 import plugin 会撞其他插件目录）。"""
    mod_name = "cwg_scale_behavior_test"
    sys.modules.pop(mod_name, None)
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _RecordingLlm:
    """伪压缩 LLM：记录每次调用的入参并返回预设响应（外部依赖桩）。"""

    def __init__(self, response: str) -> None:
        self.calls: list[Any] = []
        self._response = response

    async def __call__(self, payload: Any) -> str:
        self.calls.append(payload)
        return self._response


def _valid_compress_json() -> str:
    """合法的 5 部分 JSON 压缩响应（l1 非空，供压缩主链路使用）。"""
    return """{
  "l1": {
    "session_title": "测试会话",
    "workflow": "完成了 X 任务",
    "errors_and_corrections": null,
    "decisions": null,
    "key_results": null
  },
  "l2": {
    "intent": "做 X",
    "process": "步骤 A 然后 B",
    "results": "产出 X | 待办 Y"
  },
  "keywords": ["关键词1"],
  "state_snapshot": {
    "current_state": "进行中",
    "task_specification": "测试任务",
    "pending": "收尾",
    "key_entities": "entity_a",
    "domain_knowledge": "约束 Z",
    "user_feedback": "",
    "attention_hints": "注意 Q"
  },
  "memory_items": {
    "user_profile_updates": null,
    "project_knowledge_updates": null,
    "experience_updates": null
  }
}"""


def _g2_shape_sequence(rounds: int = 120) -> list[dict[str, Any]]:
    """G2 调研形态消息序列：每轮 user 提问 + assistant(大块思考+短正文+工具调用)
    + tool 结果；思考体量按实测比例（思考:正文 ≈ 6.7:1）。

    seq 单调递增（切分/块原位占槽依赖 seq 坐标）。
    """
    messages: list[dict[str, Any]] = []
    seq = 0
    for r in range(rounds):
        seq += 1
        messages.append(
            {"role": "user", "content": f"调研问题 {r}：插件路由机制", "seq": seq}
        )
        seq += 1
        messages.append(
            {
                "role": "assistant",
                "content": f"第 {r} 轮结论：路由经 tool_ids 白名单。",
                "reasoning_content": "思考" * (2000 + r * 10),  # ~4k+ 字符/轮
                "tool_calls": [
                    {
                        "id": f"call-{r}",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": '{"path": "x.py"}'},
                    }
                ],
                "seq": seq,
            }
        )
        seq += 1
        messages.append(
            {
                "role": "tool",
                "name": "file_read",
                "tool_call_id": f"call-{r}",
                "content": "文件内容" * 120,  # ~480 字符工具结果
                "seq": seq,
            }
        )
    return messages


def _reasoning_chars(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(m.get("reasoning_content") or "")
        for m in messages
        if isinstance(m, dict)
    )


def _content_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
    return total


class TestG2ScaleCompression:
    """G2 量级（~360 条 / 60 万字符 / 思考占 80%+）端到端压缩行为。"""

    WINDOW = 64_000  # token 估窗口；触发线 0.6×64k=38.4k；recent 预算默认 0.18×64k

    def test_reasoning_heavy_sequence_actually_compresses(self) -> None:
        """思考占大头的序列必须真压缩：块出现、被压区思考被吃、recent 保留、
        端到端估算降到触发线下（G2 33 次"压无可压"的反向复现）。"""
        mod = _load_plugin_module()
        messages = _g2_shape_sequence()
        before_est = sum(mod.estimate_message_tokens(m) for m in messages)
        before_reasoning = _reasoning_chars(messages)
        assert before_reasoning > _content_chars(messages) * 5, (
            "场景自检：思考体量必须远超正文（G2 实测 6.7:1），否则场景失真"
        )
        assert before_est > self.WINDOW * 0.6, "场景自检：总量必须超触发线"

        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        result = _run(
            svc.compress_messages(messages, context_window=self.WINDOW, trigger_ratio=0.6)
        )

        assert result is not None, "压缩必须发生（旧口径此处空转，修复后必须产出）"
        after_est = sum(mod.estimate_message_tokens(m) for m in result)
        after_reasoning = _reasoning_chars(result)
        blocks = [
            m
            for m in result
            if isinstance(m.get("metadata"), dict) and "compression_ref" in m["metadata"]
        ]

        # 行为断言 1：端到端达标——压后估算低于触发线（这是 G2 事故里
        # "压缩完成但真实 input 不降"的反面：尺子统一后达标即真达标）
        assert after_est < self.WINDOW * 0.6, (
            f"压后估算 {after_est} 必须低于触发线 {int(self.WINDOW * 0.6)}"
        )
        # 行为断言 2：思考被吃掉（被压区思考进摘要，仅 recent 内保留）
        assert after_reasoning < before_reasoning * 0.15, (
            f"思考必须被压缩吃掉：{before_reasoning} → {after_reasoning} 字符"
        )
        # 行为断言 3：压缩块存在
        assert blocks, "压缩产出必须包含压缩块消息"
        # 行为断言 4：最新消息原样保留（recent 段不被压）
        assert result[-1]["role"] == "tool"
        assert result[-1]["tool_call_id"] == f"call-{len(_g2_shape_sequence()) // 3 - 1}"

    def test_tool_pairs_never_split_across_compression(self) -> None:
        """配对完整性：压后序列里每个 tool 结果都有同序列的配对 assistant
        调用（切分线不得把 call/result 对拆散——_split_preserving_tool_pairs
        契约的行为级验证）。"""
        mod = _load_plugin_module()
        messages = _g2_shape_sequence(rounds=80)
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        result = _run(
            svc.compress_messages(messages, context_window=self.WINDOW, trigger_ratio=0.6)
        )
        assert result is not None

        call_ids = set()
        for m in result:
            for tc in m.get("tool_calls") or []:
                if tc.get("id"):
                    call_ids.add(tc["id"])
        tool_ids = {
            m["tool_call_id"] for m in result if m.get("role") == "tool" and m.get("tool_call_id")
        }
        assert tool_ids <= call_ids, (
            f"孤儿 tool 结果（assistant 配对被压掉）：{sorted(tool_ids - call_ids)[:5]}"
        )

    def test_compress_llm_sees_reasoning_in_fork(self) -> None:
        """压缩模型看得见思考：fork 载荷中被压 assistant 带 reasoning_content
        原文（思考+正文一起进压缩块的前提——fork 前缀契约）。"""
        mod = _load_plugin_module()
        messages = _g2_shape_sequence(rounds=60)
        llm = _RecordingLlm(_valid_compress_json())
        svc = mod.CompressionService(backend=None, llm_call_fn=llm)
        result = _run(
            svc.compress_messages(messages, context_window=self.WINDOW, trigger_ratio=0.6)
        )
        assert result is not None
        assert llm.calls, "压缩 LLM 必须被调用"
        saw_reasoning = any(
            isinstance(m, dict) and m.get("reasoning_content")
            for call in llm.calls
            for m in (call if isinstance(call, list) else [call])
        )
        assert saw_reasoning, "fork 载荷必须携带被压消息的思考全文"

    def test_old_ruler_would_have_skipped_new_ruler_compresses(self) -> None:
        """尺子修复有效性（性质断言）：构造"旧口径全在 recent 预算内、
        新口径远超"的序列——旧实现此处 split_idx=0 压无可压（G2 事故形态），
        新实现必须压缩且产出块。"""
        mod = _load_plugin_module()
        # 少量消息：正文小（旧口径 total 小）、思考大（新口径 total 大）
        messages = [
            {"role": "user", "content": "u" * 100, "seq": 1},
            {
                "role": "assistant",
                "content": "a",
                "reasoning_content": "t" * 40_000,
                "seq": 2,
            },
            {"role": "user", "content": "u" * 100, "seq": 3},
        ]
        window = 20_000  # recent 预算 0.18×20k=3.6k token
        old_style = sum(max(1, len(str(m.get("content", ""))) // 2) for m in messages)
        new_style = sum(mod.estimate_message_tokens(m) for m in messages)
        assert old_style < 3_600 < new_style, (
            "场景自检：旧口径须在 recent 预算内（压无可压），新口径须远超"
        )
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        result = _run(
            svc.compress_messages(messages, context_window=window, trigger_ratio=0.6)
        )
        assert result is not None, "新口径下必须发生压缩（旧实现此处返回 None）"
        blocks = [
            m
            for m in result
            if isinstance(m.get("metadata"), dict) and "compression_ref" in m["metadata"]
        ]
        assert blocks
        assert _reasoning_chars(result) < 40_000 * 0.5, "大块思考必须被吃掉"


class TestVarietyMessageKinds:
    """多种类 message 混排：压缩路径不炸、system 全保留、空/畸形形态不死循环。"""

    def test_variety_kinds_compress_without_crash(self) -> None:
        """system（纯）/ 多模态 list content / 空正文 assistant / 畸形
        tool_calls=None / 超长单条 混排 → 压缩完成；纯 system 消息全保留。"""
        mod = _load_plugin_module()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "S" * 500, "seq": 1},
            {"role": "system", "content": "T" * 500, "seq": 2},
            {"role": "user", "content": "start", "seq": 3},
        ]
        seq = 3
        for r in range(30):
            seq += 1
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": f"图注 {r}"}], "seq": seq}
            )  # 多模态 list content（非字符串形态）
            seq += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "思" * 3000,
                    "seq": seq,
                }
            )  # 纯思考（空正文）
            seq += 1
            messages.append(
                {"role": "assistant", "content": "ok", "tool_calls": None, "seq": seq}
            )  # 畸形规范化形态（JSON null）
        seq += 1
        messages.append({"role": "user", "content": "L" * 30_000, "seq": seq})  # 超长单条

        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        result = _run(
            svc.compress_messages(messages, context_window=16_000, trigger_ratio=0.6)
        )
        assert result is not None, "多种类混排必须完成压缩而非异常/空转"
        system_kept = [m for m in result if m.get("role") == "system" and "metadata" not in m]
        assert len(system_kept) == 2, "纯 system 消息（非压缩块）必须全保留"
        seqs = [m["seq"] for m in result if isinstance(m.get("seq"), int)]
        assert seqs == sorted(seqs), "压后序列 seq 必须单调（原位占槽契约）"

    def test_empty_sequence_and_all_recent_no_crash(self) -> None:
        """边界形态：空序列与"全在 recent 预算内（真无需压）"均安全返回。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        assert _run(svc.compress_messages([], context_window=8_000)) is None
        tiny = [
            {"role": "user", "content": "u", "seq": 1},
            {"role": "assistant", "content": "a", "seq": 2},
        ]
        assert _run(
            svc.compress_messages(tiny, context_window=1_000_000, trigger_ratio=0.6)
        ) is None, "真正无需压缩（新口径也在预算内）→ 不压不炸"


class TestCompactionInstructionStageContract:
    """压缩指令的阶段衔接契约（2026-10-24 用户裁定）：快照是接手者的
    状态锚——已完成的阶段标注"已完成"、pending 首条=下一步具体动作、
    禁止模糊形态。G2 循环事故：快照写"收尾中"→模型每轮恢复到
    "正要开始"的现场，40 分钟出不来。"""

    def test_instruction_carries_stage_contract(self) -> None:
        """指令文本三要素在场：衔接契约/已完成标注要求/具体动作要求。"""
        mod = _load_plugin_module()
        text = mod.ContextCompressor.COMPACTION_INSTRUCTION
        assert "哪些阶段已完成、当前正在做什么、下一步的具体动作" in text
        assert "已完成" in text
        assert "禁止写成" in text
        assert "模糊形态" in text

    def test_current_state_requires_done_annotation(self) -> None:
        """current_state 提取契约：已完成阶段必须标注，防接手者重复劳动。"""
        mod = _load_plugin_module()
        text = mod.ContextCompressor.COMPACTION_INSTRUCTION
        idx = text.find("### current_state")
        assert idx > 0
        section = text[idx : text.find("### task_specification")]
        assert "已完成" in section

    def test_pending_requires_concrete_next_action(self) -> None:
        """pending 契约：第一条=可执行粒度动作，泛写不合格清单明示。"""
        mod = _load_plugin_module()
        text = mod.ContextCompressor.COMPACTION_INSTRUCTION
        idx = text.find("### pending")
        assert idx > 0
        section = text[idx : text.find("### key_entities")]
        assert "第一条 = 下一步的具体动作" in section
        assert "不合格" in section

    def test_fork_payload_carries_stage_contract(self) -> None:
        """行为级：fork 载荷（发给压缩 LLM 的实际指令）携带阶段契约。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=_RecordingLlm(_valid_compress_json()))
        fork = compressor._build_fork_messages(
            [{"role": "user", "content": "x", "seq": 1}]
        )
        instruction = [m for m in fork if m.get("role") == "user"][-1]["content"]
        assert "哪些阶段已完成、当前正在做什么、下一步的具体动作" in instruction


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
