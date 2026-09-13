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


def _http(method: str, path: str, raw_body: str = "", query: dict | None = None,
          headers: dict | None = None) -> dict:
    kw: dict[str, object] = {
        "path": path,
        "method": method,
        "plugin_id": "pipeline_godot_context",
        "raw_body": raw_body,
    }
    if query is not None:
        kw["query"] = query
    if headers is not None:
        kw["headers"] = headers
    return asyncio.run(server_mod.http_handle(**kw))


def test_http_push_base64_body_accepted():
    """内核真机形态（raw_body=base64(JSON)）的选中推送被接受，快照可见——推送体按 base64(JSON) 解码，按裸 JSON 文本解析会 400。"""
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


# ── S4：共享密钥门控（GODOT_CONTEXT_SHARED_SECRET 设置后匿名推送 403）──


def test_http_push_with_secret_required_missing_header_forbidden(monkeypatch):
    """设置共享密钥后，无 X-Godot-Secret 头的匿名推送必须 403（选中内容会
    并入下一条用户消息进 LLM 上下文，匿名伪造 = 提示词注入原语）。"""
    monkeypatch.setenv("GODOT_CONTEXT_SHARED_SECRET", "s3cret")
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("POST", _PUSH_PATH, raw_body=_b64_body(_selection_payload()))
    assert resp["data"]["status"] == 403
    assert _decode_http(_http("GET", _PUSH_PATH))["items"] == [], "被拒推送不得进快照"


def test_http_push_with_secret_wrong_and_correct_header(monkeypatch):
    monkeypatch.setenv("GODOT_CONTEXT_SHARED_SECRET", "s3cret")
    server_mod.set_emitter(None)
    _fresh_instance()

    wrong = _http(
        "POST", _PUSH_PATH,
        raw_body=_b64_body(_selection_payload()),
        headers={"X-Godot-Secret": "nope"},
    )
    assert wrong["data"]["status"] == 403

    ok = _http(
        "POST", _PUSH_PATH,
        raw_body=_b64_body(_selection_payload()),
        headers={"x-godot-secret": "s3cret"},
    )
    assert ok["data"]["status"] == 200
    assert _decode_http(_http("GET", _PUSH_PATH))["items"][0]["name"] == "Player"


def test_http_push_without_secret_env_unchanged(monkeypatch):
    """未设置环境变量 = 既有豁免形态（ADR 2026-09-11），行为零变化。"""
    monkeypatch.delenv("GODOT_CONTEXT_SHARED_SECRET", raising=False)
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("POST", _PUSH_PATH, raw_body=_b64_body(_selection_payload()))
    assert resp["data"]["status"] == 200


# ═══════════════════════════════════════════════════════════
# 服务端适配层（server.py）缺口补测：on_load/on_unload 生命周期、
# execute 工具两种返回形态、/preview 代理（aiohttp 第三方 SDK 替身）、
# /subscribe 非法体回退、未知路由 404。
# ═══════════════════════════════════════════════════════════

import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# 1x1 PNG 魔数 + 载荷（_fetch_preview 按前 8 字节判定 PNG）
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"


class _FakeAiohttp:
    """aiohttp 最小替身（第三方网络 SDK，mock 边界）：ClientTimeout/ClientSession。

    outcome: ("resp", status, data) 按预设响应；("exc", exc) 在发起请求时抛出。
    """

    class ClientTimeout:
        def __init__(self, **kw):
            self.kw = kw

    def __init__(self, outcome):
        self._outcome = outcome
        self.last_request: tuple[str, dict] | None = None

    def ClientSession(self, **kw):  # noqa: N802 —— 符号名对齐 aiohttp.ClientSession
        mod = self

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, params=None):
                mod.last_request = (url, dict(params or {}))
                if mod._outcome[0] == "exc":
                    raise mod._outcome[1]
                _, resp_status, resp_data = mod._outcome

                class _Resp:
                    def __init__(self):
                        self.status = resp_status

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False

                    async def read(self):
                        return resp_data

                return _Resp()

        return _Session()


