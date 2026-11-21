# @feature: FP-T07 llm api | @ci: python-coverage
"""DSH 接口方言（方言注册表扩展点；主方案 §2.4 四证据 / §6.4-b）。

契约真值 = §2.4 实证四证据（每条对应本文件一段实现）：
1. **提供包形状**：DSH 的 LLM 连接 = 包化契约三件套（llm 服务本体 +
   凭据提供包 + agent-default-model 包 ``{provider, model}``）——
   ``DshProviderPack``/``DshCredentialPack``/``DshDefaultModelPack`` 逐件承载；
2. **咨询发现**：``llm.listProviders()`` / ``llm.listModels(providerId)``
   （``@deepseek-ai/dsh-llm`` 的 ``LlmProviderInfo{id,name}`` /
   ``LlmModelInfo{provider,id,name}``）——``list_providers``/``list_models``
   逐字段镜像；
3. **每流凭据**（request-level-llm-config-credentials）：凭据在每次调用时
   解析（配置读面 + ``${VAR}`` 展开 + 合法性判定），不冻结在注册期；
4. **LlmFailure 归一**：DSH ``{message, code, status?,
   providerRetryAfterMs?, requestId?}`` → 既有 ``llm_failure{code, message,
   retry_after?, request_id?}`` 词汇（与网关 ``_PluginProviderFailure`` 同一
   五值 code 空间：rate_limit/quota/auth/service_down/bad_request）；流内终态
   ``FinishReason{kind: error|aborted, failure}`` 同口径归一。

线材复用声明（不手写新实现）
- HTTP 客户端/连接池/超时语义/SSE 单行迭代与关闭释放全部复用 ``llm_wire``
  （``_get_client`` 共享池 trust_env=False、``_WireStream`` 的行迭代与 aclose
  状态机）；本文件只实现 DSH 的 **chunk 词汇翻译** 与 consult/failure 形状，
  二者在仓库内无既有实现可复制。
- DSH ``LlmRuntime.stream()`` 是进程内 AsyncIterable 契约；跨进程消费面
  **[假设]** = 同语义 HTTP/SSE 门面（门面形状按 DSH 契约形状取最合理落点，
  未来桥面实现只需替换传输 seam，不改本方言语义）：
  ``POST {api_base}/llm/stream``（body = GenerateOptions 子集 + ``provider``
  路由键；SSE 每帧一个 StreamChunk JSON，``data: [DONE]`` 收尾）、
  ``GET {api_base}/llm/providers``、
  ``GET {api_base}/llm/providers/{provider}/models``。

配置形状（llm.yaml providers 段，一条 = 一个 DSH provider 路由）：

    dsh_runtime:
      type: dsh
      api_base: ${AGENTOS_DSH_LLM_BASE_URL}
      provider_route: dsh          # DSH provider 路由键（LlmProviderInfo.id）
      keys: [{api_key: ${DEEPSEEK_API_KEY}}]   # 凭据包（每流解析）
      default_model: {provider: dsh, model: deepseek-v4-flash}  # 可选
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import llm_wire

logger = logging.getLogger(__name__)

#: 方言实现键 + 配置 type 词（新方言只经注册点扩展，不改既有条目语义）。
DSH = "dsh"
TYPE_KEYS: tuple[str, ...] = (DSH,)

#: DSH llm 服务门面路径（跨进程消费面；见文件头 [假设] 声明）。
_PROVIDERS_PATH = "/llm/providers"
_STREAM_PATH = "/llm/stream"

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ─────────────────────────── 失败词汇（证据 4） ───────────────────────────

#: DSH provider-neutral code → 既有 llm_failure code（五值空间）。
_FAILURE_CODES: dict[str, str] = {
    "RATE_LIMIT": "rate_limit",
    "RATE_LIMITED": "rate_limit",
    "TOO_MANY_REQUESTS": "rate_limit",
    "QUOTA": "quota",
    "ACCOUNT_QUOTA": "quota",
    "QUOTA_EXCEEDED": "quota",
    "AUTH": "auth",
    "AUTHENTICATION": "auth",
    "UNAUTHORIZED": "auth",
    "MISSING_CREDENTIAL": "auth",
    "INVALID_CREDENTIAL": "auth",
    "BAD_REQUEST": "bad_request",
    "INVALID_ARGS": "bad_request",
    "NO_ADAPTER": "bad_request",
    "CONTEXT_WINDOW_EXCEEDED": "bad_request",
    "IMAGE_OFFLOAD_REQUIRED": "bad_request",
    "EMPTY_RESPONSE": "bad_request",
    "INVALID_PREPARED_CALL": "bad_request",
    "DUPLICATE_ADAPTER": "bad_request",
}

#: HTTP 状态 → 失败 code（无结构化载荷时的兜底分类，与 llm_wire 同口径）。
_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "AUTH",
    403: "AUTH",
    404: "BAD_REQUEST",
    422: "BAD_REQUEST",
    429: "RATE_LIMIT",
}


class DshDialectError(RuntimeError):
    """DSH 方言基准异常（配置/契约面错误——非上游失败，不参与冷却归类）。"""


class DshConfigError(DshDialectError):
    """配置面不可用（缺 provider 路由 / api_base 仍是未解析占位符）。"""


class DshContractError(DshDialectError):
    """契约面形状不符（咨询响应缺数组字段 / 非 JSON 载荷）。"""


class DshCredentialError(DshDialectError):
    """凭据不可用（空值 / 含非法字符 / 占位符未解析）——发起 HTTP 前 fail-closed。"""


class DshServiceError(RuntimeError):
    """DSH llm 服务失败（LlmFailure 归一后的既有失败词汇）。

    Attributes:
        llm_failure: ``{code, message, retry_after?, request_id?, status?}``。
        code: 五值 code（rate_limit/quota/auth/service_down/bad_request）。
        retry_after: 上游建议等待秒数（可选；供冷却机消费）。
        request_id: 上游请求标识（可选；诊断面）。
    """

    def __init__(self, failure: Mapping[str, Any] | None) -> None:
        self.llm_failure = normalize_failure(failure)
        self.code = str(self.llm_failure["code"])
        self.retry_after = self.llm_failure.get("retry_after")
        self.request_id = self.llm_failure.get("request_id")
        # 消息形状与网关 _PluginProviderFailure 同款（前端按 [llm_failure code=…] 解析）
        super().__init__(f"[llm_failure code={self.code}] {self.llm_failure['message']}")


def _positive_seconds(value: Any) -> float | None:
    """正有限数值 → float；其余（0/负数/非数值/布尔/NaN/∞）→ None（不猜数值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def normalize_failure(failure: Mapping[str, Any] | None) -> dict[str, Any]:
    """DSH LlmFailure → 既有 ``llm_failure`` 载荷（幂等）。

    ``providerRetryAfterMs``（毫秒）→ ``retry_after``（秒，冷却机口径）；
    已归一形态的 ``retry_after``（秒）原样保留。未知/缺失 code 归
    ``service_down``（可冷却可兜底），不静默当成功。
    """
    data: Mapping[str, Any] = failure or {}
    raw_code = str(data.get("code") or "").strip()
    code = _FAILURE_CODES.get(raw_code.upper(), "service_down")
    out: dict[str, Any] = {
        "code": code,
        "message": str(data.get("message") or f"DSH llm 服务失败（{raw_code or 'unknown'}）"),
    }
    if "retry_after" in data:
        retry_after = _positive_seconds(data.get("retry_after"))
    else:
        retry_ms = data.get("providerRetryAfterMs", data.get("retry_after_ms"))
        retry_after = None
        if (ms := _positive_seconds(retry_ms)) is not None:
            retry_after = ms / 1000.0
    if retry_after is not None:
        out["retry_after"] = retry_after
    request_id = data.get("requestId", data.get("request_id"))
    if isinstance(request_id, str) and request_id.strip():
        out["request_id"] = request_id.strip()
    status = data.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and status > 0:
        out["status"] = status
    return out


