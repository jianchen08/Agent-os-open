"""Godot 选中引用 Input 插件。

灵汐 AgentOS 与 Godot 4 编辑器的选中对象引用桥（事件驱动，无轮询）：
Godot 宿主插件（hosts/godot-addon）在 EditorSelection.selection_changed
信号时 POST 推送到本插件 /ext/pipeline_godot_context/selection 端点，
本插件负责三件事：

1. 维护最新选中快照（心跳 15s 内视为在线）；
2. 选中变化时经 FrontendEmitter 向订阅线程 emit ``godot_selection_changed``
   （前端聊天框引用卡片实时镜像：选中出现、取消消失）；
3. 管道注入（prepare 链）：每条新用户消息首轮、且选中非空时，
   在该用户消息后紧邻 insert 一条 ``<reference source="godot">`` 消息
   （messages op 协议），随历史落库——agent 据此理解"对这个/这个对象"所指。

State 命名空间：
    - godot.injected_for : 已注入引用的 message_id（幂等去重）。

引用清理（dismiss）：前端可请求清除当前引用（用户不想让这条选中随消息注入）。
清理后被清理签名的**心跳**被抑制——Godot 里节点仍选中时 5s 心跳不会把引用带
回来；type=selection 的同签名推送（用户重新点选）或签名变化即恢复推送。
    - _dismissed_signature : 被清理且仍在抑制的签名；None = 无抑制。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（server.py on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# FrontendEmitter（emit 选中变化到前端）；None 时静默跳过推送
_emitter: Any | None = None


def set_emitter(emitter: Any | None) -> None:
    """注入前端推送器（server.py on_load / 测试）。"""
    global _emitter
    _emitter = emitter


class GodotContextPlugin(IInputPlugin):
    """Godot 编辑器选中上下文：快照缓存 + 前端转发 + 消息级引用注入。"""

    HEARTBEAT_STALE_MS = 15_000  # 超过 15s 无心跳视为 Godot 离线

    @property
    def name(self) -> str:
        return "godot_context"

    @property
    def priority(self) -> int:
        return 50

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {}
        self._snapshot: dict[str, Any] = {
            "connected": False,
            "items": [],
            "signature": "",
            "scene": {},
        }
        self._last_push_ms: float = 0.0
        self._subscribed_threads: set[str] = set()
        self._last_signature: str | None = None
        self._dismissed_signature: str | None = None

    # ── 推送接收与快照（http.handle 调用） ──

    async def handle_push(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理 Godot 宿主推送（type: selection/heartbeat/offline）。

        selection 且签名变化时转发订阅线程；heartbeat 仅刷新在线时间戳。
        被清理（dismiss）的签名：刷新在线时间戳但不恢复引用，签名变化才恢复。
        """
        ptype = str(payload.get("type", "selection"))
        if ptype == "offline":
            self._snapshot = {
                "connected": False,
                "items": [],
                "signature": "",
                "scene": {},
            }
            self._last_push_ms = 0.0
            self._last_signature = ""
            self._dismissed_signature = None
            await self._broadcast()
            return {"status": "ok"}

        items = payload.get("items") or []
        signature = str(payload.get("signature", ""))

        # 清理抑制：心跳携带被清理的签名时只保活，不恢复引用、不广播；
        # type=selection 的同签名推送是用户重新选中（新引用意图），走恢复路径
        if (
            ptype == "heartbeat"
            and signature
            and self._dismissed_signature == signature
        ):
            self._last_push_ms = time.monotonic() * 1000.0
            self._snapshot["scene"] = payload.get("scene") or self._snapshot.get("scene", {})
            self._snapshot["engine_version"] = payload.get("engine_version", "")
            self._snapshot["project"] = payload.get("project", "")
            self._snapshot["ts"] = payload.get("ts", 0)
            return {"status": "ok"}

        self._dismissed_signature = None
        self._snapshot = {
            "connected": True,
            "items": items,
            "signature": signature,
            "scene": payload.get("scene") or {},
            "engine_version": payload.get("engine_version", ""),
            "project": payload.get("project", ""),
            "ts": payload.get("ts", 0),
        }
        self._last_push_ms = time.monotonic() * 1000.0
        if ptype == "selection" and signature != self._last_signature:
            self._last_signature = signature
            await self._broadcast()
        return {"status": "ok"}

    def snapshot(self) -> dict[str, Any]:
        """当前快照（前端初始化）；心跳超时视为离线但保留 items 供比对。"""
        snap = dict(self._snapshot)
        if snap.get("connected") and self._last_push_ms:
            alive_ms = time.monotonic() * 1000.0 - self._last_push_ms
            snap["connected"] = alive_ms <= self.HEARTBEAT_STALE_MS
        return snap

    def subscribe(self, thread_id: str) -> dict[str, Any]:
        """前端订阅选中变化（thread_id 用于 FrontendEmitter 单播路由）。"""
        if thread_id:
            self._subscribed_threads.add(thread_id)
        return {"status": "ok", "threads": len(self._subscribed_threads)}

    async def dismiss(self) -> dict[str, Any]:
        """清除当前引用（用户点击清理）：清空 items 并抑制同签名重复推送。

        connected/scene 保留（Godot 仍在线）；签名变化（改选/取消选中）恢复。
        空引用时 no-op（幂等）。
        """
        if not self._snapshot.get("items"):
            return {"status": "ok", "cleared": False}
        self._dismissed_signature = str(self._snapshot.get("signature", ""))
        self._snapshot["items"] = []
        self._snapshot["signature"] = ""
        # 重置广播去重键：恢复同签名选中（重新点选）时 items 从空到非空须广播
        self._last_signature = ""
        await self._broadcast()
        return {"status": "ok", "cleared": True}

    async def _broadcast(self) -> None:
        """把选中变化推给所有订阅线程（失败静默，不影响推送接收）。"""
        if _emitter is None or not self._subscribed_threads:
            return
        snap = self.snapshot()
        for thread_id in list(self._subscribed_threads):
            try:
                await _emitter.emit(
                    "godot_selection_changed",
                    {"thread_id": thread_id, **snap},
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[godot_context] emit %s 失败（继续）: %s", thread_id, e)

    # ── 管道注入（prepare 链调用） ──

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """新用户消息首轮且选中非空时，在用户消息后插入引用消息。"""
        state = ctx.state
        snap = self.snapshot()
        if not snap.get("connected") or not snap.get("items"):
            return PluginResult()

        # 幂等：同一条消息只注入一次（state 标量随 pipeline_state 持久化）
        message_id = str(state.get("message_id", ""))
        if not message_id or state.get("godot.injected_for") == message_id:
            return PluginResult()

        ref_msg = {
            "role": "user",
            "name": "godot_reference",
            "content": self._build_reference_content(snap),
        }
        messages = state.get("messages") or []
        return PluginResult(
            state_updates={
                "godot.injected_for": message_id,
                "messages": {
                    "_ops": [
                        {"op": "insert", "at": len(messages), "msg": ref_msg},
                    ]
                },
            }
        )

    @staticmethod
    def _build_reference_content(snap: dict[str, Any]) -> str:
        scene = snap.get("scene") or {}
        lines = [f'<reference source="godot" scene="{scene.get("path", "")}">']
        for it in snap.get("items", []):
            lines.append(
                f'- {it.get("name", "")} ({it.get("type", "")}) @ {it.get("path", "")}'
            )
        lines.append("</reference>")
        return "\n".join(lines)
