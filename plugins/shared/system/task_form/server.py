#!/usr/bin/env python3
"""任务表单服务端——自持表单声明 + 取数端点（不塞 channel_api）。

- GET /ext/task_form/form?session_id=   表单字段声明（config/task_form.yaml），
  容器选项 datasourceUri 内嵌 session_id（前端 fieldsUri 直接消费，字段声明唯一在配置文件）。
- GET /ext/task_form/options/agents     执行 Agent 选项 {value:config_id,label:name}
  （读 config/agents/**/*.yaml）。
- GET /ext/task_form/options/containers 容器任务选项 {value:id,label:title}
  （task_service.list_all 过滤 task_scope=container，session_id 可空=全部会话）。

[设计取向] 任务工具（task_submit）只声明 LLM 工具、无服务面；新建任务的表单字段
与动态取数由本服务插件自持（插件自持服务对象），channel_api 零追加。
[协议] 与 evaluation_service 同款：http.handle 工具按 path 分发，返回
ToolExecutionResult{success,data}，data 为 HttpHandleResponse{status,headers,body(base64)}。
[来源: docs/decisions/2026-08-19-task-form-plugin-service.md]
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("task_form_service")

# tasks 包（get_task_service）与其依赖目录入 sys.path——与 channel_api 同款限定导入。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, "plugins", "shared", "system")
_TASKS_DIR = os.path.join(_PROJECT_ROOT, "plugins", "shared", "system", "tasks")
for _d in (_SYSTEM_DIR, _TASKS_DIR):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)

# 表单声明 / 执行 Agent 配置根：与 manifest config_files.task_form.path 一致。
_FORM_REL = os.path.join("config", "task_form.yaml")
_AGENTS_REL = os.path.join("config", "agents")


def _project_root() -> str:
    """定位项目根：优先 AGENTOS_PROJECT_ROOT，回退相对路径上溯。"""
    root = os.environ.get("AGENTOS_PROJECT_ROOT")
    if root and os.path.isdir(root):
        return root
    cur = os.getcwd()
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "config")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.getcwd()


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把任意 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    """错误响应：{success:false, error, data}。data 携带 HTTP 状态给前端。"""
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _load_form_fields() -> list[dict[str, Any]]:
    """读表单声明 yaml 的 fields 数组。读失败/无 fields 返回空列表（不抛）。"""
    yaml_path = os.path.join(_project_root(), _FORM_REL)
    try:
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("form", {})
    if isinstance(raw, dict):
        fields = raw.get("fields", [])
    else:
        fields = data.get("fields", [])
    return [f for f in fields if isinstance(f, dict)]


def _agent_options() -> list[dict[str, Any]]:
    """config/agents/**/*.yaml → [{value: config_id, label: name}]，按 label 排序。"""
    agents_dir = os.path.join(_project_root(), _AGENTS_REL)
    out: list[dict[str, Any]] = []
    if not os.path.isdir(agents_dir):
        return out
    for yaml_file in sorted(Path(agents_dir).rglob("*.yaml")):
        try:
            with open(yaml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        value = str(data.get("config_id") or yaml_file.stem)
        label = str(data.get("name") or data.get("display_name") or value)
        out.append({"value": value, "label": label})
    out.sort(key=lambda o: o["label"])
    return out


async def _container_options(session_id: str) -> list[dict[str, Any]]:
    """task_service.list_all 过滤 task_scope=container → [{value:id, label:title}]。

    服务不可用/无容器返回空列表（读面降级不崩）。
    """
    try:
        from tasks.service_access import get_task_service  # noqa: PLC0415
    except Exception:
        return []
    service = get_task_service()
    if service is None:
        return []
    try:
        tasks = await service.list_all(limit=1000, session_id=session_id or None)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for tm in tasks:
        meta = getattr(tm, "metadata", None) or {}
        if meta.get("task_scope") == "container":
            out.append(
                {
                    "value": str(getattr(tm, "id", "")),
                    "label": str(getattr(tm, "title", "") or getattr(tm, "id", "")),
                }
            )
    return out


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
    description="HTTP endpoint handler for task form (declaration + agents/containers options)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到任务表单三端点。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    """
    q = query or {}

    # GET /ext/task_form/form —— 表单字段声明（前端 fieldsUri；session_id 内嵌容器选项）
    if path == "/ext/task_form/form" and method == "GET":
        fields = _load_form_fields()
        session_id = q.get("session_id", "")
        if session_id:
            for f in fields:
                uri = f.get("datasourceUri")
                if isinstance(uri, str) and "containers" in uri:
                    sep = "&" if "?" in uri else "?"
                    f["datasourceUri"] = f"{uri}{sep}session_id={session_id}"
        return _ok(_json_response({"fields": fields, "id": "task_create", "title": "新建任务"}))

    # GET /ext/task_form/options/agents —— 执行 Agent 选项
    if path == "/ext/task_form/options/agents" and method == "GET":
        return _ok(_json_response({"data": _agent_options()}))

    # GET /ext/task_form/options/containers —— 容器任务选项
    if path == "/ext/task_form/options/containers" and method == "GET":
        return _ok(_json_response({"data": await _container_options(q.get("session_id", ""))}))

    # 未匹配的 path
    return _ok(_json_response({"error": "not found", "path": path}, 404))


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize task form service on load."""
    pass
