# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""HumanInteractionService 状态机与通知契约测试（纯内存版）。

与 tests/plugins/tools/human/test_human_service.py（超时竞态 F-HI-1）互补：
本文件覆盖服务状态机全路径——创建/响应/取消/超时/查看/自动完成/批量取消、
跨事件循环唤醒（call_soon_threadsafe）、通知器注入与事件契约、单例管理。

外部依赖（通知器=前端通道、时钟）注入替身/小尺度真实 sleep（同既有约定）；
服务自身状态机全部真实断言。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR.parent

_MISSING = object()


def _load_service() -> Any:
    """importlib 显式路径 + 唯一模块名加载 service.py。

    service.py 内部 `from human.models import ...` 需要 tools 目录提供
    human 包命名空间（human 未安装为包）。
    """
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    sys.modules.pop("human", None)
    name = "human_service_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "service.py")
    assert spec is not None and spec.loader is not None, "cannot load human/service.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def svc_mod() -> Any:
    return _load_service()


class _RecorderNotifier:
    """记录调用的假通知器——只记录调用，不依赖真实前端通道。"""

    def __init__(self) -> None:
        self.request_calls: list[Any] = []
        self.cancel_calls: list[tuple[str, str | None, str]] = []
        self.timeout_calls: list[tuple[str, str]] = []
        self.reminder_calls: list[dict[str, Any]] = []
        self.conversation_calls: list[dict[str, Any]] = []
        self.fail_reminder = False

    async def notify_request(self, request: Any) -> bool:
        self.request_calls.append(request)
        return True

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        self.cancel_calls.append((request_id, reason, thread_id))
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
        if self.fail_reminder:
            raise RuntimeError("reminder channel down")
        self.reminder_calls.append(
            {
                "request_id": request_id,
                "remaining_seconds": remaining_seconds,
                "thread_id": thread_id,
                "title": title,
                "mode": mode,
                "options": options,
                "questions": questions,
            }
        )
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
        self.conversation_calls.append(
            {
                "thread_id": thread_id,
                "tab_id": tab_id,
                "title": title,
                "request_id": request_id,
                "initial_message": initial_message,
                "suggestions": suggestions,
            }
        )
        return True


@pytest.fixture
async def make_svc(svc_mod: Any) -> Any:
    """构造服务实例，测试结束时取消残留的后台超时任务（防 loop 关闭噪音）。"""

    services: list[Any] = []

    def _make(**kwargs: Any) -> tuple[Any, _RecorderNotifier | None]:
        notifier = kwargs.pop("notifier", _MISSING)
        if notifier is _MISSING:
            notifier = _RecorderNotifier()
        svc = svc_mod.HumanInteractionService(notifier=notifier, **kwargs)
        services.append(svc)
        return svc, notifier

    yield _make

    for svc in services:
        for task in list(svc._timeout_tasks.values()):
            task.cancel()
    await asyncio.sleep(0)


# ════════════════════════════════════════════════════════════
# 创建路径
# ════════════════════════════════════════════════════════════


async def test_send_notification_creates_pending_record_and_notifies(make_svc: Any, svc_mod: Any) -> None:
    """通知模式：非阻塞创建 record + 通知器收到请求，不创建等待事件。"""
    svc, notifier = make_svc()
    rid = await svc.send_notification(
        "s1", "t1", "进度", message="50%", priority=svc_mod.Priority.HIGH, progress=50.0,
        file_paths=["a.md"], user_id="u1", agent_id="p1",
    )
    record = await svc.get_request(rid)
    assert record is not None
    assert record["status"] == "pending"
    assert record["session_id"] == "s1"
    msg = record["message_data"]
    assert msg["interaction_mode"] == "notification"
    assert msg["title"] == "进度"
    assert msg["description"] == "50%"
    assert msg["thread_id"] == "t1"
    assert msg["progress"] == 50.0
    assert msg["priority"] == "high"
    assert msg["file_paths"] == ["a.md"]
    assert msg["user_id"] == "u1"
    assert msg["agent_id"] == "p1"
    # 非阻塞：不创建 pending event（wait_for_choice 会按需补建）
    assert rid not in svc._pending_events
    assert notifier is not None
    assert len(notifier.request_calls) == 1
    assert notifier.request_calls[0]["id"] == rid


