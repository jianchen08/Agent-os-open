# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_submit 任务执行驱动 TDD 测试（GAP-1 统一：state 单一真值，YAML 停写）。

0.2 收尾时 pipeline-executor.start_run 占位能力移除后，task_submit 提交即落库、
**无人派发执行**——任务永远 pending（e2e 缺口 GAP-1）。统一定案后本文件覆盖：

1. 提交直接经 ``chat.send_message``（create 分支，引擎生成 pipeline_id）创建
   执行管道，**不再调用 task_service.create_task**——YAML 存储无写路径；
2. task.id = 引擎返回的 pipeline_id（身份权威统一），state 出生即带 task.*
   字段（task.id 由引擎注入）、lineage 有父/根二选一、execution_context 透传；
3. 依赖校验读 state 聚合（pipeline-state.list capability）而非 YAML；
4. 不再 start_task/bind_pipeline_run（任务状态由内核 run 终态回写 state）；
5. 派发器不可用/失败 → 话术诚实（不声称执行中），结果携带明确 warning。

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
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


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


def _make_tool(mod: Any) -> Any:
    """构造工具实例并 stub 纯参数校验（target 存在性等与存储无关的面）。"""
    tool = mod.TaskSubmitTool()
    tool._validate_target_agent = lambda t, l: (True, "", "")  # type: ignore[method-assign]
    return tool


class TestTaskPipelineDispatch:
    def test_module_exposes_sender_injection_point(self, mod: Any) -> None:
        """server.py on_load 注入点存在（set_chat_sender / _get_chat_sender）。"""
        assert hasattr(mod, "set_chat_sender")
        assert hasattr(mod, "_get_chat_sender")
        assert mod._get_chat_sender() is None

    async def test_submit_creates_pipeline_without_task_service(self, mod: Any) -> None:
        """核心契约：不再调用 task_service.create_task——YAML 无写路径；
        task.id = 引擎返回的 pipeline_id（身份权威统一）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        service = MagicMock()  # spy：断言任何存储写方法都未被触碰
        tool._get_task_service = lambda: service  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error

        assert len(sender.calls) == 1
        p = sender.calls[0]
        assert p["create"] is True
        assert "pipeline_id" not in p
        # state 出生即入
        assert p["state"]["task.goal"] == "喝水提醒"
        assert p["state"]["task.status"] == "pending"
        # task.id 不在调用方 state（引擎注入）
        assert "task.id" not in p["state"]
        # 血缘：有父形式
        assert p["lineage"] == {
            "parent_pipeline_id": "pipe_parent_9",
            "origin_session_id": "pipe_parent_9",
        }
        assert "喝水提醒" in p["message"]
        assert p["user_id"] == "user-1"
        assert p.get("background") is True

        # YAML 写路径清零：create_task/start_task/bind_pipeline_run 全不触碰
        service.create_task.assert_not_called()
        service.start_task.assert_not_called()
        service.bind_pipeline_run.assert_not_called()
        # 响应即身份：task_id == pipeline_id
        assert r.output["task_id"] == "pipe_engine_gen_1"
        assert r.output["pipeline_id"] == "pipe_engine_gen_1"
        assert "pipe_engine_gen_1" in r.output["message"]

    async def test_submit_root_lineage_without_parent_pipeline(self, mod: Any) -> None:
        """无调用方管道 → lineage 根形式（诚实声明）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs(pipeline_id=None))
        finally:
            mod._chat_sender = None
        assert r.success
        assert sender.calls[0]["lineage"] == {
            "root": True,
            "origin": {"kind": "plugin", "source": "task_submit"},
        }

    async def test_submit_passes_execution_context(self, mod: Any) -> None:
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(workspace="D:/proj/demo", workspace_mode="worktree", isolation_level="isolated")
            )
        finally:
            mod._chat_sender = None
        assert r.success
        ec = sender.calls[0]["execution_context"]
        assert ec["workspace"]["source_path"] == "D:/proj/demo"
        assert ec["workspace"]["mode"] == "worktree"
        assert ec["isolation"]["level"] == "isolated"

    async def test_submit_dependency_check_reads_state_aggregation(self, mod: Any) -> None:
        """依赖校验读 state 聚合（pipeline-state.list）而非 YAML 树。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)

        # 依赖存在（聚合行命中）
        tool._read_state_rows = lambda: [{"pipeline_id": "dep_pipe_1", "task.status": "completed"}]  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs(dependencies=["dep_pipe_1"]))
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        assert sender.calls[0]["state"]["task.dependencies"] == ["dep_pipe_1"]

        # 依赖缺失 → DEPENDENCY_NOT_FOUND（不派发）
        tool2 = _make_tool(mod)
        tool2._read_state_rows = lambda: []  # type: ignore[method-assign]
        mod.set_chat_sender(_FakeSender())
        try:
            r2 = await tool2.execute(_base_inputs(dependencies=["ghost_pipe"]))
        finally:
            mod._chat_sender = None
        assert not r2.success
        assert r2.error_code == "DEPENDENCY_NOT_FOUND"

    async def test_submit_without_sender_warns_not_lies(self, mod: Any) -> None:
        """派发器不可用 → 不得声称执行中，携带 warning。"""
        tool = _make_tool(mod)
        mod._chat_sender = None
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, "任务创建本身成功（state 语义保留——管道未建但参数合法）"
        assert "异步执行中" not in r.output["message"], "未派发不得声称异步执行中"
        assert "已提交并落库" not in r.output["message"], "不得声称落库（YAML 已停写）"
        assert r.output.get("warning")

    async def test_submit_sender_failure_honest(self, mod: Any) -> None:
        sender = _FakeSender(error=RuntimeError("kernel down"))
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success
        assert "异步执行中" not in r.output["message"]
        assert r.output.get("warning")
        assert "kernel down" in r.output["warning"]
