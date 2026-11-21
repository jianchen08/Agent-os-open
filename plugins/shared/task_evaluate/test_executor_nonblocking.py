# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""回归：tool 型指标执行不得阻塞合宿宿主事件循环。

2026-11-11 装机事故机制：task_evaluate 的 tool 型指标（bash_check 同步
``subprocess.run`` 最长 600s、scope_consistency 的 git 最长 60s）在 light
合宿宿主的事件循环线程里直接执行；超过 watchdog 30s 阈值即判心跳停滞、
宿主自杀 respawn，同进程共宿的 llm 服务在飞流全部 ``response channel
closed``，管道随后被判 ``llm_fail_loop`` 停摆。修复=执行下放线程池
（``asyncio.to_thread``），事件循环保持可打点。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
if str(_TE_DIR) not in sys.path:
    sys.path.insert(0, str(_TE_DIR))

import _executor  # noqa: E402
from _eval_core import MetricResult  # noqa: E402


def test_tool_local_metric_runs_off_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同步 tool 型执行期间事件循环必须持续推进（心跳不被冻住）。"""
    executor = _executor.PipelineEvaluationExecutor(
        chat_send=lambda _payload: None,
        state_rows=lambda: [],
        metrics_config_path=str(tmp_path / "metrics.yaml"),
    )
    monkeypatch.setattr(executor, "_load_metrics", lambda: {"bash_check": {"evaluator_type": "tool"}})

    def slow_tool_local(metric_id: str, params: dict[str, object]) -> MetricResult:
        time.sleep(0.4)  # 模拟长命令：阻塞实现会冻住事件循环
        return MetricResult(metric_id=metric_id, passed=True)

    monkeypatch.setattr(executor, "_evaluate_tool_local", slow_tool_local)

    async def scenario() -> tuple[MetricResult, int]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            result = await executor._evaluate_metric("task-x", "bash_check", {"command": "true"})
        finally:
            tick_task.cancel()
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result.passed is True
    # 0.4s / 20ms ≈ 20 次；阻塞实现只能拿到 0~1 次。
    assert ticks >= 8, f"事件循环被 tool 型指标阻塞（期间仅 {ticks} 次心跳）"
