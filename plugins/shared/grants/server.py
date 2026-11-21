#!/usr/bin/env python3
"""grants 系统插件 MCP 服务端——接口适配层（账本/策略/投影 + 管理端点）。

本目录为实现模块（平铺），本文件只做接口适配：
- services（capabilities.services 声明，provides namespaces 路由）：
  grants.ledger.request / authorize / consume / revoke / list + grants.policy.classify；
- http.handle：管理端点 GET /ext/grants/list、DELETE /ext/grants/{grant_id}、
  GET /ext/grants/audit、GET /ext/grants/metrics（auth=user，沿 ADR 2026-11-12
  端点先例；metrics 为 P3 只读聚合面）。

无 LLM 工具面：manifest capabilities.tools 恒空（ADR D1，不进任何 tool_ids）；
服务实现经 provides namespaces（grants.ledger / grants.policy）供 P1
security_check 等执法方调用，不经 LLM 工具面暴露。

设计真值：ADR 2026-11-19-grants-ledger-and-tool-classification +
docs/working/grants账本执行方案_20261119.md §1/§7/§8。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

import ledger as grants_ledger  # noqa: E402 — 平铺插件模块
import metrics as grants_metrics  # noqa: E402 — 平铺插件模块
import policy as grants_policy  # noqa: E402
from ledger import GrantsLedger, GrantsLedgerError  # noqa: E402
from policy import PolicyEngine, TrustProfile  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("grants")

_LEDGER: GrantsLedger | None = None
_POLICY: PolicyEngine | None = None


def reset_singletons() -> None:
    """清空账本/策略单例（on_unload 与测试钩子）。"""
    global _LEDGER, _POLICY
    _LEDGER = None
    _POLICY = None


def get_ledger() -> GrantsLedger:
    """账本单例（懒构建：db 落点经 resolve_db_path 用户数据根链解析）。"""
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = GrantsLedger(db_path=grants_ledger.resolve_db_path())
    return _LEDGER


def get_policy() -> PolicyEngine:
    """策略引擎单例（懒构建：config 注入 dict 优先，缺省读策略表文件）。"""
    global _POLICY
    if _POLICY is None:
        _POLICY = _build_policy(plugin.get_config())
    return _POLICY


def _build_policy(config: dict[str, Any]) -> PolicyEngine:
    """策略引擎构建：config_files 注入的 tool_policy 命名空间（解析后 dict）优先，
    缺省回读文件（resolve_policy_path，dev/未注入兜底）。

    trust profile（P3）：trust_profile 命名空间（解析后 dict）优先，缺省回读
    trust_profile.yaml；注入 settings 的 trust_profile_file 亦可达（dev/测试
    覆盖口）。生效点唯一在本插件 classify 内部（执法面零感知，ADR D2）。"""
    injected = config.get("tool_policy")
    settings = config.get("settings")
    settings_dict = settings if isinstance(settings, dict) else None
    profile = _build_trust_profile(config, settings_dict)
    if isinstance(injected, dict) and isinstance(injected.get("tools"), dict):
        return PolicyEngine(injected, ledger=get_ledger(), profile=profile)
    return PolicyEngine.from_yaml_file(
        grants_policy.resolve_policy_path(settings_dict), ledger=get_ledger(), profile=profile
    )


def _build_trust_profile(config: dict[str, Any], settings: dict[str, Any] | None) -> TrustProfile:
    """trust profile 构建：注入 dict > 注入文件路径 > 仓库缺省文件；缺文件 =
    balanced（P1 等价出厂默认），坏档名 = strict（fail-closed 收紧方向）。"""
    injected = config.get("trust_profile")
    if isinstance(injected, dict):
        return TrustProfile.from_dict(injected)
    return TrustProfile.from_yaml_file(grants_policy.resolve_trust_profile_path(settings))


def apply_settings(config: dict[str, Any]) -> None:
    """on_load / 配置变更到站：重建单例（settings.grants_db_file 可覆盖账本落点）。

    假设（形态标注）：settings 命名空间键 ``grants_db_file`` 沿 security_check
    ``permission_modes_file`` 覆盖口径；本批 config_files 未声明 settings 文件，
    键位为 P1+ 注入面预留。"""
    global _LEDGER, _POLICY
    settings = config.get("settings")
    _LEDGER = GrantsLedger(db_path=grants_ledger.resolve_db_path(settings if isinstance(settings, dict) else None))
    _POLICY = _build_policy(config)


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    apply_settings(plugin.get_config())
    logger.info("[grants] ledger ready | db=%s", get_ledger().db_path)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    reset_singletons()


@plugin.on_config_changed
def _on_config_changed(config: dict[str, Any]) -> None:
    """注入配置到站即重建单例（策略表/落点变更即时生效）。"""
    apply_settings(config)


# ── services：grants.ledger（授出/命中/记账/撤销/列）────────────────────────


@plugin.tool(
    name="grants.ledger.request",
    schema={
        "type": "object",
        "properties": {
            "subject_kind": {"type": "string", "enum": list(grants_ledger.SUBJECT_KINDS)},
            "subject_id": {"type": "string"},
            "tool": {"type": "string", "description": "'*' 允许"},
            "action": {"type": "string", "description": "'*' 允许"},
            "granularity": {"type": "string", "enum": list(grants_ledger.GRANULARITIES)},
            "source": {"type": "string", "enum": list(grants_ledger.SOURCES)},
            "scope": {"type": "object"},
            "max_uses": {"type": "integer"},
            "expires_at": {"type": "string", "description": "ISO 8601"},
            "evidence": {"type": "string", "description": "审批卡 id / 审计事件指针"},
            "pipeline_id": {"type": "string"},
            "parent_task_id": {"type": "string"},
        },
        "required": ["subject_kind", "subject_id", "tool", "action", "granularity", "source"],
    },
    description="授出 grant（授权决定面入口：P1 审批卡授予 / 用户规则 / oauth_broker）",
)
async def svc_ledger_request(
    subject_kind: str,
    subject_id: str,
    tool: str,
    action: str,
    granularity: str,
    source: str,
    scope: dict[str, Any] | None = None,
    max_uses: int | None = None,
    expires_at: str | None = None,
    evidence: str | None = None,
    pipeline_id: str | None = None,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    """授出 grant（显式签名，human 交互服务工具同款形态）。

    反向 capability 调用请求携带协议注入键（``_meta``/``config``，MCP request
    meta 与每调用配置注入），SDK 按显式签名过滤；**kwargs 全量透传会把它们
    漏进账本 API（scratch 实测 2026-11-19：TypeError: unexpected keyword
    argument '_meta'）。其余五个服务工具本就显式签名，本刀对齐。"""
    try:
        grant_id = get_ledger().request(
            subject_kind=subject_kind,
            subject_id=subject_id,
            tool=tool,
            action=action,
            granularity=granularity,
            source=source,
            scope=scope,
            max_uses=max_uses,
            expires_at=expires_at,
            evidence=evidence,
            pipeline_id=pipeline_id,
            parent_task_id=parent_task_id,
        )
    except GrantsLedgerError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "grant_id": grant_id}


@plugin.tool(
    name="grants.ledger.authorize",
    schema={
        "type": "object",
        "properties": {
            "subject_kind": {"type": "string", "enum": list(grants_ledger.SUBJECT_KINDS)},
            "subject_id": {"type": "string"},
            "tool": {"type": "string"},
            "action": {"type": "string"},
            "mandate": {
                "type": "object",
                "description": "跨端委派四要素（scope/expires_at/task_id/revocable）；携带即强制校验（P2 契约位）",
            },
            "require_mandate": {
                "type": "boolean",
                "description": "声明 mandate 强制：未携带 mandate → mandate_absent 拒（无 mandate 不放行）",
            },
            "task_id": {
                "type": "string",
                "description": "调用方声明的执行任务 id（mandate.task_id 绑定校验基准）",
            },
        },
        "required": ["subject_kind", "subject_id", "tool", "action"],
    },
    description=(
        "查活跃 grant 是否命中（P1 执法入口：特权类调用先经此面）。P2 mandate 契约位："
        "携带 mandate 或 require_mandate=true 时返回值增 mandate 校验报告——无 grant/"
        "无 mandate/四要素（scope/时限/task_id/可撤销）任一不符 → authorized=false；"
        "验签留给跨端执行面。缺省路径返回形态零变化。"
    ),
)
async def svc_ledger_authorize(
    subject_kind: str,
    subject_id: str,
    tool: str,
    action: str,
    mandate: dict[str, Any] | None = None,
    require_mandate: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """mandate 契约位（P2）：raw 账本命中 → 四要素校验 → 放行/拒 + 校验报告。

    缺省路径（无 mandate 且未声明强制）= 既有形态 ``{"authorized", "grant"}`` 零变化；
    mandate 模式下无 grant 不放行（``no_grant``）、无 mandate 不放行
    （``mandate_absent``）。验签（mandate 出处可信性）留给跨端执行面
    （delegation 未立项，本批契约 + 测试桩，不实现远端）。
    """
    grant = get_ledger().authorize(subject_kind=subject_kind, subject_id=subject_id, tool=tool, action=action)
    if mandate is None and not require_mandate:
        return {"authorized": grant is not None, "grant": grant}
    if grant is None:
        return {"authorized": False, "grant": None, "mandate": {"valid": False, "reason": "no_grant"}}
    if mandate is None:
        return {"authorized": False, "grant": None, "mandate": {"valid": False, "reason": "mandate_absent"}}
    check = grants_ledger.validate_mandate(mandate, grant=grant, task_id=task_id)
    if not check["valid"]:
        return {"authorized": False, "grant": None, "mandate": check}
    return {"authorized": True, "grant": grant, "mandate": check}


@plugin.tool(
    name="grants.ledger.consume",
    schema={
        "type": "object",
        "properties": {
            "grant_id": {"type": "string"},
            "context": {"type": "object"},
        },
        "required": ["grant_id"],
    },
    description="记账一次 grant 使用（uses+1；达 max_uses 置 consumed）",
)
async def svc_ledger_consume(grant_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        grant = get_ledger().consume(grant_id=grant_id, context=context)
    except GrantsLedgerError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "grant": grant}


@plugin.tool(
    name="grants.ledger.revoke",
    schema={
        "type": "object",
        "properties": {
            "grant_id": {"type": "string"},
            "context": {"type": "object"},
        },
        "required": ["grant_id"],
    },
    description="撤销活跃 grant（幂等；即时生效）",
)
async def svc_ledger_revoke(grant_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    revoked = get_ledger().revoke(grant_id=grant_id, context=context)
    return {"ok": True, "revoked": revoked}


@plugin.tool(
    name="grants.ledger.list",
    schema={
        "type": "object",
        "properties": {
            "subject_kind": {"type": "string"},
            "subject_id": {"type": "string"},
            "tool": {"type": "string"},
            "status": {"type": "string", "enum": list(grants_ledger.STATUSES)},
        },
    },
    description="按过滤器列 grant",
)
async def svc_ledger_list(
    subject_kind: str | None = None,
    subject_id: str | None = None,
    tool: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    grants = get_ledger().list(subject_kind=subject_kind, subject_id=subject_id, tool=tool, status=status)
    return {"ok": True, "grants": grants}


# ── services：grants.policy.classify ────────────────────────────────────────


@plugin.tool(
    name="grants.policy.classify",
    schema={
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "action": {"type": "string"},
            "args": {"type": "object", "description": "v1 不参与查表（上下文修正因子 P1+ 预留）"},
        },
        "required": ["tool", "action"],
    },
    description="动作级分级裁决（未分类 fail-closed deny + 审计，ADR D2）",
)
async def svc_policy_classify(tool: str, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_policy().classify(tool, action, args)


# ── 管理端点（auth=user；/ext/grants 命名空间 = manifest id）────────────────


def _http_response(status: int, data: dict[str, Any]) -> dict[str, Any]:
    """包装为内核 dispatcher 期望的响应结构（security_check / approval 同范式）。"""
    return {
        "success": True,
        "data": {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii"),
            "body_encoding": "base64",
        },
    }


def _query_params(kwargs: dict[str, Any]) -> dict[str, str]:
    """单值 query 解析（dict 或 [key, value] 元组列表两形态，security_check 同口径）。"""
    query = kwargs.get("query") or {}
    if isinstance(query, dict):
        return {str(k): str(v) for k, v in query.items()}
    try:
        return {str(k): str(v) for k, v in dict(query).items()}
    except Exception:  # noqa: BLE001 — query 异形按缺参走全量查询
        return {}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "query": {"type": "object", "additionalProperties": {"type": "string"}},
            "query_multi": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": ["path", "method"],
    },
    description="HTTP endpoint handler for /ext/grants/**（账本管理面：list/revoke/audit/metrics）",
)
async def http_handle(path: str, method: str, plugin_id: str = "", raw_body: str = "", **kwargs: Any) -> dict[str, Any]:
    """处理 /ext/grants/** 管理端点（GET list / DELETE revoke / GET audit）。"""
    del plugin_id, raw_body  # 管理面只读 + DELETE 无 body
    path_norm = path.rstrip("/")

    if method.upper() == "GET" and path_norm == "/ext/grants/list":
        params = _query_params(kwargs)
        grants = get_ledger().list(
            subject_kind=params.get("subject_kind"),
            subject_id=params.get("subject_id"),
            tool=params.get("tool"),
            status=params.get("status"),
        )
        return _http_response(200, {"grants": grants})

    if method.upper() == "GET" and path_norm == "/ext/grants/audit":
        params = _query_params(kwargs)
        events = get_ledger().events(
            grant_id=params.get("grant_id"),
            event_type=params.get("event_type"),
        )
        return _http_response(200, {"events": events})

    if method.upper() == "GET" and path_norm == "/ext/grants/metrics":
        # 只读聚合（P3）：两表事件 + security_audit.jsonl 若可达；取不到的数据源
        # 如实降级 null + 说明（compute_metrics 契约，禁造数）。
        events = get_ledger().events()
        settings = plugin.get_config().get("settings")
        audit_path = grants_metrics.resolve_audit_path(settings if isinstance(settings, dict) else None)
        audit = grants_metrics.read_security_audit(audit_path)
        return _http_response(200, grants_metrics.compute_metrics(events, audit))

    prefix = "/ext/grants/"
    if method.upper() == "DELETE" and path_norm.startswith(prefix):
        grant_id = path_norm[len(prefix):].strip("/")
        if grant_id and "/" not in grant_id:
            revoked = get_ledger().revoke(grant_id=grant_id)
            return _http_response(200, {"revoked": revoked, "grant_id": grant_id})
        return _http_response(404, {"error": "not found"})

    return _http_response(404, {"error": "not found"})
