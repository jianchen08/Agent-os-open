"""配置管理 API 路由。

读取和写入 YAML 配置文件，为前端设置页面提供数据。
写入后清除内存缓存，使运行中的系统自动加载新配置。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException

from config.models import invalidate_all_llm_caches

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["配置管理"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_MODELS_DIR = _PROJECT_ROOT / "config" / "models"
_CONFIG_SYSTEM_DIR = _PROJECT_ROOT / "config" / "system"

_LLM_YAML = _CONFIG_MODELS_DIR / "llm.yaml"
_CONTEXT_WINDOW_YAML = _CONFIG_SYSTEM_DIR / "context_window_config.yaml"
_API_YAML = _CONFIG_SYSTEM_DIR / "api_config.yaml"
_CONCURRENCY_YAML = _CONFIG_SYSTEM_DIR / "concurrency_config.yaml"


# ---------------------------------------------------------------------------
# YAML 读写工具
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"配置文件不存在: {path.name}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------

@router.get("/llm", summary="获取完整 LLM 配置")
def get_llm_config() -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    # 脱敏 providers 中的 api_key
    providers = data.get("providers", {})
    masked = {}
    for pid, pconf in providers.items():
        masked[pid] = {
            **pconf,
            "api_key": _mask_key(pconf.get("api_key", "")),
        }
    # 脱敏 models 中的 api_key
    models = data.get("models", {})
    masked_models = {}
    for mid, mconf in models.items():
        m = {**mconf}
        if "api_key" in m:
            m["api_key"] = _mask_key(m["api_key"])
        masked_models[mid] = m

    return {
        "models": masked_models,
        "providers": masked,
        "defaults": data.get("defaults", {}),
    }


@router.get("/llm/providers", summary="获取提供商列表")
def get_providers() -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    result = {}
    for pid, pconf in providers.items():
        result[pid] = {
            "api_base": pconf.get("api_base", ""),
            "has_key": bool(pconf.get("api_key")),
        }
    return {"providers": result}


@router.get("/llm/models", summary="获取模型列表")
def get_models() -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.get("models", {})
    masked = {}
    for mid, mconf in models.items():
        m = {**mconf}
        if "api_key" in m:
            m["api_key"] = _mask_key(m["api_key"])
        masked[mid] = m
    return {"models": masked}


@router.get("/llm/defaults", summary="获取默认模型配置")
def get_defaults() -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    defaults = data.get("defaults", {})
    return {
        "chat": defaults.get("chat", ""),
        "reasoning": defaults.get("reasoning", ""),
        "embedding": defaults.get("embedding", ""),
    }


@router.put("/llm/defaults", summary="更新默认模型配置")
def save_defaults(body: dict[str, Any]) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    if "defaults" not in data:
        data["defaults"] = {}
    for key in ("chat", "reasoning", "embedding"):
        if key in body:
            data["defaults"][key] = body[key]
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("LLM 默认配置已更新: %s", body)
    return {
        "chat": data["defaults"].get("chat", ""),
        "reasoning": data["defaults"].get("reasoning", ""),
        "embedding": data["defaults"].get("embedding", ""),
    }


@router.post("/llm/models", summary="添加模型")
def add_model(body: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    for model_id, model_conf in body.items():
        models[model_id] = model_conf
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("添加模型: %s", list(body.keys()))
    return {"models": models}


@router.put("/llm/models/{model_id}", summary="更新模型配置")
def update_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"模型 '{model_id}' 不存在")
    models[model_id].update(body)
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("更新模型配置: %s", model_id)
    return {"models": models}


@router.delete("/llm/models/{model_id}", summary="删除模型")
def delete_model(model_id: str) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.get("models", {})
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"模型 '{model_id}' 不存在")
    del models[model_id]
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("删除模型: %s", model_id)
    return {"models": models}


@router.put("/llm/providers/{provider_id}", summary="更新提供商配置")
def update_provider(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    providers = data.setdefault("providers", {})
    if provider_id not in providers:
        providers[provider_id] = {}
    providers[provider_id].update(body)
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("更新提供商配置: %s", provider_id)
    return {"providers": providers}


# ---------------------------------------------------------------------------
# 上下文窗口配置
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_WINDOW: dict[str, Any] = {
    "version": "2.0",
    "compress_trigger_ratio": 0.5,
    "budgets": {
        "system_prompt": 0.06,
        "tools_description": 0.0,
        "static_vars": 0.03,
        "dynamic_variables": 0.03,
        "l3": 0.02,
        "l2": 0.05,
        "l1": 0.1,
        "recent": 0.18,
        "retrieval": 0.05,
        "response_reserve": 0.14,
    },
}


@router.get("/context-window", summary="获取上下文窗口配置")
def get_context_window_config() -> dict[str, Any]:
    data = _read_yaml(_CONTEXT_WINDOW_YAML)
    budgets = data.get("budgets", {})
    return {
        "max_context_length": data.get("max_context_length", 200000),
        "reserved_system_messages": data.get("reserved_system_messages", 3),
        "reserved_recent_messages": data.get("reserved_recent_messages", 10),
        "summary_threshold": data.get("compress_trigger_ratio", 0.5),
        "budgets": budgets,
        "version": data.get("version", "2.0"),
        "stability": data.get("stability", {}),
        "compression": data.get("compression", {}),
    }


@router.put("/context-window", summary="更新上下文窗口配置")
def update_context_window_config(body: dict[str, Any]) -> dict[str, Any]:
    data = _read_yaml(_CONTEXT_WINDOW_YAML)
    if "max_context_length" in body:
        data["max_context_length"] = body["max_context_length"]
    if "reserved_system_messages" in body:
        data["reserved_system_messages"] = body["reserved_system_messages"]
    if "reserved_recent_messages" in body:
        data["reserved_recent_messages"] = body["reserved_recent_messages"]
    if "summary_threshold" in body:
        data["compress_trigger_ratio"] = body["summary_threshold"]
    if "budgets" in body:
        data["budgets"] = body["budgets"]
    _write_yaml(_CONTEXT_WINDOW_YAML, data)
    logger.info("上下文窗口配置已更新")
    return get_context_window_config()


@router.post("/context-window/reset", summary="重置上下文窗口配置")
def reset_context_window_config() -> dict[str, Any]:
    _write_yaml(_CONTEXT_WINDOW_YAML, copy.deepcopy(_DEFAULT_CONTEXT_WINDOW))
    logger.info("上下文窗口配置已重置")
    return get_context_window_config()


# ---------------------------------------------------------------------------
# API 配置（运行时状态）
# ---------------------------------------------------------------------------

@router.get("/api", summary="获取 API 配置")
def get_api_config() -> dict[str, Any]:
    if _API_YAML.exists():
        return _read_yaml(_API_YAML)
    return {
        "endpoint": {
            "base_url": "http://localhost:8888",
            "version": "v1",
            "timeout": 30,
        },
        "rate_limit": {
            "global_limit": "100/minute",
            "auth": "5/minute",
            "tasks": "20/minute",
            "websocket": "50/minute",
        },
        "cors_origins": ["*"],
    }


@router.put("/api", summary="更新 API 配置")
def save_api_config(body: dict[str, Any]) -> dict[str, Any]:
    _write_yaml(_API_YAML, body)
    logger.info("API 配置已更新")
    return body


# ---------------------------------------------------------------------------
# 并发配置
# ---------------------------------------------------------------------------

@router.get("/concurrency", summary="获取并发配置")
def get_concurrency_config() -> dict[str, Any]:
    if _CONCURRENCY_YAML.exists():
        return _read_yaml(_CONCURRENCY_YAML)
    data = _read_yaml(_LLM_YAML)
    conc = data.get("concurrency", {})
    return {
        "task": {
            "max_concurrent_tasks": conc.get("default_concurrency", 3),
            "task_max_workers": 4,
            "task_timeout": 600,
        },
        "agent": {
            "l1_max_concurrent": 2,
            "l2_max_concurrent": 4,
            "l3_max_concurrent": 8,
        },
        "workflow": {
            "max_concurrent": conc.get("max_concurrency", 4),
        },
        "llm": {
            "zhipu_max_concurrent": conc.get("default_concurrency", 3),
            "openai_max_concurrent": 2,
            "anthropic_max_concurrent": 2,
            "default_max_concurrent": conc.get("min_concurrency", 1),
        },
    }


@router.put("/concurrency", summary="更新并发配置")
def save_concurrency_config(body: dict[str, Any]) -> dict[str, Any]:
    _write_yaml(_CONCURRENCY_YAML, body)
    logger.info("并发配置已更新")
    return body


# ---------------------------------------------------------------------------
# 成本控制配置
# ---------------------------------------------------------------------------

_COST_CONTROL_YAML = _CONFIG_SYSTEM_DIR / "cost_control.yaml"

_DEFAULT_COST_CONTROL: dict[str, Any] = {
    "enabled": True,
    "global_config": {
        "daily_token_limit": 1000000,
        "monthly_token_limit": 30000000,
        "per_task_token_limit": 200000,
        "per_session_token_limit": 500000,
    },
    "alerts": {
        "warning_threshold": 70,
        "critical_threshold": 90,
        "exhausted_threshold": 100,
    },
    "protection": {
        "auto_save_at_warning": True,
        "auto_pause_at_critical": True,
        "auto_stop_at_exhausted": True,
    },
}


@router.get("/cost-control", summary="获取成本控制配置")
def get_cost_control_config() -> dict[str, Any]:
    if _COST_CONTROL_YAML.exists():
        return _read_yaml(_COST_CONTROL_YAML)
    return copy.deepcopy(_DEFAULT_COST_CONTROL)


@router.put("/cost-control", summary="更新成本控制配置")
def save_cost_control_config(body: dict[str, Any]) -> dict[str, Any]:
    _write_yaml(_COST_CONTROL_YAML, body)
    logger.info("成本控制配置已更新")
    return body
