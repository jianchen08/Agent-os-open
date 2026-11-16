"""E 事件汇聚接收核心——信封校验 / 版本宽容 / ULID 幂等 / 死信不阻塞。

设计依据：docs/working/分布式执行设计_事件汇聚E与插件宿主F_20261012.md §3.2–3.3。

- 信封级校验：``{origin_host, origin_kind, protocol_version, events[]}`` 必需
  字段缺失 → 整批协议 400（信封破损无法归属事件）。
- 事件级宽容：单事件必需字段（合法 ULID + 非空 event 名）缺失 → 死信并继续，
  **不阻塞同批后续事件**；未知顶层字段按配置保留（``extra``）/丢弃（版本协商：
  更高协议版本的新字段遇低版本 ingestion 按此消化）。
- 幂等：ULID 已存在即跳过计数（重传零重复，§3.6-2 的机制面）。

鉴权（token↔origin_host 绑定，防顶名）：token 按**信封里的 origin_host** 查表
比对——token 决定权威来源，信封声明与凭据不符即拒绝。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from _root_api import is_valid_ulid
from trace_ingest_store import TraceEventStore

logger = logging.getLogger(__name__)

#: 本插件支持的协议版本（当前唯一；更高版本宽容消化，见 :func:`process_envelope`）。
SUPPORTED_PROTOCOL_VERSION = 1

#: 协议 v1 事件条目的已知顶层字段（其余按 unknown_fields 配置保留/丢弃）。
_KNOWN_EVENT_KEYS = frozenset({"ulid", "event", "emitted_at", "tags"})

#: 未知字段处理策略。
UNKNOWN_FIELDS_KEEP = "keep"
UNKNOWN_FIELDS_DROP = "drop"


@dataclass
class IngestResult:
    """一批信封的处理结果（HTTP 200 响应体 + 测试断言面）。"""

    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    dead_letter_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dead_lettered": self.dead_lettered,
        }


@dataclass
class EnvelopeError(Exception):
    """信封级错误（HTTP 映射：not_json/missing_field/shape → 400）。"""

    code: str
    message: str
    status: int = 400

    def __str__(self) -> str:  # pragma: no cover —— 展示面
        return f"{self.code}: {self.message}"


class TokenAuth:
    """每宿主 token 鉴权（env 注入，fail-closed）。

    Args:
        tokens: origin_host → token 映射（空 = 未配置，拒绝一切请求）。
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    @property
    def configured_hosts(self) -> list[str]:
        return sorted(self._tokens)

    def check(self, origin_host: str, bearer: str) -> tuple[int, str]:
        """校验信封声明的 origin_host 与 Bearer 凭据。

        Returns:
            ``(0, "")`` 通过；否则 ``(http_status, 错误码)``。
        """
        if not self._tokens:
            return 403, "tokens_not_configured"
        expected = self._tokens.get(origin_host)
        if expected is None:
            return 403, "unknown_origin_host"
        if not bearer or bearer != expected:
            return 401, "invalid_token"
        return 0, ""


def parse_tokens_env(raw: str) -> dict[str, str]:
    """解析 ``AGENTOS_TRACE_INGEST_TOKENS``：``host:token`` 逗号对。

    畸形项（缺冒号/空段）跳过——解析面宽容；空串 = 未配置（鉴权 fail-closed）。
    token 内可含冒号（按首个冒号切分）。
    """
    tokens: dict[str, str] = {}
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        host, sep, token = entry.partition(":")
        host = host.strip()
        token = token.strip()
        if not sep or not host or not token:
            logger.warning("[trace_ingestion] TOKENS 畸形项跳过（应为 host:token）")
            continue
        tokens[host] = token
    return tokens


