# @feature: FP-LLM网关随包分发 | @ci: python-coverage
"""gateway_wire 网关上游调用契约测试（llm_provider.complete_stream 聚合响应形态）。

覆盖面：请求组装（model/messages/tools/params 合并/流式 stream_options）、
非流式聚合、SSE 增量聚合（文本/thinking/工具调用参数累积/usage/finish_reason）、
失败契约映射（HTTP 状态 → llm_failure code 词汇：rate_limit/auth/quota/
bad_request/service_down）、Authorization 头注入。HTTP 边界经 httpx
MockTransport 替身，零真网络。
"""

from __future__ import annotations

import json
from typing import Any

import gateway_wire as gw
import httpx
import pytest
from gateway_wire import GatewayUpstreamError

pytestmark = pytest.mark.unit


# ── 请求组装 ─────────────────────────────────────────────────────────────


def test_build_chat_payload_merges_params_and_tools() -> None:
    payload = gw.build_chat_payload(
        model_name="glm-5.3",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
        stream=False,
        params={"temperature": 0.7, "max_tokens": 128},
    )
    assert payload["model"] == "glm-5.3"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 128
    assert payload["stream"] is False
    assert "stream_options" not in payload


def test_build_chat_payload_stream_requests_usage() -> None:
    """流式请求带 stream_options.include_usage（usage 随末块回传）。"""
    payload = gw.build_chat_payload(
        model_name="m", messages=[], tools=None, stream=True, params={}
    )
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "tools" not in payload


def test_build_headers_auth_optional() -> None:
    assert gw.build_headers("") == {"Content-Type": "application/json"}
    assert gw.build_headers("sk-x")["Authorization"] == "Bearer sk-x"


# ── 非流式聚合 ───────────────────────────────────────────────────────────


def test_aggregate_response_maps_openai_json() -> None:
    upstream = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "答复",
                    "reasoning_content": "想了想",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q": "x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    result = gw.aggregate_response(upstream)
    assert result["status"] == "streamed"
    assert result["text"] == "答复"
    assert result["thinking_text"] == "想了想"
    assert result["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"q": "x"}'}}
    ]
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert result["finish_reason"] == "tool_calls"


def test_aggregate_response_empty_choices_is_not_error() -> None:
    """空 choices（异常网关响应）→ 空聚合不误判为失败（状态面在 HTTP 层）；
    usage 保持三键零值稳定形态（聚合契约形状恒定，不随载荷缺键塌缩）。"""
    result = gw.aggregate_response({"choices": [], "usage": {}})
    assert result["text"] == ""
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_map_finish_reason_normalizes() -> None:
    assert gw.map_finish_reason("stop") == "stop"
    assert gw.map_finish_reason("length") == "length"
    assert gw.map_finish_reason("tool_calls") == "tool_calls"
    assert gw.map_finish_reason(None) == "stop"
    assert gw.map_finish_reason("content_filter") == "stop"


# ── SSE 增量聚合 ─────────────────────────────────────────────────────────


def test_parse_sse_line_variants() -> None:
    chunk = {"choices": [{"delta": {"content": "a"}}]}
    assert gw.parse_sse_line(f"data: {json.dumps(chunk)}") == chunk
    assert gw.parse_sse_line("data: [DONE]") is gw.SSE_DONE
    assert gw.parse_sse_line("event: ping") is None
    assert gw.parse_sse_line(": keepalive") is None
    assert gw.parse_sse_line("") is None
    # data: 行但载荷非 JSON = 流协议损坏 → ValueError（调用方翻译 service_down）
    with pytest.raises(ValueError):
        gw.parse_sse_line("data: not-json")


def test_aggregate_sse_chunks_accumulates_text_and_usage() -> None:
    chunks = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "你"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "好"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "推理"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    result = gw.aggregate_sse_chunks(chunks)
    assert result["text"] == "你好"
    assert result["thinking_text"] == "推理"
    assert result["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 3
    assert result["usage"]["completion_tokens"] == 2
    assert result["tool_calls"] == []
    assert result["status"] == "streamed"


def test_aggregate_sse_chunks_assembles_tool_calls_by_index() -> None:
    """工具调用按 index 组装：id/name 首见捕获，arguments 增量拼接。"""
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_a", "function": {"name": "search", "arguments": '{"q":'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "1}"}},
                            {"index": 1, "id": "call_b", "function": {"name": "open", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    result = gw.aggregate_sse_chunks(chunks)
    assert result["finish_reason"] == "tool_calls"
    assert result["tool_calls"] == [
        {"id": "call_a", "type": "function", "function": {"name": "search", "arguments": '{"q":1}'}},
        {"id": "call_b", "type": "function", "function": {"name": "open", "arguments": "{}"}},
    ]


def test_aggregate_sse_chunks_empty_is_safe() -> None:
    result = gw.aggregate_sse_chunks([])
    assert result["text"] == ""
    assert result["finish_reason"] == "stop"


# ── 失败契约映射 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (400, "bad_request"),
        (401, "auth"),
        (403, "auth"),
        (429, "rate_limit"),
        (502, "service_down"),
        (500, "service_down"),
    ],
)
def test_code_for_status_mapping(status: int, expected_code: str) -> None:
    assert gw.code_for_status(status) == expected_code


