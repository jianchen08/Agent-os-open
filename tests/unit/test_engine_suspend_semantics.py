"""engine._suspend_and_wait watching_tasks 语义切分回归测试。

BUG-FIX-fix_20260629_suspend_semantics_split:
挂起循环按 watching_tasks 是否为空区分行为，避免静默时 8h 死挂。
- watching_tasks == []：1 轮 600s 无注入即 return False（管道结束 → fail）
- watching_tasks != []：6 轮（60min）周期 _check_children_terminal，覆盖
  正常等子任务终态的场景
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.engine import PipelineEngine
from pipeline.types import StateKeys


def _build_engine() -> PipelineEngine:
    services: dict[str, Any] = {"__test__": True}
    return PipelineEngine(
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        plugin_registry=MagicMock(),
        services=services,
    )


class TestSuspendSemanticsSplit:
    """watching_tasks 空/非空走不同 max_wait_rounds 路径。"""

    @pytest.mark.asyncio
    async def test_empty_watching_tasks_exits_one_round(self):
        """watching_tasks 空 + 600s 内无注入 + 无 children_terminal → 1 轮 break。"""
        engine = _build_engine()
        engine._suspended_state = {
            StateKeys.PIPELINE_ID: "pipe-empty",
            "user_input": "",  # 无新内容，模拟纯静默挂起
        }
        state = {
            StateKeys.PIPELINE_ID: "pipe-empty",
            "submitted_task_ids": [],  # 关键：watching_tasks 为空
        }

        # 把 wait_for 替成立即 TimeoutError，验证只跑 1 轮就退出
        async def _fake_wait_for(coro, timeout):  # noqa: ARG001
            # 关闭未消费的 coroutine 防 warning
            with __import__("contextlib").suppress(Exception):
                coro.close()
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", new=_fake_wait_for):
            result = await engine._suspend_and_wait(state)

        # watching_tasks 空 → 1 轮后 return False，管道结束
        assert result is False, "watching_tasks 空时应 1 轮后退出 (return False)"

    @pytest.mark.asyncio
    async def test_non_empty_watching_tasks_uses_six_rounds(self):
        """watching_tasks 非空 → max_wait_rounds = 6（60min）。"""
        engine = _build_engine()
        engine._suspended_state = {
            StateKeys.PIPELINE_ID: "pipe-watch",
            "user_input": "",
        }
        state = {
            StateKeys.PIPELINE_ID: "pipe-watch",
            "submitted_task_ids": ["child-a"],
        }

        wait_for_call_count = 0

        async def _fake_wait_for(coro, timeout):  # noqa: ARG001
            nonlocal wait_for_call_count
            wait_for_call_count += 1
            with __import__("contextlib").suppress(Exception):
                coro.close()
            raise asyncio.TimeoutError

        # 让 _check_children_terminal 一直返回 False，跑满所有轮
        with patch("asyncio.wait_for", new=_fake_wait_for), \
             patch.object(engine, "_check_children_terminal", return_value=False):
            await engine._suspend_and_wait(state)

        # watching_tasks 非空 → 应跑 6 轮（max_wait_rounds=6）
        assert wait_for_call_count == 6, (
            f"watching_tasks 非空应跑 6 轮 (60min)，实际 {wait_for_call_count}"
        )

    @pytest.mark.asyncio
    async def test_children_terminal_breaks_loop(self):
        """非空 watching_tasks 但 _check_children_terminal=True → 立即唤醒。

        新架构：子任务终态触发的唤醒（退出原因 children_terminal）直接 resume，
        不依赖 user_input 非空（system 通知也能唤醒）。
        """
        engine = _build_engine()
        engine._suspended_state = {
            StateKeys.PIPELINE_ID: "pipe-term",
            "user_input": "",  # 无 user_input，靠 children_terminal 唤醒
        }
        state = {
            StateKeys.PIPELINE_ID: "pipe-term",
            "submitted_task_ids": ["child-a"],
        }

        async def _fake_wait_for(coro, timeout):  # noqa: ARG001
            with __import__("contextlib").suppress(Exception):
                coro.close()
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", new=_fake_wait_for), \
             patch.object(engine, "_check_children_terminal", return_value=True):
            result = await engine._suspend_and_wait(state)

        # children 已终态 → 唤醒并恢复 state，应返回 True
        assert result is True
