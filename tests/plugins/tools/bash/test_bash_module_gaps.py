# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""bash 工具内部模块缺口补测（manifest 靶单：progress_reporter / bash_types /
input_handler / encoding 四个模块的未覆盖行）。

覆盖：
1. progress_reporter.ProgressReporter.bytes_total：UTF-8 字节累计（CJK 多字节
   文本字节数 ≠ 字符数）、跨 report 单调累加；
2. ProgressReporter.close 幂等：二次 close 不重复冲刷、不再取时钟（早退）；
3. bash_types.ProcessBackend.sample_unit_memory 默认实现返回 None（后端不
   支持单进程采样时的降级契约），对照组覆写实现返回真实值；
4. input_handler.InputHandler.format_input 不加换行分支：add_newline=False
   与文本已以换行结尾两条路径同回原文，且二次格式化幂等（不叠加换行）；
5. encoding.EncodingHandler._try_decode 的 surrogateescape 分支不变量：
   任意无效字节序列均不抛，非 NUL 字节可无损 round-trip。

不可达说明（encoding.py 140-141）：该 ``except UnicodeDecodeError`` 挂在
``data.decode("utf-8", errors="surrogateescape")`` 之后——surrogateescape
错误处理器把每个不可解码字节映射为 U+DC80–U+DCFF，对任意 bytes 输入都成功
返回，永不抛 UnicodeDecodeError（20 万组随机字节实测零抛出）。因此
140-141 是防御性死代码，只能以"解码不变量"测试锁住其周围契约，无法直接
命中断言。
"""

from __future__ import annotations

import pytest
from bash_types import ProcessBackend, WorkUnit
from encoding import EncodingHandler
from input_handler import InputHandler
from progress_reporter import ProgressReporter

pytestmark = pytest.mark.unit


class _FakeClock:
    """可控单调时钟（调用次数可观测——用于断言早退分支不取时间）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reporter(flushed: list, clock: _FakeClock, **kwargs) -> ProgressReporter:
    def _on_flush(delta: str, bytes_total: int) -> None:
        flushed.append((delta, bytes_total))

    defaults = {"min_chars": 4096, "min_interval_s": 60.0, "time_fn": clock}
    defaults.update(kwargs)
    return ProgressReporter(_on_flush, **defaults)


# ═══════════════════════════════════════════════════════════
# progress_reporter.ProgressReporter
# ═══════════════════════════════════════════════════════════


class TestProgressReporterBytesTotal:
    """bytes_total 属性：累计 UTF-8 字节（非字符数）。"""

    def test_cjk_text_counts_utf8_bytes_not_chars(self) -> None:
        clock = _FakeClock()
        reporter = _reporter([], clock)
        text = "中文输出行"  # 5 字符 → 15 字节

        reporter.report(text)

        assert reporter.bytes_total == len(text.encode("utf-8")) == 15
        assert reporter.bytes_total != len(text)

    def test_bytes_total_accumulates_across_reports(self) -> None:
        clock = _FakeClock()
        flushed: list = []
        reporter = _reporter(flushed, clock)

        assert reporter.bytes_total == 0
        reporter.report("abc")  # 3 字节
        first = reporter.bytes_total
        reporter.report("汉字")  # 6 字节
        second = reporter.bytes_total

        assert (first, second) == (3, 9)
        assert second > first  # 单调不减
        # 累计值随 flush 一并推给消费方（事件字节计数与属性同源）
        reporter.close()
        assert flushed[0][1] == 9

    def test_ascii_single_bytes_equal_char_count(self) -> None:
        """对照组：纯 ASCII 下字节数 == 字符数（区分多字节场景）。"""
        clock = _FakeClock()
        reporter = _reporter([], clock)

        reporter.report("hello world")

        assert reporter.bytes_total == 11 == len("hello world")


class TestProgressReporterCloseIdempotent:
    """close 幂等：二次调用不重复冲刷、不取时钟（早退）。"""

    def test_second_close_neither_flushes_nor_reads_clock(self) -> None:
        clock = _FakeClock()
        flushed: list = []
        reporter = _reporter(flushed, clock)
        reporter.report("tail without newline")

        clock_before = clock.calls
        reporter.close()
        after_first = clock.calls
        reporter.close()  # 幂等早退：不抛、不冲刷、不取时钟

        assert after_first == clock_before + 1
        assert clock.calls == after_first
        assert [item[0] for item in flushed] == ["tail without newline"]

    def test_close_with_empty_buffer_is_noop_flush(self) -> None:
        """空缓冲 close：不产生回调（但完成关闭状态）。"""
        clock = _FakeClock()
        flushed: list = []
        reporter = _reporter(flushed, clock)

        reporter.close()
        reporter.close()

        assert flushed == []


# ═══════════════════════════════════════════════════════════
# bash_types.ProcessBackend.sample_unit_memory 默认实现
# ═══════════════════════════════════════════════════════════


class _MinimalBackend(ProcessBackend):
    """只实现抽象方法的最小后端——继承 sample_unit_memory 默认实现。"""

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        unit.metadata["killed"] = True

    async def sample_memory(self) -> float | None:
        return 0.5


class _SamplingBackend(ProcessBackend):
    """覆写单进程采样的后端（对照组：默认值 vs 实现值必须可区分）。"""

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        return None

    async def sample_memory(self) -> float | None:
        return 0.1

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        return unit.pid * 1024  # 真实值：pid 线性映射，非 None


