# @feature: FP-T07 llm api | @ci: python-coverage
"""adapter.py 未覆盖分支补测（桥接异常面 / minimax 兜底 / health_check / 非流式解析 /
心跳探针 / KeyPool 错误分类重试 / 池耗尽翻译 / 直连超时与 worker 装箱）。

行为契约（断输入→输出/副作用，不钉实现）：
- _ThreadedStreamBridge：__aiter__ 返回自身；worker 异常经 exc_box 原样透传
- prompt 审计 handler 幂等：已挂 handler 不重复初始化
- data/logs 探测：逐级向上走查（未命中 → cwd 兜底）
- <think/> 提取：标准/MiniMax 无 > 两种标签、多段合并、纯空白不产 thinking、
  空内容零处理
- LLMAdapter Protocol：方法体为 no-op 桩（返回 None），实现类满足协议
- minimax 角色兜底：非首位 system→user 且摘 name；首位 system / 非 minimax 不动
- health_check：choices 非空 True / 空 False / 异常 False 不外抛
- 非流式：tools 注入、reasoning_content 优先（正文为空时回退为正文）、
  <think/> 兜底提取、usage 核算（cached 缺省 0）
- 流式心跳探针：静默时长过半升级 WARNING、stream_closed 信号透传（None/布尔）
- KeyPool：流式成功把 release 绑定 aclose（消费方关闭才归还许可）、
  BAD_REQUEST 不换 key 直抛、SERVICE_DOWN 指数退避（记录 pacing 值，cap 16s）、
  RATE_LIMIT 等“其他可恢复”交 KeySlot 策略后换 key 重试、池耗尽翻译为
  RateLimitError（__cause__ 保留）、fallback 失败回抛 last_exc
- 直连：数值 timeout 以调用方为准、非数值 timeout（httpx.Timeout 形态）保持
  first_chunk_timeout、worker 异常装箱透传（循环内/循环后两个出口）、
  建连超时 deadline 到点抛 TimeoutError 并置 close_evt

外部依赖全桩（litellm / KeyPool / router_factory 查表 / 时钟经 fake sleep 注入），
绝不触网。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
# 本插件目录置 sys.path[0]：车道共跑时其他插件同名 adapter.py 目录可能占住前位
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

# 裸名逐出：exceptions 等平铺名遍布各插件（cost_control 有同名 exceptions.py），
# 收集期被别的插件占位会让 key_pool 的 `from exceptions import ...` 打到错误
# 实现（共跑收集 ImportError）。逐出后按本目录重解析。
for _bare in ("adapter", "exceptions", "key_pool", "router_factory", "stream_client"):
    sys.modules.pop(_bare, None)

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致
import exceptions as _llm_exceptions  # noqa: E402
import key_pool as _kp_mod  # noqa: E402
import litellm as _litellm_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_flat_modules():
    """重绑平铺裸名槽位到本文件实例（同 test_adapter_branches.py 惯例）。"""
    saved: dict[str, Any] = {}
    for name, mod in (
        ("exceptions", _llm_exceptions),
        ("router_factory", _rf_mod),
        ("key_pool", _kp_mod),
    ):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class _ListHandler(logging.Handler):
    """直挂模块 logger 的记录器（propagate=False，caplog 不可达）。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.messages.append(record.getMessage())


@pytest.fixture
def stream_log_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ListHandler]:
    """_stream_logger 挂直采 handler 并放开 DEBUG（测试后还原）。"""
    h = _ListHandler()
    logger = adapter_mod._stream_logger
    old_level = logger.level
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    yield h
    logger.removeHandler(h)
    logger.setLevel(old_level)


@pytest.fixture
def fake_asyncio_sleep(monkeypatch: pytest.MonkeyPatch):
    """注入快进时钟：记录请求的 sleep 时长后立即让出（不真实等待）。

    用于背避 pacing 值断言与心跳探针驱动——断言的是计算出的间隔契约，
    不是墙钟时序。返回记录列表。
    """
    real_sleep = asyncio.sleep
    recorded: list[float] = []

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        recorded.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", _sleep)
    return recorded


