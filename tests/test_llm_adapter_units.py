# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""adapter.py 单元面补测：纯函数 / 非流式路径 / 流桥 / 超时逃逸 / prompt 审计。

与 test_llm_adapter_call_streaming.py 同一 sys.path 模式（llm 目录平铺 import）。
外部依赖（litellm、文件系统、router_factory 查表）全桩，行为断言不触网。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LLM_SERVICE_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
# 去重插入到 [0]：车道共跑时其他插件目录可能残留 sys.path 前部，
# 平铺 `import adapter` 会命中他插件同名模块。
if str(_LLM_SERVICE_DIR) in sys.path:
    sys.path.remove(str(_LLM_SERVICE_DIR))
sys.path.insert(0, str(_LLM_SERVICE_DIR))

import adapter as _adapter  # noqa: E402


# ─────────────────────────── <think/> 提取 ───────────────────────────


@pytest.mark.parametrize(
    ("content", "expect_thinking", "expect_cleaned"),
    [
        (None, None, None),
        ("", None, ""),
        ("纯正文", None, "纯正文"),
        ("<think>想法</think>正文", "想法", "正文"),
        ("<think >想法</think >正文", "想法", "正文"),
        # MiniMax 形态：开标签无 >，但 <think 后带空白
        ("<think 想法</think>正文", "想法", "正文"),
        # 多段标签合并
        ("<think>a</think>mid<think>b</think>end", "a\nb", "midend"),
        # 纯思考无正文：cleaned 为 None
        ("<think>只想想</think>", "只想想", None),
    ],
)
def test_extract_thinking_from_content(
    content: str | None, expect_thinking: str | None, expect_cleaned: str | None
) -> None:
    thinking, cleaned = _adapter._extract_thinking_from_content(content)
    assert thinking == expect_thinking
    assert cleaned == expect_cleaned


# ─────────────────────────── extra_body 透传 ───────────────────────────


def test_move_to_extra_body_creates_and_merges() -> None:
    kwargs: dict[str, Any] = {"reasoning_effort": "high", "temperature": 0.7}
    _adapter._move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))
    assert kwargs == {"temperature": 0.7, "extra_body": {"reasoning_effort": "high"}}

    kwargs = {"thinking": {"type": "on"}, "extra_body": {"existing": 1}}
    _adapter._move_to_extra_body(kwargs, ("thinking",))
    assert kwargs["extra_body"] == {"existing": 1, "thinking": {"type": "on"}}
    assert "thinking" not in kwargs


def test_needs_extra_body_transport_prefix_and_table(monkeypatch) -> None:
    # 前缀形态直接命中，不查表
    assert _adapter._needs_extra_body_transport("zai/glm-5.1") is True
    assert _adapter._needs_extra_body_transport("openai/any-model") is True
    assert _adapter._needs_extra_body_transport("deepseek/x") is False

    # 查表形态：model_id（无前缀）经 router_factory 解析 provider → prefix 命中
    import router_factory

    monkeypatch.setattr(router_factory, "get_provider_for_model", lambda bare: "apigo")
    monkeypatch.setattr(router_factory, "get_litellm_prefix", lambda provider: "openai")
    assert _adapter._needs_extra_body_transport("minimax-m3.1") is True

    # 解析抛错 → False（分类失败不扩大故障面）
    def boom(bare: str) -> str:
        raise RuntimeError("no table")

    monkeypatch.setattr(router_factory, "get_provider_for_model", boom)
    assert _adapter._needs_extra_body_transport("unknown-model") is False


# ─────────────────────────── minimax 兜底 ───────────────────────────


def test_minimax_role_safety_converts_non_leading_system() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "injected", "name": "guard"},
    ]
    out = _adapter._BaseLiteLLMAdapter._ensure_minimax_role_safety("minimax/MiniMax-M3", msgs)
    assert out[0]["role"] == "system"  # 首位不动
    assert out[2]["role"] == "user"
    assert "name" not in out[2]

    # 非 minimax 原样
    other = [{"role": "user"}, {"role": "system"}]
    assert _adapter._BaseLiteLLMAdapter._ensure_minimax_role_safety("zai/glm", other) is other


# ─────────────────────────── 后台任务簿记与超时逃逸 ───────────────────────────


async def test_track_background_task_self_cleans_and_consumes_exc() -> None:
    async def ok_job() -> str:
        return "done"

    async def boom_job() -> None:
        raise RuntimeError("swallowed")

    t1 = asyncio.ensure_future(ok_job())
    t2 = asyncio.ensure_future(boom_job())
    _adapter._track_background_task(t1)
    _adapter._track_background_task(t2)

    await asyncio.sleep(0.05)

    assert t1 not in _adapter._background_tasks
    assert t2 not in _adapter._background_tasks  # 异常也被 done 回调消费移除
    assert await t1 == "done"


