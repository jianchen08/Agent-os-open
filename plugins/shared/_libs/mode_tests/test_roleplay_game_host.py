# @feature: FP-0.2.〇 RP批4-Ⅰ 游戏域宿主通道 | @vision: V1 可进化 | @ci: python-coverage
"""游戏域会话通道端点 + 出厂游戏页骨架断言（RP 批 4-Ⅰ，Tier 1 第一刀）。

- 单宿主租约三态：新租 attached / 接管 takeover（返回前宿主信息+对账要求）/
  同 host 重入幂等续租 renewed；
- snapshot 薄 sanity（state 为 dict/键为字符串/turn_id·seq 非负整数）后只留
  最新 + 审计追加（source=host_snapshot，ws 经 pipeline-state 行 workspace 解析）
  ——不否决宿主权威（内容零校验零 clamp）；
- GET state 无快照=显式空对象非 404；pending deltas 取后清；
- detach 租约释放（错宿主 409；无租约幂等）；鉴权缺参/形态非法 fail-closed 400；
- game_host 页骨架：会话绑定/attach/detach/快照表/deltas/叙事占位/输入占位 +
  内联脚本 node --check。

通道注册表 = mode_roleplay 进程内寄存器（索引级只留最新；pipeline-state.update
内核写面仅 task.* 前缀，零内核改动前提下进程内是唯一合规落点——ADR 2026-10-11）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PANEL_EP = "/ext/mode_roleplay"
_GAME_HTML = Path(MODES_DIR) / "mode_roleplay" / "webview" / "game_host.html"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_game_host_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 通道注册表清空 + pipeline-state provider 注入（ws 解析链）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    module = _load_server()
    module.reset_providers()
    module._reset_game_channels()
    workspace = tmp_path / "ws"

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
                "mode": "roleplay",
                "workspace": str(workspace),
            }
        ]

    module._set_provider("pipeline-state", _state_rows)
    module.user_config = tmp_path / "user_config"
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)  # 会话工作空间（审计落点，生产由 lifecycle 建）
    return module


def _call(
    module: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query)
    )


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _post(module: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _call(module, path, method="POST", raw_body=json.dumps(body))


_ATTACH = f"{_PANEL_EP}/game/session/attach"
_SNAPSHOT = f"{_PANEL_EP}/game/snapshot"
_STATE = f"{_PANEL_EP}/game/state"
_DETACH = f"{_PANEL_EP}/game/detach"


def _snap_body(
    session_id: str = "pipe-1",
    host_id: str = "factory_web",
    turn_id: Any = 3,
    seq: Any = 9,
    state: Any = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "host_id": host_id,
        "turn_id": turn_id,
        "seq": seq,
        "state": {"hp": 80} if state is None else state,
    }


# ── attach：单宿主租约三态 ────────────────────────────────────────────────────


def test_attach_new_lease_registers_channel(rp: Any) -> None:
    """无租约首挂 = 新租：lease 三字段在场，GET state 可见。"""
    out = _body_json(_post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"}))

    assert out["status"] == "attached"
    assert out["lease"]["host_id"] == "factory_web"
    assert out["lease"]["game_save_id"] is None
    assert out["lease"]["attached_at"]
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["lease"]["host_id"] == "factory_web"
    assert state["snapshot"] == {}, "无快照=显式空对象非 404"
    assert state["pending_deltas"] == []


def test_attach_takeover_returns_previous_host_and_reconcile(rp: Any) -> None:
    """第二宿主 attach = 接管：返回前宿主信息 + 对账要求，租约换手。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    _post(rp, _SNAPSHOT, _snap_body())

    out = _body_json(_post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "godot_mod"}))

    assert out["status"] == "takeover"
    assert out["reconcile_required"] is True
    assert out["previous_host"]["host_id"] == "factory_web"
    assert out["previous_host"]["attached_at"]
    assert out["lease"]["host_id"] == "godot_mod"
    # 接管后快照仍在（宿主离线=保最后快照降级），但旧宿主失去租约
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["lease"]["host_id"] == "godot_mod"
    assert state["snapshot"]["turn_id"] == 3
    stale = _status_of(_post(rp, _SNAPSHOT, _snap_body(host_id="factory_web", turn_id=4)))
    assert stale == 409, "被接管宿主快照拒绝（单宿主租约执法）"


