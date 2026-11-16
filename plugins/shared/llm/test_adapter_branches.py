# @feature: FP-T07 llm api | @ci: python-coverage
"""adapter.py 未覆盖分支补测（payload_diag 钩子 / 流式边界 / 审计降级 / KeyPool 边界）。

行为契约（断输入→输出/副作用，不钉实现）：
- payload 诊断钩子（默认关闭契约由 test_payload_diag_hook.py 守护）：
  AGENTOS_LOG_DIR 落盘原始字节（字段序不变）、目录 200 上限轮转、清理失败与
  落盘失败不外抛、项目根锚定探测（config/models）、安装失败仅告警
- _await_with_escape 独立线程诊断：事件循环冻结时到点仍能记录
- _ThreadedStreamBridge 空队列短轮询直到 done
- prompt 审计降级：handler 初始化失败 → logger 停用后零开销跳过；
  data/logs 探测（命中 / 到文件系统根兜底）
- 流式：stream=True 分发、tools 注入上游、首 chunk finish_reason 透传、
  aclose 超时/异常不阻断响应、首 chunk 失败时 aclose 异常不掩盖原始异常、
  坏结构 chunk 观察面不炸、<think/> 状态机全分支、tool_call/thinking_end 事件面
- KeyPool：slot api_base 注入（两条路径）、CancelledError 直透不换 key 不
  fallback、零槽位时 fallback 失败抛 fallback 异常本体、绑定 release 的
  aclose 超时/异常、worker 收到 close 停止迭代并关闭底层流

外部依赖全桩（wire_client / KeyPool / router_factory 查表 / 文件系统经
tmp_path / AGENTOS_LOG_DIR），绝不触网。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time as _time
import types
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

# 裸名逐出：exceptions/key_pool/router_factory 等平铺名遍布各插件（cost_control
# 有自己的 exceptions.py），收集期若已被别的插件版本占位，本模块的
# `from exceptions import KeyPoolExhaustedError` 会打到错误实现（共跑收集
# ImportError）。逐出后由下面的 import 按本目录重解析。
for _bare in ("adapter", "exceptions", "key_pool", "router_factory", "stream_client"):
    sys.modules.pop(_bare, None)

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致
import exceptions as _llm_exceptions  # noqa: E402
import key_pool as _kp_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402
import wire_client as _wire_mod  # noqa: E402

# ─────────────────────────── 隔离 / 防串扰 ───────────────────────────


class _NoHandlersLogger(logging.Logger):
    """``.handlers`` 恒为空：屏蔽 pytest 日志插件挂的 handler（同 tests/ 根惯例）。"""

    @property
    def handlers(self) -> list[Any]:  # noqa: PLE0302
        return []

    @handlers.setter
    def handlers(self, value: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_diag_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """诊断 logger 隔离为无 handler：chunk 流水诊断分支稳定跳过，与宿主解耦。"""
    monkeypatch.setattr(
        adapter_mod, "_diag_logger", _NoHandlersLogger("adapter._diag.branches")
    )


@pytest.fixture(autouse=True)
def _pin_flat_modules():
    """重绑平铺裸名槽位到本文件实例（同 tests/test_llm_adapter_keypool.py 惯例）。"""
    saved: dict[str, types.ModuleType | None] = {}
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


# ─────────────────────────── 通用桩 ───────────────────────────


def _delta(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _choice(delta: SimpleNamespace, finish_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _tc(
    index: int = 0,
    *,
    id_: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(index=index, id=id_, function=SimpleNamespace(name=name, arguments=arguments))


def _chunk(choices: list[Any] | None = None, *, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(choices=choices if choices is not None else [], usage=usage)


def _usage(*, prompt: int = 10, completion: int = 5, total: int = 15, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


class _FakeStream:
    """按预设 chunk 序列异步迭代的桩流，aclose 可观测。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._it = iter(chunks)
        self.aclose_called = False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.aclose_called = True


class _StubAdapter(adapter_mod._BaseLLMAdapter):
    """_do_completion 返回预设流并记录 kwargs 的桩适配器。"""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.captured: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured = dict(kwargs)
        return self._stream


async def _run_stream(ad: Any, **kw: Any) -> Any:
    return await ad._call_streaming(
        "zai/glm-test",
        [{"role": "user", "content": "hi"}],
        inter_chunk_timeout=30,
        first_chunk_timeout=5,
        **kw,
    )


# ─────────────────────────── _await_with_escape ───────────────────────────


