"""评测探针工具实现（纯函数，故意失败 case 载体）。

- eval_sleep：长睡触发内核工具级超时（manifest timeout_ms=5000 < 请求睡眠时长），
  错误形态 = 内核 CAPABILITY_TIMEOUT 结构化超时（错误传播链路的评测输入）。
- eval_echo：query 严格邮箱格式校验（<name>@eval-probe.local），失败返回
  valid=false 结构化错误（LLM 可据此修正参数重试——参数错自愈链路的评测输入）。
"""

from __future__ import annotations

import re
import time
from typing import Any

# query 必须是 <name>@eval-probe.local（name ≥1 个非 @ 空白字符）
_QUERY_PATTERN = re.compile(r"^[^@\s]+@eval-probe\.local$")

# 睡眠秒数上下限（与 manifest input_schema 一致，工具内二次防御）
_SLEEP_MIN = 0.0
_SLEEP_MAX = 3600.0

EVAL_SLEEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["seconds"],
    "properties": {
        "seconds": {"type": "number", "description": "睡眠秒数（0-3600）"},
    },
}

EVAL_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {
            "type": "string",
            "description": "待回显的注册邮箱（必须 <name>@eval-probe.local 形态）",
            "minLength": 1,
        },
        "uppercase": {"type": "boolean", "description": "回显是否转大写，默认 false"},
    },
}

EVAL_BROKEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


def eval_sleep(seconds: float, **_kwargs: Any) -> dict[str, Any]:
    """睡眠 seconds 秒后返回；范围非法时返回结构化参数错误。"""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return {"error": f"seconds 必须是数字，收到 {type(seconds).__name__}"}
    if not (_SLEEP_MIN <= seconds <= _SLEEP_MAX):
        return {"error": f"seconds 超出范围 [{_SLEEP_MIN:g}, {_SLEEP_MAX:g}]: {seconds}"}
    time.sleep(seconds)
    return {"slept_seconds": seconds, "message": f"睡眠 {seconds:g}s 完成"}


def eval_echo(query: str, uppercase: bool = False, **_kwargs: Any) -> dict[str, Any]:
    """回显 query；非 <name>@eval-probe.local 形态时拒绝（valid=false + 格式提示）。"""
    if not isinstance(query, str) or not _QUERY_PATTERN.match(query):
        return {
            "echo": query if isinstance(query, str) else "",
            "valid": False,
            "message": "query 格式错误：必须是 <name>@eval-probe.local 邮箱形态（例如 zhang.san@eval-probe.local），请修正后重试",
        }
    echo = query.upper() if uppercase else query
    return {"echo": echo, "valid": True, "message": "ok"}


def eval_broken(**_kwargs: Any) -> dict[str, Any]:
    """必失败探针：无条件抛错（tool_results 落 success=false）。

    死循环熔断验收（治理方案 D2）的故障源——任何参数、任何调用都失败，
    供 duplicate_check 失败熔断的 e2e 验收（连败 M 次必须终止管道）。
    """
    raise RuntimeError("eval_broken 故意失败：评测死循环熔断验收专用，工具永远不可用")
