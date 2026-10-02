# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""Python 簇 H 缺口补测（triggers_ext 触发器族）——coverage.xml 2026-09-14 口径缺行。

靶单（逐条对应下方测试）：
- triggers/manager.py（循环内落盘的 done 回调：落盘任务异常 → error 留痕；
  非循环上下文且主循环缺失/已关闭 → 暂缓落盘警告）；
- triggers/types.py 206（to_state_dict 容器字段 None → {} 兜底）；
- tool.py 529（update 命中已取消触发器 → TRIGGER_UPDATE_FAILED）;
- server.py 109（on_load 重灌成功分支）、132（域事件缺 event 名 → 早退）;
- http_api.py 27（首次导入时把 plugins/shared 推入 sys.path）。

（原条件求值异常包含/条件轮询注销竞态/轮询循环包含/解析器未知节点四簇
随 CONDITION 求值上收内核 trigger-svc 的退休链路删除，M1。）

外部依赖替身：state 读写面用假件（闭包），主循环用独立线程事件循环
（既有 _LoopThread 同款）；事件驱动等待，不空转真等待。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import triggers.manager as manager_mod  # noqa: E402
from triggers.manager import TriggerManager  # noqa: E402
from triggers.types import TriggerConfig, TriggerStatus, TriggerType  # noqa: E402

_SHARED_ROOT = os.path.abspath(os.path.join(str(_PLUGIN_DIR), "..", ".."))


