#!/usr/bin/env python3
"""godot_context 管道插件 MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py（GodotContextPlugin），本文件只做接口适配：
- ``godot_context.execute``：管道 prepare 链调用（引用消息注入）；
- ``http.handle``：/ext/pipeline_godot_context/** 四条路由
  （Godot 推送 / 快照 / 订阅 / 预览代理）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from plugin import GodotContextPlugin, set_emitter  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin, FrontendEmitter  # noqa: E402
from agentos_plugin_sdk.pipeline_types import (  # noqa: E402
    PluginContext,
    create_initial_state,
)

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("godot_context_pipeline")

# Godot 宿主插件 HTTP 服务（预览代理目标）
GODOT_ADDON_ENDPOINT = os.environ.get("AGENTOS_GODOT_ENDPOINT", "http://127.0.0.1:9600")

_instance: GodotContextPlugin | None = None


def get_instance() -> GodotContextPlugin:
    """懒构建并缓存插件单例。"""
    global _instance
    if _instance is None:
        _instance = GodotContextPlugin(config=plugin.get_config())
    return _instance


@plugin.on_load
async def _on_load(_params: dict) -> None:
    """构建单例并注入前端推送器（旧内核未声明 frontend capability 时为 None）。"""
    get_instance()
    emitter = FrontendEmitter.from_plugin(plugin)
    if emitter is not None:
        set_emitter(emitter)
    else:
        logger.warning("[godot_context] frontend capability 未注入，选中变化不推送前端")


@plugin.on_unload
async def _on_unload(_params: dict) -> None:
    global _instance
    _instance = None


@plugin.tool(
    name="godot_context.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Godot Context pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """管道注入入口：选中非空时在用户消息后插入引用消息。"""
    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    route_sig = getattr(result, "route_signal", None)
    if route_sig:
        data["route_signal"] = {
            "route_type": route_sig.route_type,
            "target": route_sig.target,
            "reason": route_sig.reason,
        }
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


def _json_response(payload: dict, status: int = 200) -> dict:
    """内核期望的 HttpHandleResponse：body 须 base64（dispatcher 无条件解码）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "success": True,
        "data": {
            "status": status,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": base64.b64encode(body).decode("ascii"),
            "body_encoding": "base64",
        },
    }


def _decode_body(raw_body: str) -> dict:
    """解码 http.handle 的 raw_body（内核 dispatcher 恒 base64 编码；兼容明文）为 dict。

    解码探测：先按 base64 试解，解出内容以 { / [ 开头才判定为编码体，
    否则原样当作明文 JSON——base64.alphabet 之外的明文（含空白）必然在
    这里落入 pass 分支，属正常路径而非吞错。非法 JSON 最终由下方
    json.loads 转 ValueError（调用方转 400）。
    """
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        # 非 base64 载荷（明文直传的调用方）→ 保持原文交给 json.loads 判定
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


async def _fetch_preview(index: int) -> bytes | None:
    """代理 Godot 9600 /selection/preview，返回 PNG 字节；失败/非 PNG 返回 None。"""
    try:
        import aiohttp  # noqa: PLC0415

        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
            f"{GODOT_ADDON_ENDPOINT}/selection/preview",
            params={"index": index},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[godot_context] 预览代理失败: %s", e)
        return None


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
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    """Godot 选中引用桥 HTTP 端点。

    路由（签名覆盖 HttpHandleRequest 全部字段，SDK 展开为关键字参数）：
    - POST   /selection：Godot 宿主推送（type=selection/heartbeat/offline）
    - GET    /selection：当前选中快照（前端初始化）
    - DELETE /selection：清除当前引用（前端点击清理；抑制同签名心跳）
    - POST   /subscribe：前端订阅 {thread_id}
    - GET    /preview?index=N：代理 Godot 9600 预览 PNG
    """
    inst = get_instance()

    if method == "POST" and path.endswith("/selection"):
        try:
            payload = _decode_body(raw_body)
        except ValueError:
            return _json_response({"error": "invalid json"}, status=400)
        result = await inst.handle_push(payload)
        return _json_response(result)

    if method == "GET" and path.endswith("/selection"):
        return _json_response(inst.snapshot())

    if method == "DELETE" and path.endswith("/selection"):
        result = await inst.dismiss()
        return _json_response(result)

    if method == "POST" and path.endswith("/subscribe"):
        try:
            body = _decode_body(raw_body)
        except ValueError:
            body = {}
        return _json_response(inst.subscribe(str(body.get("thread_id", ""))))

    if method == "GET" and path.endswith("/preview"):
        try:
            index = int((query or {}).get("index", "0"))
        except (TypeError, ValueError):
            index = 0
        png = await _fetch_preview(index)
        if png is None:
            return _json_response({"error": "preview unavailable"}, status=502)
        return {
            "success": True,
            "data": {
                "status": 200,
                "headers": {"Content-Type": "image/png", "Cache-Control": "no-store"},
                "body": base64.b64encode(png).decode("ascii"),
                "body_encoding": "base64",
            },
        }

    return _json_response({"error": f"unknown route {method} {path}"}, status=404)


if __name__ == "__main__":
    plugin.run()
