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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from channels.api.deps import APIError, require_auth
from config.config_center import get_config_center
from config.models import invalidate_all_llm_caches

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/config",
    tags=["配置管理"],
    dependencies=[Depends(require_auth)],
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_ROOT = _PROJECT_ROOT / "config"
_CONFIG_MODELS_DIR = _CONFIG_ROOT / "models"
_CONFIG_SYSTEM_DIR = _CONFIG_ROOT / "system"

_LLM_YAML = _CONFIG_MODELS_DIR / "llm.yaml"
_ENV_FILE = _PROJECT_ROOT / ".env"
_CONTEXT_WINDOW_YAML = _CONFIG_SYSTEM_DIR / "context_window_config.yaml"
_API_YAML = _CONFIG_SYSTEM_DIR / "api_config.yaml"
_CONCURRENCY_YAML = _CONFIG_SYSTEM_DIR / "concurrency_config.yaml"


# ---------------------------------------------------------------------------
# Pydantic Schema 模型（S-2: 替代裸 dict[str, Any] 请求体，限制可写入字段）
# ---------------------------------------------------------------------------


class LlmDefaultsUpdateRequest(BaseModel):
    """LLM 默认模型配置更新请求。"""

    chat: str | None = None
    embedding: str | None = None
    tiers: dict[str, Any] | None = None


class ModelAddRequest(BaseModel):
    """添加模型请求，key 为模型 ID，value 为模型配置。"""

    models: dict[str, dict[str, Any]] = Field(description="模型 ID → 配置")


class ModelConfigUpdateRequest(BaseModel):
    """单模型配置更新请求，允许任意字段（透传合并到现有配置）。"""

    config: dict[str, Any] = Field(description="模型配置字段")


class ProviderConfigUpdateRequest(BaseModel):
    """提供商配置更新请求，允许任意字段（透传合并到现有配置）。"""

    config: dict[str, Any] = Field(description="提供商配置字段")


class ProviderCreateRequest(BaseModel):
    """创建提供商请求，包含 provider_id 和完整配置。

    若 config 中包含 ``api_key``，将自动写入 .env 文件，
    llm.yaml 中对应 key 改为 ``${PROVIDER_ID_UPPER}_API_KEY`` 引用格式。
    """

    provider_id: str = Field(description="提供商唯一标识（如 deepseek）")
    config: dict[str, Any] = Field(description="提供商完整配置")


class ContextWindowUpdateRequest(BaseModel):
    """上下文窗口配置更新请求，仅允许白名单字段。"""

    max_context_length: int | None = None
    compress_trigger_ratio: float | None = None
    budgets: dict[str, Any] | None = None
    compression: dict[str, Any] | None = None
    layer_order: list[str] | None = None
    include_tools_description_in_prompt: bool | None = None
    static_vars: dict[str, Any] | None = None
    dynamic_vars: dict[str, Any] | None = None
    custom_layers: dict[str, Any] | None = None


class GenericConfigUpdateRequest(BaseModel):
    """通用配置更新请求，data 为完整配置内容（白名单校验路径）。"""

    data: dict[str, Any] = Field(description="配置文件完整内容")


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

    # 通知 ConfigCenter 重载（best-effort：单例懒加载，失败仅记录不影响写入）
    try:
        get_config_center().reload(str(path))
    except Exception:
        logger.warning("ConfigCenter reload 失败: %s", path, exc_info=True)


def _mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


