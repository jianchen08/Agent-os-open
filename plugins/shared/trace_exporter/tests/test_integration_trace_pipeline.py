# @feature: FP-0.2.可观测性 E-3 集成闭环 | @ci: python-plugins-test
"""E 轨迹汇聚集成闭环（真实 HTTP + 真实 SQLite，禁全 mock）——设计 §3.6 验收 1/2。

链路：真实 TraceExporter（默认 aiohttp 出站）→ 真实 TCP（aiohttp TCPSite，
127.0.0.1 临时端口）→ trace_ingestion 的真实 http.handle（server.py 原模块，
importlib 加载，经真实 on_load 钩子装配——env 注入 token 与数据目录）→ 真实
SQLite（tmp_path）。内核 /ext dispatcher 之上的一层不属插件侧（零内核改动是
交付约束本身），信封契约按内核 HttpHandleRequest/HttpHandleResponse 同构透传。

场景对应：
- 验收 1（本机部分）：正常闭环——事件逐条一致、origin 标记落库、查询过滤命中；
- 验收 2（等效实证）：断连（拒绝连接）退避缓冲 → 恢复（换活端口重启导出器）
  续传零丢失零重复 + 同信封重放零新增（ULID 幂等）；
- token 鉴权拒绝：错 token 401 缓冲不丢，修凭据后补达；死信查看端点。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from trace_export_core import EventFilter, OutboxStore, TraceExporter

_HERE = Path(__file__).resolve().parent
_INGESTION_DIR = _HERE.parent.parent / "trace_ingestion"

_spec = importlib.util.spec_from_file_location(
    "trace_ingestion_server", _INGESTION_DIR / "server.py"
)
assert _spec is not None
assert _spec.loader is not None
ingestion_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingestion_server)

from trace_ingest_store import TraceEventStore  # noqa: E402 —— 断言面用独立连接

HOST = "dev-box"
TOKEN = "it-token-secret"


class IngestionHandle:
    """运行中的真实 ingestion 端：url + 断言面（独立 SQLite 连接）。"""

    def __init__(self, url: str, assert_store: TraceEventStore) -> None:
        self.url = url
        self.store = assert_store
        self.received_envelopes: list[dict[str, Any]] = []


async def _dispatch(request: web.Request, handle: IngestionHandle) -> web.Response:
    """内核 dispatcher 同构透传：组 HttpHandleRequest → 插件 → HttpHandleResponse。"""
    raw_body = await request.read()
    if request.method == "POST":
        try:
            handle.received_envelopes.append(json.loads(raw_body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    result = await ingestion_server.http_handle(
        path=request.path,
        method=request.method,
        raw_body=base64.b64encode(raw_body).decode("ascii"),
        headers={"authorization": request.headers.get("Authorization", "")},
        query=dict(request.query.items()),
    )
    if not result.get("success"):
        return web.Response(status=502, text="handler error")
    data = result["data"]
    body = base64.b64decode(data["body"]).decode("utf-8")
    return web.Response(
        status=int(data["status"]), text=body, content_type="application/json"
    )


@pytest.fixture
async def ingestion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 on_load 装配（env 注入 token/数据目录）+ 真实 TCP 端点。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(ingestion_server.TOKENS_ENV, f"{HOST}:{TOKEN}")
    await ingestion_server._on_load({})  # SDK 生命周期入口（内核握手缺位的最近真路径）

    handle = IngestionHandle(
        url="", assert_store=TraceEventStore(tmp_path / "data" / "trace_ingestion" / "trace_events.db")
    )
    runner = web.AppRunner(web.Application())

    async def dispatch(request: web.Request) -> web.Response:
        return await _dispatch(request, handle)

    runner.app.router.add_post("/ext/trace_ingestion/events", dispatch)
    runner.app.router.add_get("/ext/trace_ingestion/events", dispatch)
    runner.app.router.add_get("/ext/trace_ingestion/dead_letters", dispatch)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    handle.url = f"http://127.0.0.1:{runner.addresses[0][1]}/ext/trace_ingestion/events"
    yield handle
    await runner.cleanup()
    await ingestion_server._on_unload({})
    handle.store.close()


def make_exporter(
    url: str, outbox: OutboxStore, *, token: str = TOKEN, batch_max: int = 5
) -> TraceExporter:
    return TraceExporter(
        ingestion_url=url,
        token=token,
        origin_host=HOST,
        origin_kind="dev",
        outbox=outbox,
        event_filter=EventFilter(["run.*"]),
        batch_max_events=batch_max,
        flush_interval_secs=30.0,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def flush_bounded(exporter: TraceExporter, timeout: float) -> bool:
    """限时冲刷：持续失败（端点不可达/401 的重试语义）按超时打断。"""
    task = asyncio.create_task(exporter.flush_when_ready())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return False


def post_envelope(envelope: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")


class TestClosedLoop:
    async def test_events_arrive_intact_with_origin_markers(
        self, tmp_path: Path, ingestion: IngestionHandle
    ) -> None:
        # 验收 1（本机部分）：入队 → 攒批 POST → 落库逐条一致。
        exporter = make_exporter(ingestion.url, OutboxStore(tmp_path / "outbox.jsonl"))
        spec = [
            ("run.started" if i % 2 == 0 else "run.completed", {"pipeline_id": f"p-{i}"})
            for i in range(5)
        ]
        for event, tags in spec:
            assert exporter.enqueue(event, tags, "2026-10-12T08:00:00+00:00") is True
        assert await flush_bounded(exporter, 10.0) is True
        assert exporter.pending_count == 0

        rows = ingestion.store.query_events(origin_host=HOST)
        assert len(rows) == 5
        assert [r["event"] for r in rows] == [s[0] for s in spec]
        for row, (_, tags) in zip(rows, spec, strict=False):
            assert row["tags"] == tags
            assert row["origin_host"] == HOST
            assert row["origin_kind"] == "dev"
        # (origin_host, ulid) 排序存储不变量。
        keys = [(r["origin_host"], r["ulid"]) for r in rows]
        assert keys == sorted(keys)

    async def test_query_endpoint_filters_by_origin(
        self, tmp_path: Path, ingestion: IngestionHandle
    ) -> None:
        exporter = make_exporter(
            ingestion.url, OutboxStore(tmp_path / "outbox.jsonl"), batch_max=1
        )
        assert exporter.enqueue("run.completed", {}, "t") is True
        assert await flush_bounded(exporter, 10.0) is True

        result = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/events",
            method="GET",
            query={"origin_host": HOST, "limit": "10"},
        )
        assert result["success"] is True
        payload = json.loads(base64.b64decode(result["data"]["body"]))
        assert payload["count"] == 1
        assert payload["events"][0]["origin_host"] == HOST
        # 过滤面：陌生 origin 查不到本机轨迹（来源隔离读取侧）。
        result_other = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/events",
            method="GET",
            query={"origin_host": "stranger-box"},
        )
        payload_other = json.loads(base64.b64decode(result_other["data"]["body"]))
        assert payload_other["count"] == 0


class TestDisconnectReplay:
    async def test_outage_buffer_then_resume_zero_loss_zero_duplicate(
        self, tmp_path: Path, ingestion: IngestionHandle
    ) -> None:
        # 断连：指向必然拒绝连接的端口（真实 aiohttp 出站、真实连接失败）。
        dead_port = free_port()
        outbox = OutboxStore(tmp_path / "outbox.jsonl")
        exporter = make_exporter(
            f"http://127.0.0.1:{dead_port}/ext/trace_ingestion/events", outbox
        )
        for i in range(5):
            exporter.enqueue(
                "run.started" if i % 2 == 0 else "run.completed",
                {"pipeline_id": f"p-{i}"},
                "2026-10-12T08:00:00+00:00",
            )
        assert await flush_bounded(exporter, 2.5) is False  # 持续失败被限时打断
        assert exporter.pending_count == 5  # 断网缓冲：事件全数留存
        assert exporter.last_error != ""
        assert ingestion.store.count_events() == 0  # 云端零落库

        # 恢复：活端口 + 全新导出器实例共享同一 outbox（模拟进程重启续传）。
        resumed = make_exporter(ingestion.url, outbox)
        assert resumed.load_pending() == 5
        assert await flush_bounded(resumed, 10.0) is True
        assert resumed.pending_count == 0
        assert ingestion.store.count_events() == 5  # 零丢失
        rows = ingestion.store.query_events(origin_host=HOST)
        assert len({r["ulid"] for r in rows}) == 5  # 零重复

        # 重放：云端已收到的同一信封原样再发 → 全部判重（ULID 幂等实证）。
        replay = ingestion.received_envelopes[-1]
        assert replay["origin_host"] == HOST
        assert len(replay["events"]) == 5
        result = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/events",
            method="POST",
            raw_body=post_envelope(replay),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        payload = json.loads(base64.b64decode(result["data"]["body"]))
        assert payload == {"received": 5, "accepted": 0, "duplicates": 5, "dead_lettered": 0}
        assert ingestion.store.count_events() == 5  # 落库仍零新增


class TestTokenRejection:
    async def test_wrong_token_buffers_then_fixed_credentials_deliver(
        self, tmp_path: Path, ingestion: IngestionHandle
    ) -> None:
        outbox = OutboxStore(tmp_path / "outbox.jsonl")
        bad = make_exporter(ingestion.url, outbox, token="wrong-token")
        assert bad.enqueue("run.completed", {"k": 1}, "t") is True
        assert await flush_bounded(bad, 5.0) is False  # 401 → 持续失败
        assert bad.pending_count == 1
        assert ingestion.store.count_events() == 0

        fixed = make_exporter(ingestion.url, outbox)  # 正确凭据重启
        assert fixed.load_pending() == 1
        assert await flush_bounded(fixed, 10.0) is True
        assert ingestion.store.count_events(origin_host=HOST) == 1

    async def test_unconfigured_host_rejected_with_403(
        self, ingestion: IngestionHandle
    ) -> None:
        envelope = {"origin_host": "stranger", "origin_kind": "dev",
                    "protocol_version": 1, "events": []}
        result = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/events",
            method="POST",
            raw_body=post_envelope(envelope),
            headers={"authorization": "Bearer whatever"},
        )
        assert result["data"]["status"] == 403

    async def test_dead_letter_visible_via_view_endpoint(
        self, ingestion: IngestionHandle
    ) -> None:
        envelope = {
            "origin_host": HOST,
            "origin_kind": "dev",
            "protocol_version": 1,
            "events": [{"event": "run.completed"}],  # 缺 ULID → 死信，不阻塞批次
        }
        result = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/events",
            method="POST",
            raw_body=post_envelope(envelope),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        payload = json.loads(base64.b64decode(result["data"]["body"]))
        assert payload["dead_lettered"] == 1
        view = await ingestion_server.http_handle(
            path="/ext/trace_ingestion/dead_letters", method="GET"
        )
        dead = json.loads(base64.b64decode(view["data"]["body"]))
        assert len(dead["dead_letters"]) == 1
        assert dead["dead_letters"][0]["reason"] == "missing_or_bad_ulid"