def test_attach_same_host_reentry_renews_idempotent(rp: Any) -> None:
    """同 host 重入 = 幂等续租：不产出接管对账，租约身份不变。"""
    first = _body_json(_post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"}))
    again = _body_json(
        _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web", "game_save_id": "save_9"})
    )

    assert first["status"] == "attached"
    assert again["status"] == "renewed"
    assert "previous_host" not in again
    assert again["lease"]["host_id"] == "factory_web"
    assert again["lease"]["game_save_id"] == "save_9", "重入可补带存档绑定"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"host_id": "factory_web"}, "session_id"),
        ({"session_id": "pipe-1"}, "host_id"),
        ({"session_id": "pipe-1", "host_id": ""}, "host_id"),
        ({"session_id": "pipe 1!", "host_id": "factory_web"}, "非法 session_id"),
        ({"session_id": "pipe-1", "host_id": "坏 id/斜杠"}, "非法 host_id"),
    ],
)
def test_attach_failclosed_invalid_input(rp: Any, body: dict[str, Any], fragment: str) -> None:
    """缺参/形态非法 = 400 可读错误（fail-closed，不静默建租约）。"""
    result = _post(rp, _ATTACH, body)

    assert _status_of(result) == 400
    assert fragment in _body_json(result)["error"]
    assert _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))["lease"] is None


# ── snapshot：薄 sanity + 只留最新 + 审计 ────────────────────────────────────


def test_snapshot_thin_sanity_stores_latest_and_audits(rp: Any, tmp_path: Any) -> None:
    """合法快照存（只留最新）+ 审计追加 source=host_snapshot（复用共享单源）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    first = _post(rp, _SNAPSHOT, _snap_body(turn_id=1, seq=5, state={"hp": 100, "位置": "村口"}))
    second = _post(rp, _SNAPSHOT, _snap_body(turn_id=2, seq=6, state={"hp": 90, "位置": "森林"}))

    assert _status_of(first) == 200
    assert _status_of(second) == 200
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["snapshot"]["turn_id"] == 2, "只留最新快照"
    assert state["snapshot"]["state"] == {"hp": 90, "位置": "森林"}
    assert state["snapshot"]["host_id"] == "factory_web"

    audit = tmp_path / "ws" / "rp_state_audit.jsonl"
    assert audit.is_file(), "审计落会话工作空间（ws 经 pipeline-state 行解析）"
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2, "两次快照两行审计"
    assert all(row["source"] == "host_snapshot" for row in rows)
    assert rows[0]["idem"] == "pipe-1:host_snapshot:1:5", "幂等键含 turn_id+seq"
    assert rows[-1]["to"] == 2


def test_snapshot_replay_same_turn_seq_no_double_audit(rp: Any, tmp_path: Any) -> None:
    """同 turn_id+seq 重推：审计幂等不双记（快照本体仍覆盖，宿主权威）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    _post(rp, _SNAPSHOT, _snap_body(turn_id=1, seq=5, state={"hp": 100}))
    _post(rp, _SNAPSHOT, _snap_body(turn_id=1, seq=5, state={"hp": 99}))

    audit = tmp_path / "ws" / "rp_state_audit.jsonl"
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1, "同 idem 重放不双记"
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["snapshot"]["state"] == {"hp": 99}, "快照只留最新（宿主权威不否决）"


