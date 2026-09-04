# @feature: FP-0.2.三 宿主接入 | @ci: python-coverage
"""pipeline_godot_context 插件单元测试。

覆盖：推送接收→前端转发、心跳/清空/离线语义、引用与 user 消息合并（set op）
与双重幂等（同轮 injected_for / 消息已含引用）。
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


# ═══════════════════════════════════════════════════════════
# 管道合并语义（用户裁定 2026-09-03）：插件只有两个功能——引用推送前端、
# 引用与 user 消息合并。不插入独立消息（insert 路径已退役）。
# ═══════════════════════════════════════════════════════════

import pytest  # noqa: E402


def _run_execute(p, state):
    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.state = state
    ctx.config = {}
    return asyncio.run(p.execute(ctx))


@pytest.mark.parametrize(
    "messages",
    [
        # 正常：单条 user
        [{"role": "user", "content": "对这个加个碰撞体", "seq": 3}],
        # 边界：多轮后最后一条 user（前面有 assistant/tool，合并目标必须仍是它）
        [
            {"role": "user", "content": "第一轮", "seq": 0},
            {"role": "assistant", "content": "回复", "seq": 1, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "工具结果", "seq": 2},
            {"role": "assistant", "content": "继续", "seq": 4},
            {"role": "user", "content": "把这个也改一下", "seq": 5},
        ],
    ],
    ids=["single-user", "last-user-after-rounds"],
)
def test_execute_merges_reference_into_last_user_message(messages):
    """选中非空 → 引用块追加进最后一条 user 消息内容（set op 同 seq 替换，无 insert）。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    result = _run_execute(p, {"message_id": "m1", "messages": messages})

    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "set"  # 同槽位替换，非独立插入
    last_user = [m for m in messages if m["role"] == "user"][-1]
    assert op["seq"] == last_user["seq"]
    merged = op["msg"]
    # 性质断言：原文为前缀 + 引用块为后缀，一体消息
    assert merged["content"].startswith(last_user["content"])
    assert '<reference source="godot" scene="res://demo_main.tscn">' in merged["content"]
    assert "- Player (Sprite2D) @ Node2D/Player" in merged["content"]
    assert merged["role"] == "user"
    assert result.state_updates["godot.injected_for"] == "m1"


def test_execute_merge_preserves_message_fields():
    """合并保留原消息除 content 外的字段（metadata 等）。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    messages = [{
        "role": "user",
        "content": "hi",
        "seq": 7,
        "metadata": {"client_message_id": "cm-1"},
    }]
    result = _run_execute(p, {"message_id": "m1", "messages": messages})

    merged = result.state_updates["messages"]["_ops"][0]["msg"]
    assert merged["metadata"] == {"client_message_id": "cm-1"}
    assert merged["seq"] == 7


def test_execute_skips_when_last_user_message_already_has_reference():
    """前端拼接路径（消息已含引用）→ 不二次合并（幂等）。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    messages = [{"role": "user", "content": "改一下\n\n<reference source=\"godot\" scene=\"res://demo_main.tscn\">\n- Player (Sprite2D) @ Node2D/Player\n</reference>", "seq": 1}]
    result = _run_execute(p, {"message_id": "m1", "messages": messages})

    assert result.state_updates == {}


def test_execute_no_user_message_is_noop():
    """无任何 user 消息（如触发器首轮回合）→ 无处合并，不动消息。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    messages = [{"role": "assistant", "content": "欢迎"}]
    result = _run_execute(p, {"message_id": "m1", "messages": messages})

    assert result.state_updates == {}


def test_execute_skips_when_user_message_lacks_seq():
    """消息缺 seq（无法同槽寻址）→ fail-closed 跳过合并，绝不退回独立插入。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    messages = [{"role": "user", "content": "hi"}]
    result = _run_execute(p, {"message_id": "m1", "messages": messages})

    assert result.state_updates == {}


