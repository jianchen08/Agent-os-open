# @feature: FP-T07 llm api | @ci: python-coverage
"""归一化重活出事件循环 + 复读扫描确定性成本预算（2026-11-12 宿主心跳自杀根修）。

事故：装机版宿主机 light:1 连续 7 次事件循环心跳停滞 30.9-35.5s 被 watchdog
自杀，随后内核异常终止。全部 faulthandler 栈落在同一链：
``llm_complete_stream``（async handler，SDK 直接 await）→
``_normalize_inbound_messages`` → ``collapse_degenerate_history`` →
``_collapse_repeated_runs`` → 复读正则 ``sub``。

断行为（不钉实现）：
- 归一化在 worker 线程执行：同步阻塞归一化期间事件循环仍可调度，返回值/
  contextvars/异常语义不变（线程身份 + 心跳 tick + 异常原样传播三面断言）；
- 复读折叠对小文本仍是全文精确语义（正常正文不动、退化重复折叠）；
- 大历史折叠有确定性字符预算：喂给复读正则的字符数不超过
  ``_DEGENERATE_CALL_SCAN_BUDGET_CHARS``，且真退化消息仍整段折叠；
- 事故形态（近重复大块、两档正则零命中）不再烧平方级回溯（探针短路）。
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import _message_normalizer as mn  # noqa: E402

_MOD_NAME = "llm_server_cost_budget_test"

# 退化形态与事故形态两类有区分度输入（行为断言必须两者都成立）
_PERIODIC_UNIT = "本轮一条链完成收紧与提交：检查 key_pool 退避与超时策略，回归流式契约。\n"
_NEAR_REPEAT_LINE = (
    "本轮状态梳理：检查插件声明面的 output_schema 与 render 意图，"
    "确认工具白名单与状态键口径一致，随后落盘并回归"
)


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    async def notify(self, method: str, params: dict[str, Any]) -> None:  # noqa: ARG002
        return None


class RecordingAdapter:
    """记录 completion 收到载荷的伪 adapter（外部依赖替身）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def completion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="ok",
            tool_calls=[],
            thinking_text=None,
            usage=None,
            finish_reason="stop",
        )