# ─────────────────────────── 通用桩 ───────────────────────────


class FakeSlot:
    """KeySlot 桩：记录 release/on_success/handle_error（同 test_adapter_branches）。"""

    def __init__(self, key_id: str, api_key: str, api_base: str | None = None,
                 consecutive_down: int = 0) -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = api_base
        self._consecutive_down = consecutive_down
        self.released = 0
        self.succeeded = 0
        self.errors: list[Any] = []

    def release(self) -> None:
        self.released += 1

    def on_success(self) -> None:
        self.succeeded += 1

    def handle_error(self, info: Any) -> None:
        self.errors.append(info)


class FakePool:
    """acquire_slot 按预排槽位序列逐个给出；可注入耗尽异常。"""

    def __init__(self, slots: list[FakeSlot], acquire_exc: BaseException | None = None) -> None:
        self.slots: list[Any] = slots
        self._seq = list(slots)
        self._acquire_exc = acquire_exc

    async def acquire_slot(self) -> Any:
        if self._acquire_exc is not None:
            raise self._acquire_exc
        if self._seq:
            return self._seq.pop(0)
        return self.slots[0]


@pytest.fixture
def fake_router_factory(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """router_factory 查表桩：provider=apigo / prefix=openai / 默认无池。"""
    state: dict[str, Any] = {
        "pool": None,
        "provider": "apigo",
        "prefix": "openai",
        "model_name": {"minimax-m3.1": "MiniMax-M3"},
    }

    monkeypatch.setattr(_rf_mod, "get_key_pool", lambda provider: state["pool"])
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", lambda model_id: state["provider"])
    monkeypatch.setattr(_rf_mod, "get_litellm_prefix", lambda provider: state["prefix"])
    monkeypatch.setattr(_rf_mod, "get_model_name_for_id",
                        lambda model_id: state["model_name"].get(model_id, model_id))
    return state


class _ScriptedDirectStub(adapter_mod.KeyPoolAdapter):
    """覆盖 _direct_call_with_slot：按脚本吐结果/异常（CancelledError 不是 Exception）。"""

    def __init__(self, script: list[Any]) -> None:
        super().__init__(router=None)
        self._script = list(script)

    async def _direct_call_with_slot(self, slot: Any = None, **kwargs: Any) -> Any:
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _non_stream_response(
    *,
    content: str | None = "answer",
    reasoning: str | None = None,
    usage: Any = None,
    finish_reason: str | None = "stop",
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


class _NonStreamStub(adapter_mod._BaseLiteLLMAdapter):
    """_do_completion 返回预设非流式响应并记录 kwargs 的桩适配器。"""

    def __init__(self, response: Any, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.captured: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured = dict(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


_USAGE = SimpleNamespace(
    prompt_tokens=11,
    completion_tokens=7,
    total_tokens=18,
    prompt_tokens_details=SimpleNamespace(cached_tokens=None),
)


# ─────────────────────────── _ThreadedStreamBridge ───────────────────────────


def test_stream_bridge_aiter_returns_self() -> None:
    """__aiter__ 契约：返回自身（async for 直接消费同一桥接对象）。"""
    import queue as _queue

    bridge = adapter_mod._ThreadedStreamBridge(
        queue=_queue.Queue(), done_evt=threading.Event(),
        exc_box=[], close_evt=threading.Event(),
    )

    assert bridge.__aiter__() is bridge


async def test_stream_bridge_reraises_worker_exception() -> None:
    """worker 异常装箱 → __anext__ 原样透传（消费端拿到上游真实异常）。"""
    import queue as _queue

    boom = ValueError("worker blew up")
    bridge = adapter_mod._ThreadedStreamBridge(
        queue=_queue.Queue(), done_evt=threading.Event(),
        exc_box=[boom], close_evt=threading.Event(),
    )

    with pytest.raises(ValueError) as exc_info:
        await bridge.__anext__()

    assert exc_info.value is boom  # 装箱异常本体，不再包装


# ─────────────────────────── prompt 审计 / 路径探测 ───────────────────────────


def test_sync_prompt_handlers_idempotent_when_already_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已挂 handler → 直接返回（不重建文件 handler、不改 disabled 态）。"""
    pl = adapter_mod._prompt_logger
    sentinel = object()
    monkeypatch.setattr(pl, "handlers", [sentinel])

    adapter_mod._sync_prompt_handlers()

    assert pl.handlers == [sentinel]  # 幂等：未追加任何 handler


def test_resolve_prompt_log_path_walks_up_before_falling_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cwd 向上逐级探测无 data/logs → 走查后回落 cwd 拼默认路径（不误挂上级目录）。"""
    monkeypatch.chdir(tmp_path)  # tmp 向上均无 data/logs

    p = adapter_mod._resolve_prompt_log_path()

    assert p == os.path.join(str(tmp_path), "data", "logs", "prompt_audit.log")
    assert not (tmp_path / "data").exists()  # 探测只读，不建目录


# ─────────────────────────── <think/> 提取 ───────────────────────────


@pytest.mark.parametrize("content", [None, ""])
def test_extract_thinking_empty_content_passthrough(content: str | None) -> None:
    """空内容零处理：thinking None，content 原样（None/空串语义保留）。"""
    assert adapter_mod._extract_thinking_from_content(content) == (None, content)


@pytest.mark.parametrize(
    ("content", "expected_thinking", "expected_clean"),
    [
        # 标准 XML 标签
        ("<think>abc</think>rest", "abc", "rest"),
        # MiniMax 无 > 开标签
        ("<think\nabc</think>rest", "abc", "rest"),
        # 多段合并（换行 join）+ 标签外正文保留
        ("<think>a</think>x<think> b </think>y", "a\nb", "xy"),
    ],
)
def test_extract_thinking_separates_content(
    content: str, expected_thinking: str, expected_clean: str
) -> None:
    assert adapter_mod._extract_thinking_from_content(content) == (
        expected_thinking, expected_clean,
    )


def test_extract_thinking_whitespace_only_yields_none_thinking() -> None:
    """标签内纯空白 → thinking 为 None（不产空思考事件），正文照常。"""
    thinking, clean = adapter_mod._extract_thinking_from_content("<think>   </think>body")

    assert thinking is None
    assert clean == "body"


# ─────────────────────────── LLMAdapter Protocol ───────────────────────────


async def test_protocol_method_bodies_are_noop_stubs() -> None:
    """接口方法体为 no-op 桩（返回 None）；实现类满足 runtime_checkable 协议。"""

    class _Impl(adapter_mod._BaseLiteLLMAdapter):
        async def _do_completion(self, **kwargs: Any) -> Any:  # noqa: D102
            return None

    assert isinstance(_Impl(), adapter_mod.LLMAdapter)  # 结构化协议判定
    assert await adapter_mod.LLMAdapter.completion(None, "m", []) is None  # type: ignore[arg-type]
    assert await adapter_mod.LLMAdapter.health_check(None, "m") is None  # type: ignore[arg-type]


# ─────────────────────────── minimax 角色兜底 ───────────────────────────


def test_minimax_role_safety_converts_non_leading_system() -> None:
    """minimax 模型非首位 system → user 且摘 name（守护注入消息的最后防线）。"""
    messages = [
        {"role": "system", "content": "real system", "name": "sys"},
        {"role": "system", "content": "guard injected", "name": "guard"},
        {"role": "user", "content": "hi"},
    ]

    out = adapter_mod._BaseLiteLLMAdapter._ensure_minimax_role_safety(
        "minimax/MiniMax-M3", messages
    )

    assert out is messages  # 原地修正（同一引用）
    assert out[0]["role"] == "system"  # 首位 system 合法保留
    assert out[1]["role"] == "user"
    assert "name" not in out[1]
    assert out[2]["role"] == "user"


@pytest.mark.parametrize(
    ("model", "messages"),
    [
        # 仅首位 system：合法形态，不动
        ("minimax/MiniMax-M3", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]),
        # 非 minimax 模型：非首位 system 不归本兜底管
        ("zai/glm-5.2", [
            {"role": "user", "content": "u"},
            {"role": "system", "content": "guard", "name": "g"},
        ]),
    ],
)
def test_minimax_role_safety_noop_cases(model: str, messages: list[dict[str, Any]]) -> None:
    """无需修正 → 原样返回（逐字不动）。"""
    snapshot = [dict(m) for m in messages]

    out = adapter_mod._BaseLiteLLMAdapter._ensure_minimax_role_safety(model, messages)

    assert out == snapshot


# ─────────────────────────── health_check ───────────────────────────


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (SimpleNamespace(choices=[SimpleNamespace()]), True),
        (SimpleNamespace(choices=[]), False),
    ],
)
async def test_health_check_reflects_choices(response: Any, expected: bool) -> None:
    """choices 非空 → 健康；空 → 不健康（口径：有无候选即存活判定）。"""
    ad = _NonStreamStub(response)

    assert await ad.health_check("zai/glm-test") is expected


async def test_health_check_swallows_exception_and_reports_unhealthy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """上游异常 → 不外抛，返回 False 并留告警（健康检查不产生次生故障）。"""
    ad = _NonStreamStub(None, exc=RuntimeError("conn refused"))

    with caplog.at_level(logging.WARNING, logger="adapter"):
        assert await ad.health_check("zai/glm-test") is False


# ─────────────────────────── 非流式解析 ───────────────────────────


async def test_non_streaming_passes_tools_and_float_timeout() -> None:
    """tools 注入上游参数；inter_chunk_timeout 复用为 float 整体超时。"""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    ad = _NonStreamStub(_non_stream_response())

    await ad._call_non_streaming("zai/glm-test", [{"role": "user", "content": "hi"}], tools=tools)

    assert ad.captured["tools"] == tools
    assert isinstance(ad.captured["timeout"], float)
    assert "first_chunk_timeout" not in ad.captured  # 流式专属参数已剥出
    assert "max_thinking_chars" not in ad.captured


@pytest.mark.parametrize(
    ("content", "reasoning", "expected_text"),
    [
        # reasoning 与正文并存：thinking 单独成通道，正文不动
        ("answer", "why", "answer"),
        # 正文为空：reasoning 回退为正文（空响应不可用）
        (None, "why", "why"),
    ],
)
async def test_non_streaming_reasoning_content_channel(
    content: str | None, reasoning: str, expected_text: str
) -> None:
    resp = _non_stream_response(content=content, reasoning=reasoning)
    ad = _NonStreamStub(resp)

    out = await ad._call_non_streaming("m", [{"role": "user", "content": "hi"}])

    assert out.thinking_text == reasoning
    assert out.text == expected_text
    assert out.finish_reason == "stop"


@pytest.mark.parametrize(
    "content",
    ["<think>why</think>answer", "<think\nwhy</think>answer"],
)
async def test_non_streaming_falls_back_to_think_tag_extraction(content: str) -> None:
    """reasoning_content 为空 → content 中 <think/> 两种标签形态兜底提取。"""
    ad = _NonStreamStub(_non_stream_response(content=content))

    out = await ad._call_non_streaming("m", [{"role": "user", "content": "hi"}])

    assert out.thinking_text == "why"
    assert out.text == "answer"


async def test_non_streaming_usage_accounting_defaults_cached_to_zero() -> None:
    """usage 核算：三 token 计数透传，cached 缺省收敛 0（不产 None 键）。"""
    ad = _NonStreamStub(_non_stream_response(usage=_USAGE))

    out = await ad._call_non_streaming("m", [{"role": "user", "content": "hi"}])

    assert out.usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 0,
    }