def test_execute_merge_idempotent_both_guards():
    """双幂等分支互不遮蔽：同轮（injected_for）与已含引用（contains）各自跳过。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))

    state = {"message_id": "m1", "messages": [{"role": "user", "content": "hi", "seq": 0}]}
    r1 = _run_execute(p, state)
    # 模拟引擎应用 set op（同 seq 替换）+ 记录 injected_for
    for op in r1.state_updates["messages"]["_ops"]:
        state["messages"] = [
            op["msg"] if m.get("seq") == op["seq"] else m for m in state["messages"]
        ]
    state["godot.injected_for"] = r1.state_updates["godot.injected_for"]

    # 同一轮再执行：injected_for 分支
    assert _run_execute(p, state).state_updates == {}
    # 下一轮（新 message_id）：消息已含引用，contains 分支
    state["message_id"] = "m2"
    assert _run_execute(p, state).state_updates == {}


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
    kw: dict[str, object] = {
        "path": path,
        "method": method,
        "plugin_id": "pipeline_godot_context",
        "raw_body": raw_body,
    }
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


# ═══════════════════════════════════════════════════════════
# 引用清理（dismiss）：清空 items + 抑制同签名心跳 + 主动恢复语义
# ═══════════════════════════════════════════════════════════

def test_dismiss_clears_items_and_broadcasts():
    """dismiss → items 清空、cleared=True、订阅线程收到空 items 事件（卡片消失）。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")
    asyncio.run(p.handle_push(_selection_payload()))

    result = asyncio.run(p.dismiss())

    assert result == {"status": "ok", "cleared": True}
    assert p.snapshot()["items"] == []
    assert emitter.calls[-1][1]["items"] == []


def test_dismiss_suppresses_same_signature_heartbeat():
    """dismiss 后同签名心跳只保活：不恢复 items、不广播、connected 保持。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")
    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.dismiss())
    emits_before = len(emitter.calls)

    asyncio.run(p.handle_push({**_selection_payload(), "type": "heartbeat"}))

    assert p.snapshot()["items"] == []
    assert p.snapshot()["connected"] is True
    assert len(emitter.calls) == emits_before


def test_dismiss_recovers_on_reselect_same_signature():
    """dismiss 后用户在 Godot 重新点选同节点（type=selection 同签名）→ 引用恢复并广播。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()
    p.subscribe("t1")
    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.dismiss())

    asyncio.run(p.handle_push(_selection_payload()))

    assert p.snapshot()["items"][0]["name"] == "Player"
    assert emitter.calls[-1][1]["items"][0]["name"] == "Player"


def test_dismiss_recovers_on_new_selection():
    """dismiss 后改选（新签名）→ 正常恢复。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.dismiss())

    asyncio.run(p.handle_push(_selection_payload(
        signature="Enemy@Node2D/Enemy",
        items=[{"name": "Enemy", "type": "Node2D", "path": "Node2D/Enemy"}],
    )))

    assert p.snapshot()["items"][0]["name"] == "Enemy"


def test_dismiss_empty_selection_is_noop():
    """空引用时 dismiss 幂等 no-op（cleared=False，不广播）。"""
    emitter = FakeEmitter()
    gc_plugin.set_emitter(emitter)
    p = gc_plugin.GodotContextPlugin()

    result = asyncio.run(p.dismiss())

    assert result == {"status": "ok", "cleared": False}
    assert emitter.calls == []


def test_execute_skips_merge_after_dismiss():
    """dismiss 后引用合并停止（清了卡片，消息不再带 <reference>）。"""
    gc_plugin.set_emitter(None)
    p = gc_plugin.GodotContextPlugin()
    asyncio.run(p.handle_push(_selection_payload()))
    asyncio.run(p.dismiss())

    class _Ctx:
        state = {"message_id": "m1", "messages": [{"role": "user", "content": "hi"}]}
        config = {}

    assert asyncio.run(p.execute(_Ctx())).state_updates == {}


def test_http_delete_clears_selection():
    """http 层：DELETE /selection 清除引用，快照立即可见空 items。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    _http("POST", _PUSH_PATH, raw_body=_b64_body(_selection_payload()))
    resp = _http("DELETE", _PUSH_PATH)
    assert resp["data"]["status"] == 200
    assert _decode_http(resp) == {"status": "ok", "cleared": True}

    snap = _decode_http(_http("GET", _PUSH_PATH))
    assert snap["items"] == []

    # 同签名心跳不恢复（抑制生效，经 http 层全链路）
    _http("POST", _PUSH_PATH, raw_body=_b64_body({**_selection_payload(), "type": "heartbeat"}))
    assert _decode_http(_http("GET", _PUSH_PATH))["items"] == []