def test_code_for_status_quota_semantics_via_body() -> None:
    """429 且响应体含配额语义 → quota（网关冷却面区分限流与配额耗尽）。"""
    assert gw.code_for_status(429, body="quota exhausted for today") == "quota"


# ── complete_via_gateway（MockTransport 替身） ────────────────────────────


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_complete_non_stream_roundtrip() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    result = await gw.complete_via_gateway(
        "http://127.0.0.1:8317",
        model_name="glm-5.3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=False,
        params={"temperature": 0.5},
        client_api_key="sk-test",
        client=_client(handler),
    )
    assert result["text"] == "ok"
    assert result["status"] == "streamed"
    assert captured["url"] == "http://127.0.0.1:8317/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["temperature"] == 0.5


async def test_own_client_bypasses_proxy_env() -> None:
    """自建 client 恒 trust_env=False（网关=本机回环，env/Windows 注册表系统代理
    经 urllib.getproxies 进入 httpx 即恒 502——2026-11-21 装机面实证）。注入
    client 的形态不受此约束（调用方自持）。"""
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("sentinel", request=None)

        async def aclose(self) -> None:
            return None

    monkey = pytest.MonkeyPatch()
    monkey.setattr(gw.httpx, "AsyncClient", _FakeClient)
    try:
        with pytest.raises(GatewayUpstreamError) as exc_info:
            await gw.complete_via_gateway(
                "http://127.0.0.1:8317",
                model_name="m",
                messages=[],
                tools=None,
                stream=False,
                params={},
            )
    finally:
        monkey.undo()
    assert captured.get("trust_env") is False
    assert exc_info.value.code == "service_down"


async def test_complete_normalizes_v1_suffixed_base() -> None:
    """托管形态 slot 注入 api_base 带 /v1 后缀（OpenAI 惯例形态）→ 归一化剥除，
    防双 /v1（网关无 /v1/v1 路由，双拼恒 404）。裸形态见上一测（两形态区分度）。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    result = await gw.complete_via_gateway(
        "http://127.0.0.1:8317/v1",
        model_name="m",
        messages=[],
        tools=None,
        stream=False,
        params={},
        client=_client(handler),
    )
    assert result["text"] == "ok"
    assert captured["url"] == "http://127.0.0.1:8317/v1/chat/completions"


async def test_complete_stream_sse_roundtrip() -> None:
    sse_body = (
        'data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "好"}}]}\n\n'
        "\n"
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is True
        return httpx.Response(200, text=sse_body, headers={"Content-Type": "text/event-stream"})

    result = await gw.complete_via_gateway(
        "http://127.0.0.1:8317",
        model_name="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=True,
        params={},
        client=_client(handler),
    )
    assert result["text"] == "你好"
    assert result["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 2


@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (429, "rate limited", "rate_limit"),
        (429, "quota exhausted", "quota"),
        (401, "unauthorized", "auth"),
        (400, "bad payload", "bad_request"),
        (500, "boom", "service_down"),
    ],
)
async def test_complete_maps_http_failures_to_llm_failure_codes(
    status: int, body: str, expected_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    with pytest.raises(GatewayUpstreamError) as exc_info:
        await gw.complete_via_gateway(
            "http://127.0.0.1:8317",
            model_name="m",
            messages=[],
            tools=None,
            stream=False,
            params={},
            client=_client(handler),
        )
    assert exc_info.value.code == expected_code
    assert body in exc_info.value.message


async def test_complete_connect_error_is_service_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GatewayUpstreamError) as exc_info:
        await gw.complete_via_gateway(
            "http://127.0.0.1:8317",
            model_name="m",
            messages=[],
            tools=None,
            stream=False,
            params={},
            client=_client(handler),
        )
    assert exc_info.value.code == "service_down"


async def test_complete_malformed_sse_is_service_down() -> None:
    """SSE 语义坏流（非 JSON data 且非 DONE）→ service_down 结构化失败。"""
    sse = "data: {broken json}\n\n"
    served = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        served["calls"] += 1
        return httpx.Response(200, text=sse)

    with pytest.raises(GatewayUpstreamError) as exc_info:
        await gw.complete_via_gateway(
            "http://127.0.0.1:8317",
            model_name="m",
            messages=[],
            tools=None,
            stream=True,
            params={},
            client=_client(handler),
        )
    assert exc_info.value.code == "service_down"


async def test_to_failure_payload_shape() -> None:
    exc = GatewayUpstreamError("rate_limit", "slow down", retry_after=3.0)
    payload = gw.to_failure_payload(exc)
    assert payload["status"] == "error"
    assert payload["llm_failure"]["code"] == "rate_limit"
    assert payload["llm_failure"]["message"] == "slow down"
    assert payload["llm_failure"]["retry_after"] == 3.0
    unknown = gw.to_failure_payload(RuntimeError("unexpected"))
    assert unknown["llm_failure"]["code"] == "service_down"