class _ImmediateFakeTimer:
    """注入时钟的 Timer 替身：start 即同步执行到期检查，消除线程竞态。"""

    def __init__(self, interval: float, function: Any, args: Any = None, kwargs: Any = None) -> None:
        self.interval = interval
        self._function = function

    def start(self) -> None:
        self._function()

    def cancel(self) -> None:
        pass


async def test_await_with_escape_diag_reports_unfinished_task(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task 到点未完成 → 独立线程诊断日志必打（不依赖事件循环存活）；结果正常透传。"""
    monkeypatch.setattr(threading, "Timer", _ImmediateFakeTimer)
    tasks_before = len(adapter_mod._background_tasks)

    async def _slow() -> str:
        await asyncio.sleep(0.05)
        return "done"

    with caplog.at_level(logging.ERROR, logger="adapter"):
        out = await adapter_mod._await_with_escape(_slow(), 5, what="diag-probe")

    assert out == "done"  # 检查到点后 task 仍完成 → 结果透传，不误杀
    assert any("独立线程诊断" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)  # 让 done 回调跑完
    assert len(adapter_mod._background_tasks) == tasks_before  # 登记表已清理


# ─────────────────────────── _ThreadedStreamBridge ───────────────────────────



# ─────────────────────────── prompt 审计降级路径 ───────────────────────────


def test_sync_prompt_handlers_degrades_to_disabled_on_unwritable_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计落盘路径不可写 → OSError 被吞，logger 置 disabled，不阻断调用方。"""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(blocker / "sub" / "prompt_audit.log"))
    pl = adapter_mod._prompt_logger
    monkeypatch.setattr(pl, "handlers", [])
    monkeypatch.setattr(pl, "disabled", False)  # 快照，测试后还原共享 logger 状态

    adapter_mod._sync_prompt_handlers()

    assert pl.disabled is True
    assert pl.handlers == []  # 初始化失败不残留半挂 handler


def test_log_prompt_body_zero_cost_when_logger_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """开关开启但 logger 已停用（降级态）→ 记录调用零落盘、不重建 handler。"""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(blocker / "sub" / "prompt_audit.log"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        "diagnostics:\n  prompt_audit: true\n", encoding="utf-8"
    )
    from agentos_plugin_sdk import diagnostics_settings as ds

    ds._cache.clear()
    pl = adapter_mod._prompt_logger
    monkeypatch.setattr(pl, "handlers", [])
    monkeypatch.setattr(pl, "disabled", True)

    adapter_mod._log_prompt_body("zai/glm-test", [{"role": "user", "content": "hi"}], None, temperature=0.7)

    assert pl.handlers == []  # 未触发 handler 初始化
    assert not (tmp_path / "prompt_audit.log").exists()  # 零落盘


def test_resolve_prompt_log_path_ignores_cwd_data_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """cwd 及其上级的 data/logs 不再参与锚定（位置推导退役，裁定 2026-11-02）。

    存量目录即便存在也不得劫持落盘位置——审计日志唯一锚 = 用户空间 logs。
    """
    (tmp_path / "data" / "logs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTOS_LOG_PROMPT_FILE", raising=False)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))

    p = adapter_mod._resolve_prompt_log_path()

    assert p == str(tmp_path / "user" / "logs" / "prompt_audit.log")


def test_resolve_prompt_log_path_filename_stable_at_any_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """用户根指向任意路径（含盘根形态）文件名恒为 prompt_audit.log。"""
    monkeypatch.delenv("AGENTOS_LOG_PROMPT_FILE", raising=False)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))

    p = adapter_mod._resolve_prompt_log_path()

    assert p == str(tmp_path / "logs" / "prompt_audit.log")
    assert os.path.basename(p) == "prompt_audit.log"


# ─────────────────────────── _sync_diag_handlers ───────────────────────────


def test_sync_diag_handlers_inherits_parent_file_handler_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """父 logger 有 FileHandler → 复制到 diag logger 并提级 DEBUG；重复调用幂等。"""
    parent = logging.getLogger("adapter_branches.parent")
    fh = logging.FileHandler(tmp_path / "parent.log", delay=True)
    diag = logging.getLogger("adapter_branches.diag")
    monkeypatch.setattr(adapter_mod, "logger", parent)
    monkeypatch.setattr(adapter_mod, "_diag_logger", diag)
    monkeypatch.setattr(parent, "handlers", [fh])
    monkeypatch.setattr(diag, "handlers", [])
    monkeypatch.setattr(diag, "level", diag.level)
    try:
        adapter_mod._sync_diag_handlers()
        assert diag.handlers == [fh]
        assert diag.level == logging.DEBUG

        adapter_mod._sync_diag_handlers()  # 已挂 → 幂等不重复
        assert diag.handlers == [fh]
    finally:
        fh.close()