# ---------------------------------------------------------------------------
# .env 文件读写工具
# ---------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    """读取 .env 文件，返回 key=value 字典（跳过注释和空行）。

    Args:
        path: .env 文件路径

    Returns:
        变量名字典；文件不存在时返回空字典
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def _update_env_var(path: Path, var_name: str, var_value: str) -> None:
    """在 .env 文件中更新或添加一个环境变量，保留已有内容和注释。

    文件不存在时创建。同名变量更新值，新变量追加到文件末尾。

    Args:
        path: .env 文件路径
        var_name: 变量名（如 ``DEEPSEEK_API_KEY``）
        var_value: 变量值
    """
    existing = _read_env_file(path)
    existing[var_name] = var_value

    lines: list[str] = []
    if path.exists():
        current_vars = set(existing.keys())
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in current_vars:
                    lines.append(f"{key}={existing[key]}")
                    current_vars.discard(key)
                    continue
            lines.append(line)
        for key in current_vars:
            lines.append(f"{key}={existing[key]}")
    else:
        lines.append(f"{var_name}={var_value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------


@router.get("/llm", summary="获取完整 LLM 配置")
def get_llm_config() -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    # 脱敏 providers 中 keys 数组的 api_key
    providers = data.get("providers", {})
    masked = {}
    for pid, pconf in providers.items():
        m = copy.deepcopy(pconf)
        for key_entry in m.get("keys", []):
            if "api_key" in key_entry:
                key_entry["api_key"] = _mask_key(key_entry["api_key"])
        masked[pid] = m
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
        keys = pconf.get("keys", [])
        first_key = keys[0] if keys else {}
        result[pid] = {
            "api_base": pconf.get("api_base", ""),
            "has_key": bool(first_key.get("api_key")),
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
        "embedding": defaults.get("embedding", ""),
        "tiers": defaults.get("tiers", {}),
    }


@router.put("/llm/defaults", summary="更新默认模型配置")
def save_defaults(body: LlmDefaultsUpdateRequest) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    if "defaults" not in data:
        data["defaults"] = {}
    if body.chat is not None:
        data["defaults"]["chat"] = body.chat
    if body.embedding is not None:
        data["defaults"]["embedding"] = body.embedding
    if body.tiers is not None:
        data["defaults"]["tiers"] = body.tiers
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("LLM 默认配置已更新: %s", body.model_dump(exclude_none=True))
    return {
        "chat": data["defaults"].get("chat", ""),
        "embedding": data["defaults"].get("embedding", ""),
        "tiers": data["defaults"].get("tiers", {}),
    }


@router.post("/llm/models", summary="添加模型")
def add_model(body: ModelAddRequest) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    for model_id, model_conf in body.models.items():
        models[model_id] = model_conf
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("添加模型: %s", list(body.models.keys()))
    return {"models": models}


@router.put("/llm/models/{model_id}", summary="更新模型配置")
def update_model(model_id: str, body: ModelConfigUpdateRequest) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"模型 '{model_id}' 不存在")
    models[model_id].update(body.config)
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


@router.post("/llm/providers", summary="添加提供商")
def add_provider(body: ProviderCreateRequest) -> dict[str, Any]:
    """创建新 provider。

    若 config 中包含 ``api_key``，将 key 写入项目根目录 .env 文件，
    llm.yaml 中对应 ``keys[0].api_key`` 改为 ``${PROVIDER_ID_UPPER}_API_KEY`` 引用格式。

    Raises:
        HTTPException 409: provider_id 已存在
    """
    data = _read_yaml(_LLM_YAML)
    providers = data.setdefault("providers", {})
    if body.provider_id in providers:
        raise HTTPException(status_code=409, detail=f"提供商 '{body.provider_id}' 已存在")

    provider_config = copy.deepcopy(body.config)
    raw_api_key = provider_config.pop("api_key", None)
    if raw_api_key:
        env_var_name = f"{body.provider_id.upper()}_API_KEY"
        _update_env_var(_ENV_FILE, env_var_name, raw_api_key)
        provider_config["keys"] = [{"id": f"{body.provider_id}_main", "api_key": f"${{{env_var_name}}}"}]

    providers[body.provider_id] = provider_config
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("添加提供商: %s", body.provider_id)
    return {"providers": providers}


@router.put("/llm/providers/{provider_id}", summary="更新提供商配置")
def update_provider(provider_id: str, body: ProviderConfigUpdateRequest) -> dict[str, Any]:
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    if provider_id not in providers:
        raise HTTPException(status_code=404, detail=f"提供商 '{provider_id}' 不存在")
    providers[provider_id].update(body.config)
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("更新提供商配置: %s", provider_id)
    return {"providers": providers}


@router.delete("/llm/providers/{provider_id}", summary="删除提供商")
def delete_provider(provider_id: str) -> dict[str, Any]:
    """删除指定 provider。

    Raises:
        HTTPException 404: provider_id 不存在
    """
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    if provider_id not in providers:
        raise HTTPException(status_code=404, detail=f"提供商 '{provider_id}' 不存在")
    del providers[provider_id]
    _write_yaml(_LLM_YAML, data)
    invalidate_all_llm_caches()
    logger.info("删除提供商: %s", provider_id)
    return {"providers": providers}