async def test_send_notification_without_notifier(make_svc: Any) -> None:
    """无通知器时通知模式仍正常返回 request_id。"""
    svc, notifier = make_svc(notifier=None)
    rid = await svc.send_notification("s1", "t1", "标题")
    assert isinstance(rid, str) and rid
    assert notifier is None
    assert (await svc.get_request(rid)) is not None


async def test_create_choice_request_timeout_default_and_extra_fields(make_svc: Any) -> None:
    """choice 创建：timeout_seconds 缺省取 default；agent_level/pipeline_id 条件携带。"""
    svc, notifier = make_svc()
    rid = await svc.create_choice_request(
        "s1", "t1", "tab1", "审批", options=[{"id": "1", "label": "批准"}],
        questions=["确认？"], agent_level="L2", pipeline_id="pipe-1",
    )
    record = await svc.get_request(rid)
    assert record is not None
    msg = record["message_data"]
    assert msg["timeout_seconds"] == 86400  # 缺省 = default_timeout
    assert msg["options"] == [{"id": "1", "label": "批准"}]
    assert msg["questions"] == ["确认？"]
    assert msg["agent_level"] == "L2"
    assert msg["pipeline_id"] == "pipe-1"
    assert msg["timeout_reminded"] is False
    assert rid in svc._pending_events
    assert notifier is not None and len(notifier.request_calls) == 1

    # 区分度输入：显式 timeout + 不传 agent_level/pipeline_id → 不出现该键
    rid2 = await svc.create_choice_request(
        "s1", "t1", "tab1", "审批", timeout_seconds=30,
    )
    msg2 = (await svc.get_request(rid2))["message_data"]
    assert msg2["timeout_seconds"] == 30
    assert "agent_level" not in msg2
    assert "pipeline_id" not in msg2


async def test_create_conversation_request_notifies_start(make_svc: Any) -> None:
    """对话模式：通知器收到 request + conversation_start 两个事件。"""
    svc, notifier = make_svc()
    rid = await svc.create_conversation_request(
        "s1", "t1", "tab1", "讨论", initial_message="你好",
        suggestions=["好的"], agent_level="L1", pipeline_id="pipe-2",
    )
    record = await svc.get_request(rid)
    assert record is not None
    msg = record["message_data"]
    assert msg["interaction_mode"] == "conversation"
    assert msg["initial_message"] == "你好"
    assert msg["suggestions"] == ["好的"]
    assert msg["agent_level"] == "L1"
    assert msg["pipeline_id"] == "pipe-2"
    assert notifier is not None
    assert len(notifier.request_calls) == 1
    assert notifier.conversation_calls == [
        {
            "thread_id": "t1", "tab_id": "tab1", "title": "讨论",
            "request_id": rid, "initial_message": "你好", "suggestions": ["好的"],
        }
    ]


async def test_create_conversation_request_without_notifier(make_svc: Any) -> None:
    """无通知器时对话模式仍创建请求，不触发任何通知。"""
    svc, notifier = make_svc(notifier=None)
    rid = await svc.create_conversation_request("s1", "t1", "tab1", "讨论")
    assert (await svc.get_request(rid)) is not None
    assert notifier is None


# ════════════════════════════════════════════════════════════
# wait_for_choice 状态机
# ════════════════════════════════════════════════════════════


async def test_wait_for_choice_approved_returns_full_response(make_svc: Any) -> None:
    """审批通过：wait 返回完整响应字段，请求落 completed。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.submit_response(
        rid, "approved", selected_option="批准", answers=["a1"], feedback="同意",
    ) is True
    resp = await svc.wait_for_choice(rid, timeout=5)
    assert resp == {
        "request_id": rid,
        "response_type": "approved",
        "selected_option": "批准",
        "answers": ["a1"],
        "feedback": "同意",
    }
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "completed"
    assert record["message_data"]["responded_at"] is not None
    # 响应记录落库（历史查询面）
    stored = svc._responses[rid]
    assert stored["parent_record_id"] == rid
    assert stored["type"] == "interaction_response"
    assert stored["status"] == "completed"
    assert stored["message_data"]["user_id"] is None


async def test_wait_for_choice_denied_raises(make_svc: Any, svc_mod: Any) -> None:
    """审批拒绝：wait 抛 InteractionDeniedError 且携带反馈原因。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    await svc.submit_response(rid, "denied", feedback="理由不充分")
    with pytest.raises(svc_mod.InteractionDeniedError) as exc_info:
        await svc.wait_for_choice(rid, timeout=5)
    assert exc_info.value.request_id == rid
    assert exc_info.value.reason == "理由不充分"


