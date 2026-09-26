#!/usr/bin/env python3
"""onboarding_service — 引导页面插件（内容 + 进度端点）。

职责单一：把 fail-closed 校验后的 walkthrough 内容与用户空间进度经
``http.handle`` 面供给前端预置 widget（onboarding_panel）。无业务判定、
无状态机——判定逻辑全部在前端求值器（services/onboarding/evaluator.ts），
本插件只做：内容装载校验（content_schema）+ 进度读写（progress_store）。

进度落 ``<user_config_dir>/onboarding/progress.json``（用户空间 gitignored、
免工作区还原、跨浏览器一致；roleplay 用户层先例）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

plugin = AgentOSPlugin("onboarding_service")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

import content_schema  # noqa: E402
import progress_store  # noqa: E402
from http_json import (  # noqa: E402
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
    protocol_error as _protocol_error,
)

_CONTENT_DIR = Path(__file__).resolve().parent / "content" / "walkthroughs"

_WALKTHROUGHS_ROUTE = "/ext/onboarding_service/walkthroughs"
_PROGRESS_ROUTE = "/ext/onboarding_service/progress"


def _progress_path() -> Path | None:
    """用户空间进度文件路径；用户空间不可得返回 None（读面降级空、写面报错）。"""
    try:
        from user_space import user_config_dir

        resolved = user_config_dir()
    except Exception:  # noqa: BLE001 — 用户空间模块缺失按「无用户空间」定语义
        return None
    if resolved is None:
        return None
    return Path(resolved) / "onboarding" / "progress.json"


def _load_step_ids() -> tuple[dict[str, set[str]], dict[str, Any] | None]:
    """装载内容 → ({walkthrough_id: {step_id}}, 错误响应)。"""
    walkthroughs, errors = content_schema.load_walkthroughs(_CONTENT_DIR)
    if errors:
        detail = "; ".join(f"{e['file']}: {e['error']}" for e in errors[:3])
        return {}, _error(f"引导内容校验失败（拒载）: {detail}", 500)
    return {w["id"]: {s["id"] for s in w["steps"]} for w in walkthroughs}, None


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
    description="HTTP endpoint handler for /ext/onboarding_service/** (walkthroughs + progress)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发引导内容与进度端点。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    """
    # GET /ext/onboarding_service/walkthroughs —— 内容清单（校验失败整包 500 拒载）
    if path == _WALKTHROUGHS_ROUTE and method == "GET":
        walkthroughs, errors = content_schema.load_walkthroughs(_CONTENT_DIR)
        if errors:
            detail = "; ".join(f"{e['file']}: {e['error']}" for e in errors[:3])
            return _error(f"引导内容校验失败（拒载）: {detail}", 500)
        return _ok(_json_response({"walkthroughs": walkthroughs}))

    # GET /ext/onboarding_service/progress —— 读进度（用户空间缺失降级空表）
    if path == _PROGRESS_ROUTE and method == "GET":
        progress_path = _progress_path()
        if progress_path is None:
            return _ok(_json_response({"progress": {}}))
        try:
            return _ok(_json_response({"progress": progress_store.load_progress(progress_path)}))
        except progress_store.ProgressCorruptError as exc:
            return _error(str(exc), 500)

    # POST /ext/onboarding_service/progress —— 合并单条步骤进度
    if path == _PROGRESS_ROUTE and method == "POST":
        progress_path = _progress_path()
        if progress_path is None:
            return _error("用户配置目录不可得（user_config_dir()=None），进度无处落盘", 500)
        body = _decode_body(raw_body)
        step_ids, load_err = _load_step_ids()
        if load_err is not None:
            return load_err
        try:
            current = progress_store.load_progress(progress_path)
        except progress_store.ProgressCorruptError as exc:
            return _error(str(exc), 500)
        new_progress, err = progress_store.apply_update(current, step_ids, body)
        if err is not None:
            return _protocol_error(err, 400)
        progress_store.save_progress(progress_path, new_progress)
        return _ok(_json_response({"progress": new_progress}))

    return _error(f"未知端点 {method} {path}", 404)
