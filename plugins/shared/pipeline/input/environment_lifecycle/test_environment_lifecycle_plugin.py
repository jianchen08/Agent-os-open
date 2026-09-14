# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""environment_lifecycle input 插件单元测试。

行为契约（按 current_phase 分发 init/exit，其它循环体零产出）：
1. init：environment_basis 已就位 → 幂等跳过，零产出
2. init：无 execution_context / 非 dict → 跳过
3. init：execution_context 无 isolation 声明 → 跳过
4. init：isolation level 非法/缺失 → 跳过
5. init：销毁调用方已注入 → 基线 resolved=True + service_ready=True
6. init：调用方未注入（能力缺失降级）→ 基线仍写入，service_ready=False
7. exit：无 environment_basis → 零产出
8. exit：有基线但无 task_id（主会话）→ environment_released=True，不调销毁
9. exit：调用方未注入 → environment_released=False
10. exit：销毁成功 → environment_released=True，isolation.destroy_env
    收到 state[task.id]（且不再有旧路径"协程未 await、销毁从未执行"问题）
11. exit：调用抛异常 → 留痕不阻断，environment_released=False
12. exit：返回 {"error": ...}（destroy_env 业务失败约定）→ 留痕不阻断，
    environment_released=False
13. main 循环体 → 零产出

销毁调用方为外部依赖：真实链路经 tool-executor.invoke 直达 isolation_service
sidecar（容器销毁、daemon 交互均为外部子系统）——以假 async caller 注入，
插件内部分发/降级/留痕逻辑真实执行。

[来源: plugins/shared/pipeline/input/environment_lifecycle/plugin.py]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext, PluginResult  # noqa: E402


class _FakeDestroyCaller:
    """记录销毁调用的假 caller；可注入异常与业务错误返回。"""

    def __init__(
        self,
        result: Any = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result
        self.exc = exc

    async def __call__(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool_name, dict(args)))
        if self.exc is not None:
            raise self.exc
        return self.result


def _load_plugin() -> Any:
    """唯一名动态加载 plugin.py（每次新建，隔离模块级 _destroy_caller 状态）。"""
    name = "_env_lc_plugin_ut"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


def _updates(result: PluginResult) -> dict[str, Any]:
    assert isinstance(result, PluginResult)
    assert isinstance(result.state_updates, dict)
    return result.state_updates


# ── 属性契约 ────────────────────────────────────────────────


def test_name_and_priority_contract() -> None:
    mod = _load_plugin()
    assert mod.EnvironmentLifecyclePlugin().name == "environment_lifecycle"
    assert mod.EnvironmentLifecyclePlugin().priority == 6
    assert mod.EnvironmentLifecyclePlugin(config={"priority": 3}).priority == 3


# ── 循环体分发 ──────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["", "main", "post_tool"])
def test_non_init_exit_phase_yields_empty_result(phase: str) -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    result = _run(plugin.execute(_make_ctx({"current_phase": phase})))
    assert isinstance(result, PluginResult)
    assert result.state_updates == {}


# ── init：环境基线解析 ──────────────────────────────────────


def test_init_skips_when_basis_already_present() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "environment_basis": {"level": "isolated", "resolved": True}}
    result = _run(plugin.execute(_make_ctx(state)))
    assert result.state_updates == {}


def test_init_skips_without_execution_context() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    result = _run(plugin.execute(_make_ctx({"current_phase": "init"})))
    assert result.state_updates == {}


def test_init_skips_non_dict_execution_context() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    result = _run(plugin.execute(_make_ctx({"current_phase": "init", "execution_context": "oops"})))
    assert result.state_updates == {}


def test_init_skips_without_isolation_declaration() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"agent_id": "a1"}}
    result = _run(plugin.execute(_make_ctx(state)))
    assert result.state_updates == {}


def test_init_skips_with_non_dict_isolation() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"isolation": "isolated"}}
    result = _run(plugin.execute(_make_ctx(state)))
    assert result.state_updates == {}


