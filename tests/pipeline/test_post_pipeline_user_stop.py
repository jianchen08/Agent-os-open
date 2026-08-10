"""post-pipeline done-callback 不应把"用户停止生成"判成任务失败。

修复背景：用户点"停止生成"后，引擎走安静退出（_user_stop_requested=True，
state 带 USER_STOP_REQUESTED，无 result 产出）。但引擎退出的 done-callback
_cleanup_after_engine → _check_post_pipeline_state 原本只看 task.result 是否为空：
空就 _fail_after_pipeline_exit → fail_task。这把"用户只是打断输出"误判成"任务失败"，
绕过了 engine.py 的安静分支——因为那条标志只在引擎进程内，done-callback 看不到。

修复后：state 带 user_stop_requested 时，_check_post_pipeline_state 直接 return，
保持任务 running，不 fail、不进终态等待。用户重发即继续。

本测试锁定该契约。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from infrastructure.task_post_pipeline import TaskPostPipelineMixin
from pipeline.types import StateKeys


class _Worker(TaskPostPipelineMixin):
    """最小 mixin 宿主，仅暴露被测方法。"""


def _make_running_task() -> MagicMock:
    """构造一个 status=running、无 result 的任务替身（用户停止后的典型状态）。"""
    task = MagicMock()
    task.status = "running"
    task.result = None  # 用户停止 → 无产出
    return task


def _make_task_service(task: MagicMock) -> MagicMock:
    svc = MagicMock()
    svc.get_task.return_value = task
    svc.fail_task = AsyncMock()
    return svc


class TestPostPipelineUserStop:
    """用户停止生成（state 带 user_stop_requested）时，post-pipeline 不 fail。"""

    async def test_user_stop_state_skips_fail(self) -> None:
        """state 带 user_stop_requested → 不调 fail_task，任务保持 running。"""
        worker = _Worker()
        task = _make_running_task()
        svc = _make_task_service(task)

        state = {
            StateKeys.ENDED: True,
            StateKeys.USER_STOP_REQUESTED: True,
            StateKeys.ITERATION: 3,
        }

        await worker._check_post_pipeline_state(
            task_id="t1",
            task_service=svc,
            pipeline_state=state,
            lifecycle=None,
            workspace="",
            ws_meta={},
            ctx=MagicMock(),
            timer_manager=MagicMock(),
        )

        svc.fail_task.assert_not_called(), (
            "用户停止生成（state 带 user_stop_requested）不应 fail_task——"
            "停止只是打断输出，任务该保持 running，重发即继续"
        )

    async def test_user_stop_task_still_running_after_check(self) -> None:
        """用户停止后任务状态仍是 running（未被改成 failed）。"""
        worker = _Worker()
        task = _make_running_task()
        svc = _make_task_service(task)

        state = {StateKeys.USER_STOP_REQUESTED: True, StateKeys.ENDED: True}

        await worker._check_post_pipeline_state(
            task_id="t2",
            task_service=svc,
            pipeline_state=state,
            lifecycle=None,
            workspace="",
            ws_meta={},
            ctx=MagicMock(),
            timer_manager=MagicMock(),
        )

        # 任务状态未被改写（仍是 running）
        assert task.status == "running", "用户停止后任务应保持 running 状态"

    async def test_no_result_without_user_stop_still_fails(self) -> None:
        """对照组：非用户停止（无标志）且无产出 → 仍 fail_task（真崩溃路径不变）。

        这是护栏：修复不得吞掉真崩溃。没有 user_stop_requested 标志时，
        无产出的引擎退出仍应 fail_task（原行为）。
        """
        worker = _Worker()
        task = _make_running_task()
        svc = _make_task_service(task)

        # 无 user_stop_requested 标志 —— 模拟真崩溃/真异常退出
        state = {StateKeys.ENDED: True, StateKeys.RAW_ERROR: "some real error"}

        await worker._check_post_pipeline_state(
            task_id="t3",
            task_service=svc,
            pipeline_state=state,
            lifecycle=None,
            workspace="",
            ws_meta={},
            ctx=MagicMock(),
            timer_manager=MagicMock(),
        )

        svc.fail_task.assert_called_once(), (
            "非用户停止的无产出退出必须 fail_task——修复不得吞掉真崩溃路径"
        )

    async def test_user_stop_with_result_does_not_fail_either(self) -> None:
        """边界：用户停止但恰好有产出 → 同样不 fail（停止不该改变结果判定）。"""
        worker = _Worker()
        task = _make_running_task()
        task.result = "some output"  # 有产出
        svc = _make_task_service(task)

        state = {StateKeys.USER_STOP_REQUESTED: True, StateKeys.ENDED: True}

        # 注意：有 result 时正常路径会走 _transition_to_evaluating（转评估），
        # 但用户停止语义下我们直接 return，既不 fail 也不转评估——
        # 因为停止意味着用户想介入，不该自动推进。
        await worker._check_post_pipeline_state(
            task_id="t4",
            task_service=svc,
            pipeline_state=state,
            lifecycle=None,
            workspace="",
            ws_meta={},
            ctx=MagicMock(),
            timer_manager=MagicMock(),
        )

        svc.fail_task.assert_not_called(), (
            "用户停止时即使有产出也不该 fail——停止语义优先于结果判定"
        )
