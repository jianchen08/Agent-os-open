# @feature: FP-T07 llm api | @ci: python-coverage
"""DSH 接口方言契约测试（LLM 两类收敛批 4；主方案 §2.4 四证据 / §6.4-b）。

契约真值 = §2.4 实证四证据（逐条对应本文件测试段）：
1. **提供包形状**：llm 服务提供包三件套（llm 服务本体端点 / 凭据包 /
   agent-default-model 包 ``{provider, model}``）——``DshProviderPack`` 逐件承载；
2. **listProviders / listModels 咨询发现**：镜像
   ``LlmProviderInfo{id,name}`` / ``LlmModelInfo{provider,id,name}``；
3. **每流凭据**（request-level-llm-config-credentials）：凭据在每次调用时解析
   （``${VAR}`` 展开 + 配置读面），不冻结在注册期；
4. **LlmFailure 归一**：DSH ``{message, code, status?, providerRetryAfterMs?,
   requestId?}`` → 既有 ``llm_failure{code, message, retry_after?, request_id?}``
   词汇（与 ``_PluginProviderFailure`` 同一五值 code 空间）。

另含：
- 注册表扩展点断言（dsh 经注册点接入、openai-compat 条目零变化）；
- 连通 smoke（桩面：真实 httpx 序列化/SSE 解析栈 + MockTransport，不触网）；
- agentDefaultModel 通道对照（既有翻译通道 vs 方言通道，模型身份一致、
  provider 身份按 ``provider_route`` 桥接）。

真实依赖：httpx（MockTransport）；其余外部面全桩。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"
for _d in (_SYSTEM_LLM_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_SYSTEM_LLM_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

import llm_adapter  # noqa: E402
import llm_dialects  # noqa: E402
import llm_dsh  # noqa: E402
import llm_wire  # noqa: E402

_KEY_ENV = "AGENTOS_TEST_DSH_KEY"
_KEY_ENV_B = "AGENTOS_TEST_DSH_KEY_B"
_UNRESOLVED_ENV = "AGENTOS_TEST_DSH_KEY_UNSET"


def _llm_data(**overrides: Any) -> dict[str, Any]:
    """llm.yaml 形状的注入视图（providers/models 段；dsh 条目 = 新接 provider）。"""
    data: dict[str, Any] = {
        "providers": {
            "dsh_runtime": {
                "type": "dsh",
                "api_base": "http://dsh.test",
                "provider_route": "dsh",
                "keys": [{"id": "dsh_main", "api_key": f"${{{_KEY_ENV}}}"}],
                "default_model": {"provider": "dsh", "model": "deepseek-v4-flash"},
            },
            "openai_prov": {"type": "openai", "api_base": "http://gw.test/v1", "api_key": "sk-openai"},
        },
        "models": {
            "dsh-chat": {
                "provider": "dsh_runtime",
                "model_name": "deepseek-v4-flash",
                "default_params": {"temperature": 0.2, "max_tokens": 512},
            },
            "openai-chat": {"provider": "openai_prov", "model_name": "up-chat"},
        },
    }
    data.update(overrides)
    return {"llm": data}


def _dialect(config: dict[str, Any] | None = None) -> llm_dsh.DshDialect:
    payload = _llm_data() if config is None else config
    return llm_dsh.DshDialect(config_provider=lambda: payload)


def _adapter_data(key: str = "sk-direct") -> dict[str, Any]:
    """适配面用配置：凭据已是注入期解析产物（生产形态 = set_config 展开后视图）。"""
    data = _llm_data()
    data["llm"]["providers"]["dsh_runtime"]["keys"] = [{"id": "dsh_main", "api_key": key}]
    return data


@pytest.fixture(autouse=True)
def _isolated_pool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每用例独立连接池槽位（关旧池；池语义自身在 wire 测试单测）。"""
    monkeypatch.setattr(llm_wire, "_shared_client", None)
    yield
    client = llm_wire._shared_client
    if client is not None:
        import asyncio

        asyncio.run(client.aclose())
    llm_wire._shared_client = None


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(llm_wire, "_shared_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _sse_event(payload: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def _sse_response(lines: list[bytes], request: httpx.Request) -> httpx.Response:
    async def _body() -> Any:
        for line in lines:
            yield line

    return httpx.Response(200, content=_body(), request=request)


def _stream_events(*, text: str = "你好🌊", reasoning: str | None = "思考", tool: bool = False) -> list[bytes]:
    """典型 DSH StreamChunk 序列（文本/推理/工具增量 + usage + finish）。"""
    events = [
        _sse_event({"type": "block-start", "index": 0, "blockType": "text"}),
        _sse_event({"type": "text-delta", "index": 0, "text": text}),
    ]
    if reasoning is not None:
        events.insert(1, _sse_event({"type": "reasoning-delta", "index": 0, "text": reasoning}))
    if tool:
        events.append(
            _sse_event(
                {
                    "type": "tool-call-delta",
                    "index": 1,
                    "id": "call_1",
                    "name": "file_read",
                    "argumentsDelta": '{"path": "a.txt"}',
                }
            )
        )
    events.append(
        _sse_event(
            {
                "type": "usage",
                "usage": {
                    "inputTokens": 11,
                    "outputTokens": 7,
                    "cacheReadTokens": 4,
                    "totalTokens": 22,
                },
            }
        )
    )
    events.append(_sse_event({"type": "block-end", "index": 0, "block": {"type": "text", "text": text}}))
    events.append(_sse_event({"type": "finish", "reason": {"kind": "tool-calls" if tool else "stop"}}))
    events.append(_sse_done())
    return events


# ═══════════════════ 证据 1：提供包形状（三件套） ═══════════════════


def test_provider_packs_carry_service_credential_default_model_shape() -> None:
    """三件套逐件在位：服务端点 / 凭据包（引用保留）/ agent-default-model 包。"""
    packs = llm_dsh.provider_packs(_llm_data())
    assert len(packs) == 1
    pack = packs[0]
    # ① llm 服务本体：端点 + DSH provider 路由键（两件都在包上）
    assert (pack.name, pack.provider, pack.base_url) == ("dsh_runtime", "dsh", "http://dsh.test")
    # ② 凭据包：原引用保留（诊断面），不落明文解析产物
    assert pack.credential.provider == "dsh"
    assert pack.credential.api_key_ref == f"${{{_KEY_ENV}}}"
    # ③ agent-default-model 包：{provider, model} 两字段
    assert pack.default_model == llm_dsh.DshDefaultModelPack(provider="dsh", model="deepseek-v4-flash")


def test_provider_packs_only_dsh_entries_and_route_fallback() -> None:
    """只有 type=dsh 条目成包；provider_route 缺省退条目键；无端点不成包。"""
    data = _llm_data(
        providers={
            "dsh_named": {"type": "dsh", "api_base": "http://a.test", "api_key": "sk-a"},
            "dsh_no_base": {"type": "dsh", "provider_route": "x"},
            "dsh_caps_type": {"type": "DSH", "api_base": "http://b.test", "api_key": "sk-b"},
            "openai_prov": {"type": "openai", "api_base": "http://gw.test/v1", "api_key": "sk-openai"},
        }
    )
    packs = llm_dsh.provider_packs(data)
    assert [p.name for p in packs] == ["dsh_caps_type", "dsh_named"]
    # 区分度：type 大小写归一；provider_route 缺省 = 条目键；无 api_base 不可拨号→不成包
    assert {p.provider for p in packs} == {"dsh_caps_type", "dsh_named"}
    assert all(p.base_url for p in packs)
    assert all(p.default_model is None for p in packs)


def test_provider_pack_credential_resolves_env_per_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """每流凭据解析：同一包在不同时刻解析出不同 key；未设置即 fail-closed。"""
    pack = llm_dsh.provider_packs(_llm_data())[0]
    monkeypatch.setenv(_KEY_ENV, "sk-first")
    assert llm_dsh.resolve_credential(pack.credential) == "sk-first"
    monkeypatch.setenv(_KEY_ENV, "sk-second")
    assert llm_dsh.resolve_credential(pack.credential) == "sk-second"
    unresolved = llm_dsh.DshProviderPack(
        name="p",
        provider="p",
        base_url="http://x",
        credential=llm_dsh.DshCredentialPack(
            provider="p", api_key_ref=f"${{{_UNRESOLVED_ENV}}}", api_key=""
        ),
        default_model=None,
    )
    with pytest.raises(llm_dsh.DshCredentialError, match=_UNRESOLVED_ENV):
        llm_dsh.resolve_credential(unresolved.credential)
    # 非法字符（控制/空白/非 ASCII）同拒——DSH api-key 契约的 illegalCharacters 形态
    with pytest.raises(llm_dsh.DshCredentialError, match="非法字符"):
        llm_dsh.resolve_credential(
            llm_dsh.DshCredentialPack(provider="p", api_key_ref="sk-中文", api_key="")
        )


# ═══════════════════ 证据 2：listProviders / listModels ═══════════════════


async def test_list_providers_mirrors_dsh_provider_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /llm/providers → LlmProviderInfo{id,name} 逐字段镜像。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"providers": [{"id": "deepseek", "name": "DeepSeek"}, {"id": "modlens", "name": "ModLens"}]},
        )

    _install_transport(monkeypatch, handler)
    infos = await llm_dsh.list_providers("http://dsh.test", api_key="sk-abc")
    assert [(i.id, i.name) for i in infos] == [("deepseek", "DeepSeek"), ("modlens", "ModLens")]
    assert seen["url"] == "http://dsh.test/llm/providers"
    assert seen["auth"] == "Bearer sk-abc"


