"""LLM 插件配置注入桥（task_11 P0-3）。

本模块是 sidecar 内部的配置注入桥，供三处复用：
- ``server.py::_on_load`` 调 ``set_config(config)`` 注入内核下发的配置；
- ``router_factory._ensure_provider_type_map_loaded`` 和
  ``adapter.KeyPoolAdapter._route_call`` 调 ``get_model_config_loader()``
  拿配置 loader（暴露 ``load_llm_data``）；
- ``pipeline/core/llm_core/plugin.py`` 经 ``get_model_config_loader()``
  消费 ``get_llm_core_config`` / ``resolve_tier`` / ``get_default_chat_model``。

配置结构（P1：manifest ``config_files`` 映射，按 id 命名空间合并后注入）::

    {"llm": <llm.yaml 内容>, "embedding": <embedding.yaml 内容>}

``load_llm_data`` 返回 ``config["llm"]``，即 ``llm.yaml`` 的完整内容
（含顶层 ``models`` / ``providers`` / ``defaults`` / ``concurrency`` 键）。

[来源: docs/tasks/task_11_plugin_capability_unification.md P1-3；ADR §4.3 B3]
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 模块级配置存储（由 set_config 注入，进程内单例）
_config: dict[str, Any] = {}


class ModelIdAmbiguousError(ValueError):
    """模型标识歧义：同一 model_name 对应多个 model id。

    模型 id（llm.yaml models 段的 key）是全系统唯一标识；model_name（上游
    模型名）只是展示与上游请求用，**不作查表键**。反查（name→id）只在兼容
    兜底面用，且命中多个条目时**不猜**——猜错会把调用打到另一个 provider
    （网关道 vs 直连道，凭证/计价/缓存前缀全不同），宁可报错让调用方显式
    改用 model id。
    """

# 整串 ${VAR} 占位符（ADR §4.3 secrets）
_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")

# 生效 .env 文件缓存：(mtime, vars, path)；path 入键——用户空间 .env 出现/消失
# 使落点切换，仅比 mtime 会把旧文件的缓存误配到新文件。
_env_cache: tuple[float, dict[str, str], str] | None = None


def _resolve_project_root() -> Path | None:
    """向上探测项目根（包含 config/kernel 的目录）。

    解析单源在 plugins/shared/project_root.py（env 优先→向上探测→缓存，
    F-2 收敛；探测锚定本文件位置，与收敛前一致）；找不到返回 None（保持
    纯环境变量展开行为，不比原来差）。延迟导入与 user_space 先例同款
    （本模块可被仅挂插件目录的加载方导入）。
    """
    from _root_api import resolve_project_root  # noqa: PLC0415

    here = Path(__file__).resolve().parent
    return resolve_project_root("config/kernel", start=here)


def env_file_path() -> Path | None:
    """.env 落点：用户空间 .env 优先，回落项目根 .env；两者皆不可得返回 None。

    用户空间优先与内核同款（``agentos_mcp::env_file::env_path_for_root``：
    设置页写面 ``PUT /api/v1/config/env``、启动加载、spawn 叠加均落/读用户
    空间）——.env 兜底读同一文件，设置页写入的 key 才对兜底路径可见。
    """
    try:
        from _root_api import user_root  # noqa: PLC0415

        root = user_root()
    except ImportError:
        root = None
    if root is not None:
        user_env = Path(root) / ".env"
        if user_env.is_file():
            return user_env
    project = _resolve_project_root()
    if project is None:
        return None
    return project / ".env"


def env_file_vars() -> dict[str, str]:
    """读生效 .env（用户空间优先，见 :func:`env_file_path`）全量 key=value。

    FileNotFoundError（无 .env）属正常形态静默返回；其余 OSError（文件被占/
    权限等）warn 带 path 与异常摘要——静默空表会让 key 缺失只在远端 401 暴露，
    根因必须可观测。读失败结果不入缓存（下次重读）。
    """
    global _env_cache  # noqa: PLW0603
    env_path = env_file_path()
    if env_path is None:
        return {}
    try:
        mtime = env_path.stat().st_mtime
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(".env 读取失败（mtime 探测），按无变量处理 | path=%s | error=%s", env_path, exc)
        return {}
    if _env_cache and _env_cache[0] == mtime and _env_cache[2] == str(env_path):
        return _env_cache[1]
    result = read_env_file(env_path)
    if result is None:  # 读失败（已告警）不入缓存，下次重读
        return {}
    _env_cache = (mtime, result, str(env_path))
    return result


def read_env_file(path: Path) -> dict[str, str] | None:
    """读 .env 文本为 key=value 字典（跳过注释与空行，值剥首尾双引号）。

    剥引号与内核写面对齐（``env_file::write_env_updates`` 对含空白/# 的值加
    双引号、``parse_env_text`` 去引号回读）——读侧不去引号会把引号当值的一
    部分发上游。

    Returns:
        键值字典；stat 后文件被删（读取期竞态，FileNotFoundError）静默返回
        ``None``（等同无 .env，不告警）；其余 OSError warn 带 path 与异常摘要
        后返回 ``None``——调用方对 ``None`` 一律不入缓存（下次重读）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(".env 读取失败，按无变量处理 | path=%s | error=%s", path, exc)
        return None
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip().strip('"')
    return result


