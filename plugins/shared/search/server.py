#!/usr/bin/env python3
"""Resource Search 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("resource_search_tool")

_rs_tool_cls: Any = None


def _load_resource_search_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 ResourceSearchTool（合宿防遮蔽）。

    裸名 ``constants`` 是多成员同名面（邻成员装载期 import 占据静息态槽位，
    其 constants 无 ToolLimits）——摘槽/还槽语义单源在 _libs helper：exec
    窗口内本目录顶层模块一律本目录优先，exec 后静息态槽位原样恢复；加载
    结果模块级缓存。
    """
    global _rs_tool_cls
    if _rs_tool_cls is not None:
        return _rs_tool_cls
    _rs_tool_cls = load_tool_class(
        os.path.dirname(__file__), "resource_search_tool_impl", "ResourceSearchTool"
    )
    return _rs_tool_cls


# agent_manager 提供方插件 id（tool-executor 信封显式 plugin_id：系统插件服务
# 不在 LLM 工具注册表，反查必失败——task_submit 的 agent.get 消费同款形态）。
_ct_tool_cls: Any = None


def _load_call_tool() -> Any:
    """按文件路径加载本目录 call_tool.py 的 CallTool（模块级缓存）。

    与 `_load_resource_search_tool` 的 `load_tool_class` 不同，这里不走该 helper：
    它按约定只加载本目录 `tool.py`。call_tool.py 仅依赖 `agentos_plugin_sdk`，
    不 import 本目录任何裸名顶层模块（`constants` 等），因此无合宿裸名遮蔽面，
    直接 spec 装载即可。
    """
    global _ct_tool_cls
    if _ct_tool_cls is not None:
        return _ct_tool_cls
    import importlib.util  # noqa: PLC0415

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("call_tool_impl", os.path.join(here, "call_tool.py"))
    if spec is None or spec.loader is None:  # pragma: no cover —— 路径恒为本目录字面名
        raise ImportError(f"call_tool.py 规格解析失败: {os.path.join(here, 'call_tool.py')}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["call_tool_impl"] = module
    spec.loader.exec_module(module)
    _ct_tool_cls = module.CallTool
    return _ct_tool_cls


_AGENT_MANAGER_PLUGIN_ID = "agent_manager"

# 内核 HTTP 通道超时（对齐 agent_manager /api/v1/schema 取数 3s 量级；
# 列表端点 timeout_ms=5000 内必须让路）。
_TOOL_FACE_TIMEOUT_SECONDS = 3.0

# 登录 token 缓存（每次 resource_search 原本都重新 login + 取工具面 = 每搜
# 一次付两次 HTTP 往返；token 复用后常态只剩一次 GET）。TTL 取保守值：
# 内核 JWT 有效期内必然覆盖，到期/失效（401 等）由取数失败触发重登一次。
_TOKEN_TTL_SECONDS = 1500.0
_cached_token: tuple[str, float] | None = None  # (token, monotonic 到期时刻)


def _login_admin_token() -> str:
    """admin + 环境口令换 Bearer token（不缓存，调用方决定缓存策略）。"""
    password = (os.environ.get("AGENTOS_ADMIN_PASSWORD") or "").strip()
    if not password:
        raise RuntimeError("AGENTOS_ADMIN_PASSWORD 未设置：无法登录系统获取工具面")
    login_req = urllib.request.Request(
        f"{_kernel_base_url()}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open_no_proxy(login_req, _TOOL_FACE_TIMEOUT_SECONDS) as resp:
        login_payload = json.loads(resp.read().decode("utf-8"))
    token = login_payload.get("access_token") if isinstance(login_payload, dict) else None
    if not token:
        raise RuntimeError(f"系统登录响应缺少 access_token: {str(login_payload)[:200]}")
    return str(token)


def _fetch_tools_with_token(token: str) -> dict[str, Any]:
    tools_req = urllib.request.Request(
        f"{_kernel_base_url()}/api/v1/tools",
        headers={"Authorization": f"Bearer {token}"},
    )
    with _open_no_proxy(tools_req, _TOOL_FACE_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload


def _kernel_base_url() -> str:
    """内核 HTTP 基址（AGENTOS_KERNEL_PORT，sidecar 继承内核进程环境；缺省 9100）。

    回环固定 127.0.0.1：localhost 主机名在用户态系统代理 profile 切换/远端
    节点时会被劫持转发到代理远端（装机 2026-11-05 §B4），127.0.0.1 不经
    代理解析。
    """
    port = (os.environ.get("AGENTOS_KERNEL_PORT") or "").strip() or "9100"
    return f"http://127.0.0.1:{port}"


def _open_no_proxy(req: urllib.request.Request, timeout: float) -> Any:
    """回环请求强制无代理 opener（ProxyHandler({}) 覆盖注册表/env 代理面）。

    先例 llm/router_factory.disable_llm_proxy（LLM 出口强制直连）：回环调用
    走代理在任何部署形态都不成立（装机 2026-11-05 §B4）。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def _fetch_live_tools() -> dict[str, Any]:
    """GET /api/v1/tools 取内核活注册表工具面（T4 对账真值，冻结契约端点）。

    鉴权链与 mode_evolution 登录链同源：admin + ``AGENTOS_ADMIN_PASSWORD``
    换 Bearer（/api/v1/ 前缀受保护面，GET 需 admin/viewer 角色）。环境未设
    口令 = 显式失败（不内置默认凭据）。响应信封 ``{items: [...], total}``
    原样返回，条目校验在消费侧（tool.py `_live_tool_names`）。

    token 进程内缓存（TTL 内复用）；缓存 token 取数失败 = 可能过期/被撤销，
    失效缓存重登一次再试（仍失败 = 原样抛出，fail-closed 语义不变）。
    """
    global _cached_token
    now = time.monotonic()
    if _cached_token is not None and _cached_token[1] > now:
        try:
            return _fetch_tools_with_token(_cached_token[0])
        except Exception:  # noqa: BLE001 —— 缓存失效走重登，真实错误以重登结果为准
            _cached_token = None
    token = _login_admin_token()
    _cached_token = (token, time.monotonic() + _TOKEN_TTL_SECONDS)
    return _fetch_tools_with_token(token)


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """注入 agent / skill 注册表服务调用方与工具面对账通道。

    agent 面：tool-executor 信封带显式 plugin_id 直达 agent_manager（T2）。
    skill 面：provides namespace ``skill`` 的 capability handle 直连
    skill_manager（T3，approval 消费 human-interaction 生产先例）。
    工具面：内核 HTTP 通道调 ``GET /api/v1/tools``（T4 对账真值；urllib
    阻塞调用经 to_thread 下放，fs_tools 同款）。

    能力句柄在协程内懒解析：on_load 早于 capability 注入完成也能在真正调用
    时拿到（task_submit 同款）；未注入 → KeyError 传播 → 工具面显式失败信封。
    """
    impl = sys.modules.get(_load_resource_search_tool().__module__)
    if impl is None:  # pragma: no cover —— _load 刚成功，模块必在 sys.modules
        return

    async def _agent_service(tool_name: str, args: dict[str, Any]) -> Any:
        handle = plugin.get_capability("tool-executor")
        invoke = bind_capability_caller(handle, "tool-executor")
        return await invoke(
            "tool-executor.invoke",
            {"tool_name": tool_name, "plugin_id": _AGENT_MANAGER_PLUGIN_ID, "args": args},
        )

    async def _skill_service(method: str, args: dict[str, Any]) -> Any:
        handle = plugin.get_capability("skill")
        return await handle.call(method, args)

    async def _tool_surface() -> dict[str, Any]:
        return await asyncio.to_thread(_fetch_live_tools)

    async def _tool_schemas(names: list[str]) -> Any:
        # external MCP 的可执行定义面：零声明目录的工具 schema 只在活注册表，
        # 而 GET /api/v1/tools 不投喂 schema——故走内核既有 tool-surface.schemas
        # 能力（STANDARD_CAPABILITIES 已列，零新增契约）。
        handle = plugin.get_capability("tool-surface")
        invoke = bind_capability_caller(handle, "tool-surface")
        return await invoke("tool-surface.schemas", {"tool_ids": list(names)})

    impl.set_agent_service_caller(_agent_service)
    impl.set_skill_service_caller(_skill_service)
    impl.set_tool_surface_caller(_tool_surface)
    impl.set_tool_schemas_caller(_tool_schemas)

    # call_tool（动态工具薄转发壳）两条接线：读动态定义 + 转发既有执行面。
    # 执行面与 `_agent_service` 同一条既有通道（tool-executor.invoke），
    # 不新开通道、不新增契约；plugin_id 取自动态定义（定义真值 = manifest 声明）。
    ct = sys.modules.get(_load_call_tool().__module__)
    if ct is not None:
        async def _state_rows() -> Any:
            handle = plugin.get_capability("pipeline-state")
            return await handle.call("list", {})

        async def _invoke_dynamic(tool_name: str, args: dict[str, Any], plugin_id: str) -> Any:
            handle = plugin.get_capability("tool-executor")
            invoke = bind_capability_caller(handle, "tool-executor")
            payload: dict[str, Any] = {"tool_name": tool_name, "args": args}
            if plugin_id:
                payload["plugin_id"] = plugin_id
            return await invoke("tool-executor.invoke", payload)

        ct.set_state_reader(_state_rows)
        ct.set_tool_invoker(_invoke_dynamic)


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """摘除服务调用方（防卸载窗口期残留句柄——isolation_guard 同款）。"""
    impl = sys.modules.get("resource_search_tool_impl")
    if impl is not None:
        impl.set_agent_service_caller(None)
        impl.set_skill_service_caller(None)
        impl.set_tool_surface_caller(None)
        impl.set_tool_schemas_caller(None)
    ct = sys.modules.get("call_tool_impl")
    if ct is not None:
        ct.set_state_reader(None)
        ct.set_tool_invoker(None)


@plugin.tool(
    name="resource_search",
    schema={
        "type": "object",
        "properties": {
            "resource_type": {
                "type": "string",
                "enum": ["agent", "tool", "skill", "all"],
                "description": "资源类型：agent/tool/skill/all",
            },
            "query": {
                "type": "string",
                "description": "搜索关键词（支持子串匹配；为空或通配符 *,all,所有 时返回全部）",
            },
            "mode": {
                "type": "string",
                "enum": ["simple", "detailed"],
                "default": "simple",
                "description": (
                    "simple=列出匹配资源；"
                    "detailed=返回完整描述（skill 返回 SKILL.md 内容，agent 返回 "
                    "recommended_metrics；tool 返回含真实 input_schema 的 tool_defs "
                    "可执行定义）。注意：搜到的工具不会自动加载进工具面，须用 "
                    "detailed 拿到 tool_defs 后经 call_tool 调用。"
                ),
            },
            "filters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "按分类过滤（仅 tool 类型生效，匹配 plugin.json 的 plugin_type）",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["system", "user", "all"],
                        "description": "按级别过滤（仅 agent 类型生效；system≈L1，user≈L2/L3）",
                    },
                    "language": {
                        "type": "string",
                        "description": "按语言过滤 Skill 脚本：python/nodejs/bash/powershell",
                    },
                },
                "description": "可选过滤条件",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "maximum": 20,
                "description": "返回数量，默认20",
            },
        },
        "required": ["resource_type"],
    },
    description="搜索系统内资源",
)
async def resource_search(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = _load_resource_search_tool()()
    result = await t.execute(kwargs)
    # 失败信封显式 success=False（ADR 2026-09-28 收编契约）——裸 error 载荷
    # 会被归一层包成 success 伪信封。
    return tool_result_envelope(result)


@plugin.tool(
    name="call_tool",
    schema={
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "description": (
                    "目标工具名，须是本次运行里已由 resource_search(mode='detailed') "
                    "引入过的动态工具之一（引入后持续有效，不必重搜）"
                ),
            },
            "arguments": {
                "type": "object",
                "description": "目标工具参数对象，结构遵循该工具 tool_defs 里的 input_schema",
            },
        },
        "required": ["tool", "arguments"],
        "additionalProperties": False,
    },
    description="调用资源搜索引入的动态工具",
)
async def call_tool(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = _load_call_tool()()
    result = await t.execute(kwargs)
    # 失败信封显式 success=False（与 resource_search 同一归一契约）
    return tool_result_envelope(result)


if __name__ == "__main__":
    plugin.run()
