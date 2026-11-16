# @feature: FP-0.2.多端同步 2c 执行通道验收 | @ci: python-coverage
"""exec 通道轻量验收车道（单实例 + 模拟第二节点；台账 T4 续刀 R18，R19 扩）。

场景闭环（ADR 2026-10-17-unified-path-tree-and-capability-plane）：
- L1 能力上报面：属主端 exec_channel 子键装配（D8 端侧采集契约）→ 注册表 →
  routing_view 透传 → resolve_path 命中领地（前缀路由数据源闭环）；
- L2 双钥全链：发起端（device-a）经 relay 签发属主端（device-b）执行凭据 →
  属主端拉取验证集 → ExecTokenChannel 验证发起端 Bearer 正面命中（授权回执）；
- L3 诚实拒绝：错 token / 过期凭据（注入时钟）/ 协议版本不兼容 → 全部明确拒；
- L4 凭据续效：ExecTokenChannel 重建（模拟属主重启）后验证仍过（持久化面）；
- L5 掉盘诚实：属主心跳出窗 → 解析层如实呈现不可用；
- L6 引擎钩子（R19）：真 SyncAgentEngine 注册/心跳携带 exec_channel 子键入
  注册表（D8 钩子接线）+ 验证集周期拉取使引擎验证面可验发起端 Bearer（D13'）；
- L7 工作空间锚定（R19）：真 tree_anchor 锚定会话工作空间 → 设备前缀统一路径
  经路由表解析回同一真实路径（D9' 锚定↔解析往返，T5 跨端寻址铺路）。

真实依赖：真 RelayStore + 真 ExecTokenStore + 真 SyncAgentEngine +
routing_view/tree_core/exec_channel/tree_anchor 真模块；relay HTTP 壳与
handler 分发省略（语义由真实 store 承载，handler 契约由 test_relay_server
单测钉）——G-S1 双内核重车道按用户裁定暂停（R3），本车道单实例。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from exec_channel import (
    EXEC_CHANNEL_PROTOCOL_VERSION,
    ExecChannelConfig,
    ExecTokenChannel,
    build_capability,
    check_protocol_version,
    exec_channel_from_settings,
)
from exec_token_store import ExecTokenStore
from exec_tokens import issue_exec_token, verification_set
from relay_store import RelayStore
from routing_view import build_routing_view
from tree_anchor import workspace_tree_path
from tree_core import resolve_path

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()
WINDOW_SECS = 90.0
TOKEN_AB = "exec-token-a-to-b"
TERRITORY_B = "C:/workspaces-node-b"


def _heartbeat_age(store: RelayStore, device_id: str, secs: float) -> None:
    device = store.get_device(device_id)
    assert device is not None
    store.upsert_device(
        device_id=device_id,
        tier=device["tier"],
        app_version=device["app_version"],
        capabilities=device["capabilities"],
        now_iso=(NOW - timedelta(seconds=secs)).isoformat(),
    )


def _register(store: RelayStore, device_id: str, *, capabilities: dict[str, Any] | None = None) -> None:
    store.upsert_device(
        device_id=device_id,
        tier="desktop",
        app_version="1.0.0",
        capabilities=capabilities or {},
        now_iso=NOW.isoformat(),
    )


def _owner_capabilities(tmp_path: Path) -> dict[str, Any]:
    """属主端 exec_channel 能力子键 = exec_channel_from_settings 真装配产物（L1 数据源）。"""
    config = exec_channel_from_settings(
        {"exec_channel": {"mcp_url": "http://127.0.0.1:9103/mcp", "allowed_roots": [TERRITORY_B]}},
        env={},
    )
    return {"asset_types": ["config"], "exec_channel": build_capability(config)}


def test_exec_channel_lane(tmp_path: Path) -> None:
    registry = RelayStore(tmp_path / "relay.db")
    exec_store = ExecTokenStore(tmp_path / "exec_tokens.db")
    _register(registry, "device-a")
    _register(registry, "device-b", capabilities=_owner_capabilities(tmp_path))
    _heartbeat_age(registry, "device-b", secs=10.0)  # 在线窗口内

    # L1 能力上报面：装配子键 → 注册表 → routing_view 透传 → 前缀路由命中领地。
    view = build_routing_view(registry, now=NOW, window_secs=WINDOW_SECS, requested_by="device-a")
    device_b = next(d for d in view["devices"] if d["device_id"] == "device-b")
    assert device_b["online"] is True
    assert device_b["capabilities"]["exec_channel"]["allowed_roots"] == [TERRITORY_B]
    resolution = resolve_path("/device-b/tasks/demo.txt", view, self_device_id="device-a")
    assert resolution.owner_device == "device-b"
    assert resolution.reachable is True
    # 翻译一次到位：real_path 与属主端真实路径同一（分隔符差异属平台常态，按 Path 规范形比较）。
    assert Path(resolution.real_path) == Path(TERRITORY_B) / "tasks" / "demo.txt"

    # L2 双钥全链：发起端签发 → 属主端拉取验证集 → Bearer 验证正面命中。
    outcome = issue_exec_token(
        exec_store,
        issued_by="device-a",
        target_device="device-b",
        token=TOKEN_AB,
        ttl_secs=3600.0,
        now=NOW,
        device_exists=lambda d: registry.get_device(d) is not None,
    )
    assert outcome.target_device == "device-b"
    fetched = verification_set(exec_store, device_id="device-b", now=NOW)
    channel = ExecTokenChannel(tmp_path / "sync_agent" / "exec_verification.json")
    assert channel.refresh(lambda: fetched, now_iso=NOW.isoformat()) == 1
    receipt = channel.verify(TOKEN_AB, now=NOW)
    assert receipt is not None
    assert receipt["issued_by"] == "device-a"  # 授权回执面（谁签发、何时到期）
    assert datetime.fromisoformat(receipt["expires_at"]) == NOW + timedelta(hours=1)

    # L3 诚实拒绝：错 token / 过期凭据 / 协议版本不兼容，全部明确拒。
    assert channel.verify("exec-token-imposter", now=NOW) is None
    expired_at = NOW + timedelta(hours=1)
    assert channel.verify(TOKEN_AB, now=expired_at) is not None  # 压线有效
    assert channel.verify(TOKEN_AB, now=expired_at + timedelta(seconds=1)) is None
    incompatible = check_protocol_version("0.9", minimum=EXEC_CHANNEL_PROTOCOL_VERSION)
    assert incompatible is not None
    assert "0.9" in incompatible
    assert check_protocol_version(EXEC_CHANNEL_PROTOCOL_VERSION, minimum=EXEC_CHANNEL_PROTOCOL_VERSION) is None

    # L4 凭据续效：属主端重启（重建验证集对象）后验证仍过。
    reopened = ExecTokenChannel(tmp_path / "sync_agent" / "exec_verification.json")
    assert reopened.verify(TOKEN_AB, now=NOW) is not None

    # L5 掉盘诚实：属主心跳出窗 → 解析层如实呈现不可用（无本地副本）。
    _heartbeat_age(registry, "device-b", secs=WINDOW_SECS + 5.0)
    stale_view = build_routing_view(registry, now=NOW, window_secs=WINDOW_SECS, requested_by="device-a")
    stale = resolve_path("/device-b/tasks/demo.txt", stale_view, self_device_id="device-a")
    assert stale.online is False
    assert stale.reachable is False
    assert "不可用" in stale.note
    registry.close()
    exec_store.close()


# ---------------------------------------------------------------------------
# L6/L7（R19）：引擎钩子 + 工作空间统一树锚定
# ---------------------------------------------------------------------------


class _LaneRelay:
    """进程内 relay 替身：真 RelayStore/ExecTokenStore 承载语义，HTTP 壳省略。

    注册/心跳按 relay handler 契约落注册表（带 capabilities 即刷新能力面）；
    exec_tokens GET 走真 verification_set（时效闭区间，属主只回落自己）。
    """

    def __init__(self, registry: RelayStore, exec_store: ExecTokenStore, device_id: str) -> None:
        self.registry = registry
        self.exec_store = exec_store
        self.device_id = device_id
        self.heartbeats = 0

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        if url.endswith("/devices"):
            self.registry.upsert_device(
                device_id=payload["device_id"],
                tier=payload["tier"],
                app_version=payload["app_version"],
                capabilities=payload["capabilities"],
                now_iso=NOW_ISO,
            )
            return
        if url.endswith("/devices/heartbeat"):
            self.heartbeats += 1
            capabilities = payload.get("capabilities")
            self.registry.upsert_device(
                device_id=payload["device_id"],
                tier=payload["tier"],
                app_version=payload["app_version"],
                capabilities=capabilities if isinstance(capabilities, dict) else {},
                now_iso=NOW_ISO,
            )
            return
        raise AssertionError(f"车道外上行: {url}")

    async def get(self, url: str, token: str) -> dict[str, Any]:
        if url.endswith("/exec_tokens"):
            return {"tokens": verification_set(self.exec_store, device_id=self.device_id, now=NOW)}
        return {"events": [], "cursor": 0}


class _StepClock:
    """注入式单调钟（车道显式推进，时序断言零真实等待）。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, s: float) -> None:
        self.value += s