class _SpyPattern:
    """复读正则代理：统计实际喂给正则的字符数（成本预算的确定性度量）。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.scanned = 0

    def finditer(self, text: str, *args: Any) -> Any:
        self.scanned += (args[1] - args[0]) if len(args) >= 2 else len(text)
        return self.inner.finditer(text, *args)

    def sub(self, repl: Any, text: str) -> Any:
        self.scanned += len(text)
        return self.inner.sub(repl, text)


class _ScanCounter:
    def __init__(self, *spies: _SpyPattern) -> None:
        self._spies = spies

    @property
    def scanned(self) -> int:
        return sum(spy.scanned for spy in self._spies)


@pytest.fixture
def scan_counter(monkeypatch: pytest.MonkeyPatch) -> _ScanCounter:
    """两条复读正则换成计数代理（短路路径应恒等于 0）。"""
    run_spy = _SpyPattern(mn._DEGENERATE_RUN_PATTERN)
    block_spy = _SpyPattern(mn._DEGENERATE_BLOCK_PATTERN)
    monkeypatch.setattr(mn, "_DEGENERATE_RUN_PATTERN", run_spy)
    monkeypatch.setattr(mn, "_DEGENERATE_BLOCK_PATTERN", block_spy)
    return _ScanCounter(run_spy, block_spy)


def _install_bus(mod: Any, adapter: RecordingAdapter) -> None:
    bus = FakeBus()

    class _BusPlugin:
        @staticmethod
        def get_capability(name: str) -> Any:
            if name == "event-bus":
                return bus
            raise KeyError(name)

    mod._adapter = adapter
    mod.plugin.get_capability = _BusPlugin.get_capability  # type: ignore[method-assign]


def _run_through_server(
    mod: Any,
    messages: list[dict[str, Any]],
    *,
    model: str,
) -> RecordingAdapter:
    """经 llm_complete_stream 全链路跑一次，返回记录载荷的伪 adapter。"""
    adapter = RecordingAdapter()
    _install_bus(mod, adapter)

    async def _scenario() -> None:
        await mod.llm_complete_stream(model=model, messages=messages)

    asyncio.run(_scenario())
    return adapter


class TestNormalizeOffEventLoop:
    """归一化必须离开事件循环线程执行（async handler 直 await 会让 CPU 重活冻住心跳）。"""

    def test_normalize_runs_in_worker_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同步归一化执行期间事件循环仍可调度，且不在主线程（worker 线程）。"""
        mod = _load_server()
        model = "cost-budget-offloop"
        mod.set_config({"llm": {"models": {model: {"provider": "deepseek"}}}})
        probe: contextvars.ContextVar[str] = contextvars.ContextVar("cost_budget_probe")
        probe.set("bound-in-coroutine")
        seen: dict[str, Any] = {"thread": None, "probe": None}
        returned: list[dict[str, Any]] = [{"role": "user", "content": "normalized-marker"}]
        ticks_at_adapter = {"value": -1}

        def _slow_normalize(
            messages: list[dict[str, Any]],  # noqa: ARG001
            model_entry: dict[str, Any] | None,  # noqa: ARG001
            pipeline_id: str,  # noqa: ARG001
            model_name: str,  # noqa: ARG001
        ) -> list[dict[str, Any]]:
            seen["thread"] = threading.current_thread()
            seen["probe"] = probe.get("missing")
            time.sleep(0.2)  # 模拟 CPU 重活（同步 handler 的既有耗时段）
            return returned

        monkeypatch.setattr(mod, "_normalize_inbound_messages", _slow_normalize)
        counter = [0]

        class _TickAdapter(RecordingAdapter):
            async def completion(self, **kwargs: Any) -> Any:
                ticks_at_adapter["value"] = counter[0]
                return await super().completion(**kwargs)

        adapter = _TickAdapter()
        _install_bus(mod, adapter)

        async def _scenario() -> None:
            done = asyncio.Event()

            async def _ticker() -> None:
                while not done.is_set():
                    counter[0] += 1
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(_ticker())
            try:
                await mod.llm_complete_stream(model=model, messages=[{"role": "user", "content": "q"}])
            finally:
                done.set()
                await task

        try:
            asyncio.run(_scenario())
        finally:
            mod.set_config({})

        assert adapter.calls, "adapter 未被调用"
        # 线程面：归一化不在事件循环所在的主线程执行
        assert seen["thread"] is not None
        assert seen["thread"] is not threading.main_thread(), "归一化仍在事件循环线程执行"
        # contextvars 面：to_thread 复制调用方上下文（日志 _log_ctx 不丢）
        assert seen["probe"] == "bound-in-coroutine"
        # 心跳面：200ms 同步归一化期间事件循环至少调度了 8 次（10ms 节拍）
        assert ticks_at_adapter["value"] >= 8, (
            f"归一化期间事件循环几乎没跑：ticks={ticks_at_adapter['value']}"
        )
        # 返回值面：归一化返回值原样进入 adapter 载荷
        assert adapter.calls[0]["messages"] is returned, "归一化返回值语义被改写"

    def test_normalize_exception_propagates_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """归一化异常原样传播出 complete_stream（不吞、不改类型、不转业务返回）。"""
        mod = _load_server()
        model = "cost-budget-exc"
        mod.set_config({"llm": {"models": {model: {"provider": "deepseek"}}}})

        class _Boom(RuntimeError):
            pass

        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise _Boom("normalize exploded")

        monkeypatch.setattr(mod, "_normalize_inbound_messages", _boom)
        adapter = RecordingAdapter()
        try:
            with pytest.raises(_Boom, match="normalize exploded"):
                _run_through_server(mod, [{"role": "user", "content": "q"}], model=model)
        finally:
            mod.set_config({})
        assert adapter.calls == [], "异常后不应调用 adapter"