# ─────────────────────────── 流式心跳探针 ───────────────────────────


async def test_stream_heartbeat_reports_idle_and_closed_signal(
    fake_asyncio_sleep: list[float],
    stream_log_capture: _ListHandler,
) -> None:
    """静默超阈值 → WARNING 级心跳，携带 idle 时长 / chunk 序号 / stream_closed。"""
    task = asyncio.ensure_future(
        adapter_mod._BaseLiteLLMAdapter._stream_heartbeat(
            None,  # self 未用（方法内无 self 引用）
            "m",
            10.0,  # inter_chunk_timeout，half=5
            lambda: 6.0,  # idle 已过半 → 升级 WARNING
            lambda: 3,   # 已收 3 个 chunk
            SimpleNamespace(is_closed=False),
        )
    )
    for _ in range(200):
        if stream_log_capture.records:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert any("HEARTBEAT" in m and "idle=6" in m and "stream_closed=False" in m
               for m in stream_log_capture.messages)
    assert any(r.levelno == logging.WARNING for r in stream_log_capture.records)


async def test_stream_heartbeat_idle_below_half_is_debug_and_none_closed(
    fake_asyncio_sleep: list[float],
    stream_log_capture: _ListHandler,
) -> None:
    task = asyncio.ensure_future(
        adapter_mod._BaseLiteLLMAdapter._stream_heartbeat(
            None,
            "m",
            10.0,
            lambda: 1.0,  # 静默未过半 → DEBUG
            lambda: 0,
            None,  # 底层流缺失 → stream_closed=None
        )
    )
    for _ in range(200):
        if stream_log_capture.records:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert any("HEARTBEAT" in m and "idle=1" in m and "stream_closed=None" in m
               for m in stream_log_capture.messages)
    assert all(r.levelno == logging.DEBUG for r in stream_log_capture.records)