def _expand_env_vars(value: Any) -> Any:
    """递归解析配置值里的 ``${VAR}`` 占位符为真实 key。

    解析顺序：进程环境变量（sidecar 继承内核父进程环境 + 内核 spawn 时经
    ``env_delta_overlay`` 叠加的 .env 增量）→ 生效 .env 文件兜底（用户空间
    优先，与内核写面同源，见 :func:`env_file_path`）。.env 兜底使 sidecar
    重启（.env mtime 指纹触发的热重启）后即可拿到设置页新 key，无需重启内核。

    未定义的变量保持 ``${VAR}`` 原样（``expandvars`` 行为），路由构建侧
    以 ``UNRESOLVED:`` 指纹记日志定位。.env.example 的示例值
    （``your-`` 开头）同样不视为已配置。

    递归处理 dict / list / str 三种类型，其他类型原样返回。
    """
    if isinstance(value, str):
        m = _ENV_REF_RE.match(value.strip())
        if m:
            var = m.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                resolved = env_file_vars().get(var)
            if resolved is None:
                return value
            if resolved.startswith("your-"):
                # .env.example 示例值——视为未配置，保留占位符
                return value
            return resolved
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def set_config(config: dict[str, Any]) -> None:
    """注入内核下发的插件配置。

    注入时递归解析 ``${VAR}`` 占位符（ADR §4.3 secrets：占位符解析在
    sidecar 收到配置后进行，使 router_factory 拿到真实 key）。

    Args:
        config: 内核经 ``config_files`` 映射合并后注入的配置字典（P1 起）。
    """
    global _config  # noqa: PLW0603
    _config = _expand_env_vars(config) if isinstance(config, dict) else {}


def get_config() -> dict[str, Any]:
    """获取当前注入的配置（调试用）。"""
    return _config


def get_model_config_loader() -> ModelConfigLoaderShim:
    """返回基于当前注入配置的 loader 实例。

    消费方：router_factory / adapter（``load_llm_data()``，返回当前注入
    配置中的 ``llm`` 命名空间节）与 llm_core（``get_llm_core_config`` /
    ``resolve_tier`` / ``get_default_chat_model``）。
    """
    return ModelConfigLoaderShim(_config)


def resolve_model_id(model_id_hint: str, model_tier: str) -> str:
    """按统一优先级解析最终模型 id：显式 model_id > tier→defaults.tiers > defaults.chat。

    单源位置：llm_core 选模（``_apply_model_from_state``）与 ``model.resolve``
    服务处理器共用本函数，消费方（agent_config_load 落定步等）不复制解析链。
    全空（未表达模型意图 / llm.yaml 无 defaults）返回空串，由调用方保持
    自身默认。
    """
    if model_id_hint:
        return model_id_hint
    loader = get_model_config_loader()
    if model_tier:
        resolved = loader.resolve_tier(model_tier)
        if resolved:
            return resolved
    return loader.get_default_chat_model()


