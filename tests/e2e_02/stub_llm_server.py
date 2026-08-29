"""脚本化 LLM 上游替身（OpenAI 兼容 stub server）。

任务收束路径矩阵 e2e（test_task_lifecycle_paths_e2e.py）的 LLM 外部依赖替身：
本地 ThreadingHTTPServer 暴露 ``POST /v1/chat/completions``（SSE 流式 + 非流式）
与 ``POST /v1/embeddings``，按"场景标记 → 脚本步骤序列"返回可编程响应。

设计契约：
- 场景匹配：注册时绑定唯一 marker 字符串；请求体 JSON 文本中出现该 marker
  即命中该场景（marker 由测试注入任务描述/聊天消息，随管道 messages 原样
  回传，天然贯穿多轮）。marker 之间不得互为子串（注册时校验）。
- 步骤消费：每个场景按请求次数依次消费步骤；脚本耗尽后使用 default 步骤
  （缺省为纯文本收束轮），保证任何路径都有界响应、不悬挂。
- 流式协议：OpenAI chat.completion.chunk 形态（role/content delta、tool_calls
  delta、finish_reason、末尾 usage 块、[DONE]）——llm_service 经 litellm
  acompletion(stream=True, stream_options.include_usage=True) 消费。
- 观测：request_count(name) / requests(name) 供测试断言"有界轮数"与诊断。
  stub 是测试自有基建（外部依赖替身），不属被测系统观察面。

零第三方依赖（标准库 http.server），无真实网络外呼。
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_COMPLETIONS_PATHS = frozenset({"/v1/chat/completions", "/chat/completions"})
_EMBEDDINGS_PATHS = frozenset({"/v1/embeddings", "/embeddings"})

# usage 数值无需真实——只要非零即可通过 llm_usage 指纹防呆（假跑形态 = 全 0）。
_STUB_PROMPT_TOKENS = 120
_STUB_COMPLETION_TOKENS = 40


def text_step(content: str) -> dict[str, Any]:
    """脚本步骤：纯文本回复（finish_reason=stop）。"""
    return {"content": content}


def tool_call_step(name: str, **arguments: Any) -> dict[str, Any]:
    """脚本步骤：单工具调用回复（finish_reason=tool_calls）。"""
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def default_text_factory(prefix: str) -> Callable[[dict[str, Any], int], dict[str, Any]]:
    """缺省步骤工厂：第 n 次兜底请求返回带序号的可区分纯文本。"""

    def _default(body: dict[str, Any], index: int) -> dict[str, Any]:
        return text_step(f"{prefix} fallback round {index}")

    return _default


class _Script:
    """单场景脚本：marker 命中 + 步骤序列 + 兜底步骤。"""

    def __init__(
        self,
        name: str,
        marker: str,
        steps: list[dict[str, Any]],
        default: Callable[[dict[str, Any], int], dict[str, Any]] | dict[str, Any] | None,
    ) -> None:
        self.name = name
        self.marker = marker
        self.steps = steps
        if default is None:
            self.default: Callable[[dict[str, Any], int], dict[str, Any]] = (
                default_text_factory(f"[{name}]")
            )
        elif callable(default):
            self.default = default
        else:
            # 允许直接给固定步骤（dict）作兜底
            fixed = default

            def _fixed_step(_body: dict[str, Any], _index: int) -> dict[str, Any]:
                return fixed

            self.default = _fixed_step
        self.lock = threading.Lock()
        self.consumed = 0
        self.request_bodies: list[dict[str, Any]] = []

    def next_step(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.request_bodies.append(body)
            index = self.consumed
            self.consumed += 1
            if index < len(self.steps):
                return self.steps[index]
            return self.default(body, index + 1)


class ScriptedLLMUpstream:
    """可编程 OpenAI 兼容上游：register(marker, steps) 后由内核管道消费。"""

    def __init__(self) -> None:
        self._scripts: list[_Script] = []
        self._registry_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._handler_outer = self._make_handler_class()

    # ── 生命周期 ──

    def start(self) -> None:
        assert self._server is None, "stub upstream 已启动"
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_outer)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "stub upstream 未启动"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def api_base(self) -> str:
        """llm.yaml provider api_base 取值（OpenAI 兼容根路径）。"""
        return f"{self.base_url}/v1"

    # ── 脚本注册与观测 ──

    def register(
        self,
        name: str,
        marker: str,
        steps: list[dict[str, Any]],
        default: Callable[[dict[str, Any], int], dict[str, Any]] | dict[str, Any] | None = None,
    ) -> None:
        """注册场景脚本。default 可为步骤工厂 (body, index) -> step、固定步骤
        dict 或 None（纯文本兜底轮）。marker 不得与已注册 marker 互为子串。"""
        with self._registry_lock:
            for existing in self._scripts:
                if marker in existing.marker or existing.marker in marker:
                    raise ValueError(
                        f"场景 marker 重叠：{name}({marker!r}) 与 "
                        f"{existing.name}({existing.marker!r})"
                    )
            self._scripts.append(_Script(name, marker, steps, default))

    def reset(self) -> None:
        """清空全部脚本与请求记录（用例间隔离）。"""
        with self._registry_lock:
            self._scripts = []

    def request_count(self, name: str) -> int:
        script = self._find(name)
        with script.lock:
            return script.consumed

    def requests(self, name: str) -> list[dict[str, Any]]:
        script = self._find(name)
        with script.lock:
            return list(script.request_bodies)

    def _find(self, name: str) -> _Script:
        with self._registry_lock:
            for script in self._scripts:
                if script.name == name:
                    return script
        raise KeyError(f"场景未注册: {name}")

    # ── HTTP 层 ──

    def _match_script(self, raw_body: str) -> _Script | None:
        with self._registry_lock:
            scripts = list(self._scripts)
        for script in scripts:
            if script.marker in raw_body:
                return script
        return None

    def _make_handler_class(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # 静默访问日志——诊断经 requests(name) 面承载

            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/health", "/v1/health"):
                    self._send_json({"status": "ok"})
                    return
                self._send_json({"error": f"not found: {self.path}"}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}

                if self.path in _COMPLETIONS_PATHS:
                    self._handle_completions(body)
                elif self.path in _EMBEDDINGS_PATHS:
                    self._handle_embeddings(body)
                else:
                    self._send_json({"error": f"not found: {self.path}"}, status=404)

            def _handle_completions(self, body: dict[str, Any]) -> None:
                script = upstream._match_script(json.dumps(body, ensure_ascii=False))
                if script is None:
                    self._send_json(
                        {"error": "no scripted scenario matched this request"}, status=409
                    )
                    return
                step = script.next_step(body)
                model = str(body.get("model") or "stub-model")
                if body.get("stream"):
                    self._send_stream_completion(step, model)
                else:
                    self._send_json(self._completion_payload(step, model))

            def _handle_embeddings(self, body: dict[str, Any]) -> None:
                model = str(body.get("model") or "stub-embedding")
                inputs = body.get("input") or []
                items = inputs if isinstance(inputs, list) else [inputs]
                self._send_json(
                    {
                        "object": "list",
                        "model": model,
                        "data": [
                            {"object": "embedding", "index": i, "embedding": [0.1] * 8}
                            for i in range(len(items))
                        ],
                        "usage": {"prompt_tokens": 8, "total_tokens": 8},
                    }
                )

            # ── 响应构造 ──

            @staticmethod
            def _tool_call_id(index: int) -> str:
                return f"call_stub_{index}"

            def _completion_payload(self, step: dict[str, Any], model: str) -> dict[str, Any]:
                """非流式 chat.completion 载荷。"""
                tool_calls = step.get("tool_calls")
                if tool_calls:
                    message: dict[str, Any] = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": self._tool_call_id(i),
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(
                                        tc.get("arguments") or {}, ensure_ascii=False
                                    ),
                                },
                            }
                            for i, tc in enumerate(tool_calls)
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": str(step.get("content") or "")}
                    finish = "stop"
                return {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                    "usage": {
                        "prompt_tokens": _STUB_PROMPT_TOKENS,
                        "completion_tokens": _STUB_COMPLETION_TOKENS,
                        "total_tokens": _STUB_PROMPT_TOKENS + _STUB_COMPLETION_TOKENS,
                    },
                }

            def _sse_chunks(self, step: dict[str, Any], model: str) -> list[dict[str, Any]]:
                """流式 chunk 序列：首块（内容/工具调用）→ finish 块 → usage 块。"""
                chunks: list[dict[str, Any]] = []

                def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
                    return {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": finish}
                        ],
                    }

                tool_calls = step.get("tool_calls")
                if tool_calls:
                    chunks.append(_chunk({"role": "assistant", "content": ""}))
                    chunks.append(
                        _chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": self._tool_call_id(i),
                                        "type": "function",
                                        "function": {
                                            "name": tc["name"],
                                            "arguments": json.dumps(
                                                tc.get("arguments") or {},
                                                ensure_ascii=False,
                                            ),
                                        },
                                    }
                                    for i, tc in enumerate(tool_calls)
                                ]
                            }
                        )
                    )
                    chunks.append(_chunk({}, finish="tool_calls"))
                else:
                    content = str(step.get("content") or "")
                    chunks.append(_chunk({"role": "assistant", "content": content}))
                    chunks.append(_chunk({}, finish="stop"))
                chunks.append(
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": _STUB_PROMPT_TOKENS,
                            "completion_tokens": _STUB_COMPLETION_TOKENS,
                            "total_tokens": _STUB_PROMPT_TOKENS + _STUB_COMPLETION_TOKENS,
                        },
                    }
                )
                return chunks

            def _send_stream_completion(self, step: dict[str, Any], model: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in self._sse_chunks(step, model):
                    self._write_sse_event(chunk)
                self._write_sse_event(None)
                self.close_connection = True

            def _write_sse_event(self, payload: dict[str, Any] | None) -> None:
                data = "[DONE]" if payload is None else json.dumps(payload, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler
