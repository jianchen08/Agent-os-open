"""管道 state 结构化字段的统一读取契约 — JSON 还原 + 形态校验。

背景（2026-08-28 管道 b8b92a56ad72 实测四问题）：state 结构化字段
（ws_meta / task.acceptance_criteria / track.llm_usage / security.decision /
termination_advisor.status / evaluation.detected_result 等）的生命周期跨三个
边界——引擎内存（原生 dict）→ 持久层 TEXT（serde_json::to_string 序列化）→
消费端（内核读路径有 from_str 还原，插件经 pipeline-state.list 能力拿到
serde Value 透传也为原生）。

边界是**契约**而非巧合：任何一条新读写路径（直连 SQL 读表、HTTP 层二次编码、
checkpoint 标量快照改造）漏掉还原，消费点 `isinstance(x, dict)` 就会静默拿空
——上游明明写了值，下游读到 None/{}，且无任何报错痕迹（实测事故面）。

本模块提供消费点的统一读取契约（裸名导入，与 tenant_data/http_json 先例一致）：

1. :func:`as_dict` —— 还原 + 校验一体：
   - 值是 dict → 原样返回；
   - 值是 JSON 字符串且解析为 dict → 还原返回（跨边界序列化形态）；
   - 其余（None/空串/解析为非 dict）→ 按校验语义报错或降级。

2. :func:`require_dict` —— 严格形态：缺失/形态错直接抛 StateFieldError
   （fail-closed，适合"上游必须写过这个字段"的契约位）。

3. :func:`optional_dict` —— 宽松形态：合法值还原，缺失/非法返回默认值并
   留 warning 痕迹（适合"可选增强字段"）。

[来源: 用户要求 2026-08-28——同类参数转换排查 + state 字段校验显式报错]
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class StateFieldError(TypeError):
    """state 结构化字段形态违约（上游未写/形态漂移），消费点显式失败。"""


def as_dict(value: Any, *, field: str, required: bool = False) -> dict[str, Any] | None:
    """state 字段值 → dict（JSON 字符串自动还原；形态错按 required 语义处理）。

    Args:
        value: 字段原始值（dict / JSON 字符串 / None / 其他）。
        field: 字段名（报错与日志定位用）。
        required: True = 值缺失或形态非法时抛 StateFieldError（强契约位）；
            False = 返回 None 并留 warning（可选字段位）。

    Returns:
        还原后的 dict；required=False 且值缺失/非法时返回 None。

    Raises:
        StateFieldError: required=True 且值缺失、JSON 解析失败或解析结果非 dict。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as exc:
            if required:
                raise StateFieldError(
                    f"state 字段 '{field}' 为 JSON 字符串但解析失败: {exc}"
                ) from exc
            logger.warning("[state_fields] 字段 '%s' JSON 解析失败（按缺失处理）: %s", field, exc)
            return None
        if isinstance(parsed, dict):
            return parsed
    if required:
        raise StateFieldError(
            f"state 字段 '{field}' 缺失或形态非法（期望 dict 或 JSON 对象字符串，"
            f"实际 {type(value).__name__}: {str(value)[:80]!r}）——"
            "上游生产方未写入或跨边界序列化形态漂移"
        )
    if value not in (None, ""):
        logger.warning(
            "[state_fields] 字段 '%s' 形态非法（期望 dict/JSON 字符串，实际 %s），按缺失处理",
            field,
            type(value).__name__,
        )
    return None


def require_dict(value: Any, *, field: str) -> dict[str, Any]:
    """严格形态：缺失/非法直接抛 StateFieldError（强契约位）。"""
    result = as_dict(value, field=field, required=True)
    assert result is not None  # required=True 下 as_dict 必返回 dict 或抛错
    return result


def optional_dict(value: Any, *, field: str) -> dict[str, Any]:
    """宽松形态：缺失/非法返回空 dict 并留 warning（可选字段位）。"""
    result = as_dict(value, field=field, required=False)
    return result if result is not None else {}