class TestProcessBackendSampleUnitMemory:
    @pytest.mark.asyncio
    async def test_default_returns_none_for_backend_without_support(self) -> None:
        unit = WorkUnit(pid=4321, command="pytest -q")

        result = await _MinimalBackend().sample_unit_memory(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_overriding_backend_returns_measured_rss(self) -> None:
        unit = WorkUnit(pid=4321, command="pytest -q")

        result = await _SamplingBackend().sample_unit_memory(unit)

        assert result == 4321 * 1024
        assert result is not None

    @pytest.mark.asyncio
    async def test_backends_without_support_still_expose_system_sample(self) -> None:
        """同后端上 sample_memory 与 sample_unit_memory 是两个独立决策面。"""
        backend = _MinimalBackend()

        assert await backend.sample_memory() == 0.5
        assert await backend.sample_unit_memory(WorkUnit(pid=1, command="x")) is None
        assert isinstance(backend, ProcessBackend)


# ═══════════════════════════════════════════════════════════
# input_handler.InputHandler.format_input
# ═══════════════════════════════════════════════════════════


class TestFormatInputNoNewlineAdded:
    """format_input 返回原文的两条路径：显式关闭 / 已带换行。"""

    @pytest.mark.parametrize(
        ("text", "add_newline"),
        [
            ("echo hi", False),  # 显式关闭补换行
            ("echo hi\n", True),  # 已以换行结尾，不重复补
            ("多行\n第二行\n", True),  # 多字节 + 已带换行
        ],
    )
    def test_returns_text_unchanged(self, text: str, add_newline: bool) -> None:
        handler = InputHandler()

        assert handler.format_input(text, add_newline) == text

    @pytest.mark.parametrize("text", ["echo hi", "echo hi\n", "ls -la\n"])
    def test_format_is_idempotent(self, text: str) -> None:
        """二次格式化不叠加换行（补换行只发生在缺失时）。"""
        handler = InputHandler()

        once = handler.format_input(text)
        twice = handler.format_input(once)

        assert twice == once
        assert not twice.endswith("\n\n")

    @pytest.mark.parametrize(
        ("text", "add_newline", "expected"),
        [
            ("echo hi", False, "echo hi"),
            ("echo hi\n", True, "echo hi\n"),
            ("pwd", True, "pwd\n"),  # 对照：需要补换行的路径仍照旧
        ],
    )
    def test_process_passes_formatting_result_through(
        self, text: str, add_newline: bool, expected: str
    ) -> None:
        handler = InputHandler()

        ok, error, formatted = handler.process(text, add_newline)

        assert ok is True
        assert error is None
        assert formatted == expected


# ═══════════════════════════════════════════════════════════
# encoding._try_decode surrogateescape 不变量
# ═══════════════════════════════════════════════════════════


class TestDecodeNeverRaisesOnInvalidBytes:
    """任意字节序列解码不抛，且非 NUL 字节可经 surrogateescape 无损回写。

    encoding.py:140-141（surrogateescape 分支内的 ``except UnicodeDecodeError``）
    为不可达防御代码：surrogateescape 处理任意字节输入均成功，故本组用例以
    不变量锁住"解码永不抛"契约（该行本身无法被覆盖）。
    """

    @pytest.mark.parametrize(
        "data",
        [
            b"\xff\xfe",  # 孤立代理字节对（非 UTF-8 任何合法序列）
            b"\xe4\xb8",  # 截断的三字节序列（U+4E2D 缺尾字节）
            b"\xf0\x9f\x98",  # 截断的四字节 emoji 序列
            b"valid \xe4\xb8\xad tail \xff\xfe\x80",  # 有效 UTF-8 混无效字节
            b"\x80\x81\x82\x83",  # 连续高位字节（Windows 代码页区间）
        ],
    )
    def test_decode_output_line_never_raises(self, data: bytes) -> None:
        result = EncodingHandler.decode_output_line(data)

        assert isinstance(result, str)

    @pytest.mark.parametrize(
        "data",
        [
            b"\xff\xfe",  # 2 个坏字节 < max(len*0.15, 3) → 走 surrogateescape
            b"\xe4\xb8",  # 截断序列（2 坏字节）→ 同上
            b"hello world this line is valid " * 3 + b"\xff\xfe\x80",  # 长有效前缀 + 3 坏字节
        ],
    )
    def test_surrogateescape_path_round_trips_losslessly(self, data: bytes) -> None:
        """低坏字节占比时走 surrogateescape（encoding.py:128-139）→ 无损可回溯。"""
        decoded = EncodingHandler.decode_output_line(data)

        restored = decoded.encode("utf-8", errors="surrogateescape")

        assert restored == data

    @pytest.mark.parametrize(
        "data",
        [b"\x80\x81\x82\x83", b"\xff" * 8, b"\xc0\xc1\xf5\xf6\xf7\xf8"],
    )
    def test_high_invalid_ratio_still_decodes_without_raise(self, data: bytes) -> None:
        """坏字节占比过高（≥ max(len*0.15, 3)）→ 拒 surrogateescape 转代码页/
        replace 兜底：仍不抛，语义上允许替换字符（有损是设计口径）。"""
        result = EncodingHandler.decode_output_line(data)

        assert isinstance(result, str)
        # 兜底路径不残留 NUL
        assert "\x00" not in result

    def test_valid_utf8_with_nul_still_cleaned(self) -> None:
        """对照组：完全合法输入的解码/剔除语义不变。"""
        assert EncodingHandler.decode_output_line("中文\x00行".encode("utf-8")) == "中文行"
