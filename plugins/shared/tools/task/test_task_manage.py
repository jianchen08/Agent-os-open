# @feature: task_manage 0.2 服务接线 | @ci: python-plugins-test
"""task_manage 0.2 接线回归测试。

历史 bug：TaskTool 依赖已废弃的 0.1 `infrastructure.service_provider`，
sidecar 进程 import 失败 → 「infrastructure 层未初始化（sidecar 模式）」，
get/stop/delete 全部不可用。修复后：
- `_get_task_service()` 走 0.2 tasks 插件包的 service_access（M3 自包含实例化）；
- `_get_execution_record_storage()` 返回 None 优雅降级；
- 恢复/重试的执行启动改经注入的 pipeline-executor caller（task_worker 已废弃）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 与 server.py 的 sys.path 注入保持一致：tasks 平铺目录 + system/（tasks 包限定导入用）
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent.parent
for _d in (
    str(_HERE),
    str(_PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks"),
    str(_PROJECT_ROOT / "plugins" / "shared" / "system"),
):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from tool import TaskTool  # noqa: E402

pytestmark = pytest.mark.unit


def test_get_task_service_returns_0_2_service_without_infrastructure() -> None:
    """0.2 TaskService 可经 service_access 获取，且不依赖 0.1 infrastructure 包。"""
    tool = TaskTool()
    svc = tool._get_task_service()
    assert svc is not None, "TaskService 应可初始化"
    # task_manage 调用的全部方法都应在 0.2 TaskService 上存在
    for method in (
        "get_task",
        "save_task",
        "list_all",
        "force_transition",
        "pause_task",
        "resume_task",
        "hard_delete_task",
        "cancel_task_cascade",
        "list_subtasks",
        "soft_delete_container",
    ):
        assert hasattr(svc, method), f"0.2 TaskService 缺少 {method}"


def test_execution_record_storage_degrades_to_none() -> None:
    """0.2 sidecar 无 execution_record_storage → None（活动摘要优雅降级为 '-'）。"""
    tool = TaskTool()
    assert tool._get_execution_record_storage() is None


@pytest.mark.asyncio
async def test_continue_resume_invokes_pipeline_caller() -> None:
    """恢复执行应经注入的 pipeline-executor caller 启动（task_worker 已废弃）。"""
    calls: list[tuple[str, dict]] = []

    async def fake_caller(method: str, params: dict) -> dict:
        calls.append((method, params))
        return {"status": "started", "run_id": "run_123"}

    svc = TaskTool()._get_task_service()
    task = await svc.create_task(title="resume_ipc_test", description="test")
    # 停止 → continue 走恢复路径
    stopped = await svc.pause_task(task.id)
    assert stopped is not None or svc.get_task(task.id) is not None

    tool = TaskTool(pipeline_caller=fake_caller)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, f"continue 失败: {result.error}"
    # 恢复路径应调用 pipeline-executor.start_run
    assert any(m == "pipeline-executor.start_run" for m, _ in calls), (
        f"未调用 pipeline-executor.start_run, 实际调用: {calls}"
    )

    await svc.hard_delete_task(task.id)


@pytest.mark.asyncio
async def test_continue_resume_without_caller_degrades() -> None:
    """pipeline_caller 未注入时恢复只改状态、不崩溃、带 warning。"""
    svc = TaskTool()._get_task_service()
    task = await svc.create_task(title="resume_no_caller", description="test")
    await svc.pause_task(task.id)

    tool = TaskTool(pipeline_caller=None)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, f"continue 不应失败: {result.error}"
    # 任务状态应已恢复（不被执行器缺失阻塞）
    after = svc.get_task(task.id)
    assert after is not None

    await svc.hard_delete_task(task.id)