def _install_fake_aiohttp(monkeypatch, *, status: int = 200, data: bytes = b"", exc: Exception | None = None):
    fake = _FakeAiohttp(("exc", exc) if exc is not None else ("resp", status, data))
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return fake


# ── 生命周期：on_load 构建单例 + emitter 注入，on_unload 重置 ──


def test_on_load_builds_singleton_without_frontend_capability():
    """单测环境无内核 frontend capability → from_plugin 降级 None（告警路径），
    单例仍完成懒构建并缓存。"""
    gc_plugin.set_emitter(None)
    server_mod._instance = None

    asyncio.run(server_mod._on_load({}))

    inst = server_mod._instance
    # server.py 自带一份 plugin 模块副本（裸名串扰隔离），用其自己的类断言
    assert isinstance(inst, server_mod.GodotContextPlugin)
    assert server_mod.get_instance() is inst  # 同一单例被缓存复用


def test_on_load_sets_emitter_when_frontend_capability_available(monkeypatch):
    """frontend capability 可用 → on_load 把 emitter 注入插件，订阅广播经它转发。"""
    emitter = FakeEmitter()

    class _FakeFrontendEmitter:
        @classmethod
        def from_plugin(cls, _plugin):
            return emitter

    monkeypatch.setattr(server_mod, "FrontendEmitter", _FakeFrontendEmitter)
    server_mod._instance = None
    gc_plugin.set_emitter(None)

    asyncio.run(server_mod._on_load({}))

    inst = server_mod.get_instance()
    inst.subscribe("tOnLoad")
    asyncio.run(inst.handle_push(_selection_payload()))
    assert emitter.calls[-1][0] == "godot_selection_changed"
    assert emitter.calls[-1][1]["thread_id"] == "tOnLoad"


def test_on_unload_resets_singleton():
    """on_unload 清空单例缓存：下次 get_instance 重建新实例。"""
    inst = server_mod.get_instance()

    asyncio.run(server_mod._on_unload({}))

    assert server_mod._instance is None
    assert server_mod.get_instance() is not inst


# ── execute 工具：管道注入入口的返回形态适配 ──


def test_execute_tool_injects_reference_via_plugin_result():
    """真实插件 PluginResult → {state_updates} 数据面；无跳过标志不得伪造。"""
    server_mod.set_emitter(None)
    p = _fresh_instance()
    asyncio.run(p.handle_push(_selection_payload()))

    out = asyncio.run(server_mod.execute(
        state={"message_id": "m1", "messages": [{"role": "user", "content": "hi", "seq": 1}]},
        config={},
    ))

    assert out["state_updates"]["godot.injected_for"] == "m1"
    assert "skip_remaining" not in out


