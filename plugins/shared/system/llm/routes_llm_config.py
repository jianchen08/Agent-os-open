"""LLM 配置只读面（presets / provider-types / remote-models），由 llm_service
http.handle 分发。

2026-09-28 配置读写单源化（批次 A1）：llm.yaml 的读写全量收口到内核单一配置面
``GET/PUT /api/v1/plugins/llm_service/config/llm``（掩码 + ETag + 写恒用户空间 +
播种 + 接管账本），动态 provider key 经 ``PUT /api/v1/config/env`` 落用户空间
.env——本模块的文件写面与 yaml CRUD 端点**全部退役**，仅保留三个无文件写动作的
只读端点：

- get_llm_presets：配置面预置声明（前端设置页唯一来源）；
- get_provider_types：litellm 运行时类型清单；
- get_remote_models：从提供商 API 实时拉取模型（读 llm.yaml 取 key/base，
  用户空间接管文件优先）。

剥离 FastAPI 依赖：无 APIRouter/Depends/HTTPException，请求体由 server.py
http.handle 解码为 dict 传入，出错抛 :class:`ConfigAPIError`（status_code/
detail），由 server.py 统一捕获转对应 HTTP 状态；鉴权由内核 dispatcher 按
http_endpoints.auth=user 完成，handler 不读身份。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1；
 读写单源化: docs/working/LLM配置改不生效修复方案_20260928.md 批次 A1]
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

# plugins/shared 根已由 server.py bootstrap_plugin 推上 sys.path（http_json 先例）。

logger = logging.getLogger(__name__)

# Anthropic 公共 API 契约默认值（provider 未配置 api_base 时的回落基址 +
# 模型列表请求头版本号；与 anthropic 官方 SDK 默认一致，非本服务可调参数）。
_ANTHROPIC_DEFAULT_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_API_VERSION = "2023-06-01"

# 配置面预置声明（声明驱动：前端设置页唯一来源；P2-5）
_PRESETS_FILE = Path(__file__).resolve().parent / "llm_presets.yaml"

# 严格的整串占位符（如 ${DEEPSEEK_API_KEY}）
_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")
# GET 接口脱敏值包含该片段；.env.example 的示例值以 your- 开头——均视为「未配置」
_MASKED_MARK = "****"
_EXAMPLE_PREFIX = "your-"


class ConfigAPIError(Exception):
    """LLM 配置域业务异常，携带 HTTP 状态码与 detail（server.py 捕获转 HTTP 响应）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _resolve_project_root() -> Path:
    """向上查找项目根（含 config/ 目录的目录）。

    硬编码 parent×N 的层级深度不可靠（模块相对项目根的深度会随布局变化），
    按 config/ 目录特征向上探测定位；探测不中回落 parent×4（可预期、不炸）。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir():
            return candidate
    return here.parents[3] if len(here.parents) > 3 else here


_PROJECT_ROOT = _resolve_project_root()


def _user_space_path(*parts: str) -> Path | None:
    """用户空间落点（读写同源公理：读侧恒用户层优先）；不可用返回 None。"""
    try:
        from user_space import user_root  # noqa: PLC0415
    except ImportError:
        return None
    root = user_root()
    return Path(root).joinpath(*parts) if root else None


def _llm_yaml_path() -> Path:
    """llm.yaml 落点：用户空间接管文件优先，否则出厂种子。

    单一解析器语义（ADR 2026-09-13/14）：运行时生效视图 = 用户空间文件级
    整体替换。remote-models 取 provider key/base 必须与内核注入同源。
    """
    user = _user_space_path("config", "plugins", "llm", "llm.yaml")
    if user is not None and user.is_file():
        return user
    return _PROJECT_ROOT / "config" / "plugins" / "llm" / "llm.yaml"


def _env_file_path() -> Path:
    """用户空间 .env 优先（设置页 key 经内核端点写入该文件），否则出厂回落。"""
    user = _user_space_path(".env")
    if user is not None and user.is_file():
        return user
    return _PROJECT_ROOT / ".env"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigAPIError(status_code=404, detail=f"配置文件不存在: {path.name}")
    import yaml  # noqa: PLC0415

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_env_file_cache: tuple[float, dict[str, str], str] | None = None


def _env_file_vars() -> dict[str, str]:
    """读 .env 全量 key=value（mtime 缓存）。空行/注释跳过。

    文件不存在属正常形态静默返回；其余 OSError（文件被占/权限等）warn 带
    path 与异常摘要——静默空表会让 key 缺失只在远端 401 暴露，根因必须可观测。
    """
    global _env_file_cache  # noqa: PLW0603
    env_path = _env_file_path()
    try:
        mtime = env_path.stat().st_mtime
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(".env 读取失败（mtime 探测），按无变量处理 | path=%s | error=%s", env_path, exc)
        return {}
    if _env_file_cache and _env_file_cache[0] == mtime and _env_file_cache[2] == str(env_path):
        return _env_file_cache[1]
    vars_ = _read_env_file(env_path)
    _env_file_cache = (mtime, vars_, str(env_path))
    return vars_


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


def _is_placeholder_value(value: str) -> bool:
    """脱敏值或 .env.example 示例值——展示为「未配置」，绝不能写回 yaml/env。"""
    return _MASKED_MARK in value or value.startswith(_EXAMPLE_PREFIX)


def _resolve_env_value(raw: str | None) -> str | None:
    """解析 key 值：`${VAR}` → os.environ → .env 文件；明文原样返回。

    Returns:
        解析后的真实 key；未配置/占位符未展开/示例值返回 None
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _ENV_REF_RE.match(raw)
    if not m:
        return None if _is_placeholder_value(raw) else raw
    value = os.environ.get(m.group(1))
    if value is None:
        value = _env_file_vars().get(m.group(1))
    if not value or _is_placeholder_value(value):
        return None
    return value


