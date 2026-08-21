"""LLM 配置管理 API 路由（config/llm 段）——自持版，由 llm_service http.handle 分发。

迁移自 channel_api/routes_config.py 的 LLM 配置段（channel_api 退役方案
批次 1：config/llm → llm_service 插件）。api/concurrency/context-window/
generic/cost-control 段不在此处（分别随批次 0 删除 / 归 cost_control 插件）。

- 读写 config/models/llm.yaml（含 .env 的 ${VAR} 占位符解析与明文 key 落库
  语义），写入后按源语义清除内存缓存（invalidate_all_llm_caches /
  ConfigCenter.reload，best-effort null-guard——sidecar 进程内相关模块不可
  导入时跳过，与 channel_api 侧车路径行为一致）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/HTTPException，请求体由 server.py
  http.handle 解码为 dict 传入，出错抛 :class:`ConfigAPIError`（status_code/
  detail），由 server.py 统一捕获转对应 HTTP 状态（404/409/400/502 形态与
  FastAPI 版一致：body ``{"detail": ...}``）；
- 鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成，handler 不读身份。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""

from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

# DEBT: config 子模块未复制到插件目录。llm_service 是独立 sidecar 进程，
# config.config_center / config.models 不可导入时为 None，调用处已 null-guard
# （与 channel_api 侧车路径同款处理）。
try:
    from config.config_center import get_config_center
except ImportError:
    get_config_center = None  # type: ignore[assignment]
try:
    from config.models import invalidate_all_llm_caches
except ImportError:
    invalidate_all_llm_caches = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ConfigAPIError(Exception):
    """LLM 配置域业务异常，携带 HTTP 状态码与 detail（server.py 捕获转 HTTP 响应）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _invalidate_llm_caches() -> None:
    """4c 迁移：sidecar 模式下 config.models 不可导入（None），调用需 null-guard。

    channel_api 侧车路径行为不变：config.models 不可导入时跳过即可——
    sidecar 不持有 LLM 内存缓存；配置写入的即时生效由「下次直接读 YAML」
    保证（本模块所有读端点均直读磁盘）。
    """
    if invalidate_all_llm_caches is not None:
        try:
            invalidate_all_llm_caches()
        except Exception:  # noqa: BLE001
            logger.warning("invalidate_all_llm_caches 调用失败", exc_info=True)


