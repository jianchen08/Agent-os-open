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

    async def _ok(t, l):
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]
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

        # 语义统一（2026-08-22 定案）：有父管道时两次 chat.send_message——
        # ① create 分支建执行管道；② no_dispatch 登记分支把新任务以
        # task.owned.<id>.* 写回提交者管道 state（自持）。
        assert len(sender.calls) == 2
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

        # 登记分支：只写提交者管道 state，不派发
        reg = sender.calls[1]
        assert reg["pipeline_id"] == "pipe_parent_9"
        assert reg["no_dispatch"] is True
        assert reg["state"][f"task.owned.pipe_engine_gen_1.status"] == "running"
        assert reg["state"][f"task.owned.pipe_engine_gen_1.title"] == "喝水提醒"

        # YAML 写路径清零：create_task/start_task/bind_pipeline_run 全不触碰
        service.create_task.assert_not_called()
        service.start_task.assert_not_called()
        service.bind_pipeline_run.assert_not_called()
        # 响应身份（2026-08-22 短化定案 8db4c6b16）：LLM 工具面回传 12 位短 id
        # （内部权威 id 不动，登记分支的 task.owned 键仍用全 id）。
        short_id = "pipe_engine_gen_1"[:12]
        assert r.output["task_id"] == short_id
        assert r.output["pipeline_id"] == short_id
        assert short_id in r.output["message"]

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

        # 依赖存在（聚合行命中）。_read_state_rows 已是 async 方法（await 调用），
        # stub 需返回 coroutine。
        async def _rows_hit() -> list[dict]:
            return [{"pipeline_id": "dep_pipe_1", "task.status": "completed"}]

        tool._read_state_rows = _rows_hit  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs(dependencies=["dep_pipe_1"]))
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        assert sender.calls[0]["state"]["task.dependencies"] == ["dep_pipe_1"]

        # 依赖缺失 → DEPENDENCY_NOT_FOUND（不派发）
        async def _rows_empty() -> list[dict]:
            return []

        tool2 = _make_tool(mod)
        tool2._read_state_rows = _rows_empty  # type: ignore[method-assign]
        mod.set_chat_sender(_FakeSender())
        try:
            r2 = await tool2.execute(_base_inputs(dependencies=["ghost_pipe"]))
        finally:
            mod._chat_sender = None
        assert not r2.success
        assert r2.error_code == "DEPENDENCY_NOT_FOUND"

    async def test_submit_drops_retired_priority_max_retries(self, mod: Any) -> None:
        """参数退役（2026-08-24）：priority/max_retries 执行层零消费者，
        schema 与写路径整体删除——显式传入按未知参数忽略，不落派发 state。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs(priority=8, max_retries=1))
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        p = sender.calls[0]
        assert "task.priority" not in p["state"]
        assert "task.max_retries" not in p["state"]

    async def test_submit_without_priority_max_retries_no_state_keys(self, mod: Any) -> None:
        """不传退役参数 → state 无两键（不写不补默认，语义不变）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        p = sender.calls[0]
        assert "task.priority" not in p["state"]
        assert "task.max_retries" not in p["state"]

    async def test_container_registration_drops_retired_params(self, mod: Any) -> None:
        """容器登记分支同语义：退役参数不落 task.owned.<id>.* state。"""
        sender = _FakeSender(result={"status": "recorded"})
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(task_scope="container", priority=8, max_retries=1)
            )
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        reg = sender.calls[0]
        assert reg["no_dispatch"] is True
        assert not any(k.endswith(".priority") or k.endswith(".max_retries") for k in reg["state"])

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
