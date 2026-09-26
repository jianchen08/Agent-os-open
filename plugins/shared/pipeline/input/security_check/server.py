#!/usr/bin/env python3
"""security_check input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/security_check/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import (  # noqa: E402
    _PERMISSION_MODES,
    PERMISSION_MODES,
    SecurityCheckPlugin,
    _load_permission_modes,
    _save_permission_modes,
    _set_plugin_ref,
    set_frontend_emit,
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
    # 前端一次性事件通道（安全规则降级提示用；内核内置能力，缺失只降级日志）
    try:
        frontend_handle = plugin.get_capability("frontend")
    except KeyError:
        frontend_handle = None
    if frontend_handle is not None:

        async def _frontend_emit(event: str, payload: dict, thread_id: str) -> None:
            await frontend_handle.call(
                "emit",
                {"event": event, "payload": payload, "thread_id": thread_id},
            )

        set_frontend_emit(_frontend_emit)
    else:
        set_frontend_emit(None)
        logger.warning(
            "[security_check_pipeline] frontend 能力未注入，安全规则降级提示不推前端"
        )


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
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


# ─── 权限模式切换 HTTP 面（/ext/pipeline_security_check/permission_mode）──────────
# 纯插件能力：内核 dispatcher 按 plugin.json 的 http_endpoints 自动注册路由，
# 经 http.handle 进本插件。key = session_id（会话稳定键，与执行侧
# _explicit_permission_mode 同键；同会话内 pipeline_id 随任务/子代理轮换，
# 不作键——BUG-15）。高风险模式（auto/bypass）切换先经 human-interaction 弹
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
        "auto": "自动模式：未命中安全规则的操作直接执行；命中 block 级规则自动拒绝（不询问），"
        "needs_approval 级才弹审批。",
        "bypass": "旁路模式：跳过全部规则匹配与审批，危险命令可能不经确认直接执行；"
        "仅保留路径遍历、敏感目录、nul 重定向三条内置底线。请仅在信任环境使用！",
    }
    return warnings.get(mode, "")


async def _confirm_switch(session_id: str, mode: str, user_id: str = "") -> bool:
    """经 human-interaction 弹审批窗确认高风险模式切换。

    Args:
        session_id: 真实会话 id（thread_id）——前端审批 UI 按会话过滤订阅，
            传 pipeline_id 会导致审批窗不显示、确认永远超时。
        user_id: 发起切换的用户（审批归属归因，归属校验据此放行本人）。

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
            "user_id": user_id,
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
            except Exception as exc:  # noqa: BLE001 — query 异形按缺参走 GET 读取路径
                logger.debug("[security_check] GET query 解析失败（按缺参处理）| path=%s | error=%s", path_norm, exc)
        return await _get_permission_mode(body)

    # ── 授权区管理面（ADR 2026-09-24-read-deny-write-zones：名单增删查）──
    if method.upper() == "GET" and path_norm == "/ext/pipeline_security_check/zones":
        return _get_zone_policy()
    if method.upper() == "POST" and path_norm == "/ext/pipeline_security_check/zones/add":
        return _add_zone(body)
    if method.upper() == "POST" and path_norm == "/ext/pipeline_security_check/zones/remove":
        return _remove_zone(body)

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
    """解析权限模式 key：session_id（会话稳定键，BUG-15 裁定）。

    前端 FormWidget 在 POST body / GET query 中同传 pipeline_id 与 session_id，
    本端点只认 session_id——pipeline_id 同会话内随任务/子代理轮换，键在它上面
    会导致写入键与执行键失配。
    """
    return str(body.get("session_id") or "")


