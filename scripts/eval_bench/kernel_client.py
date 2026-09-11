"""评测驱动内核客户端：HTTP REST + WebSocket 派发/收流。

依赖与 tests/e2e_02 同源（urllib 标准库 + websockets），不引入评测专用第三方。
接口清单：
- 登录 / 建会话 / 管道 state 列表 / 单管道 state 全字段 / traces / 审批响应
- dispatch_and_collect：WS 发 user_input 并收流到终态（审批经回调交给 ApprovalBot）
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
import urllib.request

import websockets


class KernelClientError(RuntimeError):
    """内核 HTTP 调用失败（非 2xx / 响应缺关键字段）。"""


# 瞬时故障重试：系统重载下内核 HTTP 会短暂不可达（连接拒绝/5xx），
# 这不是评测判定的对象，重试吸收；4xx（鉴权/参数错）不重试。
_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF = 5.0


def _http_json(method: str, url: str, body: dict | None = None, token: str | None = None,
               timeout: float = 30) -> Any:
    """发起 HTTP 请求并解析 JSON；非 2xx 抛 KernelClientError（带响应体摘要）。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last_error: Exception | None = None
    for attempt in range(_TRANSIENT_RETRIES + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            err = KernelClientError(f"{method} {url} -> {exc.code}: {detail}")
            if exc.code >= 500 and attempt < _TRANSIENT_RETRIES:
                last_error = err
                time.sleep(_TRANSIENT_BACKOFF * (attempt + 1))
                continue
            raise err from exc
        except urllib.error.URLError as exc:
            err = KernelClientError(f"无法连接 {url}: {exc}")
            if attempt < _TRANSIENT_RETRIES:
                last_error = err
                time.sleep(_TRANSIENT_BACKOFF * (attempt + 1))
                continue
            raise err from exc
    raise RuntimeError("unreachable: 重试循环必然 return 或 raise")  # pragma: no cover


@dataclass
class DispatchResult:
    """一次 user_input 派发的收流结果。"""

    terminal: str = "deadline"  # stream_end / stream_error / error / deadline
    events: list[dict[str, Any]] = field(default_factory=list)  # {type, pipeline_id}
    interactions: list[dict[str, Any]] = field(default_factory=list)  # 审批事件与 bot 动作
    pipeline_id: str | None = None  # 事件中观察到的主管道


class KernelClient:
    """内核 REST 客户端（评测面只用只读写面 + 审批响应）。"""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._credentials: tuple[str, str] | None = None  # (username, password)

    def _authed_json(self, method: str, url: str, body: dict | None = None,
                     timeout: float = 30) -> Any:
        """带令牌过期的自动续期：401 → re-login → 重放一次（长评测 > token TTL）。"""
        try:
            return _http_json(method, url, body, token=self.token, timeout=timeout)
        except KernelClientError as exc:
            if "401" not in str(exc) or self._credentials is None:
                raise
            self.login(*self._credentials)
            return _http_json(method, url, body, token=self.token, timeout=timeout)

    # ── 鉴权与会话 ──────────────────────────────────────────────

    def login(self, username: str, password: str) -> str:
        body = _http_json("POST", f"{self.base_url}/api/v1/auth/login",
                          {"username": username, "password": password})
        token = body.get("access_token")
        if not token:
            raise KernelClientError(f"登录响应缺少 access_token: {body}")
        self.token = token
        self._credentials = (username, password)
        return token

    def create_session(self, title: str) -> dict[str, Any]:
        body = self._authed_json("POST", f"{self.base_url}/api/v1/sessions",
                                 {"title": title})
        if not body.get("thread_id"):
            raise KernelClientError(f"创建会话响应缺少 thread_id: {body}")
        return body

    # ── 数据面（评测指标采集） ──────────────────────────────────

    def list_pipeline_states(self) -> list[dict[str, Any]]:
        body = self._authed_json("GET", f"{self.base_url}/api/v1/pipelines/state")
        items = body.get("items") if isinstance(body, dict) else body
        return [r for r in (items or []) if isinstance(r, dict)]

    def get_pipeline_state_full(self, pipeline_id: str) -> dict[str, Any]:
        from urllib.parse import urlencode

        qs = urlencode({"pipeline_id": pipeline_id})
        return self._authed_json("GET",
                                 f"{self.base_url}/ext/monitoring/pipeline-state?{qs}")

    def get_traces(self, pipeline_id: str, limit: int = 500) -> dict[str, Any]:
        from urllib.parse import urlencode

        qs = urlencode({"pipeline_id": pipeline_id, "limit": limit})
        return self._authed_json("GET", f"{self.base_url}/ext/monitoring/traces?{qs}")

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        url = f"{self.base_url}/ext/monitoring/tasks?page_size=200"
        if status:
            url += f"&status={quote(status)}"
        body = self._authed_json("GET", url)
        return [r for r in body.get("items", []) if isinstance(r, dict)]

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """会话消息快照（GET /api/v1/sessions/{id}/messages，seq 升序）。

        真实信封形态：``{has_more, messages: [...]}``（兼容 items 兜底）。
        """
        body = self._authed_json("GET",
                                 f"{self.base_url}/api/v1/sessions/{session_id}/messages")
        items = None
        if isinstance(body, dict):
            items = body.get("messages") or body.get("items")
        return [m for m in (items or []) if isinstance(m, dict)]

    def last_assistant_reply(self, session_id: str) -> str:
        """最后一条 assistant 消息的全文（回复断言用）；无消息返回空串。"""
        for msg in reversed(self.get_messages(session_id)):
            if msg.get("role") == "assistant":
                content = msg.get("content") or msg.get("content_preview") or ""
                return str(content)
        return ""

    # ── 模型配置（评测跑前钉死默认 chat 模型，防结果失真） ──────

    def get_llm_defaults(self) -> dict[str, Any]:
        """当前默认模型配置（llm_service 插件读面：{chat, embedding, tiers}）。"""
        return self._authed_json("GET", f"{self.base_url}/ext/llm_service/config/llm/defaults")

    def set_llm_default_chat(self, model: str) -> dict[str, Any]:
        """部分更新默认 chat 模型（与模型设置页同一条道）。"""
        return self._authed_json("PUT", f"{self.base_url}/ext/llm_service/config/llm/defaults",
                                 {"chat": model})

    # ── 审批响应（与前端同一条道：内核 interaction 门面） ────────

    def respond_interaction(self, request_id: str, selected_option: str) -> dict[str, Any]:
        return self._authed_json(
            "POST", f"{self.base_url}/api/v1/interaction/response",
            {"request_id": request_id, "response_type": "answered",
             "selected_option": selected_option},
        )

    # ── WebSocket ──────────────────────────────────────────────

    def ws_chat_url(self) -> str:
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{ws_base}/ws/chat?token={quote(self.token or '')}&version=1"


async def send_stop_generation(ws_url: str, thread_id: str, pipeline_id: str) -> None:
    """发一条 stop_generation 帧后短连即断（评测自清洁：不留活口管道）。

    与前端停止按钮同一入站路由（InboundRouter.route_stop → dispatch_stop）。
    fire-and-forget：短等待让内核消费帧，不等终态回执（终态由轮询面确认）。
    """

    async def _run() -> None:
        async with websockets.connect(ws_url, open_timeout=15) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — 确认帧缺失不阻断
                pass
            await ws.send(json.dumps({
                "type": "stop_generation",
                "thread_id": thread_id,
                "pipeline_id": pipeline_id,
            }))
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — 回执不保证，忽略
                pass

    await _run()


def _extract_pipeline_id(event: dict[str, Any]) -> str | None:
    """从事件体提取 pipeline_id（顶层或 data 内，多键名防御：真实事件形态不统一）。"""
    data = event.get("data")
    sources = (event, data if isinstance(data, dict) else {})
    for source in sources:
        for key in ("pipeline_id", "pipelineId", "_pipelineId"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


async def dispatch_and_collect(
    ws_url: str,
    thread_id: str,
    content: str,
    client_message_id: str,
    on_interaction: Any = None,  # Callable[[dict], str | None] 返回 bot 动作描述
    timeout_s: float = 600,
) -> DispatchResult:
    """发送一条 user_input 并收流到终态。

    - interaction_request 事件 → 交给 on_interaction 回调（审批 bot）后继续收流
      （审批响应后管道恢复，后续流仍在同一连接上）；
    - stream_end / stream_error / error → 终态返回；
    - 超时（timeout_s）→ terminal="deadline" 返回（已收事件仍可用）。
    """
    result = DispatchResult()

    async def _run() -> None:
        async with websockets.connect(ws_url, open_timeout=15, max_size=64 * 1024 * 1024) as ws:
            # 消费连接确认帧（与前端/e2e 同构）
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — 确认帧缺失不阻断
                pass
            await ws.send(json.dumps({
                "type": "user_input",
                "thread_id": thread_id,
                "content": content,
                "pipeline_id": "",
                "attachments": [],
                "enable_thinking": False,
                "thinking_strength": "",
                "client_message_id": client_message_id,
            }))
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    return
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                etype = str(event.get("type") or "")
                pid = _extract_pipeline_id(event)
                result.events.append({"type": etype, "pipeline_id": pid})
                if pid and not result.pipeline_id:
                    result.pipeline_id = pid
                if etype == "interaction_request":
                    data = event.get("data")
                    payload = data if isinstance(data, dict) else event
                    action = None
                    if on_interaction is not None:
                        action = on_interaction(payload)
                    result.interactions.append({
                        "request_id": payload.get("request_id") or payload.get("id") or "",
                        "action": action,
                    })
                elif etype in ("stream_end", "stream_error", "error"):
                    result.terminal = etype
                    return

    await _run()
    return result
