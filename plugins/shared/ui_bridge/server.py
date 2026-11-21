#!/usr/bin/env python3
"""ui_bridge 插件服务端——侧边栏 UI 对象感知面 + 命令闭环（ADR 2026-11-18-sidebar-ui-object-model）。

三面在本进程内闭环：

- 状态面：前端 POST /ext/ui_bridge/state 上报快照，按 ``client_id`` 缓存槽
  各存一份；freshness = 插件收到时刻起 30s（``_time.monotonic`` 单调钟，
  不信任 client ts——client ts 仅存档）。respawn 缓存即失，靠 ≤30s 心跳自愈。
- 工具面：``ui_describe``（读快照）/ ``ui_act``（命令闭环，P1）读同一份进程内
  缓存；output schema 由 plugin.json 声明，tool_core fail-closed 消费。
- 命令面（P1）：``ui_act`` 词表封闭 fail-closed → 经 ``set_ui_command_notifier``
  seam 外发 ``ui_command``（生产接线 = _EventBusCommandNotifier 经 event-bus
  capability emit）→ 前端执行动作 → REST ``POST /ext/ui_bridge/command_result``
  回执 → ``ui_act`` 短等 2s 如实返回 applied/timeout。状态机：created →（校验
  失败）rejected*；created → emitted → applied* / timeout*（* 终态；回执
  rejected 不终结等待——多端取首个 applied 的前提）。

GET/POST 端点逻辑自含（``create_app()`` 提供 FastAPI app，不依赖内核进程，
TestClient 直测）；sidecar 运行面走 ``http.handle``（handler_capability 指向）
与注册工具，三者共用同一模块级缓存与等待登记。

需求真源（只读）：docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.1-§2.4。

（刻意不用 ``from __future__ import annotations``：PEP 563 字符串注解 + create_app
内延迟导入 fastapi 的组合会让 FastAPI 依赖解析认不出 ``Request`` 注解——路由把
request 当 query 参数，请求体直落 422。本模块 py>=3.11 原生类型语法自足。）
"""
import asyncio
import logging
import threading
import time as _time  # 模块命名空间时刻源：测试经 setattr(module, "_time", fake) 注入假钟
import uuid
from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path（_root_api 可达）

from _root_api import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response_body,
    ok as _ok,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("ui_bridge")

# ── 冻结契约常量（§2.1/§2.2 逐字）─────────────────────────────────────
FRESH_WINDOW_S = 30.0
_MAX_CLIENT_ID_LEN = 64
_MAX_ITEMS = 100
_MAX_SESSIONS = 50
_DEFAULT_REGION = "sidebar"
_ACTIONS_VOCAB = ["navigate", "set_collapsed", "open_page", "highlight"]

# ui_describe 工具 schema（与 plugin.json capabilities.tools[].ui_describe 逐字一致，
# G2 声明↔实现比对，漂移即剔除；单源 = 冻结件 §2.2）
_UI_DESCRIBE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "region": {
            "type": "string",
            "enum": ["sidebar"],
            "default": "sidebar",
            "description": "界面对象区域，默认 sidebar（P0 仅侧边栏）",
        },
        "client_id": {
            "type": "string",
            "description": "前端实例标识（缺省取最近上报端）",
        },
        "all": {
            "type": "boolean",
            "description": "true 时返回全部端快照列表（按 received_at 降序）",
        },
    },
}
_UI_DESCRIBE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "region"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ok", "stale", "no_snapshot"],
        },
        "region": {"type": "string"},
        "snapshot": {
            "type": "object",
            "description": "快照体：state{collapsed,width_ratio,active_view,unread_count}"
            " + items[] + sessions[] + actions[]",
        },
        "error": {"type": "string"},
    },
}
_UI_DESCRIBE_RENDER = {"card": "generic"}

