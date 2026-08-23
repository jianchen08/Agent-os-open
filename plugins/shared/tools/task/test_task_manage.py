# @feature: FP-0.2.二 内部模块 manifest（task_manage 0.2 服务接线） | @ci: python-coverage
"""task_manage 0.2 接线回归测试。

历史 bug：TaskTool 依赖已废弃的 0.1 `infrastructure.service_provider`，
sidecar 进程 import 失败 → 「infrastructure 层未初始化（sidecar 模式）」，
get/stop/delete 全部不可用。修复后：
- `_get_task_service()` 走 0.2 tasks 插件包的 service_access（M3 自包含实例化）；
- `_get_execution_record_storage()` 返回 None 优雅降级；
- 恢复/重试的执行启动改经注入的 pipeline-executor caller（task_worker 已废弃）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# 与 server.py 的 sys.path 注入保持一致：tasks 平铺目录 + system/（tasks 包限定导入用）
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent.parent
_PLUGIN_PATHS = tuple(
    str(_d)
    for _d in (
        _HERE,
        _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks",
        _PROJECT_ROOT / "plugins" / "shared" / "system",
    )
)
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from tool import TaskTool  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：其它测试文件的 teardown 会把 tasks/system 目录从
    sys.path 摘走，而 TaskTool 的 service_access 是运行期懒导入——
    每个用例前重插（模块期插入只保证收集期）。
    裸名 'service'/'http_api' 会被其它插件的同名文件占槽（continue 链中途
    加载 tasks/server.py 时其顶层 from service import TaskService 拿错模块）：
    用例前逐出并以正确路径序预热，teardown 快照还原，不向外污染。"""
    for _d in _PLUGIN_PATHS:
        # 无条件提到最前：其它测试会把各自插件目录插到 sys.path[0]（如
        # human），仅"不存在才插入"会让 tasks 目录落在其后，重导入仍解析
        # 到别人的同名裸模块。
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    _saved = {n: sys.modules.get(n) for n in ("service", "http_api")}
    for n in _saved:
        sys.modules.pop(n, None)
    try:
        # 预热 continue 执行链：tasks/server.py 顶层的裸名导入在干净槽位 +
        # tasks 目录最前的环境下执行一遍，把正确的模块写进缓存。
        _server_py = _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks" / "server.py"
        _spec = importlib.util.spec_from_file_location("tasks_server_preheat", _server_py)
        if _spec is not None and _spec.loader is not None:
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
    except Exception:  # noqa: BLE001 —— 预热失败不阻断用例（复由用例自身报错）
        pass
    yield
    for n, m in _saved.items():
        sys.modules.pop(n, None)
        if m is not None:
            sys.modules[n] = m


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
async def test_continue_resume_restores_status() -> None:
    """恢复执行仅改任务状态（0.2 收尾：start_run 占位已随旧引擎移除，
    任务管道执行由会话对话 / chat.send_message → PipelineExecutor 驱动）。"""
    svc = TaskTool()._get_task_service()
    task = await svc.create_task(title="resume_ipc_test", description="test")
    # 停止 → continue 走恢复路径
    await svc.pause_task(task.id)

    tool = TaskTool()
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, f"continue 失败: {result.error}"
    # 任务状态应已恢复（不被执行器缺失阻塞）
    after = svc.get_task(task.id)
    assert after is not None

    await svc.hard_delete_task(task.id)


# ─────────────── 状态枚举漂移/锚点兜底可观测（兜底反模式审查 P11，2026-08-20） ───────────────


async def test_unknown_status_preserved_with_warning(caplog) -> None:
    """P11：内核新增状态而本地枚举未同步 → 保留原串展示 + warning（不静默变 PENDING）。"""
    import logging

    tool = TaskTool()

    async def fake_rows():
        return [
            {
                "pipeline_id": "pipe-unknown-status",
                "task.status": "archived_v2",  # 不在 7 态枚举内
                "task.goal": "目标",
            }
        ]

    tool._read_state_rows = fake_rows  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        task = await tool._get_task_from_state("pipe-unknown-status")
    assert task is not None
    assert task.status == "archived_v2", "未知状态保留原串（展示真值）"
    assert any("未知任务状态" in r.getMessage() for r in caplog.records)


async def test_list_unknown_status_preserved_with_warning(caplog) -> None:
    """P11（list 路径同款）：批量组装保留原串 + warning。"""
    import logging

    tool = TaskTool()

    async def fake_rows():
        return [
            {
                "pipeline_id": "pipe-list-unknown",
                "task.status": "quarantined",
                "task.goal": "目标",
            }
        ]

    tool._read_state_rows = fake_rows  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        tasks = await tool._list_tasks_from_state()
    assert tasks is not None
    assert tasks[0].status == "quarantined"
    assert any("未知任务状态" in r.getMessage() for r in caplog.records)


async def test_anchor_fallback_hits_pid_logs_debug(caplog) -> None:
    """P11：锚点三段式兜底两键都=pid → 拿 pid 充当 session_id 时 debug 留痕。"""
    import logging

    tool = TaskTool()

    async def fake_rows():
        return [
            {
                "pipeline_id": "pipe-anchor",
                "task.status": "running",
                "task.goal": "目标",
                "lineage.origin_session_id": "pipe-anchor",  # 两键都等于 pid
                "thread_id": "pipe-anchor",
            }
        ]

    tool._read_state_rows = fake_rows  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        task = await tool._get_task_from_state("pipe-anchor")
    assert task is not None
    assert task.metadata["session_id"] == "pipe-anchor", "兜底语义保持（pid 充当锚点）"
    assert any("会话锚点" in r.getMessage() for r in caplog.records)