def _owner_engine(tmp_path: Path, relay: _LaneRelay, territory: Path) -> Any:
    """属主端真引擎：exec 通道配置（领地=工作空间基根）+ 真状态/outbox 存储。"""
    from agent_core import AgentConfig, SyncAgentEngine
    from agent_state import AgentStateStore
    from outbox_store import OutboxStore

    config = AgentConfig(
        relay_url="http://lane-relay/ext/sync_relay",
        token="tok-owner",
        device_id="device-owner",
        device_tier="desktop",
        app_version="1.0.0",
        watch_root=tmp_path / "config",
        exec_channel=ExecChannelConfig(
            mcp_url="http://127.0.0.1:9103/mcp", allowed_roots=(str(territory),)
        ),
        exec_verification_path=tmp_path / "data" / "exec_verification.json",
    )
    return SyncAgentEngine(
        config,
        state=AgentStateStore(tmp_path / "state.db"),
        outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        post_json=relay.post,
        get_json=relay.get,
        clock_mono=_StepClock(),
        now_fn=lambda: NOW_ISO,
    )


def test_engine_hooks_lane(tmp_path: Path) -> None:
    """L6 引擎钩子：注册/心跳携带 exec_channel 子键 + 验证集拉取闭环（D8/D13'）。"""
    registry = RelayStore(tmp_path / "relay.db")
    exec_store = ExecTokenStore(tmp_path / "exec_tokens.db")
    relay = _LaneRelay(registry, exec_store, device_id="device-owner")
    territory = tmp_path / "b_disk"
    territory.mkdir()
    engine = _owner_engine(tmp_path, relay, territory)

    # 注册：真引擎 bootstrap → 注册表 capabilities 携带 exec_channel 子键。
    asyncio.run(engine.bootstrap())
    registered = registry.get_device("device-owner")
    assert registered is not None
    assert registered["capabilities"]["exec_channel"] == {
        "mcp_url": "http://127.0.0.1:9103/mcp",
        "allowed_roots": [str(territory)],
        "protocol_version": EXEC_CHANNEL_PROTOCOL_VERSION,
    }

    # 双钥：发起端 device-a 真签发 → 属主端引擎验证集周期拉取 → Bearer 命中。
    issue_exec_token(
        exec_store,
        issued_by="device-a",
        target_device="device-owner",
        token=TOKEN_AB,
        ttl_secs=3600.0,
        now=NOW,
        device_exists=lambda d: registry.get_device(d) is not None,
    )
    asyncio.run(engine.tick())
    receipt = engine.exec_token_channel.verify(TOKEN_AB, now=NOW)
    assert receipt is not None
    assert receipt["issued_by"] == "device-a"

    # 心跳：同子键随心跳刷新（配置漂移无需重自举），验证面按节奏节流。
    pulls_before = engine.status_dict()["exec_channel"]["verification"]["tokens_persisted"]
    asyncio.run(engine.tick())
    assert relay.heartbeats >= 1
    refreshed = registry.get_device("device-owner")
    assert refreshed is not None
    assert refreshed["capabilities"]["exec_channel"]["allowed_roots"] == [str(territory)]
    assert engine.status_dict()["exec_channel"]["verification"]["tokens_persisted"] == pulls_before
    registry.close()
    exec_store.close()


