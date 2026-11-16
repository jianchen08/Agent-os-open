"""角色状态统一标记机制的共享解析面（2026-09-28 设计，单源真值）。

标记语法（AI 输出约定，rules/*.md 口径声明）::

    <state>
      {"state": {"好感度": 42}, "memory": "用户透露出生地是北境"}
    </state>

- 单标记容器 `<state>`，内体 JSON 对象；**键 = 目标账本**（分发白名单在消费侧，
  本模块只做语法层：提取/解析/区间）。
- 多标记取**最后一个**（防重放重复落账）；坏 JSON / 非 dict = None（fail-soft，
  消费方 warning 降级）。
- span = 标记段（含标签）在原文的 [start, end) 字符区间——前端渲染层据此隐藏
  原文（展示剥离归渲染层，落库原文保真）。

本模块被 Python 侧（解析步）与前端（对账测试基线）共同引用：正则形态变更须
双侧同步（机械闸对账，tests/test_state_marker_parity.py）。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 非贪婪：标记内体不含 "</state>"（约定 JSON 文本不出现该串）；DOTALL 跨行。
STATE_MARKER_RE = re.compile(r"<state>\s*(\{.*?\})\s*</state>", re.DOTALL)

MARKER_OPEN, MARKER_CLOSE = "<state>", "</state>"


def parse_state_marker(text: str) -> dict[str, Any] | None:
    """提取文本中最后一个 <state> 标记并解析内体 JSON。

    Returns:
        {"entries": dict, "span": [start, end]}；无标记 / 坏 JSON / 内体非
        dict = None（零标记是常态合法形态，调用方零动作）。
    """
    if not text or MARKER_OPEN not in text:
        return None
    matches = list(STATE_MARKER_RE.finditer(text))
    if not matches:
        # 有开标签但形态不完整（如未闭合）：视为噪声，零提取
        return None
    last = matches[-1]
    try:
        entries = json.loads(last.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(entries, dict):
        return None
    return {"entries": entries, "span": [last.start(), last.end()]}


def strip_by_span(text: str, span: list[int] | tuple[int, int]) -> str:
    """按区间精确切除标记段本身（前段+后段拼接——标记可能在正文中间，
    后半正文不可丢），并收敛接缝处的多余空行。"""
    start, end = int(span[0]), int(span[1])
    if not text or start < 0 or end > len(text) or start >= end:
        return text
    merged = text[:start] + text[end:]
    return re.sub(r"\n{3,}", "\n\n", merged).strip()


def strip_state_marker(text: str) -> str:
    """移除文本中全部 <state> 标记段并收敛多余空行（渲染层剥离参考实现）。"""
    if not text or MARKER_OPEN not in text:
        return text
    stripped = STATE_MARKER_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()
