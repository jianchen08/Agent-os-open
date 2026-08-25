# @feature: FP-0.2.三 宿主接入 | @ci: none-local（不在任何 CI 车道：python-coverage 的 BASE_TEST_PATHS 未收集本文件）
"""pipeline_godot_context 插件单元测试。

覆盖：推送接收→前端转发、心跳/清空/离线语义、管道注入 op 构造与幂等去重。
"""

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "godot_context")

import asyncio  # noqa: E402

import plugin as gc_plugin  # noqa: E402


class FakeEmitter:
    """记录 emit 调用的假 FrontendEmitter。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def emit(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


def _selection_payload(signature: str = "Player@Node2D/Player", items: list | None = None) -> dict:
    return {
        "type": "selection",
        "engine": "godot",
        "engine_version": "4.7.1",
        "project": "AgentOS Demo",
        "scene": {"name": "DemoMain", "path": "res://demo_main.tscn", "root": "Node2D"},
        "items": items if items is not None else [
            {"name": "Player", "type": "Sprite2D", "path": "Node2D/Player", "preview_kind": "texture"},
        ],
        "signature": signature,
        "ts": 1,
    }


def test_push_selection_emits_to_subscribed_threads():
    """选中推送（签名变化）→ 对订阅线程 emit godot_selection_changed。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")

    asyncio.run(p.handle_push(_selection_payload()))

    assert len(emitter.calls) == 1
    event, payload = emitter.calls[0]
    assert event == "godot_selection_changed"
    assert payload["thread_id"] == "t1"
    assert payload["items"][0]["name"] == "Player"
    assert payload["connected"] is True


def test_heartbeat_same_signature_does_not_emit():
    """心跳且签名未变：只刷新在线时间戳，不重复 emit。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")

    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.handle_push({**_selection_payload(), "type": "heartbeat"}))

    assert len(emitter.calls) == 1


def test_clear_selection_emits_empty_items():
    """取消选中（签名变空）→ emit 空 items（前端卡片消失）。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")

    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.handle_push(_selection_payload(signature="", items=[])))

    assert emitter.calls[-1][1]["items"] == []
    assert p.snapshot()["items"] == []


def test_offline_marks_disconnected_and_emits():
    """offline 推送 → 快照离线并 emit（connected=false）。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")

    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.handle_push({"type": "offline", "items": [], "signature": "", "scene": {}}))

    assert emitter.calls[-1][1]["connected"] is False
    assert p.snapshot()["connected"] is False


def test_snapshot_heartbeat_timeout_marks_offline():
    """心跳超时（>15s 无推送）→ 快照视为离线。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()

    asyncio.run(p.handle_push(_selection_payload()))
    p._last_push_ms -= gc_plugin.GodotContextPlugin.HEARTBEAT_STALE_MS + 1

    assert p.snapshot()["connected"] is False


def test_execute_inserts_reference_after_user_message():
    """新用户消息首轮且选中非空 → 在消息末尾（紧随 user）插入 <reference> 消息。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    class _Ctx:
        state = {
            "message_id": "m1",
            "messages": [{"role": "user", "content": "对这个加个碰撞体"}],
        }
        config = {}

    result = asyncio.run(p.execute(_Ctx()))

    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1
    assert ops[0]["op"] == "insert"
    assert ops[0]["at"] == 1  # 紧随最后一条 user 消息
    msg = ops[0]["msg"]
    assert msg["role"] == "user"
    assert '<reference source="godot" scene="res://demo_main.tscn">' in msg["content"]
    assert "- Player (Sprite2D) @ Node2D/Player" in msg["content"]
    assert result.state_updates["godot.injected_for"] == "m1"


def test_execute_dedup_on_same_message():
    """同一条消息（引擎 merge 后）第二轮不再注入。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    state = {
        "message_id": "m1",
        "messages": [{"role": "user", "content": "hi"}],
    }

    class _Ctx1:
        pass

    ctx1 = _Ctx1()
    ctx1.state = state
    ctx1.config = {}
    r1 = asyncio.run(p.execute(ctx1))
    state.update(r1.state_updates)  # 模拟引擎 merge state_updates

    class _Ctx2:
        pass

    ctx2 = _Ctx2()
    ctx2.state = state
    ctx2.config = {}
    r2 = asyncio.run(p.execute(ctx2))

    assert r2.state_updates == {}