async def test_await_with_escape_returns_result_and_times_out() -> None:
    async def quick() -> str:
        return "ok"

    async def hanging() -> None:
        await asyncio.sleep(1000)

    assert await _adapter._await_with_escape(quick(), 5.0, what="quick") == "ok"

    task = asyncio.ensure_future(hanging())
    with pytest.raises(asyncio.TimeoutError, match="hanging 超时"):
        await _adapter._await_with_escape(hanging(), 0.05, what="hanging")
    task.cancel()
    await asyncio.sleep(0)


# ─────────────────────────── 跨线程流桥 ───────────────────────────


async def test_threaded_stream_bridge_iteration_exc_and_close() -> None:
    import queue
    import threading

    q: queue.Queue[Any] = queue.Queue()
    done_evt = threading.Event()
    close_evt = threading.Event()

    bridge = _adapter._ThreadedStreamBridge(
        queue=q, done_evt=done_evt, exc_box=[], close_evt=close_evt
    )
    assert bridge.__aiter__() is bridge

    q.put("chunk-1")
    assert await bridge.__anext__() == "chunk-1"

    # 异常箱透传
    bridge._exc_box.append(RuntimeError("worker exploded"))
    with pytest.raises(RuntimeError, match="worker exploded"):
        await bridge.__anext__()

    # done + 空队列 → 流结束
    bridge._exc_box.clear()
    done_evt.set()
    with pytest.raises(StopAsyncIteration):
        await bridge.__anext__()

    await bridge.aclose()
    assert close_evt.is_set()


# ─────────────────────────── 脱敏与 prompt 审计 ───────────────────────────


def test_redact_prompt_masks_key_forms() -> None:
    text = "Authorization: Bearer abcd1234efgh and sk-abcd123456 and {\"api_key\": \"xyz\"}"
    masked = _adapter._redact_prompt(text)
    assert "abcd1234efgh" not in masked
    assert "Bearer abc..." in masked or "Bearer" in masked
    assert "sk-abcd..." in masked
    assert '"api_key": "***"' in masked


def test_sync_prompt_handlers_installs_rotating_file(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "audit" / "prompt.log"
    monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(log_file))
    logger = logging.getLogger(_adapter.__name__ + "._prompt")
    logger.handlers.clear()
    logger.disabled = False

    _adapter._sync_prompt_handlers()
    _adapter._sync_prompt_handlers()  # 幂等：已挂跳过

    assert len(logger.handlers) == 1
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)


def test_resolve_prompt_log_path_walks_up_then_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_LOG_PROMPT_FILE", raising=False)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    resolved = _adapter._resolve_prompt_log_path()

    assert resolved == str(deep / "data" / "logs" / "prompt_audit.log")


def test_log_prompt_body_writes_redacted_payload(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "prompt_audit.log"
    monkeypatch.setattr(_adapter, "_PROMPT_AUDIT_ENABLED", True)
    logger = logging.getLogger(_adapter.__name__ + "._prompt")
    logger.handlers.clear()
    logger.disabled = False
    monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(log_file))

    _adapter._log_prompt_body(
        "zai/glm",
        [{"role": "user", "content": "hi sk-abcd123456"}],
        [{"name": "tool1"}],
        api_key="sk-abcd123456",
    )

    content = log_file.read_text(encoding="utf-8")
    assert "PROMPT" in content
    assert "sk-abcd123456" not in content

    logger.handlers.clear()


def test_log_prompt_body_disabled_is_zero_cost(monkeypatch) -> None:
    monkeypatch.setattr(_adapter, "_PROMPT_AUDIT_ENABLED", False)
    calls: list[str] = []
    monkeypatch.setattr(_adapter, "_sync_prompt_handlers", lambda: calls.append("sync"))
    _adapter._log_prompt_body("m", [], None)
    # 开关关闭：短路返回，不初始化 handler 不落盘
    assert calls == []


# ─────────────────────────── 非流式路径 ───────────────────────────


def _resp(
    *,
    content: str | None = "answer",
    reasoning: str | None = None,
    tool_calls: Any = None,
    finish: str | None = "stop",
    usage: Any = None,
) -> Any:
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        msg.reasoning_content = reasoning
    choice = SimpleNamespace(message=msg, finish_reason=finish)
    resp = SimpleNamespace(choices=[choice], usage=usage)
    return resp


class _StubAdapter(_adapter._BaseLiteLLMAdapter):
    """_do_completion 可编程桩。"""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.last_kwargs: dict[str, Any] | None = None

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._responder()


