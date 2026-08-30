"""cost_control tokenizer 测试——TokenCounter 计数/估算/截断与单例工厂。

覆盖：

- count_tokens：空输入归零、与 tiktoken 真值一致；模型前缀 → 编码映射
  （_encoding_for_model：gpt-4o 走 o200k_base，与 cl100k 可区分）；
- count_messages：固定开销公式（4/message + 2+2 字段名 + 3 收尾）、
  不支持的模型 ValueError、KeyError 包装为 ValueError；
- count_message：单条消息开销公式；
- estimate_tokens：中英文混合快速估算规则；
- truncate_text：预算内直通、超预算按 token 精确截断、编码异常时按
  字符比例回退（monkeypatch 注入坏 encoding，不 mock 外部服务）；
- truncate_messages：预算内直通、keep_first/keep_last 中间丢弃、无锚点
  时保留最新；
- get_token_counter：单例复用与按编码名重建（测试后复位全局单例，不
  污染同进程其他用例）。

tiktoken 编码文件本地缓存可用，无需网络。
"""

from __future__ import annotations

import pytest
import tiktoken

import tokenizer as tokenizer_mod
from tokenizer import TokenCounter, get_token_counter

pytestmark = pytest.mark.unit

_CL = tiktoken.get_encoding("cl100k_base")
_O2 = tiktoken.get_encoding("o200k_base")


@pytest.fixture(autouse=True)
def _reset_singleton():
    """用例后复位全局单例，避免影响同进程其他用例的编码假设。"""
    yield
    tokenizer_mod._default_counter = None


class TestCountText:
    def test_empty_returns_zero(self) -> None:
        counter = TokenCounter()
        assert counter.count_tokens("") == 0
        assert counter.count_tokens(None) == 0  # type: ignore[arg-type]

    def test_matches_cl100k_ground_truth(self) -> None:
        counter = TokenCounter()
        text = "hello world 你好世界"
        assert counter.count_tokens(text) == len(_CL.encode(text))

    def test_gpt4o_longest_prefix_wins(self) -> None:
        # 最长前缀优先：gpt-4o 命中 "gpt-4o"（o200k_base）而非被短前缀
        # "gpt-4"（cl100k_base）遮蔽——两编码对该文本计数确实可区分
        counter = TokenCounter()
        assert counter._encoding_for_model("gpt-4o") == "o200k_base"
        text = "😀 emoji 中文测试"
        assert len(_O2.encode(text)) != len(_CL.encode(text))  # 两者确实可区分

    def test_instance_encoding_used_by_count_tokens(self) -> None:
        # 显式构造 o200k_base 的实例按自身编码计数（不受前缀映射影响）
        counter = TokenCounter("o200k_base")
        text = "😀 emoji 中文测试"
        assert counter.count_tokens(text) == len(_O2.encode(text))

    def test_glm_and_deepseek_map_to_cl100k(self) -> None:
        counter = TokenCounter()
        assert counter._encoding_for_model("glm-4") == "cl100k_base"
        assert counter._encoding_for_model("deepseek-chat") == "cl100k_base"


class TestCountTokens:
    def test_empty_returns_zero(self) -> None:
        assert TokenCounter().count_tokens("") == 0

    def test_matches_instance_encoding(self) -> None:
        counter = TokenCounter("o200k_base")
        text = "你好，世界！ Hello world."
        assert counter.count_tokens(text) == len(_O2.encode(text))

    def test_invalid_encoding_name_fallback_unreachable(self) -> None:
        # tiktoken 0.13 对未知编码名抛 ValueError（非 KeyError），
        # 构造器 except KeyError 回退分支在当前依赖下不可达——
        # 断言其真实行为：直接抛 ValueError（不静默吞错）
        with pytest.raises(ValueError):
            TokenCounter("no_such_encoding")


class TestCountMessages:
    def test_empty_list_overhead_only(self) -> None:
        # 0 条消息：只剩回复开销 3
        assert TokenCounter().count_messages([], "gpt-4") == 3

    def test_single_empty_message_formula(self) -> None:
        # 1 条空消息：4（per-message）+ 0（role/content）+ 2 + 2（字段名）+ 3 = 11
        assert TokenCounter().count_messages([{"role": "", "content": ""}], "gpt-4") == 11

    def test_formula_matches_ground_truth(self) -> None:
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "写一个快速排序"},
            {"role": "assistant", "content": "def quicksort(arr): ..."},
        ]
        expected = (
            4 * len(messages)
            + sum(len(_CL.encode(m["role"])) + len(_CL.encode(m["content"])) for m in messages)
            + 4 * len(messages)  # 每条消息 2+2 字段名开销
            + 3
        )
        assert counter.count_messages(messages, "gpt-4") == expected

    def test_missing_fields_default_to_empty(self) -> None:
        # 无 role/content 键的消息按空串计（不抛 KeyError）
        assert TokenCounter().count_messages([{}], "gpt-4") == 11

    def test_unsupported_model_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="不支持的模型名称"):
            TokenCounter().count_messages([{"role": "user", "content": "x"}], "llama-3")