# ─────────────────────────── 非流式 tool_calls 解析 ───────────────────────────


def test_parse_tool_calls_maps_id_name_arguments_with_fallback() -> None:
    """非流式 tool_calls 解析：id/name/arguments 三键映射；id 缺失按序回退 call_N。"""
    raw = [
        SimpleNamespace(id="call_0", function=SimpleNamespace(name="f0", arguments="{}")),
        SimpleNamespace(id=None, function=SimpleNamespace(name="f1", arguments='{"a"')),
    ]

    parsed = adapter_mod._BaseLiteLLMAdapter._parse_tool_calls(None, raw)

    assert parsed == [
        {"id": "call_0", "name": "f0", "arguments": "{}"},
        {"id": "call_1", "name": "f1", "arguments": '{"a"'},  # id 回退按已解析序号
    ]


# ─────────────────────────── LiteLLMAdapter 直连 ───────────────────────────


async def test_litellm_adapter_delegates_to_litellm_acompletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLMAdapter._do_completion 委托 litellm.acompletion，返回值原样透传。"""
    sentinel = SimpleNamespace(resp=1)
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(_litellm_mod, "acompletion", fake_acompletion)

    out = await adapter_mod.LiteLLMAdapter()._do_completion(model="m", messages=[])

    assert out is sentinel
    assert captured["model"] == "m"


# ─────────────────────────── KeyPool：provider 解析 / 无池回退 ───────────────────────────


@pytest.mark.parametrize(
    ("provider", "pool", "expected"),
    [
        ("apigo", None, ""),           # provider 无 KeyPool → 空串（回 Router 行为）
        (None, None, ""),              # 模型未注册任何 provider → 空串
        ("apigo", object(), "apigo"),  # 有池 → provider 名（走多 key 面）
    ],
)
def test_resolve_provider_maps_via_registry(
    fake_router_factory: dict[str, Any], provider: Any, pool: Any, expected: str
) -> None:
    fake_router_factory["provider"] = provider
    fake_router_factory["pool"] = pool

    assert adapter_mod.KeyPoolAdapter(router=None)._resolve_provider("minimax-m3.1") == expected


@pytest.mark.parametrize(
    ("kwargs_model", "expected"),
    [
        ({"model": "zai/glm-5.1"}, "glm-5.1"),  # litellm 前缀剥离
        ({"model": "glm-5.1"}, "glm-5.1"),      # 无前缀原样
    ],
)
def test_extract_model_name_strips_prefix(kwargs_model: dict[str, Any], expected: str) -> None:
    assert adapter_mod.KeyPoolAdapter(router=None)._extract_model_name(kwargs_model) == expected


async def test_do_completion_without_pool_routes_directly(
    fake_router_factory: dict[str, Any],
) -> None:
    """无 KeyPool → 整体回落 Router 通道，kwargs 原样透传。"""
    fake_router_factory["pool"] = None
    sentinel = SimpleNamespace(routed=1)
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    route_kwargs: dict[str, Any] = {}

    async def fake_route(**kwargs: Any) -> Any:
        route_kwargs.update(kwargs)
        return sentinel

    adapter._route_call = fake_route  # type: ignore[method-assign]

    out = await adapter._do_completion(model="minimax-m3.1", messages=[], temperature=0.5)

    assert out is sentinel
    assert route_kwargs["model"] == "minimax-m3.1"
    assert route_kwargs["temperature"] == 0.5


# ─────────────────────────── KeyPool：流式 release 绑定 ───────────────────────────


class _StreamResult:
    """带 __aiter__ 的流式结果桩：aclose 可被绑定替换后观测。"""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _StreamResult:
        return self

    async def aclose(self) -> None:
        self.closed = True


class _StreamDirectStub(adapter_mod.KeyPoolAdapter):
    """_direct_call_with_slot 返回预置结果（流式/非流式）并记录 kwargs。"""

    def __init__(self, result: Any) -> None:
        super().__init__(router=None)
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def _direct_call_with_slot(self, slot: Any = None, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self._result


async def test_streaming_success_defers_slot_release_to_aclose(
    fake_router_factory: dict[str, Any],
) -> None:
    """流式成功：release 延迟到 stream.aclose（传输期许可占用），关闭恰好归还一次。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])
    stream = _StreamResult()
    adapter = _StreamDirectStub(stream)

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is stream
    assert slot.succeeded == 1
    assert slot.released == 0  # 传输未开始，许可未归还
    await out.aclose()  # 消费方关闭流 → 绑定的 release 触发
    assert slot.released == 1
    assert stream.closed is True  # 原始 aclose 仍执行
    await out.aclose()  # 重复关闭幂等：release 只执行一次
    assert slot.released == 1


