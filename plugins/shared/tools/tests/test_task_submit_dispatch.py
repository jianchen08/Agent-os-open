# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_submit 任务执行驱动 TDD 测试（GAP-1 核心修复）。

0.2 收尾时 pipeline-executor.start_run 占位能力移除后，task_submit 提交即落库、
**无人派发执行**——任务永远 pending（e2e 缺口 GAP-1）。本文件覆盖新契约：

1. 提交成功后经 ``chat.send_message``（create 分支，引擎生成 pipeline_id）创建
   任务执行管道：state 出生即带 ``task.*`` 字段、lineage 有父/根二选一、
   execution_context 透传；
2. 响应返回的 pipeline_id 写回任务关联（bind_pipeline_run）+ 派发成功即
   start_task（started_at 非空——run 未真正开始前不得声称"异步执行中"）；
3. 派发器不可用 / 派发失败 → 话术诚实（不声称执行中），结果携带明确 warning。

装配：conftest.py 注入 sdk / tools 共享层；task_submit 平铺目录与
system/tasks 同 test_task_submit_migration.py 的 sys.path 装配。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[2] / "system"

for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT / "channel_api"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_dispatch_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TS_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]  # 加载失败不留坏缓存（SyntaxError 等）
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


def _fake_task(task_id: str = "task-001", title: str = "喝水提醒") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.title = title
    t.status.value = "pending"
    t.metadata = {}
    return t


def _fake_service(task: MagicMock) -> MagicMock:
    """最小 task_service fake：create_task/get_task/bind_pipeline_run/start_task。"""
    from unittest.mock import AsyncMock

    svc = MagicMock()

    async def _create(**kwargs):
        # 模拟真实 create_task：metadata 入参落到返回的 task 上（供派发读取）
        if kwargs.get("metadata"):
            task.metadata = dict(kwargs["metadata"])
        return task

    svc.create_task = _create
    svc.get_task = AsyncMock(return_value=None)
    svc.bind_pipeline_run = AsyncMock(return_value=True)
    svc.start_task = AsyncMock(return_value=True)
    return svc


class _FakeSender:
    """记录 chat.send_message 参数的派发器 fake。"""

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or {"status": "created", "pipeline_id": "pipe_engine_gen_1"}
        self._error = error

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._result


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal": {"title": "喝水提醒", "description": "每小时提醒喝水"},
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "execution_id": "exec-1",
    }
    base.update(over)
    return base


async def _run_submit(mod: Any, service: MagicMock, sender: Any, inputs: dict) -> Any:
    tool = mod.TaskSubmitTool()
    tool._get_task_service = lambda: service  # type: ignore[method-assign]
    tool._validate_target_agent = lambda t, l: (True, "", "")  # type: ignore[method-assign]
    tool._check_dependencies_exist = lambda d: []  # type: ignore[method-assign]
    tool._init_workspace = _noop_init_workspace  # type: ignore[method-assign]
    if sender is None:
        mod._chat_sender = None
    else:
        mod.set_chat_sender(sender)
    try:
        return await tool.execute(inputs)
    finally:
        mod._chat_sender = None


async def _noop_init_workspace(task, workspace, task_data, task_service):  # noqa: ARG001
    """工作空间初始化 stub：0.2 sidecar 下本就 None 降级（原样返回任务）。"""
    return task, None


