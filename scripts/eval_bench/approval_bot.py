"""审批自动响应器（评测无人值守审批链路的替代人工）。

账本自录（审批请求与响应只存内核内存，无持久化面——拦截率由 harness 记账）：
- requests_seen：收到的审批请求数
- approved / denied：按策略自动响应的计数
- response_errors：响应提交失败明细（审批挂起会导致 case 超时，报告可见）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_APPROVE_OPTION = "批准"
_DENY_OPTION = "拒绝"


class ApprovalBot:
    """按策略自动响应审批请求：approve=全部放行，deny=全部拦截。"""

    def __init__(self, client: Any, policy: str = "approve") -> None:
        if policy not in ("approve", "deny"):
            raise ValueError(f"未知审批策略: {policy}")
        self.client = client
        self.policy = policy
        self.ledger: dict[str, Any] = {
            "requests_seen": 0,
            "approved": 0,
            "denied": 0,
            "response_errors": [],
        }

    def handle(self, payload: dict[str, Any]) -> str:
        """interaction_request 回调：提交响应并记账，返回动作描述。"""
        self.ledger["requests_seen"] += 1
        request_id = payload.get("request_id") or payload.get("id") or ""
        # choice 型审批选项文本优先匹配声明选项；confirm 型回退内置批准/拒绝文本
        options = payload.get("options") or []
        if self.policy == "approve":
            selected = _match_option(options, _APPROVE_OPTION) or _APPROVE_OPTION
        else:
            selected = _match_option(options, _DENY_OPTION) or _DENY_OPTION
        try:
            self.client.respond_interaction(request_id, selected)
        except Exception as exc:  # noqa: BLE001 — 记账后继续收流（挂起超时会在报告暴露）
            self.ledger["response_errors"].append(
                {"request_id": request_id, "error": str(exc)}
            )
            logger.warning("[approval-bot] 响应提交失败 request_id=%s: %s", request_id, exc)
            return f"error: {exc}"
        if self.policy == "approve":
            self.ledger["approved"] += 1
        else:
            self.ledger["denied"] += 1
        return f"{self.policy}:{selected}"

    @property
    def intercept_rate(self) -> float | None:
        """拦截率 = 拒绝数 / 请求数；零请求返回 None（不产 0/0 假口径）。"""
        seen = self.ledger["requests_seen"]
        return self.ledger["denied"] / seen if seen else None


def _match_option(options: list[Any], keyword: str) -> str | None:
    """在选项列表里找含关键词的选项文本（审批选项措辞不统一时的宽容匹配）。"""
    for opt in options:
        if isinstance(opt, str) and keyword in opt:
            return opt
    return None
