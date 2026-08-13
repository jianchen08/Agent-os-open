# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-plugins-test
"""GitReverser 集成测试——真实临时 git 仓库 + 幂等回滚令牌。

覆盖（业界成熟回滚 = 精确恢复点 + 幂等 + 失败即停）：
- 精确回退：以 before_state.commit_hash 为唯一目标，绝不回退 HEAD~1
  （HEAD~1 在记录点后有新 commit 时会回退错误对象）；
- 失败即停：commit_hash 缺失 / 无效 / HEAD 早于记录点 → 明确失败，不静默操作；
- 幂等令牌：ROLLING_BACK/ROLLED_BACK 状态下重试跳过；reverse 成功但状态
  落库失败时 op 停留在 ROLLING_BACK，重试不会二次 reset（杜绝双回滚）；
- 目录保护：_reverse_create 不得删除「无法确认由本操作创建」的非空目录
  （防误删目录创建后写入的无关文件）。

git 不可用时整模块 skip（pytest.skip），无破坏命令（git 操作仅限 tmp_path）。
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# git 环境辅助
# ============================================================


def _run(repo: Any, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在 repo 内执行 git 命令（数组参数，无 shell）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Any) -> Any:
    """真实临时 git 仓库；git 不可用则整组 skip。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用，跳过 GitReverser 测试")
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "TDD Test")
    # 固定行尾，避免 Windows autocrlf 干扰内容断言
    _run(repo, "config", "core.autocrlf", "false")
    return repo


