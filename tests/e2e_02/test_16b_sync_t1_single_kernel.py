# @feature: FP-0.2.多端同步 T1.DB逻辑记录同步 | @ci: none-local
"""T1 轻量车道（2026-10-17 用户裁定：G-S1 双内核重车道暂停，本车道为 T1 验收面；
跨端完整 E2E 留 T3 三端验收统一做）。

单内核实例 + 进程内第二节点，真实依赖验证 capture→store→pull 链路关键断言：

- **真实内核**（单实例 desktop，stub LLM 脚本回复真管道 chat）→ **真实
  sync_agent 插件**（合宿常驻；管道步骤捕获消息/任务 + 域事件捕获会话 →
  outbox → aiohttp 上行）→ **进程内 relay 替身**（真实 sync_relay 核心逻辑：
  relay_core.process_events/select_baseline + RelayStore，仅 HTTP 面为标准
  库壳——零 mock 内部依赖）→ **模拟第二节点**（真实 SyncAgentEngine：
  自举/拉取/合并）→ 单一通用镜像表只读呈现断言。
- 断言面：会话骨架（属主=desktop-a）+ 消息全文镜像 + 属主追加镜像跟增 +
  镜像只读结构（镜像数据只落本节点自有库，不触碰内核写面）。

门控：``AGENTOS_T1=1`` 才实跑（真实内核进程，冷启 ~40s；G-M1.4 契约与
sync_agent/sync_relay 单测为进程内回归面）。

实跑：``AGENTOS_T1=1 python -m pytest tests/e2e_02/test_16b_sync_t1_single_kernel.py -v``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_REPO_ROOT / "plugins" / "shared" / "sync_agent"),
    str(_REPO_ROOT / "plugins" / "shared" / "sync_relay"),
    str(_REPO_ROOT / "plugins" / "shared"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core import AgentConfig, SyncAgentEngine  # noqa: E402
from agent_state import AgentStateStore  # noqa: E402
from outbox_store import OutboxStore  # noqa: E402
from relay_core import TokenAuth, process_events, select_baseline  # noqa: E402
from relay_store import RelayStore  # noqa: E402
from test_16_sync_gs1_dual_kernel import (  # noqa: E402  # 复用 fleet 句柄与装配兜底（复制必漂移纪律）
    KERNEL_BIN,
    TIER_OF,
    TOKENS,
    KernelInstance,
    _build_stub_config_root,
    _login_admin,
    _settle_sync_agent,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_T1") != "1",
        reason="T1 轻量车道：设 AGENTOS_T1=1 实跑（真实内核进程 ×1 + 进程内"
        "模拟第二节点；双内核车道已按用户 2026-10-17 裁定暂停）",
    ),
]

PORT_K, PORT_RELAY = 19390, 19391
RELAY_BASE = f"http://127.0.0.1:{PORT_RELAY}/ext/relay"
MARKER = "T1-LITE-MARKER-ONE"
STUB_REPLY = "T1 轻量车道脚本回复（断言不依赖本文本）"

_BOOT_TIMEOUT_SECS = 300.0
_CONVERGE_TIMEOUT_SECS = 180.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class RelayStandIn:
    """进程内 relay 替身：真实 sync_relay 核心逻辑 + 标准 HTTP 壳。

    装载 relay_core/relay_store 生产代码（鉴权/属主执法/ULID 幂等/基线选择
    全真实），仅端点壳为测试自有（sync_relay 插件进程面归 T3 三端验收）。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.store: RelayStore | None = None
        self.auth = TokenAuth(dict(TOKENS))
        self._server: Any = None
        self._ready = threading.Event()

    # 引擎注入式出站（模拟第二节点经真实 HTTP 打替身，与内核插件同路径；
    # 引擎以 await 消费，须为 async 合约——同步函数即 bootstrap 恒失败）。
    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        _relay_http("POST", url, token, payload)

    async def get(self, url: str, token: str) -> dict[str, Any]:
        return _relay_http("GET", url, token, None)

    def start(self) -> None:
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def _bearer(self) -> str:
                raw = self.headers.get("Authorization") or ""
                return raw[7:].strip() if raw.startswith("Bearer ") else ""

            def _json(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 —— http.server 约定
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                bearer = self._bearer()
                if self.path.endswith("/events"):
                    outcome = process_events(
                        stand_in.store, body.get("events"), auth=stand_in.auth,
                        bearer=bearer, received_at=_now_iso(),
                    )
                    if hasattr(outcome, "to_dict"):
                        self._json(200, outcome.to_dict())
                    else:
                        self._json(outcome.status, {"error": outcome.message})
                    return
                if self.path.endswith("/devices") or self.path.endswith("/heartbeat"):
                    device = stand_in.auth.device_for(bearer)
                    if device is None:
                        self._json(403, {"error": "unrecognized per-device token"})
                        return
                    stand_in.store.upsert_device(
                        device_id=device,
                        tier=str(body.get("tier") or TIER_OF.get(device, "")),
                        app_version=str(body.get("app_version") or ""),
                        capabilities=body.get("capabilities")
                        if isinstance(body.get("capabilities"), dict)
                        else {},
                        now_iso=_now_iso(),
                    )
                    self._json(200, {"registered": device})
                    return
                self._json(404, {"error": f"no route {self.path}"})

            def do_GET(self) -> None:  # noqa: N802
                device = stand_in.auth.device_for(self._bearer())
                if device is None:
                    self._json(403, {"error": "unrecognized per-device token"})
                    return
                if self.path.endswith("/bootstrap"):
                    tiers = {d["device_id"]: d["tier"] for d in stand_in.store.list_devices()}
                    all_events = stand_in.store.events.pull(
                        0, limit=stand_in.store.events.max_cursor() or 1
                    )
                    baseline = select_baseline(all_events, tiers)
                    self._json(
                        200,
                        {
                            "device": device,
                            "events": [_wire(e) for e in baseline],
                            "cursor": stand_in.store.events.max_cursor(),
                            "tiers": tiers,
                        },
                    )
                    return
                if self.path.startswith("/ext/relay/events"):
                    since = 0
                    for part in self.path.split("?", 1)[-1].split("&"):
                        if part.startswith("since_cursor="):
                            since = int(part.split("=", 1)[1] or 0)
                    events = stand_in.store.events.pull(since, limit=500)
                    self._json(200, {"events": [_wire(e) for e in events], "count": len(events)})
                    return
                self._json(404, {"error": f"no route {self.path}"})

            def log_message(self, *args: Any) -> None:
                del args  # 静默

        def _serve() -> None:
            stand_in.store = RelayStore(stand_in.db_path)  # 库与服务线程同生（sqlite 线程约束）
            stand_in._ready.set()
            stand_in._server = HTTPServer(("127.0.0.1", PORT_RELAY), Handler)
            stand_in._server.serve_forever()

        threading.Thread(target=_serve, daemon=True).start()
        assert self._ready.wait(10), "替身服务线程未就绪"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def all_events(self) -> list[dict[str, Any]]:
        """全量拉取（HTTP 真实路径——测试线程零直触库，替身线程序服）。"""
        events: list[dict[str, Any]] = []
        since = 0
        while True:
            payload = _relay_http(
                "GET", f"{RELAY_BASE}/events?since_cursor={since}&limit=500",
                TOKENS["phone-b"], None,
            )
            batch = payload.get("events", [])
            events.extend(batch)
            if not batch:
                return events
            since = max(int(e["cursor"]) for e in batch)

    def events_from(self, device_id: str) -> list[dict[str, Any]]:
        return [e for e in self.all_events() if e["device_id"] == device_id]


def _wire(event: Any) -> dict[str, Any]:
    wire = event.to_wire()
    wire["cursor"] = event.cursor
    return wire


def _relay_http(method: str, url: str, token: str, body: dict[str, Any] | None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _kernel_http(
    instance: KernelInstance, method: str, path: str, token: str, body: dict[str, Any],
    timeout: float = 150.0,
) -> tuple[int, Any]:
    req = urllib.request.Request(
        f"{instance.base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _kernel_log_tail(instance: KernelInstance, limit: int = 60) -> list[str]:
    """内核日志尾（sync_agent/invoker/step 相关行；日志缺失返回提示行）。"""
    import re

    logs_dir = instance.home / "logs"
    if not logs_dir.is_dir():
        return [f"<no logs dir at {logs_dir}>"]
    files = sorted(
        logs_dir.glob("kernel.log*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        return [f"<no kernel.log* under {logs_dir}>"]
    pattern = re.compile(r"sync_agent|invoker|step|pipeline|capture", re.IGNORECASE)
    hits: list[str] = []
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            if pattern.search(line):
                hits.append(line[-300:])
    return hits[-limit:] or [f"<no matching lines in {len(files)} log files>"]


def _poll_until(desc: str, probe: Any, timeout: float = _CONVERGE_TIMEOUT_SECS) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(1.0)
    raise AssertionError(f"超时（{timeout}s）: {desc}；末次观测={last!r}")


_STAND_IN_REF: list[RelayStandIn | None] = [None]


def _simulated_node(root: Path) -> SyncAgentEngine:
    """模拟第二节点（phone-b）：真实引擎 + 替身出站（与内核插件同 HTTP 路径）。"""
    stand_in = _STAND_IN_REF[0]
    assert stand_in is not None, "替身未装配"
    return SyncAgentEngine(
        AgentConfig(
            relay_url=RELAY_BASE,
            token=TOKENS["phone-b"],
            device_id="phone-b",
            device_tier="phone",
            app_version="test",
            watch_root=root / "b" / "config",
        ),
        state=AgentStateStore(root / "b" / "state.db"),
        outbox=OutboxStore(root / "b" / "outbox.jsonl"),
        post_json=stand_in.post,
        get_json=stand_in.get,
    )


def _b_tick(engine: SyncAgentEngine) -> None:
    asyncio.run(engine.tick())


def _mirror_converged(engine: SyncAgentEngine, thread_id: str) -> object:
    _b_tick(engine)
    rows = engine._state.list_mirror_records()
    sessions = [r for r in rows if r["table"] == "sessions" and r["pk"] == thread_id]
    messages = [r for r in rows if r["table"] == "messages"]
    has_user = any((r["row"] or {}).get("content") == MARKER for r in messages)
    has_assistant = any((r["row"] or {}).get("role") == "assistant" for r in messages)
    if sessions and sessions[0]["owner_device"] == "desktop-a" and has_user and has_assistant:
        return {"sessions": len(sessions), "messages": len(messages)}
    return None


@pytest.fixture(scope="module")
def lite_fleet(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """单内核实例 + 进程内 relay 替身（module 级一次起停）。

    根目录固定在 ``.zctmp/t1lite_run``（未跟踪运维区，跑前清空）：内核日志
    （home/logs/kernel.log*）进超时诊断，tmp 池被并行会话挤出也不丢证据。
    """
    if not KERNEL_BIN.is_file():
        pytest.fail(f"内核二进制缺失: {KERNEL_BIN}（先 cargo build --release）")
    import shutil

    root = _REPO_ROOT / ".zctmp" / "t1lite_run"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    del tmp_path_factory
    from stub_llm_server import ScriptedLLMUpstream, text_step

    stub = ScriptedLLMUpstream()
    stub.start()
    stub.register("t1-lite", MARKER, [text_step(STUB_REPLY)], default=text_step(STUB_REPLY))
    stand_in = RelayStandIn(root / "relay.db")
    stand_in.start()
    _STAND_IN_REF[0] = stand_in
    desktop = KernelInstance("desktop-a", PORT_K, root / "a")
    desktop.config_root = _build_stub_config_root(root / "a", stub.api_base)
    desktop.relay_url = RELAY_BASE
    desktop.start()
    try:
        desktop.wait_healthy(_BOOT_TIMEOUT_SECS)
        _settle_sync_agent(desktop)
        yield {"desktop": desktop, "stub": stub, "stand_in": stand_in, "root": root}
    finally:
        desktop.kill_tree()
        stand_in.stop()
        stub.stop()
        _STAND_IN_REF[0] = None


def _chat_with_retry(
    instance: KernelInstance, token: str, thread_id: str, message: str, attempts: int = 3
) -> tuple[int, Any]:
    """chat 有界重试：LLM 侧车（llm_core）冷启在系统高负载下有 MCP init
    竞态（kernel.log MCP_INIT_FAILED 实证）——被测面是同步链不是 LLM 侧车
    装载，故对"回复为空"的轮次重试（真实依赖零 mock；stub=测试自有基建）。"""
    body: Any = None
    status = 0
    for _ in range(attempts):
        status, body = _kernel_http(
            instance, "POST", "/api/v1/chat", token, {"message": message, "session_id": thread_id}
        )
        if status == 200 and isinstance(body, dict) and str(body.get("content") or "").strip():
            return status, body
        time.sleep(10)
    return status, body


def test_t1_capture_store_pull_full_chain(lite_fleet: dict[str, Any]) -> None:
    """T1 主链路：A 建会话+发消息 → relay 账本 → 模拟节点 B 只读镜像可见。"""
    desktop, stand_in = lite_fleet["desktop"], lite_fleet["stand_in"]
    token_a = _login_admin(desktop)

    status, sess = _kernel_http(
        desktop, "POST", "/api/v1/sessions", token_a, {"title": "t1-lite"}
    )
    assert status == 200, f"建会话失败: {sess}"
    thread_id = str(sess["thread_id"])
    status, body = _chat_with_retry(desktop, token_a, thread_id, MARKER)
    assert status == 200, f"发消息失败: {body}"
    assert str((body or {}).get("content") or "").strip(), (
        f"LLM 侧车持续未就绪（回复为空）: {body}"
    )

    # capture→store：真实内核管道捕获经 outbox 上行入替身账本（属主执法放行）。
    def captured() -> object:
        events = stand_in.events_from("desktop-a")
        msgs = [e for e in events if e["asset_type"] == "message"]
        has_user = any((e["payload"].get("row") or {}).get("content") == MARKER for e in msgs)
        has_session = any(e["asset_type"] == "session" for e in events)
        if has_user and has_session and len(msgs) >= 2:
            return {"messages": len(msgs)}
        return None

    try:
        _poll_until("desktop 捕获上行入账（消息≥2 条含用户原文 + 会话骨架）", captured)
    except AssertionError:
        agent_status = _kernel_http(
            desktop, "GET", "/ext/sync_agent/status", token_a, {}, timeout=15
        )
        ledger = stand_in.all_events()
        kernel_log_tail = _kernel_log_tail(desktop)
        raise AssertionError(
            "捕获入账超时；自诊断："
            f"sync_agent 状态={agent_status}；"
            f"替身账本事件数={len(ledger)}；"
            f"设备分布={sorted({e['device_id'] for e in ledger})}；"
            f"类型分布={sorted({e['asset_type'] for e in ledger})}；"
            f"内核日志尾(步调相关)={kernel_log_tail}"
        ) from None

    # store→pull：模拟节点自举（注册+层级权威基线）→ 单一镜像表可见。
    node_b = _simulated_node(lite_fleet["root"])
    _poll_until("模拟节点 B 镜像收敛（会话+用户原文+assistant 回复）",
                lambda: _mirror_converged(node_b, thread_id))

    # 属主追加：A 再发一条 → B 镜像跟增（只追加语义）。
    second = MARKER + "-SECOND"
    status, body = _chat_with_retry(desktop, token_a, thread_id, second)
    assert status == 200, f"追加消息失败: {body}"
    assert str((body or {}).get("content") or "").strip(), (
        f"LLM 侧车持续未就绪（回复为空）: {body}"
    )

    def appended() -> object:
        _b_tick(node_b)
        hit = any(
            (r["row"] or {}).get("content") == second
            for r in node_b._state.list_mirror_records("messages")
        )
        return True if hit else None

    _poll_until("B 镜像跟增第二条消息", appended)

    # 只读结构：镜像只落本节点自有库（B 引擎从未接触内核 DB），行恒标属主。
    mirror = node_b._state.list_mirror_records()
    assert mirror, "镜像非空"
    assert all(r["owner_device"] == "desktop-a" for r in mirror), "镜像行恒标属主"
    sessions = node_b._state.list_mirror_records("sessions")
    assert any(r["pk"] == thread_id and not r["deleted"] for r in sessions)


def test_t1_relay_owner_enforcement_in_chain(lite_fleet: dict[str, Any]) -> None:
    """链路执法零回归：替身（真实 relay_core）对属主违规写照拒（死信留痕）。

    先以 desktop-a 合法认领会话（首写定属主），再以 phone-b 伪造写 = 拒。
    """
    stand_in = lite_fleet["stand_in"]
    legit = {
        "ulid": "01ARZ3NDEKGS1E00000000000E",
        "device_id": "desktop-a",
        "asset_type": "session",
        "asset_id": "sess-owned-by-desktop",
        "version": 1,
        "updated_at": "2026-10-17T08:00:00+00:00",
        "payload": {
            "table": "sessions", "pk": "sess-owned-by-desktop",
            "row": {"thread_id": "sess-owned-by-desktop"}, "tombstone": False,
        },
        "hlc": {"physical_ms": int(time.time() * 1000) - 5_000, "logical": 0,
                "device_id": "desktop-a"},
    }
    claimed = _relay_http(
        "POST", f"{RELAY_BASE}/events", TOKENS["desktop-a"], {"events": [legit]}
    )
    assert claimed.get("accepted") == 1, f"属主首写应放行: {claimed}"
    forge = {
        "ulid": "01ARZ3NDEKGS1D00000000000D",
        "device_id": "phone-b",
        "asset_type": "session",
        "asset_id": "sess-owned-by-desktop",
        "version": 1,
        "updated_at": "2026-10-17T08:00:00+00:00",
        "payload": {
            "table": "sessions", "pk": "sess-owned-by-desktop",
            "row": {"thread_id": "sess-owned-by-desktop"}, "tombstone": False,
        },
        "hlc": {"physical_ms": int(time.time() * 1000), "logical": 0, "device_id": "phone-b"},
    }
    outcome = _relay_http(
        "POST", f"{RELAY_BASE}/events", TOKENS["phone-b"], {"events": [forge]}
    )
    assert outcome.get("accepted") == 0, f"伪造写应被拒: {outcome}"
    assert "owner_violation" in json.dumps(outcome, ensure_ascii=False)
    assert not any(
        e["ulid"] == forge["ulid"] for e in stand_in.events_from("phone-b")
    ), "违规写不入事件账本"
