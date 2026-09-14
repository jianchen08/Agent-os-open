# @feature: FP-0.2.二 内部模块 manifest（task_manage 工具读面缺口） | @ci: python-coverage
"""task_manage 工具缺口补测（行锚定 2026-09-14 插桩车道 coverage.xml）。

断行为不断实现（断输入→输出/副作用）：
- ``_load_activity_entries``：traces 读面返回非列表 → 降级空列表 + warning；
  条目形状非 dict（trace 行 / patch_data 解析结果 / 工具调用项三层）各自跳过；
  patch_data 非法 JSON 串 → 空 dict 兜底后仍组装轮次条目；无 args 无结果的
  工具调用摘要为空串（_tool_activity_summary 的末段兜底）。
- ``_calc_elapsed_from_runs``：runs 列表中的非 dict 行跳过，其余行照常计算。
- ``_read_state_rows``：聚合行里的非 dict 元素被滤除（只把 dict 行交给消费面）。
- ``_stop_task``：挂起主流程完成后级联枚举读面故障 → warning 留痕、跳过级联，
  stop 结果仍成功（尽力而为不阻断已发生的挂起）。
- ``_delete_task``：state 任务删管道成功后清同名 YAML 镜像失败 → 非致命留痕，
  删除结果仍成功。
- tools/task/server.py ``_list_traces`` / ``_list_runs`` 闭包：经
  service-registry 按 pipeline_id 直查，返回非列表归一为空列表。

真实依赖：TaskService（tmp 数据目录）+ 真实 TaskStorage；仅 mock 跨进程
内核能力（pipeline-executor 句柄 / traces·runs 读面经 setter 注入的假 reader）。

结构性不可达（逐条说明，勿硬凑，不硬凑断言）：
1. tool.py:1392（``_resume_from_stopped`` 的 ``execution_warning`` 回填）——
   该变量在 1379 行赋 None 之后到 1391 行判定之间没有任何再赋值，
   `if execution_warning:` 恒假；这是 0.2 移除占位 capability 后的残留
   （执行由会话对话驱动，resume 不再产生告警），属死分支。
2. tool.py:202-205 的 warning 与 return 已覆盖；本文件不覆盖的第 1392 行
   以断言 `"execution_warning" not in data` 固化"resume 成功不产生该字段"
   的现状契约（把死分支的可观察后果钉住，而不是绕过）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent.parent
_PLUGIN_PATHS = tuple(
    str(_d)
    for _d in (
        _HERE,
        _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks",
        _PROJECT_ROOT / "plugins" / "shared" / "system",
        _PROJECT_ROOT / "plugins" / "shared",
    )
)
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
sys.modules.pop("tool", None)

import tool as _task_mod  # noqa: E402
from tool import TaskTool  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：路径重插 + service/http_api 槽位快照还原（同既有测试文件）。"""
    for _d in _PLUGIN_PATHS:
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    _saved = {n: sys.modules.get(n) for n in ("service", "http_api")}
    for n in _saved:
        sys.modules.pop(n, None)
    saved_tool = sys.modules.get("tool")
    sys.modules["tool"] = _task_mod
    try:
        yield
    finally:
        for n, m in _saved.items():
            sys.modules.pop(n, None)
            if m is not None:
                sys.modules[n] = m
        sys.modules.pop("tool", None)
        if saved_tool is not None:
            sys.modules["tool"] = saved_tool


@pytest.fixture(autouse=True)
def _capabilities():
    """能力全局注入点还原（用例会经 setter 改写）。"""
    prev = (
        _task_mod._chat_sender,
        _task_mod._state_reader,
        _task_mod._pipeline_executor,
        _task_mod._traces_reader,
        _task_mod._runs_reader,
    )
    yield
    (
        _task_mod._chat_sender,
        _task_mod._state_reader,
        _task_mod._pipeline_executor,
        _task_mod._traces_reader,
        _task_mod._runs_reader,
    ) = prev


@pytest.fixture
def svc(tmp_path: Path) -> Any:
    """真实 TaskService（tmp 数据目录，每用例隔离）。"""
    from service import TaskService

    return TaskService(data_dir=str(tmp_path))


@pytest.fixture
def tool(svc: Any) -> TaskTool:
    t = TaskTool()
    t._task_service = svc
    return t


def _state_row(pid: str, status: str = "running") -> dict[str, Any]:
    """典型任务管道聚合行（内核 STATE_SUMMARY_KEYS 白名单形状）。"""
    return {
        "pipeline_id": pid,
        "task.status": status,
        "task.goal": "目标",
        "task.submitted_by": "u1",
        "lineage.origin_session_id": f"sess-{pid}",
        "thread_id": f"thread-{pid}",
    }