def test_execute_tool_no_selection_returns_empty_updates():
    """无选中 → 空 state_updates；config=None 走缺省 {}。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    out = asyncio.run(server_mod.execute(
        state={"message_id": "m1", "messages": [{"role": "user", "content": "hi", "seq": 1}]},
        config=None,
    ))

    assert out == {"state_updates": {}}


def test_execute_tool_dict_result_passthrough():
    """插件直接返回 dict（已是数据面形态）→ 原样透传不二次包装。"""
    captured: dict = {}

    class _DictPlugin:
        async def execute(self, ctx):
            captured["state"] = ctx.state
            return {"already": "shaped"}

    server_mod._instance = _DictPlugin()

    out = asyncio.run(server_mod.execute(state={"message_id": "mx"}, config={}))

    assert out == {"already": "shaped"}
    assert captured["state"]["message_id"] == "mx"  # state 经 create_initial_state 归一并透传


def test_execute_tool_skip_remaining_flag_propagates():
    """PluginResult.skip_remaining=True → 数据面携带 skip_remaining 标志。"""

    class _SkipPlugin:
        async def execute(self, ctx):
            return SimpleNamespace(state_updates={"k": "v"}, skip_remaining=True)

    server_mod._instance = _SkipPlugin()

    out = asyncio.run(server_mod.execute(state={}, config={}))

    assert out == {"state_updates": {"k": "v"}, "skip_remaining": True}


# ── /preview 代理：_fetch_preview 结果形态 + 路由信封 ──


@pytest.mark.parametrize(
    ("status", "data", "expected"),
    [
        (200, _PNG_BYTES, _PNG_BYTES),  # 200 + PNG 魔数 → 返回字节
        (200, b"junkjunk!", None),  # 200 但非 PNG → None
        (503, _PNG_BYTES, None),  # 非 200 → None
    ],
    ids=["png-ok", "non-png", "non-200"],
)
def test_fetch_preview_returns_png_only(monkeypatch, status, data, expected):
    fake = _install_fake_aiohttp(monkeypatch, status=status, data=data)

    out = asyncio.run(server_mod._fetch_preview(3))

    assert out == expected
    # 请求形态：代理到 Godot 宿主端点 /selection/preview，index 透传
    url, params = fake.last_request
    assert url.endswith("/selection/preview")
    assert params == {"index": 3}


def test_fetch_preview_connection_error_returns_none(monkeypatch):
    """建连失败（网络异常）→ None，不向上抛。"""
    _install_fake_aiohttp(monkeypatch, exc=OSError("connect refused"))

    assert asyncio.run(server_mod._fetch_preview(0)) is None


_PREVIEW_PATH = "/ext/pipeline_godot_context/preview"


def test_http_preview_proxies_png(monkeypatch):
    """GET /preview 命中 PNG → ToolExecutionResult 信封（200 + base64 body）。"""
    fake = _install_fake_aiohttp(monkeypatch, status=200, data=_PNG_BYTES)
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("GET", _PREVIEW_PATH, query={"index": "2"})

    assert resp["success"] is True
    assert resp["data"]["status"] == 200
    assert resp["data"]["headers"]["Content-Type"] == "image/png"
    assert resp["data"]["body_encoding"] == "base64"
    assert base64.b64decode(resp["data"]["body"]) == _PNG_BYTES
    assert fake.last_request[1] == {"index": 2}  # query 字符串 → int


def test_http_preview_unavailable_returns_502(monkeypatch):
    """预览代理失败 → 502 + error 载荷（域内错误仍走信封）。"""
    _install_fake_aiohttp(monkeypatch, exc=OSError("godot down"))
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("GET", _PREVIEW_PATH, query={"index": "1"})

    assert resp["data"]["status"] == 502
    assert _decode_http(resp) == {"error": "preview unavailable"}


@pytest.mark.parametrize("query", [{"index": "abc"}, None], ids=["garbage-index", "no-query"])
def test_http_preview_bad_index_falls_back_to_zero(monkeypatch, query):
    """非法/缺失 index → 回退 0 继续代理（不 4xx）。"""
    fake = _install_fake_aiohttp(monkeypatch, status=200, data=_PNG_BYTES)
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http("GET", _PREVIEW_PATH, query=query)

    assert resp["success"] is True
    assert fake.last_request[1] == {"index": 0}


# ── /subscribe 非法体回退 + 未知路由 ──


@pytest.mark.parametrize(
    "raw_body",
    ["not-json", _b64_body([1, 2])],
    ids=["invalid-json", "non-object-json"],
)
def test_http_subscribe_invalid_body_yields_empty_thread(raw_body):
    """订阅体解码失败 → 按空体回退（不 4xx）：空 thread_id 不新增订阅。"""
    server_mod.set_emitter(None)
    p = _fresh_instance()
    p.subscribe("tKeep")

    resp = _http("POST", _SUB_PATH, raw_body=raw_body)

    assert resp["data"]["status"] == 200
    assert _decode_http(resp) == {"status": "ok", "threads": 1}


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/ext/pipeline_godot_context/whatever"), ("PATCH", _PUSH_PATH)],
    ids=["unknown-path", "unknown-method"],
)
def test_http_unknown_route_404(method, path):
    """未匹配路由 → 404，error 携带 method+path 便于定位。"""
    server_mod.set_emitter(None)
    _fresh_instance()

    resp = _http(method, path)

    assert resp["data"]["status"] == 404
    error = _decode_http(resp)["error"]
    assert method in error
    assert path in error