async def test_wait_for_choice_cancelled_raises(make_svc: Any, svc_mod: Any) -> None:
    """用户取消：wait 抛 InteractionCancelledError 且携带原因。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    await svc.submit_response(rid, "cancelled", feedback="改主意了")
    with pytest.raises(svc_mod.InteractionCancelledError) as exc_info:
        await svc.wait_for_choice(rid, timeout=5)
    assert exc_info.value.request_id == rid
    assert exc_info.value.reason == "改主意了"


async def test_wait_for_choice_timeout_raises_and_marks_timeout(make_svc: Any, svc_mod: Any) -> None:
    """等待超时：抛 InteractionTimeoutError，状态落 timeout，通知一次，后台任务清理。"""
    svc, notifier = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=1)
    start = time.monotonic()
    with pytest.raises(svc_mod.InteractionTimeoutError) as exc_info:
        await svc.wait_for_choice(rid, timeout=0.1)
    elapsed = time.monotonic() - start
    assert exc_info.value.request_id == rid
    assert exc_info.value.timeout == 0.1
    assert elapsed < 5.0, f"短超时等待悬挂了 {elapsed:.1f}s"
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "timeout"
    assert notifier is not None
    assert notifier.timeout_calls == [(rid, "t1")]
    assert rid not in svc._timeout_tasks


async def test_wait_for_choice_unknown_request_raises_value_error(make_svc: Any) -> None:
    """未知 request_id：wait 抛 ValueError（fail-closed）。"""
    svc, _ = make_svc()
    with pytest.raises(ValueError):
        await svc.wait_for_choice("no-such-request", timeout=0.1)


async def test_wait_for_choice_creates_event_for_notification_record(make_svc: Any, svc_mod: Any) -> None:
    """通知记录无 event：wait 按需补建 event 后正常等待（超时收敛）。"""
    svc, notifier = make_svc()
    rid = await svc.send_notification("s1", "t1", "通知")
    assert rid not in svc._pending_events
    with pytest.raises(svc_mod.InteractionTimeoutError):
        await svc.wait_for_choice(rid, timeout=0.1)
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "timeout"
    assert notifier is not None and len(notifier.timeout_calls) == 1


async def test_wait_for_choice_woken_without_response_raises(make_svc: Any, svc_mod: Any) -> None:
    """event 被置位但无 response（如 mark_as_viewed）：wait 抛超时异常而非悬挂。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.mark_as_viewed(rid) is True
    with pytest.raises(svc_mod.InteractionTimeoutError):
        await svc.wait_for_choice(rid, timeout=5)


async def test_wait_for_choice_uses_record_timeout_seconds(make_svc: Any, svc_mod: Any) -> None:
    """未传 timeout 时取记录内 timeout_seconds（消息数据契约）。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=0.3)
    start = time.monotonic()
    with pytest.raises(svc_mod.InteractionTimeoutError):
        await svc.wait_for_choice(rid)
    assert time.monotonic() - start < 2.0


# ════════════════════════════════════════════════════════════
# respond / submit_response
# ════════════════════════════════════════════════════════════


async def test_respond_routes_nested_payload(make_svc: Any) -> None:
    """respond 解析嵌套 response 并路由到 submit_response（approved 全字段）。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    ok = await svc.respond(
        rid, {"response": {"response_type": "approved", "selected_option": "a", "answers": ["x"], "feedback": "f"}}
    )
    assert ok is True
    resp = await svc.wait_for_choice(rid, timeout=5)
    assert resp["response_type"] == "approved"
    assert resp["selected_option"] == "a"
    assert resp["answers"] == ["x"]
    assert resp["feedback"] == "f"