# ═══════════════════════════════════════════════════════════
# _load_activity_entries：形状违约与脏 patch_data
# ═══════════════════════════════════════════════════════════


async def test_activity_reader_non_list_degrades_with_warning(
    tool: TaskTool, caplog: pytest.LogCaptureFixture
) -> None:
    """traces 读面返回非列表 → 空活动列表 + warning（不阻断任务主数据）。"""
    import logging

    async def reader(_pipeline_id: str) -> Any:
        return {"rows": []}

    _task_mod.set_traces_reader(reader)
    with caplog.at_level(logging.WARNING):
        assert await tool._load_activity_entries("pipe-nonlist") == []
    assert any("非列表形态" in r.getMessage() for r in caplog.records)


async def test_activity_entries_skip_malformed_shapes(tool: TaskTool) -> None:
    """三层形状违约各自跳过：非 dict trace 行 / patch_data 非 dict / 非 dict 工具调用项。

    同一列表里另有合法轮次 → 只产出合法条目（跳过不是整体拒收）。
    """
    traces = [
        "junk-trace",  # 非 dict 行 → 跳过
        {
            "plugin_id": "core",
            "patch_data": "not-json{",  # 非法 JSON 串 → 空 dict 兜底 → 无工具轮次
            "created_at": "2026-09-14T10:00:01",
        },
        {
            "plugin_id": "core",
            "patch_data": [1, 2, 3],  # 解析结果非 dict → 跳过
            "created_at": "2026-09-14T10:00:02",
        },
        {
            "plugin_id": "core",
            "patch_data": {
                "_executed_tool_calls": ["junk-call", {"name": "bash"}],
                "tool_results": [],
            },
            "created_at": "2026-09-14T10:00:03",
        },
    ]

    async def reader(_pipeline_id: str) -> Any:
        return traces

    _task_mod.set_traces_reader(reader)
    entries = await tool._load_activity_entries("pipe-shapes")

    assert [e["action"] for e in entries] == ["thinking", "bash"]
    assert [e["iteration"] for e in entries] == [1, 2], "非 dict 行/调用项不占轮次"
    # 无 args、无 tool_result 的工具调用 → 摘要空串（末段兜底而非抛错）
    assert entries[1]["summary"] == ""
    # 非法 JSON 兜底后的轮次按无工具处理（llm_usage 缺失 → 通用文案）
    assert entries[0]["summary"] == "LLM 轮次"


async def test_activity_summary_falls_back_to_empty_for_shape_violations() -> None:
    """纯函数面：结果与调用项都无可用内容 → 空串（两路输入形状各验一次）。"""
    assert TaskTool._tool_activity_summary(None, {}) == ""
    assert TaskTool._tool_activity_summary({"metadata": "not-a-dict"}, {"arguments": {}}) == ""


# ═══════════════════════════════════════════════════════════
# 读面形状：非 dict 行滤除
# ═══════════════════════════════════════════════════════════


async def test_read_state_rows_filters_non_dict_elements(tool: TaskTool) -> None:
    """聚合行里的非 dict 元素被滤除，dict 行原样交给消费面。"""

    def reader() -> Any:
        return [{"pipeline_id": "p1"}, "junk", 42, {"pipeline_id": "p2"}]

    _task_mod.set_state_reader(reader)
    assert await tool._read_state_rows() == [{"pipeline_id": "p1"}, {"pipeline_id": "p2"}]


async def test_elapsed_from_runs_skips_non_dict_rows(tool: TaskTool) -> None:
    """runs 列表中的非 dict 行跳过；其余行仍产出耗时（起点为最早 created_at）。"""

    def reader(_pipeline_id: str) -> Any:
        return [
            "junk-run",
            {
                "run_id": "r1",
                "status": "running",
                "created_at": "2000-01-01T00:00:00+00:00",
            },
        ]

    _task_mod.set_runs_reader(reader)
    elapsed = await tool._calc_elapsed_from_runs("pipe-runs")
    assert elapsed is not None
    assert elapsed > 0, "非 dict 行不得吞掉合法 run 的起点"


# ═══════════════════════════════════════════════════════════
# _stop_task：级联枚举读面故障不阻断已发生的挂起
# ═══════════════════════════════════════════════════════════


