# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-plugins-test
"""RollbackManager 单元测试——DB 持久化模式（带 AsyncSession，SQLite 内存库）。

产品决策：内核已用数据库接口（SQLite / kernel db_routes），插件侧 DB 模式
必须补全而非砍掉。本文件验证 manager 在 session=非 None 时走真实持久化层
（plugins/shared/system/rollback/_db_models.py 的 ORM 模型），且行为与内存模式
严格一致：检查点 CRUD、操作日志 sequence 排序/status 过滤、按 sequence 回滚
（Windows 墙钟同 tick 也精确）、status 流转、跨 manager 实例数据共享。

意图：DB 模式不是占位符——重启/换实例后数据与 sequence 语义必须依然正确，
否则回滚按序号定位会静默错乱（重复 sequence → 回滚错对象/漏对象）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# Fixture：SQLite 内存库 + 建表 + AsyncSession
# ============================================================


@pytest.fixture
def db_session() -> Any:
    """每个测试独立的内存 SQLite 库（StaticPool 单连接保活）与同步 Session。"""
    from _db_models import Base, create_db_engine
    from sqlalchemy.orm import Session

    engine = create_db_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ============================================================
# 辅助
# ============================================================


def _make_db_manager(session: Any, reverser: Any | None = None) -> Any:
    """构造 DB 模式 RollbackManager（必带 AsyncSession）。"""
    from manager import RollbackManager

    if reverser is None:
        return RollbackManager(session=session)
    from reversers import ReverserRegistry

    reg = ReverserRegistry()
    reg._reversers = {reverser.name: reverser}
    reg._tool_mapping = {}
    for tool in reverser.supported_tools:
        reg._tool_mapping[tool] = reverser.name
    return RollbackManager(session=session, reverser_registry=reg)


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


class _FixedClock:
    """固定时刻的钟：now() 永远返回同一时刻，模拟 Windows 墙钟同 tick。"""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


# ============================================================
# DB 模式：检查点管理
# ============================================================


class TestDbCheckpointManagement:
    """create / get / list / delete 检查点（DB 持久化）。"""

    @pytest.mark.asyncio
    async def test_创建检查点后get字段一致含sequence(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
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
        assert cp.sequence == 0  # 创建检查点时该 task 尚无操作

    @pytest.mark.asyncio
    async def test_检查点sequence记录当前操作序号(self, db_session: Any) -> None:
        from models import OperationType

        mgr = _make_db_manager(db_session)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})
        cp_id = await mgr.create_checkpoint(task_id="t1")

        cp = await mgr.get_checkpoint(cp_id)
        assert cp is not None and cp.sequence == 2  # 与内存模式一致：记录创建时的序号

    @pytest.mark.asyncio
    async def test_未传name自动生成默认名metadata空字典(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
        cp_id = await mgr.create_checkpoint(task_id="t1")

        cp = await mgr.get_checkpoint(cp_id)
        assert cp is not None
        assert cp.name is not None and cp.name.startswith("checkpoint_")
        assert cp.metadata == {}

    @pytest.mark.asyncio
    async def test_list按task过滤且不串任务(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
        a1 = await mgr.create_checkpoint(task_id="t1")
        a2 = await mgr.create_checkpoint(task_id="t1")
        b1 = await mgr.create_checkpoint(task_id="t2")

        t1_cps = await mgr.list_checkpoints("t1")
        t2_cps = await mgr.list_checkpoints("t2")

        assert {c.id for c in t1_cps} == {a1, a2}
        assert [c.id for c in t2_cps] == [b1]
        assert await mgr.list_checkpoints("nope") == []

    @pytest.mark.asyncio
    async def test_delete存在返回True删后查不到(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
        cp_id = await mgr.create_checkpoint(task_id="t1")

        assert await mgr.delete_checkpoint(cp_id) is True
        assert await mgr.get_checkpoint(cp_id) is None
        assert await mgr.list_checkpoints("t1") == []

    @pytest.mark.asyncio
    async def test_delete不存在返回False(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
        assert await mgr.delete_checkpoint("nope") is False

    @pytest.mark.asyncio
    async def test_同一session新manager实例读到既有数据(self, db_session: Any) -> None:
        """意图：DB 是真实持久化层——换实例/重启后数据不丢，而非单实例内存缓存。"""
        mgr1 = _make_db_manager(db_session)
        cp_id = await mgr1.create_checkpoint(
            task_id="t1", name="cp-a", description="d", metadata={"k": "v"}
        )

        mgr2 = _make_db_manager(db_session)  # 全新实例，同一 session
        cp = await mgr2.get_checkpoint(cp_id)
        assert cp is not None
        assert cp.name == "cp-a"
        assert cp.description == "d"
        assert cp.metadata == {"k": "v"}
        assert [c.id for c in await mgr2.list_checkpoints("t1")] == [cp_id]


# ============================================================
# DB 模式：操作日志与序号
# ============================================================


class TestDbOperationRecording:
    """record_operation / get_operation / list_operations（DB 持久化）。"""

    @pytest.mark.asyncio
    async def test_记录操作get字段完整往返(self, db_session: Any) -> None:
        from models import OperationStatus, OperationType

        mgr = _make_db_manager(db_session)
        op_id = await mgr.record_operation(
            "t1",
            "file_write",
            OperationType.UPDATE,
            "/a",
            {"k": "v"},
            before_state={"content": "old"},
            after_state={"content": "new"},
            reversible=True,
            reverse_action={"type": "http"},
        )

        op = await mgr.get_operation(op_id)
        assert op is not None
        assert op.task_id == "t1"
        assert op.tool_name == "file_write"
        assert op.operation_type == OperationType.UPDATE
        assert op.target == "/a"
        assert op.params == {"k": "v"}
        assert op.before_state == {"content": "old"}
        assert op.after_state == {"content": "new"}
        assert op.reversible is True
        assert op.reverse_action == {"type": "http"}
        assert op.status == OperationStatus.EXECUTED
        assert op.error_message is None

    @pytest.mark.asyncio
    async def test_sequence按task递增且任务间独立(self, db_session: Any) -> None:
        from models import OperationType

        mgr = _make_db_manager(db_session)
        s1 = await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        s2 = await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})
        s3 = await mgr.record_operation("t2", "file_write", OperationType.CREATE, "c", {})

        op1, op2, op3 = (
            await mgr.get_operation(s1),
            await mgr.get_operation(s2),
            await mgr.get_operation(s3),
        )
        assert op1.sequence == 1
        assert op2.sequence == 2  # t1 继续递增
        assert op3.sequence == 1  # t2 独立从 1 开始

    @pytest.mark.asyncio
    async def test_新实例sequence接续库内最大值(self, db_session: Any) -> None:
        """意图：持久化层跨实例必须保持 sequence 单调，否则回滚按序号定位会错乱。"""
        from models import OperationType

        mgr1 = _make_db_manager(db_session)
        await mgr1.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        await mgr1.record_operation("t1", "file_write", OperationType.CREATE, "b", {})

        mgr2 = _make_db_manager(db_session)  # 全新实例（内存计数器已重置）
        s3 = await mgr2.record_operation("t1", "file_write", OperationType.CREATE, "c", {})

        op3 = await mgr2.get_operation(s3)
        assert op3.sequence == 3  # 从库内 max=2 续接，而非重新从 1 开始

    @pytest.mark.asyncio
    async def test_list按sequence升序返回(self, db_session: Any) -> None:
        from models import OperationType

        mgr = _make_db_manager(db_session)
        for tgt in ["c", "a", "b"]:
            await mgr.record_operation("t1", "file_write", OperationType.CREATE, tgt, {})

        ops = await mgr.list_operations("t1")
        assert [o.target for o in ops] == ["c", "a", "b"]
        assert [o.sequence for o in ops] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_list按status过滤(self, db_session: Any) -> None:
        from models import OperationStatus, OperationType

        mgr = _make_db_manager(db_session)
        op_id = await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        await mgr._update_operation_status(op_id, OperationStatus.ROLLED_BACK)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})

        executed = await mgr.list_operations("t1", status=OperationStatus.EXECUTED)
        rolled = await mgr.list_operations("t1", status=OperationStatus.ROLLED_BACK)
        assert len(executed) == 1 and executed[0].target == "b"
        assert len(rolled) == 1 and rolled[0].target == "a"

    @pytest.mark.asyncio
    async def test_get不存在的operation返回None(self, db_session: Any) -> None:
        mgr = _make_db_manager(db_session)
        assert await mgr.get_operation("nope") is None

    @pytest.mark.asyncio
    async def test_error_message字段可往返(self, db_session: Any) -> None:
        """error_message 与 models.OperationLog 对齐：写库后读回不丢。"""
        from _db_models import RollbackOperationLog
        from models import OperationType
        from sqlalchemy import select

        mgr = _make_db_manager(db_session)
        op_id = await mgr.record_operation("t1", "file_write", OperationType.CREATE, "/a", {})

        # 模拟外部流程直接标注失败原因后落库（同步 Session，勿 await）
        row = (
            db_session.execute(
                select(RollbackOperationLog).where(RollbackOperationLog.id == op_id)
            )
        ).scalar_one()
        row.error_message = "boom"
        db_session.commit()

        op = await mgr.get_operation(op_id)
        assert op is not None and op.error_message == "boom"


# ============================================================
# DB 模式：回滚执行
# ============================================================


class TestDbRollbackExecution:
    """rollback() 的 to_checkpoint / steps / status 流转（DB 持久化）。"""

    @pytest.mark.asyncio
    async def test_按checkpoint回滚只撤销其后操作(self, db_session: Any) -> None:
        from models import OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_db_manager(db_session, fake)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        cp_id = await mgr.create_checkpoint(task_id="t1")
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "c", {})

        result = await mgr.rollback(task_id="t1", to_checkpoint=cp_id)

        assert result.rolled_back_count == 2
        assert [c.target for c in fake.calls] == ["c", "b"]

    @pytest.mark.asyncio
    async def test_同tick下按sequence精确回滚(self, db_session: Any, monkeypatch: Any) -> None:
        """意图：Windows 墙钟精度约 15ms，同测试内 op 与 checkpoint 可能同 tick——
        created_at 全相等时按时间过滤必然误伤，必须按单调 sequence 精确定位。"""
        import datetime as _dt

        import manager as manager_mod
        from models import OperationType

        fixed = _dt.datetime(2026, 8, 12, 12, 0, 0)
        monkeypatch.setattr(manager_mod, "datetime", _FixedClock(fixed))  # 所有时间戳同 tick

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_db_manager(db_session, fake)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        cp_id = await mgr.create_checkpoint(task_id="t1")
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "c", {})

        # 先确认真的是同 tick 场景
        ops = await mgr.list_operations("t1")
        cp = await mgr.get_checkpoint(cp_id)
        assert cp is not None
        assert len({o.created_at for o in ops} | {cp.created_at}) == 1

        result = await mgr.rollback(task_id="t1", to_checkpoint=cp_id)

        assert result.rolled_back_count == 2  # a(seq=1) 在检查点(seq=1)之前，必须保留
        assert [c.target for c in fake.calls] == ["c", "b"]

    @pytest.mark.asyncio
    async def test_status流转EXECUTED到ROLLED_BACK(self, db_session: Any) -> None:
        from models import OperationStatus, OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_db_manager(db_session, fake)
        ids = []
        for tgt in ["a", "b"]:
            ids.append(
                await mgr.record_operation("t1", "file_write", OperationType.CREATE, tgt, {})
            )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 1
        assert (await mgr.get_operation(ids[1])).status == OperationStatus.ROLLED_BACK
        assert (await mgr.get_operation(ids[0])).status == OperationStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_不可逆操作保持EXECUTED计入skipped(self, db_session: Any) -> None:
        from models import OperationStatus, OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_db_manager(db_session, fake)
        op_id = await mgr.record_operation(
            "t1", "file_write", OperationType.CREATE, "a", {}, reversible=False
        )

        result = await mgr.rollback(task_id="t1", steps=1)

        assert result.rolled_back_count == 0
        assert result.skipped_count == 1
        assert result.failed_count == 0
        assert result.success is True
        assert any("不可逆" in w for w in result.warnings)
        assert (await mgr.get_operation(op_id)).status == OperationStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_回滚后list_operations按status可继续过滤(self, db_session: Any) -> None:
        """意图：回滚不是终态——残留 EXECUTED 操作仍可被后续轮次回滚。"""
        from models import OperationStatus, OperationType

        fake = _FakeReverser(supported=["file_write"])
        mgr = _make_db_manager(db_session, fake)
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "a", {})
        await mgr.record_operation("t1", "file_write", OperationType.CREATE, "b", {})

        r1 = await mgr.rollback(task_id="t1", steps=1)
        assert r1.rolled_back_count == 1
        assert len(await mgr.list_operations("t1", status=OperationStatus.ROLLED_BACK)) == 1
        assert len(await mgr.list_operations("t1", status=OperationStatus.EXECUTED)) == 1

        r2 = await mgr.rollback(task_id="t1", steps=1)
        assert r2.rolled_back_count == 1
        assert len(await mgr.list_operations("t1", status=OperationStatus.EXECUTED)) == 0
