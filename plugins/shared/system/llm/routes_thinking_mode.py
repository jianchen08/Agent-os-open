"""思考模式 API 路由（thinking-mode 域），由 llm_service http.handle 分发。

- 基于 config/models/llm.yaml 中标记 ``reasoning_model: true`` 的模型，
  提供思考模式切换、模型支持检查等接口；响应形态与
  /ext/channel_api/thinking-mode/** 逐项对齐（前端直接消费）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/require_auth，返回纯 dict，
  请求体由 server.py http.handle 解码为 dict 传入（switch/recommendations）；
- 数据读取：本模块只以 sidecar（http.handle）形态运行，故恒走「直接读
  llm.yaml」路径（sidecar 无 config.models loader 内存缓存；直接读 YAML
  保证配置写入后立即生效）；
- 出错抛 :class:`ThinkingModeAPIError`（status_code/error_code/message），
  由 server.py http.handle 统一捕获转对应 HTTP 状态；
- 鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成，handler 不读身份。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ThinkingModeAPIError(Exception):
    """thinking-mode 域业务异常，携带 HTTP 状态码与错误码（server.py 捕获转 HTTP 响应）。"""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def _resolve_project_root() -> Path:
    """向上查找项目根（含 config/ + config/models/ 的目录）。

    按 config/ 特征探测，避免硬编码 parent×N（本模块在
    plugins/shared/system/llm/）。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "config" / "models").is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent.parent.parent


_LLM_YAML = _resolve_project_root() / "config" / "models" / "llm.yaml"


def _get_llm_data() -> dict[str, Any]:
    """读取 llm.yaml 数据（models + defaults）。

    本模块只走 sidecar（http.handle）形态，恒直接读 YAML（无内存缓存，
    思考模式接口调用频率低，且保证配置写入后立即生效）。返回结构：
    {models: {...}, defaults: {...}, ...}。
    """
    if not _LLM_YAML.exists():
        logger.warning("llm.yaml 不存在: %s", _LLM_YAML)
        return {"models": {}, "defaults": {}}
    with open(_LLM_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"models": {}, "defaults": {}}


def health() -> dict[str, Any]:
    """思考模式服务健康检查。"""
    data = _get_llm_data()
    models = data.get("models", {})
    count = sum(1 for m in models.values() if m.get("reasoning_model"))
    return {"status": "ok", "available_models": count, "service": "thinking-mode"}


def list_models() -> list[dict[str, Any]]:
    """获取支持思考模式的模型列表。"""
    data = _get_llm_data()
    models = data.get("models", {})
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


def get_model_info(model_name: str) -> dict[str, Any]:
    """获取模型思考模式信息。"""
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


def check_support(model_name: str) -> dict[str, Any]:
    """检查模型是否支持思考模式。"""
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


def switch_mode(body: dict[str, Any]) -> dict[str, Any]:
    """切换思考模式（生成目标参数，不落盘——原语义）。"""
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


def recommendations(body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """获取思考模式推荐。"""
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
