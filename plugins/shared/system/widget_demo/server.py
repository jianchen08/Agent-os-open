"""widget_demo sidecar——前端 widget 特性演示端点（widget 化全特性测试插件）。

纯声明插件的运行侧：为演示台的 datasourceUri / dataUri / endpoint 声明提供
真实数据：
- GET  /ext/widget_demo/config       演示配置 + fetch_count（G6-b poll 消费）
- PUT  /ext/widget_demo/config       写回演示配置（dataUri 提交）
- GET  /ext/widget_demo/schema       演示表单字段（fieldsUri 消费）
- GET  /ext/widget_demo/state        提交计数（G3 watch 订阅侧消费）
- GET  /ext/widget_demo/options/models?provider=xx  级联模型选项（G2 模板）
- GET  /ext/widget_demo/options/regions             静态数据源选项（G6-a 代理）
- POST /ext/widget_demo/actions/submit  通用提交（计数递增）
- POST /ext/widget_demo/actions/toggle  compact 切换（返回 switched）

http.handle 协议：dispatcher 把 HttpHandleRequest 整体作 arguments 传入，
返回 {success, data}，data 为 HttpHandleResponse{status, headers, body(base64)}。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("widget_demo")

# ── 内存态 ──────────────────────────────────────────────────
_state: dict[str, int] = {"fetch_count": 0, "submit_count": 0, "toggle_count": 0}

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "level": "l1",
    "threshold": 50,
    "tags": ["alpha"],
    "note": "",
}

# 级联选项：provider → models
_MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "zhipu": ["glm-5.2", "glm-4.7"],
    "openai": ["gpt-5.1", "gpt-4.5"],
    "deepseek": ["deepseek-v4", "deepseek-r2"],
}
_REGIONS: list[str] = ["cn-east", "cn-north", "global"]


def _find_config_path() -> Path:
    """向上查找项目根 config/system/widget_demo.yaml（sidecar cwd 是插件目录）。"""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "config" / "system" / "widget_demo.yaml"
        if candidate.exists():
            return candidate
    return cwd / "config" / "system" / "widget_demo.yaml"


def _read_config() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(_find_config_path().read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in _DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def _write_config(values: dict[str, Any]) -> None:
    import yaml  # noqa: PLC0415

    target = _find_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_config()
    merged.update({k: v for k, v in values.items() if k in _DEFAULT_CONFIG})
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body_str.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _decode_body(raw_body: str) -> dict[str, Any]:
    try:
        return json.loads(base64.b64decode(raw_body or "").decode("utf-8")) or {}
    except Exception:
        return {}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    _state.update({"fetch_count": 0, "submit_count": 0, "toggle_count": 0})
    logger.info("widget_demo loaded")


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
    description="HTTP endpoint handler for /ext/widget_demo/** (widget demo REST)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path/method 分发到演示端点。"""
    q = query or {}
    try:
        if path == "/ext/widget_demo/config" and method == "GET":
            _state["fetch_count"] += 1
            payload = _read_config()
            payload["fetch_count"] = _state["fetch_count"]
            return _ok(_json_response(payload))

        if path == "/ext/widget_demo/config" and method == "PUT":
            values = _decode_body(raw_body)
            _write_config(values)
            _state["submit_count"] += 1
            return _ok(_json_response({"ok": True, "saved": list(values.keys())}))

        if path == "/ext/widget_demo/schema" and method == "GET":
            # 与 config_files.fields 同构的字段声明（FormWidget fieldsUri 消费）
            fields = [
                {"name": "enabled", "type": "toggle", "label": "启用演示"},
                {"name": "threshold", "type": "slider", "label": "阈值", "min": 0, "max": 100},
                {"name": "note", "type": "input", "label": "备注"},
            ]
            return _ok(_json_response({"fields": fields}))

        if path == "/ext/widget_demo/state" and method == "GET":
            return _ok(_json_response(dict(_state)))

        if path == "/ext/widget_demo/options/models" and method == "GET":
            provider = q.get("provider", "")
            models = _MODELS_BY_PROVIDER.get(provider, ["unknown-model"])
            return _ok(
                _json_response(
                    {"options": [{"label": m, "value": m} for m in models]}
                )
            )

        if path == "/ext/widget_demo/options/regions" and method == "GET":
            return _ok(
                _json_response({"options": [{"label": r, "value": r} for r in _REGIONS]})
            )

        if path == "/ext/widget_demo/actions/submit" and method == "POST":
            values = _decode_body(raw_body)
            _state["submit_count"] += 1
            return _ok(_json_response({"ok": True, "echo": values, "submit_count": _state["submit_count"]}))

        if path == "/ext/widget_demo/actions/toggle" and method == "POST":
            values = _decode_body(raw_body)
            _state["toggle_count"] += 1
            mode = values.get("mode", "standard")
            return _ok(_json_response({"switched": True, "mode": mode, "toggle_count": _state["toggle_count"]}))

        logger.warning("widget_demo: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:
        logger.exception("widget_demo http.handle failed: %s", exc)
        return {"success": False, "error": str(exc), "data": _json_response({"error": str(exc)}, 500)}


if __name__ == "__main__":
    plugin.run()