async def test_stop_survives_cascade_enumeration_read_failure(
    tool: TaskTool, caplog: pytest.LogCaptureFixture
) -> None:
    """挂起管道成功后级联读面故障 → warning + 跳过级联，stop 结果保持成功。

    读面替身按调用序：首次（取目标任务）返回聚合行，其后（级联枚举）抛
    StateRowsReadError（桥内故障语义）——主流程（任务已挂起）不回卷。
    """
    import logging

    calls = {"n": 0}

    async def reader() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return [_state_row("pipe-stop")]
        raise _task_mod.StateRowsReadError("桥故障")

    suspended: list[str] = []

    async def executor(params: dict[str, Any]) -> dict[str, Any]:
        suspended.append(params["params"]["pipeline_id"])
        return {"ok": True}

    tool._read_state_rows = reader  # type: ignore[method-assign]
    _task_mod.set_pipeline_executor(executor)

    with caplog.at_level(logging.WARNING):
        result = await tool._stop_task({"task_id": "pipe-stop"}, 1)

    assert result.success, result.error
    assert result.output["stopped"] is True
    assert result.output["new_status"] == "suspended"
    assert "cascaded_subtasks" not in result.output, "读面故障不得谎报级联成功"
    assert suspended == ["pipe-stop"], "仅目标管道被挂起（级联枚举已跳过）"
    assert any("级联子管道枚举读取失败" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# _delete_task：YAML 镜像清理失败非致命
# ═══════════════════════════════════════════════════════════


async def test_delete_state_task_yaml_mirror_cleanup_failure_non_fatal(
    tool: TaskTool, svc: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """state 任务：删管道成功后清 YAML 镜像失败 → warning 留痕，删除仍成功。"""
    import logging

    async def reader() -> Any:
        return [_state_row("pipe-del", status="completed")]

    deleted: list[str] = []

    async def executor(params: dict[str, Any]) -> dict[str, Any]:
        deleted.append(params["params"]["pipeline_id"])
        return {"ok": True}

    async def boom(_task_id: str) -> bool:
        raise RuntimeError("yaml mirror locked")

    tool._read_state_rows = reader  # type: ignore[method-assign]
    _task_mod.set_pipeline_executor(executor)
    monkeypatch.setattr(svc, "hard_delete", boom)

    with caplog.at_level(logging.WARNING):
        result = await tool._delete_task({"task_id": "pipe-del"}, 1)

    assert result.success, result.error
    assert result.output == {"task_id": "pipe-del", "deleted": True}
    assert deleted == ["pipe-del"], "管道数据删除是主流程（已发生）"
    assert any("清理 YAML 镜像失败" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# resume 现状契约：成功结果不带 execution_warning（死分支的可观察后果）
# ═══════════════════════════════════════════════════════════


async def test_resume_success_reports_no_execution_warning(tool: TaskTool, svc: Any) -> None:
    """stopped 任务恢复成功 → resumed/新状态正确，且结果不含 execution_warning。"""
    from task_types import TaskModel, TaskStatus

    calls: list[str] = []

    async def executor(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["method"])
        return {"ok": True}

    _task_mod.set_pipeline_executor(executor)
    task = TaskModel(id="pipe-resume", title="恢复", status=TaskStatus.STOPPED, pipeline_run_id="pipe-resume")
    svc.storage.save(task)

    result = await tool._resume_from_stopped(task, "", svc, 1, {})

    assert result.success, result.error
    assert result.output["resumed"] is True
    assert result.output["new_status"] == "running"
    assert "execution_warning" not in result.output
    assert calls == ["resume_pipeline"]


# ═══════════════════════════════════════════════════════════
# tools/task/server.py：service-registry 读面闭包
# ═══════════════════════════════════════════════════════════


class _RecordingCapability:
    """service-registry 能力句柄替身：记录调用并按预置值回包。"""

    def __init__(self, value: Any) -> None:
        self._value = value
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: Any = None) -> Any:
        self.calls.append((method, params))
        return self._value


def _load_tool_server() -> Any:
    """按显式路径加载 tools/task/server.py（唯一模块名隔离同名 server.py）。"""
    mod_name = "task_manage_server_gaps_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(_HERE / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServiceRegistryReaders:
    @pytest.mark.parametrize(
        ("reader_attr", "method", "value", "expected"),
        [
            ("_traces_reader", "traces.list_by_pipeline", [{"trace_id": "t1"}], [{"trace_id": "t1"}]),
            ("_runs_reader", "pipeline-runs.list_by_pipeline", [{"run_id": "r1"}], [{"run_id": "r1"}]),
            ("_traces_reader", "traces.list_by_pipeline", {"rows": []}, []),
            ("_runs_reader", "pipeline-runs.list_by_pipeline", "junk", []),
        ],
    )
    async def test_reader_queries_service_registry_by_pipeline(
        self, reader_attr: str, method: str, value: Any, expected: list[dict[str, Any]]
    ) -> None:
        """on_load 接线后：traces/runs 读面按 pipeline_id 直查并归一列表形态。"""
        srv = _load_tool_server()
        handle = _RecordingCapability(value)
        srv.plugin._capabilities = {"service-registry": handle}

        await srv._on_load({})
        rows = await getattr(_task_mod, reader_attr)("pipe-1")

        assert rows == expected
        assert handle.calls == [(method, {"pipeline_id": "pipe-1"})]
