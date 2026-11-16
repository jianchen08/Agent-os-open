# @feature: L5 超时后处置语义 | @ci: python-coverage
"""首块/流超时归入既有重试/兜底链 + 兜底全程预算约束（设计 2026-11-05 §5.8/L3）。

装机 2026-11-05 实证（kernel.log：19 次 ``Stream first chunk timeout`` 全部
WireTimeoutError → RuntimeError 裸穿 MCP ``tools/call``，``fallback_notice``
全天 0 次）：首块超时发生在 KeyPool 逐 key 重试循环之外（流对象返回后、消费
期间），冷却/换 key/兜底链对超时错误类全部未接——上游抖动本可恢复，直接变
用户可见失败。

本文件锁定修复后契约：
- 首块超时（WireTimeoutError）= 可重试类：该 key 立即冷却 → 换 key → 兜底链
  （fallback_notice 降级信号在位）；
- 不可重试类（HTTP 结构化 4xx）维持既有快败分支；
- 兜底全程预算约束（L3 公式运行时镜像）：剩余预算不足一跳 → 结构化失败收尾，
  不许逐跳烧预算撞传输界。

防拟合：可重试/不可重试/预算耗尽三组区分输入各有断言；预算断言配性质校验
（预算 ≥ 跳数 × 单跳界）；时钟打桩在模块命名空间（不碰全局 time）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 注意：裸名 sys.modules.pop 在此禁用——router_factory/wire_client/key_pool 是
# llm 独有名（不在 plugins/conftest 逐出名单），收集期 pop+重导入会以第二实例
# 顶掉共享槽位，先收集文件的 monkeypatch 目标与 adapter 消费目标分叉（全量
# 车道串扰实证）。adapter/exceptions 的跨测试串扰由 plugins/conftest 驱动统一
# 逐出，本文件不自行处理。
import adapter as adapter_mod  # noqa: E402
import wire_client  # noqa: E402

from agentos_plugin_sdk.error_classifier import ErrorKind  # noqa: E402


class FakeClock:
    """adapter._time 替身（模块命名空间打桩，不碰全局 time.monotonic）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class RecSlot:
    """KeySlot 记录桩：handle_timeout/handle_error/release/on_success 全记录。"""

    def __init__(self, key_id: str, api_key: str = "sk-test-1234567890") -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = ""
        self._consecutive_down = 0  # SERVICE_DOWN 退避公式读取面
        self.timeouts: list[float] = []
        self.errors: list[Any] = []
        self.succeeded = 0
        self.released = 0

    def handle_timeout(self, window: float) -> None:
        self.timeouts.append(window)

    def handle_error(self, info: Any) -> None:
        self.errors.append(info)

    def on_success(self) -> None:
        self.succeeded += 1

    def release(self) -> None:
        self.released += 1


class SeqPool:
    """acquire_slot 按预排序列逐个给出（耗尽后停发——模拟 select 无可用键）。"""

    def __init__(self, slots: list[RecSlot]) -> None:
        self.slots: list[Any] = slots
        self._seq = list(slots)

    async def acquire_slot(self) -> Any:
        if self._seq:
            return self._seq.pop(0)
        raise asyncio.CancelledError  # 不应走到：预算检查须先行拦截


class _ScriptedStub(adapter_mod.KeyPoolAdapter):
    """_direct_call_with_slot / _direct_call_no_pool 按脚本吐结果/异常。

    脚本项为 (outcome, clock_advance)：outcome 是异常则抛出并拨钟（模拟整窗
    黑洞耗时），否则原样返回。
    """

    def __init__(self, script: list[Any], clock: FakeClock | None = None) -> None:
        super().__init__(router=None)
        self._script = list(script)
        self._clock = clock
        self.no_pool_called: list[str] = []

    async def _direct_call_with_slot(self, slot: Any = None, **kwargs: Any) -> Any:
        outcome = self._script.pop(0)
        advance = 0.0
        if isinstance(outcome, tuple):
            outcome, advance = outcome
        if self._clock is not None and advance:
            self._clock.advance(advance)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _direct_call_no_pool(self, model_id: str, **kwargs: Any) -> Any:
        self.no_pool_called.append(model_id)
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok_stream() -> Any:
    """带预读首块的流桩（成功形态，供成功脚本返回）。"""
    from types import SimpleNamespace

    stream = SimpleNamespace(choices=[], usage=None)
    return stream


