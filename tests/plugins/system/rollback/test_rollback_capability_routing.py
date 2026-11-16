# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""rollback 能力路由测试（B4：虚构工具名表修复）。

逆操作器路由依据 = OperationLog 自带数据（operation_type + before_state/
reverse_action 载荷），不再依赖工具名平行清单：
- 真实工具名（builtin_tools manifests 实名 delete_file/file_write）记录的
  操作必须回滚命中；
- 路由按能力判定泛化：陌生工具名只要语义是文件态变更即可路由（防再虚构）；
- 查不中逆操作器 → failed + errors 显式记录、整体 success=False，
  不再静默 skipped。

manager/reversers 走进程内真实实现，文件逆操作作用于真实 tmp_path 文件。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _make_manager() -> Any:
    """默认注册表（能力路由）下的内存模式 manager。"""
    from manager import RollbackManager

    return RollbackManager()


# ============================================================
# B4 验收：真实工具名回滚命中（红→绿，≥2 组区分度输入）
# ============================================================


class TestRealToolNameRollbackHit:
    """manifests 实名工具记录的操作必须回滚命中。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "filename"),
        [
            ("keep-me-中文内容", "restored.txt"),
            ("x" * 500, "nested/dir/restored.bin"),
        ],
    )
    async def test_delete_file按真实工具名回滚恢复文件(
        self, tmp_path: Any, content: str, filename: str,
    ) -> None:
        """delete_file（builtin_tools 实名）DELETE 操作 → 从 before_state 恢复文件。

        契约：路由按操作能力判定，真实工具名 delete_file 不在任何名表内也必须命中。
        """
        from models import OperationType

        mgr = _make_manager()
        target = tmp_path / filename
        await mgr.record_operation(
            task_id="t1",
            tool_name="delete_file",
            operation_type=OperationType.DELETE,
            target=str(target),
            params={"path": str(target)},
            before_state={"content": content},
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 1
        assert result.skipped_count == 0
        assert result.success is True
        assert target.exists()  # 性质断言：文件确已恢复（含父目录重建）
        assert target.read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_file_write更新操作按真实工具名回滚恢复原内容(
        self, tmp_path: Any,
    ) -> None:
        """file_write（builtin_tools 实名）UPDATE 操作 → 恢复 before_state 原内容。"""
        from models import OperationType

        mgr = _make_manager()
        target = tmp_path / "edited.txt"
        target.write_text("new-content", encoding="utf-8")
        await mgr.record_operation(
            task_id="t1",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target=str(target),
            params={"path": str(target)},
            before_state={"content": "old-content"},
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 1
        assert result.skipped_count == 0
        assert target.read_text(encoding="utf-8") == "old-content"

    @pytest.mark.asyncio
    async def test_file_write创建操作回滚删除产物(self, tmp_path: Any) -> None:
        """区分度输入（方向相反）：file_write CREATE 操作 → 回滚删除本操作产物。"""
        from models import OperationType

        mgr = _make_manager()
        target = tmp_path / "created.txt"
        target.write_text("produced", encoding="utf-8")
        await mgr.record_operation(
            task_id="t1",
            tool_name="file_write",
            operation_type=OperationType.CREATE,
            target=str(target),
            params={"path": str(target), "content": "produced"},
            before_state={"exists": False},
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 1
        assert not target.exists()  # 性质断言：产物确已删除


# ============================================================
# B4 验收：查不中不再静默 skipped
# ============================================================


class TestNoReverserExplicitFailure:
    """无可逆操作器 → failed + errors 显式记录，整体 success=False。"""

    @pytest.mark.asyncio
    async def test_查不中逆操作器计入failed且整体失败(self) -> None:
        """EXECUTE、无逆操作载荷、无自定义定义 → 无可逆操作器，显式失败。"""
        from models import OperationType

        mgr = _make_manager()
        await mgr.record_operation(
            task_id="t1",
            tool_name="totally_unknown_tool",
            operation_type=OperationType.EXECUTE,
            target="https://api.example.com/items/7",
            params={},
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.failed_count == 1
        assert result.skipped_count == 0
        assert result.success is False
        assert any("未找到逆操作器" in e for e in result.errors)
        # 操作清单显式落账该失败（可观察，不静默吞）
        assert result.operations[0]["success"] is False
        assert "未找到逆操作器" in result.operations[0]["message"]

    @pytest.mark.asyncio
    async def test_不可逆操作仍按skipped不误报失败(self) -> None:
        """对照路径：reversible=False 是显式声明，保持 skipped 语义不变。"""
        from models import OperationType

        mgr = _make_manager()
        await mgr.record_operation(
            task_id="t1",
            tool_name="some_tool",
            operation_type=OperationType.EXECUTE,
            target="x",
            params={},
            reversible=False,
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.skipped_count == 1
        assert result.failed_count == 0
        assert result.success is True


# ============================================================
# 能力路由契约（不依赖工具名表）
# ============================================================


class TestCapabilityRouting:
    """注册表按 OperationLog 自带数据路由。"""

    def test_文件态变更语义路由到文件逆操作器_与工具名无关(self) -> None:
        """陌生工具名 + DELETE 语义 → 文件逆操作器（新文件工具无需登记即路由）。"""
        from models import OperationLog, OperationType
        from reversers import FileReverser, get_reverser_registry

        reg = get_reverser_registry()
        op = OperationLog(
            tool_name="some_future_file_tool", operation_type=OperationType.DELETE
        )

        reverser = reg.get_reverser(op)

        assert isinstance(reverser, FileReverser)

    def test_携带自定义逆操作定义路由到API逆操作器(self) -> None:
        from models import OperationLog, OperationType
        from reversers import APIReverser, get_reverser_registry

        reg = get_reverser_registry()
        op = OperationLog(
            tool_name="whatever",
            operation_type=OperationType.EXECUTE,
            reverse_action={"type": "http", "method": "DELETE", "url": "https://x/y"},
        )

        assert isinstance(reg.get_reverser(op), APIReverser)

    def test_携带git逆操作载荷路由到Git逆操作器(self) -> None:
        from models import OperationLog, OperationType
        from reversers import GitReverser, get_reverser_registry

        reg = get_reverser_registry()
        for payload in (
            {"before_state": {"commit_hash": "a" * 40}, "params": {}},
            {"before_state": {"stash_index": 0}, "params": {}},
            {"before_state": {}, "params": {"branch_name": "feature-x"}},
        ):
            op = OperationLog(
                tool_name="some_git_tool",
                operation_type=OperationType.EXECUTE,
                before_state=payload["before_state"],
                params=payload["params"],
            )

            assert isinstance(reg.get_reverser(op), GitReverser), payload

    def test_无能力匹配返回None(self) -> None:
        """EXECUTE、无载荷、无定义 → 无人认领，返回 None（由 manager 显式失败）。"""
        from models import OperationLog, OperationType
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        op = OperationLog(
            tool_name="unknown", operation_type=OperationType.EXECUTE
        )

        assert reg.get_reverser(op) is None

    def test_不可逆操作不参与路由(self) -> None:
        """reversible=False → 任何逆操作器都不认领（manager 侧另有 skipped 语义）。"""
        from models import OperationLog, OperationType
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        op = OperationLog(
            tool_name="delete_file",
            operation_type=OperationType.DELETE,
            reversible=False,
        )

        assert reg.get_reverser(op) is None