async def test_list_models_queries_named_provider_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /llm/providers/{provider}/models → LlmModelInfo{provider,id,name}。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "models": [
                    {"provider": "deepseek", "id": "deepseek-v4-flash", "name": "DSH DeepSeek V4 Flash"},
                    {"provider": "deepseek", "id": "deepseek-v4-pro", "name": "DSH DeepSeek V4 Pro"},
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    models = await llm_dsh.list_models("http://dsh.test/", "deepseek", api_key="")
    assert seen["url"] == "http://dsh.test/llm/providers/deepseek/models"
    assert [m.id for m in models] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert all(m.provider == "deepseek" for m in models)


async def test_consult_http_failure_normalized_to_existing_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """咨询面 HTTP 失败 → LlmFailure 归一（code 落既有五值空间 + retry_after 秒）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "llm_failure": {
                    "message": "rate limited",
                    "code": "RATE_LIMIT",
                    "providerRetryAfterMs": 2500,
                    "requestId": "req-9",
                }
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(llm_dsh.DshServiceError) as raised:
        await llm_dsh.list_providers("http://dsh.test", api_key="k")
    assert raised.value.code == "rate_limit"
    assert raised.value.retry_after == 2.5
    assert raised.value.request_id == "req-9"
    assert str(raised.value).startswith("[llm_failure code=rate_limit]")


async def test_consult_rejects_malformed_catalog_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约面形状不符 → DshContractError（不静默当空目录）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": "nope"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(llm_dsh.DshContractError, match="models"):
        await llm_dsh.list_models("http://dsh.test", "deepseek")


def test_catalog_entries_shape_matches_llm_yaml_models_convention() -> None:
    """咨询产物 → llm.yaml models 条目形状（provider/model_name/display_name）。"""
    entries = llm_dsh.catalog_entries(
        "dsh_runtime",
        (
            llm_dsh.DshModelInfo(provider="deepseek", id="deepseek-v4-flash", name="Flash"),
            llm_dsh.DshModelInfo(provider="deepseek", id="deepseek-v4-pro", name=""),
        ),
    )
    assert entries["deepseek-v4-flash"] == {
        "provider": "dsh_runtime",
        "model_name": "deepseek-v4-flash",
        "display_name": "Flash",
    }
    # 空显示名不猜翻译：回落 model id
    assert entries["deepseek-v4-pro"]["display_name"] == "deepseek-v4-pro"


# ═══════════════════ 证据 3：每流凭据 + provider 路由 ═══════════════════


async def test_stream_resolves_credential_per_call_with_provider_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次流调用：凭据随配置/环境逐流解析，body 携带 provider 路由键。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content.decode("utf-8")),
                "url": str(request.url),
            }
        )
        return _sse_response(_stream_events(reasoning=None), request)

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [{"role": "user", "content": "hi"}], tools=None, stream=True, extra={})

    monkeypatch.setenv(_KEY_ENV, "sk-one")
    stream = await dialect.stream("http://dsh.test", "", body, timeout=5.0, model_label="dsh-chat")
    assert await _collect_text(stream) == "你好🌊"

    monkeypatch.setenv(_KEY_ENV, "sk-two")
    stream = await dialect.stream("http://dsh.test", "", body, timeout=5.0, model_label="dsh-chat")
    assert await _collect_text(stream) == "你好🌊"

    assert [s["auth"] for s in seen] == ["Bearer sk-one", "Bearer sk-two"]
    first = seen[0]
    assert first["url"] == "http://dsh.test/llm/stream"
    parsed = first["body"]
    assert parsed["provider"] == "dsh"
    assert parsed["model"] == "deepseek-v4-flash"
    assert parsed["messages"] == [{"role": "user", "content": "hi"}]
    assert parsed["stream"] is True


async def test_stream_fail_closed_on_unresolved_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据解析不出来 → 发起 HTTP 前 fail-closed（transport 零调用）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        calls.append(str(request.url))
        return httpx.Response(500, json={})

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv(_KEY_ENV, raising=False)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [{"role": "user", "content": "hi"}], tools=None, stream=True, extra={})
    with pytest.raises(llm_dsh.DshCredentialError, match=_KEY_ENV):
        await dialect.stream("http://dsh.test", "", body, timeout=5.0, model_label="dsh-chat")
    assert calls == []


async def test_stream_rejects_unresolved_base_url_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_base 占位符未解析 → 发起 HTTP 前 DshConfigError（对齐 api_key 纪律）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    data = _llm_data(
        providers={
            "dsh_runtime": {
                "type": "dsh",
                "api_base": "${AGENTOS_TEST_DSH_BASE}",
                "provider_route": "dsh",
                "api_key": "sk-abc",
            }
        },
        models={"dsh-chat": {"provider": "dsh_runtime", "model_name": "m"}},
    )
    dialect = _dialect(data)
    body = dialect.build_payload("m", [{"role": "user", "content": "hi"}], tools=None, stream=True, extra={})
    with pytest.raises(llm_dsh.DshConfigError, match="api_base"):
        await dialect.stream("${AGENTOS_TEST_DSH_BASE}", "sk-abc", body, timeout=5.0, model_label="dsh-chat")
    assert calls == []


async def test_stream_requires_provider_route_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型解析不到 dsh provider 路由 → DshConfigError（契约必填 provider）。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    dialect = _dialect({"llm": {"providers": {}, "models": {}}})
    with pytest.raises(llm_dsh.DshConfigError, match="provider"):
        await dialect.stream(
            "http://dsh.test", "sk-x", {"model": "m", "messages": []}, timeout=5.0, model_label="unknown"
        )


async def test_stream_chunk_vocabulary_translates_to_normalized_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSH StreamChunk → 既有归一 chunk 形状（text/reasoning/tool_call/usage/finish）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(_stream_events(tool=True), request)

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [{"role": "user", "content": "hi"}], tools=None, stream=True, extra={})
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="dsh-chat")
    chunks = [chunk async for chunk in stream]
    await stream.aclose()

    text = "".join(
        c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content
    )
    reasoning = "".join(
        c.choices[0].delta.reasoning_content
        for c in chunks
        if c.choices and c.choices[0].delta.reasoning_content
    )
    assert (text, reasoning) == ("你好🌊", "思考")
    tool_delta = next(c for c in chunks if c.choices and c.choices[0].delta.tool_calls)
    tc = tool_delta.choices[0].delta.tool_calls[0]
    assert (tc.index, tc.id, tc.function.name, tc.function.arguments) == (
        1,
        "call_1",
        "file_read",
        '{"path": "a.txt"}',
    )
    usage_chunk = next(c for c in chunks if c.usage is not None)
    # DSH 计数互斥（input 不含缓存）→ 既有口径 prompt_tokens 含缓存
    assert (usage_chunk.usage.prompt_tokens, usage_chunk.usage.completion_tokens) == (15, 7)
    assert usage_chunk.usage.total_tokens == 22
    assert usage_chunk.usage.prompt_tokens_details.cached_tokens == 4
    finish = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
    assert finish == ["tool_calls"]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("stop", "stop"), ("tool-calls", "tool_calls"), ("max-tokens", "length")],
)
async def test_finish_reason_vocabulary_maps_to_our_five_values(
    monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
) -> None:
    """DSH FinishReason.kind → 既有五值（stop/length/tool_calls/error/interrupted）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [_sse_event({"type": "finish", "reason": {"kind": kind}}), _sse_done()], request
        )

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [], tools=None, stream=True, extra={})
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="dsh-chat")
    reasons = [c.choices[0].finish_reason async for c in stream]
    await stream.aclose()
    assert reasons == [expected]