def normalize_model_id(model: str) -> str:
    """模块级模型标识归一（跨插件可达的归一单点）。

    见 :meth:`ModelConfigLoaderShim.normalize_model_id`：模型 id 是全系统
    唯一标识，model_name 只作展示/上游串；本函数把任意形态的模型标识归一
    为 model id，供 llm 插件、llm_core、context_window_guard 等消费方在
    查表/路由前统一调用。
    """
    return get_model_config_loader().normalize_model_id(model)


class ModelConfigLoaderShim:
    """基于注入配置的模型配置 loader。

    ``router_factory.build_router`` / ``build_adapter`` 及
    ``adapter.KeyPoolAdapter._route_call`` 调用 ``load_llm_data()``
    获取 ``llm.yaml`` 解析后的字典；llm_core 消费
    ``get_llm_core_config`` / ``resolve_tier`` / ``get_default_chat_model``。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def load_llm_data(self) -> dict[str, Any]:
        """返回 ``llm.yaml`` 内容（即注入配置的 ``llm`` 命名空间节）。

        P1 config_files 映射后内核注入结构为
        ``{"llm": <llm.yaml 全文>, "embedding": <embedding.yaml 全文>}``，
        本方法取 ``config["llm"]``。缺失或非 dict 返回 ``{}``，
        不抛异常（让调用方按空配置降级，而非崩溃）。
        """
        llm_config = self._config.get("llm")
        if not isinstance(llm_config, dict):
            return {}
        return llm_config

    def _load_embedding_data(self) -> dict[str, Any]:
        """返回 ``embedding.yaml`` 内容（注入配置的 ``embedding`` 命名空间节）。"""
        emb = self._config.get("embedding")
        return emb if isinstance(emb, dict) else {}

    @staticmethod
    def _case_insensitive_lookup(mapping: dict[str, Any], key: str) -> tuple[str, Any] | None:
        """大小写不敏感查 key，返回 (真实key, value)；未命中返回 None。"""
        if not key:
            return None
        key_lower = key.lower()
        for k, v in mapping.items():
            if k.lower() == key_lower:
                return k, v
        return None

    def get_model_config(self, model_id: str) -> dict[str, Any] | None:
        """根据 model_id 从 ``llm.yaml`` 的 models 段取模型配置。

        仅查 LLM models（sidecar 场景不做 embedding
        回退——LLMCore 不会用 embedding）。

        严格按 model id 查（不做 model_name 回退）：模型 id 是全系统唯一
        标识，查表面只认 id；name 形态的标识请先经 ``normalize_model_id``
        归一再入查（归一单点在本模块，不在查表面）。
        """
        models = self.load_llm_data().get("models", {})
        hit = self._case_insensitive_lookup(models, model_id)
        return hit[1] if hit else None

    def get_model_id_for_name(self, model_name: str) -> str:
        """按 model_name 反查 model_id（llm.yaml models 段的 key）。

        判定序（id 优先，name 兜底）：

        1. 传入值本身命中某个 model id（key 精确/大小写不敏感）→ 返回该 id
           （id 是唯一标识，传入值本身就是 id 时它最权威）；
        2. model_name 精确匹配某条目 → 返回该条目的 id；
        3. model_name 大小写不敏感唯一匹配 → 返回该条目的 id；
        4. 多条目同名且无 key 命中 → 抛 :class:`ModelIdAmbiguousError`
           （不猜：猜错会打到另一个 provider，凭证/计价/缓存前缀全不同）；
        5. 无命中 → 返回 ""（调用方按既有「模型不可用」语义处理）。
        """
        models = self.load_llm_data().get("models", {})
        if not model_name or not isinstance(models, dict):
            return ""

        hit = self._case_insensitive_lookup(models, model_name)
        if hit is not None:
            return hit[0]

        exact = [
            key
            for key, conf in models.items()
            if isinstance(conf, dict) and str(conf.get("model_name", "")) == model_name
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ModelIdAmbiguousError(
                f"模型标识 {model_name!r} 歧义：对应多个 model id {sorted(exact)}，"
                "请改用 model id 调用（模型 id 是唯一标识）"
            )

        lowered = model_name.lower()
        fuzzy = [
            key
            for key, conf in models.items()
            if isinstance(conf, dict)
            and str(conf.get("model_name", "")).lower() == lowered
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise ModelIdAmbiguousError(
                f"模型标识 {model_name!r} 歧义（忽略大小写）：对应多个 model id "
                f"{sorted(fuzzy)}，请改用 model id 调用（模型 id 是唯一标识）"
            )
        return ""

    def normalize_model_id(self, model: str) -> str:
        """把模型标识归一为 model id（models 段 key）——模型路由的归一单点。

        模型 id 是全系统唯一标识：查表（models 段 / provider / 凭证 / 超时）、
        KeyPool 选路、Router 部署、缓存前缀全部以它为基础；model_name 只用于
        拼上游请求串与展示。

        归一规则：已是 id → 返回该 id（含大小写容错的真实 key）；否则按
        model_name 反查（歧义抛 :class:`ModelIdAmbiguousError`）；反查未命中
        → 原样返回（下游按既有「模型不可用」语义报错，不静默换模型）。

        含 provider 前缀的串（``openai/xxx``）是上游请求形态而非
        模型标识，原样返回（不参与归一）。
        """
        if not model or "/" in model:
            return model
        return self.get_model_id_for_name(model) or model

    def get_provider_config(self, provider_name: str) -> dict[str, Any] | None:
        """根据 provider 名从 ``llm.yaml`` 的 providers 段取提供商配置。"""
        providers = self.load_llm_data().get("providers", {})
        conf = providers.get(provider_name)
        return dict(conf) if isinstance(conf, dict) else None

    def resolve_tier(self, tier: str) -> str:
        """从 ``llm.yaml`` defaults.tiers 解析 tier 为 model_id。

        tier(large/medium/small) → defaults.tiers[tier] → model_id。
        """
        if not tier:
            return ""
        tiers = self.load_llm_data().get("defaults", {}).get("tiers", {})
        return tiers.get(tier, "")

    def get_default_chat_model(self) -> str:
        """``llm.yaml`` defaults.chat（默认对话模型 id）。"""
        return self.load_llm_data().get("defaults", {}).get("chat", "")

    def get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """获取 LLMCore 消费格式的模型配置（llm_core/plugin.py 经此取数）。

        合并 model_conf + provider_conf，产出扁平的
        provider/model_name/api_base/api_key/default_params/context_window/
        call_timeout/first_token_timeout/stream_idle_timeout 字典。
        """
        model_conf = self.get_model_config(model_id)
        if model_conf is None:
            return None

        provider_name = model_conf.get("provider", "")
        provider_conf = self.get_provider_config(provider_name) or {}

        # api_key: 模型配置优先，provider.keys[0] 回退
        api_key = model_conf.get("api_key", "") or provider_conf.get("api_key", "")
        if not api_key:
            keys_list = provider_conf.get("keys", [])
            if keys_list:
                api_key = keys_list[0].get("api_key", "")

        api_base = model_conf.get("api_base", "") or provider_conf.get("api_base", "")
        # 模型条目 default_params 原样透传：未配置即空 dict——不发明兜底值
        # （参数缺省由 llm.complete_stream 按 llm.yaml 回填，缺即不发，
        # 上游按模型自身默认运行）
        default_params = model_conf.get("default_params", {})

        defaults = self.load_llm_data().get("defaults", {})
        call_timeout = model_conf.get("call_timeout", defaults.get("call_timeout", 300))
        first_token_timeout = model_conf.get("first_token_timeout", defaults.get("first_token_timeout", 60))
        stream_idle_timeout = model_conf.get("stream_idle_timeout", defaults.get("stream_idle_timeout", 90))

        result: dict[str, Any] = {
            "provider": provider_name,
            "model_name": model_conf.get("model_name", model_id),
            "model_id": model_id,
            "api_base": api_base,
            "api_key": api_key,
            "context_window": model_conf.get("context_window"),
            "default_params": default_params,
            "call_timeout": call_timeout,
            "first_token_timeout": first_token_timeout,
            "stream_idle_timeout": stream_idle_timeout,
        }
        return result