# ui_act 工具 schema（与 plugin.json capabilities.tools[].ui_act 逐字一致，
# G2 声明↔实现比对，漂移即剔除；单源 = 冻结件 §2.4）
_UI_ACT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "region": {
            "type": "string",
            "default": "sidebar",
            "description": "界面对象区域，默认 sidebar（P1 仅侧边栏）",
        },
        "action": {
            "type": "string",
            "enum": ["navigate", "set_collapsed", "open_page", "highlight"],
            "description": "操作词表（封闭四值）",
        },
        "params": {
            "type": "object",
            "description": "动作参数：navigate{target} / set_collapsed{collapsed} / "
            "open_page{page_id} / highlight{item_id, duration_ms?}",
        },
    },
}
_UI_ACT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["applied", "rejected", "timeout"],
        },
        "reason": {
            "type": "string",
            "enum": ["unknown_action", "unknown_target", "no_fresh_snapshot", "invalid_params"],
        },
        "snapshot_after": {
            "description": "applied 时附最新缓存快照（无快照为 null）",
            "anyOf": [{"type": "object"}, {"type": "null"}],
        },
    },
}
_UI_ACT_RENDER = {"card": "generic"}

# ── 进程内快照缓存（per-client_id 槽；respawn 即失，靠 ≤30s 心跳自愈）──
_SNAPSHOTS: dict[str, dict[str, Any]] = {}

# ── 命令闭环状态（P1）：等待登记 + notifier seam ─────────────────────
_COMMAND_WAIT_S = 2.0
_COMMAND_POLL_S = 0.05  # 等待窗轮询步进（假钟下 sleep 步进即推进等待窗）
_HIGHLIGHT_DURATION_MAX_MS = 5000

_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, "PendingCommand"] = {}

_NOTIFIER_LOCK = threading.Lock()
_ui_command_notifier: Callable[[dict[str, Any]], Any] | None = None


class PendingCommand:
    """ui_act 等待者登记（布防先于发令：回执先于等待到达不失效）。"""

    __slots__ = ("applied", "cond")

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.applied = False


def set_ui_command_notifier(fn: Callable[[dict[str, Any]], Any] | None) -> None:
    """注册 ui_command 外发 seam（生产 = _EventBusCommandNotifier；测试注入捕获器）。

    notifier 以命令载荷 dict（flat，含冻结四字段 {command_id, region, action,
    params}）被调用恰一次/命令。缺省（未注册）外发面降级：不抛异常，命令按
    无回执路径走完等待窗口落 timeout。
    """
    global _ui_command_notifier
    with _NOTIFIER_LOCK:
        _ui_command_notifier = fn


def _emit_ui_command(payload: dict[str, Any]) -> bool:
    """外发一条 ui_command（seam 调用点）。注册面缺失时降级返回 False。"""
    with _NOTIFIER_LOCK:
        fn = _ui_command_notifier
    if fn is None:
        logger.warning(
            "[ui_bridge] ui_command notifier not registered; degrade %s to timeout path",
            payload.get("command_id"),
        )
        return False
    fn(payload)
    return True


def _resolve_command_applied(command_id: str) -> bool:
    """applied 回执唤醒等待者（取首个 applied：登记即摘除，其余回执视为未知）。"""
    with _PENDING_LOCK:
        pending = _PENDING.pop(command_id, None)
    if pending is None:
        return False
    with pending.cond:
        pending.applied = True
        pending.cond.notify_all()
    return True


def handle_command_result(body: Any) -> tuple[dict[str, Any], int]:
    """POST /ext/ui_bridge/command_result 路由核（FastAPI app 与 http.handle 共用）。

    已知 command_id 受理（applied 唤醒等待者；rejected 不终结等待——emitted 后
    终态仅 applied/timeout，§2.4 等待窗口行 + §四 状态机逐字）；未知 → 202
    忽略（幂等，零状态污染）。
    """
    if not isinstance(body, dict):
        return {"error": "request body must be a JSON object"}, 400
    command_id = body.get("command_id")
    if not isinstance(command_id, str) or not command_id:
        return {"error": "command_id must be a non-empty string"}, 400
    status = body.get("status")
    if status not in ("applied", "rejected"):
        return {"error": "status must be 'applied' or 'rejected'"}, 400
    with _PENDING_LOCK:
        known = command_id in _PENDING
    if not known:
        return {"ignored": True}, 202
    if status == "applied":
        _resolve_command_applied(command_id)
    return {"ok": True}, 200