async def test_post_assembles_stream_into_nonstream_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """非流式面 = 同一 DSH 流端点 + 本地组装（DSH 只有 stream 一种调用 API）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://dsh.test/llm/stream"
        return _sse_response(_stream_events(text="done", reasoning="r", tool=True), request)

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [{"role": "user", "content": "hi"}], tools=None, stream=False, extra={})
    assert "stream" not in body  # 非流式不带 stream 标志；传输面仍是同一流端点
    response = await dialect.post("http://dsh.test", "sk-x", body, timeout=5.0)
    assert response.choices[0].message.content == "done"
    assert response.choices[0].message.reasoning_content == "r"
    assert response.choices[0].finish_reason == "tool_calls"
    tool_calls = response.choices[0].message.tool_calls
    assert [(t.id, t.function.name, t.function.arguments) for t in tool_calls] == [
        ("call_1", "file_read", '{"path": "a.txt"}')
    ]
    assert response.usage.prompt_tokens == 15
    assert response.usage.prompt_tokens_details.cached_tokens == 4


# ═══════════════════ 证据 4：LlmFailure 归一 ═══════════════════


@pytest.mark.parametrize(
    ("dsh_code", "expected"),
    [
        ("RATE_LIMIT", "rate_limit"),
        ("QUOTA", "quota"),
        ("ACCOUNT_QUOTA", "quota"),
        ("AUTH", "auth"),
        ("MISSING_CREDENTIAL", "auth"),
        ("INVALID_CREDENTIAL", "auth"),
        ("BAD_REQUEST", "bad_request"),
        ("INVALID_ARGS", "bad_request"),
        ("CONTEXT_WINDOW_EXCEEDED", "bad_request"),
        ("IMAGE_OFFLOAD_REQUIRED", "bad_request"),
        ("EMPTY_RESPONSE", "bad_request"),
        ("NO_ADAPTER", "bad_request"),
        # 未知/缺失 → 上游不可用语义（可冷却、可兜底）
        ("SOMETHING_NEW", "service_down"),
        ("", "service_down"),
        # 已归一形态幂等（门面回显 llm_failure 时二次归一不漂移）
        ("rate_limit", "rate_limit"),
    ],
)
def test_normalize_failure_maps_dsh_codes_into_existing_vocabulary(dsh_code: str, expected: str) -> None:
    normalized = llm_dsh.normalize_failure(
        {"code": dsh_code, "message": "boom", "providerRetryAfterMs": 1500, "requestId": "r-1", "status": 429}
    )
    assert normalized == {
        "code": expected,
        "message": "boom",
        "retry_after": 1.5,
        "request_id": "r-1",
        "status": 429,
    }


def test_normalize_failure_defaults_and_unit_guards() -> None:
    """缺 message/非法 retry 值 → 兜底文案 + 丢弃非法延迟（不猜数值）。"""
    assert llm_dsh.normalize_failure(None)["code"] == "service_down"
    assert "DSH" in llm_dsh.normalize_failure(None)["message"]
    for bad in (0, -3, "nope"):
        normalized = llm_dsh.normalize_failure({"code": "QUOTA", "providerRetryAfterMs": bad})
        assert "retry_after" not in normalized
    # 秒值形态（既有词汇回显）不被当成毫秒二次换算
    assert llm_dsh.normalize_failure({"code": "rate_limit", "retry_after": 3.0})["retry_after"] == 3.0


async def test_stream_terminal_failure_chunk_becomes_partial_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """流内终态 FinishReason{kind:error, failure} → 半截快照 + 既有失败词汇。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _sse_event({"type": "text-delta", "index": 0, "text": "半截"}),
                _sse_event(
                    {
                        "type": "finish",
                        "reason": {
                            "kind": "error",
                            "failure": {"code": "QUOTA", "message": "balance exhausted"},
                        },
                    }
                ),
                _sse_done(),
            ],
            request,
        )

    _install_transport(monkeypatch, handler)
    # 适配面与方言共享同一注入视图（生产形态：_config_models.set_config 单点）
    import _config_models

    monkeypatch.setattr(_config_models, "_config", _adapter_data())
    face = llm_adapter.LLMAdapterFace(config_provider=_adapter_data)
    result = await face.execute("dsh-chat", [{"role": "user", "content": "hi"}], stream=True)
    assert result["status"] == "error"
    assert result["dialect"] == "dsh"
    assert result["partial"]["text"] == "半截"
    assert "[llm_failure code=quota]" in result["llm_error_info"]["error_message"]


