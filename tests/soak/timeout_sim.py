"""超时判定模拟器 — 用脚本化的"假 LLM 流"代替真实 litellm 调用。

跑这个脚本就能验证 LLM Adapter 的各类超时判定（首字超时 / chunk 间静默超时 /
正常活跃流不误杀 / 资源回收 / 建连阶段挂死 / 中途断流），不需要真实 API key
或网络。核心做法：

  1. monkeypatch ``litellm.acompletion`` → 返回一个发"脚本化 chunk"的 FakeStream，
     每个 chunk 之间可以注入任意 sleep / 异常。
  2. 直接调用 ``LiteLLMAdapter._call_streaming``，用极小的超时阈值压缩时间。
  3. 通过断言"实际耗时窗口 / 异常类型 / aclose 调用次数"判定 PASS/FAIL。

运行：
    python tests/soak/timeout_sim.py
    python tests/soak/timeout_sim.py --case 3
    python tests/soak/timeout_sim.py --list
    python tests/soak/timeout_sim.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 让脚本能从仓库根目录直接跑（不依赖 pytest / 安装）
_ROOT = Path(__file__).resolve().parents[2]
# 仓库根（解析 `from src.xxx ...` 形式的绝对导入）+ src/（解析 `from llm ...` 等）
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import litellm  # noqa: E402

from llm.adapter import LiteLLMAdapter  # noqa: E402


# ===========================================================================
# 1. 假 LLM 流：脚本化 chunk 发生器
# ===========================================================================

@dataclass
class ScriptedChunk:
    """脚本里的一行：'sleep N 秒后吐这段内容'。

    Attributes:
        delay: chunk 发出前的等待秒数（模拟上游响应延迟 / 静默）
        content: 文本增量；None 表示无
        reasoning: 思考增量；None 表示无
        tool_call: 若设置，作为 delta.tool_calls 发出（dict 含 index/id/name/arguments）
        finish_reason: 若设置，作为 choices[0].finish_reason
        raise_exc: 若设置，发该 chunk 时抛此异常（模拟上游中断）
    """

    delay: float = 0.0
    content: str | None = None
    reasoning: str | None = None
    tool_call: dict[str, Any] | None = None
    finish_reason: str | None = None
    raise_exc: BaseException | None = None


class _FakeFunc:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.index = payload.get("index", 0)
        self.id = payload.get("id")
        self.function = _FakeFunc(
            payload.get("name", ""), payload.get("arguments", "")
        )


class _FakeDelta:
    def __init__(self, chunk: ScriptedChunk) -> None:
        self.content = chunk.content
        self.reasoning_content = chunk.reasoning
        if chunk.tool_call:
            self.tool_calls = [_FakeToolCallDelta(chunk.tool_call)]
        else:
            self.tool_calls = None


class _FakeChoice:
    def __init__(self, chunk: ScriptedChunk) -> None:
        self.delta = _FakeDelta(chunk)
        self.finish_reason = chunk.finish_reason


class _FakeChunkObj:
    def __init__(self, chunk: ScriptedChunk) -> None:
        self.choices = [_FakeChoice(chunk)]
        self.usage = None


class _FakeUnderlying:
    """模拟 litellm CustomStreamWrapper.completion_stream（heartbeat 读 is_closed）。"""

    def __init__(self) -> None:
        self.is_closed = False


class FakeStream:
    """脚本驱动的异步流，模拟 litellm CustomStreamWrapper。

    - chunk 间 ``delay`` 用 asyncio.sleep 真实等待（超时判定的核心）
    - ``aclose_calls``：让上层断言连接被正确关闭
    - ``connect_delay`` / ``connect_exc``：模拟建连阶段的延迟 / 异常
    """

    def __init__(
        self,
        script: list[ScriptedChunk],
        *,
        connect_delay: float = 0.0,
        connect_exc: BaseException | None = None,
    ) -> None:
        self._script = list(script)
        self._idx = 0
        self.aclose_calls = 0
        self.completion_stream = _FakeUnderlying()
        self._connect_delay = connect_delay
        self._connect_exc = connect_exc

    async def _simulate_connect(self) -> None:
        if self._connect_delay > 0:
            await asyncio.sleep(self._connect_delay)
        if self._connect_exc is not None:
            raise self._connect_exc

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> _FakeChunkObj:
        if self._idx >= len(self._script):
            raise StopAsyncIteration
        item = self._script[self._idx]
        self._idx += 1
        if item.delay > 0:
            await asyncio.sleep(item.delay)
        if item.raise_exc is not None:
            raise item.raise_exc
        return _FakeChunkObj(item)

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.completion_stream.is_closed = True


# ===========================================================================
# 2. 假 litellm.acompletion 安装/卸载
# ===========================================================================

def install_fake_completion(stream: FakeStream) -> Any:
    """把 litellm.acompletion 替换为返回指定 FakeStream 的 fake。

    返回原函数，便于场景结束后还原（避免污染下一个场景）。
    """
    original = litellm.acompletion

    async def fake_acompletion(**_kwargs: Any) -> FakeStream:
        await stream._simulate_connect()
        return stream

    litellm.acompletion = fake_acompletion  # type: ignore[assignment]
    return original


def restore_completion(original: Any) -> None:
    litellm.acompletion = original


# ===========================================================================
# 3. 场景定义
# ===========================================================================

@dataclass
class Scenario:
    """一个超时判定场景。

    expect_exception=None 表示场景应正常完成。
    expect_min/max_elapsed 是耗时窗口；既验证不被提前误杀，也验证按时触发。
    """

    name: str
    script: list[ScriptedChunk]
    first_chunk_timeout: float = 5.0
    inter_chunk_timeout: float = 5.0
    connect_delay: float = 0.0
    connect_exc: BaseException | None = None
    expect_exception: type[BaseException] | None = None
    expect_min_elapsed: float = 0.0
    expect_max_elapsed: float = 30.0
    expect_aclose: bool = True
    expect_text: str | None = None
    desc: str = ""


def build_scenarios() -> list[Scenario]:
    return [
        Scenario(
            name="happy_path_streaming",
            desc="正常活跃流，不该触发任何超时",
            script=[
                ScriptedChunk(delay=0.05, reasoning="思考"),
                ScriptedChunk(delay=0.05, reasoning="中..."),
                ScriptedChunk(delay=0.05, content="Hello"),
                ScriptedChunk(delay=0.05, content=" world", finish_reason="stop"),
            ],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=2.0,
            expect_min_elapsed=0.15,
            expect_max_elapsed=2.0,
            expect_text="Hello world",
        ),
        Scenario(
            name="first_chunk_timeout",
            desc="建连成功但首个 chunk 永远不来",
            script=[ScriptedChunk(delay=10.0, content="never")],
            first_chunk_timeout=0.5,
            inter_chunk_timeout=5.0,
            expect_exception=litellm.Timeout,
            expect_min_elapsed=0.4,
            expect_max_elapsed=1.5,
        ),
        Scenario(
            name="connect_phase_hang",
            desc="_do_completion 本身就 hang（建连阶段挂死）",
            script=[ScriptedChunk(delay=0, content="ok")],
            connect_delay=10.0,
            first_chunk_timeout=0.5,
            inter_chunk_timeout=5.0,
            expect_exception=litellm.Timeout,
            expect_min_elapsed=0.4,
            expect_max_elapsed=1.5,
            expect_aclose=False,  # 建连卡死时 FakeStream 还没返回，无法 aclose
        ),
        Scenario(
            name="inter_chunk_idle_timeout",
            desc="首字到了，中途突然死寂",
            script=[
                ScriptedChunk(delay=0.05, reasoning="r1"),
                ScriptedChunk(delay=0.05, reasoning="r2"),
                ScriptedChunk(delay=10.0, reasoning="silenced"),
            ],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=0.6,
            expect_exception=litellm.Timeout,
            expect_min_elapsed=0.6,
            expect_max_elapsed=2.0,
        ),
        Scenario(
            name="active_stream_near_threshold_no_false_kill",
            desc="单次间隔接近 timeout 但不超，正常流不该被误杀（回归 httpx_timeout_too_short）",
            script=[
                ScriptedChunk(delay=0.05, reasoning="r1"),
                ScriptedChunk(delay=0.5, reasoning="r2"),
                ScriptedChunk(delay=0.5, reasoning="r3"),
                ScriptedChunk(delay=0.5, content="final", finish_reason="stop"),
            ],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=0.8,
            expect_min_elapsed=1.4,
            expect_max_elapsed=2.5,
            expect_text="final",
        ),
        Scenario(
            name="midstream_exception_releases_resource",
            desc="上游中途断流抛异常，资源必须被 aclose",
            script=[
                ScriptedChunk(delay=0.05, content="part1"),
                ScriptedChunk(
                    delay=0.05,
                    raise_exc=ConnectionResetError("upstream RST"),
                ),
            ],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=2.0,
            expect_exception=ConnectionResetError,
            expect_min_elapsed=0.05,
            expect_max_elapsed=1.0,
        ),
        Scenario(
            name="tool_call_with_long_silence_before_close",
            desc="reasoning 结束后紧接 tool_call，间隙接近 timeout 但被 chunk 重置计时",
            script=[
                ScriptedChunk(delay=0.05, reasoning="thinking..."),
                ScriptedChunk(delay=0.5, reasoning="more thinking..."),
                ScriptedChunk(
                    delay=0.5,
                    tool_call={"index": 0, "id": "tc1", "name": "file_write",
                               "arguments": '{"path":"a.txt"}'},
                    finish_reason="tool_calls",
                ),
            ],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=0.8,
            expect_min_elapsed=0.9,
            expect_max_elapsed=2.5,
        ),
        Scenario(
            name="empty_stream",
            desc="空流：建连成功但立刻 StopAsyncIteration，adapter 按首 token 失败抛 Timeout",
            script=[],
            first_chunk_timeout=2.0,
            inter_chunk_timeout=2.0,
            # adapter.py:453-464 明确设计：空流（首字节即 EOF）按首 token 失败处理，
            # 抛 litellm.Timeout。原 expect_exception=None 是错误期望，与 adapter 契约相悖。
            expect_exception=litellm.Timeout,
            expect_min_elapsed=0.0,
            expect_max_elapsed=0.5,  # 空流立即抛，不等满 timeout
            expect_aclose=False,
        ),
    ]


# ===========================================================================
# 4. Runner
# ===========================================================================

@dataclass
class Result:
    name: str
    passed: bool
    elapsed: float
    actual_exc: str | None
    aclose_calls: int
    reason: str  # 失败原因；PASS 为空


async def run_one(sc: Scenario) -> Result:
    stream = FakeStream(
        sc.script,
        connect_delay=sc.connect_delay,
        connect_exc=sc.connect_exc,
    )
    original = install_fake_completion(stream)
    adapter = LiteLLMAdapter()

    actual_exc: BaseException | None = None
    text: str | None = None
    start = time.monotonic()
    try:
        resp = await adapter._call_streaming(
            model="fake/scripted",
            messages=[{"role": "user", "content": "hi"}],
            first_chunk_timeout=sc.first_chunk_timeout,
            inter_chunk_timeout=sc.inter_chunk_timeout,
        )
        text = resp.text
    except BaseException as exc:  # noqa: BLE001
        actual_exc = exc
    finally:
        elapsed = time.monotonic() - start
        restore_completion(original)

    # ---- 判定 -----------------------------------------------------------
    reasons: list[str] = []

    # 异常类型
    if sc.expect_exception is None:
        if actual_exc is not None:
            reasons.append(
                f"不应抛异常但抛了 {type(actual_exc).__name__}: {actual_exc}"
            )
    else:
        if actual_exc is None:
            reasons.append(f"应抛 {sc.expect_exception.__name__} 但正常完成")
        elif not isinstance(actual_exc, sc.expect_exception):
            reasons.append(
                f"应抛 {sc.expect_exception.__name__} 但抛了 "
                f"{type(actual_exc).__name__}"
            )

    # 耗时窗口
    if elapsed < sc.expect_min_elapsed:
        reasons.append(
            f"耗时 {elapsed:.2f}s < min {sc.expect_min_elapsed:.2f}s（可能被提前误杀）"
        )
    if elapsed > sc.expect_max_elapsed:
        reasons.append(
            f"耗时 {elapsed:.2f}s > max {sc.expect_max_elapsed:.2f}s（超时未及时触发）"
        )

    # aclose 调用（资源回收）
    if sc.expect_aclose and stream.aclose_calls == 0:
        reasons.append("应调用 stream.aclose 释放连接，但未调用")

    # text 内容
    if sc.expect_text is not None and text != sc.expect_text:
        reasons.append(f"text 期望 {sc.expect_text!r} 实际 {text!r}")

    return Result(
        name=sc.name,
        passed=not reasons,
        elapsed=elapsed,
        actual_exc=(
            f"{type(actual_exc).__name__}" if actual_exc is not None else None
        ),
        aclose_calls=stream.aclose_calls,
        reason="; ".join(reasons),
    )


async def run_all(selected: list[int] | None) -> list[Result]:
    scenarios = build_scenarios()
    if selected:
        scenarios = [scenarios[i - 1] for i in selected if 1 <= i <= len(scenarios)]
    results: list[Result] = []
    for sc in scenarios:
        r = await run_one(sc)
        results.append(r)
        flag = "PASS" if r.passed else "FAIL"
        exc_str = f" exc={r.actual_exc}" if r.actual_exc else ""
        print(
            f"[{flag}] {sc.name:<48} "
            f"elapsed={r.elapsed:5.2f}s aclose={r.aclose_calls}{exc_str}"
        )
        if not r.passed:
            print(f"       └── {r.reason}")
    return results


# ===========================================================================
# 5. CLI
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="LLM Adapter 超时判定模拟器")
    parser.add_argument(
        "--case", type=int, action="append", default=None,
        help="只跑指定编号的场景（可重复，如 --case 1 --case 3）",
    )
    parser.add_argument("--list", action="store_true", help="列出全部场景后退出")
    parser.add_argument("--verbose", action="store_true", help="打开 adapter 内部 DEBUG 日志")
    args = parser.parse_args()

    if args.list:
        for i, sc in enumerate(build_scenarios(), 1):
            print(f"  {i}. {sc.name}")
            print(f"       {sc.desc}")
        return 0

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    results = asyncio.run(run_all(args.case))

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print()
    print(f"== {passed}/{total} passed ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