def _validate_report(body: Any) -> str | None:
    """POST 快照 fail-closed 校验（§2.1 + §四 边界表）：违规返回错误文案，合法返回 None。"""
    if not isinstance(body, dict):
        return "request body must be a JSON object"
    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        return "client_id must be a non-empty string"
    if len(client_id) > _MAX_CLIENT_ID_LEN:
        return f"client_id must be at most {_MAX_CLIENT_ID_LEN} characters"
    if body.get("region") != _DEFAULT_REGION:
        return f"region must be {_DEFAULT_REGION!r}"
    state = body.get("state")
    if not isinstance(state, dict):
        return "state must be an object"
    unread = state.get("unread_count")
    if unread is not None and (isinstance(unread, bool) or not isinstance(unread, int) or unread < 0):
        return "state.unread_count must be an int >= 0"
    items = body.get("items")
    if not isinstance(items, list):
        return "items must be a list"
    if len(items) > _MAX_ITEMS:
        return f"items must have at most {_MAX_ITEMS} entries"
    sessions = body.get("sessions")
    if not isinstance(sessions, list):
        return "sessions must be a list"
    if len(sessions) > _MAX_SESSIONS:
        return f"sessions must have at most {_MAX_SESSIONS} entries"
    if not isinstance(body.get("actions"), list):
        return "actions must be a list"
    return None


def _store_snapshot(body: dict[str, Any]) -> None:
    """入库：received_at = 插件收到时刻（单调钟）；同 client 连发后者替换。"""
    client_id = body["client_id"]
    _SNAPSHOTS[client_id] = {
        "region": body["region"],
        "client_id": client_id,
        "received_at": float(_time.monotonic()),
        "snapshot": {key: body[key] for key in ("state", "items", "sessions", "actions")},
    }


def _is_fresh(entry: dict[str, Any]) -> bool:
    """fresh = 收到后 ≤30s（状态机：fresh → stale → 同 client 再报替换重来）。"""
    return (_time.monotonic() - entry["received_at"]) <= FRESH_WINDOW_S


def _pick_entry(region: str, client_id: str | None) -> dict[str, Any] | None:
    """取缓存槽：client_id 定向命中；缺省取最近 received 的单端。"""
    entries = [e for e in _SNAPSHOTS.values() if e["region"] == region]
    if not entries:
        return None
    if client_id is not None:
        for entry in entries:
            if entry["client_id"] == client_id:
                return entry
        return None
    return max(entries, key=lambda e: e["received_at"])


def _null_snapshot(region: str) -> dict[str, Any]:
    """无快照响应面：null 三件套 + fresh=False（§2.1：不是 404）。"""
    return {
        "region": region,
        "client_id": None,
        "received_at": None,
        "fresh": False,
        "snapshot": None,
    }


def query_state(
    region: str = _DEFAULT_REGION, client_id: str | None = None, all_clients: bool = False
) -> dict[str, Any]:
    """GET /ext/ui_bridge/state 的路由核（FastAPI app 与 http.handle 共用）。"""
    if all_clients:
        entries = sorted(
            (e for e in _SNAPSHOTS.values() if e["region"] == region),
            key=lambda e: e["received_at"],
            reverse=True,
        )
        return {
            "region": region,
            "clients": [
                {
                    "client_id": e["client_id"],
                    "received_at": e["received_at"],
                    "fresh": _is_fresh(e),
                    "snapshot": e["snapshot"],
                }
                for e in entries
            ],
        }
    entry = _pick_entry(region, client_id)
    if entry is None:
        return _null_snapshot(region)
    return {
        "region": region,
        "client_id": entry["client_id"],
        "received_at": entry["received_at"],
        "fresh": _is_fresh(entry),
        "snapshot": entry["snapshot"],
    }


