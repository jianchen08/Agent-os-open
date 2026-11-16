#!/usr/bin/env python3
"""E 事件汇聚接收插件——云端权威库的写入面（http_endpoints，零内核改动）。

端点面（声明见 plugin.json http_endpoints；鉴权分工按仓内既定口径）::

    POST /ext/trace_ingestion/events     auth:"none" —— webhook 验签自管：
                                         Bearer 对照 env 每宿主 token（信封
                                         origin_host 查表，token 决定权威来源，
                                         防顶名）；schema 宽容 + ULID 幂等落库。
    GET  /ext/trace_ingestion/events     auth:"admin" —— 内核 JWT 管理面；
                                         origin 过滤（后续检索/召回集成预留，
                                         v1 不接记忆召回）。
    GET  /ext/trace_ingestion/dead_letters  auth:"admin" —— 死信查看。

token 来源：env ``AGENTOS_TRACE_INGEST_TOKENS``（``host:token`` 逗号对）；
未配置 = 一律 403 fail-closed（装载不受阻，端点在但全拒）。
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（共享裸模块）入 sys.path

from _root_api import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    now_iso_utc,  # noqa: E402
    ok as _ok,
    protocol_error as _error,
    user_data_dir,  # noqa: E402
)
from trace_ingest_core import (  # noqa: E402
    UNKNOWN_FIELDS_DROP,
    UNKNOWN_FIELDS_KEEP,
    EnvelopeError,
    TokenAuth,
    parse_tokens_env,
    process_envelope,
)
from trace_ingest_store import TraceEventStore  # noqa: E402

plugin = AgentOSPlugin("trace_ingestion")
logger = logging.getLogger(__name__)

#: 每宿主 token env（``host:token`` 逗号对；不落配置明文）。
TOKENS_ENV = "AGENTOS_TRACE_INGEST_TOKENS"

_store: TraceEventStore | None = None
_auth = TokenAuth({})
_unknown_fields = UNKNOWN_FIELDS_KEEP


def _default_db_path() -> str:
    data_root = user_data_dir()
    if data_root is None:
        raise RuntimeError("trace_ingestion 无法定位用户数据目录（USER_ROOT 未配置）")
    return str(data_root / "trace_ingestion" / "trace_events.db")


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """装配存储与鉴权；token 未配置不拒装载——端点在、全拒（fail-closed + 告警）。"""
    global _store, _auth, _unknown_fields
    settings = plugin.get_config().get("settings", {})
    configured = str(settings.get("unknown_fields", UNKNOWN_FIELDS_KEEP)).strip().lower()
    valid = (UNKNOWN_FIELDS_KEEP, UNKNOWN_FIELDS_DROP)
    _unknown_fields = configured if configured in valid else UNKNOWN_FIELDS_KEEP
    tokens = parse_tokens_env(os.environ.get(TOKENS_ENV, ""))
    _auth = TokenAuth(tokens)
    _store = TraceEventStore(_default_db_path())
    if not tokens:
        logger.warning(
            "[trace_ingestion] %s 未配置，全部写入请求将 403（fail-closed）", TOKENS_ENV
        )
    logger.info(
        "trace_ingestion 已装载: hosts=%s unknown_fields=%s",
        _auth.configured_hosts, _unknown_fields,
    )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None


def _bearer(headers: dict[str, str] | None) -> str:
    """取 Bearer 凭据（内核 header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and isinstance(v, str):
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


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
    },
    description="HTTP endpoint handler for /ext/trace_ingestion/**",
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "integer"},
            "body": {"type": "object"},
        },
    },
    render={"card": "generic"},
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    query_multi: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """/ext/trace_ingestion/** 分发：写面 token 自管，读面内核 admin JWT。"""
    del plugin_id  # dispatcher 已路由到本插件
    if _store is None:
        return _error("trace_ingestion store not initialized (load pending)", 502)
    store = _store
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="trace_ingestion"
    rest = parts[2:]
    if rest == ["events"] and method == "POST":
        return _handle_ingest(store, raw_body, _bearer(headers))
    if rest == ["events"] and method == "GET":
        return _handle_query(store, query or {})
    if rest == ["dead_letters"] and method == "GET":
        return _ok(_json_response({"dead_letters": store.list_dead_letters()}))
    return _error(f"trace_ingestion: no route for {method} {path}", 404)


def _handle_ingest(
    store: TraceEventStore, raw_body: str, bearer: str
) -> dict[str, Any]:
    """写入面：信封解析 → 鉴权 → 宽容处理（信封级失败映射协议错误响应）。"""
    try:
        envelope = _decode_body(raw_body, strict_object=True)
    except ValueError as exc:
        return _error(str(exc), 400)
    outcome = process_envelope(
        store,
        envelope,
        auth=_auth,
        bearer=bearer,
        unknown_fields=_unknown_fields,
        received_at=now_iso_utc(),
    )
    if isinstance(outcome, EnvelopeError):
        return _error(outcome.message, outcome.status)
    logger.info(
        "[trace_ingestion] 批处理完成: %s", {k: outcome[k] for k in sorted(outcome)}
    )
    return _ok(_json_response(outcome, 200))


def _handle_query(store: TraceEventStore, query: dict[str, str]) -> dict[str, Any]:
    """查询面：origin 过滤 + ULID 游标 + limit（§3.3 来源隔离的读取侧）。"""
    origin_host = (query.get("origin_host") or "").strip() or None
    after_ulid = (query.get("after_ulid") or "").strip()
    try:
        limit = int(query.get("limit") or "100")
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 1000))
    events = store.query_events(origin_host=origin_host, limit=limit, after_ulid=after_ulid)
    return _ok(_json_response({"events": events, "count": len(events)}, 200))


if __name__ == "__main__":
    plugin.run()
