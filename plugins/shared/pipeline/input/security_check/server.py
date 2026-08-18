#!/usr/bin/env python3
"""security_check input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/security_check/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from plugin import (  # noqa: E402
    PERMISSION_MODES,
    SecurityCheckPlugin,
    _PERMISSION_MODES,
    _load_permission_modes,
    _save_permission_modes,
    _set_plugin_ref,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("security_check_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> SecurityCheckPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    # 注入 plugin 引用，供 SecurityCheckPlugin 内部拿 human-interaction capability。
    _set_plugin_ref(plugin)
    config = plugin.get_config()
    return SecurityCheckPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize security_check plugin."""
    get_instance()  # 预热：注入 plugin 引用 + 构建单例（保持原 on_load 构造时机）
    _load_permission_modes()  # 加载权限模式持久化表


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup security_check plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="security_check.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Security Check pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the security_check pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    # Core 插件返回 dict，Input/Output 返回 PluginResult/OutputResult
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


# ─── 权限模式切换 HTTP 面（/ext/pipeline_security_check/permission_mode）──────────
# 纯插件能力：内核 dispatcher 按 plugin.json 的 http_endpoints 自动注册路由，
# 经 http.handle 进本插件。key = pipeline_id（每管道独立，对齐"权限跟当前
# 选中管道标签走"）。高风险模式（auto/bypass）切换先经 human-interaction 弹
# 审批窗确认（复用现有审批 UI + 留痕），确认后才写入权限模式表。

# 高风险模式：切换需用户审批确认
_HIGH_RISK_MODES = frozenset({"auto", "bypass"})


def _switch_options() -> list[dict[str, str]]:
    return [
        {"id": "confirm", "label": "确认切换"},
        {"id": "cancel", "label": "取消"},
    ]


def _mode_warning(mode: str) -> str:
    warnings = {
        "auto": "自动模式将尽量不打扰：常规工具自动放行，仅危险/未授权操作弹审批。",
        "bypass": "旁路模式将跳过所有审批（仅保留路径遍历与敏感目录底线检查），"
        "危险命令可能自动执行。仅在信任环境使用！",
    }
    return warnings.get(mode, "")


async def _confirm_switch(session_id: str, mode: str) -> bool:
    """经 human-interaction 弹审批窗确认高风险模式切换。

    Args:
        session_id: 真实会话 id（thread_id）——前端审批 UI 按会话过滤订阅，
            传 pipeline_id 会导致审批窗不显示、确认永远超时。

    Returns:
        True=用户确认；False=取消/超时/交互服务不可用。
    """
    try:
        hi_cap = plugin.get_capability("human-interaction")
    except (KeyError, AttributeError):
        logger.warning("[security_check] human-interaction capability 不可用，拒绝切换 | mode=%s", mode)
        return False
    try:
        create_res = await hi_cap.call("create_choice", {
            "session_id": session_id,
            "thread_id": session_id,
            "tab_id": "",
            "title": f"权限模式切换确认: {mode}",
            "description": f"将要切换到「{mode}」模式。\n{_mode_warning(mode)}",
            "options": _switch_options(),
            "priority": "high",
        })
        if not isinstance(create_res, dict) or create_res.get("error"):
            raise RuntimeError(f"create_choice failed: {create_res}")
        request_id = create_res.get("request_id", "")
        wait_res = await hi_cap.call("wait_for_choice", {
            "request_id": request_id,
            "timeout": 300,
        })
        if not isinstance(wait_res, dict) or wait_res.get("error"):
            return False
        raw = wait_res.get("selected_option", "")
        return raw in ("confirm", "确认切换")
    except Exception as exc:
        logger.warning("[security_check] 模式切换确认异常 | error=%s", exc)
        return False


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string", "description": "Base64 编码的请求体"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
            "query_multi": {"type": "object"},
        },
        "required": ["path", "method"],
    },
    description="HTTP endpoint handler for /ext/security_check/**（权限模式切换）",
)
async def http_handle(path: str, method: str, plugin_id: str = "", raw_body: str = "", **kwargs: dict) -> dict:
    """处理 /ext/security_check/** HTTP 请求（权限模式切换端点）。"""
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    body: dict = {}
    if raw_body:
        try:
            body = json.loads(base64.b64decode(raw_body).decode("utf-8"))
        except Exception:
            body = {}

    path_norm = path.rstrip("/")

    if method.upper() == "POST" and path_norm == "/ext/pipeline_security_check/permission_mode":
        return await _switch_permission_mode(body)
    if method.upper() == "GET" and path_norm == "/ext/pipeline_security_check/permission_mode":
        # GET 参数经 query（query 可能是 dict 或 [key, value] 元组列表）
        query = kwargs.get("query") or {}
        if isinstance(query, dict):
            body = {**query, **body}
        else:
            try:
                body = {k: v for k, v in query}  # type: ignore[misc]
            except Exception:
                pass
        return await _get_permission_mode(body)

    return _http_response(404, {"error": "not found"})


def _http_response(status: int, data: dict) -> dict:
    """包装为内核 dispatcher 期望的响应结构（user_admin 范式）。"""
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    return {
        "success": True,
        "data": {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii"),
            "body_encoding": "base64",
        },
    }


def _resolve_key(body: dict) -> str:
    """解析权限模式 key：pipeline_id 为主（每管道独立），兼容旧 session_id。"""
    return str(body.get("pipeline_id") or body.get("session_id") or "")


async def _switch_permission_mode(body: dict) -> dict:
    """切换管道权限模式（高风险模式需审批确认）。"""
    key = _resolve_key(body)
    mode = str(body.get("mode", "") or "")

    if not key:
        return _http_response(400, {"error": "pipeline_id required", "switched": False})
    if mode not in PERMISSION_MODES:
        return _http_response(400, {"error": f"invalid mode: {mode}", "switched": False})

    current = _PERMISSION_MODES.get(key, "default")
    if mode == current:
        return _http_response(200, {"switched": True, "mode": mode, "unchanged": True})

    # 高风险模式：经 human-interaction 弹审批窗确认（复用现有审批 UI；
    # 确认请求用真实 session_id——前端审批 UI 按会话过滤，pipeline_id 会不显示）
    if mode in _HIGH_RISK_MODES:
        confirmed = await _confirm_switch(
            str(body.get("session_id") or key),
            mode,
        )
        if not confirmed:
            return _http_response(
                200,
                {"switched": False, "reason": "用户未确认或确认超时", "mode": current},
            )

    _PERMISSION_MODES[key] = mode
    _save_permission_modes()
    logger.info(
        "[security_check] 权限模式切换 | pipeline=%s | %s → %s",
        key,
        current,
        mode,
    )
    return _http_response(200, {"switched": True, "mode": mode})


async def _get_permission_mode(body: dict) -> dict:
    """查询管道当前权限模式（GET 经 query 参数传 pipeline_id）。"""
    key = _resolve_key(body)
    mode = _PERMISSION_MODES.get(key, "default")
    return _http_response(
        200,
        {"mode": mode, "valid_modes": list(PERMISSION_MODES.keys())},
    )


if __name__ == "__main__":
    plugin.run()