class TestDegenerateCollapseBehaviour:
    """折叠行为：正常正文不动 / 退化重复折叠（两组有区分度输入）。"""

    def test_normal_prose_untouched(self) -> None:
        """正常长正文不动（返回同一对象，不复制、不误折）。"""
        text = "".join(
            f"第 {i} 步：检查 plugin.json 的 output_schema 与 render 声明，确认 tool_ids 口径。\n"
            for i in range(80)
        )
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        assert mn.collapse_degenerate_history(messages, name="t") == 0
        assert messages[0]["content"] is text

    def test_periodic_repeat_collapsed(self) -> None:
        """同一短句连发 → 折叠为占位（两组周期不同，避免拟合单一周期）。"""
        for unit in (_PERIODIC_UNIT, "步骤复核：日志、取证、回归。\n"):
            messages: list[dict[str, Any]] = [{"role": "assistant", "content": unit * 600}]
            collapsed = mn.collapse_degenerate_history(messages, name="t")

            assert collapsed > 0
            assert "已折叠重复输出" in messages[0]["content"]
            assert len(messages[0]["content"]) < len(unit) * 20

    def test_huge_degenerate_message_collapses_fully(self) -> None:
        """超采样窗的退化消息仍整段折叠（"找"有界不影响"折"的覆盖范围）。"""
        text = _PERIODIC_UNIT * 30_000  # ~1M 字符
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed > len(text) // 2
        assert len(messages[0]["content"]) < 200
        assert "已折叠重复输出" in messages[0]["content"]