def _resolve_project_root() -> Path:
    """向上查找项目根（含 config/ 目录的目录）。

    硬编码 parent×N 的层级深度不可靠（模块相对项目根的深度会随布局变化），
    按 config/ 目录特征向上探测定位。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "config" / "models").is_dir():
            return candidate
    # 兜底：回退 parent×4 语义。
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _resolve_project_root()
_CONFIG_MODELS_DIR = _PROJECT_ROOT / "config" / "models"

_LLM_YAML = _CONFIG_MODELS_DIR / "llm.yaml"
_ENV_FILE = _PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# YAML 读写工具
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigAPIError(status_code=404, detail=f"配置文件不存在: {path.name}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    # 通知 ConfigCenter 重载（best-effort：单例懒加载，失败仅记录不影响写入）
    try:
        get_config_center().reload(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ConfigCenter reload 失败 | path=%s err=%s", path, exc, exc_info=True)


def _mask_key(key: str) -> str:
    """脱敏 key：保留首 4 尾 4 字符，中间打码；短 key 整体打码。"""
    if not key or len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


# ---------------------------------------------------------------------------
# ${VAR} 占位符解析（.env 兜底）
# ---------------------------------------------------------------------------

# 严格的整串占位符（如 ${DEEPSEEK_API_KEY}）
_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")
# GET 接口脱敏值包含该片段；.env.example 的示例值以 your- 开头——均视为「未配置」
_MASKED_MARK = "****"
_EXAMPLE_PREFIX = "your-"

_env_file_cache: tuple[float, dict[str, str]] | None = None


def _env_file_vars() -> dict[str, str]:
    """读取项目根 .env（mtime 缓存）。内核只在启动时加载一次 .env，
    UI 后写入的变量不在进程环境里，需回退读文件才能反映最新状态。"""
    global _env_file_cache  # noqa: PLW0603
    try:
        mtime = _ENV_FILE.stat().st_mtime
    except OSError:
        return {}
    if _env_file_cache and _env_file_cache[0] == mtime:
        return _env_file_cache[1]
    vars_ = _read_env_file(_ENV_FILE)
    _env_file_cache = (mtime, vars_)
    return vars_


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


def _provider_key_status(pconf: dict[str, Any]) -> tuple[bool, str | None]:
    """计算 provider 的 (has_key, env_var)。

    has_key 按「占位符能否解析出真实 key」判定——预置提供者未填 key 时
    yaml 里已有 `${VAR}` 字符串，不能据此误报已配置。

    Returns:
        (是否已配置可用 key, 占位符变量名或 None)
    """
    keys = pconf.get("keys") or []
    raw = ""
    if keys and isinstance(keys[0], dict):
        raw = keys[0].get("api_key", "")
    if not raw:
        raw = pconf.get("api_key", "")
    resolved = _resolve_env_value(raw if isinstance(raw, str) else None)
    m = _ENV_REF_RE.match(raw.strip()) if isinstance(raw, str) and raw else None
    return bool(resolved), (m.group(1) if m else None)


def _extract_api_key_to_env(provider_id: str, provider_config: dict[str, Any]) -> None:
    """从提交的 provider 配置中提取明文 api_key 写入 .env，yaml 内改写为
    ``${PROVIDER_ID_UPPER}_API_KEY`` 占位符（原地修改 provider_config）。

    处理两种形态：顶层 ``api_key`` 字段与 ``keys[0].api_key``（「更新 Key」
    流程直接传 keys 数组）。脱敏值（含 ``****``）与示例值（``your-`` 开头）
    一律忽略，杜绝掩码值写回 yaml 污染配置。写入后同步 os.environ，
    使本进程的 has_key 判定即时生效。
    """
    env_var_name = f"{provider_id.upper()}_API_KEY"
    placeholder = f"${{{env_var_name}}}"

    raw_key = provider_config.pop("api_key", None)
    keys = provider_config.get("keys")
    if raw_key is not None:
        # 顶层 api_key：明文 → .env；掩码/示例值 → 丢弃
        if isinstance(raw_key, str) and not _is_placeholder_value(raw_key):
            _update_env_var(_ENV_FILE, env_var_name, raw_key)
            os.environ[env_var_name] = raw_key
            provider_config["keys"] = [{"id": f"{provider_id}_main", "api_key": placeholder}]
        return

    if isinstance(keys, list) and keys and isinstance(keys[0], dict):
        k0 = keys[0]
        raw_key = k0.get("api_key")
        if isinstance(raw_key, str) and raw_key and not _ENV_REF_RE.match(raw_key.strip()):
            if _is_placeholder_value(raw_key):
                # 掩码值回传：从提交中剔除，避免覆盖磁盘上的占位符
                k0.pop("api_key", None)
            else:
                _update_env_var(_ENV_FILE, env_var_name, raw_key)
                os.environ[env_var_name] = raw_key
                k0["api_key"] = placeholder


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
# LLM 配置端点（config/llm 段，原 routes_config.py 同名端点语义）
# ---------------------------------------------------------------------------


def get_llm_config() -> dict[str, Any]:
    """获取完整 LLM 配置（providers/models 脱敏 + key 配置状态）。"""
    data = _read_yaml(_LLM_YAML)
    # 脱敏 providers 中 keys 数组的 api_key，并附上 key 配置状态
    providers = data.get("providers", {})
    masked: dict[str, Any] = {}
    for pid, pconf in providers.items():
        m = copy.deepcopy(pconf)
        for key_entry in m.get("keys", []):
            if "api_key" in key_entry:
                key_entry["api_key"] = _mask_key(key_entry["api_key"])
        has_key, env_var = _provider_key_status(pconf)
        m["has_key"] = has_key
        m["env_var"] = env_var
        masked[pid] = m
    # 脱敏 models 中的 api_key
    models = data.get("models", {})
    masked_models: dict[str, Any] = {}
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


def get_providers() -> dict[str, Any]:
    """获取提供商列表（含 key 配置状态）。"""
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    result: dict[str, Any] = {}
    for pid, pconf in providers.items():
        has_key, env_var = _provider_key_status(pconf)
        result[pid] = {
            "api_base": pconf.get("api_base", ""),
            "has_key": has_key,
            "env_var": env_var,
        }
    return {"providers": result}


def get_models() -> dict[str, Any]:
    """获取模型列表（api_key 脱敏）。"""
    data = _read_yaml(_LLM_YAML)
    models = data.get("models", {})
    masked: dict[str, Any] = {}
    for mid, mconf in models.items():
        m = {**mconf}
        if "api_key" in m:
            m["api_key"] = _mask_key(m["api_key"])
        masked[mid] = m
    return {"models": masked}


def get_defaults() -> dict[str, Any]:
    """获取默认模型配置。"""
    data = _read_yaml(_LLM_YAML)
    defaults = data.get("defaults", {})
    return {
        "chat": defaults.get("chat", ""),
        "embedding": defaults.get("embedding", ""),
        "tiers": defaults.get("tiers", {}),
    }


def save_defaults(body: dict[str, Any]) -> dict[str, Any]:
    """更新默认模型配置（chat/embedding/tiers 可空字段部分更新）。"""
    data = _read_yaml(_LLM_YAML)
    if "defaults" not in data:
        data["defaults"] = {}
    if body.get("chat") is not None:
        data["defaults"]["chat"] = body["chat"]
    if body.get("embedding") is not None:
        data["defaults"]["embedding"] = body["embedding"]
    if body.get("tiers") is not None:
        data["defaults"]["tiers"] = body["tiers"]
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("LLM 默认配置已更新: %s", body)
    return {
        "chat": data["defaults"].get("chat", ""),
        "embedding": data["defaults"].get("embedding", ""),
        "tiers": data["defaults"].get("tiers", {}),
    }


def add_model(body: dict[str, Any]) -> dict[str, Any]:
    """添加模型。

    Raises:
        ConfigAPIError 409: model_id 已存在（避免静默覆盖既有配置）
    """
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    model_entries = body.get("models") or {}
    # 写入前逐个检测：已存在的 model_id 视为冲突，避免静默覆盖既有配置。
    for model_id in model_entries:
        if model_id in models:
            raise ConfigAPIError(status_code=409, detail=f"模型 '{model_id}' 已存在")
    for model_id, model_conf in model_entries.items():
        models[model_id] = model_conf
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("添加模型: %s", list(model_entries.keys()))
    return {"models": models}


def update_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新模型配置（config 字段透传合并）。

    Raises:
        ConfigAPIError 404: 模型不存在
    """
    data = _read_yaml(_LLM_YAML)
    models = data.setdefault("models", {})
    if model_id not in models:
        raise ConfigAPIError(status_code=404, detail=f"模型 '{model_id}' 不存在")
    models[model_id].update(body.get("config") or {})
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("更新模型配置: %s", model_id)
    return {"models": models}