def test_snapshot_host_authority_not_vetoed(rp: Any) -> None:
    """薄 sanity 不否决宿主权威：任意嵌套值/超大数值原样存（零 clamp 零 schema）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    weird = {"hp": 10**18, "背包": [{"id": "剑", "n": 99}], "嵌": {"a": {"b": [1, 2, 3]}}}
    out = _post(rp, _SNAPSHOT, _snap_body(state=weird))

    assert _status_of(out) == 200
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["snapshot"]["state"] == weird


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"session_id": ""}, "session_id"),
        ({"host_id": "x/y"}, "非法 host_id"),
        ({"state": "不是对象"}, "state"),
        ({"state": []}, "state"),
        ({"turn_id": "3"}, "turn_id"),
        ({"turn_id": True}, "turn_id"),
        ({"seq": -1}, "seq"),
    ],
)
def test_snapshot_failclosed_invalid_input(rp: Any, override: dict[str, Any], fragment: str) -> None:
    """薄 sanity：形态非法 = 400（键为字符串/state 为 dict/turn·seq 非负整数）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    body = _snap_body()
    body.update(override)

    result = _post(rp, _SNAPSHOT, body)

    assert _status_of(result) == 400
    assert fragment in _body_json(result)["error"]


def test_snapshot_non_string_state_keys_failclosed(rp: Any) -> None:
    """state 键非字符串 = 400（MCP 直调形态可携带非 str 键，HTTP JSON 形态不可达）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = asyncio.run(rp._handle_game_snapshot(_snap_body(state={1: "非字符串键"})))

    assert result["data"]["status"] == 400
    assert "字符串" in _body_json(result)["error"]


def test_snapshot_without_lease_conflict(rp: Any) -> None:
    """未 attach 直推快照 = 409（先租后推）。"""
    result = _post(rp, _SNAPSHOT, _snap_body())

    assert _status_of(result) == 409
    assert "租约" in _body_json(result)["error"]


# ── state：读面（快照+租约+pending deltas 取后清）────────────────────────────


def test_state_unknown_session_explicit_empty(rp: Any) -> None:
    """未知会话 = 显式空对象 200（非 404，宿主轮询友好）。"""
    out = _body_json(_call(rp, _STATE, query={"session_id": "nope"}))

    assert out == {"session_id": "nope", "lease": None, "snapshot": {}, "pending_deltas": []}


def test_state_missing_session_id_400(rp: Any) -> None:
    result = _call(rp, _STATE)

    assert _status_of(result) == 400
    assert "session_id" in _body_json(result)["error"]


def test_state_pending_deltas_take_then_clear(rp: Any) -> None:
    """push_delta 入队 → GET state 携带 → 再 GET 为空（取后清）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    queued = asyncio.run(
        rp.mode_game_push_delta(
            session_id="pipe-1",
            turn_seq=7,
            deltas=[{"key": "game.hp", "value": "+5"}, {"key": "game.pos", "value": "森林"}],
        )
    )
    assert queued["queued"] == 2

    first = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert first["pending_deltas"] == [
        {"key": "game.hp", "value": "+5", "turn_seq": 7},
        {"key": "game.pos", "value": "森林", "turn_seq": 7},
    ], "推送 turn_seq 随行入队（对账序）"
    second = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert second["pending_deltas"] == [], "取后清"


def test_push_delta_without_lease_dropped(rp: Any) -> None:
    """无租约会话的 delta 丢弃（无宿主=Tier 0，delta 无去处）——不崩不积压。"""
    out = asyncio.run(
        rp.mode_game_push_delta(session_id="pipe-1", turn_seq=7, deltas=[{"key": "game.hp", "value": 1}])
    )

    assert out["queued"] == 0
    assert _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))["pending_deltas"] == []