class TestSyncBlockingRouteOffEventLoop:
    """同步阻塞的配置域端点同样不得占用事件循环（同类风险：同步 httpx 拉取）。"""

    def test_remote_models_fetch_off_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote-models 的同步 httpx 拉取在 worker 线程执行，事件循环持续调度。"""
        mod = _load_server()
        seen: dict[str, Any] = {"thread": None}
        counter = [0]

        def _slow_fetch(provider_id: str) -> dict[str, Any]:
            seen["thread"] = threading.current_thread()
            time.sleep(0.2)  # 模拟同步 httpx.get(timeout=8s) 的阻塞窗口
            return {"provider": provider_id, "models": []}

        fake_routes = ModuleType("routes_llm_config")
        fake_routes.get_remote_models = _slow_fetch  # type: ignore[attr-defined]
        # 字面量表在分发时整体构建，其余端点需在位（本用例不命中）
        fake_routes.get_provider_types = lambda: {}  # type: ignore[attr-defined]
        fake_routes.get_llm_presets = lambda: {}  # type: ignore[attr-defined]
        fake_routes.get_thinking_levels = lambda model: {"model": model}  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "routes_llm_config", fake_routes)

        async def _scenario() -> None:
            done = asyncio.Event()

            async def _ticker() -> None:
                while not done.is_set():
                    counter[0] += 1
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(_ticker())
            try:
                result = await mod.http_handle(
                    path="/ext/llm_service/config/llm/providers/prov/remote-models",
                    method="GET",
                )
            finally:
                done.set()
                await task
            assert result["data"]["status"] == 200

        asyncio.run(_scenario())

        assert seen["thread"] is not None
        assert seen["thread"] is not threading.main_thread(), "同步拉取仍在事件循环线程执行"
        assert counter[0] >= 8, f"拉取期间事件循环几乎没跑：ticks={counter[0]}"


class TestScanBudget:
    """确定性成本预算：退化历史折叠不再是无界全量扫描。"""

    @staticmethod
    def _incident_history() -> list[dict[str, Any]]:
        """事故形态历史：341 条、约 1.7M 字符，最新两条一近重复大块一真退化。"""
        near_repeat = "".join(f"{_NEAR_REPEAT_LINE}（第{i}遍）\n" for i in range(24_000))
        messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
        messages.extend(
            {"role": "user" if i % 2 else "assistant", "content": "正常正文，逐轮推进。" * 30}
            for i in range(337)
        )
        messages.append({"role": "assistant", "content": near_repeat})
        messages.append({"role": "assistant", "content": _PERIODIC_UNIT * 20_000})
        return messages

    def test_incident_history_scan_is_bounded(self, scan_counter: _ScanCounter) -> None:
        """1.7M 字符历史：喂给复读正则的字符数 ≤ 预算，真退化消息仍被折叠。"""
        messages = self._incident_history()
        total_chars = sum(len(m.get("content") or "") for m in messages)
        assert total_chars > 1_500_000

        started = time.perf_counter()
        collapsed = mn.collapse_degenerate_history(messages, name="t", pipeline_id="p")
        elapsed = time.perf_counter() - started

        assert collapsed > 200_000, "最新真退化消息未被折叠"
        assert "已折叠重复输出" in messages[-1]["content"]
        assert scan_counter.scanned <= mn._DEGENERATE_CALL_SCAN_BUDGET_CHARS
        # 药方是"确定性预算"而非"更快的机器"：给 30s 事故两个数量级余量
        assert elapsed < 5.0, f"折叠耗时 {elapsed:.2f}s，疑似回到无界全量扫描"

    def test_near_repeat_pathology_short_circuits(self, scan_counter: _ScanCounter) -> None:
        """近重复病态正文（两档正则零命中）由探针短路：不进正则、不折叠。"""
        text = "".join(f"{_NEAR_REPEAT_LINE}（第{i}遍）\n" for i in range(60_000))
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        started = time.perf_counter()
        collapsed = mn.collapse_degenerate_history(messages, name="t")
        elapsed = time.perf_counter() - started

        assert collapsed == 0
        assert messages[0]["content"] is text
        assert scan_counter.scanned == 0, "近重复正文不应进入复读正则（平方级回溯源）"
        assert elapsed < 2.0, f"病态正文耗时 {elapsed:.2f}s，探针短路失效"

    def test_budget_exhaustion_prefers_newest(
        self, monkeypatch: pytest.MonkeyPatch, scan_counter: _ScanCounter
    ) -> None:
        """预算耗尽后更早消息不再分析；最新消息优先折叠（确定性顺序）。"""
        monkeypatch.setattr(mn, "_DEGENERATE_CALL_SCAN_BUDGET_CHARS", 4096)
        older = _PERIODIC_UNIT * 300
        newer = _PERIODIC_UNIT * 300
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": older},
            {"role": "assistant", "content": newer},
        ]

        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed > 0
        assert "已折叠重复输出" in messages[1]["content"], "最新消息应优先被折叠"
        assert messages[0]["content"] is older, "预算耗尽后的更早消息应保持原样"
        assert scan_counter.scanned <= 4096

    def test_small_texts_stop_when_budget_short(
        self, monkeypatch: pytest.MonkeyPatch, scan_counter: _ScanCounter
    ) -> None:
        """预算不足以覆盖剩余小文本时停止分析（不做半截扫描，顺序确定）。"""
        monkeypatch.setattr(mn, "_DEGENERATE_CALL_SCAN_BUDGET_CHARS", 4096)
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": ("短周期复读。" * 120)} for _ in range(6)
        ]
        originals = [msg["content"] for msg in messages]

        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed > 0, "最新消息应被折叠"
        assert messages[-1]["content"] != originals[-1]
        assert messages[0]["content"] is originals[0], "预算耗尽后的更早消息应保持原样"
        assert scan_counter.scanned <= 4096


class TestSampledWindowCoverage:
    """采样窗路径的边界：跨窗 run / 窗口内截断 / 短 run 不动 / 长周期块。"""

    def test_run_spanning_windows_merges_once(self, scan_counter: _ScanCounter) -> None:
        """跨多个采样窗的同一条 run 只折叠一次（区间合并，不重复折叠）。"""
        filler = "".join(f"第 {i} 行：进度记录与回归结论。\n" for i in range(150))  # ~3000 字符
        run = "abcd" * 900  # 3600 字符周期复读，横跨中窗与尾窗
        text = filler + run + filler

        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]
        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed >= len(run) - 40
        assert messages[0]["content"].count("已折叠重复输出") == 1
        assert scan_counter.scanned <= mn._DEGENERATE_CALL_SCAN_BUDGET_CHARS

    def test_run_starting_before_window_is_fully_folded(self, scan_counter: _ScanCounter) -> None:
        """run 起点在中窗之前：命中后向前延伸回真实起点（不残留半条 run）。"""
        head = "编排记录：逐轮核对插件声明面与回归证据。\n" * 60  # ~1500 字符
        run = "xy" * 1200  # 2400 字符周期复读（起点落在中窗之前）
        text = head + run + "收尾说明。\n" * 40

        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]
        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed >= len(run) - 40
        assert messages[0]["content"].count("已折叠重复输出") == 1
        assert scan_counter.scanned <= mn._DEGENERATE_CALL_SCAN_BUDGET_CHARS

    def test_short_repeat_run_left_untouched(self) -> None:
        """短于最小折叠长度的复读片段不动（避免误折正常强调）。"""
        tail = "以上为记录，逐项确认后归档，交接说明覆盖验收口径与遗留项。"
        text = "复核结论如下：" + ("ab12" * 30) + tail * 3
        assert len(text) >= mn._DEGENERATE_RUN_MIN_CHARS
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        assert mn.collapse_degenerate_history(messages, name="t") == 0
        assert messages[0]["content"] is text

    def test_high_diversity_text_skipped(self, scan_counter: _ScanCounter) -> None:
        """高多样性长文本被预筛短路：不进正则、不折叠（廉价必要条件）。"""
        # 2500 个不同汉字（确定性构造），采样窗不同字符数远高于 350 门槛
        text = "".join(chr(0x4E00 + (i * 7919) % 3000) for i in range(2500))
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        assert mn.collapse_degenerate_history(messages, name="t") == 0
        assert messages[0]["content"] is text
        assert scan_counter.scanned == 0

    def test_merge_windows_cover_mid_sized_text(self, scan_counter: _ScanCounter) -> None:
        """中长文本（首中尾窗重叠）合并为一窗全量覆盖，退化内容照常折叠。"""
        text = _PERIODIC_UNIT * 50  # 2000 字符：三窗重叠 → 合并成全文一窗
        assert len(text) < mn._DEGENERATE_SCAN_WINDOW_CHARS * 3
        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed > len(text) // 2
        assert scan_counter.scanned == len(text), "重叠窗口应合并为单窗（不重复扫描）"

    def test_window_short_match_is_skipped(self, scan_counter: _ScanCounter) -> None:
        """窗口内命中的短 run（< 200）不折，同文本内的长 run 照常折叠。"""
        short_run = "ab12" * 30  # 120 字符，落在首窗
        filler = "".join(f"第 {i} 行：编排记录与回归结论。\n" for i in range(140))
        long_run = "abcd" * 300  # 1200 字符，落在尾窗
        text = short_run + filler + long_run

        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]
        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed >= len(long_run) - 40
        assert short_run in messages[0]["content"], "短 run 应原样保留"
        assert scan_counter.scanned <= mn._DEGENERATE_CALL_SCAN_BUDGET_CHARS

    def test_long_period_block_is_folded(self, scan_counter: _ScanCounter) -> None:
        """长周期（100-800 块）退化：短周期零命中时由长周期扫描兜住。"""
        # 块内 6 句互不相同（无 ≤64 字短周期），整块连发 4 份 → 只命中长周期档
        block = (
            "第一阶段核对插件声明面的 output_schema 与 render 意图以及工具白名单口径。"
            "第二阶段复核 key_pool 的退避策略、超时边界与并发信号量优先级排队。"
            "第三阶段比对流式事件顺序、keepalive 节拍与断流兜底路径的日志证据。"
            "第四阶段检查历史裁剪、tool_calls 配对与孤儿结果摘除的无状态幂等性。"
            "第五阶段验证配置热注入后的 router 重建与旧 key 失效回收行为。"
            "第六阶段归档本轮回归结论、基线变化与下轮候选，准备交接说明。"
        )
        assert 200 <= len(block) <= 800
        run_start = 2800 - 450  # 恰好落在中窗 [2800-512, 2800+512] 内
        head_parts: list[str] = []
        idx = 0
        while sum(len(p) for p in head_parts) < run_start:
            head_parts.append(f"第 {idx} 行：编排记录与回归结论，附本轮证据与下轮候选。\n")
            idx += 1
        head = "".join(head_parts)[:run_start]
        text = head + block * 4
        tail_parts: list[str] = []
        idx = 0
        while len(text) + sum(len(p) for p in tail_parts) < 5600:
            tail_parts.append(f"收尾 {idx}：交接说明与待办清单，含验收口径与风险条目。\n")
            idx += 1
        text += "".join(tail_parts)
        assert len(text) >= mn._DEGENERATE_BLOCK_MIN_CHARS

        messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]
        collapsed = mn.collapse_degenerate_history(messages, name="t")

        assert collapsed > len(block) * 3, "长周期块未被折叠"
        assert "已折叠重复输出" in messages[0]["content"]
        assert scan_counter.scanned <= mn._DEGENERATE_CALL_SCAN_BUDGET_CHARS
