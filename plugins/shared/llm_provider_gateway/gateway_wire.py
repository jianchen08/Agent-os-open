"""llm_provider_gateway 网关上游调用（complete_stream 契约方）。

契约（ADR 2026-11-04 + plugins/shared/llm/test_plugin_provider_route.py 同源）：
- 请求参数：model/model_name/messages/tools/stream/params（+网关托管形态的
  api_key/api_base 由调用面注入）；上游 = 网关 OpenAI 兼容 /v1/chat/completions；
- 聚合响应：{status:"streamed", text, tool_calls, thinking_text, usage,
  finish_reason}（网关 _plugin_response 直接消费）；
- 失败契约：{status:"error", llm_failure:{code, message, retry_after?}}，code
  词汇 = rate_limit/auth/quota/bad_request/service_down（未知归 service_down）；
- 流式语义：stream=True 时上游走 SSE，收流聚合（前端逐字增量为已知边界，
  见 server.py 模块注记）；stream=False 走 JSON。

httpx AsyncClient 可注入（httpx.MockTransport 承载测试替身，零真网络）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_CALL_TIMEOUT_SECS = 1800.0
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

SSE_DONE = object()  # sentinel：data: [DONE]

# HTTP 状态 → 失败 code 词汇（llm 网关冷却机直接消费）
_STATUS_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    404: "bad_request",
    422: "bad_request",
    401: "auth",
    403: "auth",
    429: "rate_limit",
}
class GatewayUpstreamError(Exception):
    """网关上游结构化失败（code 词汇对齐 llm_failure 契约）。"""

    def __init__(self, code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


def to_failure_payload(exc: Exception) -> dict[str, Any]:
    """异常 → llm_failure 契约载荷（未知异常兜底 service_down）。"""
    if isinstance(exc, GatewayUpstreamError):
        failure: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.retry_after is not None:
            failure["retry_after"] = exc.retry_after
        return {"status": "error", "llm_failure": failure}
    return {"status": "error", "llm_failure": {"code": "service_down", "message": str(exc)[:200]}}


def code_for_status(status: int, body: str = "") -> str:
    """HTTP 状态 → 失败 code；429 按响应体区分限流与配额耗尽；未知 4xx 归
    bad_request，5xx/网络层归 service_down。"""
    if status == 429:
        if "quota" in body.lower():
            return "quota"
        return "rate_limit"
    if status in _STATUS_CODE_MAP:
        return _STATUS_CODE_MAP[status]
    if status >= 500:
        return "service_down"
    if status >= 400:
        return "bad_request"
    return "service_down"


def build_headers(client_api_key: str) -> dict[str, str]:
    """请求头：Content-Type 恒在；Authorization 仅在有 key 时注入。"""
    headers = {"Content-Type": "application/json"}
    if client_api_key:
        headers["Authorization"] = f"Bearer {client_api_key}"
    return headers


def _normalize_base(base_url: str) -> str:
    """base 归一化到裸主机形态（路径由 CHAT_COMPLETIONS_PATH 拼接）。

    注入面两形态都吃：插件配置 base_url = 裸形态（http://127.0.0.1:8317）；
    llm.yaml 托管形态 slot 注入 api_base = OpenAI 惯例形态（.../v1）——
    尾部 /v1 剥除，防双 /v1（网关无 /v1/v1 路由，双拼恒 404）。"""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def build_chat_payload(
    *,
    model_name: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    stream: bool,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """组装上游 chat/completions 请求体：params 顶层展开（温度/max_tokens 等
    透传网关），stream=True 加 stream_options.include_usage（usage 随末块回传）。"""
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream_options"] = {"include_usage": True}
    for key, value in (params or {}).items():
        if key in ("model", "messages", "stream", "stream_options", "tools"):
            continue  # 契约键不透传覆盖
        payload[key] = value
    return payload


def map_finish_reason(reason: str | None) -> str:
    """finish_reason 收敛五值词汇（stop/length/tool_calls/error/interrupted）。"""
    mapping = {
        "stop": "stop",
        "length": "length",
        "tool_calls": "tool_calls",
        "function_call": "tool_calls",
        "error": "error",
        "interrupted": "interrupted",
    }
    return mapping.get(reason or "", "stop")


def _normalize_tool_call(raw: dict[str, Any]) -> dict[str, Any]:
    function = raw.get("function") or {}
    return {
        "id": raw.get("id") or "",
        "type": raw.get("type") or "function",
        "function": {
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "",
        },
    }


def aggregate_response(payload: dict[str, Any]) -> dict[str, Any]:
    """非流式 JSON 响应 → 聚合契约形态（空 choices 非失败：状态面在 HTTP 层）。"""
    choices = payload.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    usage = payload.get("usage") or {}
    return {
        "status": "streamed",
        "text": message.get("content") or "",
        "tool_calls": [_normalize_tool_call(tc) for tc in (message.get("tool_calls") or [])],
        "thinking_text": message.get("reasoning_content") or message.get("reasoning"),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "finish_reason": map_finish_reason(choices[0].get("finish_reason") if choices else None),
    }


def parse_sse_line(line: str) -> Any:
    """SSE 行解析：``data: {...}`` → dict；``data: [DONE]`` → SSE_DONE；
    其他行（event:/注释/空行）→ None；data 载荷非 JSON → ValueError
    （流协议损坏，调用方翻译 service_down）。"""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    data = stripped[len("data:"):].strip()
    if data == "[DONE]":
        return SSE_DONE
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SSE data 载荷非 JSON（流协议损坏）: {data[:80]}") from exc


def aggregate_sse_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """SSE chunk 序列 → 聚合契约形态。

    文本/思考增量拼接；工具调用按 delta index 组装（id/name 首见捕获、
    arguments 字符串增量拼接）；usage 取末次出现；finish_reason 取最后一次
    非空值。"""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    tool_order: list[int] = []

    for chunk in chunks:
        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            text_parts.append(content)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            thinking_parts.append(reasoning)
        for raw in delta.get("tool_calls") or []:
            index = int(raw.get("index", 0))
            if index not in tool_calls_by_index:
                tool_order.append(index)
                tool_calls_by_index[index] = {
                    "id": raw.get("id") or "",
                    "type": raw.get("type") or "function",
                    "function": {"name": "", "arguments": ""},
                }
            entry = tool_calls_by_index[index]
            if raw.get("id"):
                entry["id"] = raw["id"]
            function = raw.get("function") or {}
            if function.get("name"):
                entry["function"]["name"] = function["name"]
            if function.get("arguments"):
                entry["function"]["arguments"] += function["arguments"]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        if chunk.get("usage"):
            usage = chunk["usage"]

    return {
        "status": "streamed",
        "text": "".join(text_parts),
        "tool_calls": [tool_calls_by_index[i] for i in tool_order],
        "thinking_text": "".join(thinking_parts) or None,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "finish_reason": map_finish_reason(finish_reason),
    }


async def complete_via_gateway(
    base_url: str,
    *,
    model_name: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    stream: bool,
    params: dict[str, Any] | None,
    client_api_key: str = "",
    timeout_secs: float = DEFAULT_CALL_TIMEOUT_SECS,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """调网关 /v1/chat/completions 并返回聚合契约形态。

    失败翻译：HTTP 非 2xx → code_for_status；连接/超时/SSE 协议坏流 →
    service_down。响应体摘录截断 200 字符进 message（可诊断，不吞病因）。

    Args:
        client: 注入 httpx.AsyncClient（测试 MockTransport）；None 时本次调用
            自建自关。
    """
    url = _normalize_base(base_url) + CHAT_COMPLETIONS_PATH
    payload = build_chat_payload(
        model_name=model_name,
        messages=messages,
        tools=tools,
        stream=stream,
        params=params,
    )
    headers = build_headers(client_api_key)
    timeout = httpx.Timeout(timeout_secs, connect=10.0)
    own_client = client is None
    if own_client:
        # trust_env=False：网关恒为本机回环服务（127.0.0.1:8317），任何代理
        # 环境（env / Windows 注册表系统代理，urllib.getproxies 双源）都不得
        # 劫持回环调用——实测系统代理在场时恒 502（2026-11-21 装机面实证）。
        client = httpx.AsyncClient(trust_env=False)
    assert client is not None
    try:
        if stream:
            return await _complete_streaming(client, url, headers, payload, timeout)
        return await _complete_oneshot(client, url, headers, payload, timeout)
    finally:
        if own_client:
            await client.aclose()


async def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        body = response.text[:200]
        raise GatewayUpstreamError(
            code_for_status(response.status_code, body),
            f"网关上游 HTTP {response.status_code}: {body}",
            retry_after=float(response.headers["Retry-After"]) if response.headers.get("Retry-After") else None,
        )


async def _complete_oneshot(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: httpx.Timeout,
) -> dict[str, Any]:
    try:
        response = await client.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        raise GatewayUpstreamError("service_down", f"网关连接失败: {type(exc).__name__}: {exc}") from exc
    await _raise_for_status(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise GatewayUpstreamError("service_down", f"网关响应非 JSON: {response.text[:200]}") from exc
    return aggregate_response(data if isinstance(data, dict) else {})


async def _complete_streaming(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: httpx.Timeout,
) -> dict[str, Any]:
    try:
        response = await client.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        raise GatewayUpstreamError("service_down", f"网关连接失败: {type(exc).__name__}: {exc}") from exc
    await _raise_for_status(response)
    chunks: list[dict[str, Any]] = []
    try:
        async for line in response.aiter_lines():
            parsed = parse_sse_line(line)
            if parsed is None:
                continue
            if parsed is SSE_DONE:
                break
            if isinstance(parsed, dict):
                chunks.append(parsed)
    except ValueError as exc:
        raise GatewayUpstreamError("service_down", str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise GatewayUpstreamError("service_down", f"网关流中断: {type(exc).__name__}: {exc}") from exc
    return aggregate_sse_chunks(chunks)


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_CALL_TIMEOUT_SECS",
    "SSE_DONE",
    "GatewayUpstreamError",
    "aggregate_response",
    "aggregate_sse_chunks",
    "build_chat_payload",
    "build_headers",
    "code_for_status",
    "complete_via_gateway",
    "map_finish_reason",
    "parse_sse_line",
    "to_failure_payload",
]