async def test_nonstream_service_failure_raises_normalized_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """非流式 HTTP 失败 → DshServiceError（code 归一；不返回伪成功空响应）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"llm_failure": {"code": "AUTH", "message": "bad key"}})

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [], tools=None, stream=False, extra={})
    with pytest.raises(llm_dsh.DshServiceError) as raised:
        await dialect.post("http://dsh.test", "sk-x", body, timeout=5.0)
    assert raised.value.code == "auth"
    assert raised.value.llm_failure["message"] == "bad key"


# ═══════════════════ 注册表扩展点（openai-compat 零变化） ═══════════════════


def test_dsh_dialect_registered_through_extension_point() -> None:
    """dsh 经注册点接入：type 词 dsh → 实现键 dsh；openai-compat 不受扰。"""
    dialect = llm_dialects.get_dialect("dsh")
    assert dialect.key == "dsh"
    assert llm_dialects.resolve_dialect_key("dsh") == "dsh"
    assert "dsh" in llm_dialects.type_keys()
    assert llm_dialects.resolve_dialect_key("openai") == llm_dialects.OPENAI_COMPAT


def test_dsh_registration_does_not_shadow_openai_type_space() -> None:
    """两方言的 type 词空间不重叠（同一 type 词不指向两个实现）。"""
    openai_side = set(llm_dialects.get_dialect(llm_dialects.OPENAI_COMPAT).type_keys)
    dsh_side = set(llm_dialects.get_dialect("dsh").type_keys)
    assert openai_side & dsh_side == set()
    assert dsh_side == {"dsh"}


# ═══════════════════ 连通 smoke（桩面）+ agentDefaultModel 对照 ═══════════════════


async def test_connectivity_smoke_through_adapter_face(monkeypatch: pytest.MonkeyPatch) -> None:
    """连通 smoke（桩面实测）：经 llm_adapter.execute 走完整方言链，取证形态=MockTransport。"""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return _sse_response(_stream_events(text="smoke-ok", reasoning=None), request)

    _install_transport(monkeypatch, handler)
    monkeypatch.setenv(_KEY_ENV, "sk-smoke")
    # 适配面与方言共享同一注入视图（生产形态：_config_models.set_config 单点）
    import _config_models

    monkeypatch.setattr(_config_models, "_config", _adapter_data())
    face = llm_adapter.LLMAdapterFace(config_provider=_adapter_data)
    result = await face.execute(
        "dsh-chat",
        [{"role": "user", "content": "ping"}],
        stream=True,
        params={"max_tokens": 128},
    )
    assert result["status"] == "streamed"
    assert result["dialect"] == "dsh"
    assert result["text"] == "smoke-ok"
    assert result["usage"]["total_tokens"] == 22
    assert requests[0]["provider"] == "dsh"
    # llm.yaml snake 参数 → DSH 调用控制字段（adapter-owned 参数原样透传）
    assert requests[0]["maxTokens"] == 128


def _load_runtime_workspace() -> Any:
    """按路径装载 dsh_adapter.runtime_workspace（跨插件只读引用；测试面唯一依赖）。"""
    path = _REPO_ROOT / "plugins" / "shared" / "dsh_adapter" / "runtime_workspace.py"
    spec = importlib.util.spec_from_file_location("_dsh_runtime_workspace_for_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    dsh_dir = str(path.parent)
    if dsh_dir not in sys.path:
        sys.path.insert(0, dsh_dir)
    spec.loader.exec_module(module)
    return module


async def test_agent_default_model_channel_matches_dsh_dialect_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通道对照：同一 DSH 模型源，agentDefaultModel 翻译通道与方言通道的身份一致。

    取证形态：既有翻译通道 = ``runtime_workspace.default_agent_model``（真实现）；
    方言通道 = 提供包 default_model + listModels 咨询（桩面 DSH llm 服务）。
    结论口径：**模型身份（model id）两条通道逐字一致**；provider 身份按设计不同
    ——翻译通道给 llm.yaml provider 键（DSH 侧需已注册 provider），方言通道给
    ``provider_route``（DSH provider 路由键），桥接字段即 provider_route。
    """
    rw = _load_runtime_workspace()
    llm = _llm_data()["llm"]
    llm["defaults"] = {"chat": "dsh-chat"}
    translated = rw.default_agent_model(llm)
    assert translated == {"provider": "dsh_runtime", "model": "deepseek-v4-flash"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"provider": "dsh", "id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"}]},
        )

    _install_transport(monkeypatch, handler)
    pack = llm_dsh.provider_packs({"llm": llm})[0]
    models = await llm_dsh.list_models(pack.base_url, pack.provider, api_key="k")
    assert pack.default_model is not None
    # 模型身份：翻译通道 vs agent-default-model 包 vs 咨询目录 —— 三者一致
    assert {translated["model"], pack.default_model.model, models[0].id} == {"deepseek-v4-flash"}
    # provider 身份：llm.yaml 键 vs DSH 路由键，桥接字段 = provider_route
    assert translated["provider"] == pack.name
    assert pack.provider == models[0].provider == "dsh"