def test_sync_diag_handlers_skips_non_file_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """父 logger 只有非 FileHandler（如 NullHandler）→ diag 不挂任何 handler。"""
    parent = logging.getLogger("adapter_branches.parent_nofile")
    diag = logging.getLogger("adapter_branches.diag_nofile")
    monkeypatch.setattr(adapter_mod, "logger", parent)
    monkeypatch.setattr(adapter_mod, "_diag_logger", diag)
    monkeypatch.setattr(parent, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(diag, "handlers", [])

    adapter_mod._sync_diag_handlers()

    assert diag.handlers == []


# ─────────────────────────── completion 分发 / 流式边界 ───────────────────────────


async def test_completion_stream_true_routes_to_streaming_path() -> None:
    """completion(stream=True) → 走流式编排（建连参数带 stream/drop_params），文本可用。"""
    chunks = [
        _chunk([_choice(_delta(content="hi there"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad.completion(
        "zai/glm-test",
        [{"role": "user", "content": "hi"}],
        stream=True,
        first_chunk_timeout=5,
        inter_chunk_timeout=30,
    )

    assert resp.text == "hi there"
    assert ad.captured["stream"] is True
    assert ad.captured["drop_params"] is True
    assert ad.captured["stream_options"] == {"include_usage": True}


async def test_call_streaming_passes_tools_to_upstream() -> None:
    """tools 声明随流式调用原样进入上游请求参数。"""
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    ad = _StubAdapter(_FakeStream([_chunk([_choice(_delta(content="ok"))]), _chunk(usage=_usage())]))

    resp = await _run_stream(ad, tools=tools)

    assert ad.captured["tools"] == tools
    assert resp.text == "ok"


async def test_first_chunk_finish_reason_is_surfaced() -> None:
    """首 chunk 携带 finish_reason → 透传到最终响应。"""
    chunks = [
        _chunk([_choice(_delta(content="hi"), finish_reason="stop")]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await _run_stream(ad)

    assert resp.finish_reason == "stop"


async def test_stream_survives_aclose_error_in_finally() -> None:
    """收尾 aclose 自身异常 → 被吞，响应正常返回（不掩盖主路径结果）。"""

    class _BrokenCloseStream(_FakeStream):
        async def aclose(self) -> None:
            raise RuntimeError("socket gone")

    chunks = [_chunk([_choice(_delta(content="payload"))]), _chunk(usage=_usage())]
    ad = _StubAdapter(_BrokenCloseStream(chunks))

    resp = await _run_stream(ad)

    assert resp.text == "payload"


async def test_stream_survives_hanging_aclose_via_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半死连接 aclose 挂起 → 限时放弃关闭，响应照常返回（不永久阻塞 finally）。"""
    monkeypatch.setattr(adapter_mod, "_ACLOSE_TIMEOUT_SECONDS", 0.1)

    class _HangingCloseStream(_FakeStream):
        async def aclose(self) -> None:
            await asyncio.sleep(0.4)
            self.aclose_called = True

    chunks = [_chunk([_choice(_delta(content="still ok"))]), _chunk(usage=_usage())]
    stream = _HangingCloseStream(chunks)
    ad = _StubAdapter(stream)

    resp = await _run_stream(ad)

    assert resp.text == "still ok"
    assert stream.aclose_called is False  # 优雅关闭被放弃，未执行到底


async def test_first_chunk_failure_close_error_does_not_mask_original() -> None:
    """首 chunk 失败且 aclose 也炸 → 原始异常透传（不被 aclose 异常顶替）。"""

    class _FirstChunkErrorStream(_FakeStream):
        async def __anext__(self) -> Any:
            raise RuntimeError("upstream reset")

        async def aclose(self) -> None:
            raise ValueError("close blew up")

    ad = _StubAdapter(_FirstChunkErrorStream([]))
    with pytest.raises(RuntimeError, match="upstream reset"):
        await _run_stream(ad)


# ─────────────────────────── chunk 流水诊断（diag logger 有 handler 时） ───────────────────────────


class _RecordingDiag:
    """记录 debug 行的诊断 logger 桩（带 handler 使诊断分支生效）。"""

    def __init__(self) -> None:
        self.handlers: list[Any] = [object()]
        self.lines: list[str] = []

    def addHandler(self, _h: Any) -> None:  # noqa: N802
        pass

    def debug(self, msg: str, *args: Any) -> None:
        self.lines.append(msg % args)


async def _run_with_recording_diag(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> tuple[Any, list[str]]:
    rec = _RecordingDiag()
    monkeypatch.setattr(adapter_mod, "_diag_logger", rec)
    chunks_iter = iter(chunks)

    class _OneShot(_FakeStream):
        async def __anext__(self) -> Any:
            try:
                return next(chunks_iter)
            except StopIteration:
                raise StopAsyncIteration from None

    ad = _StubAdapter(_OneShot([]))
    resp = await _run_stream(ad)
    return resp, rec.lines


async def test_chunk_flow_diag_logs_head_and_usage_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前 2 个 chunk 必记流水；中段普通 chunk 跳过；idx%200==0 或携 usage 补记。"""
    # 200 个普通正文 chunk + 1 个携带 usage 的收尾 chunk（落在 idx=200，命中 %200 闸门；
    # choices 非空——choices=[] 的 usage chunk 在诊断分支会索引越界，见既有测试的
    # _HandlerlessLogger 防御注释）
    chunks = [_chunk([_choice(_delta(content="a"))]) for _ in range(200)]
    chunks.append(_chunk([_choice(_delta(), finish_reason="stop")], usage=_usage()))

    resp, lines = await _run_with_recording_diag(monkeypatch, chunks)

    assert len(resp.text) == 200
    assert any("chunk #0" in line for line in lines)  # 首 chunk 必记
    assert any("chunk #1" in line for line in lines)  # 第 2 chunk 必记
    assert not any("chunk #199" in line for line in lines)  # 中段普通 chunk 跳过
    assert any("chunk #200" in line and "usage=Y" in line for line in lines)  # %200 + usage 补记


async def test_chunk_flow_diag_flags_tool_call_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tool_calls chunk 无论序号一律补记 tc=Y。"""
    chunks = [
        _chunk([_choice(_delta(tool_calls=[_tc(0, id_="call_1", name="f", arguments="{}")]))]),
        _chunk([_choice(_delta(content="done"))]),
    ]

    resp, lines = await _run_with_recording_diag(monkeypatch, chunks)

    assert resp.tool_calls == [{"id": "call_1", "name": "f", "arguments": "{}"}]
    assert any("tc=Y" in line for line in lines)


# ─────────────────────────── <think/> 状态机分支 ───────────────────────────


async def test_think_tag_prefix_text_and_in_tag_continuation() -> None:
    """开标签前前缀走 text 事件（检查 stop）；标签内续片走 thinking；闭合后回 text。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> None:
        received.append(evt)

    chunks = [
        _chunk([_choice(_delta(content="Hello<think>plan"))]),
        _chunk([_choice(_delta(content="more"))]),  # 标签内、无闭合 → 续 thinking
        _chunk([_choice(_delta(content="</think>Answer"))]),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], on_chunk=_on_chunk,
        inter_chunk_timeout=30, first_chunk_timeout=5,
    )

    assert resp.text == "HelloAnswer"
    assert resp.thinking_text == "planmore"
    assert resp.stream_repetition is False
    assert received == [
        {"type": "text", "content": "Hello"},
        {"type": "thinking", "content": "plan"},
        {"type": "thinking", "content": "more"},
        {"type": "text", "content": "Answer"},
    ]


@pytest.mark.parametrize("stop_on_text", [True, False])
async def test_think_open_and_close_in_same_chunk(stop_on_text: bool) -> None:
    """开闭标签同 chunk（带前缀）：闭合后正文可被 stop 截断，也可继续消费。

    首个 chunk 放普通正文：首 chunk 由建连步骤处理（不中断消费循环），
    stop 语义须落在循环 chunk 上才体现截断。
    """
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> Any:
        received.append(evt)
        if stop_on_text and evt["type"] == "text" and evt["content"] == "b":
            return "stop"
        return None

    chunks = [
        _chunk([_choice(_delta(content="0"))]),
        _chunk([_choice(_delta(content="x<think>a</think>b"))]),
        _chunk([_choice(_delta(content="!"))]),  # stop 时不应处理
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], on_chunk=_on_chunk,
        inter_chunk_timeout=30, first_chunk_timeout=5,
    )

    expected_text = "0xb" if stop_on_text else "0xb!"
    assert resp.text == expected_text
    assert resp.thinking_text == "a"
    assert resp.stream_repetition is stop_on_text
    assert received[0] == {"type": "text", "content": "0"}
    assert {"type": "text", "content": "x"} in received


async def test_think_tag_close_with_stop_sets_repetition() -> None:
    """标签内收到闭合 + 正文 stop → 截断并置 stream_repetition。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> Any:
        received.append(evt)
        if evt["type"] == "text" and evt["content"] == "c":
            return "stop"
        return None

    chunks = [
        _chunk([_choice(_delta(content="<think>a"))]),
        _chunk([_choice(_delta(content="b</think>c"))]),
        _chunk([_choice(_delta(content="never"))]),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], on_chunk=_on_chunk,
        inter_chunk_timeout=30, first_chunk_timeout=5,
    )

    assert resp.stream_repetition is True
    assert resp.text == "c"
    assert resp.thinking_text == "ab"
    assert "never" not in resp.text


# ─────────────────────────── tool_call 事件面 ───────────────────────────


async def test_tool_call_events_emit_after_thinking_end() -> None:
    """thinking → tool_calls 过渡：先补 thinking_end 再发 tool_call 事件。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> None:
        received.append(evt)

    tc = _tc(0, id_="call_9", name="search", arguments='{"q":')
    chunks = [
        _chunk([_choice(_delta(reasoning="plan"))]),
        _chunk([_choice(_delta(tool_calls=[tc]))]),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], on_chunk=_on_chunk,
        inter_chunk_timeout=30, first_chunk_timeout=5,
    )

    assert [e["type"] for e in received] == ["thinking", "thinking_end", "tool_call"]
    assert received[-1]["tool_calls"] == [tc]  # 事件携带原始增量
    assert resp.tool_calls == [{"id": "call_9", "name": "search", "arguments": '{"q":'}]


async def test_tool_call_event_without_prior_thinking() -> None:
    """无 thinking 直接工具调用：只发 tool_call 事件，不画蛇添足补 thinking_end。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> None:
        received.append(evt)

    chunks = [_chunk([_choice(_delta(tool_calls=[_tc(0, id_="c1", name="f", arguments="{}")]))])]
    ad = _StubAdapter(_FakeStream(chunks))

    await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], on_chunk=_on_chunk,
        inter_chunk_timeout=30, first_chunk_timeout=5,
    )

    assert [e["type"] for e in received] == ["tool_call"]


# ─────────────────────────── 接收端点诊断鲁棒性 ───────────────────────────


async def test_recv_diag_swallows_malformed_choice() -> None:
    """choice.finish_reason 访问即抛 → 观察面吞异常留痕，流处理不受影响。"""

    class _BadFinishChoice:
        def __init__(self) -> None:
            self.delta = _delta(content="first")

        @property
        def finish_reason(self) -> str:
            raise RuntimeError("corrupt frame")

    chunks = [
        _chunk([_BadFinishChoice()]),
        _chunk([_choice(_delta(content="ok"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await _run_stream(ad)

    assert resp.text == "firstok"  # 坏结构 chunk 不中断消费（delta 正文照常累积）
    assert resp.finish_reason is None  # 观察失败不产生假 finish


# ─────────────────────────── KeyPool 边界 ───────────────────────────


class FakeSlot:
    """KeySlot 桩：记录 release/on_success/handle_error。"""

    def __init__(self, key_id: str, api_key: str, api_base: str | None = None) -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = api_base
        self._consecutive_down = 0
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
    """acquire_slot 按预排槽位序列逐个给出。"""

    def __init__(self, slots: list[FakeSlot]) -> None:
        self.slots: list[Any] = slots
        self._seq = list(slots)
        self.acquires = 0

    async def acquire_slot(self) -> Any:
        self.acquires += 1
        if self._seq:
            return self._seq.pop(0)
        return self.slots[0]


@pytest.fixture
def fake_router_factory(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """router_factory 查表桩：单 provider=apigo / prefix=openai / 默认无池。"""
    state: dict[str, Any] = {
        "pool": None,
        "provider": "apigo",
        "prefix": "openai",
        "model_name": {"minimax-m3.1": "MiniMax-M3"},
        "router_calls": [],
        "router_result": SimpleNamespace(router="ok"),
    }

    def fake_get_key_pool(provider: str) -> Any:
        return state["pool"]

    def fake_get_provider(model_id: str) -> str | None:
        return state["provider"]

    def fake_get_prefix(provider: str) -> str:
        return state["prefix"]

    def fake_get_model_name(model_id: str) -> str:
        return state["model_name"].get(model_id, model_id)

    monkeypatch.setattr(_rf_mod, "get_key_pool", fake_get_key_pool)
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", fake_get_provider)
    monkeypatch.setattr(_rf_mod, "get_provider_prefix", fake_get_prefix)
    monkeypatch.setattr(_rf_mod, "get_model_name_for_id", fake_get_model_name)
    return state


class _DirectStub(adapter_mod.KeyPoolAdapter):
    """覆盖 _direct_call_with_slot：按脚本吐结果/异常并记录 kwargs。"""

    def __init__(self, router: Any, script: list[Any]) -> None:
        super().__init__(router)
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def _direct_call_with_slot(self, slot: Any = None, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):  # CancelledError 不是 Exception 子类
            raise outcome
        return outcome


async def test_slot_api_base_forwarded_to_direct_call(fake_router_factory: dict[str, Any]) -> None:
    """slot 携带 api_base → 注入直连调用（与既有「无 api_base 不注入」互补）。"""
    slot = FakeSlot("k1", "sk-1", api_base="https://relay.example.com/v1")
    fake_router_factory["pool"] = FakePool([slot])
    adapter = _DirectStub(router=None, script=[SimpleNamespace(resp=1)])

    await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert adapter.calls[0]["api_base"] == "https://relay.example.com/v1"


async def test_cancelled_error_propagates_without_rotation_or_fallback(
    fake_router_factory: dict[str, Any],
) -> None:
    """用户取消：直透 CancelledError——不冷却 key、不换 key、不触发 Router fallback。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    adapter = _DirectStub(router=None, script=[asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert slot1.released == 1  # 槽位归还
    assert slot1.errors == []  # 取消不冷却
    assert fake_router_factory["router_calls"] == []  # 取消不走 fallback


async def test_fallback_error_raised_when_no_key_was_attempted(
    fake_router_factory: dict[str, Any],
) -> None:
    """池零槽位（无 key 可试）且 Router fallback 也失败 → 抛 fallback 异常本体。"""
    fake_router_factory["pool"] = FakePool([])
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    fb_err = RuntimeError("router fallback down")

    async def failing_route(**kwargs: Any) -> Any:
        raise fb_err

    adapter._route_call = failing_route  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as ei:
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert ei.value is fb_err  # 无原始 key 异常可回抛 → fallback 异常即最终异常


async def test_router_fallback_success_attaches_degradation_notice(
    fake_router_factory: dict[str, Any],
) -> None:
    """观测C（R7）：所有 key 均失败 → Router fallback 接管成功 → 返回值携带
    降级标记（keypool_fallback_notice）——用户可见信号的载体，禁静默降级。"""
    fake_router_factory["pool"] = FakePool([])
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    fb_result = SimpleNamespace(resp="fallback-stream")

    async def ok_route(**kwargs: Any) -> Any:
        return fb_result

    adapter._route_call = ok_route  # type: ignore[method-assign]

    result = await adapter._do_completion(model="minimax-m3.1", messages=[])

    notice = getattr(result, "keypool_fallback_notice", None)
    assert notice is not None, "fallback 接管后返回值必须携带降级标记"
    assert notice["provider"] == "apigo"
    assert notice["model"] == "minimax-m3.1"
    assert "部分 API key 暂不可用" in notice["message"]


async def test_key_success_after_retry_carries_no_degradation_notice(
    fake_router_factory: dict[str, Any],
) -> None:
    """防拟合对照：key 换试成功（未走 Router fallback）→ 返回值零降级标记。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    adapter = _DirectStub(router=None, script=[RuntimeError("boom"), SimpleNamespace(resp=1)])

    result = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert result.resp == 1
    assert not hasattr(result, "keypool_fallback_notice"), "未 fallback 不得伪造降级标记"
    assert fake_router_factory["router_calls"] == []


# ─────────────── _bind_release_to_stream：aclose 超时 / 异常 ───────────────


async def test_bound_aclose_timeout_abandons_but_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """底层 aclose 挂起（半死 socket）→ 限时放弃优雅关闭，release 已完成且不外抛。"""
    monkeypatch.setattr(adapter_mod, "_ACLOSE_TIMEOUT_SECONDS", 0.1)
    slot = FakeSlot("k", "sk-1")

    class _HangingCloseStream:
        async def aclose(self) -> None:
            await asyncio.sleep(0.4)

    stream = _HangingCloseStream()
    adapter_mod.KeyPoolAdapter._bind_release_to_stream(stream, slot)

    await stream.aclose()

    assert slot.released == 1  # 释放先于关闭，放弃关闭不影响许可归还


async def test_bound_aclose_swallows_original_close_error() -> None:
    """底层 aclose 抛异常（非超时）→ 吞掉不阻断 finally，release 恰一次。"""
    slot = FakeSlot("k", "sk-1")

    class _ExplodingCloseStream:
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    stream = _ExplodingCloseStream()
    adapter_mod.KeyPoolAdapter._bind_release_to_stream(stream, slot)

    await stream.aclose()
    await stream.aclose()  # 幂等：release 只执行一次

    assert slot.released == 1


# ─────────────── _direct_call_with_slot：api_base / worker close 停止 ───────────────



# ─────────────── _accumulate_tool_call_deltas：tool_call id 单一关联键 ───────────────


def _tc_chunk(idx: int, id: str | None, name: str = "", arguments: str = "") -> SimpleNamespace:
    """构造归一形态的 tool_calls delta chunk。"""
    return SimpleNamespace(index=idx, id=id, function=SimpleNamespace(name=name, arguments=arguments))


def test_stream_tool_call_late_id_adopts_placeholder() -> None:
    """首个 chunk 无 id → 占位；迟到的真实 id 覆盖占位（与流事件同键）。

    单一关联键（ADR 2026-09-28-tool-call-live-card）：translator 取首个非空
    chunk id 发流事件，最终结果必须同键——占位不覆盖会让同一工具调用以
    两个 id 分叉（前端双卡）。
    """
    state = adapter_mod._StreamState()
    acc = adapter_mod._BaseLLMAdapter._accumulate_tool_call_deltas
    # self 未被该方法使用；类型面仍传真实实例（None 裸调是 mypy arg-type 红）
    noop = adapter_mod._BaseLLMAdapter()

    acc(noop, [_tc_chunk(0, None, name="f", arguments='{"a"')], state)
    placeholder = state.tool_calls_map[0]["id"]
    assert placeholder  # 占位存在（tc_ 前缀形态）
    assert state.tool_calls_map[0]["_id_fabricated"] is True

    acc(noop, [_tc_chunk(0, "call-real-1")], state)

    assert state.tool_calls_map[0]["id"] == "call-real-1"
    assert state.tool_calls_map[0]["_id_fabricated"] is False


def test_stream_tool_call_first_id_not_overwritten() -> None:
    """首个 chunk 已带真实 id → 后续 chunk 携带不同 id 不覆盖（首非空赢，
    与 translator 的 not state.id 语义一致）。"""
    state = adapter_mod._StreamState()
    acc = adapter_mod._BaseLLMAdapter._accumulate_tool_call_deltas
    # self 未被该方法使用；类型面仍传真实实例（None 裸调是 mypy arg-type 红）
    noop = adapter_mod._BaseLLMAdapter()

    acc(noop, [_tc_chunk(0, "call-first", name="f")], state)
    acc(noop, [_tc_chunk(0, "call-second")], state)

    assert state.tool_calls_map[0]["id"] == "call-first"
    assert state.tool_calls_map[0]["_id_fabricated"] is False


def test_stream_tool_call_no_id_keeps_placeholder() -> None:
    """全程无 id → 占位保留（占位 id 即最终 id，流事件侧无 id 不建卡，无双卡面）。"""
    state = adapter_mod._StreamState()
    acc = adapter_mod._BaseLLMAdapter._accumulate_tool_call_deltas
    noop = adapter_mod._BaseLLMAdapter()

    acc(noop, [_tc_chunk(0, None, name="f", arguments="{}")], state)
    acc(noop, [_tc_chunk(0, None, arguments="{}")], state)

    tid = state.tool_calls_map[0]["id"]
    assert tid.startswith("tc_0_")
    assert state.tool_calls_map[0]["_id_fabricated"] is True


# ─────────────── 流式超时键归一（装机 2026-11-04 死窗 10min 事故回归） ───────────────


class _StallingAfterFirstStream:
    """首 chunk 即达、随后永久静默的桩流（模拟上游中途冻结，aclose 可观测）。"""

    def __init__(self, first: Any) -> None:
        self._first = first
        self.aclose_called = False

    def __aiter__(self) -> _StallingAfterFirstStream:
        return self

    async def __anext__(self) -> Any:
        if self._first is not None:
            chunk, self._first = self._first, None
            return chunk
        await asyncio.sleep(30)  # 上游冻结：watchdog 必须先于此超时掐断
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_called = True


class _HangingDoCompletionAdapter(adapter_mod._BaseLLMAdapter):
    """_do_completion 永久挂起的桩适配器（模拟建连后首字节不到达）。"""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured = dict(kwargs)
        await asyncio.sleep(30)
        raise AssertionError("unreachable: watchdog 应先掐断")


def test_normalize_timeout_keys_maps_and_keeps_internal_precedence() -> None:
    """配置键→内部键归一：配置键映射生效；内部键在场时配置键仅弹出（内部
    键优先）；两组键绝不残留（残留即随请求体外发上游触发参数拒绝）。"""
    # 配置键映射
    kw: dict[str, Any] = {"stream_idle_timeout": 30, "first_token_timeout": 5}
    adapter_mod._normalize_timeout_keys(kw)
    assert kw == {"inter_chunk_timeout": 30, "first_chunk_timeout": 5}
    # 内部键优先，配置键弹出
    kw = {"inter_chunk_timeout": 30, "stream_idle_timeout": 999,
          "first_chunk_timeout": 5, "first_token_timeout": 999}
    adapter_mod._normalize_timeout_keys(kw)
    assert kw == {"inter_chunk_timeout": 30, "first_chunk_timeout": 5}
    # 两键皆缺 → 原样（默认值由调用点 pop 决定）
    kw = {"temperature": 0.7}
    adapter_mod._normalize_timeout_keys(kw)
    assert kw == {"temperature": 0.7}


async def test_config_native_stream_idle_timeout_drives_watchdog() -> None:
    """llm.yaml 配置键 stream_idle_timeout 直接驱动 inter-chunk 看门狗。

    事故形态：配置键从未被消费（llm_core 不透传 + adapter 只认内部键），
    流中途冻结恒等 600s 兜底才判错。修复后配置键即刻生效。
    """
    stream = _StallingAfterFirstStream(_chunk([_choice(_delta(reasoning="plan"))]))
    ad = _StubAdapter(stream)

    t0 = _time.monotonic()
    with pytest.raises(_wire_mod.WireTimeoutError, match="inter-chunk"):
        await ad._call_streaming(
            "zai/glm-test", [{"role": "user", "content": "hi"}],
            stream_idle_timeout=0.3, first_token_timeout=5,
        )
    assert _time.monotonic() - t0 < 5.0  # 0.3s 到点掐断，绝不死等 30s 桩流
    # 配置键不残留进上游请求参数（UnsupportedParamsError 面）
    assert "stream_idle_timeout" not in ad.captured
    assert "first_token_timeout" not in ad.captured


async def test_config_native_first_token_timeout_drives_connect_watchdog() -> None:
    """显式 first_token_timeout（直连/测试容差路径）仍驱动首字节看门狗。"""
    ad = _HangingDoCompletionAdapter()

    with pytest.raises(_wire_mod.WireTimeoutError, match="first chunk"):
        await ad._call_streaming(
            "zai/glm-test", [{"role": "user", "content": "hi"}],
            first_token_timeout=0.3, stream_idle_timeout=30,
        )
    assert "first_token_timeout" not in ad.captured
    assert "stream_idle_timeout" not in ad.captured


async def test_stream_idle_timeout_also_governs_first_chunk_budget() -> None:
    """吐字间隔语义统一（2026-11-04 用户裁定）：stream_idle_timeout 同时覆盖
    首 token 间隔——未显式给 first_chunk_timeout 时，首块预算=吐字间隔预算，
    建连后迟迟不出首 token 也按同一预算掐断（不再依赖退役的独立更短预算）。"""
    ad = _HangingDoCompletionAdapter()

    t0 = _time.monotonic()
    with pytest.raises(_wire_mod.WireTimeoutError, match="first chunk"):
        await ad._call_streaming(
            "zai/glm-test", [{"role": "user", "content": "hi"}],
            stream_idle_timeout=0.3,
        )
    assert _time.monotonic() - t0 < 5.0
    assert "stream_idle_timeout" not in ad.captured


async def test_config_native_timeout_keys_happy_path_streams_normally() -> None:
    """配置键在场 + 流健康 → 正常出流（归一不破坏正常消费）。"""
    chunks = [_chunk([_choice(_delta(reasoning="plan"))]),
              _chunk([_choice(_delta(content="答案"))])]
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream(chunks))

    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}],
        on_chunk=received.append,
        stream_idle_timeout=30, first_token_timeout=5,
    )

    assert resp.text == "答案"
    assert resp.thinking_text == "plan"
    assert [e["type"] for e in received] == ["thinking", "thinking_end", "text"]
