"""oauth_broker → grants 账本接线缝（P2 覆盖面补全，ADR 2026-11-19）。

三个接线点（执行方案 §4）：
1. **授权成立 → issue**：``grants.ledger.request`` 落 domain grant（subject=user/*、
   tool=mcp、action=规范化 server_url、scope 携 domain/resource/scopes/env_prefix），
   幂等——同服务器已有活跃 oauth_broker grant 时复用其 id，不重复 issue
   （刷新 / 复用路径零重复）；
2. **撤销联动 revoke**：刷新被拒（凭据失效信号）→ 该服务器全部活跃 oauth_broker
   grant 逐条 revoke；
3. **记录面降级**：OAuth 授权是用户在浏览器当面完成的独立授权行为，账本在本批
   只是记录面（无执法消费方；执法面 fail-closed 语义属 security_check P1）——
   账本缺席/异常/被拒只留日志，绝不阻断授权主流程。

seam 形态沿 security_check ``approval_scope`` 同构先例：显式注入 cap（测试缝）优先，
生产路径经 ref resolver（``server.py`` 模块级 ``lambda: plugin``）按 provides
namespace（``grants.ledger``）解析；解析失败按 None（记录面缺席）处理。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: grant 形态常量（执行方案 §4：granularity=domain、source=oauth_broker）。
GRANT_SOURCE = "oauth_broker"
GRANT_GRANULARITY = "domain"
GRANT_TOOL = "mcp"
GRANT_SUBJECT_KIND = "user"
GRANT_SUBJECT_ID = "*"

_LEDGER_CAP: Any = None
_REF_RESOLVER: Callable[[], Any] | None = None


def set_ledger_cap(cap: Any) -> None:
    """注入 grants.ledger capability handle（测试/装配缝；传 None 恢复自动解析）。"""
    global _LEDGER_CAP  # noqa: PLW0603
    _LEDGER_CAP = cap


def set_ref_resolver(resolver: Callable[[], Any] | None) -> None:
    """注入 plugin ref 访问器（生产解析缝；server.py 模块级落 ``lambda: plugin``）。"""
    global _REF_RESOLVER  # noqa: PLW0603
    _REF_RESOLVER = resolver


def reset_link() -> None:
    """清空注入缝（测试钩子）。"""
    global _LEDGER_CAP, _REF_RESOLVER  # noqa: PLW0603
    _LEDGER_CAP = None
    _REF_RESOLVER = None


def get_ledger_cap() -> Any:
    """解析 grants.ledger cap：显式注入 > plugin ref 按 namespace 解析；缺席 = None。

    解析失败（grants 插件缺席 / ref 无该 capability）一律返回 None——调用方按
    记录面缺席降级，OAuth 主流程不受影响。
    """
    global _LEDGER_CAP  # noqa: PLW0603
    if _LEDGER_CAP is None:
        resolver = _REF_RESOLVER
        if resolver is None:
            return None
        try:
            ref = resolver()
        except Exception:  # noqa: BLE001 — ref 缺席按解析失败降级（记录面）
            return None
        if ref is None:
            return None
        try:
            _LEDGER_CAP = ref.get_capability("grants.ledger")
        except (KeyError, AttributeError):
            return None
    return _LEDGER_CAP


def _grant_matches(grant: Any, server_url: str) -> bool:
    """幂等匹配：本插件落的 grant（source=oauth_broker）且 action=该服务器。"""
    return (
        isinstance(grant, dict)
        and str(grant.get("source") or "") == GRANT_SOURCE
        and str(grant.get("action") or "") == server_url
    )


async def _list_active_server_grants(cap: Any, server_url: str) -> list[dict[str, Any]]:
    """列该服务器（user/*）活跃的 oauth_broker grant；形态异常按空表（收缩方向）。"""
    result = await cap.call(
        "list",
        {
            "subject_kind": GRANT_SUBJECT_KIND,
            "subject_id": GRANT_SUBJECT_ID,
            "tool": GRANT_TOOL,
            "status": "active",
        },
    )
    rows = result.get("grants") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if _grant_matches(row, server_url)]


async def ensure_server_grant(
    *,
    server_url: str,
    prefix: str,
    resource: str | None,
    method: str,
    scopes: list[str] | None = None,
) -> str | None:
    """授权成立的账本记录（幂等）：已有活跃 grant 复用其 id，否则 issue 新 grant。

    Returns:
        grant_id（复用或新建）；账本缺席/异常/被拒 → None（记录面降级，不抛）。
    """
    cap = get_ledger_cap()
    if cap is None:
        return None
    try:
        existing = await _list_active_server_grants(cap, server_url)
        if existing:
            grant_id = str(existing[0].get("grant_id") or "")
            if grant_id:
                return grant_id
        issued = await cap.call(
            "request",
            {
                "subject_kind": GRANT_SUBJECT_KIND,
                "subject_id": GRANT_SUBJECT_ID,
                "tool": GRANT_TOOL,
                "action": server_url,
                "granularity": GRANT_GRANULARITY,
                "source": GRANT_SOURCE,
                "scope": {
                    "domain": server_url,
                    "resource": resource or server_url,
                    "env_prefix": prefix,
                    "scopes": list(scopes or []),
                },
                "evidence": f"{GRANT_SOURCE}:{method}:{prefix}",
            },
        )
        if isinstance(issued, dict) and issued.get("ok"):
            grant_id = str(issued.get("grant_id") or "")
            logger.info(
                "[oauth_broker] OAuth 授权落账本 | server=%s | grant=%s | method=%s",
                server_url,
                grant_id,
                method,
            )
            return grant_id or None
        logger.warning("[oauth_broker] OAuth 授权账本写入被拒: %s", issued)
        return None
    except Exception as exc:  # noqa: BLE001 — 记录面降级：账本异常不阻断授权主流程
        logger.warning("[oauth_broker] OAuth 授权账本写入失败（授权不受影响）: %s", exc)
        return None


async def revoke_server_grants(*, server_url: str, context: dict[str, Any] | None = None) -> int:
    """撤销联动：revoke 该服务器全部活跃 oauth_broker grant；返回成功撤销数。

    账本缺席/异常 → 0（记录面降级，不抛）。
    """
    cap = get_ledger_cap()
    if cap is None:
        return 0
    revoked = 0
    try:
        for row in await _list_active_server_grants(cap, server_url):
            grant_id = str(row.get("grant_id") or "")
            if not grant_id:
                continue
            outcome = await cap.call(
                "revoke",
                {"grant_id": grant_id, "context": {"broker": GRANT_SOURCE, **(context or {})}},
            )
            if isinstance(outcome, dict) and outcome.get("ok") and outcome.get("revoked"):
                revoked += 1
        if revoked:
            logger.info(
                "[oauth_broker] 凭据失效联动撤销 grant | server=%s | revoked=%s", server_url, revoked
            )
        return revoked
    except Exception as exc:  # noqa: BLE001 — 记录面降级：撤销失败不阻断授权主流程
        logger.warning("[oauth_broker] 撤销联动失败（授权流程不受影响）: %s", exc)
        return revoked