def _patch_rf(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pool: Any,
    provider: str = "cpa_gateway",
    fallbacks: list[str],
) -> None:
    """router_factory 查表桩：池/provider/兜底链。"""
    import router_factory as rf_mod

    monkeypatch.setattr(rf_mod, "get_key_pool", lambda _p: pool)
    monkeypatch.setattr(rf_mod, "get_provider_for_model", lambda _m: provider)
    monkeypatch.setattr(rf_mod, "get_fallback_models", lambda _m: list(fallbacks))
    monkeypatch.setattr(rf_mod, "get_plugin_provider_route", lambda _m: None)


# ─────────────────── 可重试类：首块超时 → 冷却 → 换 key → 兜底链 ───────────────────


class TestFirstChunkTimeoutRetryable:
    """WireTimeoutError 归入可重试类：既有链路（冷却/换 key/兜底）全接。"""

    @pytest.mark.asyncio
    async def test_first_chunk_timeout_cools_key_and_rotates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一组区分输入：首块超时 → 该 key 立即冷却 → 换 key 成功。

        修复前契约缺口：超时发生在循环外（流消费期），key 不冷却不换，异常裸穿。
        """
        s1, s2 = RecSlot("k1"), RecSlot("k2")
        pool = SeqPool([s1, s2])
        _patch_rf(monkeypatch, pool=pool, fallbacks=[])
        adapter = _ScriptedStub(
            script=[wire_client.WireTimeoutError("Stream first chunk timeout: 180s"), _ok_stream()]
        )
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None  # 换 key 后成功返回
        assert s1.timeouts == [180.0]  # 整窗黑洞 → 冷却窗=单跳界
        assert s1.errors == []  # 超时不走 handle_error 文本猜测路径
        assert s2.succeeded == 1  # 换 key 生效
        assert s1.released >= 1
        assert s2.released >= 1

    @pytest.mark.asyncio
    async def test_httpx_read_timeout_gets_same_disposition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx 层读超时（传输半边漏出）与 WireTimeoutError 同处置：冷却换 key。

        分类登记单点在循环分支：两类超时证据等价（整窗无响应），不得因异常
        包装层不同而分叉成 UNKNOWN 无处置漂移。
        """
        import httpx

        s1, s2 = RecSlot("k1"), RecSlot("k2")
        pool = SeqPool([s1, s2])
        _patch_rf(monkeypatch, pool=pool, fallbacks=[])
        adapter = _ScriptedStub(script=[httpx.ReadTimeout("timed out"), _ok_stream()])
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert s1.timeouts == [180.0]
        assert s2.succeeded == 1

    @pytest.mark.asyncio
    async def test_all_keys_timeout_then_fallback_chain_notices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全部 key 超时 → 兜底链接管到备用模型，fallback_notice 降级信号在位。

        装机实证反面：fallback_notice 全天 0 次——兜底链对超时类未接。修复后
        key 超时耗尽即落兜底链（自路由首候选=主模型自身，再切备用模型），notice
        挂返回值，消费方翻译用户可见信号。
        """
        s1 = RecSlot("k1")
        pool = SeqPool([s1])
        _patch_rf(monkeypatch, pool=pool, fallbacks=["glm-5.3-flash"])
        adapter = _ScriptedStub(
            script=[
                wire_client.WireTimeoutError("first chunk timeout"),  # key 槽整窗黑洞
                wire_client.WireTimeoutError("first chunk timeout"),  # 链候选1：主模型
                _ok_stream(),  # 链候选2：备用模型成功
            ]
        )
        out = await adapter._do_completion(
            model="minimax-m3.1", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        # 兜底链逐候选展开：主模型自身 → 备用模型（超时同样接住逐跳下移）
        assert adapter.no_pool_called == ["minimax-m3.1", "glm-5.3-flash"]
        notice = getattr(out, "keypool_fallback_notice", None)
        assert notice is not None
        assert notice["model"] == "minimax-m3.1"
        assert "已自动切换备用" in notice["message"]

    @pytest.mark.asyncio
    async def test_connect_refused_gets_service_down_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """建连被拒（不可归因文本族）→ SERVICE_DOWN 结构化分类（不再 UNKNOWN 无状态）。

        既有 SERVICE_DOWN 机制承载（退避 + 连败冷却），拒绝类别至少产生可归因
        ErrorInfo 而非文本嗅探漂移。
        """
        import httpx

        recorded_sleeps: list[float] = []

        async def _instant_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
            recorded_sleeps.append(delay)

        monkeypatch.setattr(adapter_mod.asyncio, "sleep", _instant_sleep)
        s1 = RecSlot("k1")
        # 单 key 泳道拒连的恢复已收进池内加试位（TestServiceDownFastRetry），
        # 池桩需可复用同槽（SeqPool 单发形态会打空第二次 acquire）
        pool = _SingleSlotPool(s1)
        _patch_rf(monkeypatch, pool=pool, fallbacks=[])
        adapter = _ScriptedStub(script=[httpx.ConnectError("Connection refused"), _ok_stream()])
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert s1.errors, "SERVICE_DOWN 分类必须进 handle_error（退避/连败冷却既有机制）"
        assert s1.errors[0].kind == ErrorKind.SERVICE_DOWN
        assert recorded_sleeps  # 退避重试既有语义在位


# ─────────────────── 兜底全程预算约束（L3 运行时镜像） ───────────────────


class TestFallbackBudgetConstraint:
    """跳数 × 单跳界 ≤ 声明预算；剩余预算不足一跳 → 结构化失败收尾。"""

    def test_budget_formula_matches_l3_shape(self) -> None:
        """预算公式 = 跳数 × 单跳界 + 跳数 × B5(60) + 余量(60)（与静态门禁同形）。"""
        budget = adapter_mod._declare_fallback_budget(hops=2, per_hop=180.0)
        assert budget == 2 * 180.0 + 2 * 60.0 + 60.0
        # 性质断言：预算必须盖住全部跳的单跳界之和（防「逐跳烧预算撞传输界」）
        assert budget >= 2 * 180.0

    @pytest.mark.asyncio
    async def test_budget_exhaustion_structured_before_next_hop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第三组区分输入：整窗烧完剩余预算不足 → 结构化失败，不启动下一跳。

        时钟脚本：预算 = 3 跳（1 slot + 1 自路由首候选 + 1 备用）× 300 + 3 × 60
        + 60 = 1140s。单 key 整窗超时拨钟 900s → 兜底链检查点剩余 240s < 单跳界
        300s → 预算收尾，兜底候选不得被尝试（防逐跳烧预算撞传输界）。
        """
        clock = FakeClock()
        monkeypatch.setattr(adapter_mod, "_time", clock)
        s1 = RecSlot("k1")
        pool = SeqPool([s1])
        _patch_rf(monkeypatch, pool=pool, fallbacks=["glm-5.3-flash"])
        adapter = _ScriptedStub(
            script=[(wire_client.WireTimeoutError("first chunk timeout"), 900.0)],
            clock=clock,
        )
        with pytest.raises(adapter_mod._PluginProviderFailure) as exc_info:
            await adapter._do_completion(
                model="minimax-m3.1", messages=[], stream=True, timeout=300.0
            )
        failure = exc_info.value
        assert failure.code == "service_down"
        assert "预算" in str(failure)
        assert adapter.no_pool_called == []  # 剩余预算不足一跳：兜底候选不启动（结构化收尾）

    @pytest.mark.asyncio
    async def test_budget_sufficient_still_walks_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预算充足（未超支）→ 兜底链照常走（预算约束不误伤正常兜底）。"""
        clock = FakeClock()
        monkeypatch.setattr(adapter_mod, "_time", clock)
        s1 = RecSlot("k1")
        pool = SeqPool([s1])
        _patch_rf(monkeypatch, pool=pool, fallbacks=["glm-5.3-flash"])
        adapter = _ScriptedStub(
            script=[(wire_client.WireTimeoutError("first chunk timeout"), 240.0), _ok_stream()],
            clock=clock,
        )
        out = await adapter._do_completion(
            model="minimax-m3.1", messages=[], stream=True, timeout=300.0
        )
        assert out is not None
        assert adapter.no_pool_called == ["minimax-m3.1"]  # 首候选=主模型自身（预算内放行）

    @pytest.mark.asyncio
    async def test_establish_budget_widens_with_hops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """establish 伞预算 = 跳数 × 首块窗 + 跳数 × B5 + 余量（单窗伞会饿死重试）。

        修复前：外层伞 = 单窗（180s），首跳超时吃光全部预算，后续 key/兜底
        永远轮不到——换 key 语义在伞层被静默截断。
        """
        clock = FakeClock()
        monkeypatch.setattr(adapter_mod, "_time", clock)
        s1, s2 = RecSlot("k1"), RecSlot("k2")
        pool = SeqPool([s1, s2])
        _patch_rf(monkeypatch, pool=pool, fallbacks=["glm-5.3-flash"])

        from types import SimpleNamespace

        adapter = _ScriptedStub(script=[])
        resp = SimpleNamespace(choices=[], usage=None)
        first = SimpleNamespace(delta=SimpleNamespace(content="x"))
        captured: dict[str, Any] = {}

        async def fake_open(model: str, call_kwargs: dict[str, Any]) -> tuple[Any, Any]:
            captured["call_kwargs"] = call_kwargs
            return resp, first

        real_await = adapter_mod._await_with_escape

        async def spy_await(coro: Any, timeout: float, *, what: str, abort_event: Any = None) -> Any:
            captured["umbrella"] = timeout
            captured["what"] = what
            return await real_await(coro, timeout, what=what, abort_event=abort_event)

        monkeypatch.setattr(adapter, "_open_and_first_chunk", fake_open)
        monkeypatch.setattr(adapter_mod, "_await_with_escape", spy_await)

        await adapter._establish_first_chunk(
            "minimax-m3.1", {"model": "minimax-m3.1", "timeout": 180.0}, 180.0
        )
        # 跳数 = 2 slot + 1 主 + 1 兜底 = 4 → 4×180 + 4×60 + 60 = 1020
        assert captured["umbrella"] == 4 * 180.0 + 4 * 60.0 + 60.0
        assert "_budget_deadline" in captured["call_kwargs"]
        assert captured["call_kwargs"]["_budget_deadline"] == pytest.approx(
            clock() + captured["umbrella"]
        )


# ────────── SERVICE_DOWN 加试位 + 拨号拒绝族快退避（2026-11-06） ──────────


class _SingleSlotPool:
    """单槽池桩：未冷却恒发同槽（生产 select 对未冷却槽即此语义），冷却停发。"""

    def __init__(self, slot: Any) -> None:
        self.slots: list[Any] = [slot]
        self._slot = slot

    async def acquire_slot(self) -> Any:
        if bool(getattr(self._slot, "is_cooling", False)):
            from exceptions import KeyPoolExhaustedError

            raise KeyPoolExhaustedError("p", 1.0, [self._slot.key_id])
        return self._slot


class TestServiceDownFastRetry:
    """单 key 泳道 SERVICE_DOWN 加试 + 拨号拒绝族快退避（装机 11-06 实证驱动）。

    修复前：max_retries = len(pool.slots)，单 key 泳道 = 1——「退避 2.0s 重试
    (attempt 1/1)」名不副实，池内零重试；唯一第二发来自 Router fallback 的
    no_pool 重发且隔 2s 指数退避。CPA 上游泳道拨号拒绝全天 attempt 1/1 直接
    判死（装机 kernel.log 17:03-17:26 实证），拒连毫秒级确死，等 2s 不换概率。
    """

    @staticmethod
    def _refused_500() -> Any:
        """生产形态桩：CPA 活着、其上游泳道拨号拒绝 → wire 500 内嵌拒连正文。"""
        return wire_client.WireHTTPError(
            500,
            '{"error":{"message":"Post \\"http://127.0.0.1:8080/v1/chat/completions\\": '
            'dial tcp 127.0.0.1:8080: connectex: No connection could be made because '
            'the target machine actively refused it.","code":"internal_server_error"}}',
        )

    @staticmethod
    def _instant_sleep_stub(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []

        async def _instant_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
            recorded.append(delay)

        monkeypatch.setattr(adapter_mod.asyncio, "sleep", _instant_sleep)
        return recorded

    @pytest.mark.asyncio
    async def test_single_key_refused_recovers_inside_pool_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拒连 → 池内加试第二发成功（不经 Router fallback 重发）。"""
        recorded = self._instant_sleep_stub(monkeypatch)
        s1 = RecSlot("k1")
        _patch_rf(monkeypatch, pool=_SingleSlotPool(s1), fallbacks=[])
        adapter = _ScriptedStub(script=[self._refused_500(), _ok_stream()])
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert adapter.no_pool_called == [], "恢复必须发生在池循环内（加试位），非 Router 重发"
        assert recorded, "拒连族退避必须发生"
        assert max(recorded) < 1.0, "拒连族毫秒级确死，退避必须亚秒级"
        assert s1.errors, "SERVICE_DOWN 分类必须进 handle_error"
        assert s1.errors[0].kind == ErrorKind.SERVICE_DOWN

    @pytest.mark.asyncio
    async def test_non_refused_service_down_keeps_exponential_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """区分输入：非拒连的 wire 500（上游 400 包 500，11102 正文形态）
        → 不进快车道，指数退避首档 2.0s；加试位同样可用（池内第二发成功）。"""
        recorded = self._instant_sleep_stub(monkeypatch)
        s1 = RecSlot("k1")
        _patch_rf(monkeypatch, pool=_SingleSlotPool(s1), fallbacks=[])
        upstream_400 = wire_client.WireHTTPError(
            500,
            '{"error":{"message":"upstream 400: {\\"code\\":11102,\\"msg\\":\\"model '
            '[MiniMax-M3.1-Flash] service info not found\\"}"}}',
        )
        adapter = _ScriptedStub(script=[upstream_400, _ok_stream()])
        out = await adapter._do_completion(
            model="minimax-m3.1", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert adapter.no_pool_called == []
        assert recorded, "退避必须发生"
        assert recorded[0] >= 2.0, "非拒连 SERVICE_DOWN 沿用指数退避首档"

    @pytest.mark.asyncio
    async def test_extra_attempts_stop_when_real_slot_cools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真冷却机形态：连续两次拒连 → 槽位冷却 → 第三发止步，兜底重发接住。"""
        recorded = self._instant_sleep_stub(monkeypatch)

        from key_pool import KeySlot  # noqa: PLC0415 函数内惰性导入（共跑次序纪律）

        slot = KeySlot(key_id="k1", api_key="sk-test-1234567890")
        _patch_rf(monkeypatch, pool=_SingleSlotPool(slot), fallbacks=[])
        adapter = _ScriptedStub(
            script=[self._refused_500(), self._refused_500(), _ok_stream()]
        )
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert adapter.no_pool_called == ["glm-5.3-flash"], "第三发必须止步于池内、由兜底重发接住"
        assert len(recorded) == 2, "池内恰好两次拒连退避，槽位冷却后不再空转"
        assert all(d < 1.0 for d in recorded)

    @pytest.mark.asyncio
    async def test_non_service_down_does_not_consume_extra_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """加试位排他性：认证失败类不得占用加试预算——池内一次即止，
        第二发只能经 Router fallback 重发（no_pool），而非池内加试。"""
        self._instant_sleep_stub(monkeypatch)
        s1 = RecSlot("k1")
        _patch_rf(monkeypatch, pool=_SingleSlotPool(s1), fallbacks=[])
        adapter = _ScriptedStub(
            script=[wire_client.WireHTTPError(401, "bad key"), _ok_stream()]
        )
        out = await adapter._do_completion(
            model="glm-5.3-flash", messages=[], stream=True, timeout=180.0
        )
        assert out is not None
        assert adapter.no_pool_called == ["glm-5.3-flash"], "认证失败快败，池内零加试"
        assert s1.errors, "AUTH_FAILED 分类必须进 handle_error"
        assert s1.errors[0].kind == ErrorKind.AUTH_FAILED