# ═══════════════════ 防御面（契约 fail-closed 分支） ═══════════════════


def test_provider_packs_accepts_bare_llm_data_and_rejects_bad_shapes() -> None:
    """配置读面两形态（``{"llm": …}`` / 裸 llm 数据）+ 脏形态不猜。"""
    bare = _llm_data()["llm"]
    assert llm_dsh.provider_packs(bare)[0].name == "dsh_runtime"
    assert llm_dsh.provider_packs(None) == ()
    assert llm_dsh.provider_packs("nope") == ()
    assert llm_dsh.provider_packs({"llm": "nope"}) == ()
    assert llm_dsh.provider_packs({"llm": {"providers": ["junk"]}}) == ()
    # 非对象条目与坏 default_model 形态：跳过/不提供包，不抛（缺口 fail-visible）
    dirty = {
        "llm": {
            "providers": {
                "junk": "not-a-mapping",
                "half": {"type": "dsh", "api_base": "http://h", "default_model": {"provider": "dsh"}},
            }
        }
    }
    packs = llm_dsh.provider_packs(dirty)
    assert [p.name for p in packs] == ["half"]
    assert packs[0].default_model is None


def test_resolve_credential_rejects_empty_value() -> None:
    """空凭据（既无明文也无占位符）→ DshCredentialError。"""
    with pytest.raises(llm_dsh.DshCredentialError, match="为空"):
        llm_dsh.resolve_credential(llm_dsh.DshCredentialPack(provider="p", api_key_ref="", api_key=""))


