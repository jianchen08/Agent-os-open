#!/usr/bin/env python3
"""E 事件汇聚导出插件——本机域事件批量上云（outbox 模式，零内核改动）。

链路（ADR 详规 docs/working/分布式执行设计_事件汇聚E与插件宿主F_20261012.md
§3.1；订阅机制 = channel 族同款 domain_event hook）::

    内核域事件（run.* 等） → notifications/domain_event → 本插件
      → EventFilter 过滤（默认 run.*，配置化——privacy 过滤落点）
      → ULID 幂等键 → outbox.jsonl 追加（断网天然缓冲，§3.4）
      → 后台循环：大小/时间双阈值攒批 → POST {origin_host, origin_kind,
        protocol_version, events[]} → 失败指数退避，成功推进游标
      → 云端 trace_ingestion（ULID 幂等，重传零重复）

云不可达不阻塞本机任何执行：入队是同步快路径，推送失败只退避重试。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（共享裸模块）入 sys.path

from _root_api import (
    now_iso_utc,  # noqa: E402
    user_data_dir,  # noqa: E402
)
from trace_export_core import (  # noqa: E402
    EventFilter,
    OutboxStore,
    TraceExporter,
)

plugin = AgentOSPlugin("trace_exporter")
logger = logging.getLogger(__name__)

#: 出站凭据 env（不落配置明文；缺失降级不装载——导出面缺席，域事件不入 outbox）。
TOKEN_ENV = "AGENTOS_TRACE_EXPORT_TOKEN"

_exporter: TraceExporter | None = None


def _parse_float(settings: dict[str, Any], key: str, default: float) -> float:
    raw = settings.get(key)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _parse_int(settings: dict[str, Any], key: str, default: int) -> int:
    raw = settings.get(key)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _parse_event_types(settings: dict[str, Any]) -> list[str]:
    """事件类型过滤：配置为逗号分隔通配串（表单友好），空 = 全不导出（fail-closed）。"""
    raw = settings.get("event_types")
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return []


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """装配 outbox + 导出循环；未配置导出目标 → 降级不装载（同族 sync_relay /
    trace_ingestion 口径：缺 token fail-closed 只告警，不炸 on_load）。"""
    global _exporter
    settings = plugin.get_config().get("settings", {})
    ingestion_url = str(settings.get("ingestion_url", "") or "").strip()
    origin_host = str(settings.get("origin_host", "") or "").strip()
    origin_kind = str(settings.get("origin_kind", "") or "").strip() or "dev"
    token = os.environ.get(TOKEN_ENV, "").strip()
    missing = [
        name for name, value in (
            ("settings.ingestion_url", ingestion_url),
            ("settings.origin_host", origin_host),
            (TOKEN_ENV, token),
        ) if not value
    ]
    if missing:
        logger.warning(
            "[trace_exporter] %s 未配置；导出面不装载（域事件不入 outbox，fail-closed 不变）。"
            "配置后重启用插件即生效", ", ".join(missing),
        )
        return

    data_root = user_data_dir()
    if data_root is None:
        raise RuntimeError("trace_exporter 无法定位用户数据目录（USER_ROOT 未配置）")
    outbox_path = data_root / "trace_exporter" / "outbox.jsonl"
    outbox = OutboxStore(outbox_path)
    _exporter = TraceExporter(
        ingestion_url=ingestion_url.rstrip("/"),
        token=token,
        origin_host=origin_host,
        origin_kind=origin_kind,
        outbox=outbox,
        event_filter=EventFilter(_parse_event_types(settings)),
        batch_max_events=_parse_int(settings, "batch_max_events", 100),
        flush_interval_secs=_parse_float(settings, "flush_interval_secs", 30.0),
    )
    restored = _exporter.load_pending()
    _exporter.start()
    logger.info(
        "trace_exporter 已装载: url=%s origin=%s/%s 恢复待投递=%s",
        ingestion_url, origin_host, origin_kind, restored,
    )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """停导出循环；未投递事件留 outbox，下次装载续传。"""
    global _exporter
    if _exporter is not None:
        await _exporter.stop()
        _exporter = None


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """域事件 → 过滤 → outbox 追加（同步快路径；失败只告警不上抛）。"""
    if _exporter is None:
        return
    event = str(params.get("event", "") or "")
    if not event:
        return
    tags = {k: v for k, v in params.items() if k != "event"}
    try:
        accepted = _exporter.enqueue(event, tags, now_iso_utc())
    except Exception:  # noqa: BLE001 —— 域事件通知 fire-and-forget，失败不阻塞内核
        logger.exception("[trace_exporter] 事件入队失败（本条丢弃）: %s", event)
        return
    if not accepted:
        logger.debug("[trace_exporter] 事件被过滤未导出: %s", event)


@plugin.tool(
    name="trace_exporter.get_status",
    schema={"type": "object", "properties": {}},
    description="E 轨迹导出器状态（待投递数/计数面/末次错误）",
    output_schema={
        "type": "object",
        "properties": {
            "pending": {"type": "integer"},
            "enqueued_total": {"type": "integer"},
            "flushed_total": {"type": "integer"},
            "failed_flushes": {"type": "integer"},
            "dropped_total": {"type": "integer"},
            "last_error": {"type": "string"},
        },
    },
    render={"card": "generic"},
)
async def get_status() -> dict[str, Any]:
    """导出器运行状态（断网续传的可观测面）。"""
    if _exporter is None:
        return {"loaded": False}
    exp = _exporter
    return {
        "loaded": True,
        "pending": exp.pending_count,
        "enqueued_total": exp.enqueued_total,
        "flushed_total": exp.flushed_total,
        "failed_flushes": exp.failed_flushes,
        "dropped_total": exp.dropped_total,
        "last_error": exp.last_error,
    }


if __name__ == "__main__":
    plugin.run()