async def test_respond_non_dict_inner_defaults_to_answered(make_svc: Any) -> None:
    """respond 的 response 非 dict / 缺失时按 answered 兜底（不静默丢响应）。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.respond(rid, {"response": "not-a-dict"}) is True
    resp = await svc.wait_for_choice(rid, timeout=5)
    assert resp["response_type"] == "answered"

    rid2 = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.respond(rid2, {}) is True
    resp2 = await svc.wait_for_choice(rid2, timeout=5)
    assert resp2["response_type"] == "answered"


async def test_submit_response_unknown_request_returns_false(make_svc: Any) -> None:
    """未知请求提交响应返回 False（不落库）。"""
    svc, _ = make_svc()
    assert await svc.submit_response("no-such", "approved") is False


async def test_submit_response_non_pending_returns_false(make_svc: Any) -> None:
    """非 pending 状态（已 completed）再次响应返回 False。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.submit_response(rid, "approved") is True
    assert await svc.submit_response(rid, "approved") is False


async def test_submit_response_without_event_still_succeeds(make_svc: Any) -> None:
    """通知记录（无 event）提交响应：成功且落 completed（event 缺失仅告警）。"""
    svc, _ = make_svc()
    rid = await svc.send_notification("s1", "t1", "通知")
    assert await svc.submit_response(rid, "answered", user_id="u1") is True
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "completed"
    assert svc._responses[rid]["message_data"]["user_id"] == "u1"


async def test_submit_response_without_wait_direct_set(make_svc: Any) -> None:
    """choice 记录未进入 wait 即提交：event 存在但无等待方循环 → 直接 set。"""
    svc, _ = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.submit_response(rid, "approved") is True
    # 事件已置位：后续 wait 立即返回
    resp = await svc.wait_for_choice(rid, timeout=5)
    assert resp["response_type"] == "approved"


# ════════════════════════════════════════════════════════════
# mark_as_viewed / cancel_request
# ════════════════════════════════════════════════════════════


async def test_mark_as_viewed_transitions_and_guards(make_svc: Any) -> None:
    """mark_as_viewed：pending → viewed 并记录时间；缺失/非 pending 返回 False。"""
    svc, _ = make_svc()
    assert await svc.mark_as_viewed("no-such") is False
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.mark_as_viewed(rid) is True
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "viewed"
    assert record["message_data"]["viewed_at"] is not None
    # 已 viewed 再标记 → False
    assert await svc.mark_as_viewed(rid) is False


async def test_cancel_request_transitions_and_guards(make_svc: Any, svc_mod: Any) -> None:
    """cancel_request：pending → cancelled 并通知；终态/缺失返回 False。"""
    svc, notifier = make_svc()
    assert await svc.cancel_request("no-such") is False

    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.cancel_request(rid, reason="user_abort") is True
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "cancelled"
    assert notifier is not None
    assert notifier.cancel_calls == [(rid, "user_abort", "t1")]
    # 终态再取消 → False
    assert await svc.cancel_request(rid) is False

    # completed / timeout 终态同样拒绝取消
    rid2 = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    await svc.submit_response(rid2, "approved")
    assert await svc.cancel_request(rid2) is False
    rid3 = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=1)
    with pytest.raises(svc_mod.InteractionTimeoutError):
        await svc.wait_for_choice(rid3, timeout=0.1)
    assert await svc.cancel_request(rid3) is False


async def test_cancel_request_without_notifier(make_svc: Any) -> None:
    """无通知器时取消仍成功（通知跳过）。"""
    svc, notifier = make_svc(notifier=None)
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    assert await svc.cancel_request(rid, reason="r") is True
    assert notifier is None


# ════════════════════════════════════════════════════════════
# 批量操作：auto_complete / cancel_pending_for_thread
# ════════════════════════════════════════════════════════════


