"""思考模式 API 路由。

基于 config/models/llm.yaml 中标记 reasoning_model: true 的模型，
提供思考模式切换、模型支持检查等接口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from deps import require_auth
from fastapi import APIRouter, Depends

# DEBT: config 子模块未复制到插件目录。channel_api 是独立进程入口，
# 完整运行时由 PYTHONPATH 提供。Sidecar 模式下 server.py 不导入此模块。
# ceiling: routes_thinking_mode.py 无法在 sidecar 模式下直接 import。
# upgrade: 当 config 迁移为独立插件后，通过 MCP 调用替换直接 import。
try:
    from config.models import get_model_config_loader
except ImportError:
    get_model_config_loader = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/thinking-mode",
    tags=["思考模式"],
    dependencies=[Depends(require_auth)],
)


def _resolve_project_root() -> Path:
    """向上查找项目根（含 config/ + config/models/ 的目录）。

    4c 迁移：与 routes_config._resolve_project_root 同源逻辑——本模块在
    plugins/shared/system/channel_api/ 与 src/channels/api/ 两处存在，深度不同，
    按 config/ 特征探测，避免硬编码 parent×N 差一层。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "config" / "models").is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent.parent.parent


_LLM_YAML = _resolve_project_root() / "config" / "models" / "llm.yaml"


def _get_llm_data() -> dict[str, Any]:
    """读取 llm.yaml 数据（models + defaults）。

    主进程 FastAPI 路径：用 config.models.get_model_config_loader()（带内存缓存）。
    Sidecar（http.handle）路径：loader 不可导入（None），直接读 llm.yaml（4c 迁移）。
    返回结构与 loader._load_llm_data() 对齐：{models: {...}, defaults: {...}, ...}。
    """
    if get_model_config_loader is not None:
        loader = get_model_config_loader()
        return loader._load_llm_data()
    # sidecar 兜底：直接读 YAML（无缓存，思考模式接口调用频率低）
    if not _LLM_YAML.exists():
        logger.warning("llm.yaml 不存在: %s", _LLM_YAML)
        return {"models": {}, "defaults": {}}
    with open(_LLM_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"models": {}, "defaults": {}}


def _is_reasoning_model(model_id: str) -> bool:
    data = _get_llm_data()
    model = data.get("models", {}).get(model_id)
    return bool(model and model.get("reasoning_model"))


@router.get("/health", summary="思考模式服务健康检查")
def health() -> dict[str, Any]:
    data = _get_llm_data()
    models = data.get("models", {})
    count = sum(1 for m in models.values() if m.get("reasoning_model"))
    return {"status": "ok", "available_models": count, "service": "thinking-mode"}


@router.get("/models", summary="获取支持思考模式的模型列表")
def list_models() -> list[dict[str, Any]]:
    data = _get_llm_data()
    models = data.get("models", {})
    data.get("defaults", {})
    result = []
    for mid, mconf in models.items():
        if not mconf.get("reasoning_model"):
            continue
        result.append(
            {
                "model_name": mid,
                "display_name": mconf.get("display_name", mid),
                "thinking_type": "parameter_switch",
                "base_model": mid,
                "thinking_model": mid,
                "is_same_model": True,
                "supports_reasoning_effort": True,
                "description": f"{mconf.get('display_name', mid)} 支持思考模式",
            }
        )
    return result


@router.get("/models/{model_name}", summary="获取模型思考模式信息")
def get_model_info(model_name: str) -> dict[str, Any]:
    data = _get_llm_data()
    model = data.get("models", {}).get(model_name)
    if not model:
        return {
            "model_name": model_name,
            "thinking_type": "none",
            "display_name": model_name,
            "base_model": model_name,
            "thinking_model": model_name,
            "is_same_model": True,
            "switch_description": "该模型不支持思考模式",
            "thinking_params": {},
            "normal_params": model.get("default_params", {}) if model else {},
        }

    is_reasoning = model.get("reasoning_model", False)
    default_params = model.get("default_params", {})
    thinking_params = {**default_params}
    if is_reasoning:
        thinking_params["reasoning_effort"] = 99

    return {
        "model_name": model_name,
        "thinking_type": "parameter_switch" if is_reasoning else "none",
        "display_name": model.get("display_name", model_name),
        "base_model": model_name,
        "thinking_model": model_name,
        "is_same_model": True,
        "switch_description": (
            f"启用 {model.get('display_name', model_name)} 的深度思考模式" if is_reasoning else "该模型不支持思考模式"
        ),
        "thinking_params": thinking_params,
        "normal_params": default_params,
    }


@router.get("/check/{model_name}", summary="检查模型是否支持思考模式")
def check_support(model_name: str) -> dict[str, Any]:
    data = _get_llm_data()
    model = data.get("models", {}).get(model_name)
    if not model:
        return {"model_name": model_name, "supports_thinking": False}

    is_reasoning = model.get("reasoning_model", False)
    result: dict[str, Any] = {
        "model_name": model_name,
        "supports_thinking": is_reasoning,
    }
    if is_reasoning:
        result["thinking_type"] = "parameter_switch"
        result["display_name"] = model.get("display_name", model_name)
        result["switch_description"] = f"启用 {model.get('display_name', model_name)} 的深度思考模式"
    return result


@router.post("/switch", summary="切换思考模式")
def switch_mode(body: dict[str, Any]) -> dict[str, Any]:
    current_model = body.get("current_model", "")
    enable_thinking = body.get("enable_thinking", False)

    data = _get_llm_data()
    model = data.get("models", {}).get(current_model)

    if not model:
        return {
            "target_model": current_model,
            "params": {},
            "switch_type": "none",
            "description": f"模型 {current_model} 未找到",
        }

    default_params = model.get("default_params", {})
    if enable_thinking:
        params = {**default_params, "reasoning_effort": 99}
        description = f"已启用 {model.get('display_name', current_model)} 的深度思考模式"
    else:
        params = dict(default_params)
        description = f"已关闭 {model.get('display_name', current_model)} 的思考模式"

    logger.info("思考模式切换: model=%s, enabled=%s", current_model, enable_thinking)

    return {
        "target_model": current_model,
        "params": params,
        "switch_type": "parameter_switch" if model.get("reasoning_model") else "none",
        "description": description,
    }


@router.post("/recommendations", summary="获取思考模式推荐")
def recommendations(body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = _get_llm_data()
    models = data.get("models", {})
    defaults = data.get("defaults", {})
    default_chat = defaults.get("chat", "")

    result = []
    for mid, mconf in models.items():
        if not mconf.get("reasoning_model"):
            continue
        is_default = mid == default_chat
        result.append(
            {
                "model_name": mid,
                "display_name": mconf.get("display_name", mid),
                "thinking_type": "parameter_switch",
                "suitability_score": 0.95 if is_default else 0.7,
                "optimal_params": {**mconf.get("default_params", {}), "reasoning_effort": 99},
                "best_for": ["复杂推理", "代码分析", "问题解决"],
                "tips": ["适合需要深度思考的任务"],
                "cost_estimate": f"约 {mconf.get('default_params', {}).get('max_tokens', 4096)} tokens/次",
            }
        )

    return sorted(result, key=lambda x: x["suitability_score"], reverse=True)