def ui_describe(params: dict[str, Any]) -> dict[str, Any]:
    """ui_describe 工具入口（sidecar 主循环与测试共用同一入口，§2.2）。

    读快照缓存返回界面对象：无快照 → ``no_snapshot``；新鲜 → ``ok``；
    过期 → ``stale``（快照仍完整返回）。``all=true`` 附全部端列表
    （按 received_at 降序，语义同 GET ``?all=1``）。
    """
    region = _DEFAULT_REGION
    client_id: str | None = None
    want_all = False
    if isinstance(params, dict):
        requested = params.get("region")
        if isinstance(requested, str) and requested:
            region = requested
        raw_client = params.get("client_id")
        if isinstance(raw_client, str) and raw_client:
            client_id = raw_client
        want_all = bool(params.get("all"))

    if want_all:
        listing = query_state(region=region, all_clients=True)
        clients = listing["clients"]
        if not clients:
            return {
                "status": "no_snapshot",
                "region": region,
                "error": f"no snapshot reported for region {region}",
            }
        latest = clients[0]
        return {
            "status": "ok" if latest["fresh"] else "stale",
            "region": region,
            "snapshot": latest["snapshot"],
            "clients": clients,
        }

    entry = _pick_entry(region, client_id)
    if entry is None:
        detail = (
            f"no snapshot for client_id {client_id}" if client_id is not None else f"no snapshot for region {region}"
        )
        return {"status": "no_snapshot", "region": region, "error": detail}
    return {
        "status": "ok" if _is_fresh(entry) else "stale",
        "region": region,
        "snapshot": entry["snapshot"],
    }