@pytest.mark.parametrize("bad_level", ["", "sandboxed", "container", "unknown", None])
def test_init_skips_on_invalid_isolation_level(bad_level: Any) -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"isolation": {"level": bad_level}}}
    result = _run(plugin.execute(_make_ctx(state)))
    assert result.state_updates == {}


@pytest.mark.parametrize("level", ["isolated", "non_isolated"])
def test_init_resolves_basis_with_service_ready(level: str) -> None:
    mod = _load_plugin()
    mod.set_destroy_caller(_FakeDestroyCaller())
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"isolation": {"level": level}}}
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates["environment_basis"] == {
        "level": level,
        "resolved": True,
        "service_ready": True,
    }


def test_init_degrades_without_destroy_caller() -> None:
    """调用方未注入（tool-executor 能力缺失）→ 基线仍写入，service_ready=False。"""
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"isolation": {"level": "isolated"}}}
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates["environment_basis"] == {
        "level": "isolated",
        "resolved": True,
        "service_ready": False,
    }


# ── exit：环境释放 ──────────────────────────────────────────


def test_exit_skips_without_environment_basis() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    result = _run(plugin.execute(_make_ctx({"current_phase": "exit"})))
    assert result.state_updates == {}


def test_exit_no_task_id_marks_released_without_destroy() -> None:
    mod = _load_plugin()
    caller = _FakeDestroyCaller()
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "exit", "environment_basis": {"level": "isolated", "resolved": True}}
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": True}
    assert caller.calls == []  # 主会话不销毁


def test_exit_caller_missing_marks_not_released() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-9",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": False}


def test_exit_destroys_environment_on_task_id() -> None:
    mod = _load_plugin()
    caller = _FakeDestroyCaller(result={"destroyed": True, "task_id": "task-42"})
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-42",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": True}
    # 销毁真实发生：正门工具名 + 任务身份取自 state["task.id"]；无绑定时空串透传
    assert caller.calls == [
        ("isolation.destroy_env", {"task_id": "task-42", "container_name": ""})
    ]


def test_exit_passes_state_container_name_binding() -> None:
    """D6：state 落地过容器绑定时，销毁必须带 container_name（state 真值直删，
    服务重启后内存登记为空也能删干净，杜绝登记丢失=泄漏）。"""
    mod = _load_plugin()
    caller = _FakeDestroyCaller(result={"destroyed": True, "container_name": "cua-task-42"})
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-42",
        "isolation.container_name": "cua-task-42",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": True}
    assert caller.calls == [
        ("isolation.destroy_env", {"task_id": "task-42", "container_name": "cua-task-42"})
    ]


@pytest.mark.parametrize("task_id", ["task-7", "p-abc-123"])
def test_exit_task_identity_from_flat_state_key(task_id: str) -> None:
    """任务身份一律取扁平键 task.id（0.2 任务身份 = pipeline_id）。"""
    mod = _load_plugin()
    caller = _FakeDestroyCaller(result={"destroyed": True})
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": task_id,
    }
    _updates(_run(plugin.execute(_make_ctx(state))))
    assert caller.calls == [
        ("isolation.destroy_env", {"task_id": task_id, "container_name": ""})
    ]


def test_exit_caller_raises_keeps_mark_not_released() -> None:
    """调用抛异常 → 留痕不阻断，environment_released=False。"""
    mod = _load_plugin()
    caller = _FakeDestroyCaller(exc=ConnectionError("daemon unreachable"))
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-1",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": False}
    assert len(caller.calls) == 1


def test_exit_error_payload_keeps_mark_not_released() -> None:
    """destroy_env 业务失败以 {"error": ...} 返回 → 留痕不阻断，released=False。"""
    mod = _load_plugin()
    caller = _FakeDestroyCaller(result={"error": "隔离服务未初始化"})
    mod.set_destroy_caller(caller)
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-2",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": False}
    assert len(caller.calls) == 1