# ─────────────────────────── 提供包形状（证据 1） ───────────────────────────


@dataclass(frozen=True)
class DshCredentialPack:
    """凭据提供包（``dsh-llm-deepseek-api-key`` 形态）：引用 + 解析产物。

    ``api_key_ref`` 保留配置原值（含 ``${VAR}`` 形态，诊断面）；``api_key``
    是注册期可见的解析结果（占位符未解析时为空）——真正的每流解析在
    ``resolve_credential`` 调用期完成。
    """

    provider: str
    api_key_ref: str
    api_key: str


@dataclass(frozen=True)
class DshDefaultModelPack:
    """agent-default-model 包（``@deepseek-ai/dsh-agent-default-model``）：两字段。"""

    provider: str
    model: str


@dataclass(frozen=True)
class DshProviderPack:
    """一条 DSH provider 路由的三件套声明（llm.yaml providers.<name> 解析产物）。"""

    name: str
    provider: str
    base_url: str
    credential: DshCredentialPack
    default_model: DshDefaultModelPack | None


@dataclass(frozen=True)
class DshProviderInfo:
    """``LlmProviderInfo`` 镜像：provider 路由键 + 显示名。"""

    id: str
    name: str


@dataclass(frozen=True)
class DshModelInfo:
    """``LlmModelInfo`` 镜像：所属路由 + 模型 id + 显示名。"""

    provider: str
    id: str
    name: str