async def _switch_permission_mode(body: dict) -> dict:
    """切换会话权限模式（高风险模式需审批确认）。

    写表即显式选择：包括显式选 default（表内无条目时也要落表）——隔离/worktree
    会话的免审批默认仅在「未显式选择」（表内无条目）时生效；显式选
    default/accept_edits/auto 表示黑名单照常生效，必须能与缺省态区分；
    显式选 bypass 即免审批本身（与隔离默认同档，落表留痕）。
    """
    key = _resolve_key(body)
    mode = str(body.get("mode", "") or "")

    if not key:
        return _http_response(400, {"error": "session_id required", "switched": False})
    if mode not in PERMISSION_MODES:
        return _http_response(400, {"error": f"invalid mode: {mode}", "switched": False})

    current = _PERMISSION_MODES.get(key)
    if current is not None and current == mode:
        return _http_response(200, {"switched": True, "mode": mode, "unchanged": True})

    # 高风险模式：经 human-interaction 弹审批窗确认（复用现有审批 UI；
    # 确认请求用会话 id——前端审批 UI 按会话过滤）
    if mode in _HIGH_RISK_MODES:
        confirmed = await _confirm_switch(
            key,
            mode,
            user_id=str(body.get("user_id") or ""),
        )
        if not confirmed:
            return _http_response(
                200,
                {"switched": False, "reason": "用户未确认或确认超时", "mode": current or "default"},
            )

    _PERMISSION_MODES[key] = mode
    _save_permission_modes()
    logger.info(
        "[security_check] 权限模式切换 | session=%s | %s → %s",
        key,
        current or "未显式选择",
        mode,
    )
    return _http_response(200, {"switched": True, "mode": mode})


async def _get_permission_mode(body: dict) -> dict:
    """查询会话当前权限模式（GET 经 query 参数传 session_id）。

    explicit=True 表示用户显式选择过（表内条目）；False 表示缺省态——
    前端据此显示隔离/worktree 会话的免审批默认而非 default 档。
    """
    key = _resolve_key(body)
    explicit = key in _PERMISSION_MODES
    mode = _PERMISSION_MODES.get(key, "default")
    return _http_response(
        200,
        {"mode": mode, "explicit": explicit, "valid_modes": list(PERMISSION_MODES.keys())},
    )


# ─── 授权区管理面（/ext/pipeline_security_check/zones*）────────────────────
# 名单 = 用户空间真值 config/users/{tenant}/project_whitelist.yaml
# （entries=写区，read_deny=读排除；ADR 2026-09-24-read-deny-write-zones 决策4）。
# locked 写面：写入仅经本端点（用户显式操作）与位置闸授权卡两条系统通道，
# agent 工具面无写通道。

_ZONE_SECTIONS = ("entries", "read_deny")


def _get_zone_policy() -> dict:
    """读授权名单全量（真值解析：用户空间优先，legacy 读取回退）。"""
    try:
        from project_registry import load_read_deny, load_registration_whitelist
        return _http_response(200, {
            "entries": load_registration_whitelist(),
            "read_deny": load_read_deny(),
        })
    except Exception as exc:  # noqa: BLE001 — 共享根缺失等降级可见
        logger.error("[security_check] 授权名单读取失败: %s", exc)
        return _http_response(500, {"error": f"read failed: {exc}"})


def _add_zone(body: dict) -> dict:
    """追加名单条目（section=entries|read_deny）。"""
    section = str(body.get("section", "") or "")
    path = str(body.get("path", "") or "").strip()
    if section not in _ZONE_SECTIONS:
        return _http_response(400, {"error": f"invalid section: {section}"})
    if not path:
        return _http_response(400, {"error": "path required"})
    try:
        from project_registry import add_read_deny, add_write_zone
        merged = (
            add_write_zone(path) if section == "entries" else add_read_deny(path)
        )
    except ValueError as exc:
        return _http_response(400, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — 共享根缺失等降级可见
        logger.error("[security_check] 授权名单追加失败: %s", exc)
        return _http_response(500, {"error": f"add failed: {exc}"})
    logger.info("[security_check] 授权名单追加 | section=%s | path=%s", section, path)
    return _http_response(200, {"added": True, "section": section, section: merged})


def _remove_zone(body: dict) -> dict:
    """移除名单条目（幂等：条目不存在时无变化）。"""
    section = str(body.get("section", "") or "")
    path = str(body.get("path", "") or "").strip()
    if section not in _ZONE_SECTIONS:
        return _http_response(400, {"error": f"invalid section: {section}"})
    if not path:
        return _http_response(400, {"error": "path required"})
    try:
        from project_registry import remove_zone
        merged = remove_zone(path, section)
    except ValueError as exc:
        return _http_response(400, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — 共享根缺失等降级可见
        logger.error("[security_check] 授权名单移除失败: %s", exc)
        return _http_response(500, {"error": f"remove failed: {exc}"})
    logger.info("[security_check] 授权名单移除 | section=%s | path=%s", section, path)
    return _http_response(200, {"removed": True, "section": section, section: merged})


if __name__ == "__main__":
    plugin.run()