async def test_non_streaming_success_releases_immediately(
    fake_router_factory: dict[str, Any],
) -> None:
    """非流式成功：release 即时归还（对照流式延迟语义）。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])
    result = SimpleNamespace(non_stream=1)
    adapter = _StreamDirectStub(result)

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is result
    assert slot.released == 1


# ─────────────────────────── KeyPool：错误分类决策 ───────────────────────────


class BadRequestError(Exception):
    """按类型名映射用例：与 litellm.BadRequestError 同名（classify_error 以类名判定）。"""


class ServiceUnavailableError(Exception):
    """与 litellm.ServiceUnavailableError 同名 → SERVICE_DOWN。"""


class RateLimitError(Exception):
    """与 litellm.RateLimitError 同名 → RATE_LIMIT（"其他可恢复"策略面）。"""


async def test_bad_request_aborts_without_key_rotation(
    fake_router_factory: dict[str, Any],
) -> None:
    """BAD_REQUEST 不可恢复 → 直接抛出，不换 key、不 fallback。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    boom = BadRequestError("invalid tool messages")
    adapter = _ScriptedDirectStub([boom])

    with pytest.raises(BadRequestError) as exc_info:
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert exc_info.value is boom  # 原异常透传
    assert slot1.errors == []  # 参数错误不冷却 key
    assert slot1.released == 1  # 槽位归还
    assert slot2.released == 0  # 未换 key