async def test_auto_complete_conversation_for_pipeline(make_svc: Any) -> None:
    """自动完成：只处理同管道 pending conversation，choice/其他管道/终态不动。"""
    svc, _ = make_svc()
    # 目标：pending conversation（应完成）
    rid_target = await svc.create_conversation_request("pipe-1", "t1", "tab1", "讨论")
    # choice 模式（不应动）
    rid_choice = await svc.create_choice_request("pipe-1", "t1", "tab1", "审批", timeout_seconds=30)
    # 其他管道 conversation（不应动）
    rid_other = await svc.create_conversation_request("pipe-2", "t2", "tab2", "讨论")
    # 同管道已 completed（不应动）
    rid_done = await svc.create_conversation_request("pipe-1", "t3", "tab3", "讨论")
    await svc.submit_response(rid_done, "approved")

    completed = await svc.auto_complete_conversation_for_pipeline("pipe-1")
    assert completed == 1
    assert (await svc.get_request(rid_target))["status"] == "completed"
    assert (await svc.get_request(rid_choice))["status"] == "pending"
    assert (await svc.get_request(rid_other))["status"] == "pending"
    assert (await svc.get_request(rid_done))["status"] == "completed"


async def test_auto_complete_survives_single_failure(make_svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """自动完成单请求失败不连坐（防御分支：异常仅告警，计数不增）。"""
    svc, _ = make_svc()
    rid = await svc.create_conversation_request("pipe-1", "t1", "tab1", "讨论")

    async def _boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("submit down")

    monkeypatch.setattr(svc, "submit_response", _boom)  # 故障注入：验证 except 分支
    assert await svc.auto_complete_conversation_for_pipeline("pipe-1") == 0
    assert (await svc.get_request(rid))["status"] == "pending"


async def test_cancel_pending_for_thread(make_svc: Any) -> None:
    """批量取消：只取消指定 thread 的 pending 请求。

    注：record 顶层无 thread_id 字段（thread_id 在 message_data 内），
    cancel_pending_for_thread 的 `record.get("thread_id")` 恒为 None，
    实际匹配键是 session_id——按现状断言（thread_id 参数实际按 session_id 匹配）。
    """
    svc, notifier = make_svc()
    # session_id == "t1" 的记录会被取消（thread_id 参数按 session_id 匹配）
    rid_s = await svc.create_choice_request("t1", "", "tab1", "审批", timeout_seconds=30)
    # session_id 为 "s1" 的记录（thread_id 传了 "t1"）不会被取消
    rid_t = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    rid_other = await svc.create_choice_request("s2", "t2", "tab2", "审批", timeout_seconds=30)
    rid_done = await svc.create_choice_request("t1", "t1", "tab1", "审批", timeout_seconds=30)
    await svc.submit_response(rid_done, "approved")

    cancelled = await svc.cancel_pending_for_thread("t1", reason="new_message_arrived")
    assert cancelled == 1
    assert (await svc.get_request(rid_s))["status"] == "cancelled"
    assert (await svc.get_request(rid_t))["status"] == "pending"
    assert (await svc.get_request(rid_other))["status"] == "pending"
    assert (await svc.get_request(rid_done))["status"] == "completed"
    assert notifier is not None
    assert len(notifier.cancel_calls) == 1

    # 区分度输入：按 session_id 匹配取消
    cancelled2 = await svc.cancel_pending_for_thread("s1")
    assert cancelled2 == 1
    assert (await svc.get_request(rid_t))["status"] == "cancelled"


async def test_cancel_pending_for_thread_survives_single_failure(make_svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """批量取消单请求失败不连坐（防御分支）。"""
    svc, _ = make_svc()
    await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)

    async def _boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("cancel down")

    monkeypatch.setattr(svc, "cancel_request", _boom)  # 故障注入：验证 except 分支
    # 匹配键是 session_id（见 test_cancel_pending_for_thread 注释）
    assert await svc.cancel_pending_for_thread("s1") == 0


# ════════════════════════════════════════════════════════════
# 查询面
# ════════════════════════════════════════════════════════════


async def test_get_pending_requests_filters(make_svc: Any) -> None:
    """待处理列表：session/user/limit 过滤，非 pending 排除。"""
    svc, _ = make_svc()
    await svc.create_choice_request("s1", "t1", "tab1", "A", user_id="u1", timeout_seconds=30)
    await svc.send_notification("s1", "t2", "B", user_id="u2")
    await svc.create_choice_request("s2", "t3", "tab3", "C", timeout_seconds=30)
    rid_done = await svc.create_choice_request("s1", "t4", "tab4", "D", timeout_seconds=30)
    await svc.submit_response(rid_done, "approved")

    assert len(await svc.get_pending_requests()) == 3
    assert len(await svc.get_pending_requests(session_id="s1")) == 2
    assert len(await svc.get_pending_requests(session_id="s1", user_id="u1")) == 1
    assert len(await svc.get_pending_requests(session_id="s1", limit=1)) == 1
    assert len(await svc.get_pending_requests(session_id="s2")) == 1
    assert await svc.get_pending_requests(session_id="no-such") == []


async def test_get_interaction_history_merges_requests_and_responses(make_svc: Any) -> None:
    """历史：请求 + 响应合并、按 session 过滤、limit 截断。"""
    svc, _ = make_svc()
    rid1 = await svc.create_choice_request("s1", "t1", "tab1", "A", timeout_seconds=30)
    await svc.submit_response(rid1, "approved")
    await svc.create_choice_request("s1", "t2", "tab2", "B", timeout_seconds=30)
    await svc.create_choice_request("s2", "t3", "tab3", "C", timeout_seconds=30)

    history = await svc.get_interaction_history("s1")
    assert len(history) == 3  # 2 请求 + 1 响应
    assert {h["type"] for h in history} == {"interaction_request", "interaction_response"}
    assert len(await svc.get_interaction_history("s1", limit=2)) == 2
    assert await svc.get_interaction_history("no-such") == []


async def test_get_request_missing_returns_none(make_svc: Any) -> None:
    """get_request：缺失返回 None。"""
    svc, _ = make_svc()
    assert await svc.get_request("no-such") is None


# ════════════════════════════════════════════════════════════
# 通知器注入 / 单例
# ════════════════════════════════════════════════════════════


async def test_set_notifier_swaps_channel(make_svc: Any) -> None:
    """set_notifier 后通知走新通道。"""
    svc, notifier = make_svc(notifier=None)
    assert notifier is None
    new_notifier = _RecorderNotifier()
    svc.set_notifier(new_notifier)
    await svc.send_notification("s1", "t1", "标题")
    assert len(new_notifier.request_calls) == 1


async def test_singleton_get_set_reset(svc_mod: Any) -> None:
    """单例：get 惰性创建、set 替换、reset 后重建。"""
    svc_mod.reset_human_interaction_service()
    first = svc_mod.get_human_interaction_service()
    assert svc_mod.get_human_interaction_service() is first
    replacement = svc_mod.HumanInteractionService()
    svc_mod.set_human_interaction_service(replacement)
    assert svc_mod.get_human_interaction_service() is replacement
    svc_mod.reset_human_interaction_service()
    assert svc_mod.get_human_interaction_service() is not replacement
    svc_mod.reset_human_interaction_service()


async def test_make_request_record_without_extra(svc_mod: Any) -> None:
    """_make_request_record：extra=None 时 message_data 只有默认键。"""
    svc = svc_mod.HumanInteractionService()
    record = svc._make_request_record(
        request_id="r1", session_id="s1", mode=svc_mod.InteractionMode.CHOICE, title="T",
        description="D", thread_id="t1", tab_id="tab1", user_id=None, agent_id=None,
    )
    assert record["status"] == "pending"
    assert record["type"] == "interaction_request"
    assert record["message_data"] == {
        "interaction_mode": "choice", "title": "T", "description": "D",
        "thread_id": "t1", "tab_id": "tab1", "user_id": None, "agent_id": None,
        "viewed_at": None,
    }


# ════════════════════════════════════════════════════════════
# 超时后台任务（F-HI-1 契约的补充路径）
# ════════════════════════════════════════════════════════════


async def test_short_timeout_background_no_reminder(make_svc: Any) -> None:
    """短超时（timeout < remind）：后台任务直接收敛，不发提前提醒。"""
    svc, notifier = make_svc(remind_before_seconds=1)
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=0.3)
    await asyncio.sleep(0.8)
    assert notifier is not None
    assert notifier.reminder_calls == []
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "timeout"
    assert notifier.timeout_calls == [(rid, "t1")]


