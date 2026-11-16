# @feature: FP-T07 llm api | @ci: python-coverage
"""薄客户端——OpenAI 兼容 wire 直连（2026-11-04 网关化）。

职责（网关翻译核心的传输半边；格式半边在 _llm_streaming/provider_adapters）：
- build_body：统一请求词汇（messages/tools/采样参数/extra_body 合并）→
  OpenAI chat.completions wire body
- stream_chat：SSE 流 → 归一 chunk（SimpleNamespace），下游
  _consume_stream/看门狗/重复守卫/StreamTranslator 全链零改动复用
- post_chat：非流式 JSON → 归一响应对象（choices/usage）
- WireHTTPError：HTTP 状态 → 失败契约 code（rate_limit/auth/bad_request/
  service_down），KeySlot 冷却机直接消费
- 共享 httpx 客户端池（进程级单例，连接复用；原 session_pool 的池化语义
  在本模块自持（_get_client 单例）

无第三方 LLM 依赖（仅 httpx），无运行时外联（模型清单/词表下载），
受限网络与离线环境天然安全。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: SSE 流结束哨兵（OpenAI wire 契约）
_SSE_DONE = "[DONE]"

#: HTTP 状态 → 失败契约 code（KeySlot 冷却机的 ErrorKind 由上游映射）
_STATUS_KIND: dict[int, str] = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "bad_request",
    422: "bad_request",
    429: "rate_limit",
}


class WireHTTPError(Exception):
    """HTTP 层失败（状态码已译失败契约 code，供冷却机/兜底机消费）。"""

    def __init__(self, status_code: int, message: str, kind: str | None = None) -> None:
        self.status_code = status_code
        self.code = kind or _STATUS_KIND.get(status_code, "service_down")
        super().__init__(f"[wire http {status_code}] {message}")


class WireTimeoutError(RuntimeError):
    """薄客户端超时（首块/块间/整体——超时后处置语义见 adapter._prefetch_first_chunk）。"""


#: 进程级共享 httpx 客户端（懒建；事件循环绑定由首个调用方所在 loop 决定——
#: llm_service 为单循环 sidecar/合宿成员，与旧 session_pool 的注入前提一致）
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client  # noqa: PLW0603
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(None),  # 超时由调用方按阶段显式控制
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            # LLM 出口强制直连的传输层落点（router_factory.disable_llm_proxy
            # 契约）：httpx 默认 trust_env=True，经 urllib getproxies() 读取
            # 环境变量与 Windows 注册表系统代理——只清 env 解不干净。系统代理
            # 在场时本机 stub 与外呼一起被劫持成 502/流中断（2026-11-04 e2e
            # 实测：httpx 走 127.0.0.1:7993 代理返回空体 502，urllib 直连 200）。
            trust_env=False,
        )
        logger.info("[wire_client] 共享 httpx 客户端已创建（trust_env=False，出口直连）")
    return _shared_client


async def aclose_shared_client() -> None:
    """关闭共享客户端（on_unload；幂等）。"""
    global _shared_client  # noqa: PLW0603
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
    logger.info("[wire_client] 共享 httpx 客户端已关闭")


def build_body(
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一请求词汇 → OpenAI wire body。

    extra 为模型参数合并面（temperature/max_tokens/thinking/reasoning_effort
    等 default_params 回填产物）：顶层平铺（
    GLM thinking 等字段本就是官方 body 字段）。
    """
    body: dict[str, Any] = {"model": model_name, "messages": messages}
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    for k, v in (extra or {}).items():
        if v is not None:
            body[k] = v
    return body


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


# ─────────── payload 诊断捕获（「LLM 请求」调试页数据源；请求体构造点
# ─────────── 随退役下沉到 wire 层——这里就是真实发出的 body 构造点） ───────────

_PAYLOAD_DIR_NAME = "payload_diag"
_PAYLOAD_KEEP = 200


def _payload_diag_dir() -> str:
    """诊断目录：AGENTOS_LOG_DIR 部署覆盖优先，否则用户空间 logs/payload_diag。"""
    from agentos_plugin_sdk.diagnostics_settings import payload_diag_dir as _sdk_dir  # noqa: PLC0415

    return str(_sdk_dir())


