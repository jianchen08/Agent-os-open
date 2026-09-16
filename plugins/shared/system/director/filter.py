"""director 内容过滤两级端口实现（T1.5，细化设计 §6.1/D6/D9）。

两级：① 本地敏感词（词表文件，毫秒级）② LLM 语义审查（注入端口）。
红名单梗检查（meme_rights.yaml，D9）作用于全部文本面（narration/subtitle/
visual_prompts）。

fail-closed 纪律：
- LLM 语义端口未绑定 → 拦截（FILTER_LLM_UNBOUND）——内容安全不降级；
- 词表/红名单文件缺失 → 视为空表继续（放行路径不受配置缺失卡死），
  状态里记录 loaded 计数供巡检。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class LlmSemanticChecker(Protocol):
    """LLM 语义审查端口：文本组 → (allowed, reason)。"""

    async def __call__(self, narration: str, subtitle: str, visual_prompts: list[str]) -> tuple[bool, str]: ...


def _load_lines(path: str | None) -> list[str]:
    """读词表：一行一词；文件缺失/空路径 → 空表。"""
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    return [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _load_red_list(path: str | None) -> list[str]:
    """读 meme_rights.yaml 红名单（结构见 T1.6）；缺失 → 空表。"""
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    red = data.get("red", [])
    if isinstance(red, list):
        return [str(item) for item in red if item]
    return []


class TwoTierContentFilter:
    """两级内容过滤（director SegmentPipeline 的 ContentFilter 端口）。"""

    def __init__(
        self,
        llm_checker: LlmSemanticChecker | None,
        sensitive_words_path: str | None = None,
        meme_rights_path: str | None = None,
    ) -> None:
        self._llm = llm_checker
        self._sensitive = _load_lines(sensitive_words_path)
        self._red_list = _load_red_list(meme_rights_path)

    async def __call__(
        self, narration: str, subtitle: str, visual_prompts: list[str]
    ) -> tuple[bool, str]:
        """两级检查：①敏感词+红名单（本地，快）②LLM 语义（端口）。

        返回 (allowed, reason)；拦截 reason 带命中项（台账统计口径）。
        """
        surfaces = {"narration": narration, "subtitle": subtitle, **{
            f"visual[{i}]": prompt for i, prompt in enumerate(visual_prompts)
        }}
        for name, text in surfaces.items():
            for word in self._sensitive:
                if word in text:
                    return False, f"SENSITIVE:{name}:{word}"
            for term in self._red_list:
                if term in text:
                    return False, f"RED_LIST:{name}:{term}"
        if self._llm is None:
            return False, "FILTER_LLM_UNBOUND"
        return await self._llm(narration, subtitle, visual_prompts)

    def status(self) -> dict[str, Any]:
        """巡检观测：两级装载计数。"""
        return {
            "sensitive_words": len(self._sensitive),
            "red_list": len(self._red_list),
            "llm_bound": self._llm is not None,
        }