async def test_consult_rejects_non_json_and_bad_item_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """咨询面：非 JSON 200 / 条目非对象 / 条目缺 id / 空 provider —— 全部 fail-closed。"""
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, text="<html>"))
    with pytest.raises(llm_dsh.DshContractError, match="不是 JSON"):
        await llm_dsh.list_providers("http://dsh.test")

    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json={"providers": ["junk"]}))
    with pytest.raises(llm_dsh.DshContractError, match="条目不是对象"):
        await llm_dsh.list_providers("http://dsh.test")

    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json={"providers": [{"name": "x"}]}))
    with pytest.raises(llm_dsh.DshContractError, match="缺 id"):
        await llm_dsh.list_providers("http://dsh.test")

    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json={"models": [{"name": "x"}]}))
    with pytest.raises(llm_dsh.DshContractError, match="缺 id"):
        await llm_dsh.list_models("http://dsh.test", "deepseek")

    with pytest.raises(llm_dsh.DshConfigError, match="provider 路由键"):
        await llm_dsh.list_models("http://dsh.test", "  ")

    with pytest.raises(llm_dsh.DshConfigError, match="为空"):
        await llm_dsh.list_providers("http://dsh.test".replace("http://dsh.test", ""))


async def test_consult_bare_array_payload_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """裸数组载荷（无外层键）同被接受（门面两形态宽松读、严格形状）。"""
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, json=[{"id": "deepseek", "name": "DeepSeek"}]),
    )
    infos = await llm_dsh.list_providers("http://dsh.test")
    assert [(i.id, i.name) for i in infos] == [("deepseek", "DeepSeek")]


