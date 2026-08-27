# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""child_task_guard 挂起判定 + 回退路径 + MCP server.py 适配层测试。

覆盖行为面（既有 tests/test_state_read_unify.py 未触达的）：
- 配置接口：name/priority 默认与覆盖、idle_remind_limit 默认与覆盖
- execute 全流程：无活跃子任务放行 / 活跃但非 llm_call 放行 / 活跃 + raw_tool_calls
  放行 / 活跃 + 纯文本 → route_signal=wait + submitted_task_ids + skip_remaining
- _get_active_children：
    - 主路径（state 聚合读）：pipeline_id 空时跳过主路径；scheduled/evaluating 计入
    - 回退路径（读面未注入 + task_service）：list_by_status 匹配 parent_pipeline_id；
      查询异常 → warning 留痕不中断
    - 回退路径 + task_id：list_subtasks 匹配（TaskStatus 枚举 + 字符串值两种形态）；
      safe_enum_value 提取枚举原始值
    - list_subtasks 异常 → warning 留痕
- _read_state_rows：未注入 None / sync 行 / async 行 / 非 list 返回 / 非 dict 行过滤 /
  读取异常 → warning 降级 None
- _get_task_service：ctx 优先、ctx 缺失回退 service_access、service_access 不可用 → None
- server.py：get_instance 懒构建缓存、on_load 注入 state 读取器（pipeline-state
  capability）、on_unload 清缓存、execute 工具契约（dict 直通/PluginResult 序列化）

外部依赖（task_service / state 读取器）用轻量替身；判定逻辑全部真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PIPELINE_ROOT = Path(__file__).resolve().parents[4]  # plugins/shared
_TASKS_DIR = _PIPELINE_ROOT / "system" / "tasks"

for _d in [_PLUGIN_DIR, _PIPELINE_ROOT, _TASKS_DIR]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# 真实 tasks.types 以唯一模块名加载；`from tasks.types import` 走 sys.modules 映射
# （插件 import 硬编码 tasks.types，真实包在 0.2 平铺布局下不存在）。
_TASKS_TYPES_MOD = "child_task_guard_tasks_types_real"
spec = importlib.util.spec_from_file_location(_TASKS_TYPES_MOD, _TASKS_DIR / "task_types.py")
assert spec is not None and spec.loader is not None
_TASKS_TYPES = importlib.util.module_from_spec(spec)
sys.modules[_TASKS_TYPES_MOD] = _TASKS_TYPES
spec.loader.exec_module(_TASKS_TYPES)

_TASKS_PKG = types.ModuleType("tasks")
_TASKS_PKG.types = _TASKS_TYPES
sys.modules["tasks"] = _TASKS_PKG
sys.modules["tasks.types"] = _TASKS_TYPES

# 本地 enum_utils 以唯一模块名显式加载——裸名 `from enum_utils import` 会被
# sys.path 上先序的 tasks/ 目录同名模块抢走（代码相同但覆盖面错文件）。
_ENUM_UTILS_MOD = "child_task_guard_enum_utils_real"
spec = importlib.util.spec_from_file_location(_ENUM_UTILS_MOD, _PLUGIN_DIR / "enum_utils.py")
assert spec is not None and spec.loader is not None
_ENUM_UTILS = importlib.util.module_from_spec(spec)
sys.modules[_ENUM_UTILS_MOD] = _ENUM_UTILS
spec.loader.exec_module(_ENUM_UTILS)


def _load_plugin() -> Any:
    mod_name = "child_task_guard_full_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_PLUGIN_MOD = _load_plugin()
ChildTaskGuard: Any = _PLUGIN_MOD.ChildTaskGuard
OutputResult: Any = _PLUGIN_MOD.OutputResult
RouteSignal: Any = _PLUGIN_MOD.RouteSignal
TS: Any = _TASKS_TYPES.TaskStatus


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _async_stub(value: Any) -> Any:
    """构造固定返回值的异步替身（_get_active_children 是协程）。"""

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        return value

    return _stub


