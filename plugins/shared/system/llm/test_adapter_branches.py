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

外部依赖全桩（litellm / KeyPool / router_factory 查表 / 文件系统经
tmp_path / AGENTOS_LOG_DIR），绝不触网。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import queue
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

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致
import exceptions as _llm_exceptions  # noqa: E402
import key_pool as _kp_mod  # noqa: E402
import litellm as _litellm_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402

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


class _StubAdapter(adapter_mod._BaseLiteLLMAdapter):
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


# ─────────────────── payload 诊断钩子（AGENTOS_PAYLOAD_DIAG=1 路径） ───────────────────


class _TransformStub:
    """假 provider transformation：返回可控 body（类属性 body 注入，含坏结构场景）。

    async 版本不得复用 self.transform_request——钩子会给两个方法分别包一层，
    复用会导致一次调用写两份诊断文件。
    """

    body: Any = None

    def transform_request(self, model: str, messages: Any, optional_params: Any, litellm_params: Any, headers: Any) -> Any:
        if _TransformStub.body is not None:
            return _TransformStub.body
        return {"model": model, "messages": messages, "marker": "stub"}

    async def async_transform_request(self, model: str, messages: Any, optional_params: Any, litellm_params: Any, headers: Any) -> Any:
        if _TransformStub.body is not None:
            return _TransformStub.body
        return {"model": model, "messages": messages, "marker": "stub"}


_REAL_TRANSFORM_MODULES = (
    "litellm.llms.openai.chat.gpt_transformation",
    "litellm.llms.deepseek.chat.transformation",
    "litellm.llms.anthropic.chat.transformation",
)
_FAKE_TRANSFORM_MODULE = "litellm.llms.zhipu.chat.transformation"
# 原始桩方法（钩子会给类方法包 wrapper，测试后必须还原，防止跨测试叠层）
_STUB_SYNC_ORIG = _TransformStub.__dict__["transform_request"]
_STUB_ASYNC_ORIG = _TransformStub.__dict__["async_transform_request"]


@pytest.fixture
def fake_transform_module():
    """注入假 zhipu transformation 模块并保存/还原真实与假类的钩子现场。

    _install_payload_diag_hook 按 importlib.import_module 查找 4 个 transformation
    模块——sys.modules 命中即用，故假模块只需塞进 sys.modules。真实 openai/
    deepseek/anthropic 类与假桩类会被就地 patch，测试后还原快照，避免 wrapper
    叠层残留到邻居测试。
    """
    _TransformStub.body = None
    saved_mod = sys.modules.get(_FAKE_TRANSFORM_MODULE)
    mod = types.ModuleType(_FAKE_TRANSFORM_MODULE)
    mod.TransformStub = _TransformStub  # type: ignore[attr-defined]
    sys.modules[_FAKE_TRANSFORM_MODULE] = mod
    saved_attrs: list[tuple[type, Any, Any]] = []
    for mp in _REAL_TRANSFORM_MODULES:
        try:
            m = importlib.import_module(mp)
        except Exception:
            continue
        for obj in list(vars(m).values()):
            if isinstance(obj, type) and "transform_request" in obj.__dict__:
                saved_attrs.append(
                    (obj, obj.__dict__.get("transform_request"), obj.__dict__.get("async_transform_request"))
                )
    try:
        yield mod
    finally:
        if saved_mod is None:
            sys.modules.pop(_FAKE_TRANSFORM_MODULE, None)
        else:
            sys.modules[_FAKE_TRANSFORM_MODULE] = saved_mod
        for cls, sync_f, async_f in saved_attrs:
            if sync_f is not None:
                cls.transform_request = sync_f
            if async_f is not None:
                cls.async_transform_request = async_f
        _TransformStub.transform_request = _STUB_SYNC_ORIG  # type: ignore[method-assign]
        _TransformStub.async_transform_request = _STUB_ASYNC_ORIG  # type: ignore[method-assign]