def _llm_data(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """注入视图 → llm.yaml 数据（``{"llm": {...}}`` 或裸 llm 数据两形态）。"""
    if not isinstance(config, Mapping):
        return {}
    llm = config.get("llm")
    if isinstance(llm, Mapping):
        return dict(llm)
    if "providers" in config or "models" in config:
        return dict(config)
    return {}


def _expand_env(ref: str, env: Mapping[str, str] | None = None) -> str:
    """``${VAR}`` 展开（未定义保持字面量——与 llm 配置注入同一 expandvars 行为）。"""
    source = os.environ if env is None else env

    def _sub(match: re.Match[str]) -> str:
        return source.get(match.group(1), match.group(0))

    return _ENV_VAR_RE.sub(_sub, ref)


def _assert_usable_key(value: str, ref: str) -> str:
    """凭据合法性判定（DSH api-key 契约形态）：空值/控制与非 ASCII 字符即拒。"""
    key = value.strip()
    if not key:
        raise DshCredentialError(f"DSH 凭据为空（引用 {ref}）：发起 HTTP 前 fail-closed")
    if any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key):
        raise DshCredentialError(f"DSH 凭据含非法字符（引用 {ref}）：发起 HTTP 前 fail-closed")
    return key


def resolve_credential(credential: DshCredentialPack, *, env: Mapping[str, str] | None = None) -> str:
    """每流凭据解析：调用期展开 + 合法性判定（不在注册期冻结）。

    Raises:
        DshCredentialError: 占位符未解析、值为空或含非法字符。
    """
    expanded = _expand_env(credential.api_key_ref, env)
    if "${" in expanded:
        raise DshCredentialError(
            f"DSH 凭据占位符未解析（{credential.api_key_ref}）：发起 HTTP 前 fail-closed"
        )
    return _assert_usable_key(expanded, credential.api_key_ref)


def _credential_ref(conf: Mapping[str, Any]) -> str:
    """providers 条目 → 凭据引用（keys[0] 优先，其次 api_key）。"""
    keys = conf.get("keys")
    if isinstance(keys, list):
        for key_conf in keys:
            if isinstance(key_conf, Mapping):
                ref = key_conf.get("api_key")
                if isinstance(ref, str) and ref.strip():
                    return ref
    ref = conf.get("api_key")
    return ref if isinstance(ref, str) else ""


def _provider_route(name: str, conf: Mapping[str, Any]) -> str:
    """providers 条目 → DSH provider 路由键（provider_route 优先，缺省条目键）。"""
    for field in ("provider_route", "provider"):
        value = conf.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return name


def _default_model_pack(conf: Mapping[str, Any]) -> DshDefaultModelPack | None:
    """agent-default-model 包：字段缺失/非法即不提供（缺口由消费面 fail-visible）。"""
    raw = conf.get("default_model")
    if not isinstance(raw, Mapping):
        return None
    provider = raw.get("provider")
    model = raw.get("model")
    if not (isinstance(provider, str) and provider.strip()) or not (isinstance(model, str) and model.strip()):
        return None
    return DshDefaultModelPack(provider=provider.strip(), model=model.strip())


def provider_packs(config: Mapping[str, Any] | None) -> tuple[DshProviderPack, ...]:
    """枚举 ``type: dsh`` provider 条目 → 三件套包（确定性排序；无端点不成包）。"""
    providers = _llm_data(config).get("providers")
    if not isinstance(providers, Mapping):
        return ()
    packs: list[DshProviderPack] = []
    for name in sorted(providers):
        conf = providers[name]
        if not isinstance(conf, Mapping):
            continue
        if str(conf.get("type") or "").strip().lower() != DSH:
            continue
        base_url = conf.get("api_base")
        if not (isinstance(base_url, str) and base_url.strip()):
            continue
        route = _provider_route(str(name), conf)
        ref = _credential_ref(conf)
        expanded = _expand_env(ref)
        packs.append(
            DshProviderPack(
                name=str(name),
                provider=route,
                base_url=base_url.strip(),
                credential=DshCredentialPack(
                    provider=route,
                    api_key_ref=ref,
                    api_key="" if "${" in expanded else expanded,
                ),
                default_model=_default_model_pack(conf),
            )
        )
    return tuple(packs)