def process_envelope(
    store: TraceEventStore,
    envelope: Any,
    *,
    auth: TokenAuth,
    bearer: str,
    unknown_fields: str = UNKNOWN_FIELDS_KEEP,
    received_at: str,
) -> dict[str, Any] | EnvelopeError:
    """处理一个信封：形状校验 → 鉴权 → 信封校验 → 逐事件（宽容）落库。

    顺序契约：先验形状拿到 origin_host，鉴权才有查表键（破损信封 400，
    凭据问题 401/403，两类失败不混淆）。

    Returns:
        成功 = :class:`IngestResult.as_dict`；信封级失败 = :class:`EnvelopeError`
        （单事件失败不在此列——它们进死信，批处理继续）。
    """
    if not isinstance(envelope, dict):
        return EnvelopeError(code="not_json_object", message="envelope must be an object")
    origin_host = envelope.get("origin_host")
    if origin_host is None or origin_host == "":
        return EnvelopeError(
            code="missing_field", message="envelope missing required field: origin_host"
        )
    if not isinstance(origin_host, str):
        return EnvelopeError(code="bad_shape", message="origin_host must be a string")
    status, code = auth.check(origin_host, bearer)
    if status:
        return EnvelopeError(code=code, message="auth rejected", status=status)

    origin_kind = envelope.get("origin_kind")
    protocol_version = envelope.get("protocol_version")
    events = envelope.get("events")
    missing = [
        name for name, value in (
            ("origin_kind", origin_kind),
            ("protocol_version", protocol_version),
            ("events", events),
        ) if value is None or value == ""
    ]
    if missing:
        return EnvelopeError(
            code="missing_field", message=f"envelope missing required fields: {missing}"
        )
    if not isinstance(origin_kind, str):
        return EnvelopeError(code="bad_shape", message="origin_kind must be a string")
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int) or protocol_version < 1:
        return EnvelopeError(code="bad_shape", message="protocol_version must be a positive integer")
    if not isinstance(events, list):
        return EnvelopeError(code="bad_shape", message="events must be a list")

    result = IngestResult(received=len(events))
    for raw_event in events:
        outcome = _process_event(
            store,
            raw_event,
            origin_host=origin_host,
            origin_kind=str(origin_kind),
            protocol_version=protocol_version,
            unknown_fields=unknown_fields,
            received_at=received_at,
        )
        if isinstance(outcome, IngestResult):
            result.accepted += outcome.accepted
            result.duplicates += outcome.duplicates
        else:
            result.dead_lettered += 1
            result.dead_letter_ids.append(outcome)
    return result.as_dict()


def _process_event(
    store: TraceEventStore,
    raw_event: Any,
    *,
    origin_host: str,
    origin_kind: str,
    protocol_version: int,
    unknown_fields: str,
    received_at: str,
) -> IngestResult | int:
    """处理单事件：宽容校验 → 幂等落库 / 死信。

    Returns:
        计数增量（:class:`IngestResult`，仅用 accepted/duplicates）或死信 id。
    """
    if not isinstance(raw_event, dict):
        return _dead_letter(store, origin_host, "event_not_object", raw_event, received_at)
    ulid = raw_event.get("ulid")
    event = raw_event.get("event")
    if not isinstance(ulid, str) or not is_valid_ulid(ulid):
        return _dead_letter(store, origin_host, "missing_or_bad_ulid", raw_event, received_at)
    if not isinstance(event, str) or not event.strip():
        return _dead_letter(store, origin_host, "missing_event_name", raw_event, received_at)
    tags = raw_event.get("tags", {})
    if tags is None:
        tags = {}
    if not isinstance(tags, dict):
        return _dead_letter(store, origin_host, "tags_not_object", raw_event, received_at)
    if unknown_fields not in (UNKNOWN_FIELDS_KEEP, UNKNOWN_FIELDS_DROP):
        unknown_fields = UNKNOWN_FIELDS_KEEP
    extra: dict[str, Any] = {}
    if unknown_fields == UNKNOWN_FIELDS_KEEP:
        extra = {k: v for k, v in raw_event.items() if k not in _KNOWN_EVENT_KEYS}
    emitted_at = raw_event.get("emitted_at", "")
    stored = store.insert_event(
        origin_host=origin_host,
        origin_kind=origin_kind,
        ulid=ulid,
        event=event,
        tags=tags,
        extra=extra,
        protocol_version=protocol_version,
        emitted_at=emitted_at if isinstance(emitted_at, str) else "",
        received_at=received_at,
    )
    if protocol_version > SUPPORTED_PROTOCOL_VERSION:
        # 版本宽容提示（一次性语义：批内多条也只逐条 DEBUG，不打扰主日志）。
        logger.debug(
            "[trace_ingestion] 高协议版本事件按宽容规则消化: v%s origin=%s",
            protocol_version, origin_host,
        )
    result = IngestResult()
    if stored:
        result.accepted = 1
    else:
        result.duplicates = 1
    return result


def _dead_letter(
    store: TraceEventStore, origin_host: str, reason: str, payload: Any, received_at: str
) -> int:
    """坏事件入死信（不抛不阻塞——调用方继续处理同批后续事件）。"""
    if not isinstance(origin_host, str):
        origin_host = ""
    dead_id = store.insert_dead_letter(
        origin_host=origin_host,
        reason=reason,
        payload=payload if isinstance(payload, dict) else {"raw": payload},
        received_at=received_at,
    )
    logger.warning("[trace_ingestion] 事件入死信（reason=%s, id=%s）", reason, dead_id)
    return dead_id