async def test_long_timeout_reminder_then_timeout(make_svc: Any) -> None:
    """长超时：先提醒（含 payload 契约），到点超时通知一次。"""
    svc, notifier = make_svc(remind_before_seconds=0.2)
    rid = await svc.create_choice_request(
        "s1", "t1", "tab1", "审批", options=[{"id": "1", "label": "批准"}],
        questions=["确认？"], timeout_seconds=0.6,
    )
    await asyncio.sleep(0.45)
    assert notifier is not None
    assert notifier.reminder_calls == [
        {
            "request_id": rid, "remaining_seconds": 0.2, "thread_id": "t1",
            "title": "审批", "mode": "choice",
            "options": [{"id": "1", "label": "批准"}], "questions": ["确认？"],
        }
    ]
    record = await svc.get_request(rid)
    assert record is not None and record["message_data"]["timeout_reminded"] is True
    assert record["status"] == "pending"  # 提醒时刻尚未超时

    await asyncio.sleep(0.3)
    assert notifier.timeout_calls == [(rid, "t1")]
    assert (await svc.get_request(rid))["status"] == "timeout"
    assert len(notifier.reminder_calls) == 1  # 提醒不重复


async def test_long_timeout_skips_reminder_when_not_pending(make_svc: Any) -> None:
    """提醒时刻记录已非 pending（viewed）：跳过提醒，超时处理幂等返回。"""
    svc, notifier = make_svc(remind_before_seconds=0.2)
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=0.6)
    await svc.mark_as_viewed(rid)  # viewed 不取消后台任务（与 submit 不同）
    await asyncio.sleep(0.7)
    assert notifier is not None
    assert notifier.reminder_calls == []
    assert notifier.timeout_calls == []
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "viewed"