def test_push_delta_queue_cap_drops_oldest(rp: Any) -> None:
    """队列上限（宿主离线保护）：超出丢最旧，新 delta 仍在队尾可取。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    cap = int(rp._GAME_CHANNEL_CAP)
    deltas = [{"key": f"game.k{i}", "value": i} for i in range(cap + 5)]

    out = asyncio.run(rp.mode_game_push_delta(session_id="pipe-1", turn_seq=1, deltas=deltas))

    assert out["queued"] == cap
    assert out["dropped"] == 5
    pending = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))["pending_deltas"]
    assert len(pending) == cap
    assert pending[0]["key"] == "game.k5", "最旧 5 条被丢弃"
    assert pending[-1]["key"] == f"game.k{cap + 4}"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"session_id": "", "deltas": [{"key": "g", "value": 1}]}, "session_id"),
        ({"session_id": "pipe-1", "deltas": "不是列表"}, "deltas"),
        ({"session_id": "pipe-1", "deltas": [{"value": 1}]}, "key"),
        ({"session_id": "pipe-1", "deltas": []}, "deltas"),
    ],
)
def test_push_delta_failclosed(rp: Any, kwargs: dict[str, Any], fragment: str) -> None:
    """push_delta 入参 fail-closed（服务面同通道校验，ValueError 信封上行）。"""
    with pytest.raises(ValueError, match=fragment):
        asyncio.run(rp.mode_game_push_delta(**kwargs))


# ── detach：租约释放 ─────────────────────────────────────────────────────────


def test_detach_releases_lease_keeps_snapshot_clears_pending(rp: Any) -> None:
    """detach：租约释放 + pending 清积压；快照保留（宿主离线=保最后快照降级）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    asyncio.run(
        rp.mode_game_push_delta(session_id="pipe-1", turn_seq=1, deltas=[{"key": "game.hp", "value": 1}])
    )
    _post(rp, _SNAPSHOT, _snap_body())

    out = _body_json(_post(rp, _DETACH, {"session_id": "pipe-1", "host_id": "factory_web"}))

    assert out["status"] == "detached"
    assert out["released"] is True
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["lease"] is None
    assert state["pending_deltas"] == []
    assert state["snapshot"]["turn_id"] == 3, "快照保留（离线降级读面）"


def test_detach_idempotent_without_lease(rp: Any) -> None:
    out = _body_json(_post(rp, _DETACH, {"session_id": "pipe-1", "host_id": "factory_web"}))

    assert out["status"] == "detached"
    assert out["released"] is False


def test_detach_wrong_host_conflict(rp: Any) -> None:
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _DETACH, {"session_id": "pipe-1", "host_id": "intruder"})

    assert _status_of(result) == 409
    state = _body_json(_call(rp, _STATE, query={"session_id": "pipe-1"}))
    assert state["lease"]["host_id"] == "factory_web", "他宿主不可释我租约"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (_SNAPSHOT, {}),
        (_DETACH, {"host_id": "factory_web"}),
    ],
)
def test_post_endpoints_missing_params_400(rp: Any, path: str, body: dict[str, Any]) -> None:
    """POST 通道端点缺参 = 400（鉴权由内核 auth:user 承担，此处验参数面）。"""
    result = _post(rp, path, body)

    assert _status_of(result) == 400


def test_game_routes_wrong_method_404(rp: Any) -> None:
    result = _call(rp, _STATE.replace("/game/state", "/game/session/attach"), method="GET")

    assert _status_of(result) == 404


# ── 出厂游戏页骨架（webview 结构断言 + node --check）──────────────────────────


def test_game_page_route_serves_html(rp: Any) -> None:
    result = _call(rp, f"{_PANEL_EP}/page/game-host")
    assert result["success"] is True
    assert result["data"]["headers"]["Content-Type"].startswith("text/html")
    html = base64.b64decode(result["data"]["body"]).decode("utf-8")
    assert "游戏宿主" in html


def test_game_page_skeleton_structure() -> None:
    """骨架结构断言：绑定指示/attach/detach/快照表/deltas/叙事占位/输入占位。"""
    src = _GAME_HTML.read_text(encoding="utf-8")
    for tid in (
        "gh-session",
        "gh-attach",
        "gh-detach",
        "gh-lease",
        "gh-snapshot",
        "gh-deltas",
        "gh-narration",
        "gh-input",
        "gh-send",
    ):
        assert f'data-testid="{tid}"' in src, f"游戏页骨架缺 {tid}"
    assert "factory_web" in src, "出厂宿主 id 常量在场"
    assert "/game/session/attach" in src, "attach 通道调用在场"
    assert "/game/detach" in src, "detach 通道调用在场"
    assert "/game/state" in src, "快照轮询消费 GET state"
    assert "data/sessions" in src, "会话绑定轮询 data/sessions"
    assert "data/messages" in src, "叙事流消费既有消息端点"
    assert re.search(r"POLL_INTERVAL_MS\s*=\s*\d+", src), "轮询间隔可配置常量在场"


