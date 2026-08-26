# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""environment_lifecycle input 插件单元测试。

行为契约（按 current_phase 分发 init/exit，其它循环体零产出）：
1. init：environment_basis 已就位 → 幂等跳过，零产出
2. init：无 execution_context / 非 dict → 跳过
3. init：execution_context 无 isolation 声明 → 跳过
4. init：isolation level 非法/缺失 → 跳过
5. init：level=isolated/non_isolated 且服务可用 → 基线 resolved=True + service_ready=True
6. init：服务实例化失败降级 → 基线仍写入，service_ready=False
7. init：服务调用参数透传（config_path 传入 IsolationManager）——真实隔离包路径
8. exit：无 environment_basis → 零产出
9. exit：有基线但无 task_id（主会话）→ environment_released=True，不销毁
10. exit：manager 不可用 → environment_released=False
11. exit：销毁成功 → environment_released=True，task_id 透传（task_id / task.id 双键）
12. exit：销毁抛异常 → 留痕不阻断，environment_released=False
13. main 循环体 → 零产出

[疑似产品 bug，2026-08-26 现状断言]：`_release` 经 `ctx_await` 在
`run_in_executor` 线程池里执行 `manager.destroy_by_task_id`，而真实
`IsolationManager.destroy_by_task_id` 是 **async** 方法——线程池回调只负责
「调用」得到未 await 的协程对象，无人 await。可观察结果：exit 时销毁**从未
真正执行**，且 `environment_released` 恒为 True（协程泄漏产生 RuntimeWarning，
仅真实销毁中抛出的异常可能被留痕）。本文件按现状断言（destroy 记录为空），
修复属产品职责。

IsolationManager 为外部依赖：真实模块在 plugins/shared/system/isolation/，
构造/销毁需容器与配置中心，属外部子系统——以模块级假实现注入
（sys.modules 替换 `isolation.manager` 包路径），内部插件逻辑真实执行。
__init__ 阶段不落盘，写日志可中断（测试内随测试日志可见）。

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


class _FakeIsolationManager:
    """记录构造/销毁调用的假 IsolationManager（duck-typed）。"""

    instances: list["_FakeIsolationManager"] = []

    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path
        self.destroyed: list[tuple[str, bool]] = []
        _FakeIsolationManager.instances.append(self)

    async def destroy_by_task_id(self, task_id: str, success: bool = True) -> None:
        self.destroyed.append((task_id, success))


def _install_fake_isolation() -> None:
    """注入假 isolation.manager 模块，锁住真实系统插件导入。

    覆写 sys.modules["isolation"/"isolation.manager"]——由 autouse fixture
    _restore_isolation_modules 在每用例后还原原条目，否则共跑车里后续
    真 isolation 测试（isolation_guard/container_landing 等）全部被毒化。
    """
    fake_pkg = type(sys)("isolation")
    fake_mod = type(sys)("isolation.manager")
    fake_mod.IsolationManager = _FakeIsolationManager
    sys.modules["isolation"] = fake_pkg
    sys.modules["isolation.manager"] = fake_mod


@pytest.fixture(autouse=True)
def _restore_isolation_modules():
    """还原被 _install_fake_isolation 覆写的 isolation 模块槽位。"""
    saved = {n: sys.modules.get(n) for n in ("isolation", "isolation.manager")}
    try:
        yield
    finally:
        for n in ("isolation.manager", "isolation"):
            sys.modules.pop(n, None)
            mod = saved.get(n)
            if mod is not None:
                sys.modules[n] = mod


def _load_plugin() -> Any:
    """唯一名动态加载 plugin.py（每次新建，隔离模块级状态）。"""
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
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "init", "execution_context": {"isolation": {"level": level}}}
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    basis = updates["environment_basis"]
    assert basis == {"level": level, "resolved": True, "service_ready": True}
    assert len(_FakeIsolationManager.instances) == 1
    assert _FakeIsolationManager.instances[0].config_path is None


def test_init_degrades_when_service_instantiation_fails() -> None:
    mod = _load_plugin()

    class _Boom:
        def __init__(self, config_path: str | None = None) -> None:
            raise RuntimeError("backend down")

    _install_fake_isolation()
    fake_manager = sys.modules["isolation.manager"]
    original = fake_manager.IsolationManager
    fake_manager.IsolationManager = _Boom  # type: ignore[attr-defined]
    try:
        plugin = mod.EnvironmentLifecyclePlugin()
        state = {"current_phase": "init", "execution_context": {"isolation": {"level": "isolated"}}}
        updates = _updates(_run(plugin.execute(_make_ctx(state))))
        basis = updates["environment_basis"]
        assert basis == {"level": "isolated", "resolved": True, "service_ready": False}
        assert plugin._manager is None
    finally:
        fake_manager.IsolationManager = original