def _load_module(mod_name: str, filename: str, evict_bare: tuple[str, ...] = ()) -> Any:
    """按唯一模块名装载插件内模块（逐出同名裸名缓存，防跨插件串扰）。"""
    import importlib.util

    if mod_name in sys.modules:
        del sys.modules[mod_name]
    for bare in evict_bare:
        sys.modules.pop(bare, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _make_config(**overrides: Any) -> TriggerConfig:
    base: dict[str, Any] = {
        "trigger_id": "t1",
        "name": "测试触发器",
        "trigger_type": TriggerType.CONDITION,
        "condition_expression": "ready == true",
        "max_fires": 0,
        "message": "到点了",
        "pipeline_id": "pipe-1",
        "metadata": {"register_time": "2026-09-14T00:00:00+00:00"},
    }
    base.update(overrides)
    return TriggerConfig(**base)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _LoopThread:
    """独立线程运行事件循环（供 persist 的 run_coroutine_threadsafe 使用）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> _LoopThread:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


# ═══════════════════════════════════════════════════════════
# manager.evaluate_condition_rows：求值异常包含（协作模块故障注入）
# ═══════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════
# manager.persist：循环内调度 / 主循环不可用 / done 回调
# ═══════════════════════════════════════════════════════════


class TestPersistLoopBranches:
    """persist 的三条执行上下文分支。"""

    async def test_loop_context_write_failure_logged_by_done_callback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """659-661：循环内调度的落盘任务失败 → done 回调留 error，不炸调用方。

        事件驱动等待（gather 本回合新调度的任务），不睡真时间。
        """
        async def failing_writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            raise RuntimeError("state store down")

        mgr = TriggerManager()
        mgr.set_state_writer(failing_writer)
        config = _make_config(trigger_id="t-persist", pipeline_id="pipe-9")

        with caplog.at_level(logging.ERROR):
            before = asyncio.all_tasks()
            mgr.persist(config)  # 循环内：调度后台任务而非阻塞等待
            scheduled = asyncio.all_tasks() - before
            assert scheduled, "循环内 persist 必须调度后台落盘任务"
            await asyncio.gather(*scheduled, return_exceptions=True)
            await asyncio.sleep(0)  # done 回调在任务完成后经 call_soon 执行

        messages = [r.getMessage() for r in caplog.records]
        assert any("触发器 state 落盘异常" in m for m in messages)
        assert any("t-persist" in m for m in messages)

    async def test_loop_context_write_success_logs_no_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """对照支：落盘成功 → done 回调不产生 error（守卫条件成立与否的区别）。"""
        written: list[tuple[str, dict[str, Any]]] = []

        async def ok_writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            written.append((pipeline_id, fields))

        mgr = TriggerManager()
        mgr.set_state_writer(ok_writer)
        config = _make_config(trigger_id="t-ok", pipeline_id="pipe-9")

        with caplog.at_level(logging.ERROR):
            before = asyncio.all_tasks()
            mgr.persist(config)
            scheduled = asyncio.all_tasks() - before
            await asyncio.gather(*scheduled, return_exceptions=True)
            await asyncio.sleep(0)

        assert written
        assert written[0][0] == "pipe-9"
        assert not any("落盘异常" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("loop_state", ["absent", "closed"], ids=["no-main-loop", "closed-loop"])
    def test_write_deferred_when_main_loop_unusable(
        self, caplog: pytest.LogCaptureFixture, loop_state: str
    ) -> None:
        """674,678：非循环上下文 + 主循环缺失/已关闭 → 警告暂缓落盘，内存注册仍成立。"""
        calls: list[str] = []

        async def writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            calls.append(pipeline_id)

        mgr = TriggerManager()
        mgr.set_state_writer(writer)
        if loop_state == "closed":
            closed = asyncio.new_event_loop()
            closed.close()
            mgr.set_main_loop(closed)
        config = _make_config(trigger_id="t-deferred", pipeline_id="pipe-2")

        with caplog.at_level(logging.WARNING):
            mgr.persist(config)  # 无运行循环：不得抛异常

        messages = [r.getMessage() for r in caplog.records]
        assert any("state 暂缓落盘" in m for m in messages)
        assert any("t-deferred" in m for m in messages)
        assert calls == [], "主循环不可用时不得触达写面"


# ═══════════════════════════════════════════════════════════
# manager._poll_conditions：评估期间注销 / 检查循环包含性
# ═══════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════
# types / condition_parser：容器兜底与未知节点
# ═══════════════════════════════════════════════════════════


class TestStateDictContainerFallback:
    """types.py 206：容器字段为 None（腐败 state 值）时序列化兜底为 {}。"""

    @pytest.mark.parametrize(
        ("nulls", "expect_keys"),
        [
            (("metadata",), ("metadata",)),
            (("event_filter", "action_params"), ("event_filter", "action_params")),
            (("metadata", "event_filter", "action_params"), ("metadata", "event_filter", "action_params")),
        ],
        ids=["metadata-only", "filters-only", "all-containers"],
    )
    def test_null_containers_become_empty_dicts(self, nulls: tuple[str, ...], expect_keys: tuple[str, ...]) -> None:
        """三组区分度输入：任一/多个容器为 None 均收敛为 {}。

        性质断言：全部容器字段序列化后恒为 dict（前端/state 读写面契约）。
        """
        payload: dict[str, Any] = {"trigger_id": "t-null", "trigger_type": "condition", "status": "active"}
        for key in nulls:
            payload[key] = None
        config = TriggerConfig.from_state_dict(payload)
        for key in expect_keys:
            assert getattr(config, key) is None, "还原期保留 null 现状（不在读侧臆造）"

        state = config.to_state_dict()
        for key in ("event_filter", "action_params", "metadata"):
            assert isinstance(state[key], dict), f"{key} 序列化必须为 dict"
        assert state["trigger_id"] == "t-null"

    def test_populated_containers_survive_round_trip(self) -> None:
        """对照支：非空容器原样往返（兜底不覆盖真实内容）。"""
        config = _make_config(metadata={"user_id": "u1"}, action_params={"cmd": ["echo"]})
        state = config.to_state_dict()
        assert state["metadata"]["user_id"] == "u1"
        assert state["action_params"]["cmd"] == ["echo"]



# ═══════════════════════════════════════════════════════════
# tool.py：update 命中已取消触发器
# ═══════════════════════════════════════════════════════════


class TestTriggerSetupToolUpdate:
    """tool.py 529：update 的目标触发器已取消 → TRIGGER_UPDATE_FAILED。"""

    @pytest.fixture
    def tool(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        tool_mod = _load_module("triggers_ext_tool_cluster_h", "tool.py")
        fresh = TriggerManager()
        monkeypatch.setattr(tool_mod, "get_trigger_manager", lambda: fresh)
        instance = tool_mod.TriggerSetupTool()
        try:
            yield instance
        finally:
            fresh.stop_check_loop()

    def test_update_cancelled_trigger_fails_visibly(self, tool: Any) -> None:
        """先 cancel（状态置 CANCELLED 且仍在表内）再 update → 明确失败，不静默成功。"""
        setup = _run(tool.execute({
            "trigger_type": "delay", "delay_seconds": 600, "message": "m", "pipeline_id": "p-1",
        }))
        assert setup.success
        trigger_id = setup.output["trigger_id"]
        assert tool._manager.cancel(trigger_id) is True

        result = _run(tool.execute({"action": "update", "trigger_id": trigger_id, "max_count": 5}))
        assert not result.success
        assert result.error_code == "TRIGGER_UPDATE_FAILED"
        assert trigger_id in result.error

    def test_update_active_trigger_succeeds(self, tool: Any) -> None:
        """对照支：未取消的触发器 update 成功且反映新旧值（区分"失败"非普遍行为）。"""
        setup = _run(tool.execute({
            "trigger_type": "delay", "delay_seconds": 600, "message": "m", "pipeline_id": "p-1",
        }))
        trigger_id = setup.output["trigger_id"]
        result = _run(tool.execute({"action": "update", "trigger_id": trigger_id, "max_count": 7}))
        assert result.success
        assert result.output["new_max_fires"] == 7


# ═══════════════════════════════════════════════════════════
# server.py：on_load 重灌成功 / 域事件缺 event 名
# ═══════════════════════════════════════════════════════════


class _StateCapability:
    """pipeline-state capability 替身：list 返回预设聚合行。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        assert method == "list"
        return self._rows


class TestServerLifecycleBranches:
    """server.py on_load 重灌成功分支与域事件早退守卫。"""

    @pytest.fixture
    def clean_manager(self) -> Any:
        mgr = manager_mod.get_trigger_manager()
        mgr.stop_check_loop()
        for config in mgr.list_all():
            mgr.unregister(config.trigger_id)
        yield mgr
        mgr.stop_check_loop()
        for config in mgr.list_all():
            mgr.unregister(config.trigger_id)

    def test_on_load_logs_restored_count_from_state(
        self, clean_manager: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """109：state 桥可用 → 重灌成功并记录数量（不再落 except 分支）。"""
        rows = [
            {"pipeline_id": "p-1", "task.trigger.registry.a": {
                "trigger_id": "a", "trigger_type": "condition", "status": "active",
                "condition_expression": "ready == true",
            }},
            {"pipeline_id": "p-2", "task.trigger.registry.b": {
                "trigger_id": "b", "trigger_type": "event", "status": "active", "event_name": "x",
            }},
        ]
        server = _load_module("triggers_ext_server_cluster_h", "server.py", ("tool", "http_api", "server"))
        monkeypatch.setattr(server.plugin, "get_capability", lambda _name: _StateCapability(rows))

        loop = asyncio.new_event_loop()
        try:
            with caplog.at_level(logging.INFO):
                loop.run_until_complete(server._on_load({}))
        finally:
            loop.run_until_complete(server._on_unload({}))
            loop.close()

        messages = [r.getMessage() for r in caplog.records]
        assert any("state 触发器重灌完成: 2 个" in m for m in messages)
        assert sorted(c.trigger_id for c in clean_manager.list_all()) == ["a", "b"]

    def test_on_domain_event_without_event_name_is_noop(self, clean_manager: Any) -> None:
        """132：params 缺 event 名 → 早退，不评估任何触发器（fire_count 不变）。"""
        server = _load_module("triggers_ext_server_cluster_h2", "server.py", ("tool", "http_api", "server"))
        config = _make_config(
            trigger_id="t-completed", trigger_type=TriggerType.EVENT,
            condition_expression="", event_name="task_completed", max_fires=5,
        )
        clean_manager.register(config)
        try:
            assert _run(server._on_domain_event({"pipeline_id": "p-1"})) is None
            assert config.fire_count == 0

            # 对照：带 event 名时同一触发器正常命中（证明早退是 event 名守卫）
            _run(server._on_domain_event({"event": "task_completed", "pipeline_id": "p-1"}))
            assert config.fire_count == 1
        finally:
            clean_manager.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# http_api.py：首次导入的共享根 sys.path 注入
# ═══════════════════════════════════════════════════════════


class TestHttpApiSharedRootInjection:
    """http_api.py 27：共享根不在 sys.path 时由模块自身注入（其后裸名导入成立）。"""

    def test_shared_root_pushed_to_sys_path_on_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """先摘掉共享根再装载 → 模块执行期把它插回并完成 http_json/kernel_token 导入。"""
        pruned = [p for p in sys.path if os.path.abspath(p or ".") != _SHARED_ROOT]
        assert len(pruned) < len(sys.path), "前置条件：共享根此前在 sys.path 内"
        monkeypatch.setattr(sys, "path", pruned)

        module = _load_module("triggers_ext_http_api_cluster_h", "http_api.py", ("tool", "http_api", "server"))

        assert _SHARED_ROOT in sys.path, "模块导入必须把共享根推回 sys.path"
        assert module.PREFIX == "/ext/trigger_setup_tool/triggers"
        assert callable(module.handle_http_dispatch)

    def test_existing_shared_root_entry_not_duplicated(self) -> None:
        """对照支：共享根已在 sys.path → 装载不产生重复条目（幂等注入）。"""
        if _SHARED_ROOT not in sys.path:
            sys.path.insert(0, _SHARED_ROOT)
        _load_module("triggers_ext_http_api_cluster_h2", "http_api.py", ("tool", "http_api", "server"))
        assert sum(1 for p in sys.path if os.path.abspath(p or ".") == _SHARED_ROOT) == 1
