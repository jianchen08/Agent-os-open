"""triggers 域 REST 面测试（channel_api triggers stub 接真）。

覆盖 http_api 端点：列表/创建（工具语义 + Bearer user_id 解析）/管道选项/
详情/更新/删除/enable/disable/手动触发/统计——进程内 TriggerManager 单例，
fake 注入器验证 fire_manually 投递路径。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO / "plugins" / "shared" / "tools" / "triggers_ext"
_SDK = _REPO / "plugins" / "sdk" / "src"
for _d in (str(_PLUGIN_DIR), str(_SDK)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import http_api  # noqa: E402
import server as trigger_server  # noqa: E402
from triggers.manager import get_trigger_manager  # noqa: E402
from triggers.types import TriggerConfig, TriggerType  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单测：进程内单例 + fake 注入器，零外部依赖


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    """http.handle 信封 → 业务 JSON。"""
    assert resp.get("success") is True, resp
    data = resp["data"]
    body = base64.b64decode(data["body"]).decode("utf-8")
    return json.loads(body)


def _encode_body(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _token(user_id: str = "u1", username: str = "alice", *, expired: bool = False) -> str:
    """内核 0.2 开发期 token：base64_nopad("access:{user_id}:{username}:{exp}")。"""
    exp = int(time.time()) - 10 if expired else int(time.time()) + 600
    payload = f"access:{user_id}:{username}:{exp}"
    return base64.b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _make_trigger(
    trigger_id: str = "trigger_event_abc123def456",
    *,
    pipeline_id: str = "p1",
    trigger_type: TriggerType = TriggerType.EVENT,
) -> TriggerConfig:
    return TriggerConfig(
        trigger_id=trigger_id,
        name="测试触发器",
        trigger_type=trigger_type,
        event_name="task_completed",
        message="触发注入消息",
        pipeline_id=pipeline_id,
    )


@pytest.fixture(autouse=True)
def _clean_manager():
    """每用例重置单例注册表与注入器/桥。"""
    mgr = get_trigger_manager()
    for cfg in mgr.list_all():
        mgr.unregister(cfg.trigger_id)
    mgr.set_injector(None)
    mgr.set_main_loop(None)
    mgr.set_state_provider(None)
    yield
    for cfg in mgr.list_all():
        mgr.unregister(cfg.trigger_id)


# ── list / stats ────────────────────────────────────────────────────


def test_list_empty() -> None:
    body = _unwrap(asyncio.run(http_api.list_triggers(None)))
    assert body == {"items": [], "total": 0}


def test_list_with_status_filter() -> None:
    mgr = get_trigger_manager()
    # register 自动置 ACTIVE（既有语义）
    mgr.register(_make_trigger("trigger_event_a1".ljust(24, "0")))
    mgr.register(_make_trigger("trigger_event_b2".ljust(24, "0")))
    body_active = _unwrap(asyncio.run(http_api.list_triggers({"status": "active"})))
    assert body_active["total"] == 2
    body_pending = _unwrap(asyncio.run(http_api.list_triggers({"status": "pending"})))
    assert body_pending["total"] == 0


def test_serialize_shape() -> None:
    mgr = get_trigger_manager()
    mgr.register(_make_trigger())
    body = _unwrap(asyncio.run(http_api.list_triggers(None)))
    item = body["items"][0]
    assert item["trigger_id"] == "trigger_event_abc123def456"
    assert item["trigger_type"] == "event"
    assert item["status"] == "active"
    assert item["message"] == "触发注入消息"
    assert item["event_name"] == "task_completed"


def test_stats() -> None:
    mgr = get_trigger_manager()
    mgr.register(_make_trigger("trigger_event_a1".ljust(24, "0")))
    mgr.register(_make_trigger(
        "trigger_delay_b2".ljust(24, "0"), trigger_type=TriggerType.DELAY))
    mgr.register(_make_trigger("trigger_event_c3".ljust(24, "0")))
    body = _unwrap(asyncio.run(http_api.trigger_stats()))
    assert body["total"] == 3
    assert body["by_type"]["event"] == 2
    assert body["by_type"]["delay"] == 1
    assert body["active"] == 3


# ── get / update / delete ───────────────────────────────────────────


def test_get_ok_and_404() -> None:
    mgr = get_trigger_manager()
    mgr.register(_make_trigger())
    body = _unwrap(asyncio.run(http_api.get_trigger("trigger_event_abc123def456")))
    assert body["trigger"]["trigger_id"] == "trigger_event_abc123def456"
    resp = asyncio.run(http_api.get_trigger("trigger_event_zzz999zzz999"))
    assert resp["success"] is False and resp["data"]["status"] == 404


def test_update_404_when_missing() -> None:
    resp = asyncio.run(http_api.update_trigger("trigger_event_zzz999zzz999", {}))
    assert resp["success"] is False and resp["data"]["status"] == 404


def test_delete_ok_and_404() -> None:
    mgr = get_trigger_manager()
    mgr.register(_make_trigger())
    body = _unwrap(asyncio.run(http_api.delete_trigger("trigger_event_abc123def456")))
    assert body == {"deleted": True, "trigger_id": "trigger_event_abc123def456"}
    assert mgr.get("trigger_event_abc123def456").status.value == "cancelled"
    resp = asyncio.run(http_api.delete_trigger("trigger_event_zzz999zzz999"))
    assert resp["success"] is False and resp["data"]["status"] == 404


# ── enable / disable ────────────────────────────────────────────────


def test_enable_disable_cycle() -> None:
    mgr = get_trigger_manager()
    mgr.register(_make_trigger())
    # 走端点路径：enable → ACTIVE
    body = _unwrap(asyncio.run(http_api.handle_triggers_http(
        "POST", "/ext/trigger_setup_tool/triggers/trigger_event_abc123def456/enable", None)))
    assert body["trigger"]["status"] == "active"
    body = _unwrap(asyncio.run(http_api.handle_triggers_http(
        "POST", "/ext/trigger_setup_tool/triggers/trigger_event_abc123def456/disable", None)))
    assert body["trigger"]["status"] == "pending"
    resp = asyncio.run(http_api.handle_triggers_http(
        "POST", "/ext/trigger_setup_tool/triggers/trigger_event_zzz999zzz999/enable", None))
    assert resp["success"] is False and resp["data"]["status"] == 404


# ── fire（手动触发经注入器投递）──────────────────────────────────────


def test_fire_manually_injects() -> None:
    delivered: list[tuple[str, str]] = []

    async def _fake_injector(pipeline_id: str, message: str, user_id: str) -> Any:
        delivered.append((pipeline_id, message))
        return {"ok": True}

    mgr = get_trigger_manager()
    loop = asyncio.new_event_loop()
    try:
        mgr.set_main_loop(loop)
        mgr.set_injector(_fake_injector)
        cfg = _make_trigger(pipeline_id="p_fire")
        mgr.register(cfg)
        body = _unwrap(loop.run_until_complete(http_api.fire_trigger(cfg.trigger_id)))
        assert body == {"fired": True, "trigger_id": cfg.trigger_id}
        loop.run_until_complete(asyncio.sleep(0))  # 注入经 loop 调度
        # 注入消息带通知前缀（与到期触发同语义，_format_fire_info 打包）
        assert delivered == [("p_fire", "[触发器通知] 触发器 '测试触发器' 已触发 (第 1 次/共 1 次)\n触发注入消息")]
        assert cfg.fire_count == 1
    finally:
        loop.close()


def test_fire_404_when_missing() -> None:
    resp = asyncio.run(http_api.fire_trigger("trigger_event_zzz999zzz999"))
    assert resp["success"] is False and resp["data"]["status"] == 404


# ── http.handle 分发（路径解析/body 解码/未知 404）────────────────────


def test_dispatch_direct_handlers() -> None:
    """handle_triggers_http 路由覆盖全部 9 端点语义。"""
    mgr = get_trigger_manager()
    mgr.register(_make_trigger("trigger_event_a1".ljust(24, "0")))
    # GET 列表
    body = _unwrap(asyncio.run(http_api.handle_triggers_http(
        "GET", "/ext/trigger_setup_tool/triggers", None)))
    assert body["total"] == 1
    # GET stats
    body = _unwrap(asyncio.run(http_api.handle_triggers_http(
        "GET", "/ext/trigger_setup_tool/triggers/stats", None)))
    assert body["total"] == 1
    # GET 详情
    tid = "trigger_event_a1".ljust(24, "0")
    body = _unwrap(asyncio.run(http_api.handle_triggers_http(
        "GET", f"/ext/trigger_setup_tool/triggers/{tid}", None)))
    assert body["trigger"]["trigger_id"] == tid
    # 未知路径 404
    resp = asyncio.run(http_api.handle_triggers_http(
        "GET", "/ext/trigger_setup_tool/triggers/xxx/yyy", None))
    assert resp["success"] is False and resp["data"]["status"] == 404


def test_dispatch_bad_json_body_400() -> None:
    resp = asyncio.run(http_api.handle_triggers_http(
        "POST", "/ext/trigger_setup_tool/triggers", None, raw_body="not-json"))
    assert resp["success"] is False and resp["data"]["status"] == 400


def test_server_dispatch_fallthrough() -> None:
    """server 入口：非本插件前缀 → 404（fail-closed）。"""
    resp = asyncio.run(http_api.handle_http_dispatch(
        "/ext/other/triggers", "GET"))
    assert resp["success"] is False and resp["data"]["status"] == 404


# ── 管道选项（创建表单 pipeline_id 数据源）──────────────────────────


def _fake_state_provider(rows: list[dict[str, Any]]) -> Any:
    async def _provide() -> list[dict[str, Any]]:
        return rows
    return _provide


def test_pipeline_options_label_and_value() -> None:
    """display_name/name/task.goal 依次取显示名，value=pipeline_id；无 id 行跳过。"""
    get_trigger_manager().set_state_provider(_fake_state_provider([
        {"pipeline_id": "p1", "display_name": "会话A"},
        {"pipeline_id": "p2", "name": "任务B"},
        {"pipeline_id": "p3", "task.goal": "写周报"},
        {"pipeline_id": "p4"},
        {"display_name": "无管道行"},
    ]))
    body = _unwrap(asyncio.run(http_api.handle_http_dispatch(
        "/ext/trigger_setup_tool/pipelines", "GET", "", None, {})))
    assert body["options"] == [
        {"label": "会话A（p1）", "value": "p1"},
        {"label": "任务B（p2）", "value": "p2"},
        {"label": "写周报（p3）", "value": "p3"},
        {"label": "p4", "value": "p4"},
    ]


def test_pipeline_options_bridge_missing_500() -> None:
    """state provider 未注入 → 500（fail-visible，不静默空选项）。"""
    resp = asyncio.run(http_api.handle_http_dispatch(
        "/ext/trigger_setup_tool/pipelines", "GET", "", None, {}))
    assert resp["success"] is False and resp["data"]["status"] == 500


# ── 创建（Bearer user_id 解析 → 触发器 metadata）───────────────────


_CREATE_BODY = {
    "trigger_type": "delay",
    "delay_seconds": 60,
    "message": "检查任务状态",
    "pipeline_id": "p_target",
    "name": "UI 创建的延迟触发器",
}


def test_create_with_bearer_token_records_user() -> None:
    """Bearer token 的 user_id 落触发器 metadata（到期注入 chat.send_message 必需）。"""
    for uid, username in (("u1", "alice"), ("u2", "bob")):
        resp = asyncio.run(http_api.handle_http_dispatch(
            "/ext/trigger_setup_tool/triggers", "POST",
            _encode_body({**_CREATE_BODY, "name": f"trg-{uid}"}),
            None, {"Authorization": f"Bearer {_token(uid, username)}"},
        ))
        body = _unwrap(resp)
        cfg = get_trigger_manager().get(body["trigger"]["trigger_id"])
        assert cfg is not None
        assert cfg.pipeline_id == "p_target"
        assert cfg.metadata["user_id"] == uid


def test_create_without_credentials_401() -> None:
    """缺 Bearer/过期 token → 401（注册到期必投递失败的触发器是静默债）。"""
    for headers in (None, {}, {"Authorization": "Bearer not-a-token"},
                    {"Authorization": f"Bearer {_token(expired=True)}"}):
        resp = asyncio.run(http_api.handle_http_dispatch(
            "/ext/trigger_setup_tool/triggers", "POST",
            _encode_body(_CREATE_BODY), None, headers,
        ))
        assert resp["success"] is False and resp["data"]["status"] == 401, headers
    assert get_trigger_manager().list_all() == []


def test_server_http_handle_forwards_headers_to_create() -> None:
    """server.http.handle 入口 → 分发：headers 透传到 create 的 Bearer 解析。"""
    # server.py 的 http_handle 内惰性 `from http_api import ...` 在**调用期**
    # 按 sys.modules 解析——tasks 系测试运行期可能残留 tasks 版 http_api
    # 且其目录仍在 sys.path 更前位；逐出裸名并置顶本插件目录，保证命中
    # 本插件副本（conftest 逐出只覆盖收集期）。
    _was = sys.modules.pop("http_api", None)
    _plugin_dir = str(_PLUGIN_DIR)
    _was_first = sys.path[0] == _plugin_dir
    if _plugin_dir in sys.path:
        sys.path.remove(_plugin_dir)
    sys.path.insert(0, _plugin_dir)
    try:
        resp = asyncio.run(trigger_server.http_handle(
            path="/ext/trigger_setup_tool/triggers",
            method="POST",
            raw_body=_encode_body(_CREATE_BODY),
            headers={"Authorization": f"Bearer {_token('u9', 'carol')}"},
        ))
    finally:
        sys.path.remove(_plugin_dir)
        if _was_first:
            sys.path.insert(0, _plugin_dir)
        if _was is not None:
            sys.modules["http_api"] = _was
    body = _unwrap(resp)
    cfg = get_trigger_manager().get(body["trigger"]["trigger_id"])
    assert cfg is not None
    assert cfg.metadata["user_id"] == "u9"