class TestCountMessage:
    def test_single_message_formula(self) -> None:
        counter = TokenCounter()
        message = {"role": "user", "content": "hello"}
        expected = 4 + len(_CL.encode("user")) + len(_CL.encode("hello")) + 4
        assert counter.count_message(message) == expected

    def test_empty_message_minimum(self) -> None:
        assert TokenCounter().count_message({"role": "", "content": ""}) == 8  # 4 + 0 + 4


class TestEstimateTokens:
    def test_empty_returns_zero(self) -> None:
        assert TokenCounter().estimate_tokens("") == 0

    def test_pure_english(self) -> None:
        # 8 个 ASCII 字母 ÷ 4 = 2
        assert TokenCounter().estimate_tokens("abcdefgh") == 2

    def test_pure_chinese(self) -> None:
        # 4 个中文字符 ÷ 2 = 2
        assert TokenCounter().estimate_tokens("中文字符") == 2

    def test_mixed_content(self) -> None:
        # 4 英文字母 ÷ 4 + 2 中文 ÷ 2 = 1 + 1
        assert TokenCounter().estimate_tokens("abcd中文") == 2

    def test_other_chars_counted(self) -> None:
        # 数字+空格 8 个 ÷ 3 ≈ 2（int 截断）
        assert TokenCounter().estimate_tokens("12345678") == 2


class TestTruncateText:
    def test_within_budget_unchanged(self) -> None:
        counter = TokenCounter()
        text = "short text stays"
        assert counter.truncate_text(text, 1000) == text

    def test_over_budget_truncated_by_tokens(self) -> None:
        counter = TokenCounter()
        text = " ".join(f"word{i}" for i in range(200))
        result = counter.truncate_text(text, 5)
        assert result == _CL.decode(_CL.encode(text)[:5])

    def test_encoder_failure_falls_back_to_char_ratio(self, monkeypatch) -> None:
        counter = TokenCounter()

        class _BrokenEncoding:
            def encode(self, _text):
                raise RuntimeError("encoder unavailable")

            def decode(self, _tokens):
                raise RuntimeError("encoder unavailable")

        monkeypatch.setattr(counter, "encoding", _BrokenEncoding())
        text = "x" * 1000
        result = counter.truncate_text(text, 10)
        # 按字符比例截断且留 10% 余量：明显短于原文、是原文前缀
        assert 0 < len(result) < len(text)
        assert text.startswith(result)


class TestTruncateMessages:
    @staticmethod
    def _msg(content: str) -> dict[str, str]:
        return {"role": "user", "content": content}

    def test_within_budget_unchanged(self) -> None:
        counter = TokenCounter()
        messages = [self._msg("small"), self._msg("also small")]
        assert counter.truncate_messages(messages, 10_000) == messages

    def test_keep_first_keep_last_drop_middle(self) -> None:
        counter = TokenCounter()
        messages = [
            self._msg("系统指令"),  # keep_first
            self._msg("很长的历史消息" * 50),
            self._msg("另一条很长的历史消息" * 50),
            self._msg("最新问题"),  # keep_last
        ]
        # 预算只够首尾两条
        budget = (
            counter.count_messages(messages[:1], "gpt-4")
            + counter.count_messages(messages[-1:], "gpt-4")
        )
        result = counter.truncate_messages(messages, budget, keep_first=1, keep_last=1)
        assert result[0] is messages[0]  # 首条原样保留
        assert result[-1] is messages[-1]  # 尾条原样保留
        assert all(m not in messages[1:3] for m in result)  # 中间被丢弃

    def test_keep_anchors_with_partial_middle(self) -> None:
        # 预算富余：中间靠新的部分消息仍被保留（反向填充，保留最新的）
        counter = TokenCounter()
        messages = [
            self._msg("系统指令"),  # keep_first
            self._msg("巨大的历史块" * 100),  # 老的中间消息，放不下
            self._msg("较小的近期消息"),  # 新的中间消息，放得下
            self._msg("最新问题"),  # keep_last
        ]
        budget = (
            counter.count_messages(messages[:1], "gpt-4")
            + counter.count_messages(messages[-1:], "gpt-4")
            + counter.count_messages([messages[2]], "gpt-4")
        )
        result = counter.truncate_messages(messages, budget, keep_first=1, keep_last=1)
        assert result == [messages[0], messages[2], messages[3]]
        assert messages[1] not in result  # 放不下的老中间消息被丢弃

    def test_no_anchor_keeps_newest(self) -> None:
        counter = TokenCounter()
        messages = [self._msg(f"消息编号{i}内容" * 20) for i in range(10)]
        newest = messages[-1]
        # 预算仅够最后一条
        budget = counter.count_messages([newest], "gpt-4")
        result = counter.truncate_messages(messages, budget)
        assert result[-1] is newest  # 最新的保留
        assert len(result) < len(messages)  # 老消息被丢弃


class TestGetTokenCounter:
    def test_singleton_reused_for_same_encoding(self) -> None:
        first = get_token_counter()
        second = get_token_counter()
        assert first is second

    def test_different_encoding_rebuilds_singleton(self) -> None:
        first = get_token_counter()
        rebuilt = get_token_counter("o200k_base")
        assert rebuilt is not first
        assert rebuilt.encoding.name == "o200k_base"
        # 换回默认编码又重建
        again = get_token_counter()
        assert again is not rebuilt
        assert again.encoding.name == "cl100k_base"
