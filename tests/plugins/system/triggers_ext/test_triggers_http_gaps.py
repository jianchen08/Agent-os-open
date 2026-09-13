# @feature: FP-0.2.二 triggers_ext http 缺口分支补测 | @ci: python-coverage
"""triggers 域 REST 面行覆盖缺口补充测试。

聚焦既有 test_triggers_http.py 未触达的分支：创建时工具校验失败（400）、
PUT 更新（成功/校验失败）、DELETE 与手动触发经 dispatch 路由分发。
harness（信封解包/Bearer token/单例清理）与 test_triggers_http.py 同款。
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
for _d in (str(_PLUGIN_DIR), str(_REPO / "plugins" / "sdk" / "src")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import http_api  # noqa: E402
from triggers.manager import get_trigger_manager  # noqa: E402
from triggers.types import TriggerConfig, TriggerType  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单测：进程内单例 + fake 注入器，零外部依赖

PREFIX = "/ext/trigger_setup_tool/triggers"


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


# ── 创建：工具校验失败 → 400 ────────────────────────────────────────


def test_create_tool_failure_returns_400() -> None:
    """合法 Bearer 但 body 缺 message → 工具校验失败 → 400，注册表不残留。"""
    resp = asyncio.run(http_api.handle_http_dispatch(
        PREFIX, "POST",
        _encode_body({"trigger_type": "delay", "delay_seconds": 60, "pipeline_id": "p_target"}),
        None, {"Authorization": f"Bearer {_token('u1', 'alice')}"},
    ))
    assert resp["success"] is False, resp
    assert resp["data"]["status"] == 400
    assert get_trigger_manager().list_all() == []


# ── PUT 更新：dispatch 分发 + 成功/校验失败双路径 ────────────────────


def test_update_via_dispatch_ok_and_validation_400() -> None:
    """PUT /triggers/{id}：管道不匹配 → 400 不改配置；匹配 → 更新生效并回序列化结果。"""
    mgr = get_trigger_manager()
    cfg = _make_trigger()
    mgr.register(cfg)
    resp = asyncio.run(http_api.handle_http_dispatch(
        f"{PREFIX}/{cfg.trigger_id}", "PUT",
        _encode_body({"pipeline_id": "other", "max_count": 5}),
    ))
    assert resp["success"] is False, resp
    assert resp["data"]["status"] == 400
    assert cfg.max_fires == 1, "校验失败的更新不得改动触发器"

    body = _unwrap(asyncio.run(http_api.handle_http_dispatch(
        f"{PREFIX}/{cfg.trigger_id}", "PUT",
        _encode_body({"pipeline_id": "p1", "max_count": 5, "max_time": "2h"}),
    )))
    assert body["trigger"]["trigger_id"] == cfg.trigger_id
    assert body["trigger"]["max_fires"] == 5
    assert cfg.max_fires == 5
    assert cfg.max_time_seconds == 2 * 3600


# ── DELETE / 手动触发：dispatch 分发 ────────────────────────────────


def test_delete_via_dispatch() -> None:
    mgr = get_trigger_manager()
    cfg = _make_trigger()
    mgr.register(cfg)
    body = _unwrap(asyncio.run(http_api.handle_http_dispatch(
        f"{PREFIX}/{cfg.trigger_id}", "DELETE")))
    assert body == {"deleted": True, "trigger_id": cfg.trigger_id}
    cancelled = mgr.get(cfg.trigger_id)
    assert cancelled is not None
    assert cancelled.status.value == "cancelled"


def test_fire_via_dispatch() -> None:
    """POST /triggers/{id}/trigger 经 dispatch 分发 → 手动触发注入。"""
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
        body = _unwrap(loop.run_until_complete(http_api.handle_http_dispatch(
            f"{PREFIX}/{cfg.trigger_id}/trigger", "POST")))
        assert body == {"fired": True, "trigger_id": cfg.trigger_id}
        loop.run_until_complete(asyncio.sleep(0))  # 注入经 loop 调度
        assert delivered and delivered[0][0] == "p_fire"
        assert "[触发器通知]" in delivered[0][1]
        assert cfg.fire_count == 1
    finally:
        loop.close()