def test_init_config_path_forwarded_to_manager() -> None:
    mod = _load_plugin()
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin(config={"config_path": "isolation/isolation_config.yaml"})
    state = {"current_phase": "init", "execution_context": {"isolation": {"level": "isolated"}}}
    _updates(_run(plugin.execute(_make_ctx(state))))
    assert _FakeIsolationManager.instances[0].config_path == "isolation/isolation_config.yaml"


def test_init_manager_lazy_single_instance() -> None:
    mod = _load_plugin()
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin()
    state_a = {"current_phase": "init", "execution_context": {"isolation": {"level": "isolated"}}}
    state_b = {"current_phase": "init", "execution_context": {"isolation": {"level": "non_isolated"}}}
    _run(plugin.execute(_make_ctx(state_a)))
    _run(plugin.execute(_make_ctx(state_b)))
    assert len(_FakeIsolationManager.instances) == 1


# ── exit：环境释放 ──────────────────────────────────────────


def test_exit_skips_without_environment_basis() -> None:
    mod = _load_plugin()
    plugin = mod.EnvironmentLifecyclePlugin()
    result = _run(plugin.execute(_make_ctx({"current_phase": "exit"})))
    assert result.state_updates == {}


def test_exit_no_task_id_marks_released_without_destroy() -> None:
    mod = _load_plugin()
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {"current_phase": "exit", "environment_basis": {"level": "isolated", "resolved": True}}
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": True}
    assert _FakeIsolationManager.instances == []  # 主会话不销毁


def test_exit_destroys_environment_on_task_id() -> None:
    mod = _load_plugin()
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task_id": "task-42",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": True}
    # 现状断言（疑似产品 bug，见模块 docstring）：ctx_await 在线程池里调用
    # async destroy_by_task_id，协程从未被 await，销毁实际未执行。
    assert _FakeIsolationManager.instances[0].destroyed == []


def test_exit_task_dot_id_key_accepted() -> None:
    mod = _load_plugin()
    _install_fake_isolation()
    _FakeIsolationManager.instances.clear()
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "task-7",
    }
    _run(plugin.execute(_make_ctx(state)))
    # 现状断言：与 test_exit_destroys_environment_on_task_id 相同（销毁未执行）
    assert _FakeIsolationManager.instances[0].destroyed == []


def test_exit_manager_unavailable_marks_not_released() -> None:
    mod = _load_plugin()

    class _NoneManager:
        def __init__(self, config_path: str | None = None) -> None:
            raise RuntimeError("no backend")

    _install_fake_isolation()
    fake_manager = sys.modules["isolation.manager"]
    original = fake_manager.IsolationManager
    fake_manager.IsolationManager = _NoneManager  # type: ignore[attr-defined]
    try:
        plugin = mod.EnvironmentLifecyclePlugin()
        state = {
            "current_phase": "exit",
            "environment_basis": {"level": "isolated", "resolved": True},
            "task_id": "task-9",
        }
        updates = _updates(_run(plugin.execute(_make_ctx(state))))
        assert updates == {"environment_released": False}
    finally:
        fake_manager.IsolationManager = original


def test_exit_destroy_failure_keeps_mark_not_released() -> None:
    mod = _load_plugin()
    _install_fake_isolation()

    class _FailingManager:
        """同步抛错实现——覆盖 ctx_await 线程池内异常留痕分支（164-170 行）。"""

        instances: list["_FailingManager"] = []

        def __init__(self, config_path: str | None = None) -> None:
            self.called = 0
            _FailingManager.instances.append(self)

        def destroy_by_task_id(self, task_id: str, success: bool = True) -> None:
            self.called += 1
            raise ConnectionError("daemon unreachable")

    sys.modules["isolation.manager"].IsolationManager = _FailingManager  # type: ignore[attr-defined]
    plugin = mod.EnvironmentLifecyclePlugin()
    state = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task_id": "task-1",
    }
    updates = _updates(_run(plugin.execute(_make_ctx(state))))
    assert updates == {"environment_released": False}
    assert _FailingManager.instances[0].called == 1