async def test_payload_diag_hook_intercepts_and_writes_raw_payload(
    fake_transform_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """钩子安装后 transform 前后能看到真实 HTTP body，并按原始字节序落盘。"""
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    # messages 键在 model 之前（非字母序）：落盘内容若被 sort_keys 重排即失配
    body = {"messages": [{"role": "user", "content": "hi"}], "model": "zai/glm-test", "stream": True}
    with caplog.at_level(logging.INFO):
        adapter_mod._install_payload_diag_hook()

        out = _TransformStub.transform_request(_TransformStub(), "zai/glm-test", body["messages"], None, None, None)
        out_async = await _TransformStub.async_transform_request(
            _TransformStub(), "zai/glm-test", body["messages"], None, None, None
        )

    # 原方法返回值透传不被钩子改写
    assert out["marker"] == "stub"
    assert out_async["marker"] == "stub"
    # 同步 + 异步两条 wrapper 各拦截一次（文件名含毫秒时间戳，同毫秒合并同名为常态，
    # 故不断言文件数等于调用数，只断言拦截次数与落盘内容）
    post_records = [r for r in caplog.records if "POST_TRANSFORM model=" in r.getMessage()]
    assert len(post_records) == 2
    diag_dir = tmp_path / "logs" / "payload_diag"
    files = sorted(diag_dir.glob("*.json"))
    assert len(files) >= 1
    # 原始字节契约：落盘的是 transform 返回的 body（即真实发出的 HTTP body），
    # 与 ensure_ascii=False、无 sort_keys 的序列化逐字一致——桩 body 键序非字母序
    # （model, messages, marker），若被重排即失配
    expected_raw = json.dumps(
        {"model": "zai/glm-test", "messages": [{"role": "user", "content": "hi"}], "marker": "stub"},
        ensure_ascii=False,
    )
    assert files[0].read_text(encoding="utf-8") == expected_raw
    # 文件名携带 model/消息数元数据（model 中 "/" 被安全化）
    assert "zai_glm-test" in files[0].name
    assert files[0].name.endswith("1msg.json")
    # 拦截日志（含 body/msgs hash 摘要）确实打出
    assert any("POST_TRANSFORM model=zai/glm-test" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("cleanup_mode", ["rotate", "cleanup_failure"])
async def test_payload_diag_dir_rotation_cap(
    fake_transform_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_mode: str,
) -> None:
    """诊断目录超 200 上限轮转删最老；轮转清理失败不影响落盘主路径。"""
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    diag_dir = tmp_path / "logs" / "payload_diag"
    diag_dir.mkdir(parents=True)
    for i in range(202):
        (diag_dir / f"seed_{i:03d}.json").write_text("{}", encoding="utf-8")
    if cleanup_mode == "cleanup_failure":

        def _locked(*_a: Any, **_k: Any) -> None:
            raise PermissionError("file locked")

        monkeypatch.setattr(adapter_mod.os, "remove", _locked)

    adapter_mod._install_payload_diag_hook()
    _TransformStub.transform_request(_TransformStub(), "m", [{"role": "user", "content": "x"}], None, None, None)

    count = len(list(diag_dir.glob("*.json")))
    if cleanup_mode == "rotate":
        assert count == 200  # 上限性质：无论写入多少，收口恒为 200
    else:
        # 清理失败被吞：203 个文件保留、无异常外抛、诊断文件已写入
        assert count == 203


async def test_payload_diag_walks_up_to_anchor_project_root(
    fake_transform_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未设 AGENTOS_LOG_DIR：从 adapter 文件位置向上探测 config/kernel 锚定项目根。"""
    monkeypatch.delenv("AGENTOS_LOG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # 探测失败时回落 cwd → 落 tmp，不污染别处

    adapter_mod._install_payload_diag_hook()
    _TransformStub.transform_request(_TransformStub(), "m", [{"role": "user", "content": "x"}], None, None, None)

    # 与实现同一判据（config/kernel 目录）推导锚定根，作为预期落盘基目录
    base = Path(adapter_mod.__file__).resolve().parent
    while base != base.parent and not (base / "config" / "models").is_dir():
        base = base.parent
    expected_base = base if (base / "config" / "models").is_dir() else tmp_path
    files = list((expected_base / "logs" / "payload_diag").glob("*.json"))
    assert len(files) >= 1
    assert json.loads(files[-1].read_text(encoding="utf-8"))["marker"] == "stub"


async def test_payload_diag_failure_does_not_break_transform(
    fake_transform_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """诊断落盘失败（日志目录不可写）→ 异常被吞，transform 照常返回原 body。"""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    # AGENTOS_LOG_DIR 指向以普通文件为父的路径：makedirs 必失败（OSError 系）
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(blocker / "sub"))
    adapter_mod._install_payload_diag_hook()

    body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    out = _TransformStub.transform_request(_TransformStub(), "m", body["messages"], None, None, None)
    out_async = await _TransformStub.async_transform_request(_TransformStub(), "m", body["messages"], None, None, None)

    assert out["marker"] == "stub"  # 主路径返回不受诊断失败影响
    assert out_async["marker"] == "stub"
    assert not (tmp_path / "logs").exists()  # 未半途落盘


def test_payload_diag_install_failure_only_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钩子安装自身失败（模块扫描/日志通道）→ 仅告警，不向 import 方向外抛。"""

    class _BrokenInfoLogger:
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def info(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("info channel down")

        def warning(self, msg: str, *args: Any) -> None:
            self.warnings.append(msg % args if args else msg)

    broken = _BrokenInfoLogger()
    monkeypatch.setattr(adapter_mod, "logger", broken)

    def _boom(name: str, *args: Any) -> Any:
        raise RuntimeError(f"import unavailable: {name}")

    monkeypatch.setattr(importlib, "import_module", _boom)
    adapter_mod._install_payload_diag_hook()

    assert len(broken.warnings) == 1
    assert "拦截钩子安装失败" in broken.warnings[0]


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


async def test_stream_bridge_polls_empty_queue_until_done() -> None:
    """空队列 + 未 done：短轮询等待（不 busy 死循环）；done 置位后正常收流。"""
    done = threading.Event()
    bridge = adapter_mod._ThreadedStreamBridge(
        queue=queue.Queue(), done_evt=done, exc_box=[], close_evt=threading.Event()
    )

    async def _finish_later() -> None:
        await asyncio.sleep(0.12)
        done.set()

    setter = asyncio.ensure_future(_finish_later())
    with pytest.raises(StopAsyncIteration):
        await bridge.__anext__()
    await setter


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
    monkeypatch.setattr(adapter_mod, "_PROMPT_AUDIT_ENABLED", True)
    pl = adapter_mod._prompt_logger
    monkeypatch.setattr(pl, "handlers", [])
    monkeypatch.setattr(pl, "disabled", True)

    adapter_mod._log_prompt_body("zai/glm-test", [{"role": "user", "content": "hi"}], None, temperature=0.7)

    assert pl.handlers == []  # 未触发 handler 初始化
    assert not (tmp_path / "prompt_audit.log").exists()  # 零落盘


def test_resolve_prompt_log_path_prefers_existing_data_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """cwd（含向上）已有 data/logs → 直接锚定该目录。"""
    (tmp_path / "data" / "logs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    p = adapter_mod._resolve_prompt_log_path()

    assert p == str(tmp_path / "data" / "logs" / "prompt_audit.log")


def test_resolve_prompt_log_path_breaks_at_filesystem_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向上探测到文件系统根（parent == cwd）即止，回落 cwd 拼默认路径。"""
    monkeypatch.setattr(adapter_mod.os, "getcwd", lambda: "Z:\\")

    p = adapter_mod._resolve_prompt_log_path()

    assert p == os.path.join("Z:\\", "data", "logs", "prompt_audit.log")
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
    monkeypatch.setattr(_rf_mod, "get_litellm_prefix", fake_get_prefix)
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


@pytest.fixture
def fake_litellm_acompletion(monkeypatch: pytest.MonkeyPatch):
    """替换 litellm.acompletion（线程 worker 实际调用点），结果/异常可编排。"""
    calls: list[dict[str, Any]] = []
    box: dict[str, Any] = {"result": None, "error": None, "delay": 0.0}

    async def fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if box["delay"]:
            await asyncio.sleep(box["delay"])
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    monkeypatch.setattr(_litellm_mod, "acompletion", fake_acompletion)
    return calls, box


async def test_direct_call_forwards_slot_api_base(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """slot.api_base 非空 → 作为 api_base 进 litellm 调用；model 反查为 litellm 串。"""
    calls, box = fake_litellm_acompletion
    box["result"] = SimpleNamespace(ok=1)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x", api_base="https://slot-base.example.com"),
        model="minimax-m3.1",
        messages=[],
    )

    assert calls[0]["api_base"] == "https://slot-base.example.com"
    assert calls[0]["model"] == "openai/MiniMax-M3"  # model_id → prefix/model_name


class _GatedStream:
    """yield 首 chunk 后停顿再给下一 chunk，供主循环在停顿窗口调用 aclose。"""

    def __init__(self) -> None:
        self._n = 0
        self.aclose_called = 0

    def __aiter__(self) -> _GatedStream:
        return self

    async def __anext__(self) -> str:
        self._n += 1
        if self._n == 1:
            return "a"
        if self._n == 2:
            await asyncio.sleep(0.3)
            return "b"
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_called += 1


async def test_direct_call_worker_stops_iterating_after_close(
    fake_router_factory: dict[str, Any],
    fake_litellm_acompletion: Any,
) -> None:
    """桥接 aclose（close_evt 置位）→ worker 停止迭代并关闭底层流。"""
    _calls, box = fake_litellm_acompletion
    underlying = _GatedStream()
    box["result"] = underlying
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    bridge = await adapter._direct_call_with_slot(slot=FakeSlot("k", "sk-x"), model="m", messages=[])

    first = await bridge.__anext__()
    assert first == "a"
    await bridge.aclose()  # 设 close_evt：worker 应停止迭代

    deadline = _time.monotonic() + 3.0
    while underlying.aclose_called == 0:
        if _time.monotonic() > deadline:
            pytest.fail("worker 未在 close 后关闭底层流")
        await asyncio.sleep(0.05)
    assert underlying.aclose_called == 1
