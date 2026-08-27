#!/usr/bin/env python3
"""任务表单服务端——自持表单声明 + 取数端点（不塞 channel_api）。

- GET /ext/task_form/form?session_id=   表单字段声明（config/task_form.yaml），
  前端 fieldsUri 直接消费，字段声明唯一在配置文件。
- GET /ext/task_form/options/agents     执行 Agent 选项 {value:config_id,label:name}
  （读 config/agents/**/*.yaml）。
- GET /ext/task_form/options/projects   项目选项 {value:id,label:title}
  （共享 project_registry 登记行，project = 文件夹+登记）。

[设计取向] 任务工具（task_submit）只声明 LLM 工具、无服务面；新建任务的表单字段
与动态取数由本服务插件自持（插件自持服务对象），channel_api 零追加。
[协议] 与 evaluation_service 同款：http.handle 工具按 path 分发，返回
ToolExecutionResult{success,data}，data 为 HttpHandleResponse{status,headers,body(base64)}。
[来源: docs/decisions/2026-08-19-task-form-plugin-service.md]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("task_form")

# 共享层（http_json / project_registry）入 sys.path。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_SHARED_DIR = os.path.join(_PROJECT_ROOT, "plugins", "shared")
if os.path.isdir(_SHARED_DIR):
    sys.path.insert(0, _SHARED_DIR)

# http.handle 响应封装走公共实现（plugins/shared/http_json.py），调用点零改名。
# noqa: E402 —— 共享层自举后才能导入。
from http_json import (  # noqa: E402
    json_response as _json_response,
    ok as _ok,
)

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


async def _project_options() -> tuple[list[dict[str, Any]], str | None]:
    """共享 project_registry 登记行 → (选项列表, 降级原因)。

    登记簿可用（含真空——确实无项目）：(列表, None)；
    登记簿故障（模块缺失/实例化失败）：([], 原因)——故障不伪装成"无项目"，
    http_handle 在响应体补 warning 字段供前端提示；读面不崩。
    """
    try:
        from project_registry import ProjectRegistry  # noqa: PLC0415
    except Exception as exc:
        return [], f"project_registry 模块不可用: {exc}"
    try:
        registry = ProjectRegistry()
    except Exception as exc:
        return [], f"project_registry 初始化失败: {exc}"
    return [
        {"value": str(p.id), "label": str(p.title or p.id)}
        for p in registry.list()
    ], None


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
    description="HTTP endpoint handler for task form (declaration + agents/projects options)",
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

    # GET /ext/task_form/form —— 表单字段声明（前端 fieldsUri）
    if path == "/ext/task_form/form" and method == "GET":
        fields = _load_form_fields()
        return _ok(_json_response({"fields": fields, "id": "task_create", "title": "新建任务"}))

    # GET /ext/task_form/options/agents —— 执行 Agent 选项
    if path == "/ext/task_form/options/agents" and method == "GET":
        return _ok(_json_response({"data": _agent_options()}))

    # GET /ext/task_form/options/projects —— 项目选项（登记行，全局不过滤会话）；
    # 登记簿故障时 data 为空列表 + warning 提示字段（故障与真空可区分）
    if path == "/ext/task_form/options/projects" and method == "GET":
        options, degrade_reason = await _project_options()
        payload: dict[str, Any] = {"data": options}
        if degrade_reason:
            payload["warning"] = degrade_reason
        return _ok(_json_response(payload))

    # 未匹配的 path
    return _ok(_json_response({"error": "not found", "path": path}, 404))


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize task form service on load."""
    pass


if __name__ == "__main__":
    plugin.run()