async def test_http_failure_payload_shapes_and_status_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败载荷三形态：内嵌 llm_failure / 顶层 code / 纯文本（状态码兜底分类）。"""
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(400, json={"code": "BAD_REQUEST", "message": "bad args"}),
    )
    with pytest.raises(llm_dsh.DshServiceError) as top_level:
        await llm_dsh.list_providers("http://dsh.test")
    assert top_level.value.code == "bad_request"
    assert top_level.value.llm_failure["status"] == 400

    _install_transport(monkeypatch, lambda _request: httpx.Response(503, text="upstream down"))
    with pytest.raises(llm_dsh.DshServiceError) as fallback:
        await llm_dsh.list_providers("http://dsh.test")
    assert fallback.value.code == "service_down"
    assert "upstream down" in fallback.value.llm_failure["message"]


async def test_stream_rejects_empty_base_url_and_non_object_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 api_base fail-closed；流帧非对象 fail-closed。"""
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [], tools=None, stream=True, extra={})
    with pytest.raises(llm_dsh.DshConfigError, match="为空"):
        await dialect.stream("", "sk-x", body, timeout=5.0, model_label="dsh-chat")

    # 非对象流帧（JSON 数组）：frame 不是 StreamChunk 对象 → fail-closed
    bad_frame = ("data: " + json.dumps([1, 2]) + "\n\n").encode()
    _install_transport(monkeypatch, lambda _request: _sse_response([bad_frame, _sse_done()], _request))
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="dsh-chat")
    with pytest.raises(llm_dsh.DshContractError, match="JSON 对象"):
        await anext(stream)
    await stream.aclose()