def capture_payload_diag(model: str, body: dict[str, Any]) -> None:
    """按开关落盘真实发出的请求体（默认关；200 份惰性轮转；失败不外抛）。"""
    import pathlib as _pl  # noqa: PLC0415

    from agentos_plugin_sdk.diagnostics_settings import read_diagnostics  # noqa: PLC0415

    try:
        if not read_diagnostics()["payload_diag"]:
            return
        diag_dir = _pl.Path(_payload_diag_dir())
        diag_dir.mkdir(parents=True, exist_ok=True)
        # 惰性轮转：超 keep 上限按 mtime 删最旧
        files = sorted(diag_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for old in files[: max(0, len(files) + 1 - _PAYLOAD_KEEP)]:
            old.unlink(missing_ok=True)
        import time as _time  # noqa: PLC0415

        path = diag_dir / f"{_time.strftime('%Y%m%d_%H%M%S')}_{abs(hash(model)) % 10**8}.json"
        path.write_text(json.dumps({"model": model, "body": body}, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 —— 诊断面失败不影响主路径
        logger.debug("[wire_client] payload 诊断落盘失败: %s", exc)


def _kind_from_status(status_code: int) -> str:
    return _STATUS_KIND.get(status_code, "service_down")


def _chunk_ns(choices: list[Any], usage: Any = None) -> Any:
    """构造归一 chunk（SimpleNamespace），下游消费链零改动。"""
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(choices=choices, usage=usage)


def _parse_sse_chunk(data: dict[str, Any]) -> Any:
    """SSE JSON → 归一 chunk（choices[].delta{content,reasoning_content,tool_calls}）。"""
    from types import SimpleNamespace  # noqa: PLC0415

    choices_out: list[Any] = []
    for ch in data.get("choices") or []:
        delta_raw = ch.get("delta") or {}
        delta = SimpleNamespace(
            content=delta_raw.get("content"),
            reasoning_content=delta_raw.get("reasoning_content"),
            tool_calls=[
                SimpleNamespace(
                    index=tc.get("index") or 0,
                    id=tc.get("id"),
                    function=SimpleNamespace(
                        name=(tc.get("function") or {}).get("name"),
                        arguments=(tc.get("function") or {}).get("arguments"),
                    ),
                )
                for tc in (delta_raw.get("tool_calls") or [])
            ],
        )
        choices_out.append(SimpleNamespace(delta=delta, finish_reason=ch.get("finish_reason")))
    usage_raw = data.get("usage")
    usage = (
        SimpleNamespace(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=(usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            ),
        )
        if isinstance(usage_raw, dict)
        else None
    )
    return _chunk_ns(choices_out, usage)


class _WireStream:
    """SSE 流的归一 chunk 异步迭代器（aclose 语义对齐旧 stream wrapper）。

    解析状态跨 ``__anext__`` 保留：底层行迭代器**只建一次**（惰性），每次取值
    从前一次的位置继续。旧实现每次 ``__anext__`` 都新建一条 ``aiter_lines()``
    ——首个 chunk 从旧生成器返回时其内部仍停在上一次读取处，第二个生成器在
    httpx 半消费流上取行会永久阻塞（2026-11-04 实测：tool_call 类 chunk 首
    chunk 后挂死，text 流偶发不复现；MiniMax 真机同样卡在首 chunk 后）。
    """

    def __init__(self, response: httpx.Response, model_label: str) -> None:
        self._response = response
        self._label = model_label
        self._closed = False
        self._lines: Any = None  # httpx aiter_lines 生成器（惰性、单例、跨调用保留）

    def __aiter__(self) -> _WireStream:
        return self

    def _line_iter(self) -> Any:
        if self._lines is None:
            self._lines = self._response.aiter_lines()
        return self._lines

    async def __anext__(self) -> Any:
        try:
            while True:
                if self._closed:
                    raise StopAsyncIteration
                line = await self._line_iter().__anext__()
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == _SSE_DONE:
                    raise StopAsyncIteration
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug("[wire_client] 非 JSON SSE 行忽略（%s）", payload[:80])
                    continue
                chunk = _parse_sse_chunk(data)
                # 空尾 usage 块（choices 空且无 usage）跳过不产出
                if chunk.choices or chunk.usage is not None:
                    return chunk
        except httpx.RemoteProtocolError as exc:
            # 上游中途断流：转半截语义（消费方按 finish 前异常处理）
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            aclose = getattr(self._response, "aclose", None)
            if aclose is not None:
                # httpx 流式响应（client.send(stream=True)）必须走 aclose 释放连接
                await aclose()
            else:
                self._response.close()


async def stream_chat(
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    *,
    timeout: float,
    model_label: str = "",
) -> _WireStream:
    """POST /chat/completions（stream）→ _WireStream。

    httpx 层 timeout 按阶段显式控制：读超时=timeout（卡死请求
    到点必抛，不依赖事件循环调度）。
    """
    capture_payload_diag(model_label or str(body.get("model", "")), body)
    client = _get_client()
    url = base_url.rstrip("/") + "/chat/completions"
    # 流式发送契约：必须 send(stream=True)——client.post() 是非流式 send，httpx
    # 会把响应体读到 EOF 才返回；SSE 到流结束才 EOF，全部 chunk 攒到生成结束
    # 一次性交付（2026-11-06 dev 实测：223 chunk 挤在 2ms 尾端窗口，前端
    # 「转圈后回复与工具卡一起弹出」的根因）。流式 send 下响应头到达即返回，
    # chunk 随上游逐批交付；读超时语义随之修正为「相邻读之间」的静默预算。
    request = client.build_request(
        "POST",
        url,
        json=body,
        headers=_auth_headers(api_key),
        timeout=httpx.Timeout(connect=min(timeout, 30.0), read=timeout, write=timeout, pool=timeout),
    )
    response = await client.send(request, stream=True)
    if response.status_code != 200:
        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
        await response.aclose()
        raise WireHTTPError(response.status_code, text)
    return _WireStream(response, model_label)


async def post_chat(
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    *,
    timeout: float,
) -> Any:
    """POST /chat/completions（非流式）→ 归一响应对象。"""
    client = _get_client()
    url = base_url.rstrip("/") + "/chat/completions"
    wire_body = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
    response = await client.post(
        url,
        json=wire_body,
        headers=_auth_headers(api_key),
        timeout=httpx.Timeout(connect=min(timeout, 30.0), read=timeout, write=timeout, pool=timeout),
    )
    if response.status_code != 200:
        raise WireHTTPError(response.status_code, response.text[:400])
    data = response.json()
    return _nonstream_ns(data)


def _nonstream_ns(data: dict[str, Any]) -> Any:
    """非流式 JSON → 归一响应（choices[].message + usage）。"""
    from types import SimpleNamespace  # noqa: PLC0415

    choices_out: list[Any] = []
    for ch in data.get("choices") or []:
        msg = ch.get("message") or {}
        choices_out.append(
            SimpleNamespace(
                message=SimpleNamespace(
                    content=msg.get("content"),
                    reasoning_content=msg.get("reasoning_content"),
                    tool_calls=[
                        SimpleNamespace(
                            id=tc.get("id"),
                            function=SimpleNamespace(
                                name=(tc.get("function") or {}).get("name"),
                                arguments=(tc.get("function") or {}).get("arguments"),
                            ),
                        )
                        for tc in (msg.get("tool_calls") or [])
                    ],
                ),
                finish_reason=ch.get("finish_reason"),
            )
        )
    usage_raw = data.get("usage") or {}
    usage = SimpleNamespace(
        prompt_tokens=usage_raw.get("prompt_tokens", 0),
        completion_tokens=usage_raw.get("completion_tokens", 0),
        total_tokens=usage_raw.get("total_tokens", 0),
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=(usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        ),
    )
    return SimpleNamespace(choices=choices_out, usage=usage, model=data.get("model", ""))