def get_llm_presets() -> dict[str, Any]:
    """下发 LLM 配置面预置声明（provider 分组/常用类型/思考强度白名单）。

    单一真值源 = 插件目录 llm_presets.yaml（声明驱动，前端零硬编码）：
    新增预置厂商仅改声明文件 + llm.yaml，前端零改动。声明文件缺失属插件
    自身损坏，fail-closed 抛错（不回退空清单让前端静默降级成"无预置"）。

    Raises:
        ConfigAPIError 500: 声明文件缺失/解析失败
    """
    presets_file = _PRESETS_FILE
    if not presets_file.exists():
        raise ConfigAPIError(
            status_code=500,
            detail=f"LLM 预置声明文件缺失: {presets_file.name}",
        )
    import yaml  # noqa: PLC0415

    try:
        with open(presets_file, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigAPIError(
            status_code=500, detail=f"LLM 预置声明解析失败: {exc}"
        ) from exc
    return {
        "provider_groups": data.get("provider_groups", []),
        "common_provider_types": data.get("common_provider_types", []),
        "thinking_strength": data.get("thinking_strength", {"levels": [], "allowed_keys": []}),
    }


def _find_model_entry(models: dict[str, Any], model: str) -> dict[str, Any] | None:
    """按 model_name 精确匹配定位模型条目，再按 key（model_id）兜底。"""
    if not model:
        return None
    for entry in models.values():
        if isinstance(entry, dict) and entry.get("model_name") == model:
            return entry
    entry = models.get(model)
    return entry if isinstance(entry, dict) else None


def _param_leaf_values(value: Any) -> list[str]:
    """参数值 → 标量叶子值列表（嵌套字典递归展开，保持配置序）。"""
    if isinstance(value, dict):
        leaves: list[str] = []
        for child in value.values():
            leaves.extend(_param_leaf_values(child))
        return leaves
    return [str(value)]


def _render_params(params: dict[str, Any]) -> str:
    """参数组 → 选项标签：**实际字段值**直显（嵌套取标量叶子），如
    ``reasoning_effort=max`` → ``max``、``thinking={"type": "adaptive"}`` →
    ``adaptive``；多键按配置序以 " / " 连接（如 off 组 → ``disabled / none``）。"""
    parts: list[str] = []
    for value in params.values():
        parts.extend(_param_leaf_values(value))
    return " / ".join(parts)


def get_thinking_levels(model: str) -> dict[str, Any]:
    """当前模型可切的思考参数组（聊天页选择器真值源，前端零硬编码零映射）。

    选项 = thinking_strength_params 配置的**参数组本身**（无档位词汇映射层）：
    厂商级（providers.<provider>）参数组在前、模型级（models.<id>）补位，按
    参数内容去重、配置顺序即选项顺序；标签 = 参数渲染（_render_params）。

    响应 ``fields`` = 表单字段声明（select，options 即参数组）——声明渲染层
    fieldsUri 数据源的通用契约（前端选择器直接消费，渲染容器零适配）；
    ``current`` = 模型 default_params 思考参数命中的参数组 value，未匹配为
    None（前端显示值只剩「标签显式记忆 ?? current」，无任何推断）。

    选项 ``value`` = 参数组的 JSON 串（紧凑序），即消息 thinking_strength 的
    线上形态：选中即透传，llm_core 解析后白名单过滤直覆盖（无档位查表）。

    模型未命中 / 两侧均未配置 → ``fields=[]``（声明层不渲染选择器：选了也
    没有参数可覆盖）。
    """
    data = _read_yaml(_llm_yaml_path())
    entry = _find_model_entry(data.get("models", {}) or {}, model)
    if entry is None:
        return {"model": model, "fields": [], "options": [], "current": None}
    provider_conf = (data.get("providers", {}) or {}).get(entry.get("provider") or "", {})
    provider_levels = (
        provider_conf.get("thinking_strength_params") if isinstance(provider_conf, dict) else None
    ) or {}
    model_levels = entry.get("thinking_strength_params") or {}

    # 参数组按内容去重（厂商组在前），插入顺序 = 选项顺序
    seen: set[str] = set()
    options: list[dict[str, Any]] = []
    for mapping in (provider_levels, model_levels):
        for params in mapping.values():
            if not isinstance(params, dict) or not params:
                continue
            value = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value in seen:
                continue
            seen.add(value)
            options.append({"value": value, "params": params, "label": _render_params(params)})

    current = _match_params(options, entry.get("default_params") or {})
    fields = (
        [{"name": "strength", "type": "select", "options": options}] if options else []
    )
    return {"model": model, "fields": fields, "options": options, "current": current}


def _match_params(
    options: list[dict[str, Any]], default_params: dict[str, Any]
) -> str | None:
    """模型 default_params 思考参数 → 命中参数组的 value（JSON 串；不匹配 None）。

    reasoning_effort 精确相等优先；其次 thinking.type 精确相等（覆盖厂商
    关闭形态 thinking.type=disabled 的参数组）。
    """
    effort = default_params.get("reasoning_effort")
    thinking = default_params.get("thinking")
    thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
    if effort is not None:
        for option in options:
            if option["params"].get("reasoning_effort") == effort:
                return option["value"]
    if thinking_type is not None:
        for option in options:
            thinking = option["params"].get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") == thinking_type:
                return option["value"]
    return None


def get_provider_types() -> dict[str, Any]:
    """获取 litellm 支持的提供者类型清单。

    运行时读取已安装 litellm 的 ``provider_list``。
    litellm pip 升级后新提供者自动出现，前端「添加自定义提供商」的类型
    下拉直接消费此清单。读取失败时回退常用核心类型。
    """
    try:
        import litellm  # noqa: PLC0415

        # provider_list 是 LlmProviders 枚举；Python 3.12 下 str() 会得到
        # "LlmProviders.X" 而非值本身，统一取 .value
        types = sorted(
            {
                str(getattr(p, "value", p))
                for p in litellm.provider_list
            }
        )
    except Exception:  # noqa: BLE001
        logger.warning("读取 litellm.provider_list 失败，回退核心类型", exc_info=True)
        types = ["anthropic", "deepseek", "minimax", "openai", "zai"]
    return {"types": types}


def get_remote_models(provider_id: str) -> dict[str, Any]:
    """从提供商 API 实时拉取可用模型。

    - anthropic 类型：``GET {api_base}/v1/models``（``x-api-key`` 头）
    - 其余（OpenAI 兼容）：``GET {api_base}/models``（Bearer 头）

    Raises:
        ConfigAPIError 404: 提供商不存在
        ConfigAPIError 400: 未配置 API Key
        ConfigAPIError 502: 上游请求失败（提示用户可手动输入模型名）
    """
    data = _read_yaml(_llm_yaml_path())
    pconf = data.get("providers", {}).get(provider_id)
    if pconf is None:
        raise ConfigAPIError(status_code=404, detail=f"提供商 '{provider_id}' 不存在")

    keys = pconf.get("keys") or []
    raw_key = keys[0].get("api_key", "") if keys and isinstance(keys[0], dict) else ""
    api_key = _resolve_env_value(raw_key if isinstance(raw_key, str) else None)
    if not api_key:
        raise ConfigAPIError(
            status_code=400,
            detail=f"提供商 '{provider_id}' 尚未配置可用的 API Key，请先填写",
        )

    api_base = str(pconf.get("api_base") or "").rstrip("/")
    ptype = pconf.get("type", "openai")

    import httpx  # noqa: PLC0415

    try:
        if ptype == "anthropic":
            base = api_base or _ANTHROPIC_DEFAULT_API_BASE
            if not base.endswith("/v1"):
                base += "/v1"
            resp = httpx.get(
                f"{base}/models",
                headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_API_VERSION},
                timeout=8.0,
            )
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = httpx.get(f"{api_base}/models", headers=headers, timeout=8.0)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        raise ConfigAPIError(
            status_code=502,
            detail=f"拉取模型列表失败（HTTP {exc.response.status_code}），可手动输入模型名",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ConfigAPIError(
            status_code=502, detail=f"拉取模型列表失败：{exc}；可手动输入模型名"
        ) from exc

    items = payload.get("data") if isinstance(payload, dict) else None
    if items is None and isinstance(payload, dict):
        items = payload.get("models")
    models: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                model_id = str(item["id"])
                entry: dict[str, Any] = {
                    "id": model_id,
                    "owned_by": str(item.get("owned_by") or ""),
                }
                entry.update(_lookup_model_limits(ptype, model_id))
                models.append(entry)
    models.sort(key=lambda m: m["id"])
    return {"provider": provider_id, "models": models}


def _lookup_model_limits(provider_type: str, model_id: str) -> dict[str, int]:
    """litellm 注册表查模型真实上限（context_window / max_output_tokens）。

    厂商 ``/models`` 端点只返回 id/object/owned_by，不携带上下文与输出上限
    ——上限事实取自 litellm 内置注册表，键为 ``{provider_type}/{model_id}``
    精确匹配。查不到返回空 dict（调用方用保守默认，不发明数字）。

    不做后缀模糊匹配：同名模型挂在不同厂商下时上限不同（如 ollama 的
    glm-5.2 与 dashscope 的 glm-5.2），跨厂商套用会把别人的上限当自己的事实。
    """
    if not provider_type or not model_id:
        return {}
    try:
        import litellm  # noqa: PLC0415

        info = litellm.model_cost.get(f"{provider_type}/{model_id}")
    except Exception:  # noqa: BLE001 —— 注册表读取失败按"无事实"降级
        logger.warning("litellm 模型注册表读取失败，模型 %s 上限未知", model_id, exc_info=True)
        return {}
    if not isinstance(info, dict):
        return {}
    limits: dict[str, int] = {}
    context_window = info.get("max_input_tokens")
    max_output = info.get("max_output_tokens")
    if isinstance(context_window, int) and context_window > 0:
        limits["context_window"] = context_window
    if isinstance(max_output, int) and max_output > 0:
        limits["max_output_tokens"] = max_output
    return limits