async def test_stream_skips_non_data_and_malformed_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 噪声行（event:/注释/坏 JSON）跳过，正常帧照常产出；close 后迭代即止。"""

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            b": keepalive\n\n",
            b"event: ping\n\n",
            b"data: {broken\n\n",
            _sse_event({"type": "text-delta", "index": 0, "text": "ok"}),
            _sse_done(),
        ]
        return _sse_response(lines, request)

    _install_transport(monkeypatch, handler)
    dialect = _dialect()
    body = dialect.build_payload("deepseek-v4-flash", [], tools=None, stream=True, extra={})
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="dsh-chat")
    texts = []
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            texts.append(chunk.choices[0].delta.content)
    await stream.aclose()
    assert texts == ["ok"]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_stream_midway_protocol_error_becomes_partial_semantics() -> None:
    """上游中途断流（RemoteProtocolError）→ StopAsyncIteration（半截语义由消费机承接）。"""

    class _ExplodingResponse:
        def aiter_lines(self) -> Any:
            async def _gen() -> Any:
                yield 'data: {"type": "text-delta", "text": "半截"}'
                raise httpx.RemoteProtocolError("boom")

            return _gen()

        async def aclose(self) -> None:
            return None

    stream = llm_dsh._DshStream(_ExplodingResponse(), "m")
    first = await anext(stream)
    assert first.choices[0].delta.content == "半截"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_build_payload_carries_tools_and_skips_none_params() -> None:
    """payload 构建：tools 透传；None 参数不落 body；既有采样词映射 DSH 字段。"""
    body = llm_dsh.build_payload(
        "m",
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "t"}}],
        stream=False,
        extra={"temperature": None, "top_p": 0.9, "reasoning_effort": "high"},
    )
    assert body["tools"] == [{"type": "function", "function": {"name": "t"}}]
    assert "temperature" not in body
    assert (body["top_p"], body["reasoningEffort"]) == (0.9, "high")
    assert "stream" not in body


async def test_provider_entry_falls_back_to_endpoint_and_rejects_non_dsh(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型→provider 解析：非 dsh provider 条目不成路由 → 端点兜底命中 dsh 条目。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return _sse_response(_stream_events(reasoning=None), request)

    _install_transport(monkeypatch, handler)
    data = _llm_data(
        models={
            # provider 指向 openai 条目（不是 dsh）：不猜，走端点兜底
            "orphan-model": {"provider": "openai_prov", "model_name": "orphan"},
            # provider 键在 providers 段不存在（脏配置）：同样不猜，走端点兜底
            "ghost-model": {"provider": "ghost", "model_name": "ghost"},
        }
    )
    dialect = _dialect(data)
    body = dialect.build_payload("orphan", [], tools=None, stream=True, extra={})
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="orphan-model")
    assert seen[0]["provider"] == "dsh"
    await stream.aclose()
    stream = await dialect.stream("http://dsh.test", "sk-x", body, timeout=5.0, model_label="ghost-model")
    assert seen[1]["provider"] == "dsh"
    await stream.aclose()
    # model_label 与 body.model 皆空 → 候选跳过，端点兜底仍工作
    stream = await dialect.stream("http://dsh.test", "sk-x", {"messages": []}, timeout=5.0)
    assert seen[2]["provider"] == "dsh"
    await stream.aclose()


async def test_nonstream_adapter_path_produces_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    """非流式经适配面走模块级 post 入口（注册表动作面）→ 聚合响应非空。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(_stream_events(text="ns", reasoning="r"), request)

    _install_transport(monkeypatch, handler)
    import _config_models

    monkeypatch.setattr(_config_models, "_config", _adapter_data())
    face = llm_adapter.LLMAdapterFace(config_provider=_adapter_data)
    result = await face.execute("dsh-chat", [{"role": "user", "content": "hi"}], stream=False)
    assert result["status"] == "streamed"
    assert result["dialect"] == "dsh"
    assert result["text"] == "ns"
    assert result["thinking_text"] == "r"
    assert result["finish_reason"] == "stop"


# ─────────────────────────── 测试辅助 ───────────────────────────


async def _collect_text(stream: Any) -> str:
    """收集文本并关闭流（消费面契约：aclose 释放连接）。"""
    try:
        parts: list[str] = []
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
        return "".join(parts)
    finally:
        await stream.aclose()