def catalog_entries(provider_name: str, models: tuple[DshModelInfo, ...] | list[DshModelInfo]) -> dict[str, dict[str, str]]:
    """咨询发现产物 → llm.yaml ``models`` 条目形状（配置候选，不改既有条目）。"""
    return {
        model.id: {
            "provider": provider_name,
            "model_name": model.id,
            "display_name": model.name or model.id,
        }
        for model in models
    }


# ─────────────────────────── 传输面（复用 llm_wire） ───────────────────────────


def _auth_headers(api_key: str) -> dict[str, str]:
    """Bearer 鉴权头（空凭据不伪造 Authorization——consult 面允许匿名目录）。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _timeout_budget(timeout: float) -> httpx.Timeout:
    """分阶段超时（与 llm_wire.stream_chat 同口径）。"""
    return httpx.Timeout(connect=min(timeout, 30.0), read=timeout, write=timeout, pool=timeout)


def _failure_from_http(response: httpx.Response) -> dict[str, Any]:
    """HTTP 失败响应 → LlmFailure 形状（结构化载荷优先，状态码兜底分类）。"""
    text = response.text[:400]
    payload: Any
    try:
        payload = response.json()
    except ValueError:
        payload = None
    failure: Mapping[str, Any] | None = None
    if isinstance(payload, Mapping):
        embedded = payload.get("llm_failure")
        if isinstance(embedded, Mapping):
            failure = embedded
        elif "code" in payload or "message" in payload:
            failure = payload
    if failure is None:
        failure = {"code": _STATUS_CODES.get(response.status_code, "SERVICE_DOWN"), "message": text}
    normalized = normalize_failure(failure)
    normalized.setdefault("status", response.status_code)
    return normalized


async def _get_json(url: str, *, api_key: str, timeout: float) -> Any:
    """GET consult 端点（httpx 共享池；非 200 归一失败）。"""
    client = llm_wire._get_client()
    response = await client.get(url, headers=_auth_headers(api_key), timeout=_timeout_budget(timeout))
    if response.status_code != 200:
        raise DshServiceError(_failure_from_http(response))
    try:
        return response.json()
    except ValueError as exc:
        raise DshContractError(f"DSH 咨询响应不是 JSON（{url}）") from exc


def _catalog_items(payload: Any, field: str) -> list[Mapping[str, Any]]:
    """咨询载荷 → 条目数组（裸数组或 ``{<field>: [...]}`` 两形态）。"""
    items = payload.get(field) if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        raise DshContractError(f"DSH 咨询响应 {field} 字段不是数组（fail-closed）")
    entries: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise DshContractError(f"DSH 咨询响应 {field} 条目不是对象（fail-closed）")
        entries.append(item)
    return entries


async def list_providers(
    base_url: str, *, api_key: str = "", timeout: float = 30.0
) -> tuple[DshProviderInfo, ...]:
    """``llm.listProviders()`` 镜像：已注册 provider 路由目录。"""
    _assert_base_url(base_url)
    payload = await _get_json(
        base_url.rstrip("/") + _PROVIDERS_PATH, api_key=api_key, timeout=timeout
    )
    infos: list[DshProviderInfo] = []
    for item in _catalog_items(payload, "providers"):
        provider_id = item.get("id")
        if not (isinstance(provider_id, str) and provider_id.strip()):
            raise DshContractError("DSH 咨询响应 providers 条目缺 id（fail-closed）")
        name = item.get("name")
        infos.append(
            DshProviderInfo(
                id=provider_id.strip(),
                name=name.strip() if isinstance(name, str) and name.strip() else provider_id.strip(),
            )
        )
    return tuple(infos)


async def list_models(
    base_url: str, provider: str, *, api_key: str = "", timeout: float = 30.0
) -> tuple[DshModelInfo, ...]:
    """``llm.listModels(providerId)`` 镜像：单 provider 路由的模型目录。"""
    _assert_base_url(base_url)
    route = str(provider or "").strip()
    if not route:
        raise DshConfigError("DSH listModels 需要 provider 路由键（不能为空）")
    url = base_url.rstrip("/") + _PROVIDERS_PATH + "/" + route + "/models"
    payload = await _get_json(url, api_key=api_key, timeout=timeout)
    models: list[DshModelInfo] = []
    for item in _catalog_items(payload, "models"):
        model_id = item.get("id")
        if not (isinstance(model_id, str) and model_id.strip()):
            raise DshContractError("DSH 咨询响应 models 条目缺 id（fail-closed）")
        owner = item.get("provider")
        name = item.get("name")
        models.append(
            DshModelInfo(
                provider=owner.strip() if isinstance(owner, str) and owner.strip() else route,
                id=model_id.strip(),
                name=name.strip() if isinstance(name, str) and name.strip() else model_id.strip(),
            )
        )
    return tuple(models)


def _assert_base_url(base_url: str) -> None:
    """端点可用性判定：空/未解析占位符 fail-closed（对齐 api_key 纪律）。"""
    url = str(base_url or "").strip()
    if not url:
        raise DshConfigError("DSH api_base 为空（providers.<name>.api_base 未配置，fail-closed）")
    if "${" in url:
        raise DshConfigError(
            f"DSH api_base 占位符未解析（{url}）：发起 HTTP 前 fail-closed"
        )


# ─────────────────────────── chunk 词汇翻译 ───────────────────────────

#: DSH FinishReason.kind → 既有五值 finish_reason（error/aborted 在流内终态归一为失败）。
_FINISH_REASONS: dict[str, str] = {
    "stop": "stop",
    "tool-calls": "tool_calls",
    "max-tokens": "length",
}


def _empty_delta() -> SimpleNamespace:
    """空增量（消费机要求 delta 恒为对象且 tool_calls 恒为列表）。"""
    return SimpleNamespace(content=None, reasoning_content=None, tool_calls=[])


def _chunk(delta: SimpleNamespace | None, finish_reason: str | None = None) -> Any:
    """归一 chunk（与 llm_wire._parse_sse_chunk 同形状）。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta or _empty_delta(), finish_reason=finish_reason)],
        usage=None,
    )