def _set_state_reader(reader: Any) -> None:
    """注入模块级 state 读取器（与 server.py on_load 同入口）。"""
    _PLUGIN_MOD.set_state_reader(reader)


def _clear_state_reader() -> None:
    _PLUGIN_MOD._state_reader = None


def _make_ctx(state: dict[str, Any] | None = None, services: dict[str, Any] | None = None) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=dict(state or {}), _services=dict(services or {}))


class _Task:
    """回退路径用的最小任务替身。"""

    def __init__(
        self,
        id: str,
        status: Any = "running",
        parent_pipeline_id: str | None = None,
    ) -> None:
        self.id = id
        self.status = status
        self.parent_pipeline_id = parent_pipeline_id


class _TaskService:
    """轻量 task_service 替身：list_by_status / list_subtasks 真实逻辑。"""

    def __init__(self, by_status: dict[str, list[_Task]] | None = None, subtasks: list[_Task] | None = None) -> None:
        self.by_status = by_status or {}
        self.subtasks = subtasks or []

    def list_by_status(self, status: Any) -> list[_Task]:
        return list(self.by_status.get(str(status.value), []))

    def list_subtasks(self, parent_id: str) -> list[_Task]:
        return list(self.subtasks)


# ═══════════════════════════════════════════════════════════
# 配置接口
# ═══════════════════════════════════════════════════════════


class TestConfigInterface:
    def test_name_and_priority(self) -> None:
        """name 固定；priority 默认 28、支持配置覆盖。"""
        assert ChildTaskGuard(config={}).name == "child_task_guard"
        assert ChildTaskGuard(config={}).priority == 28
        assert ChildTaskGuard(config={"priority": 5}).priority == 5

    def test_idle_remind_limit(self) -> None:
        """idle_remind_limit 默认 3、支持配置覆盖。"""
        assert ChildTaskGuard(config={})._idle_remind_limit == 3
        assert ChildTaskGuard(config={"idle_remind_limit": 7})._idle_remind_limit == 7


# ═══════════════════════════════════════════════════════════
# execute 全流程
# ═══════════════════════════════════════════════════════════


class TestExecuteFlow:
    def test_no_active_children_passes_through(self) -> None:
        """无活跃子任务 → 空 OutputResult，不挂起。"""
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "llm_call"})
        guard._get_active_children = _async_stub((False, []))  # type: ignore[method-assign]
        result = _run(guard.execute(ctx))
        assert isinstance(result, OutputResult)
        assert result.state_updates == {}
        assert result.route_signal is None
        assert result.skip_remaining is False

    def test_active_but_non_llm_core_type_passes_through(self) -> None:
        """活跃子任务但 core_type 非 llm_call → 延后挂起。"""
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "tool_execute"})
        guard._get_active_children = _async_stub((True, ["c1"]))  # type: ignore[method-assign]
        result = _run(guard.execute(ctx))
        assert result.route_signal is None
        assert result.state_updates == {}

    def test_active_with_raw_tool_calls_passes_through(self) -> None:
        """活跃子任务但 LLM 有挂起工具调用 → 继续。"""
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "llm_call", "raw_tool_calls": [{"x": 1}]})
        guard._get_active_children = _async_stub((True, ["c1"]))  # type: ignore[method-assign]
        result = _run(guard.execute(ctx))
        assert result.route_signal is None
        assert result.state_updates == {}

    def test_active_plain_text_suspends(self) -> None:
        """活跃子任务 + llm_call + 纯文本 → wait 信号 + submitted_task_ids + 跳过后续。"""
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "llm_call"})
        guard._get_active_children = _async_stub((True, ["c1", "c2"]))  # type: ignore[method-assign]
        result = _run(guard.execute(ctx))
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"
        assert "child_task_guard" in result.route_signal.reason  # 原因含守护名
        assert set(result.state_updates["submitted_task_ids"]) == {"c1", "c2"}
        assert result.skip_remaining is True

    def test_suspend_reason_carries_core_type(self) -> None:
        """wait 原因携带 core_type（性质断言：不同 core_type → 不同原因字符串）。"""
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "llm_call"})
        guard._get_active_children = _async_stub((True, ["c1"]))  # type: ignore[method-assign]
        result = _run(guard.execute(ctx))
        assert "llm_call" in result.route_signal.reason

    def test_uses_context_state_services(self) -> None:
        """execute 从 ctx 取 state 与 service（集成：真实 _get_active_children 主路径）。"""
        rows = [{"pipeline_id": "c1", "task.status": "running", "lineage.parent_pipeline_id": "p"}]
        guard = ChildTaskGuard(config={})
        _set_state_reader(lambda: rows)
        try:
            ctx = _make_ctx(state={"pipeline_id": "p", "core_type": "llm_call"})
            result = _run(guard.execute(ctx))
            assert result.route_signal is not None
            assert result.route_signal.route_type == "wait"
            assert result.state_updates["submitted_task_ids"] == ["c1"]
        finally:
            _clear_state_reader()