def test_execute_skips_when_no_selection_or_offline():
    """无选中 / Godot 离线 → 不注入。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()

    class _Ctx:
        state = {"message_id": "m1", "messages": [{"role": "user", "content": "hi"}]}
        config = {}

    # 离线（从未收到推送）
    assert asyncio.run(p.execute(_Ctx())).state_updates == {}

    # 在线但选中为空
    asyncio.run(p.handle_push(_selection_payload(signature="", items=[])))
    assert asyncio.run(p.execute(_Ctx())).state_updates == {}


def test_no_emit_without_subscribers():
    """未订阅线程时推送只更新快照，不 emit、不报错。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()

    asyncio.run(p.handle_push(_selection_payload()))

    assert emitter.calls == []
    assert p.snapshot()["connected"] is True


# ═══════════════════════════════════════════════════════════
# http.handle 层回归：内核 dispatcher 恒把 raw_body base64 编码后
# 传入插件（kernel/crates/api/src/http_dispatcher.rs dispatch_http），
# 服务端解码须兼容 base64（真机形态）与明文。
# ═══════════════════════════════════════════════════════════

import base64  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

_PUSH_PATH = "/ext/pipeline_godot_context/selection"
_SUB_PATH = "/ext/pipeline_godot_context/subscribe"


def _load_server_module():
    # 裸名 server 会与其他插件测试的 sys.modules 串扰（如 security_check），
    # 以独立模块名从文件加载；add_plugin_dir 保证 server.py 内 `from plugin import`
    # 解析到本插件目录。
    add_plugin_dir("input", "godot_context")
    server_py = Path(gc_plugin.__file__).parent / "server.py"
    spec = importlib.util.spec_from_file_location("godot_context_server", server_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server_mod = _load_server_module()


def _fresh_instance():
    """重置 server 单例（插件内快照状态测试间隔离）。"""
    server_mod._instance = None
    return server_mod.get_instance()


def _b64_body(payload) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_http(resp: dict) -> dict:
    return json.loads(base64.b64decode(resp["data"]["body"]).decode("utf-8"))


def _http(method: str, path: str, raw_body: str = "", query: dict | None = None) -> dict:
    kw = {"path": path, "method": method, "plugin_id": "pipeline_godot_context", "raw_body": raw_body}
    if query is not None:
        kw["query"] = query
    return asyncio.run(server_mod.http_handle(**kw))


def test_http_push_base64_body_accepted():
    """内核真机形态（raw_body=base64(JSON)）的选中推送被接受，快照可见——回归：曾直接 json.loads(base64 串) 恒 400。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("POST", _PUSH_PATH, raw_body=_b64_body(_selection_payload()))

    assert resp["data"]["status"] == 200
    assert _decode_http(resp) == {"status": "ok"}
    snap = _decode_http(_http("GET", _PUSH_PATH))
    assert snap["connected"] is True
    assert snap["items"][0]["name"] == "Player"


def test_http_push_plaintext_body_tolerated():
    """明文 JSON 体同样被接受（解码兼容两种形态）。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("POST", _PUSH_PATH, raw_body=json.dumps(_selection_payload()))

    assert resp["data"]["status"] == 200
    assert _decode_http(_http("GET", _PUSH_PATH))["items"][0]["name"] == "Player"


def test_http_push_invalid_or_non_object_body_400():
    """非法 JSON / 非对象 JSON → 400（fail-fast，不静默吞）。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    assert _http("POST", _PUSH_PATH, raw_body="not-json")["data"]["status"] == 400
    assert _http("POST", _PUSH_PATH, raw_body=_b64_body([1, 2]))["data"]["status"] == 400


def test_http_subscribe_base64_then_push_broadcasts():
    """订阅（base64 体）→ 推送 → 订阅线程收到 emit（http 层端到端）。"""
    emitter = FakeEmitter()
    server_mod.set_emitter(emitter)
    _fresh_instance()

    r1 = _http("POST", _SUB_PATH, raw_body=_b64_body({"thread_id": "t9"}))
    assert _decode_http(r1)["threads"] == 1

    r2 = _http("POST", _PUSH_PATH, raw_body=_b64_body(_selection_payload()))
    assert _decode_http(r2) == {"status": "ok"}
    assert emitter.calls[-1][0] == "godot_selection_changed"
    assert emitter.calls[-1][1]["thread_id"] == "t9"