async def test_timeout_without_notifier(make_svc: Any) -> None:
    """无通知器：长超时后台任务正常收敛（通知跳过）。"""
    svc, notifier = make_svc(notifier=None, remind_before_seconds=0.2)
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=0.6)
    await asyncio.sleep(0.7)
    assert notifier is None
    assert (await svc.get_request(rid))["status"] == "timeout"


async def test_timeout_handler_exception_cleans_registry(make_svc: Any) -> None:
    """提醒通道异常：后台任务记录异常退出并自清理（不悬挂登记）。"""
    svc, notifier = make_svc(remind_before_seconds=0.2)
    assert notifier is not None
    notifier.fail_reminder = True
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=0.6)
    await asyncio.sleep(0.7)
    assert rid not in svc._timeout_tasks  # finally 自清理
    record = await svc.get_request(rid)
    assert record is not None and record["status"] == "pending"  # 超时处理未执行


async def test_timeout_task_cancel_cleans_registry(make_svc: Any) -> None:
    """后台任务运行中被取消：CancelledError 被吞掉，任务正常结束，finally 自清理登记。"""
    svc, _ = make_svc(remind_before_seconds=0.2)
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=5)
    task = svc._timeout_tasks[rid]
    # 让协程体启动并进入 sleep（否则 finally 不执行，登记残留）
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()  # CancelledError 被 handler 吞掉 → 正常结束
    assert rid not in svc._timeout_tasks  # finally 自清理


# ════════════════════════════════════════════════════════════
# 内部辅助：_handle_timeout / _cancel_timeout_task / _set_event_threadsafe
# ════════════════════════════════════════════════════════════


async def test_handle_timeout_unknown_request_notifies(make_svc: Any) -> None:
    """_handle_timeout：未知请求仍发超时通知（thread_id 空）。"""
    svc, notifier = make_svc()
    await svc._handle_timeout("no-such")
    assert notifier is not None
    assert notifier.timeout_calls == [("no-such", "")]


async def test_handle_timeout_non_pending_returns_early(make_svc: Any) -> None:
    """_handle_timeout：非 pending 状态幂等返回，不通知。"""
    svc, notifier = make_svc()
    rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
    await svc.submit_response(rid, "approved")
    await svc._handle_timeout(rid)
    assert notifier is not None
    assert notifier.timeout_calls == []
    assert (await svc.get_request(rid))["status"] == "completed"