def _usage_chunk(raw: Any) -> Any:
    """DSH TokenUsage → 既有 usage 口径。

    DSH 计数互斥（``inputTokens`` 不含缓存，缓存单列 cacheRead/cacheWrite）；
    既有 ``prompt_tokens`` 含缓存（OpenAI 口径），故 prompt = input + cacheRead。
    """
    usage = raw if isinstance(raw, Mapping) else {}
    input_tokens = int(usage.get("inputTokens") or 0)
    cache_read = int(usage.get("cacheReadTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    prompt_tokens = input_tokens + cache_read
    total = usage.get("totalTokens")
    total_tokens = (
        int(total) if isinstance(total, int) and not isinstance(total, bool) else prompt_tokens + output_tokens
    )
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cache_read),
        ),
    )


def _translate_chunk(data: Any) -> Any | None:
    """单帧 DSH StreamChunk → 归一 chunk（None = 该帧不产出下游 chunk）。

    Raises:
        DshServiceError: ``finish`` 帧的 ``FinishReason{kind: error|aborted}``
            （终态失败；failure 缺省按 service_down 归一）。
    """
    if not isinstance(data, Mapping):
        raise DshContractError("DSH 流帧不是 JSON 对象（fail-closed）")
    kind = data.get("type")
    if kind == "text-delta":
        text = data.get("text")
        return _chunk(SimpleNamespace(content=text, reasoning_content=None, tool_calls=[])) if text else None
    if kind == "reasoning-delta":
        text = data.get("text")
        return _chunk(SimpleNamespace(content=None, reasoning_content=text, tool_calls=[])) if text else None
    if kind == "tool-call-delta":
        index = data.get("index")
        return _chunk(
            SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=index if isinstance(index, int) and not isinstance(index, bool) else 0,
                        id=data.get("id"),
                        function=SimpleNamespace(
                            name=data.get("name"),
                            arguments=str(data.get("argumentsDelta") or ""),
                        ),
                    )
                ],
            )
        )
    if kind == "usage":
        return _usage_chunk(data.get("usage"))
    if kind == "finish":
        reason = data.get("reason")
        reason_kind = reason.get("kind") if isinstance(reason, Mapping) else reason
        reason_kind = str(reason_kind or "")
        if reason_kind in ("error", "aborted"):
            failure = reason.get("failure") if isinstance(reason, Mapping) else None
            raise DshServiceError(failure if isinstance(failure, Mapping) else None)
        return _chunk(None, finish_reason=_FINISH_REASONS.get(reason_kind, "stop"))
    # block-start/block-end 与扩展帧：增量通道已承载内容，此处不重复产出
    return None


