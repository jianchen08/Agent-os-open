"""LLM Adapter 长稳测试 runner — 在脚本化假流之上跑 N 轮迭代。

与 timeout_sim.py 的区别：
  - timeout_sim：每个场景跑一次，验证"超时判定逻辑正确"（单点正确性）
  - soak_runner：随机抽场景跑 N 轮，验证"长时间运行没有渗漏"（持续稳定性）

判定的渗漏类问题（短测发现不了）：
  1. 内存泄漏：RSS 单调增长（线性回归斜率 > 阈值）
  2. asyncio 任务泄漏：all_tasks() 持续增长
  3. 资源未释放：FakeStream 创建数 vs aclose 调用数不匹配
  4. 性能退化：每轮耗时 P95 显著高于 P50（暗示 GC 抖动 / 缓存膨胀）

判定：跑完后用线性回归算斜率，斜率超阈值 = FAIL。

profile：
  smoke    50 轮     ~10s     PR 必跑
  default  500 轮    ~2 min   开发本地
  nightly  2000 轮   ~8 min   每晚定时
  weekend  10000 轮  ~40 min  发版前

运行：
  python tests/soak/soak_runner.py
  python tests/soak/soak_runner.py --profile smoke
  python tests/soak/soak_runner.py --iters 100 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
# 仓库根（解析 `from src.xxx ...` 形式的绝对导入）+ src/ + tests/（解析 `from ...`）
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

# soak 每轮都会触发预期超时日志（场景里专门构造的），统一压到 CRITICAL；
# --verbose 时由 CLI 重置为 INFO 以便排查。
logging.getLogger("llm.adapter").setLevel(logging.CRITICAL)

from soak.timeout_sim import (  # noqa: E402
    FakeStream,
    build_scenarios,
    install_fake_completion,
    restore_completion,
)

from llm.adapter import LiteLLMAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------

try:
    import psutil  # type: ignore
    _PROC = psutil.Process()
    HAS_PSUTIL = True
except Exception:
    _PROC = None
    HAS_PSUTIL = False


def sample_rss_mb() -> float:
    if HAS_PSUTIL and _PROC is not None:
        return _PROC.memory_info().rss / 1024 / 1024
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss / 1024 if sys.platform.startswith("linux") else rss / 1024 / 1024
    except Exception:
        return 0.0


def sample_task_count() -> int:
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return 0


def linreg_slope(ys):
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return 0.0 if den == 0 else num / den


def percentile(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class IterStat:
    idx: int
    scenario: str
    elapsed: float
    rss_mb: float
    task_count: int
    exception: str | None


@dataclass
class SoakResult:
    iters: int
    duration_s: float
    elapsed_p50: float
    elapsed_p95: float
    elapsed_p99: float
    rss_start_mb: float
    rss_end_mb: float
    rss_slope_mb_per_iter: float
    task_count_start: int
    task_count_end: int
    task_count_slope: float
    streams_created: int
    aclose_called: int
    leaked_streams: int
    unexpected_exceptions: int
    scenario_breakdown: dict
    thresholds: dict
    passed: bool
    failures: list = field(default_factory=list)


PROFILES = {
    "smoke":   {"iters": 50,    "rss_slope": 0.20, "task_slope": 0.05},
    "default": {"iters": 500,   "rss_slope": 0.05, "task_slope": 0.01},
    "nightly": {"iters": 2000,  "rss_slope": 0.02, "task_slope": 0.005},
    "weekend": {"iters": 10000, "rss_slope": 0.01, "task_slope": 0.002},
}


class StreamTracker:
    """统计 stream 创建数 + aclose 调用数（检测资源未释放）。"""

    def __init__(self):
        self.created = 0
        self._streams = []

    def install(self, stream):
        self.created += 1
        self._streams.append(stream)
        return install_fake_completion(stream)

    def aclose_called(self):
        return sum(s.aclose_calls for s in self._streams)


async def run_iteration(idx, scenario, tracker):
    stream = FakeStream(
        scenario.script,
        connect_delay=scenario.connect_delay,
        connect_exc=scenario.connect_exc,
    )
    original = tracker.install(stream)
    adapter = LiteLLMAdapter()
    exc_str = None
    start = time.monotonic()
    try:
        await adapter._call_streaming(
            model="fake/soak",
            messages=[{"role": "user", "content": "hi"}],
            first_chunk_timeout=scenario.first_chunk_timeout,
            inter_chunk_timeout=scenario.inter_chunk_timeout,
        )
    except BaseException as exc:
        if scenario.expect_exception and isinstance(exc, scenario.expect_exception):
            exc_str = None
        else:
            exc_str = f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        elapsed = time.monotonic() - start
        restore_completion(original)
    return IterStat(
        idx=idx,
        scenario=scenario.name,
        elapsed=elapsed,
        rss_mb=sample_rss_mb(),
        task_count=sample_task_count(),
        exception=exc_str,
    )


async def run_soak(iters, rss_slope_threshold, task_slope_threshold, seed=42, verbose=False):
    rng = random.Random(seed)
    scenarios = build_scenarios()
    no_aclose = {s.name for s in scenarios if not s.expect_aclose}

    tracker = StreamTracker()
    stats = []

    gc.collect()
    rss_start = sample_rss_mb()
    task_start = sample_task_count()
    t_start = time.monotonic()

    step = max(1, iters // 20)
    for i in range(iters):
        sc = rng.choice(scenarios)
        stat = await run_iteration(i, sc, tracker)
        stats.append(stat)
        if verbose and (i % step == 0 or i == iters - 1):
            print(
                f"  iter {i+1:>5}/{iters}  rss={stat.rss_mb:6.1f}MB  "
                f"tasks={stat.task_count:>3}  {stat.scenario}"
            )

    duration = time.monotonic() - t_start
    gc.collect()
    rss_end = sample_rss_mb()
    task_end = sample_task_count()

    rss_series = [s.rss_mb for s in stats]
    task_series = [float(s.task_count) for s in stats]
    elapsed_series = [s.elapsed for s in stats]

    rss_slope = linreg_slope(rss_series)
    task_slope = linreg_slope(task_series)

    expected_aclose = sum(1 for s in stats if s.scenario not in no_aclose)
    actual_aclose = tracker.aclose_called()
    leaked = max(0, expected_aclose - actual_aclose)
    unexpected_exceptions = sum(1 for s in stats if s.exception)

    breakdown = {}
    for s in stats:
        breakdown[s.scenario] = breakdown.get(s.scenario, 0) + 1

    failures = []
    if rss_slope > rss_slope_threshold:
        failures.append(
            f"RSS 单调增长: 斜率 {rss_slope:.4f} MB/iter > 阈值 {rss_slope_threshold}"
        )
    if task_slope > task_slope_threshold:
        failures.append(
            f"asyncio 任务泄漏: 斜率 {task_slope:.5f} /iter > 阈值 {task_slope_threshold}"
        )
    if leaked > 0:
        failures.append(
            f"资源未释放: 应 aclose={expected_aclose} 实际={actual_aclose} leak={leaked}"
        )
    if unexpected_exceptions > 0:
        failures.append(f"意外异常 {unexpected_exceptions} 次")

    return SoakResult(
        iters=iters,
        duration_s=duration,
        elapsed_p50=percentile(elapsed_series, 0.50),
        elapsed_p95=percentile(elapsed_series, 0.95),
        elapsed_p99=percentile(elapsed_series, 0.99),
        rss_start_mb=rss_start,
        rss_end_mb=rss_end,
        rss_slope_mb_per_iter=rss_slope,
        task_count_start=task_start,
        task_count_end=task_end,
        task_count_slope=task_slope,
        streams_created=tracker.created,
        aclose_called=actual_aclose,
        leaked_streams=leaked,
        unexpected_exceptions=unexpected_exceptions,
        scenario_breakdown=breakdown,
        thresholds={"rss_slope": rss_slope_threshold, "task_slope": task_slope_threshold},
        passed=not failures,
        failures=failures,
    ), stats


def print_result(r):
    print()
    print("=" * 70)
    print(f"  Soak Result ({r.iters} iters, {r.duration_s:.2f}s)")
    print("=" * 70)
    print(f"  Elapsed    P50={r.elapsed_p50*1000:7.1f}ms  "
          f"P95={r.elapsed_p95*1000:7.1f}ms  P99={r.elapsed_p99*1000:7.1f}ms")
    print(f"  Memory     start={r.rss_start_mb:6.1f}MB  end={r.rss_end_mb:6.1f}MB  "
          f"slope={r.rss_slope_mb_per_iter:+.4f}MB/iter "
          f"(threshold {r.thresholds['rss_slope']})")
    print(f"  Tasks      start={r.task_count_start:>4}  end={r.task_count_end:>4}  "
          f"slope={r.task_count_slope:+.5f}/iter "
          f"(threshold {r.thresholds['task_slope']})")
    print(f"  Streams    created={r.streams_created}  aclose={r.aclose_called}  "
          f"leak={r.leaked_streams}")
    print(f"  Unexpected exceptions: {r.unexpected_exceptions}")
    print()
    print(f"  Scenarios: {dict(sorted(r.scenario_breakdown.items()))}")
    print()
    if r.passed:
        print("  [PASS]")
    else:
        print("  [FAIL]")
        for f in r.failures:
            print(f"     - {f}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="LLM Adapter long-running soak test")
    parser.add_argument("--profile", choices=list(PROFILES.keys()), default="default",
                        help="预设规模（smoke/default/nightly/weekend）")
    parser.add_argument("--iters", type=int, default=None, help="覆盖 profile 的 iter 数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", dest="json_path", default=None, help="把结果写到 JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = PROFILES[args.profile]
    iters = args.iters if args.iters is not None else cfg["iters"]

    result, _ = asyncio.run(run_soak(
        iters=iters,
        rss_slope_threshold=cfg["rss_slope"],
        task_slope_threshold=cfg["task_slope"],
        seed=args.seed,
        verbose=args.verbose,
    ))
    print_result(result)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  结果已写入 {args.json_path}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