class TestTaskPipelineDispatch:
    def test_module_exposes_sender_injection_point(self, mod: Any) -> None:
        """server.py on_load 注入点存在（set_chat_sender / _get_chat_sender）。"""
        assert hasattr(mod, "set_chat_sender")
        assert hasattr(mod, "_get_chat_sender")
        assert mod._get_chat_sender() is None

    async def test_submit_dispatches_via_chat_send_message(self, mod: Any) -> None:
        """核心契约：创建任务后经 chat.send_message 创建执行管道。"""
        task = _fake_task()
        svc = _fake_service(task)
        sender = _FakeSender()
        r = await _run_submit(mod, svc, sender, _base_inputs())
        assert r.success, r.error

        assert len(sender.calls) == 1, "应恰好派发一次"
        p = sender.calls[0]
        # 创建分支：引擎生成 id（不接受调用方传入）
        assert p["create"] is True
        assert "pipeline_id" not in p
        # state 出生即入：task.* 字段
        assert p["state"]["task.id"] == "task-001"
        assert p["state"]["task.goal"] == "喝水提醒"
        assert p["state"]["task.status"] == "pending"
        # 血缘：有父形式（parent = 调用方管道；origin_session 同管道——主会话
        # thread_id 与 pipeline_id 同值）
        assert p["lineage"] == {
            "parent_pipeline_id": "pipe_parent_9",
            "origin_session_id": "pipe_parent_9",
        }
        # message 携带任务目标（kickoff 输入）
        assert "喝水提醒" in p["message"]
        assert p["user_id"] == "user-1"
        # 后台执行：不阻塞工具调用等待任务完成
        assert p.get("background") is True

        # 关联回写：引擎返回的 pipeline_id 绑定任务
        svc.bind_pipeline_run.assert_awaited_with("task-001", "pipe_engine_gen_1")
        # 派发成功即开始（started_at 语义——run 已真正派发）
        svc.start_task.assert_awaited_with("task-001")

        # 话术：报告执行管道已创建（诚实），响应携带 pipeline_id
        assert "pipe_engine_gen_1" in r.output["message"]
        assert r.output.get("pipeline_id") == "pipe_engine_gen_1"

    async def test_submit_root_lineage_without_parent_pipeline(self, mod: Any) -> None:
        """无调用方管道（如系统/通道自举）→ lineage 根形式（诚实声明）。"""
        task = _fake_task(task_id="task-root")
        svc = _fake_service(task)
        sender = _FakeSender()
        r = await _run_submit(
            mod, svc, sender, _base_inputs(task_id=None, pipeline_id=None)
        )
        assert r.success
        p = sender.calls[0]
        assert p["lineage"] == {
            "root": True,
            "origin": {"kind": "plugin", "source": "task_submit"},
        }
        # 无管道时 user_id 兜底系统身份
        assert p["user_id"]

    async def test_submit_passes_execution_context(self, mod: Any) -> None:
        """workspace/isolation 显式声明 → execution_context 透传给执行管道。"""
        task = _fake_task()
        svc = _fake_service(task)
        sender = _FakeSender()
        r = await _run_submit(
            mod,
            svc,
            sender,
            _base_inputs(workspace="D:/proj/demo", workspace_mode="worktree", isolation_level="isolated"),
        )
        assert r.success
        ec = sender.calls[0]["execution_context"]
        assert ec["workspace"]["source_path"] == "D:/proj/demo"
        assert ec["workspace"]["mode"] == "worktree"
        assert ec["isolation"]["level"] == "isolated"


class TestHonestMessaging:
    async def test_submit_without_sender_warns_not_lies(self, mod: Any) -> None:
        """派发器不可用（capability 缺席）→ 不得声称"异步执行中"，携带 warning。"""
        task = _fake_task()
        svc = _fake_service(task)
        r = await _run_submit(mod, svc, None, _base_inputs())
        assert r.success, "任务创建本身成功（存储语义保留）"
        assert "异步执行中" not in r.output["message"], "未派发不得声称异步执行中"
        assert r.output.get("warning"), "应携带未派发警告"
        svc.start_task.assert_not_awaited()
        svc.bind_pipeline_run.assert_not_awaited()

    async def test_submit_sender_failure_honest(self, mod: Any) -> None:
        """派发失败（内核错误）→ 任务保留 + 话术诚实 + warning。"""
        task = _fake_task()
        svc = _fake_service(task)
        sender = _FakeSender(error=RuntimeError("kernel down"))
        r = await _run_submit(mod, svc, sender, _base_inputs())
        assert r.success
        assert "异步执行中" not in r.output["message"]
        assert r.output.get("warning")
        assert "kernel down" in r.output["warning"]
        svc.start_task.assert_not_awaited()
