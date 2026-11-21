"""evaluation_result 文本解析——跨插件单源（task_reminder / task_evaluate 共用）。

评估者（agent 型指标）在最终 assistant 文本里输出
``{"evaluation_result": {"passed": bool, "score": number|null, "feedback": ...,
"suggestions": [...]}}``。消费面两处：

- task_reminder：评估者模式下检测结论并收束评估子管道（写
  ``evaluation.detected_result``，同时投影子管道 task.status=completed）；
- task_evaluate：“评估子管道派发 → 轮询回收”路径在超时前对
  ``raw_result`` 做最终兜底解析（结论已在文中、检测面缺席时不丢结论）。

解析契约（fail-closed：坏 payload 不是证据，绝不把失败判成通过）：

- 候选 = 文本中花括号配平的 JSON 片段，从后往前逐个尝试（最后输出优先）；
- JSON 解析失败 → 跳过该候选继续往前（脏片段不毒化整体）；
- 形状：``evaluation_result`` 为 dict → 用它；否则顶层含 ``passed`` → 用顶层；
  两形状皆不命中 → 跳过；
- ``passed`` 仅接受 bool 或大小写不敏感的 ``"true"/"false"`` 字符串（strip 后
  比较）；其余类型（1/0/"yes" 等）不构成证据 → **整体返回 None**（不继续往前
  找——更早的候选可能是提示词里的示例 JSON，拿它判通过是危险方向）；
- ``score`` 仅接受 int/float（bool 除外）或有限数值字符串，其余（None/"N/A"/
  nan/inf）→ None（output_schema: number|null）；
- ``feedback`` 归一为字符串；``suggestions`` 非 list → []。
"""

from __future__ import annotations

import json
import math
from typing import Any


def _json_candidates(text: str) -> list[str]:
    """抽取文本中花括号配平的 JSON 候选片段（按出现顺序）。"""
    candidates: list[str] = []
    brace_depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == "}":
            if brace_depth == 0:
                # 无开括号的游离右括号：忽略（不计负深度，防后续配平错位）
                continue
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidates.append(text[start : i + 1])
                start = -1
    return candidates


def normalize_passed(raw: Any) -> bool | None:
    """``passed`` 归一：bool 原样；"true"/"false" 字符串大小写不敏感；其余 None。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def normalize_score(raw: Any) -> float | None:
    """``score`` 归一：int/float（bool 除外）或有限数值字符串 → float，其余 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def extract_evaluation_result(text: Any) -> dict[str, Any] | None:
    """从文本中提取有效的 evaluation_result 结论；无有效结论返回 None。

    Args:
        text: 候选文本（raw_result / assistant 消息内容）；非字符串按无结论。

    Returns:
        ``{"passed": bool, "score": float | None, "feedback": str,
        "suggestions": list}``；任何非法形态返回 None（不抛异常）。
    """
    if not isinstance(text, str) or not text:
        return None
    for candidate in reversed(_json_candidates(text)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        inner = parsed.get("evaluation_result")
        if isinstance(inner, dict):
            target = inner
        elif "evaluation_result" in parsed:
            # 形状对了但内容不是结论对象：畸形结论，判无证据（fail-closed）
            return None
        elif "passed" in parsed:
            target = parsed
        else:
            continue
        passed = normalize_passed(target.get("passed"))
        if passed is None:
            # 命中结论形状但 passed 类型不受支持：不构成证据，且不再往前找
            # （更早候选可能是提示词示例，不能拿来判通过）
            return None
        suggestions = target.get("suggestions")
        return {
            "passed": passed,
            "score": normalize_score(target.get("score")),
            "feedback": str(target.get("feedback") or ""),
            "suggestions": suggestions if isinstance(suggestions, list) else [],
        }
    return None