async def test_cancel_timeout_task_variants(make_svc: Any) -> None:
    """_cancel_timeout_task：无任务/当前任务/其他任务三种形态。"""
    svc, _ = make_svc()
    # 无任务：不崩溃
    svc._cancel_timeout_task("no-such")

    # 当前任务：跳过 cancel 仅移除登记
    async def _self_cancel() -> None:
        current = asyncio.current_task()
        assert current is not None
        svc._timeout_tasks["r-self"] = current
        svc._cancel_timeout_task("r-self")
        assert "r-self" not in svc._timeout_tasks
        assert current.cancelled() is False

    await _self_cancel()

    # 其他任务：cancel 并移除
    dummy = asyncio.create_task(asyncio.sleep(30))
    svc._timeout_tasks["r-other"] = dummy
    svc._cancel_timeout_task("r-other")
    assert "r-other" not in svc._timeout_tasks
    await asyncio.gather(dummy, return_exceptions=True)
    assert dummy.cancelled() is True


async def test_set_event_threadsafe_missing_event_logs(make_svc: Any) -> None:
    """_set_event_threadsafe：event 缺失仅告警不崩溃。"""
    svc, _ = make_svc()
    svc._set_event_threadsafe("no-such")


def test_cross_loop_submit_wakes_waiting_loop(svc_mod: Any) -> None:
    """跨事件循环唤醒：wait 在引擎线程循环、submit 在 API 线程循环。

    契约：_set_event_threadsafe 必须经 call_soon_threadsafe 调度到等待方循环，
    否则另一循环的 Event.set() 无法唤醒 wait()（历史 bug 场景）。
    """
    shared: dict[str, Any] = {}

    def _waiter() -> None:
        async def _main() -> None:
            svc = svc_mod.HumanInteractionService()
            shared["svc"] = svc
            rid = await svc.create_choice_request("s1", "t1", "tab1", "审批", timeout_seconds=30)
            shared["rid"] = rid
            try:
                shared["resp"] = await svc.wait_for_choice(rid, timeout=10)
            except Exception as exc:  # noqa: BLE001 —— 记录异常供主线程断言
                shared["err"] = exc

        asyncio.run(_main())

    def _responder() -> None:
        async def _main() -> None:
            svc = shared["svc"]
            # 同步点：等 wait 方注册事件循环（确定性覆盖 call_soon_threadsafe 分支）
            deadline = time.monotonic() + 5
            while shared["rid"] not in svc._pending_event_loops and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert shared["rid"] in svc._pending_event_loops, "waiter 未注册事件循环"
            shared["ok"] = await svc.submit_response(shared["rid"], "approved", selected_option="yes")

        asyncio.run(_main())

    t1 = threading.Thread(target=_waiter)
    t1.start()
    deadline = time.monotonic() + 5
    while "rid" not in shared and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "rid" in shared, "waiter 未在 5s 内创建请求"
    t2 = threading.Thread(target=_responder)
    t2.start()
    t2.join(timeout=5)
    t1.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive(), "跨线程等待悬挂"
    assert shared.get("ok") is True
    assert "err" not in shared, shared.get("err")
    resp = shared.get("resp", {})
    assert resp.get("response_type") == "approved"
    assert resp.get("selected_option") == "yes"


# ════════════════════════════════════════════════════════════
# wait_for_conversation_arrival
# ════════════════════════════════════════════════════════════


async def test_wait_for_conversation_arrival_states(make_svc: Any) -> None:
    """对话到达等待：无事件/超时/到达三种结果。"""
    svc, _ = make_svc()
    # 无事件（未知请求）→ 直接 timeout
    assert await svc.wait_for_conversation_arrival("no-such") == {
        "status": "timeout", "message": "用户未到达对话页面",
    }
    # 超时
    rid = await svc.create_conversation_request("s1", "t1", "tab1", "讨论")
    assert await svc.wait_for_conversation_arrival(rid, timeout=0.1) == {
        "status": "timeout", "message": "用户在 0.1 秒内未到达对话页面",
    }
    # 到达（mark_as_viewed 置位事件）
    rid2 = await svc.create_conversation_request("s1", "t2", "tab2", "讨论")
    await svc.mark_as_viewed(rid2)
    assert await svc.wait_for_conversation_arrival(rid2, timeout=5) == {
        "status": "arrived", "message": "用户已到达对话页面",
    }
