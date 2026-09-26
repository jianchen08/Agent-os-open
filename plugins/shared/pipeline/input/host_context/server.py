#!/usr/bin/env python3
"""host_context 管道插件 MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py（HostContextPlugin），本文件只做接口适配：
- ``host_context.execute``：管道 prepare 链调用（引用消息注入）；
- ``http.handle``：/ext/pipeline_host_context/** 五条路由
  （宿主推送 / 快照 / 订阅 / 预览代理）。
"""

from __future__ import annotations

import base64
import logging
import os

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from http_json import (  # noqa: E402
    decode_body as _decode_body_common,
    json_response as _json_response_body,
    ok as _ok,
)
from plugin import HostContextPlugin, set_emitter  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin, FrontendEmitter  # noqa: E402
from agentos_plugin_sdk.pipeline_types import (  # noqa: E402
    PluginContext,
    create_initial_state,
)

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("host_context_pipeline")

_instance: HostContextPlugin | None = None


def get_instance() -> HostContextPlugin:
    """懒构建并缓存插件单例。"""
    global _instance
    if _instance is None:
        _instance = HostContextPlugin(config=plugin.get_config())
    return _instance


@plugin.on_load
async def _on_load(_params: dict) -> None:
    """构建单例并注入前端推送器（旧内核未声明 frontend capability 时为 None）。"""
    get_instance()
    emitter = FrontendEmitter.from_plugin(plugin)
    if emitter is not None:
        set_emitter(emitter)
    else:
        logger.warning("[host_context] frontend capability 未注入，选中变化不推送前端")


@plugin.on_unload
async def _on_unload(_params: dict) -> None:
    global _instance
    _instance = None


@plugin.tool(
    name="host_context.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Host Context pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """管道注入入口：选中非空时把引用合并进最后一条用户消息。"""
    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


def _json_response(payload: dict, status: int = 200) -> dict:
    """本插件响应直接带 ToolExecutionResult 信封（ok 包 HttpHandleResponse，
    body base64——公共 json_response 产物再包一层 success/data）。"""
    return _ok(_json_response_body(payload, status))


def _decode_body(raw_body: str) -> dict:
    """解码 http.handle 的 raw_body（内核 dispatcher 恒 base64 编码；兼容明文）为 dict。

    解码探测与非法 JSON 处置走公共 decode_body；strict_object=True：顶层非
    object（数组/标量）显式抛 ValueError（调用方转 400），比公共默认（归一
    空体走缺参路径）更严——本面端点均为单对象 body 契约。
    """
    return _decode_body_common(raw_body, strict_object=True)


async def _fetch_preview(endpoint: str, index: int) -> bytes | None:
    """代理宿主 /selection/preview，返回 PNG 字节；失败/非 PNG/端点为空返回 None。"""
    if not endpoint:
        return None
    try:
        import aiohttp  # noqa: PLC0415

        timeout = aiohttp.ClientTimeout(total=5)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                f"{endpoint}/selection/preview",
                params={"index": index},
            ) as resp,
        ):
            if resp.status != 200:
                return None
            data = await resp.read()
            return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[host_context] 预览代理失败: %s", e)
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
    """宿主选中引用桥 HTTP 端点。

    路由（签名覆盖 HttpHandleRequest 全部字段，SDK 展开为关键字参数）：
    - POST   /selection：宿主推送（type=selection/heartbeat/offline）
    - GET    /selection：当前选中快照（前端初始化）
    - DELETE /selection：清除当前引用（前端点击清理；抑制同签名心跳）
    - POST   /subscribe：前端订阅 {thread_id}
    - GET    /preview?index=N：代理宿主 preview_endpoint 预览 PNG
    """
    inst = get_instance()

    if method == "POST" and path.endswith("/selection"):
        try:
            payload = _decode_body(raw_body)
        except ValueError:
            return _json_response({"error": "invalid json"}, status=400)
        # 共享密钥校验（S4，ADR 2026-09-11 豁免的匿名写面的补偿控制）：
        # 设置 HOST_CONTEXT_SHARED_SECRET 后，推送必须携带
        # X-Host-Secret 头（宿主插件侧同步配置）；未设置该环境变量时
        # 行为不变（编辑器无法携带 token 的既有豁免形态）。匿名伪造选中内容
        # 即注入下一条用户消息进 LLM 上下文，部署暴露内核端口时必须启用。
        secret = os.environ.get("HOST_CONTEXT_SHARED_SECRET", "")
        if secret:
            supplied = ""
            for k, v in (headers or {}).items():
                if isinstance(k, str) and k.lower() == "x-host-secret":
                    supplied = str(v)
                    break
            if supplied != secret:
                return _json_response({"error": "forbidden"}, status=403)
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
        png = await _fetch_preview(inst.preview_endpoint, index)
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