def _is_number(value: Any) -> bool:
    """数值判定（bool 是 int 子类，显式排除）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_action_params(action: str, params: dict[str, Any]) -> str | None:
    """per-action params 形状校验（§2.4 必填面）：违规返回 invalid_params。"""
    if action == "navigate":
        target = params.get("target")
        if not isinstance(target, str) or not target:
            return "invalid_params"
        return None
    if action == "set_collapsed":
        if not isinstance(params.get("collapsed"), bool):
            return "invalid_params"
        return None
    if action == "open_page":
        page_id = params.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            return "invalid_params"
        return None
    # highlight
    item_id = params.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return "invalid_params"
    duration = params.get("duration_ms")
    if duration is not None and (not _is_number(duration) or duration > _HIGHLIGHT_DURATION_MAX_MS):
        return "invalid_params"
    return None


def _target_known(action: str, snapshot: dict[str, Any], target: str) -> bool:
    """目标存在性（§2.4「目标不在最新 items/sessions」）：navigate 查 items∪sessions，
    open_page 查 items（页面项）。"""
    known = {item.get("id") for item in snapshot.get("items", []) if isinstance(item, dict)}
    if action == "navigate":
        known |= {s.get("id") for s in snapshot.get("sessions", []) if isinstance(s, dict)}
    return target in known


def ui_act(params: dict[str, Any]) -> dict[str, Any]:
    """ui_act 工具入口（sidecar 主循环与测试共用，§2.4 命令闭环）。

    fail-closed 校验链（同步拒绝路径，不发令、拒绝 id 不可被回执终结）：
    action ∉ 词表 → unknown_action；params 缺必填/形状错 → invalid_params；
    navigate/open_page 无 fresh 快照 → no_fresh_snapshot；目标不在快照 →
    unknown_target。合法命令：布防等待登记 → notifier 外发 ``ui_command``
    （载荷四字段）→ 等待 2s（``_time`` 注入钟）→ applied（附最新缓存快照）/
    timeout。多端回执取首个 applied，rejected 回执不终结等待。
    """
    if not isinstance(params, dict):
        params = {}
    region = params.get("region")
    if not isinstance(region, str) or not region:
        region = _DEFAULT_REGION
    action = params.get("action")
    if not isinstance(action, str) or action not in _ACTIONS_VOCAB:
        return {"status": "rejected", "reason": "unknown_action"}
    raw_command_params = params.get("params")
    command_params: dict[str, Any] = raw_command_params if isinstance(raw_command_params, dict) else {}
    invalid = _validate_action_params(action, command_params)
    if invalid is not None:
        return {"status": "rejected", "reason": invalid}

    if action in ("navigate", "open_page"):
        entry = _pick_entry(region, None)
        if entry is None or not _is_fresh(entry):
            return {"status": "rejected", "reason": "no_fresh_snapshot"}
        target = command_params.get("target" if action == "navigate" else "page_id")
        assert isinstance(target, str)  # _validate_action_params 已收窄类型
        if not _target_known(action, entry["snapshot"], target):
            return {"status": "rejected", "reason": "unknown_target"}

    command_id = f"ui-cmd-{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "command_id": command_id,
        "region": region,
        "action": action,
        "params": command_params,
    }
    pending = PendingCommand()
    with _PENDING_LOCK:
        _PENDING[command_id] = pending  # 先布防：notifier 发令时等待者必已登记
    try:
        _emit_ui_command(payload)
        deadline = float(_time.monotonic()) + _COMMAND_WAIT_S
        while True:
            with pending.cond:
                if pending.applied:
                    latest = _pick_entry(region, None)
                    return {
                        "status": "applied",
                        "snapshot_after": latest["snapshot"] if latest is not None else None,
                    }
                remaining = deadline - float(_time.monotonic())
                if remaining <= 0:
                    break
            _time.sleep(min(remaining, _COMMAND_POLL_S))
        return {"status": "timeout"}
    finally:
        with _PENDING_LOCK:
            _PENDING.pop(command_id, None)


def create_app() -> Any:
    """自含 FastAPI app（POST/GET /ext/ui_bridge/state），不依赖内核进程。

    fastapi 仅供本测试面入口（TestClient 直测感知闭环）；sidecar 运行面走
    ``plugin.run()`` 主循环不经此处，故延迟导入、不进 sidecar 依赖面。
    """
    from fastapi import FastAPI, Query, Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    app = FastAPI(title="ui_bridge")

    @app.post("/ext/ui_bridge/state")
    async def report_state(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — 非 JSON 体归一为校验失败（fail-closed 400）
            body = None
        error = _validate_report(body)
        if error is not None:
            return JSONResponse({"error": error}, status_code=400)
        _store_snapshot(body)
        return JSONResponse({"ok": True})

    @app.get("/ext/ui_bridge/state")
    async def read_state(
        region: str = _DEFAULT_REGION,
        client_id: str | None = None,
        all: str | None = Query(default=None),  # noqa: A002 — 查询参数名冻结为 all
    ) -> JSONResponse:
        return JSONResponse(
            query_state(region=region, client_id=client_id, all_clients=all is not None)
        )

    @app.post("/ext/ui_bridge/command_result")
    async def command_result(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — 非 JSON 体归一为校验失败（fail-closed 400）
            body = None
        payload, status = handle_command_result(body)
        return JSONResponse(payload, status_code=status)

    return app


# ── sidecar 运行面注册（plugin.run() 主循环消费）──────────────────────


async def _ui_describe_tool(**kwargs: Any) -> dict[str, Any]:
    """SDK 分发入口（handler 以 kwargs 收参数）：汇回模块级共用入口 ui_describe。"""
    return ui_describe(kwargs)


plugin.register_tool(
    "ui_describe",
    schema=_UI_DESCRIBE_INPUT_SCHEMA,
    handler=_ui_describe_tool,
    description=(
        "读取前端界面对象快照（P0：侧边栏）。返回 status=ok|stale|no_snapshot 与快照体"
        "（state 视图态 4 字段 / items 侧栏项 / sessions 会话列表 / actions 支持的操作词表）；"
        "freshness 由插件收到时刻 30s 窗口判定，过期如实 stale。可选 client_id 定向某前端实例，"
        "all=true 返回全部端。"
    ),
    output_schema=_UI_DESCRIBE_OUTPUT_SCHEMA,
    render=_UI_DESCRIBE_RENDER,
)


async def _ui_act_tool(**kwargs: Any) -> dict[str, Any]:
    """SDK 分发入口（handler 以 kwargs 收参数）：汇回模块级共用入口 ui_act。

    ui_act 含 2s 等待窗同步轮询（sidecar 主循环与测试线程共用同一同步入口，
    ``_time.sleep`` 打桩面锁死），async 分发经 to_thread 下放线程池——等待窗
    不阻塞事件循环（async-blocking 闸合法出口）。
    """
    return await asyncio.to_thread(ui_act, kwargs)


plugin.register_tool(
    "ui_act",
    schema=_UI_ACT_INPUT_SCHEMA,
    handler=_ui_act_tool,
    description=(
        "操作前端界面对象（P1：侧边栏命令闭环）。词表封闭四动作：navigate（target=item id"
        " 或会话 id，切侧栏视图）/ set_collapsed（collapsed=true|false 折叠侧栏）/ open_page"
        "（page_id=插件页 id，开对应工作区页签）/ highlight（item_id 高亮侧栏项，"
        "duration_ms ≤5000 默认 2000 自动消退）。fail-closed：未知动作/缺参/目标不在最新"
        "快照即 rejected（reason 注明）；合法命令广播给全部前端实例，2s 内任一端 applied"
        " 即 applied（附最新快照），否则如实 timeout。"
    ),
    output_schema=_UI_ACT_OUTPUT_SCHEMA,
    render=_UI_ACT_RENDER,
)


class _EventBusCommandNotifier:
    """ui_command 出站生产接线：命令载荷经 event-bus capability emit 推前端。

    仿 human 插件 _EventBusNotifier 惯例（bus.notify("emit", ...) fire-and-forget，
    避免 emit 与内核 tools/call 响应形成嵌套死锁）。ui_act 在 SDK 线程池同步执行
    （asyncio.to_thread），本 notifier 经 on_load 捕获的 sidecar 主循环
    ``run_coroutine_threadsafe`` 投递，任意线程调用安全；event-bus 未注入 /
    主循环未捕获时如实降级（警告日志 + 不外发），命令走 timeout 收场。
    """

    def __init__(self, plugin_ref: Any) -> None:
        self._plugin = plugin_ref
        self._loop: asyncio.AbstractEventLoop | None = None

    def capture_loop(self) -> None:
        """on_load（主循环线程）调用：捕获 sidecar 主事件循环供线程安全投递。"""
        self._loop = asyncio.get_running_loop()

    def _bus(self) -> Any | None:
        try:
            return self._plugin.get_capability("event-bus")
        except (KeyError, AttributeError, RuntimeError):
            return None

    def __call__(self, payload: dict[str, Any]) -> None:
        bus = self._bus()
        if bus is None:
            logger.warning(
                "[ui_bridge] event-bus not injected; ui_command %s dropped",
                payload.get("command_id"),
            )
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning(
                "[ui_bridge] main loop not captured; ui_command %s dropped",
                payload.get("command_id"),
            )
            return
        try:
            asyncio.run_coroutine_threadsafe(
                bus.notify("emit", {"event": "ui_command", "payload": payload}), loop
            ).add_done_callback(self._log_emit_failure)
        except RuntimeError:
            logger.exception("[ui_bridge] ui_command %s emit failed", payload.get("command_id"))

    def _log_emit_failure(self, future: Any) -> None:
        try:
            future.result()
        except Exception:  # noqa: BLE001 — fire-and-forget 出站面，异常只记诊断不外抛
            logger.exception("[ui_bridge] ui_command emit failed")


_ui_bus_notifier = _EventBusCommandNotifier(plugin)


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """sidecar 生命周期：捕获主循环 + 注册生产 ui_command 外发面。"""
    _ui_bus_notifier.capture_loop()
    set_ui_command_notifier(_ui_bus_notifier)


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """ToolExecutionResult 信封（ok 包 HttpHandleResponse）。"""
    return _ok(_json_response_body(payload, status))


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="ui_bridge HTTP endpoints (GET/POST /ext/ui_bridge/state, POST /ext/ui_bridge/command_result)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """端点路由核：POST 上报（fail-closed）/ GET 回读（单端/定向/全端）/ POST 命令回执。"""
    if path.endswith("/command_result"):
        if method == "POST":
            try:
                body = _decode_body(raw_body, strict_object=True)
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)
            payload, status = handle_command_result(body)
            return _json_response(payload, status=status)
    if path.endswith("/state"):
        params = query or {}
        if method == "POST":
            try:
                body = _decode_body(raw_body, strict_object=True)
            except ValueError as exc:
                return _json_response({"error": str(exc)}, status=400)
            error = _validate_report(body)
            if error is not None:
                return _json_response({"error": error}, status=400)
            _store_snapshot(body)
            return _json_response({"ok": True})
        if method == "GET":
            return _json_response(
                query_state(
                    region=params.get("region", _DEFAULT_REGION),
                    client_id=params.get("client_id"),
                    all_clients=params.get("all") in ("1", "true"),
                )
            )
    return _json_response({"error": f"unknown route {method} {path}"}, status=404)


if __name__ == "__main__":
    plugin.run()