# ---------------------------------------------------------------------------
# 上下文窗口配置
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_WINDOW: dict[str, Any] = {
    "version": "2.0",
    "compress_trigger_ratio": 0.55,
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
    "include_tools_description_in_prompt": False,
    "templates": {},
    "stability": {
        "system_prompt": "stable",
        "tools_description": "stable",
        "static_vars": "stable",
        "l3_memory": "semi_stable",
        "l2_memory": "semi_stable",
        "l1_memory": "semi_stable",
        "recent_messages": "dynamic",
        "dynamic_vars": "dynamic",
    },
    "budget_mapping": {},
    "layer_order": [
        "system_prompt",
        "tools_description",
        "static_vars",
        "l3",
        "l2",
        "l1",
        "recent",
        "dynamic_variables",
    ],
    "static_vars": {"enabled": True, "sources": []},
    "dynamic_vars": {
        "enabled": True,
        "vars": ["Date", "Time", "Knowledge", "Retrieval", "Rules"],
        "rules": {"enabled": True, "hard_constraints": [], "max_rules": 10},
    },
    "compression": {
        "enabled": True,
        "model": "",
        "layer_trigger_ratio": 0.8,
        "max_turn_ratio": 0.5,
    },
    "custom_layers": {},
}


@router.get("/context-window", summary="获取上下文窗口配置")
def get_context_window_config() -> dict[str, Any]:
    """返回完整的上下文窗口配置，字段与 YAML 文件一一对应。"""
    return _read_yaml(_CONTEXT_WINDOW_YAML)


@router.put("/context-window", summary="更新上下文窗口配置")
def update_context_window_config(body: ContextWindowUpdateRequest) -> dict[str, Any]:
    """合并前端提交的字段到现有配置，支持 budgets/compression 等嵌套对象。"""
    data = _read_yaml(_CONTEXT_WINDOW_YAML)
    _EDITABLE_KEYS = {  # noqa: N806
        "max_context_length",
        "compress_trigger_ratio",
        "budgets",
        "compression",
        "layer_order",
        "include_tools_description_in_prompt",
        "static_vars",
        "dynamic_vars",
        "custom_layers",
    }
    body_data = body.model_dump(exclude_none=True)
    for key in _EDITABLE_KEYS:
        if key in body_data:
            data[key] = body_data[key]
    _write_yaml(_CONTEXT_WINDOW_YAML, data)
    logger.info("上下文窗口配置已更新: %s", list(body_data.keys()))
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
            "base_url": "http://localhost:8988",
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
def save_api_config(body: GenericConfigUpdateRequest) -> dict[str, Any]:
    _write_yaml(_API_YAML, body.data)
    logger.info("API 配置已更新")
    return body.data


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
def save_concurrency_config(body: GenericConfigUpdateRequest) -> dict[str, Any]:
    _write_yaml(_CONCURRENCY_YAML, body.data)
    logger.info("并发配置已更新")
    return body.data


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
def save_cost_control_config(body: GenericConfigUpdateRequest) -> dict[str, Any]:
    _write_yaml(_COST_CONTROL_YAML, body.data)
    logger.info("成本控制配置已更新")
    return body.data


# ---------------------------------------------------------------------------
# 通用配置端点（白名单模式，供前端 GenericConfigPage 使用）
# ---------------------------------------------------------------------------

_GENERIC_CONFIG_WHITELIST: dict[str, Path] = {
    "system/api_config": _CONFIG_SYSTEM_DIR / "api_config.yaml",
    "system/concurrency_config": _CONFIG_SYSTEM_DIR / "concurrency_config.yaml",
    "system/context_window_config": _CONFIG_SYSTEM_DIR / "context_window_config.yaml",
    "system/cost_control": _CONFIG_SYSTEM_DIR / "cost_control.yaml",
    "system/memory_storage": _CONFIG_SYSTEM_DIR / "memory_storage.yaml",
    "system/editor_config": _CONFIG_SYSTEM_DIR / "editor_config.yaml",
    "system/long_term_task": _CONFIG_SYSTEM_DIR / "long_term_task.yaml",
    "models/media_providers": _CONFIG_MODELS_DIR / "media_providers.yaml",
    "isolation/isolation_config": _CONFIG_ROOT / "isolation" / "isolation_config.yaml",
    "isolation/isolation_policy": _CONFIG_ROOT / "isolation" / "isolation_policy.yaml",
    "isolation/security_rules": _CONFIG_ROOT / "isolation" / "security_rules.yaml",
    "isolation/approval": _CONFIG_ROOT / "isolation" / "approval.yaml",
    "evaluation/evaluation_metrics": _CONFIG_ROOT / "evaluation" / "evaluation_metrics.yaml",
    "capability_adapters": _CONFIG_ROOT / "capability_adapters.yaml",
    "external_tools/default": _CONFIG_ROOT / "external_tools" / "default.yaml",
    "external_tools/godot": _CONFIG_ROOT / "external_tools" / "godot.yaml",
    "external_tools/vscode": _CONFIG_ROOT / "external_tools" / "vscode.yaml",
    "pipelines/default": _CONFIG_ROOT / "pipelines" / "default.yaml",
    "pipelines/l1-main": _CONFIG_ROOT / "pipelines" / "l1-main.yaml",
    "pipelines/l2-evaluator": _CONFIG_ROOT / "pipelines" / "l2-evaluator.yaml",
    "pipelines/l2-subtask": _CONFIG_ROOT / "pipelines" / "l2-subtask.yaml",
}


