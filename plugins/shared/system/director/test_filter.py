# @feature: FP-0.2.二 内容过滤两级端口 | @ci: python-coverage
"""两级内容过滤测试：敏感词/红名单/LLM 端口/fail-closed/文件缺失容错。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from filter import TwoTierContentFilter

pytestmark = pytest.mark.unit


class FakeLlmChecker:
    def __init__(self, allowed: bool = True, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        self.calls: list[tuple[str, str, list[str]]] = []

    async def __call__(self, narration: str, subtitle: str, visual_prompts: list[str]) -> tuple[bool, str]:
        self.calls.append((narration, subtitle, visual_prompts))
        return self.allowed, self.reason


def test_sensitive_word_hit_denies_with_location() -> None:
    tmp = Path(__file__).parent / "_test_words.txt"
    tmp.write_text("# 注释行\n违禁词\n", encoding="utf-8")
    flt = TwoTierContentFilter(llm_checker=FakeLlmChecker(), sensitive_words_path=str(tmp))
    allowed, reason = asyncio.run(flt("正文含违禁词片段", "字幕", ["prompt"]))
    assert not allowed
    assert reason.startswith("SENSITIVE:narration:违禁词")
    tmp.unlink()


def test_red_list_hit_denies_in_visual_prompt_too() -> None:
    rights = Path(__file__).parent / "_test_rights.yaml"
    rights.write_text("red:\n  - 已登记原梗\nyellow: []\ngreen: []\n", encoding="utf-8")
    flt = TwoTierContentFilter(
        llm_checker=FakeLlmChecker(), meme_rights_path=str(rights)
    )
    allowed, reason = asyncio.run(flt("干净正文", "干净字幕", ["a registered 已登记原梗 scene"]))
    assert not allowed
    assert reason.startswith("RED_LIST:visual[0]:已登记原梗")
    rights.unlink()


def test_clean_text_passes_to_llm_tier_and_through() -> None:
    llm = FakeLlmChecker(allowed=True)
    flt = TwoTierContentFilter(llm_checker=llm)
    allowed, reason = asyncio.run(flt("正文", "字幕", ["p1", "p2"]))
    assert allowed and reason == ""
    assert llm.calls[0] == ("正文", "字幕", ["p1", "p2"])


def test_llm_deny_propagates() -> None:
    flt = TwoTierContentFilter(llm_checker=FakeLlmChecker(allowed=False, reason="LLM:语义擦边"))
    allowed, reason = asyncio.run(flt("正文", "字幕", ["p"]))
    assert not allowed and reason == "LLM:语义擦边"


def test_llm_unbound_fails_closed() -> None:
    """D6 纪律：LLM 语义端口未绑定 → 拦截（不静默放行）。"""
    flt = TwoTierContentFilter(llm_checker=None)
    allowed, reason = asyncio.run(flt("正文", "字幕", ["p"]))
    assert not allowed
    assert reason == "FILTER_LLM_UNBOUND"


def test_missing_word_files_tolerated(tmp_path: Path) -> None:
    """词表/红名单文件缺失 → 空表放行路径不卡死（status 计数 0）。"""
    flt = TwoTierContentFilter(
        llm_checker=FakeLlmChecker(),
        sensitive_words_path=str(tmp_path / "nope.txt"),
        meme_rights_path=str(tmp_path / "nope.yaml"),
    )
    allowed, _ = asyncio.run(flt("正文", "字幕", ["p"]))
    assert allowed
    status = flt.status()
    assert status == {"sensitive_words": 0, "red_list": 0, "llm_bound": True}