async def test_service_down_backs_off_then_rotates_key(
    fake_router_factory: dict[str, Any],
    fake_asyncio_sleep: list[float],
) -> None:
    """SERVICE_DOWN → 指数退避（2^n 秒，cap 16s）后换 key 重试成功。"""
    slot1 = FakeSlot("k1", "sk-1", consecutive_down=0)  # 退避 2*2^0 = 2s
    slot2 = FakeSlot("k2", "sk-2", consecutive_down=3)  # 退避封顶 16s
    slot3 = FakeSlot("k3", "sk-3")
    fake_router_factory["pool"] = FakePool([slot1, slot2, slot3])
    ok = SimpleNamespace(recovered=1)
    adapter = _ScriptedDirectStub([
        ServiceUnavailableError("upstream down"),
        ServiceUnavailableError("still down"),
        ok,
    ])

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is ok
    assert fake_asyncio_sleep == [2.0, 16.0]  # 指数退避 + 封顶（pacing 契约值）
    assert len(slot1.errors) == 1  # 失败 key 已交 KeySlot 处理（冷却）
    assert slot1.released == 1 and slot2.released == 1  # 槽位均归还


async def test_rate_limit_is_recoverable_and_rotates_key(
    fake_router_factory: dict[str, Any],
    fake_asyncio_sleep: list[float],
) -> None:
    """RATE_LIMIT 等可恢复错误：交 KeySlot 策略处理（无退避 sleep）后换 key 重试。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    ok = SimpleNamespace(rotated=1)
    adapter = _ScriptedDirectStub([RateLimitError("429 too many requests"), ok])

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is ok
    assert fake_asyncio_sleep == []  # 非 SERVICE_DOWN 不走退避 sleep
    assert len(slot1.errors) == 1  # 已交 KeySlot 统一策略（冷却/降级）
    assert slot1.released == 1 and slot2.released == 1


# ─────────────────────────── KeyPool：池耗尽翻译 ───────────────────────────


async def test_pool_exhausted_translates_to_rate_limit_with_cause(
    fake_router_factory: dict[str, Any],
) -> None:
    """池耗尽（所有 key 不可用）→ RateLimitError 翻译，__cause__ 保留原始异常。"""
    exhausted = _llm_exceptions.KeyPoolExhaustedError("apigo", timeout=30.0, unavailable=["k1"])
    # 槽位表非空（len 驱动重试轮数），acquire 到点抛耗尽 → 翻译面触发
    fake_router_factory["pool"] = FakePool([FakeSlot("k1", "sk-1")], acquire_exc=exhausted)
    fb_err = RuntimeError("router fallback down")
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    async def failing_route(**kwargs: Any) -> Any:
        raise fb_err

    adapter._route_call = failing_route  # type: ignore[method-assign]

    with pytest.raises(_litellm_mod.RateLimitError) as exc_info:
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert exc_info.value.__cause__ is exhausted  # 异常链保留
    assert "30" in str(exc_info.value)  # 等待超时信息透出
    assert "k1" in str(exc_info.value)  # 不可用 key 诊断透出


async def test_pool_exhausted_fallback_success_returns_result(
    fake_router_factory: dict[str, Any],
) -> None:
    """池耗尽但 Router fallback 成功 → 返回 fallback 结果（不误报失败）。"""
    exhausted = _llm_exceptions.KeyPoolExhaustedError("apigo", timeout=5.0, unavailable=["k1"])
    fake_router_factory["pool"] = FakePool([FakeSlot("k1", "sk-1")], acquire_exc=exhausted)
    sentinel = SimpleNamespace(fallback=1)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    async def ok_route(**kwargs: Any) -> Any:
        return sentinel

    adapter._route_call = ok_route  # type: ignore[method-assign]

    assert await adapter._do_completion(model="minimax-m3.1", messages=[]) is sentinel


# ─────────────────────────── 直连：timeout 语义 ───────────────────────────


@pytest.fixture
def fake_litellm_acompletion(monkeypatch: pytest.MonkeyPatch):
    """替换 litellm.acompletion（线程 worker 实际调用点），结果/异常/挂起可编排。"""
    calls: list[dict[str, Any]] = []
    box: dict[str, Any] = {"result": None, "error": None, "delay": 0.0, "hang": False}

    async def fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if box["hang"]:
            await asyncio.sleep(3600)
        if box["delay"]:
            await asyncio.sleep(box["delay"])
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    monkeypatch.setattr(_litellm_mod, "acompletion", fake_acompletion)
    return calls, box


async def test_direct_call_numeric_timeout_forwarded(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """显式数值 timeout：以调用方为准（不被首字节超时默认覆盖）。"""
    calls, box = fake_litellm_acompletion
    box["result"] = SimpleNamespace(ok=1)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x"), model="minimax-m3.1", messages=[],
        first_chunk_timeout=7, timeout=5.0,
    )

    assert calls[0]["timeout"] == 5.0


async def test_direct_call_non_numeric_timeout_keeps_first_chunk_timeout(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """timeout 为 httpx.Timeout 等非数值对象 → float 失败，保持 first_chunk_timeout。"""
    import httpx

    calls, box = fake_litellm_acompletion
    box["result"] = SimpleNamespace(ok=1)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x"), model="minimax-m3.1", messages=[],
        first_chunk_timeout=7, timeout=httpx.Timeout(5.0, connect=2.0),
    )

    assert calls[0]["timeout"] == 7.0  # 非数值不可覆盖 → HTTP 层沿用首字节超时


# ─────────────────────────── 直连：worker 装箱与超时 ───────────────────────────


async def test_direct_call_worker_exception_after_delay_raises_from_poll_loop(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """worker 延迟失败（建连后异常）→ 主循环轮询窗口内感知并抛出原始异常。"""
    _calls, box = fake_litellm_acompletion
    box["error"] = RuntimeError("boom after connect")
    box["delay"] = 0.25  # 失败晚于首个 0.1s 轮询窗口
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    with pytest.raises(RuntimeError, match="boom after connect"):
        await adapter._direct_call_with_slot(
            slot=FakeSlot("k", "sk-x"), model="minimax-m3.1", messages=[],
            first_chunk_timeout=5,
        )


class _ChunkThenRaiseStream:
    """首 chunk 正常、随后炸的桩流：驱动「队列非空 + 异常已装箱」出口。"""

    def __init__(self) -> None:
        self._n = 0

    def __aiter__(self) -> _ChunkThenRaiseStream:
        return self

    async def __anext__(self) -> str:
        self._n += 1
        if self._n == 1:
            return "chunk-a"
        raise RuntimeError("stream broke mid-flight")

    async def aclose(self) -> None:
        pass


async def test_direct_call_stream_worker_exception_surfaces_after_queue_exit(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """流式 worker 收 chunk 后炸 → 主循环经队列出口检查到装箱异常并抛出。"""
    _calls, box = fake_litellm_acompletion
    box["result"] = _ChunkThenRaiseStream()
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    with pytest.raises(RuntimeError, match="stream broke mid-flight"):
        await adapter._direct_call_with_slot(
            slot=FakeSlot("k", "sk-x"), model="minimax-m3.1", messages=[],
            first_chunk_timeout=5,
        )


async def test_direct_call_deadline_exceeded_raises_timeout(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """建连挂死 → deadline 到点抛 TimeoutError（HTTP 层超时双保险语义）。"""
    _calls, box = fake_litellm_acompletion
    box["hang"] = True  # litellm 挂死（残留线程由 daemon 回收）
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    with pytest.raises(asyncio.TimeoutError, match="litellm.acompletion 超时"):
        await adapter._direct_call_with_slot(
            slot=FakeSlot("k", "sk-x"), model="minimax-m3.1", messages=[],
            first_chunk_timeout=0.3,
        )