@router.get("/generic/{config_path:path}", summary="获取通用配置")
def get_generic_config(config_path: str) -> dict[str, Any]:
    """根据路径读取 YAML 配置文件（白名单校验）。"""
    if config_path not in _GENERIC_CONFIG_WHITELIST:
        raise HTTPException(status_code=404, detail=f"未知配置路径: {config_path}")
    return _read_yaml(_GENERIC_CONFIG_WHITELIST[config_path])


@router.put("/generic/{config_path:path}", summary="更新通用配置")
def save_generic_config(config_path: str, body: GenericConfigUpdateRequest) -> dict[str, Any]:
    """根据路径写入 YAML 配置文件（白名单校验），并触发 config_center reload。"""
    if config_path not in _GENERIC_CONFIG_WHITELIST:
        raise HTTPException(status_code=404, detail=f"未知配置路径: {config_path}")
    file_path = _GENERIC_CONFIG_WHITELIST[config_path]
    _write_yaml(file_path, body.data)

    # 触发 config_center reload，使 watcher 生效（热更新）
    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        rel = str(file_path).replace("\\", "/")
        if "config/" in rel:
            rel = rel[rel.index("config/") + len("config/") :]
        get_config_center().reload(rel)
        logger.info("通用配置已更新并触发 reload: %s", config_path)
    except Exception as e:
        logger.warning("通用配置 reload 失败: %s | error=%s", config_path, e)

    return body.data


# ---------------------------------------------------------------------------
# 手动热重载端点
# ---------------------------------------------------------------------------

# 仅允许 YAML 配置文件触发重载（防止任意文件触发，backend_rules §5.1）
_ALLOWED_RELOAD_EXTS = {".yaml", ".yml"}


@router.post(
    "/configs/{config_path:path}:reload",
    summary="手动重载配置",
    dependencies=[Depends(require_auth)],
)
def reload_config(config_path: str) -> dict[str, Any]:
    """手动触发配置文件重载。

    调用 ConfigCenter.reload() 重新读取并应用配置。

    Raises:
        APIError: 403 路径越界 / 400 类型不允许 / 404 不存在 / 400 解析失败
    """
    resolved = (_CONFIG_ROOT / config_path).resolve()
    try:
        resolved.relative_to(_CONFIG_ROOT.resolve())
    except ValueError:
        raise APIError(
            status_code=403,
            error_code="CFG_PERM_4001",
            message="路径不在允许的配置目录内",
        ) from None  # GE-8: relative_to 抛 ValueError，显式断链

    if resolved.suffix.lower() not in _ALLOWED_RELOAD_EXTS:
        raise APIError(
            status_code=400,
            error_code="CFG_TYPE_4002",
            message=f"仅支持 YAML 配置文件，得到: {resolved.suffix}",
        )

    try:
        result = get_config_center().reload(str(resolved))
    except FileNotFoundError as e:
        raise APIError(
            status_code=404,
            error_code="CFG_NOTF_4004",
            message=f"配置文件不存在: {config_path}",
        ) from e
    except ValueError as e:
        raise APIError(
            status_code=400,
            error_code="CFG_PARSE_4005",
            message=str(e),
        ) from e

    # 字段白名单：只返回可公开的元数据，过滤 ConfigCenter 内部字段（GE-7）
    return {
        "config_path": config_path,
        "config_type": result.get("config_type"),
        "success": result.get("success", False),
    }
