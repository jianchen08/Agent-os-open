#!/usr/bin/env python3
"""
任务状态实时同步功能验证脚本（可复现）

验证内容：
  1. 后端消息格式验证 (create_task_status_changed_message)
  2. 后端调用链验证 (_emit_state_change → _push_status_change_ws → _do_push_status_change_ws → MessageBus.emit)
  3. 状态转换覆盖验证 (pending→running→completed, pending→running→failed, running→timeout)
  4. 防御性检查 (无 session_id 不推送, 任务不存在不推送, 推送失败不阻塞)
  5. 前端 Store 验证 (需单独运行 npx vitest)

用法：
  PYTHONPATH=src:. python3 tests/verify_task_status_sync.py
  cd frontend && npx vitest run src/stores/__tests__/taskStore.test.ts
"""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
from unittest.mock import AsyncMock, patch

from api.websocket.message_bus import MessageBus, SourceType
from api.websocket.message_types import create_task_status_changed_message
from tasks.service import TaskService
from tasks.state_machine import _TASK_TRANSITIONS
from tasks.types import TaskStatus

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")
    if detail:
        print(f"     {detail[:150]}")


async def verify_backend():
    print("\n" + "=" * 60)
    print("后端验证")
    print("=" * 60)

    # ── 1. 消息格式 ──
    print("\n── 1. 消息格式验证 ──")
    msg = create_task_status_changed_message(
        task_id="t-001", status="running", previous_status="pending",
        title="测试任务", updated_at="2026-06-08T00:00:00Z",
    )
    check("消息 type = task_status_changed", msg["type"] == "task_status_changed")
    check("data 包含 5 个必填字段",
          set(msg["data"].keys()) == {"task_id", "status", "previous_status", "title", "updated_at"})
    check("字段值正确",
          msg["data"]["task_id"] == "t-001"
          and msg["data"]["status"] == "running"
          and msg["data"]["previous_status"] == "pending")

    # ── 2. 接口签名 ──
    print("\n── 2. 接口签名验证 ──")
    sig = inspect.signature(MessageBus.emit)
    params = list(sig.parameters.keys())
    check("MessageBus.emit 含 thread_id + message + source_type",
          "thread_id" in params and "message" in params and "source_type" in sig.parameters)
    check("SourceType.SYSTEM = 'system'", SourceType.SYSTEM.value == "system")

    # ── 3. 非阻塞设计 ──
    print("\n── 3. 非阻塞设计验证 ──")
    src_push = inspect.getsource(TaskService._push_status_change_ws)
    check("_push_status_change_ws 使用 asyncio.create_task", "asyncio.create_task" in src_push)
    check("捕获 RuntimeError (无事件循环时)", "RuntimeError" in src_push)

    src_do = inspect.getsource(TaskService._do_push_status_change_ws)
    check("_do_push_status_change_ws 3 层防御 (storage/task/thread_id)",
          "self._storage is None" in src_do and "task is None" in src_do and "not thread_id" in src_do)

    src_emit = inspect.getsource(TaskService._emit_state_change)
    check("_emit_state_change 调用 _push_status_change_ws", "_push_status_change_ws" in src_emit)

    # ── 4. 状态机路径 ──
    print("\n── 4. 状态机路径验证 ──")
    paths = {
        "pending→running": ("pending", "running"),
        "running→completed": ("running", "completed"),
        "running→failed": ("running", "failed"),
        "running→timeout": ("running", "timeout"),
    }
    for label, (src, dst) in paths.items():
        ok = dst in _TASK_TRANSITIONS.get(src, set())
        check(f"状态机允许 {label}", ok)

    # ── 5. 调用链端到端 ──
    print("\n── 5. 调用链端到端验证 ──")
    tmp_dir = tempfile.mkdtemp(prefix="verify_sync_")
    svc = TaskService(data_dir=tmp_dir)

    # pending→running
    task = await svc.create_task(title="链路测试", metadata={"session_id": "sess-verify"})
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(task.id, "pending", "running")
        ok = (mock_bus.emit.call_count == 1
              and mock_bus.emit.call_args[0][0] == "sess-verify"
              and mock_bus.emit.call_args[0][1]["type"] == "task_status_changed"
              and mock_bus.emit.call_args[0][1]["data"]["status"] == "running"
              and mock_bus.emit.call_args[1]["source_type"] == SourceType.SYSTEM)
        check("pending→running: emit 正确调用", ok,
              json.dumps(mock_bus.emit.call_args[0][1], ensure_ascii=False) if mock_bus.emit.called else "未调用")

    # running→completed
    await svc.start_task(task.id)
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(task.id, "running", "completed")
        check("running→completed: 消息正确",
              mock_bus.emit.call_args[0][1]["data"]["status"] == "completed")

    # running→failed
    t2 = await svc.create_task(title="失败测试", metadata={"session_id": "sess-verify"})
    await svc.start_task(t2.id)
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(t2.id, "running", "failed")
        check("running→failed: 消息正确",
              mock_bus.emit.call_args[0][1]["data"]["status"] == "failed")

    # running→timeout
    t3 = await svc.create_task(title="超时测试", metadata={"session_id": "sess-verify"})
    await svc.start_task(t3.id)
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(t3.id, "running", "timeout")
        check("running→timeout: 消息正确",
              mock_bus.emit.call_args[0][1]["data"]["status"] == "timeout")

    # ── 6. 防御性验证 ──
    print("\n── 6. 防御性验证 ──")

    # 无 session_id
    t4 = await svc.create_task(title="无session", metadata={})
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(t4.id, "pending", "running")
        check("无 session_id 不推送", not mock_bus.emit.called)

    # 任务不存在
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws("nonexistent", "pending", "running")
        check("任务不存在不推送", not mock_bus.emit.called)

    # 推送失败不阻塞
    t5 = await svc.create_task(title="推送失败", metadata={"session_id": "sess-verify"})
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_bus.emit.side_effect = ConnectionError("模拟断连")
        mock_get.return_value = mock_bus
        try:
            await svc._do_push_status_change_ws(t5.id, "pending", "running")
            check("推送失败不阻塞 (fire-and-forget)", True)
        except Exception:
            check("推送失败不阻塞 (fire-and-forget)", False, "异常冒泡了")

    # 连续推送
    t6 = await svc.create_task(title="连续推送", metadata={"session_id": "sess-verify"})
    with patch("api.websocket.message_bus.get_message_bus") as mock_get:
        mock_bus = AsyncMock()
        mock_get.return_value = mock_bus
        await svc._do_push_status_change_ws(t6.id, "pending", "running")
        await svc._do_push_status_change_ws(t6.id, "running", "evaluating")
        await svc._do_push_status_change_ws(t6.id, "evaluating", "completed")
        check("连续 3 次推送全部成功", mock_bus.emit.call_count == 3)

    # 回归：原有任务管理功能不受影响
    t7 = await svc.create_task(title="回归测试", metadata={"session_id": "sess-verify"})
    await svc.start_task(t7.id)
    await svc.complete_task(t7.id)
    stored = svc._storage.get(t7.id)
    check("回归: 任务创建→启动→完成 状态正确", stored.status == TaskStatus.COMPLETED)

    t8 = await svc.create_task(title="列表回归", metadata={"session_id": "sess-verify"})
    all_tasks = await svc.list_all()
    check("回归: list_all 返回正确", len(all_tasks) >= 8)


def main():
    global PASS, FAIL
    print("=" * 60)
    print("任务状态实时同步功能 — 自动化验证脚本")
    print("=" * 60)

    asyncio.run(verify_backend())

    print("\n" + "=" * 60)
    print(f"后端验证结果: ✅ {PASS}  ❌ {FAIL}  总计 {PASS + FAIL}")
    print("=" * 60)

    print("""
前端验证（需单独运行）:
  cd frontend && npx vitest run src/stores/__tests__/taskStore.test.ts

前端 UI 验证（需启动前后端服务 + Playwright）:
  验证 DebugTasksPage 自动显示最新状态
  验证组件卸载时清理监听
    """)

    if FAIL > 0:
        print("⚠️  有验证项未通过")
        exit(1)
    else:
        print("✅ 全部后端验证项通过")
        exit(0)


if __name__ == "__main__":
    main()