def delete_model(model_id: str) -> dict[str, Any]:
    """删除模型。

    Raises:
        ConfigAPIError 404: 模型不存在
    """
    data = _read_yaml(_LLM_YAML)
    models = data.get("models", {})
    if model_id not in models:
        raise ConfigAPIError(status_code=404, detail=f"模型 '{model_id}' 不存在")
    del models[model_id]
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("删除模型: %s", model_id)
    return {"models": models}


def add_provider(body: dict[str, Any]) -> dict[str, Any]:
    """创建新 provider。

    若 config 中包含 ``api_key``（顶层或 keys[0]），将 key 写入项目根目录
    .env 文件，llm.yaml 中对应值改写为 ``${PROVIDER_ID_UPPER}_API_KEY``
    占位符（见 ``_extract_api_key_to_env``）。

    Raises:
        ConfigAPIError 409: provider_id 已存在
    """
    data = _read_yaml(_LLM_YAML)
    providers = data.setdefault("providers", {})
    provider_id = body.get("provider_id", "")
    if provider_id in providers:
        raise ConfigAPIError(status_code=409, detail=f"提供商 '{provider_id}' 已存在")

    provider_config = copy.deepcopy(body.get("config") or {})
    _extract_api_key_to_env(provider_id, provider_config)

    providers[provider_id] = provider_config
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("添加提供商: %s", provider_id)
    return {"providers": providers}


def update_provider(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新 provider 配置。

    提交中的明文 ``api_key``（顶层或 ``keys[0].api_key``）与添加流程同义：
    写入 .env、yaml 保持 ``${VAR}`` 占位符；脱敏/示例值被忽略，
    防止 GET 返回的掩码值经保存回路污染 yaml。

    ``keys`` 数组按条目与磁盘现状合并（未提交的字段保留原值）——前端
    只改并发/RPM 时不带 api_key，合并保证磁盘上的占位符不被清掉。

    Raises:
        ConfigAPIError 404: 提供商不存在
    """
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    if provider_id not in providers:
        raise ConfigAPIError(status_code=404, detail=f"提供商 '{provider_id}' 不存在")
    provider_config = copy.deepcopy(body.get("config") or {})
    _extract_api_key_to_env(provider_id, provider_config)

    # keys 条目合并：按索引把提交项覆盖到磁盘现状上（api_key 等
    # 未提交/被剔除的字段保留磁盘值，避免整组替换丢占位符）
    new_keys = provider_config.get("keys")
    existing_keys = providers[provider_id].get("keys")
    if isinstance(new_keys, list) and isinstance(existing_keys, list) and existing_keys:
        merged: list[Any] = []
        for i, entry in enumerate(new_keys):
            if isinstance(entry, dict):
                base = (
                    dict(existing_keys[i])
                    if i < len(existing_keys) and isinstance(existing_keys[i], dict)
                    else {}
                )
                base.update({k: v for k, v in entry.items() if v is not None})
                merged.append(base)
            else:
                merged.append(entry)
        provider_config["keys"] = merged

    providers[provider_id].update(provider_config)
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("更新提供商配置: %s", provider_id)
    return {"providers": providers}


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
    data = _read_yaml(_LLM_YAML)
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
            base = api_base or "https://api.anthropic.com"
            if not base.endswith("/v1"):
                base += "/v1"
            resp = httpx.get(
                f"{base}/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
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
    models: list[dict[str, str]] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                models.append(
                    {"id": str(item["id"]), "owned_by": str(item.get("owned_by") or "")}
                )
    models.sort(key=lambda m: m["id"])
    return {"provider": provider_id, "models": models}


def delete_provider(provider_id: str) -> dict[str, Any]:
    """删除指定 provider。

    Raises:
        ConfigAPIError 404: provider_id 不存在
    """
    data = _read_yaml(_LLM_YAML)
    providers = data.get("providers", {})
    if provider_id not in providers:
        raise ConfigAPIError(status_code=404, detail=f"提供商 '{provider_id}' 不存在")
    del providers[provider_id]
    _write_yaml(_LLM_YAML, data)
    _invalidate_llm_caches()
    logger.info("删除提供商: %s", provider_id)
    return {"providers": providers}