def _commit(repo: Any, filename: str, content: str) -> str:
    """提交一个文件，返回新 commit hash。"""
    (repo / filename).write_text(content, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", f"commit {filename}")
    return _head(repo)


def _head(repo: Any) -> str:
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


def _log_hashes(repo: Any) -> list[str]:
    out = _run(repo, "log", "--format=%H").stdout.strip()
    return out.splitlines() if out else []


def _make_git_op(before_state: dict[str, Any]) -> Any:
    """构造一条 git_commit 操作日志（before_state 可指定记录点）。"""
    from models import OperationLog, OperationType

    return OperationLog(
        tool_name="git_commit",
        operation_type=OperationType.EXECUTE,
        target=".",
        params={},
        before_state=before_state,
        reversible=True,
    )


def _make_git_manager(repo: Any) -> Any:
    """内存模式 manager + 指向临时仓库的真实 GitReverser。"""
    from manager import RollbackManager
    from reversers import GitReverser, ReverserRegistry

    reg = ReverserRegistry()
    reg._reversers = {}
    reg._tool_mapping = {}
    reg.register(GitReverser(repo_path=str(repo)))
    return RollbackManager(session=None, reverser_registry=reg)


# ============================================================
# 精确回退到记录点
# ============================================================


class TestGitReverseCommitPrecision:
    """以 before_state.commit_hash 为唯一回退目标，绝不 HEAD~1 误伤。"""

    @pytest.mark.asyncio
    async def test_记录点后有多个新commit回滚精确回到记录点(self, git_repo: Any) -> None:
        from reversers import GitReverser

        h1 = _commit(git_repo, "a.txt", "A1")
        _commit(git_repo, "b.txt", "B")
        _commit(git_repo, "c.txt", "C")

        result = await GitReverser(repo_path=str(git_repo)).reverse(
            _make_git_op({"commit_hash": h1})
        )

        assert result["success"] is True
        # 精确恢复点：HEAD 回到记录点，记录点之后的新 commit 全部退出历史
        assert _head(git_repo) == h1
        assert _log_hashes(git_repo) == [h1]
        # soft reset 不丢工作树：记录点内容仍在
        assert (git_repo / "a.txt").read_text(encoding="utf-8") == "A1"

    @pytest.mark.asyncio
    async def test_commit_hash缺失时失败不静默(self, git_repo: Any) -> None:
        from reversers import GitReverser

        _commit(git_repo, "a.txt", "A1")
        _commit(git_repo, "b.txt", "B")
        head_before = _head(git_repo)

        # 记录点 hash 缺失（老日志/未捕获）——绝不能回退 HEAD~1（会回退错误对象）
        result = await GitReverser(repo_path=str(git_repo)).reverse(_make_git_op({}))

        assert result["success"] is False
        assert "commit_hash" in result["message"]
        assert _head(git_repo) == head_before  # 仓库未被触碰

    @pytest.mark.asyncio
    async def test_commit_hash无效时失败不静默(self, git_repo: Any) -> None:
        from reversers import GitReverser

        _commit(git_repo, "a.txt", "A1")
        head_before = _head(git_repo)

        result = await GitReverser(repo_path=str(git_repo)).reverse(
            _make_git_op({"commit_hash": "0" * 40})
        )

        assert result["success"] is False
        assert _head(git_repo) == head_before

    @pytest.mark.asyncio
    async def test_HEAD早于记录点时拒绝回滚(self, git_repo: Any) -> None:
        from reversers import GitReverser

        h1 = _commit(git_repo, "a.txt", "A1")
        h2 = _commit(git_repo, "b.txt", "B")
        # 分支被外部回退到记录点之前（HEAD=C1，早于记录点 C2）
        _run(git_repo, "reset", "--hard", "HEAD~1")
        assert _head(git_repo) == h1

        # 若盲目 reset --soft h2 会把 HEAD 向前推到 C2、复活已回退的历史——必须拒绝
        result = await GitReverser(repo_path=str(git_repo)).reverse(
            _make_git_op({"commit_hash": h2})
        )

        assert result["success"] is False
        assert _head(git_repo) == h1  # 保持不动


# ============================================================
# 幂等反转令牌（manager 路径）
# ============================================================


class TestGitRollbackIdempotency:
    """重试不得双回滚：ROLLING_BACK/ROLLED_BACK 状态下跳过。"""

    @pytest.mark.asyncio
    async def test_连续两次rollback第二次幂等跳过(self, git_repo: Any) -> None:
        from models import OperationType

        h1 = _commit(git_repo, "a.txt", "A1")
        _commit(git_repo, "b.txt", "B")
        mgr = _make_git_manager(git_repo)
        await mgr.record_operation(
            task_id="t1",
            tool_name="git_commit",
            operation_type=OperationType.EXECUTE,
            target=".",
            params={},
            before_state={"commit_hash": h1},
        )

        r1 = await mgr.rollback(task_id="t1", steps=1)
        assert r1.rolled_back_count == 1
        assert _head(git_repo) == h1

        # 第二次回滚：op 已 ROLLED_BACK，直接跳过，仓库保持记录点
        r2 = await mgr.rollback(task_id="t1", steps=1)
        assert r2.rolled_back_count == 0
        assert _head(git_repo) == h1
        assert _log_hashes(git_repo) == [h1]

    @pytest.mark.asyncio
    async def test_reverse成功但状态落库失败重试不双回滚(self, git_repo: Any) -> None:
        from models import OperationStatus, OperationType

        h1 = _commit(git_repo, "a.txt", "A1")
        _commit(git_repo, "b.txt", "B")
        mgr = _make_git_manager(git_repo)
        op_id = await mgr.record_operation(
            task_id="t1",
            tool_name="git_commit",
            operation_type=OperationType.EXECUTE,
            target=".",
            params={},
            before_state={"commit_hash": h1},
        )

        # 模拟 reverse 成功但 ROLLED_BACK 落库失败（DB 抖动）：
        # 只允许 ROLLING_BACK 令牌写入，其余状态一律落库失败
        real_update = mgr._update_operation_status

        async def _broken_status_update(oid: str, status: OperationStatus) -> None:
            if status is not OperationStatus.ROLLING_BACK:
                raise RuntimeError("db down")
            await real_update(oid, status)

        mgr._update_operation_status = _broken_status_update  # type: ignore[method-assign]

        r1 = await mgr.rollback(task_id="t1", steps=1)
        # reverse 已执行成功（HEAD=记录点），但状态落库失败 → 报告失败
        assert r1.failed_count == 1
        assert _head(git_repo) == h1
        op = await mgr.get_operation(op_id)
        # 反转令牌留下，op 未变回 EXECUTED——这是防双回滚的关键
        assert op.status == OperationStatus.ROLLING_BACK

        # 用户继续工作：在记录点之上又提交了新 commit
        h_new = _commit(git_repo, "d.txt", "D")

        # 重试回滚：op 处于 ROLLING_BACK 令牌状态 → 跳过，绝不再次 reset
        r2 = await mgr.rollback(task_id="t1", steps=1)
        assert r2.rolled_back_count == 0
        # 新 commit 安然无恙——若发生双回滚会被二次 reset 抹掉
        assert _head(git_repo) == h_new
        assert h_new in _log_hashes(git_repo)

    @pytest.mark.asyncio
    async def test_回滚中令牌状态的操作直接跳过(self, git_repo: Any) -> None:
        from models import OperationStatus, OperationType

        h1 = _commit(git_repo, "a.txt", "A1")
        mgr = _make_git_manager(git_repo)
        op_id = await mgr.record_operation(
            task_id="t1",
            tool_name="git_commit",
            operation_type=OperationType.EXECUTE,
            target=".",
            params={},
            before_state={"commit_hash": h1},
        )
        await mgr._update_operation_status(op_id, OperationStatus.ROLLING_BACK)
        op = await mgr.get_operation(op_id)
        assert op is not None

        result = await mgr._rollback_single_operation(op)

        assert result["success"] is False
        assert result.get("skipped") is True
        assert _head(git_repo) == h1  # 未执行任何 git 操作


# ============================================================
# _reverse_create 目录保护
# ============================================================


class TestReverseCreateDirectoryProtection:
    """目录删除保护：不销毁「无法确认由本操作创建」的内容。"""

    @pytest.mark.asyncio
    async def test_非空目录无before_state拒绝删除(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        d = tmp_path / "created_dir"
        d.mkdir()
        (d / "unrelated.txt").write_text("later", encoding="utf-8")  # 后续无关写入
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(d),
            before_state=None,
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is False
        # 无关文件必须原样保留——rmtree 会把它一并销毁（爆炸半径过大）
        assert (d / "unrelated.txt").read_text(encoding="utf-8") == "later"

    @pytest.mark.asyncio
    async def test_非空目录before_state确认本操作创建才删除(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        d = tmp_path / "created_dir"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        # {"exists": False} = 操作前目录不存在 → 目录由本操作创建，删除属操作自身范围
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(d),
            before_state={"exists": False},
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert not d.exists()

    @pytest.mark.asyncio
    async def test_空目录无before_state可安全删除(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        d = tmp_path / "empty_dir"
        d.mkdir()
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(d),
            before_state=None,
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is True
        assert not d.exists()

    @pytest.mark.asyncio
    async def test_目录操作前已存在则拒绝删除(self, tmp_path: Any) -> None:
        from models import OperationLog, OperationType
        from reversers import FileReverser

        d = tmp_path / "preexisting"
        d.mkdir()
        (d / "keep.txt").write_text("keep", encoding="utf-8")
        # 操作前目录已存在 → 目录不是本操作创建的对象，不得 rmtree
        op = OperationLog(
            tool_name="file_create",
            operation_type=OperationType.CREATE,
            target=str(d),
            before_state={"exists": True, "is_dir": True},
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is False
        assert d.exists()
        assert (d / "keep.txt").read_text(encoding="utf-8") == "keep"