class _DshStream(llm_wire._WireStream):
    """DSH SSE 流（行迭代与关闭释放复用 ``llm_wire._WireStream``）。

    差异仅在 chunk 词汇：``data:`` 帧解析为 DSH StreamChunk 后经
    ``_translate_chunk`` 归一；中途断流与 llm_wire 同口径转半截语义。
    """

    async def __anext__(self) -> Any:
        try:
            while True:
                if self._closed:
                    raise StopAsyncIteration
                line = (await self._line_iter().__anext__()).strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == llm_wire._SSE_DONE:
                    raise StopAsyncIteration
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug("[llm_dsh] 非 JSON SSE 行忽略（%s）", payload[:80])
                    continue
                chunk = _translate_chunk(data)
                if chunk is not None:
                    return chunk
        except httpx.RemoteProtocolError as exc:
            raise StopAsyncIteration from exc


# ─────────────────────────── 方言装配（注册点实现） ───────────────────────────


def _default_config() -> Mapping[str, Any] | None:
    """进程内注入视图（与 llm_adapter 同源：_config_models 单点）。"""
    import _config_models  # noqa: PLC0415

    return _config_models.get_config()


class DshDialect:
    """dsh 方言实现：payload 构建 + provider 路由 + 每流凭据 + 流/非流执行。

    生命周期（§6.2 三线）：实例只持只读配置读面；per-request 状态全部在
    调用局部（无跨请求累积）。
    """

    def __init__(self, config_provider: Callable[[], Any] | None = None) -> None:
        self._config_provider = config_provider or _default_config

    # ── 配置面解析 ──

    def _llm(self) -> dict[str, Any]:
        return _llm_data(self._config_provider())

    def _provider_entry(self, model_id: str, model_name: str, base_url: str) -> tuple[str, Mapping[str, Any]]:
        """模型 → (provider 条目键, 条目)：id → model_name → 端点 三级解析。

        Raises:
            DshConfigError: 三级都不命中（不猜路由——路由错会打到别人的凭据）。
        """
        data = self._llm()
        providers = data.get("providers")
        models = data.get("models")
        providers = providers if isinstance(providers, Mapping) else {}
        models = models if isinstance(models, Mapping) else {}

        def _from_model_entry(entry: Any) -> tuple[str, Mapping[str, Any]] | None:
            if not isinstance(entry, Mapping):
                return None
            provider_key = entry.get("provider")
            if not (isinstance(provider_key, str) and provider_key in providers):
                return None
            conf = providers[provider_key]
            if isinstance(conf, Mapping) and str(conf.get("type") or "").strip().lower() == DSH:
                return provider_key, conf
            return None

        for candidate in (model_id, model_name):
            if not candidate:
                continue
            hit = _from_model_entry(models.get(candidate))
            if hit is not None:
                return hit

        wanted = base_url.rstrip("/")
        matches = [
            (str(name), conf)
            for name, conf in providers.items()
            if isinstance(conf, Mapping)
            and str(conf.get("type") or "").strip().lower() == DSH
            and str(conf.get("api_base") or "").rstrip("/") == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        raise DshConfigError(
            f"模型 {model_id or model_name or base_url!r} 解析不到 dsh provider 路由"
            "（llm.yaml models.<id>.provider 必须指向 type: dsh 条目，fail-closed）"
        )

    def _per_stream_credential(self, conf: Mapping[str, Any], api_key: str) -> str:
        """每流凭据：调用期解析（调用方已解析的 key 优先，缺省回落方言自解析）。"""
        if api_key.strip():
            return _assert_usable_key(api_key, "调用方注入的每流凭据")
        ref = _credential_ref(conf)
        return resolve_credential(DshCredentialPack(provider="", api_key_ref=ref, api_key=""))

    # ── 方言三动作 ──

    def build_payload(
        self,
        model_name: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """统一请求词汇 → DSH GenerateOptions 子集（provider 由 stream/post 注入）。"""
        body: dict[str, Any] = {"model": model_name, "messages": messages}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        for key, value in (extra or {}).items():
            if value is None:
                continue
            body[_PARAM_KEYS.get(key, key)] = value
        return body

    async def stream(
        self,
        base_url: str,
        api_key: str,
        body: dict[str, Any],
        *,
        timeout: float,
        model_label: str = "",
    ) -> Any:
        """provider-routed 流式调用 → ``_DshStream``（SSE）。"""
        _assert_base_url(base_url)
        model_id = model_label or str(body.get("model") or "")
        provider_key, conf = self._provider_entry(model_id, str(body.get("model") or ""), base_url)
        route = _provider_route(provider_key, conf)
        credential = self._per_stream_credential(conf, api_key)
        envelope = {**body, "provider": route}
        client = llm_wire._get_client()
        url = base_url.rstrip("/") + _STREAM_PATH
        request = client.build_request(
            "POST", url, json=envelope, headers=_auth_headers(credential), timeout=_timeout_budget(timeout)
        )
        response = await client.send(request, stream=True)
        if response.status_code != 200:
            failure = _failure_from_http(response)
            await response.aclose()
            raise DshServiceError(failure)
        return _DshStream(response, model_label or model_id)

    async def post(self, base_url: str, api_key: str, body: dict[str, Any], *, timeout: float) -> Any:
        """非流式面 = 同一 DSH 流端点 + 本地组装（DSH 只有 stream 一种调用 API）。"""
        stream = await self.stream(base_url, api_key, body, timeout=timeout)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        usage: Any = None
        finish_reason: str | None = None
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta.content:
                    text_parts.append(delta.content)
                if delta.reasoning_content:
                    thinking_parts.append(delta.reasoning_content)
                for tc in delta.tool_calls:
                    entry = tool_calls.setdefault(
                        int(getattr(tc, "index", 0)), {"id": "", "name": "", "arguments": ""}
                    )
                    tc_id = getattr(tc, "id", None)
                    if tc_id and not entry["id"]:
                        entry["id"] = str(tc_id)
                    function = getattr(tc, "function", None)
                    if function is not None:
                        if function.name:
                            entry["name"] += str(function.name)
                        if function.arguments:
                            entry["arguments"] += str(function.arguments)
        finally:
            await stream.aclose()
        message = SimpleNamespace(
            content="".join(text_parts) or None,
            reasoning_content="".join(thinking_parts) or None,
            tool_calls=[
                SimpleNamespace(
                    id=entry["id"] or f"call_{index}",
                    function=SimpleNamespace(name=entry["name"], arguments=entry["arguments"]),
                )
                for index, entry in sorted(tool_calls.items())
            ],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=usage,
        )


#: 既有采样词汇 → DSH GenerateOptions 字段（其余参数 provider/adapter 自持，原样透传）
_PARAM_KEYS: dict[str, str] = {
    "max_tokens": "maxTokens",
    "reasoning_effort": "reasoningEffort",
}


_DEFAULT_DIALECT = DshDialect()


def build_payload(
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """注册表构建动作（模块级入口 = 进程默认方言单例）。"""
    return _DEFAULT_DIALECT.build_payload(model_name, messages, tools=tools, stream=stream, extra=extra)


async def stream(
    base_url: str, api_key: str, body: dict[str, Any], *, timeout: float, model_label: str = ""
) -> Any:
    """注册表流动作（模块级入口）。"""
    return await _DEFAULT_DIALECT.stream(
        base_url, api_key, body, timeout=timeout, model_label=model_label
    )


async def post(base_url: str, api_key: str, body: dict[str, Any], *, timeout: float) -> Any:
    """注册表非流动作（模块级入口）。"""
    return await _DEFAULT_DIALECT.post(base_url, api_key, body, timeout=timeout)


__all__ = [
    "DSH",
    "TYPE_KEYS",
    "DshConfigError",
    "DshContractError",
    "DshCredentialError",
    "DshCredentialPack",
    "DshDefaultModelPack",
    "DshDialect",
    "DshDialectError",
    "DshModelInfo",
    "DshProviderInfo",
    "DshProviderPack",
    "DshServiceError",
    "build_payload",
    "catalog_entries",
    "list_models",
    "list_providers",
    "normalize_failure",
    "post",
    "provider_packs",
    "resolve_credential",
    "stream",
]
