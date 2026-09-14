# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""rollback 缺口分支补测（根级，供插桩基集登记）。

覆盖目标（行号语义经源码逐行确认）：
- server.py:52-53（on_unload 清空全局 manager）、60（_ensure_manager 冷启动
  惰性构造）、95-102（rollback.create_checkpoint 工具面端到端落账）、
  206-212（rollback.execute 工具面端到端回滚并 to_dict）
- manager.py:222-224（list_operations 按 checkpoint 过滤：命中时只留其后
  操作）、354-355（置 ROLLING_BACK 令牌失败 → 拒绝执行，不落双回滚）、
  404-407（get_rollback_manager 全局单例惰性构造与复用）

真实依赖：manager/reversers 走进程内真实实现（FileReverser 对真实
tmp_path 文件生效）；仅「状态落账失败」这一数据库边界用替身脚本化。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLLBACK_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "rollback"


def _load(name: str) -> Any:
    """按文件路径加载 rollback 模块（平铺 import，同名槽位先逐出）。"""
    for mod in ("models", "reversers", "manager", "server"):
        sys.modules.pop(mod, None)
    if str(_ROLLBACK_DIR) not in sys.path:
        sys.path.insert(0, str(_ROLLBACK_DIR))
    spec = importlib.util.spec_from_file_location(name, _ROLLBACK_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if name == "server":
        sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def manager_mod() -> Any:
    return _load("manager")


@pytest.fixture()
def server_mod() -> Any:
    module = _load("server")
    module._manager = None
    yield module
    module._manager = None


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# server.py：生命周期 / 惰性单例 / 两个工具面端到端
# ═══════════════════════════════════════════════════════════


class TestServerLifecycle:
    async def test_on_load_creates_manager(self, server_mod: Any) -> None:
        await server_mod._on_load({})
        assert server_mod._manager is not None
        assert type(server_mod._manager).__name__ == "RollbackManager"

    async def test_on_unload_clears_manager(self, server_mod: Any) -> None:
        """on_unload 清空全局 manager（52-53），随后 _ensure_manager 重建。"""
        await server_mod._on_load({})
        first = server_mod._manager
        assert first is not None

        await server_mod._on_unload({})
        assert server_mod._manager is None

        rebuilt = server_mod._ensure_manager()
        assert rebuilt is not None and rebuilt is not first

    def test_ensure_manager_lazy_constructs(self, server_mod: Any) -> None:
        """未 load 即调用 → 惰性构造（60），二次取用同一实例。"""
        assert server_mod._manager is None
        first = server_mod._ensure_manager()
        assert type(first).__name__ == "RollbackManager"
        assert server_mod._ensure_manager() is first


class TestServerToolFaces:
    async def test_create_checkpoint_lands_and_is_retrievable(
        self, server_mod: Any
    ) -> None:
        """rollback.create_checkpoint：返回 id 且经 manager 可查（95-102）。"""
        server_mod._manager = None
        response = await server_mod.rollback_create_checkpoint(
            task_id="t-cp",
            name="里程碑",
            description="回滚点",
            metadata={"round": 1},
        )
        checkpoint_id = response["checkpoint_id"]
        assert checkpoint_id

        checkpoints = await server_mod._manager.list_checkpoints("t-cp")
        assert [cp.id for cp in checkpoints] == [checkpoint_id]
        assert checkpoints[0].name == "里程碑"
        assert checkpoints[0].metadata == {"round": 1}

    async def test_create_checkpoint_optional_args_default(
        self, server_mod: Any
    ) -> None:
        """仅传 task_id：可选参数缺省仍成功（有区分度输入）。"""
        response = await server_mod.rollback_create_checkpoint(task_id="t-min")
        checkpoints = await server_mod._manager.list_checkpoints("t-min")
        assert [cp.id for cp in checkpoints] == [response["checkpoint_id"]]
        assert checkpoints[0].description is None

    async def test_execute_rolls_back_recorded_operations(
        self, server_mod: Any, tmp_path: Path
    ) -> None:
        """rollback.execute：记录操作 → 回滚 → to_dict 结果（206-212）。"""
        target = tmp_path / "artifact.txt"
        target.write_text("orig\n", encoding="utf-8")

        await server_mod.rollback_record_operation(
            task_id="t-exec",
            tool_name="file_write",
            operation_type="update",
            target=str(target),
            params={"path": str(target)},
            before_state={"content": "orig\n"},
        )
        target.write_text("changed\n", encoding="utf-8")

        result = await server_mod.rollback_execute(task_id="t-exec", steps=1)

        assert result["success"] is True
        assert result["rolled_back_count"] == 1
        assert result["failed_count"] == 0
        assert result["operations"][0]["tool_name"] == "file_write"
        assert target.read_text(encoding="utf-8") == "orig\n"

    async def test_execute_without_operations_reports_warning(
        self, server_mod: Any
    ) -> None:
        """无操作可回滚 → success 且带 warning（对照组，证明非恒回滚）。"""
        result = await server_mod.rollback_execute(task_id="t-empty")
        assert result["rolled_back_count"] == 0
        assert any("没有需要回滚" in w for w in result["warnings"])

    async def test_record_operation_invalid_type_raises(
        self, server_mod: Any
    ) -> None:
        """非法 operation_type → OperationType 构造报错（不静默降级）。"""
        with pytest.raises(ValueError):
            await server_mod.rollback_record_operation(
                task_id="t-bad",
                tool_name="file_write",
                operation_type="teleport",
                target="/x",
                params={},
            )


# ═══════════════════════════════════════════════════════════
# manager.py：checkpoint 过滤 / 令牌落账失败 / 全局单例
# ═══════════════════════════════════════════════════════════


class TestListOperationsCheckpointFilter:
    async def test_checkpoint_filter_keeps_only_later_operations(
        self, manager_mod: Any
    ) -> None:
        """指定 checkpoint_id → 只留该检查点之后的操作（222-224）。

        本通道按**墙钟 created_at** 过滤（与 _get_operations_to_rollback 的
        序号口径不同），故用真实等待拉开检查点前后的时间差——Windows 下
        datetime.now() 精度约 15ms，无间隔时两批操作会落在同一 tick。
        """
        from models import OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        for idx in range(2):
            await manager.record_operation(
                task_id="t-filter",
                tool_name="file_write",
                operation_type=OperationType.CREATE,
                target=f"/tmp/before{idx}.txt",
                params={},
            )
        time.sleep(0.05)
        cp_id = await manager.create_checkpoint(task_id="t-filter", name="基线")
        time.sleep(0.05)
        later_ids = []
        for idx in range(3):
            later_ids.append(
                await manager.record_operation(
                    task_id="t-filter",
                    tool_name="file_write",
                    operation_type=OperationType.UPDATE,
                    target=f"/tmp/after{idx}.txt",
                    params={},
                )
            )

        all_ops = await manager.list_operations("t-filter")
        filtered = await manager.list_operations("t-filter", checkpoint_id=cp_id)

        assert len(all_ops) == 5
        assert [op.id for op in filtered] == later_ids, "只留检查点之后的操作"
        # 性质断言：过滤结果恒为全集的真子集，且按序号升序
        assert {op.id for op in filtered} < {op.id for op in all_ops}
        assert filtered == sorted(filtered, key=lambda x: x.sequence)

    async def test_unknown_checkpoint_leaves_operations_untouched(
        self, manager_mod: Any
    ) -> None:
        """未知 checkpoint_id → 无过滤（有区分度输入）。"""
        from models import OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        for idx in range(2):
            await manager.record_operation(
                task_id="t-unknown-cp",
                tool_name="file_write",
                operation_type=OperationType.CREATE,
                target=f"/tmp/u{idx}.txt",
                params={},
            )
        plain = await manager.list_operations("t-unknown-cp")
        with_unknown = await manager.list_operations(
            "t-unknown-cp", checkpoint_id="no-such-checkpoint"
        )
        # 未知检查点解析为 None → 不施加任何过滤（全集原样返回）
        assert [op.id for op in with_unknown] == [op.id for op in plain]
        assert len(with_unknown) == 2

    async def test_status_filter_combines_with_checkpoint(
        self, manager_mod: Any, tmp_path: Path
    ) -> None:
        """checkpoint + status 双过滤（组合场景，断言两条件同时成立）。"""
        from models import OperationStatus, OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        target = tmp_path / "f.txt"
        target.write_text("v0\n", encoding="utf-8")
        time.sleep(0.05)
        cp_id = await manager.create_checkpoint(task_id="t-combo", name="基线")
        time.sleep(0.05)
        op_id = await manager.record_operation(
            task_id="t-combo",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target=str(target),
            params={},
            before_state={"content": "v0\n"},
        )
        target.write_text("v1\n", encoding="utf-8")
        await manager.rollback(task_id="t-combo", steps=1)

        executed_after_cp = await manager.list_operations(
            "t-combo", checkpoint_id=cp_id, status=OperationStatus.EXECUTED
        )
        assert [op.id for op in executed_after_cp] == [], "回滚后无 EXECUTED 状态"
        rolled_back = await manager.list_operations(
            "t-combo", checkpoint_id=cp_id, status=OperationStatus.ROLLED_BACK
        )
        assert [op.id for op in rolled_back] == [op_id]


class TestRollbackTokenFailure:
    """置 ROLLING_BACK 令牌失败 → 拒绝执行（354-355，失败即停）。"""

    async def test_token_write_failure_rejects_execution(
        self, manager_mod: Any, tmp_path: Path
    ) -> None:
        from models import OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        target = tmp_path / "guarded.txt"
        target.write_text("before\n", encoding="utf-8")
        await manager.record_operation(
            task_id="t-token",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target=str(target),
            params={},
            before_state={"content": "before\n"},
        )
        target.write_text("after\n", encoding="utf-8")

        async def _fail(op_id: str, status: Any) -> None:
            raise RuntimeError("state store unavailable")

        manager._update_operation_status = _fail  # type: ignore[method-assign]

        result = await manager.rollback(task_id="t-token", steps=1)

        assert result.success is False
        assert result.failed_count == 1
        assert any("置回滚中令牌失败" in err for err in result.errors)
        # 关键不变量：令牌写不进去时逆操作绝不执行（文件保持未回滚）
        assert target.read_text(encoding="utf-8") == "after\n"

    async def test_token_write_success_allows_execution(
        self, manager_mod: Any, tmp_path: Path
    ) -> None:
        """对照组：令牌正常落账 → 逆操作执行，文件被还原。"""
        from models import OperationStatus, OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        target = tmp_path / "ok.txt"
        target.write_text("before\n", encoding="utf-8")
        op_id = await manager.record_operation(
            task_id="t-token-ok",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target=str(target),
            params={},
            before_state={"content": "before\n"},
        )
        target.write_text("after\n", encoding="utf-8")

        result = await manager.rollback(task_id="t-token-ok", steps=1)

        assert result.success is True
        assert target.read_text(encoding="utf-8") == "before\n"
        final = await manager.get_operation(op_id)
        assert final.status is OperationStatus.ROLLED_BACK

    async def test_already_rolled_back_operation_is_skipped(
        self, manager_mod: Any, tmp_path: Path
    ) -> None:
        """幂等令牌：已 ROLLED_BACK 的操作不重复回滚（二次回滚跳过）。"""
        from models import OperationType  # noqa: PLC0415

        manager = manager_mod.RollbackManager()
        target = tmp_path / "idem.txt"
        target.write_text("v0\n", encoding="utf-8")
        await manager.record_operation(
            task_id="t-idem",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target=str(target),
            params={},
            before_state={"content": "v0\n"},
        )
        target.write_text("v1\n", encoding="utf-8")
        assert (await manager.rollback(task_id="t-idem", steps=1)).success is True

        # 手工把状态放回 EXECUTED 模拟重试命中过滤条件
        from models import OperationStatus  # noqa: PLC0415

        operations = await manager.list_operations("t-idem")
        await manager._update_operation_status(operations[0].id, OperationStatus.ROLLED_BACK)
        second = await manager._rollback_single_operation(operations[0])
        assert second["skipped"] is True
        assert "已在回滚中或已回滚" in second["warning"]


class TestGlobalManagerSingleton:
    def test_get_rollback_manager_lazily_builds_and_reuses(
        self, manager_mod: Any
    ) -> None:
        """全局单例：首次构造（404-405），二次返回同一实例（407）。"""
        manager_mod._global_rollback_manager = None
        first = manager_mod.get_rollback_manager()
        assert type(first).__name__ == "RollbackManager"
        assert manager_mod.get_rollback_manager() is first

    def test_get_rollback_manager_respects_existing_instance(
        self, manager_mod: Any
    ) -> None:
        """已存在实例 → 直接返回（不重建，有区分度输入）。"""
        preseeded = manager_mod.RollbackManager()
        manager_mod._global_rollback_manager = preseeded
        try:
            assert manager_mod.get_rollback_manager() is preseeded
        finally:
            manager_mod._global_rollback_manager = None