# ═══════════════════════════════════════════════════════════
# _get_active_children 主路径（state 聚合读）
# ═══════════════════════════════════════════════════════════


class TestActiveChildrenMainPath:
    def test_pending_and_evaluating_count_as_active(self) -> None:
        """pending/evaluating 也是活跃状态（性质断言：活跃状态集合超集）。

        活跃集合与 TaskStatus 枚举对齐（pending/running/evaluating）——枚举
        无 scheduled，终态 completed/failed 不计入。
        """
        rows = [
            {"pipeline_id": "c_pend", "task.status": "pending", "lineage.parent_pipeline_id": "p"},
            {"pipeline_id": "c_eval", "task.status": "evaluating", "lineage.parent_pipeline_id": "p"},
            {"pipeline_id": "c_done", "task.status": "completed", "lineage.parent_pipeline_id": "p"},
        ]
        guard = ChildTaskGuard(config={})
        _set_state_reader(lambda: rows)
        try:
            has_active, ids = _run(guard._get_active_children("p", None, _make_ctx(state={})))
            assert has_active is True
            assert set(ids) == {"c_pend", "c_eval"}
        finally:
            _clear_state_reader()

    def test_empty_pipeline_id_skips_main_path(self) -> None:
        """pipeline_id 为空 → 主路径跳过（不抛、不误判）。"""
        rows = [{"pipeline_id": "c1", "task.status": "running", "lineage.parent_pipeline_id": "p"}]
        guard = ChildTaskGuard(config={})
        _set_state_reader(lambda: rows)
        try:
            has_active, ids = _run(guard._get_active_children("", None, _make_ctx(state={})))
            assert (has_active, ids) == (False, [])
        finally:
            _clear_state_reader()

    def test_state_reader_none_falls_back_to_task_service(self) -> None:
        """读面未注入 → 回退 task_service 主路径（list_by_status 三状态遍历）。"""
        svc = _TaskService(
            by_status={
                "running": [_Task("c1", TS.RUNNING, "p"), _Task("other", TS.RUNNING, "q")],
                "pending": [_Task("c2", TS.PENDING, "p")],
            }
        )
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        has_active, ids = _run(guard._get_active_children("p", None, ctx))
        assert has_active is True
        assert set(ids) == {"c1", "c2"}
        assert "other" not in ids

    def test_fallback_list_by_status_exception_warns(self, caplog: Any) -> None:
        """list_by_status 查询异常 → warning 留痕，不中断，走 task_id 分支。"""
        svc = _TaskService(subtasks=[_Task("c1", TS.RUNNING)])
        svc.list_by_status = lambda status: (_ for _ in ()).throw(RuntimeError("db down"))  # type: ignore[method-assign]
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        with caplog.at_level(logging.WARNING):
            has_active, ids = _run(guard._get_active_children("p", "parent-task", ctx))
        assert (has_active, ids) == (True, ["c1"])
        assert any("list_by_status query failed" in r.getMessage() for r in caplog.records)

    def test_fallback_no_task_service_returns_false(self) -> None:
        """读面与 task_service 均不可用 → (False, []) 不崩。"""
        guard = ChildTaskGuard(config={})
        guard._get_task_service = lambda ctx: None  # type: ignore[method-assign]
        has_active, ids = _run(guard._get_active_children("p", None, _make_ctx(state={})))
        assert (has_active, ids) == (False, [])