def test_workspace_anchoring_lane(tmp_path: Path) -> None:
    """L7 工作空间锚定：tree_anchor 锚定 ↔ 解析层往返同一（D9'，T5 寻址契约）。"""
    registry = RelayStore(tmp_path / "relay.db")
    ws_base = tmp_path / "b_disk_ws"
    session_ws = ws_base / "sessions" / "abc123def456"
    session_ws.mkdir(parents=True)
    registry.upsert_device(
        device_id="device-owner",
        tier="desktop",
        app_version="1.0.0",
        capabilities={
            "asset_types": ["config"],
            "exec_channel": {
                "mcp_url": "http://127.0.0.1:9103/mcp",
                "allowed_roots": [str(ws_base)],
                "protocol_version": EXEC_CHANNEL_PROTOCOL_VERSION,
            },
        },
        now_iso=NOW_ISO,
    )
    registry.upsert_device(
        device_id="device-a", tier="desktop", app_version="1.0.0", capabilities={}, now_iso=NOW_ISO
    )

    # 会话工作空间创建期锚定（workspace_lifecycle 接线产物同形）：真实路径 →
    # /本设备id/... 统一路径。
    tree_path = workspace_tree_path(
        str(session_ws), device_id="device-owner", subtree_root=str(ws_base)
    )
    assert tree_path == "/device-owner/sessions/abc123def456"

    # 发起端解析：统一路径一次翻译回属主端真实路径（往返同一，路径保真）。
    view = build_routing_view(registry, now=NOW, window_secs=WINDOW_SECS, requested_by="device-a")
    resolution = resolve_path(tree_path, view, self_device_id="device-a")
    assert resolution.owner_device == "device-owner"
    assert resolution.reachable is True
    assert Path(resolution.real_path) == session_ws

    # 本端前缀：离你最近的前缀（本地执行零网络开销，可达性不依赖心跳）。
    mine = resolve_path(tree_path, view, self_device_id="device-owner")
    assert mine.local is True
    assert mine.reachable is True
    registry.close()