async def test_non_streaming_basic_text_and_usage() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    stub = _StubAdapter(lambda: _resp(usage=usage))

    out = await stub.completion("m", [{"role": "user", "content": "hi"}])

    assert out.text == "answer"
    assert out.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 3,
    }
    assert out.finish_reason == "stop"
    # timeout 恒 float；流式专属参数被剥除
    call_kwargs = stub.last_kwargs
    assert call_kwargs is not None
    assert isinstance(call_kwargs["timeout"], float)
    assert "first_chunk_timeout" not in call_kwargs
    assert "max_thinking_chars" not in call_kwargs
    assert call_kwargs["drop_params"] is True


async def test_non_streaming_tools_passthrough() -> None:
    tools = [{"type": "function", "function": {"name": "t"}}]
    stub = _StubAdapter(lambda: _resp())

    await stub.completion("m", [{"role": "user", "content": "hi"}], tools=tools)

    assert stub.last_kwargs is not None
    assert stub.last_kwargs["tools"] == tools


async def test_non_streaming_reasoning_content_becomes_text_when_empty() -> None:
    stub = _StubAdapter(lambda: _resp(content=None, reasoning="deep thought"))

    out = await stub.completion("m", [{"role": "user", "content": "hi"}])

    assert out.text == "deep thought"
    assert out.thinking_text == "deep thought"


async def test_non_streaming_think_tag_fallback_extraction() -> None:
    stub = _StubAdapter(lambda: _resp(content="<think>why</think>final"))

    out = await stub.completion("m", [{"role": "user", "content": "hi"}])

    assert out.thinking_text == "why"
    assert out.text == "final"


async def test_non_streaming_parses_tool_calls() -> None:
    tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="bash", arguments='{"cmd":"ls"}'),
    )
    stub = _StubAdapter(lambda: _resp(tool_calls=[tc], finish="tool_calls"))

    out = await stub.completion("m", [{"role": "user", "content": "hi"}])

    assert out.tool_calls == [
        {"id": "call_1", "name": "bash", "arguments": '{"cmd":"ls"}'}
    ]


async def test_health_check_true_and_false() -> None:
    ok = _StubAdapter(lambda: _resp(content="pong"))
    assert await ok.health_check("m") is True

    async def explode(**kwargs: Any) -> Any:
        raise RuntimeError("down")

    bad = _StubAdapter(explode)
    assert await bad.health_check("m") is False


# ─────────────────────────── 心跳探针 ───────────────────────────


async def test_stream_heartbeat_logs_and_exits_on_cancel(monkeypatch) -> None:
    # 心跳间隔硬编码 30s：本测试内把 sleep 缩短以驱动循环体
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float, *a: Any, **k: Any) -> None:
        await real_sleep(min(seconds, 0.02) / 1000)

    monkeypatch.setattr(_adapter.asyncio, "sleep", fast_sleep)

    task = asyncio.ensure_future(
        _adapter._BaseLiteLLMAdapter._stream_heartbeat(
            None,  # type: ignore[arg-type]
            "model-x",
            10.0,
            lambda: 20.0,  # idle 超过 half=5s → WARNING 级
            lambda: 3,
            SimpleNamespace(is_closed=False),
        )
    )
    await real_sleep(0.1)
    task.cancel()
    # _stream_heartbeat 吞掉 CancelledError 优雅退出（CancelledError 单独捕获 pass）
    await asyncio.wait_for(task, timeout=2.0)


# ─────────────────────────── LiteLLM 适配器转发 ───────────────────────────


async def test_litellm_adapter_forwards_to_acompletion(monkeypatch) -> None:
    import litellm

    sentinel = object()
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    out = await _adapter.LiteLLMAdapter()._do_completion(model="m", messages=[])

    assert out is sentinel
    assert captured["model"] == "m"


# ─────────────────────────── 工具调用归一化边界 ───────────────────────────


def test_parse_and_normalize_tool_calls_empty() -> None:
    base = _adapter._BaseLiteLLMAdapter()
    assert base._parse_tool_calls(None) == []
    assert base._normalize_tool_calls({}) == []


def test_normalize_tool_calls_orders_by_index_and_fills_id() -> None:
    base = _adapter._BaseLiteLLMAdapter()
    out = base._normalize_tool_calls(
        {2: {"id": "", "name": "b", "arguments": "{}"}, 0: {"id": "c0", "name": "a", "arguments": ""}}
    )
    assert [tc["id"] for tc in out] == ["c0", "call_2"]
    assert [tc["name"] for tc in out] == ["a", "b"]