# ═══════════════════════════════════════════════════════════
# _get_active_children 回退 task_id 分支
# ═══════════════════════════════════════════════════════════


class TestActiveChildrenTaskIdBranch:
    def test_subtasks_matching_active_statuses(self) -> None:
        """list_subtasks 命中活跃状态（枚举形态）→ 计入；完成/异状态不计入。"""
        svc = _TaskService(
            subtasks=[
                _Task("c_run", TS.RUNNING),
                _Task("c_eval", TS.EVALUATING),
                _Task("c_done", TS.COMPLETED),
            ]
        )
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        has_active, ids = _run(guard._get_active_children("", "parent-task", ctx))
        assert has_active is True
        assert set(ids) == {"c_run", "c_eval"}

    def test_subtasks_string_status_normalized(self) -> None:
        """子任务状态为字符串（非枚举）→ safe_enum_value 原样比较，同样判定。"""
        svc = _TaskService(
            subtasks=[
                _Task("c1", "running"),
                _Task("c2", "pending"),
            ]
        )
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        has_active, ids = _run(guard._get_active_children("", "parent-task", ctx))
        assert has_active is True
        assert set(ids) == {"c1", "c2"}  # seen_ids 是 set，顺序依赖哈希随机化

    def test_subtasks_exception_warns(self, caplog: Any) -> None:
        """list_subtasks 异常 → warning 留痕，不中断。"""
        svc = _TaskService()
        svc.list_subtasks = lambda pid: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        with caplog.at_level(logging.WARNING):
            has_active, ids = _run(guard._get_active_children("", "parent-task", ctx))
        assert (has_active, ids) == (False, [])
        assert any("list_subtasks failed" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# _read_state_rows
# ═══════════════════════════════════════════════════════════


class TestReadStateRows:
    def test_reader_none_returns_none(self) -> None:
        """读取器未注入 → None。"""
        guard = ChildTaskGuard(config={})
        assert _run(guard._read_state_rows()) is None

    def test_sync_reader_rows_filtered(self) -> None:
        """同步读取器返回行 → 只保留 dict 行。"""
        guard = ChildTaskGuard(config={})
        _set_state_reader(lambda: [{"pipeline_id": "a"}, "not-dict", 42])
        try:
            rows = _run(guard._read_state_rows())
            assert rows == [{"pipeline_id": "a"}]
        finally:
            _clear_state_reader()

    def test_async_reader_awaited(self) -> None:
        """异步读取器 → await 后取行。"""
        guard = ChildTaskGuard(config={})

        async def reader() -> list[dict[str, Any]]:
            return [{"pipeline_id": "a"}]

        _set_state_reader(reader)
        try:
            rows = _run(guard._read_state_rows())
            assert rows == [{"pipeline_id": "a"}]
        finally:
            _clear_state_reader()

    def test_non_list_return_returns_none(self) -> None:
        """读取器返回非 list → None（调用方回退）。"""
        guard = ChildTaskGuard(config={})
        _set_state_reader(lambda: "nope")
        try:
            assert _run(guard._read_state_rows()) is None
        finally:
            _clear_state_reader()

    def test_reader_exception_warns_and_returns_none(self, caplog: Any) -> None:
        """读取器抛异常 → warning 留痕，返回 None 降级。"""
        guard = ChildTaskGuard(config={})

        def bad() -> list[dict[str, Any]]:
            raise RuntimeError("bridge down")

        _set_state_reader(bad)
        try:
            with caplog.at_level(logging.WARNING):
                assert _run(guard._read_state_rows()) is None
            assert any("state 聚合读取失败" in r.getMessage() for r in caplog.records)
        finally:
            _clear_state_reader()

    def test_async_reader_exception_warns(self, caplog: Any) -> None:
        """异步读取器抛异常 → 同样降级 None。"""
        guard = ChildTaskGuard(config={})

        async def bad() -> list[dict[str, Any]]:
            raise RuntimeError("async down")

        _set_state_reader(bad)
        try:
            with caplog.at_level(logging.WARNING):
                assert _run(guard._read_state_rows()) is None
            assert any("state 聚合读取失败" in r.getMessage() for r in caplog.records)
        finally:
            _clear_state_reader()


# ═══════════════════════════════════════════════════════════
# _get_task_service
# ═══════════════════════════════════════════════════════════


class TestGetTaskService:
    def test_ctx_service_priority(self) -> None:
        """ctx 有 task_service → 直接返回。"""
        svc = _TaskService()
        guard = ChildTaskGuard(config={})
        ctx = _make_ctx(state={}, services={"task_service": svc})
        assert guard._get_task_service(ctx) is svc

    def test_fallback_to_service_access(self, monkeypatch: Any) -> None:
        """ctx 无 task_service → 回退 tasks.service_access.get_task_service。"""
        svc = _TaskService()
        monkeypatch.setitem(sys.modules, "tasks.service_access", types.SimpleNamespace(get_task_service=lambda: svc))
        guard = ChildTaskGuard(config={})
        assert guard._get_task_service(_make_ctx(state={})) is svc

    def test_service_access_returns_none(self, monkeypatch: Any) -> None:
        """回退接口不可用（返回 None）→ 返回 None。"""
        monkeypatch.setitem(sys.modules, "tasks.service_access", types.SimpleNamespace(get_task_service=lambda: None))
        guard = ChildTaskGuard(config={})
        assert guard._get_task_service(_make_ctx(state={})) is None

    def test_service_access_missing_module(self, monkeypatch: Any) -> None:
        """tasks.service_access 不可用（返回 None）→ 返回 None（生产兜底语义）。"""
        monkeypatch.setitem(sys.modules, "tasks.service_access", types.SimpleNamespace(get_task_service=lambda: None))
        guard = ChildTaskGuard(config={})
        assert guard._get_task_service(_make_ctx(state={})) is None


# ═══════════════════════════════════════════════════════════
# enum_utils
# ═══════════════════════════════════════════════════════════


class TestSafeEnumValue:
    def test_enum_member_extracts_value(self) -> None:
        """枚举成员 → 返回原始值。"""
        assert _ENUM_UTILS.safe_enum_value(TS.RUNNING) == "running"

    def test_non_enum_passthrough(self) -> None:
        """非枚举对象 → 原样返回（性质断言：返回值恒等于入参）。"""
        for value in ["running", 42, None, ["x"]]:
            assert _ENUM_UTILS.safe_enum_value(value) is value


# ═══════════════════════════════════════════════════════════
# server.py MCP 适配层
# ═══════════════════════════════════════════════════════════


def _load_server() -> Any:
    """显式路径加载 server.py；逐出裸名 plugin 防劫持。"""
    sys.modules.pop("plugin", None)
    mod_name = "child_task_guard_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServerAdapter:
    def test_get_instance_returns_plugin(self) -> None:
        """get_instance 懒构建 ChildTaskGuard（缓存单例）。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ChildTaskGuard)
        assert inst.name == "child_task_guard"
        assert inst is server.get_instance()

    def test_on_load_injects_state_reader(self, monkeypatch: Any) -> None:
        """on_load 经 pipeline-state capability 注入 state 聚合读取器。"""
        server = _load_server()

        class _CallableHandle:
            async def call(self, method: str, params: dict[str, Any]) -> Any:
                return [{"pipeline_id": "r1"}]

        monkeypatch.setattr(server.plugin, "get_capability", lambda name: _CallableHandle())
        _run(server._on_load({}))
        try:
            # server.py 内部 `import plugin as plugin_mod` 命中裸名模块
            reader = sys.modules["plugin"]._get_state_reader()
            assert reader is not None
            rows = _run(reader())
            assert rows == [{"pipeline_id": "r1"}]
        finally:
            sys.modules["plugin"]._state_reader = None

    def test_on_load_reader_non_list_coerced(self, monkeypatch: Any) -> None:
        """capability 返回非 list → 读取器返回空列表（不崩）。"""
        server = _load_server()

        class _Handle:
            async def call(self, method: str, params: dict[str, Any]) -> Any:
                return "not-a-list"

        monkeypatch.setattr(server.plugin, "get_capability", lambda name: _Handle())
        _run(server._on_load({}))
        try:
            reader = sys.modules["plugin"]._get_state_reader()
            assert _run(reader()) == []
        finally:
            sys.modules["plugin"]._state_reader = None

    def test_on_unload_clears_cache(self) -> None:
        """on_unload 清空单例缓存，之后可重建。"""
        server = _load_server()
        _run(server._on_unload({}))
        inst = server.get_instance()
        assert isinstance(inst, server.ChildTaskGuard)

    def test_execute_tool_dict_passthrough(self, monkeypatch: Any) -> None:
        """插件返回 dict → execute 直接透传。"""
        server = _load_server()

        class _DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"state_updates": {"submitted_task_ids": ["c1"]}}

        monkeypatch.setattr(server, "get_instance", lambda: _DictPlugin())
        data = _run(server.execute({"pipeline_id": "p"}))
        assert data == {"state_updates": {"submitted_task_ids": ["c1"]}}

    def test_execute_tool_serializes_result(self, monkeypatch: Any) -> None:
        """OutputResult 的 route_signal/skip_remaining 序列化进返回 dict。"""
        server = _load_server()
        from pipeline.plugin import OutputResult as SdkOutputResult
        from pipeline.types import RouteSignal as SdkRouteSignal

        class _StubPlugin:
            async def execute(self, ctx: Any) -> Any:
                return SdkOutputResult(
                    state_updates={"submitted_task_ids": ["c1"]},
                    route_signal=SdkRouteSignal(route_type="wait", reason="active children"),
                    skip_remaining=True,
                )

        monkeypatch.setattr(server, "get_instance", lambda: _StubPlugin())
        data = _run(server.execute({"pipeline_id": "p"}))
        assert data["state_updates"] == {"submitted_task_ids": ["c1"]}
        assert data["route_signal"] == {"route_type": "wait", "target": None, "reason": "active children"}
        assert data["skip_remaining"] is True

    def test_execute_tool_plain_result_no_optional_keys(self, monkeypatch: Any) -> None:
        """无 route_signal/skip_remaining → 返回 dict 不含对应键。"""
        server = _load_server()

        class _StubPlugin:
            async def execute(self, ctx: Any) -> Any:
                from pipeline.plugin import OutputResult as SdkOutputResult

                return SdkOutputResult(state_updates={})

        monkeypatch.setattr(server, "get_instance", lambda: _StubPlugin())
        data = _run(server.execute({"pipeline_id": "p"}))
        assert data["state_updates"] == {}
        assert "route_signal" not in data
        assert "skip_remaining" not in data

    def test_tool_registered_with_schema(self) -> None:
        """工具名与入参 schema 契约（state 必填、config 可选）。"""
        server = _load_server()
        tool_def = server.plugin._tools.get("child_task_guard.execute")
        assert tool_def is not None
        assert "state" in tool_def.schema["required"]
        assert "config" in tool_def.schema["properties"]
