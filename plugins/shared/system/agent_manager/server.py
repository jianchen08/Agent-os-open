#!/usr/bin/env python3
"""Agent Manager 服务端——agent 配置管理面（原内核 /api/v1/agents* 4 路由语义承接）。

端点（http.handle 按 path 分发，协议与 task_form/monitoring 同款）：
- GET  /ext/agent_manager/agents                列表（?agent_type= 过滤，{items,total}）
- GET  /ext/agent_manager/agents/schema         字段声明（硬编码 12 字段，原样搬内核）
- GET  /ext/agent_manager/agents/{id}/config    读（敏感字段掩码 + 磁盘原文 etag）
- PUT  /ext/agent_manager/agents/{id}/config    写（语法校验 400 → If-Match 409 →
                                                 .bak 备份 → 写回；admin 由本插件自持检查）

服务（capabilities.services，供跨插件经 tool-executor 显式 plugin_id 调用）：
- agent.get             按 agent_id（文件名/config_id 两轮匹配）取解析后的 yaml dict
- agent.list            列表（同 HTTP 列表语义）
- agent.config-validate yaml 语法校验（不写盘）

行为契约（照 kernel/crates/api/src/routes.rs 现实现逐项搬移，一项不丢）：
- 两轮匹配：顶层 <id>.yaml 优先 → 递归文件名匹配 → config_id 回退（server.rs find_agent_yaml）
- agent_id 白名单 [A-Za-z0-9_-]（防路径穿越，routes.rs is_safe_agent_id）
- 掩码：key 含 api_key/apikey/secret/token/password → 值 "****"（${ENV} 占位符保留；
  config_service.rs mask_secrets）
- etag：磁盘原文 sha256 hex（B4 compute_etag）；PUT if_match 缺失/不匹配 → 409
- 备份：写前 copy 为 <file>.yaml.bak；非法 yaml → 400 不写盘
[来源: docs/decisions/2026-08-20-agent-manager-plugin.md]
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("agent_manager")

sys.path.insert(0, os.path.dirname(__file__))

# http.handle 响应封装（内核 HttpHandleResponse/ToolExecutionResult 样板）：
# 公共实现 plugins/shared/http_json.py，经共享层自举裸名导入。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from http_json import (  # noqa: E402
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
)

# ── 目录定位：AGENTOS_CONFIG_ROOT（内核启动写入，sidecar 继承进程环境）优先；
#    回退 __file__ 上溯项目根（与 context_build/task_form 同款防御）。──
_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parents[3]  # plugins/shared/system/agent_manager → 仓库根

# 内核 0.2 开发期 token 格式（http/src/auth.rs）：base64_nopad("access:{user_id}:{username}:{exp}")
_BUILTIN_ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"


def _agents_dir() -> Path:
    """定位 config/agents 目录（内核 AGENTOS_CONFIG_ROOT=<root>/config）。"""
    root = os.environ.get("AGENTOS_CONFIG_ROOT", "")
    if root:
        return Path(root) / "agents"
    return _PROJECT_ROOT / "config" / "agents"


# ══ 定位与扫描（routes.rs collect_yaml_files / resolve_agent_yaml_path +
#    server.rs find_agent_yaml 语义）══


def is_safe_agent_id(agent_id: str) -> bool:
    """agent_id 白名单（字母/数字/`-`/`_`）——防 percent-decode 后路径穿越。"""
    return bool(agent_id) and all(c.isascii() and (c.isalnum() or c in "-_") for c in agent_id)


def collect_yaml_files(dir_path: Path) -> list[Path]:
    """递归收集 .yaml/.yml（按路径排序保证稳定）。"""
    if not dir_path.is_dir():
        return []
    files = [p for p in dir_path.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file()]
    return sorted(files)


def find_agent_yaml(dir_path: Path, agent_id: str) -> Path | None:
    """两轮匹配：文件名 <agent_id>.yaml → yaml 内 config_id（递归）。"""
    target = f"{agent_id}.yaml"
    fallback: list[Path] = []
    for p in sorted(dir_path.iterdir()) if dir_path.is_dir() else []:
        if p.is_dir():
            found = find_agent_yaml(p, agent_id)
            if found is not None:
                return found
        elif p.name == target:
            return p
        elif p.suffix == ".yaml":
            fallback.append(p)
    for p in fallback:
        try:
            raw = p.read_text(encoding="utf-8")
            cfg = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(cfg, dict) and cfg.get("config_id") == agent_id:
            return p
    return None


def resolve_agent_yaml_path(agent_id: str) -> Path | None:
    """顶层 config/agents/<id>.yaml 优先，再递归分类子目录；非法 id 一律 None。"""
    if not is_safe_agent_id(agent_id):
        return None
    agents_dir = _agents_dir()
    top = agents_dir / f"{agent_id}.yaml"
    if top.is_file():
        return top
    return find_agent_yaml(agents_dir, agent_id)


# ══ 掩码与 etag（config_service.rs mask_secrets / compute_etag 语义）══

_SECRET_KEY_MARKERS = ("api_key", "apikey", "secret", "token", "password")


def _is_secret_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in _SECRET_KEY_MARKERS)


def _mask_secret_value(value: Any) -> Any:
    """`${ENV}` 占位符原样，真实明文 → `****`（非字符串原样保留）。"""
    if not isinstance(value, str):
        return value
    if value.startswith("${") and value.endswith("}"):
        return value
    return "****"


def mask_secrets(value: Any) -> Any:
    """递归掩码敏感字段值（secret 命中后其子树不再递归——对齐内核实现）。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_secret_key(k):
                out[k] = _mask_secret_value(v)
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def compute_etag(raw: str) -> str:
    """磁盘原文 sha256 hex（弱校验语义，B4）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ══ 业务语义（routes.rs agents_handler / get / put 逐项对齐）══


def list_agents(agent_type: str | None = None) -> dict[str, Any]:
    """扫描 config/agents/**/*.yaml → {items, total}（agent_type 过滤）。"""
    items: list[dict[str, Any]] = []
    for path in collect_yaml_files(_agents_dir()):
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        at = str(parsed.get("agent_type") or "")
        if agent_type is not None and at != agent_type:
            continue
        config_id = str(
            parsed.get("config_id")
            or parsed.get("id")
            or ""
        )
        if not config_id:
            continue
        items.append({
            "id": config_id,
            "config_id": config_id,
            "name": str(parsed.get("name") or config_id),
            "description": str(parsed.get("description") or ""),
            "agent_type": at,
            "status": "active",
            "model": str(parsed.get("model") or parsed.get("model_tier") or ""),
            "level": str(parsed.get("level") or ""),
            "model_tier": str(parsed.get("model_tier") or ""),
        })
    return {"items": items, "total": len(items)}


# 字段声明原样搬内核 agents_schema_handler（来源 .agent_template_spec.yaml）
AGENT_SCHEMA_FIELDS: list[dict[str, Any]] = [
    {"name": "config_id", "type": "string", "label": "配置ID", "required": True},
    {"name": "name", "type": "string", "label": "名称", "required": True},
    {"name": "display_name", "type": "string", "label": "显示名称"},
    {"name": "description", "type": "textarea", "label": "描述"},
    {
        "name": "agent_type",
        "type": "select",
        "label": "类型",
        "options": [
            {"label": "主控", "value": "main"},
            {"label": "编排", "value": "orchestrator"},
            {"label": "专用", "value": "specialized"},
            {"label": "原子", "value": "atomic"},
            {"label": "系统", "value": "system"},
        ],
    },
    {
        "name": "level",
        "type": "select",
        "label": "层级",
        "options": [
            {"label": "L1", "value": "L1"},
            {"label": "L2", "value": "L2"},
            {"label": "L3", "value": "L3"},
        ],
    },
    {"name": "model_tier", "type": "string", "label": "模型档位"},
    {"name": "system_prompt", "type": "textarea", "label": "系统提示词"},
    {"name": "tool_ids", "type": "multiselect", "label": "工具"},
    {"name": "max_iterations", "type": "number", "label": "最大迭代"},
    {"name": "timeout_seconds", "type": "number", "label": "超时秒"},
    {"name": "tags", "type": "multiselect", "label": "标签"},
]


def get_agent_config(agent_id: str) -> tuple[int, dict[str, Any]]:
    """读 agent yaml（掩码 + 磁盘原文 etag）。返回 (http_status, payload)。"""
    path = resolve_agent_yaml_path(agent_id)
    if path is None:
        return 404, {"error": f"agent config not found: {agent_id}"}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"read agent config {path}: {exc}"}
    etag = compute_etag(raw)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return 500, {"error": f"agent config yaml parse error: {exc}"}
    if parsed is None:
        parsed = {}
    masked = mask_secrets(parsed)
    yaml_text = yaml.safe_dump(masked, allow_unicode=True, sort_keys=True, default_flow_style=False)
    return 200, {"config_id": agent_id, "yaml": yaml_text, "etag": etag}


def put_agent_config(agent_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """写回 agent yaml：语法校验 400 → If-Match 409 → .bak 备份 → 写回。"""
    new_yaml = body.get("yaml")
    if not isinstance(new_yaml, str):
        return 400, {"error": "missing required field: yaml"}
    if_match = body.get("if_match")

    path = resolve_agent_yaml_path(agent_id)
    if path is None:
        return 404, {"error": f"agent config not found: {agent_id}"}

    # 语法校验先行（T2）：解析失败一律 400 拒写，磁盘保持原值。
    try:
        yaml.safe_load(new_yaml)
    except yaml.YAMLError as exc:
        return 400, {"error": f"agent config yaml invalid: {exc}"}

    # If-Match 乐观锁（A13）：必须匹配磁盘当前 ETag（缺失/不匹配 → 409）。
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"read agent config {path}: {exc}"}
    current_etag = compute_etag(raw)
    if not (isinstance(if_match, str) and if_match == current_etag):
        return 409, {
            "error": f"ETag mismatch: current={current_etag}, given={if_match!r}"
        }

    # 先备份原文件（同目录，内核 with_extension("yaml.bak") 同构：<stem>.yaml.bak），再写新内容。
    backup = path.with_suffix(".yaml.bak")
    try:
        backup.write_text(raw, encoding="utf-8")
        path.write_text(new_yaml, encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"write agent config {path}: {exc}"}
    return 200, {
        "config_id": agent_id,
        "success": True,
        "backup": backup.name,
        "etag": compute_etag(new_yaml),
    }


# ══ PUT 鉴权（内核 0.2 token 自持检查，write_surface_auth 的 /ext 等价）══


def _decode_kernel_token(token: str) -> tuple[str, str, int] | None:
    """解码内核 0.2 开发期 token（base64_nopad("access:{user_id}:{username}:{exp}")）。

    与 kernel http/src/auth.rs decode_token 同构；无效/过期返回 None。
    """
    try:
        padded = token.strip() + "=" * (-len(token.strip()) % 4)
        payload = base64.b64decode(padded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = payload.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        exp = int(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], exp


def _require_admin(headers: dict[str, str] | None) -> tuple[int, str] | None:
    """admin 闸：无效/缺失/过期 token → 401；有效但非 admin → 403。

    内核 write_surface_auth 不覆盖 /ext/**，本插件自持等价检查。角色判定按
    token 内 username/user_id（内核 token 无签名同水位；store 自定义 admin
    用户名不同时会误拒——0.2 单 admin 开发期已知限制，ADR 已记录）。
    """
    authz = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            authz = str(v)
            break
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        return 401, "missing bearer token"
    decoded = _decode_kernel_token(token)
    if decoded is None:
        return 401, "invalid or expired token"
    user_id, username, exp = decoded
    if int(time.time()) >= exp:
        return 401, "invalid or expired token"
    if username != "admin" and user_id != _BUILTIN_ADMIN_USER_ID:
        return 403, "admin role required"
    return None


# ══ http.handle 分发（/ext/agent_manager/** 入口）══

_CONFIG_PATH_RE = re.compile(r"^/ext/agent_manager/agents/(?P<id>[^/]+)/config$")


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
    description="HTTP endpoint handler for /ext/agent_manager/** (agent config management)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 agent_manager 4 端点（语义对齐原内核 /api/v1/agents*）。"""
    try:
        q = query or {}

        if path == "/ext/agent_manager/agents" and method == "GET":
            agent_type = q.get("agent_type") or None
            return _ok(_json_response(list_agents(agent_type)))

        if path == "/ext/agent_manager/agents/schema" and method == "GET":
            return _ok(_json_response({"fields": AGENT_SCHEMA_FIELDS}))

        m = _CONFIG_PATH_RE.match(path) if path else None
        if m and method == "GET":
            status, payload = get_agent_config(m.group("id"))
            return _ok(_json_response(payload, status))

        if m and method == "PUT":
            # admin 闸先行（内核 middleware 语义：鉴权先于 handler 一切判定）。
            denied = _require_admin(headers)
            if denied is not None:
                status, message = denied
                return _ok(_json_response({"error": message}, status))
            try:
                body = _decode_body(raw_body)
            except ValueError as exc:
                return _ok(_json_response({"error": str(exc)}, 400))
            status, payload = put_agent_config(m.group("id"), body)
            return _ok(_json_response(payload, status))

        logger.warning("http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent_manager http.handle failed: %s", exc)
        return _error(f"agent_manager service error: {exc}", 500)


# ══ 服务面（capabilities.services 声明即契约，G2 校验声明=实现）══


@plugin.tool(
    name="agent.get",
    schema={
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent ID (yaml filename stem or config_id)",
            }
        },
        "required": ["agent_id"],
    },
    description="Load one agent config (parsed yaml dict) by agent_id (filename or config_id two-round match)",
)
async def agent_get(agent_id: str = "") -> dict[str, Any]:
    """按 agent_id 取解析后的 yaml dict（内部服务，不掩码——消费方为插件而非面板）。"""
    path = resolve_agent_yaml_path(agent_id)
    if path is None:
        return {"found": False, "config": None}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {"found": False, "config": None}
    if not isinstance(parsed, dict):
        parsed = {}
    return {"found": True, "config": parsed, "path": str(path)}


@plugin.tool(
    name="agent.list",
    schema={
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": "Optional filter by agent_type field (e.g. main/orchestrator/specialized/atomic/system)",
            }
        },
    },
    description="List agent configs from config/agents/**/*.yaml (optional agent_type filter)",
)
async def agent_list(agent_type: str = "") -> dict[str, Any]:
    """agent 列表（同 HTTP 列表语义；agent_type 空串 = 不过滤）。"""
    return list_agents(agent_type or None)


@plugin.tool(
    name="agent.config-validate",
    schema={
        "type": "object",
        "properties": {
            "yaml": {
                "type": "string",
                "description": "Agent config yaml text to validate",
            }
        },
        "required": ["yaml"],
    },
    description="Validate agent config yaml syntax (parse check, no disk write)",
)
async def agent_config_validate(**kwargs: Any) -> dict[str, Any]:
    """yaml 语法校验（解析检查，不写盘）。

    参数名 ``yaml`` 与模块名冲突，经 ``**kwargs`` 接收（SDK 按签名过滤，
    VAR_KEYWORD 全量透传——agentos_plugin_sdk/server.py:_filter_handler_kwargs）。
    """
    yaml_text = kwargs.get("yaml")
    if not isinstance(yaml_text, str) or not yaml_text:
        return {"valid": False, "error": "missing required field: yaml"}
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return {"valid": False, "error": f"agent config yaml invalid: {exc}"}
    return {"valid": True, "error": None}


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    logger.info("agent_manager started (agents dir: %s)", _agents_dir())


if __name__ == "__main__":
    plugin.run()
