# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""HumanInteractionService 超时竞态单元测试（F-HI-1）。

意图：审批闭环中 human_interaction 的超时必须"一次且及时"——
- 短超时（timeout_seconds < remind_before_seconds）时，wait_for_choice 必须按时
  抛 InteractionTimeoutError，不能被后台任务的 +300s 提醒链路悬挂/带偏；
- 超时通知（notify_timeout）必须幂等：wait_for_choice 与后台 timeout_handler
  同一时刻竞争触发时只发一次，不重复打扰用户；
- 用户 submit/cancel 之后，后台超时任务必须被取消，绝不能再触发通知；
- 长超时 + 提醒（remind）路径保持原语义，不受短超时修复影响。

为让测试可快速运行，所有用例用小的 remind_before_seconds（1 秒）代替生产默认 300
秒，只改变时间尺度、不改变控制流。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 时间尺度参数（生产 remind=300s；测试缩到 1s）
_REMIND = 1.0
_SHORT_TIMEOUT = 0.3


class _RecorderNotifier:
    """记录调用的假通知器——只记录调用，不依赖真实前端。"""

    def __init__(self) -> None:
        self.timeout_calls: list[tuple[str, str]] = []      # (request_id, thread_id)
        self.reminder_calls: list[tuple[str, int]] = []     # (request_id, remaining)
        self.cancel_calls: list[tuple[str, str | None]] = []  # (request_id, reason)

    async def notify_request(self, request: Any) -> bool:
        return True

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        self.cancel_calls.append((request_id, reason))
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        self.timeout_calls.append((request_id, thread_id))
        return True

    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        self.reminder_calls.append((request_id, remaining_seconds))
        return True

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        return True


@pytest.fixture
async def human_factory():
    """构造服务实例，并在测试结束时取消残留的后台超时任务。

    修复前 wait_for_choice 超时不会取消后台任务，任务会悬挂到 +remind 秒；
    若不在 teardown 取消，pytest-asyncio 关 loop 时会报
    "Task was destroyed but it is pending" 噪音并拖慢测试。
    """
    services: list[Any] = []

    def _make(**kwargs: Any) -> tuple[Any, _RecorderNotifier]:
        notifier = _RecorderNotifier()
        svc = _make_service(notifier=notifier, **kwargs)
        services.append(svc)
        return svc, notifier

    yield _make

    for svc in services:
        for task in list(svc._timeout_tasks.values()):
            task.cancel()
    await asyncio.sleep(0)


def _make_service(**kwargs: Any) -> Any:
    """延迟导入 service（依赖 sys.path 注入，见 conftest）。"""
    from service import HumanInteractionService

    return HumanInteractionService(**kwargs)


def _timeout_calls(notifier: _RecorderNotifier, request_id: str) -> int:
    return sum(1 for rid, _ in notifier.timeout_calls if rid == request_id)


async def _wait_times_out(svc: Any, request_id: str, timeout: float) -> float:
    """调用 wait_for_choice 并断言抛 InteractionTimeoutError，返回耗时。"""
    from service import InteractionTimeoutError

    start = time.monotonic()
    with pytest.raises(InteractionTimeoutError):
        await svc.wait_for_choice(request_id, timeout=timeout)
    return time.monotonic() - start


# ============================================================
# 1. 短超时：按时超时、不悬挂
# ============================================================


async def test_short_timeout_wait_raises_promptly_without_hang(human_factory):
    """短超时 wait_for_choice 必须按时抛 InteractionTimeoutError，而不是悬挂。

    为什么重要：审批链路阻塞在 wait_for_choice 上，若超时不生效，管道会无限挂起，
    用户永远得不到超时反馈。默认 remind=300 时后台任务会 sleep(300) 才处理超时，
    本用例断言等待本身按传入 timeout 结束（<5s），同时状态正确落为 TIMEOUT。
    """
    svc, notifier = human_factory(remind_before_seconds=300)  # 生产默认，验证不悬挂
    request_id = await svc.create_choice_request(
        "s1", "t1", "tab1", "审批", timeout_seconds=1
    )

    elapsed = await _wait_times_out(svc, request_id, timeout=0.1)

    assert elapsed < 5.0, f"短超时等待悬挂了 {elapsed:.1f}s"
    record = await svc.get_request(request_id)
    assert record["status"] == "timeout"
    assert _timeout_calls(notifier, request_id) == 1