def test_game_page_turn_channel_wired() -> None:
    """玩家输入区 turn 通道已接通（RP 批 5 解除批 4 占位红线）：发送经 rpHost.turn
    打 /game/turn；外部桥 turn 不接（扩展波 A）。既有兼容态断言不动——不发消息
    =现状零变化由行为测试锁定，本测试只锁接线面。"""
    src = _GAME_HTML.read_text(encoding="utf-8")
    assert "/game/turn" in src, "turn 通道已接通（批 5）"
    assert "rpHost.turn(" in src, "发送走 rpHost 桥单源"
    assert "game_bridge/turn" not in src, "外部桥 turn 亦不接（扩展波 A）"


def test_game_page_operations_wired() -> None:
    """操作区接线（世界内嵌批次二：自由操作→结构化命令）：声明清单→按钮+内联
    参数输入，点击走 rpBuildOperationPayload 闸 + rpHost.operation 打
    /game/operation；applied 回执带 state_delta 回显、rejected 原因原文上页不吞
    （拒绝必须带原因）。stardew 自包含页不接（has_page 页引擎区未接，台账登记）。"""
    src = _GAME_HTML.read_text(encoding="utf-8")
    assert "/game/operation" in src, "operation 通道已接通（批次二）"
    assert "rpHost.operation(" in src, "发送走 rpHost 桥单源"
    assert "rpBuildOperationPayload" in src, "操作发送闸纯函数（与 turn 闸同形）"
    assert "rpCoerceParamValue" in src, "内联输入按声明 type 尽力转形"
    assert 'data-testid="gh-operations"' in src, "操作区容器（声明清单→按钮）"
    assert 'data-testid="gh-op-btn"' in src, "清单条目按钮"
    assert "gh-op-receipt" in src, "操作回执元素"
    assert "state_delta" in src, "applied 回执 state_delta 回显"
    assert "操作被拒" in src, "rejected 原因原文上页（不吞）"
    assert "config.operations" in src, "声明源走 game/config operations 键"


def test_game_page_tick_wired() -> None:
    """世界演化区接线（世界内嵌批次三：tick/长时段演化）：声明在场才渲染⏩推进
    （ticks 输入缺省 1），点击走 rpBuildTickPayload 闸 + rpHost.tick 打
    /game/tick；advanced=状态刷新+delta 回显、空转/被拒原因上页不吞；stardew
    自包含页不接（has_page 页引擎区未接，台账登记留白）。"""
    src = _GAME_HTML.read_text(encoding="utf-8")
    assert "/game/tick" in src, "tick 通道已接通（批次三）"
    assert "rpHost.tick(" in src, "发送走 rpHost 桥单源"
    assert "rpBuildTickPayload" in src, "推进发送闸纯函数（与 operation 闸同形）"
    assert 'data-testid="gh-tick"' in src, "世界演化区容器（声明在场才渲染）"
    assert 'data-testid="gh-tick-btn"' in src, "⏩ 推进按钮"
    assert "gh-tick-receipt" in src, "推进回执元素"
    assert "advanced" in src, "回执 advanced 键（空转/生效二态可辨）"
    assert "state_delta" in src, "advanced 回执 state_delta 回显"
    assert "推进被拒" in src, "被拒原因原文上页（不吞）"
    assert "config.tick" in src, "声明源走 game/config tick 键"


