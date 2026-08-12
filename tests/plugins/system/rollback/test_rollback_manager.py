# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-plugins-test
"""RollbackManager 单元测试——内存模式（无 DB session）下的核心逻辑。

覆盖：检查点 CRUD、操作日志记录与序号、回滚执行（按 steps / 按 checkpoint）、
ReverserRegistry 路由、FileReverser 逆操作（用 tmp_path 真实文件）。
不触及任何数据库/网络——manager 在 session=None 时纯内存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _make_manager() -> Any:
    """构造内存模式 RollbackManager（无 DB session）。"""
    from manager import RollbackManager

    return RollbackManager(session=None)


def _make_manager_with_fake_reverser(reverser: Any) -> Any:
    """构造带自定义 reverser_registry 的 manager。"""
    from manager import RollbackManager
    from reversers import ReverserRegistry

    reg = ReverserRegistry()
    # 清掉默认注册的 reverser，换成我们指定的
    reg._reversers = {reverser.name: reverser}
    reg._tool_mapping = {}
    for tool in reverser.supported_tools:
        reg._tool_mapping[tool] = reverser.name
    return RollbackManager(session=None, reverser_registry=reg)


class _FakeReverser:
    """可编程的假逆操作器：按 tool_name 返回预设结果。"""

    def __init__(self, name: str = "fake", supported: list[str] | None = None) -> None:
        self._name = name
        self._supported = supported or ["file_write"]
        self.calls: list[Any] = []
        self.next_result: dict[str, Any] = {"success": True, "message": "ok"}

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_tools(self) -> list[str]:
        return self._supported

    async def reverse(self, operation: Any) -> dict[str, Any]:
        self.calls.append(operation)
        return dict(self.next_result)


# ============================================================
# 检查点管理
# ============================================================


class TestCheckpointManagement:
    """create / get / list / delete 检查点（内存模式）。"""

    @pytest.mark.asyncio
    async def test_创建检查点返回id并可取回(self) -> None:
        mgr = _make_manager()
        cp_id = await mgr.create_checkpoint(
            task_id="t1", name="cp-a", description="d", metadata={"k": "v"}
        )

        assert isinstance(cp_id, str) and len(cp_id) > 0
        cp = await mgr.get_checkpoint(cp_id)
        assert cp is not None
        assert cp.task_id == "t1"
        assert cp.name == "cp-a"
        assert cp.description == "d"
        assert cp.metadata == {"k": "v"}
        assert isinstance(cp.created_at, datetime)

    @pytest.mark.asyncio
    async def test_未传name时自动生成带时间戳的默认名(self) -> None:
        mgr = _make_manager()
        cp_id = await mgr.create_checkpoint(task_id="t1")

        cp = await mgr.get_checkpoint(cp_id)
        assert cp is not None
        assert cp.name is not None and cp.name.startswith("checkpoint_")

    @pytest.mark.asyncio
    async def test_未传metadata默认空字典(self) -> None:
        mgr = _make_manager()
        cp_id = await mgr.create_checkpoint(task_id="t1")

        cp = await mgr.get_checkpoint(cp_id)
        assert cp.metadata == {}

    @pytest.mark.asyncio
    async def test_list_按task_id过滤且不串任务(self) -> None:
        mgr = _make_manager()
        a1 = await mgr.create_checkpoint(task_id="t1")
        a2 = await mgr.create_checkpoint(task_id="t1")
        b1 = await mgr.create_checkpoint(task_id="t2")

        t1_cps = await mgr.list_checkpoints("t1")
        t2_cps = await mgr.list_checkpoints("t2")

        assert {c.id for c in t1_cps} == {a1, a2}
        assert [c.id for c in t2_cps] == [b1]
        assert await mgr.list_checkpoints("nope") == []

    @pytest.mark.asyncio
    async def test_get_不存在的id返回None(self) -> None:
        mgr = _make_manager()
        assert await mgr.get_checkpoint("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_delete_存在时返回True删除后查不到(self) -> None:
        mgr = _make_manager()
        cp_id = await mgr.create_checkpoint(task_id="t1")

        assert await mgr.delete_checkpoint(cp_id) is True
        assert await mgr.get_checkpoint(cp_id) is None
        assert await mgr.list_checkpoints("t1") == []

    @pytest.mark.asyncio
    async def test_delete_不存在时返回False(self) -> None:
        mgr = _make_manager()
        assert await mgr.delete_checkpoint("nope") is False


# ============================================================
# 操作日志与序号
# ============================================================


class TestOperationRecording:
    """record_operation / get_operation / list_operations。"""

    @pytest.mark.asyncio
    async def test_记录操作返回id并存入(self) -> None:
        from models import OperationType

        mgr = _make_manager()
        op_id = await mgr.record_operation(
            task_id="t1",
            tool_name="file_write",
            operation_type=OperationType.CREATE,
            target="/tmp/x",
            params={"content": "hi"},
        )

        assert isinstance(op_id, str) and len(op_id) > 0
        op = await mgr.get_operation(op_id)
        assert op is not None
        assert op.task_id == "t1"
        assert op.tool_name == "file_write"
        assert op.operation_type == OperationType.CREATE
        assert op.target == "/tmp/x"
        assert op.params == {"content": "hi"}

    @pytest.mark.asyncio
    async def test_序号按task递增且任务间独立(self) -> None:
        from models import OperationType

        mgr = _make_manager()
        s1 = await mgr.record_operation(
            "t1", "file_write", OperationType.CREATE, "a", {}
        )
        s2 = await mgr.record_operation(
            "t1", "file_write", OperationType.CREATE, "b", {}
        )
        s3 = await mgr.record_operation(
            "t2", "file_write", OperationType.CREATE, "c", {}
        )

        op1, op2, op3 = (
            await mgr.get_operation(s1),
            await mgr.get_operation(s2),
            await mgr.get_operation(s3),
        )
        assert op1.sequence == 1
        assert op2.sequence == 2  # t1 继续递增
        assert op3.sequence == 1  # t2 独立从 1 开始

    @pytest.mark.asyncio
    async def test_list_按sequence升序返回(self) -> None:
        from models import OperationType

        mgr = _make_manager()
        for tgt in ["c", "a", "b"]:
            await mgr.record_operation(
                "t1", "file_write", OperationType.CREATE, tgt, {}
            )

        ops = await mgr.list_operations("t1")
        targets = [o.target for o in ops]
        assert targets == ["c", "a", "b"]  # 按记录顺序（=sequence 升序）
        seqs = [o.sequence for o in ops]
        assert seqs == sorted(seqs)

    @pytest.mark.asyncio
    async def test_list_按status过滤(self) -> None:
        from models import OperationStatus, OperationType

        mgr = _make_manager()
        op_id = await mgr.record_operation(
            "t1", "file_write", OperationType.CREATE, "a", {}
        )
        # 手动改一条为 ROLLED_BACK
        await mgr._update_operation_status(op_id, OperationStatus.ROLLED_BACK)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})

        executed = await mgr.list_operations(
            "t1", status=OperationStatus.EXECUTED
        )
        rolled = await mgr.list_operations(
            "t1", status=OperationStatus.ROLLED_BACK
        )
        assert len(executed) == 1 and executed[0].target == "b"
        assert len(rolled) == 1 and rolled[0].target == "a"

    @pytest.mark.asyncio
    async def test_get_不存在的operation返回None(self) -> None:
        mgr = _make_manager()
        assert await mgr.get_operation("nope") is None


# ============================================================
# 回滚执行
# ============================================================


class TestRollbackExecution:
    """rollback() 的 steps / to_checkpoint / 计数与状态更新。"""

    @pytest.mark.asyncio
    async def test_无操作时返回空结果带warning(self) -> None:
        mgr = _make_manager()
        result = await mgr.rollback(task_id="t1", steps=3)

        assert result.rolled_back_count == 0
        assert result.failed_count == 0
        assert result.success is True
        assert any("没有需要回滚" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_按steps回滚倒序执行逆操作(self) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_manager_with_fake_reverser(fake)

        ids = []
        for tgt in ["a", "b", "c"]:
            ids.append(
                await mgr.record_operation(
                    "t1", "file_write", OperationType.CREATE, tgt, {}
                )
            )

        result = await mgr.rollback(task_id="t1", steps=2)

        assert result.success is True
        assert result.rolled_back_count == 2
        # 倒序：先回滚最后记录的（c, b）
        assert [c.target for c in fake.calls] == ["c", "b"]
        # 被回滚的操作状态变成 ROLLED_BACK
        from models import OperationStatus

        for oid in ids[1:]:  # b, c
            op = await mgr.get_operation(oid)
            assert op.status == OperationStatus.ROLLED_BACK
        # a 未被回滚
        assert (await mgr.get_operation(ids[0])).status == OperationStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_按checkpoint回滚只撤销其后操作(self) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_manager_with_fake_reverser(fake)
        # 检查点夹在中间
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        cp_id = await mgr.create_checkpoint(task_id="t1")
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "c", {})

        result = await mgr.rollback(task_id="t1", to_checkpoint=cp_id)

        assert result.rolled_back_count == 2
        assert [c.target for c in fake.calls] == ["c", "b"]

    @pytest.mark.asyncio
    async def test_不可逆操作计入skipped不计failed(self) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_manager_with_fake_reverser(fake)
        # reversible=False
        await mgr.record_operation(
            "t1",
            "file_write",
            OperationType.CREATE,
            "a",
            {},
            reversible=False,
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 0
        assert result.skipped_count == 1
        assert result.failed_count == 0
        assert result.success is True  # 没有 failed
        assert any("不可逆" in w for w in result.warnings)
        assert fake.calls == []  # 不可逆不调用 reverser

    @pytest.mark.asyncio
    async def test_无reverser时跳过并warn(self) -> None:
        from models import OperationType

        # registry 里只支持 file_write，传入未知 tool
        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_manager_with_fake_reverser(fake)
        await mgr.record_operation(
            "t1", "unknown_tool", OperationType.CREATE, "a", {}
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.skipped_count == 1
        assert any("未找到逆操作器" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_reverser抛异常计入failed且整体success为False(self) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        fake.next_result = {}  # 模拟异常：reverse 内部 raise
        call_count = 0

        async def _raise(op: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        fake.reverse = _raise  # type: ignore[assignment]
        mgr = _make_manager_with_fake_reverser(fake)
        await mgr.record_operation(
            "t1", "file_write", OperationType.CREATE, "a", {}
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert call_count == 1
        assert result.failed_count == 1
        assert result.success is False
        assert any("boom" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_to_checkpoint优先级高于steps(self) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_manager_with_fake_reverser(fake)
        cp_id = await mgr.create_checkpoint(task_id="t1")
        # 检查点后只有 1 条，但 steps 给 99
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})

        result = await mgr.rollback(
            task_id="t1", to_checkpoint=cp_id, steps=99
        )

        # to_checkpoint 胜出：只回滚 cp 之后的 1 条
        assert result.rolled_back_count == 1


# ============================================================
# ReverserRegistry
# ============================================================


class TestReverserRegistry:
    """注册表路由与默认注册。"""

    def test_默认注册file_git_api三种reverser(self) -> None:
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        names = {r.name for r in reg.list_reversers()}
        assert {"file_reverser", "git_reverser", "api_reverser"}.issubset(names)

    def test_工具名映射到对应reverser(self) -> None:
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        assert reg.get_reverser("file_write").name == "file_reverser"
        assert reg.get_reverser("git_commit").name == "git_reverser"
        assert reg.get_reverser("api_create").name == "api_reverser"

    def test_未知工具返回None且is_tool_reversible为False(self) -> None:
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        assert reg.get_reverser("unknown") is None
        assert reg.is_tool_reversible("unknown") is False
        assert reg.is_tool_reversible("file_write") is True

    def test_get_reverser_by_name(self) -> None:
        from reversers import get_reverser_registry

        reg = get_reverser_registry()
        assert reg.get_reverser_by_name("file_reverser") is not None
        assert reg.get_reverser_by_name("nope") is None

    def test_注册自定义reverser覆盖工具映射(self) -> None:
        from reversers import ReverserRegistry

        reg = ReverserRegistry()
        custom = _FakeReverser(name="custom", supported=["file_write"])
        reg.register(custom)
        # file_write 现在映射到 custom
        assert reg.get_reverser("file_write") is custom
        assert reg.get_reverser_by_name("custom") is custom


# ============================================================
# FileReverser（真实 tmp 文件）
# ============================================================


class TestFileReverser:
    """FileReverser 的 create/update/delete 逆操作（用 tmp_path）。"""

    @pytest.mark.asyncio
    async def test_逆create删除已存在文件(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        f = tmp_path / "created.txt"
        f.write_text("data")
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(f),
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_逆create文件已不存在视为成功skip(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        f = tmp_path / "ghost.txt"
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(f),
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert result["details"]["action"] == "skip"

    @pytest.mark.asyncio
    async def test_逆update恢复before_state内容(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        f = tmp_path / "f.txt"
        f.write_text("new")
        op = OperationLog(
            tool_name="file_update",
            operation_type=OperationType.UPDATE,
            target=str(f),
            before_state={"content": "old"},
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert f.read_text() == "old"

    @pytest.mark.asyncio
    async def test_逆update缺content失败(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        f = tmp_path / "f.txt"
        op = OperationLog(
            tool_name="file_update",
            operation_type=OperationType.UPDATE,
            target=str(f),
            before_state={},  # 无 content
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is False
        assert "原始内容" in result["message"]

    @pytest.mark.asyncio
    async def test_逆delete从before_state恢复文件(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        f = tmp_path / "deleted.txt"
        op = OperationLog(
            tool_name="file_delete",
            operation_type=OperationType.DELETE,
            target=str(f),
            before_state={"content": "restored"},
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert f.read_text() == "restored"

    def test_supported_tools列表(self) -> None:
        from reversers import FileReverser

        tools = FileReverser().supported_tools
        assert "file_write" in tools and "file_create" in tools

    def test_can_reverse仅对支持的reversible操作为True(self) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        rev = FileReverser()
        ok = OperationLog(
            tool_name="file_write", operation_type=OperationType.UPDATE, reversible=True
        )
        no_tool = OperationLog(
            tool_name="git_commit", operation_type=OperationType.UPDATE, reversible=True
        )
        no_rev = OperationLog(
            tool_name="file_write", operation_type=OperationType.UPDATE, reversible=False
        )
        assert rev.can_reverse(ok) is True
        assert rev.can_reverse(no_tool) is False
        assert rev.can_reverse(no_rev) is False


# ============================================================
# 数据模型 round-trip
# ============================================================


class TestModelsRoundTrip:
    """OperationLog / Checkpoint / RollbackResult 的 to_dict/from_dict。"""

    def test_OperationLog_round_trip(self) -> None:
        from models import OperationLog, OperationStatus, OperationType

        op = OperationLog(
            id="op-1",
            task_id="t1",
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target="/a",
            params={"k": "v"},
            before_state={"content": "old"},
            after_state={"content": "new"},
            reversible=True,
            reverse_action={"type": "http"},
            sequence=7,
            status=OperationStatus.EXECUTED,
        )
        d = op.to_dict()
        assert d["operation_type"] == "update"
        assert d["status"] == "executed"
        assert d["sequence"] == 7

        op2 = OperationLog.from_dict(d)
        assert op2.id == "op-1"
        assert op2.operation_type == OperationType.UPDATE
        assert op2.status == OperationStatus.EXECUTED
        assert op2.params == {"k": "v"}

    def test_Checkpoint_round_trip(self) -> None:
        from models import Checkpoint

        cp = Checkpoint(
            id="cp-1", task_id="t1", name="n", description="d", metadata={"x": 1}
        )
        d = cp.to_dict()
        cp2 = Checkpoint.from_dict(d)
        assert cp2.id == "cp-1"
        assert cp2.name == "n"
        assert cp2.metadata == {"x": 1}

    def test_RollbackResult_to_dict字段完整(self) -> None:
        from models import RollbackResult

        r = RollbackResult()
        r.rolled_back_count = 2
        r.warnings.append("w")
        d = r.to_dict()
        assert d["success"] is True
        assert d["rolled_back_count"] == 2
        assert d["warnings"] == ["w"]
        assert "operations" in d and "errors" in d and "skipped_count" in d

    def test_OperationType枚举值(self) -> None:
        from models import OperationType

        assert OperationType("create") == OperationType.CREATE
        assert OperationType("delete").value == "delete"