async def test_short_timeout_background_handles_without_early_reminder(human_factory):
    """短超时（timeout < remind）时后台任务直接按时处理，不发提前提醒。

    为什么重要：修复前 timeout_handler 先 sleep(0) 再 sleep(remind)，短超时请求
    会在创建瞬间收到一条"即将超时"的误提醒，且真正的超时处理被推迟到 +remind 秒。
    用户收到错误提醒、审批状态又迟迟不收敛——两者都会破坏审批闭环。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s2", "t2", "tab2", "审批", timeout_seconds=_SHORT_TIMEOUT
    )
    # 不调用 wait_for_choice，仅靠后台任务收敛
    await asyncio.sleep(0.8)

    assert notifier.reminder_calls == [], (
        f"短超时不应触发提醒，实际触发 {len(notifier.reminder_calls)} 次"
    )
    record = await svc.get_request(request_id)
    assert record["status"] == "timeout", (
        f"短超时后台任务应在 {_SHORT_TIMEOUT}s 内处理，当前状态={record['status']}"
    )
    assert _timeout_calls(notifier, request_id) == 1


# ============================================================
# 2. 超时后后台任务被取消，不再触发通知
# ============================================================


async def test_wait_timeout_cancels_background_task_and_no_late_notify(human_factory):
    """wait_for_choice 超时后必须取消后台 timeout 任务，之后不再触发任何通知。

    为什么重要：修复前 wait_for_choice 超时抛错后，后台任务仍在 sleep(remind)，
    悬挂到 +remind 秒才醒来——期间任务一直占着事件循环，且若时序竞争还可能再发
    一次超时通知。审批闭环要求超时"一锤定音"。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s3", "t3", "tab3", "审批", timeout_seconds=_SHORT_TIMEOUT
    )

    await _wait_times_out(svc, request_id, timeout=_SHORT_TIMEOUT)

    # 后台任务必须已取消或已结束，绝不能仍是存活中的 pending 任务
    task = svc._timeout_tasks.get(request_id)
    assert task is None or task.cancelled() or task.done(), (
        f"wait_for_choice 超时后后台任务仍存活: cancelled={task.cancelled() if task else None}, "
        f"done={task.done() if task else None}"
    )
    assert notifier.reminder_calls == [], "短超时不应有提前提醒"

    # 跨过原后台任务本应醒来的时刻（+remind），确认不再触发任何通知
    await asyncio.sleep(1.5)
    assert _timeout_calls(notifier, request_id) == 1, (
        "超时通知必须只发一次（后台残留任务不得再次触发）"
    )
    record = await svc.get_request(request_id)
    assert record["status"] == "timeout"


async def test_submit_response_cancels_background_task(human_factory):
    """用户提交响应后，后台超时任务被取消，超时/提醒均不再触发。

    为什么重要：审批闭环中用户快速点"同意"后，若后台还在倒计时，到点会向已完成的
    请求再发一条超时通知，造成前端状态错乱。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s4", "t4", "tab4", "审批", timeout_seconds=5
    )

    assert await svc.submit_response(request_id, "approved") is True

    await asyncio.sleep(0.4)
    assert _timeout_calls(notifier, request_id) == 0
    assert notifier.reminder_calls == []
    record = await svc.get_request(request_id)
    assert record["status"] == "completed"


async def test_cancel_request_cancels_background_task(human_factory):
    """用户取消请求后，后台超时任务被取消，不再触发超时通知。

    为什么重要：请求已取消却再收到超时通知，会误导前端把已取消的请求渲染成超时。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s5", "t5", "tab5", "审批", timeout_seconds=5
    )

    assert await svc.cancel_request(request_id, reason="user_abort") is True

    await asyncio.sleep(0.4)
    assert _timeout_calls(notifier, request_id) == 0
    assert notifier.cancel_calls == [(request_id, "user_abort")]
    record = await svc.get_request(request_id)
    assert record["status"] == "cancelled"


# ============================================================
# 3. 幂等：notify_timeout 只触发一次
# ============================================================


async def test_notify_timeout_fires_exactly_once_under_concurrent_trigger(human_factory):
    """多个入口同时进入超时处理时，notify_timeout 只触发一次（幂等）。

    为什么重要：wait_for_choice 超时与后台 timeout_handler 会在同一时刻竞争
    调用 _handle_timeout（审批闭环中最常见的竞态窗口）。若无幂等守卫，用户会收到
    两条重复超时通知。本用例用两个并发 wait_for_choice 复现同一竞态形状。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s6", "t6", "tab6", "审批", timeout_seconds=_SHORT_TIMEOUT
    )

    results = await asyncio.gather(
        svc.wait_for_choice(request_id, timeout=_SHORT_TIMEOUT),
        svc.wait_for_choice(request_id, timeout=_SHORT_TIMEOUT),
        return_exceptions=True,
    )

    from service import InteractionTimeoutError

    assert all(isinstance(r, InteractionTimeoutError) for r in results), results
    assert _timeout_calls(notifier, request_id) == 1, (
        f"notify_timeout 必须只触发一次，实际 {_timeout_calls(notifier, request_id)} 次"
    )
    record = await svc.get_request(request_id)
    assert record["status"] == "timeout"


# ============================================================
# 4. 长超时 + 提醒路径不受影响
# ============================================================


async def test_long_timeout_reminder_path_unaffected(human_factory):
    """长超时（timeout > remind）路径保持原语义：先提醒、到点超时。

    为什么重要：短超时修复不得破坏生产主路径——长审批超时仍须在
    (timeout - remind) 时刻给用户一次"即将超时"提醒，再在完整 timeout 时刻
    收敛为 TIMEOUT 并通知一次。
    """
    svc, notifier = human_factory(remind_before_seconds=_REMIND)
    request_id = await svc.create_choice_request(
        "s7", "t7", "tab7", "审批", timeout_seconds=3
    )

    # (timeout - remind) = 2s 时应已发送提醒（此时尚未超时）
    await asyncio.sleep(2.3)
    assert notifier.reminder_calls == [(request_id, 1)], "应在剩余 1s 时提醒一次"

    # 完整 timeout=3s 后收敛为 TIMEOUT，且只通知一次
    await asyncio.sleep(1.2)
    assert _timeout_calls(notifier, request_id) == 1
    assert notifier.reminder_calls == [(request_id, 1)], "提醒不应重复发送"
    record = await svc.get_request(request_id)
    assert record["status"] == "timeout"