def test_game_page_setup_view_wired() -> None:
    """开演设置（刀4「工作区玩法页全 webview 化」）：通用 init_ui 声明式表单
    接线面——IIFE 嵌入安全（面板「工作区」签 document 注入同窗挂载不炸宿主
    全局）、data/packs 消费走桥、开演载荷与面板既有开演同键同源（rp_init 组装
    走 rp-core 单源装配）、零包名硬编码、空会话自动展开 + 手动收起记忆。"""
    src = _GAME_HTML.read_text(encoding="utf-8")
    assert "/*@agentos-include:rp-core*/" in src, "rp-core 装配标记（rp_init 组装单源）"
    assert "(function () {" in src, "页内 var/function 收敛进 IIFE（嵌入面板全局安全）"
    assert 'id="gh-setup"' in src
    assert 'id="gh-setup-body"' in src
    assert 'data-testid="gh-setup-pack"' in src
    # 玩法内定角色（2026-10-17 用户裁定）：包绑定卡在场 = 免选（固定卡司，无选卡件）
    assert 'data-testid="gh-setup-card"' in src, "无绑定卡包回落全库下拉（兼容态）"
    assert 'data-testid="gh-setup-route-opt"' in src, "init_ui 路线单选件"
    assert 'data-testid="gh-setup-trait-opt"' in src, "init_ui 词条多选件"
    assert 'data-testid="gh-setup-start"' in src
    assert "setupOffer()" in src, "无进行中会话=落开演设置（收起过不再自动展开）"
    assert "setup.dismissed" in src
    start = src.split("function setupStart(")[1].split("$('gh-setup-open')")[0]
    assert "mode.session" in start, "开演走 mode.session 既有路径"
    assert "wizard_pack_ids" in start
    assert "wizard_profile" in start
    assert "rpInitSnapshot(rpInitPairs(route, traits))" in start, "rp_init 组装走 rp-core 单源"
    # 可见默认即状态（2026-10-15 用户实测：下拉显示已选卡、开演报未选——隐式
    # 选中未回写状态层）：renderSetup 尾部必须把浏览器默认选中回写 setup 状态
    assert "if (!inPool) setup.cardId = cardSelEl.value;" in src, "卡可见默认回写（作用域感知）"
    assert "if (packSelEl && packSelEl.value && !setupPack()) setup.packId = packSelEl.value;" in src, "包可见默认回写"
    # 开演失败原因必须上表单可见（gh-status 小字用户看不见）
    assert 'id="gh-setup-err"' in src, "表单内错误行"
    assert "setupError('开演失败: ' + err.message)" in start, "失败原因上表单"
    # 玩法内定角色：包绑定卡（卡顶层 pack_ids 引用包）= 免选锁定 + 内定注记，
    # 无绑定卡回落全库下拉（兼容态）
    assert "var boundCards = pack" in src, "包绑定卡识别"
    assert "var fixedCast = boundCards.length > 0;" in src, "内定形态判定"
    assert "setup.cardId = boundCards[0].id;" in src, "内定身份锁定（残留选择自动归位）"
    assert "本玩法固定卡司" in src, "内定注记上表单（免选可辨）"
    assert "roleplay_greeting" in start
    assert "rp:session-bound" in start, "页内开演回执广播（面板同窗同步锚）"
    assert "rpHost.packs" in src, "清点消费走 rpHost 桥单源"
    lowered = src.lower()
    assert "stardew" not in lowered, "零包名硬编码（星露谷）"
    assert "xianxia" not in lowered, "零包名硬编码（修仙）"
    assert "__RP_PAGE_PACK__" in src, "挂载上下文包通道（面板「工作区」签预选）"


def test_game_page_inline_script_syntax_valid() -> None:
    """内联脚本 node --check（webview 纯 HTML 无构建线的语法自检）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用（webview 无构建线，语法自检择机补跑）")
    src = _GAME_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", src, re.S)
    assert match is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(match.group(1))
        path = fh.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True, check=False)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, f"node --check 失败: {proc.stderr}"


def test_manifest_declares_game_pages_routes_and_service() -> None:
    """manifest 面：游戏页 HTML 路由 + 四通道端点 + push_delta 服务声明（零新增
    插件）；单页化改钉（2026-10-14 用户裁定「怎么有两个页面」）：contributes.pages
    无 game_host 条目——游戏界面入口=面板玩法子页内嵌挂载（/page/game-host 路由
    保留），不再渲染独立工作区 tab；message-style 卡载体两声明不在本断言面。"""
    manifest = json.loads(
        (Path(MODES_DIR) / "mode_roleplay" / "plugin.json").read_text(encoding="utf-8")
    )
    routes = {r["path"]: r["method"] for r in manifest["http_endpoints"]}
    assert routes[f"{_PANEL_EP}/game/session/attach"] == "POST"
    assert routes[f"{_PANEL_EP}/game/snapshot"] == "POST"
    assert routes[f"{_PANEL_EP}/game/state"] == "GET"
    assert routes[f"{_PANEL_EP}/game/detach"] == "POST"
    assert f"{_PANEL_EP}/page/game-host" in routes, "游戏页 HTML 路由声明在场（内嵌挂载源）"
    assert f"{_PANEL_EP}/page/roleplay-panel" in routes, "面板页路由在场（唯一 workspace tab）"
    services = {s["name"] for s in manifest["capabilities"]["services"]}
    assert "mode.game.push_delta" in services
    pages = manifest["contributes"]["pages"]
    assert all(p["id"] != "game_host" for p in pages), "单页化：pages 无 game_host 独立 tab 条目"
    workspace_tabs = [p for p in pages if p.get("space") == "workspace" and p.get("slot") == "tab"]
    assert [p["id"] for p in workspace_tabs] == ["roleplay_studio"], "workspace tab 仅面板一个"
    message_styles = [p for p in pages if p.get("slot") == "message-style"]
    assert message_styles == [], \
        "卡型页退役（2026-10-17 用户裁定：消息流不挂固定 webview 卡，状态/选项归工作区玩法页）"


# ── 车道补覆盖：失败分支面（OSError 降级/方法错配/形态校验剩余分支）──────────────


def test_attach_game_save_id_must_be_string(rp: Any) -> None:
    """game_save_id 非字符串 = 400（fail-closed，不做静默 str 归一）。"""
    result = _post(
        rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web", "game_save_id": 123}
    )

    assert _status_of(result) == 400
    assert "game_save_id" in _body_json(result)["error"]


def test_state_bad_shape_session_id_400(rp: Any) -> None:
    """GET state 形态非法 session_id = 400（与 POST 通道同闸）。"""
    result = _call(rp, _STATE, query={"session_id": "坏 id/斜杠"})

    assert _status_of(result) == 400
    assert "非法 session_id" in _body_json(result)["error"]


def test_detach_missing_host_id_400(rp: Any) -> None:
    result = _post(rp, _DETACH, {"session_id": "pipe-1"})

    assert _status_of(result) == 400
    assert "host_id" in _body_json(result)["error"]


def test_push_delta_bad_shape_session_id_rejected(rp: Any) -> None:
    """push_delta 形态非法 session_id = ValueError（与缺参同通道）。"""
    with pytest.raises(ValueError, match="非法 session_id"):
        asyncio.run(
            rp.mode_game_push_delta(session_id="bad/slash", deltas=[{"key": "g", "value": 1}])
        )


def test_game_state_route_post_method_404(rp: Any) -> None:
    """GET 端点 POST = 404（方法错配）。"""
    result = _post(rp, _STATE, {"session_id": "pipe-1"})

    assert _status_of(result) == 404


def test_game_page_read_failure_500(rp: Any, patch_shared: Any) -> None:
    """游戏页 HTML 读失败 = success=False + 500（面板页同款降级形态）。"""
    patch_shared("_GAME_PAGE_HTML_PATH", "Z:/不存在/game_host.html")  # 消费点在 rp_game 自持绑定

    result = _call(rp, f"{_PANEL_EP}/page/game-host")

    assert result["success"] is False
    assert _status_of(result) == 500
