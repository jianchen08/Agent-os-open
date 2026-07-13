"""隔离降级禁用测试。

核心契约（修复 .checkpoints 跨容器串台事故后确立）：
- 隔离级别不可用一律拒绝（blocked），绝不降级到其它级别。
- host 模式不再建文件检查点（工作区由 git 托管）。

旧实现：container 不可用 + fallback:allow → 静默降级到 host，
导致属于其它容器/工作区的任务落到本编排进程执行，checkpoint
跟着进程 CWD 落到错误的容器根，造成跨容器污染。
"""
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from isolation.decider import IsolationDecider, IsolationError
from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.types import IsolationLevel
from pipeline.plugin import PluginContext


# ═══════════════════════════════════════════════════════════════
# IsolationDecider — 容器不可用一律报错，不降级
# ═══════════════════════════════════════════════════════════════


class TestDeciderNoFallback:
    """隔离级别不可用时不再降级，直接抛 IsolationError。"""

    @staticmethod
    def _make_decider():
        """创建使用空配置的 decider，默认策略为 container。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._default = ToolIsolationPolicy(isolation=IsolationLevel.CONTAINER)
        return IsolationDecider(policy_loader=loader), loader

    @pytest.mark.asyncio
    async def test_container_unavailable_host_available_still_raises(self):
        """container 不可用 + host 可用 → 抛 IsolationError（不降级到 host）。"""
        decider, _ = self._make_decider()
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: True}

        with pytest.raises(IsolationError, match="不可用"):
            await decider.decide("bash_execute", available_providers=available)

    @pytest.mark.asyncio
    async def test_all_unavailable_raises(self):
        """container+host 都不可用 → 抛 IsolationError。"""
        decider, _ = self._make_decider()
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: False}

        with pytest.raises(IsolationError):
            await decider.decide("any_tool", available_providers=available)

    @pytest.mark.asyncio
    async def test_container_available_returns_container(self):
        """container 可用时正常返回 container 策略。"""
        decider, loader = self._make_decider()
        loader._tools["bash_execute"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER
        )
        available = {IsolationLevel.CONTAINER: True, IsolationLevel.HOST: True}

        policy = await decider.decide("bash_execute", available_providers=available)
        assert policy.isolation == IsolationLevel.CONTAINER

    @pytest.mark.asyncio
    async def test_no_availability_check_returns_original(self):
        """不做可用性检查时直接返回原始策略。"""
        decider, _ = self._make_decider()
        policy = await decider.decide("any_tool")
        assert policy.isolation == IsolationLevel.CONTAINER


# ═══════════════════════════════════════════════════════════════
# IsolationGuard — 容器不可用一律 blocked，不降级
# ═══════════════════════════════════════════════════════════════


def _make_guard(docker_available=False, force_host=False):
    """创建 IsolationGuard 实例，使用空策略配置。

    所有工具默认/显式策略为 container——container 不可用时一律 blocked，
    不再有 allow/deny 之分。
    """
    from plugins.input.isolation_guard.plugin import IsolationGuard

    guard = IsolationGuard(config={
        "docker_available": docker_available,
        "force_host": force_host,
    })
    loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
    loader._default = ToolIsolationPolicy(isolation=IsolationLevel.CONTAINER)
    loader._tools["bash_execute"] = ToolIsolationPolicy(
        isolation=IsolationLevel.CONTAINER
    )
    loader._tools["file_read"] = ToolIsolationPolicy(
        isolation=IsolationLevel.CONTAINER
    )
    guard._decider = IsolationDecider(policy_loader=loader)
    return guard


def _make_ctx(state=None):
    """创建最小 PluginContext。

    默认标 L2（子任务）：metadata/policy 路径的用例测的是"docker 可用进容器 /
    不可用 blocked"这类子任务场景；主 agent（L1）的 bash_execute 一律走 host。
    force_host 用例不受影响——force_host 判断在 L1 路由之前，无论层级都先 blocked。
    """
    _state = {"agent_level": "L2"}
    if state:
        _state.update(state)
    return PluginContext(state=_state, _services={})


class TestGuardForceHostBlocked:
    """force_host 不能把要求容器隔离的工具放到宿主机执行。"""

    def test_force_host_denies_bash_execute(self):
        """force_host=True + bash_execute(container) → blocked。"""
        guard = _make_guard(docker_available=False, force_host=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"
        assert result["tool_name"] == "bash_execute"

    def test_force_host_denies_unknown_container_tool(self):
        """force_host=True + 未知工具(默认 container) → blocked。"""
        guard = _make_guard(docker_available=False, force_host=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("unknown_dangerous_tool", ctx)

        assert result["blocked"] is True


class TestGuardMetadataPath:
    """metadata 路径在 Docker 不可用时一律 blocked。"""

    def test_metadata_container_docker_unavailable_blocked(self):
        """metadata 要求 isolated + Docker 不可用 → blocked（不降级）。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "isolated"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"
        assert "docker_unavailable" in result["reason"]

    def test_metadata_container_docker_available(self):
        """metadata 要求 isolated + Docker 可用 → docker。"""
        guard = _make_guard(docker_available=True)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "isolated"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "docker"
        assert result.get("blocked") is not True

    def test_metadata_host(self):
        """metadata 要求 non_isolated → 直接 host（不检查 Docker 可用性）。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "non_isolated"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "host"
        assert result.get("blocked") is not True


class TestGuardPolicyPath:
    """策略路径（无 metadata 覆盖）：容器不可用一律 blocked。"""

    def test_policy_container_docker_unavailable_blocked(self):
        """策略 container + Docker 不可用 → blocked。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx()

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"

    def test_policy_container_docker_available(self):
        """策略 container + Docker 可用 → docker。"""
        guard = _make_guard(docker_available=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "docker"
        assert result.get("blocked") is not True


class TestGuardBlockedPropagates:
    """blocked 工具在 execute 方法中正确传播到 isolation.blocked。"""

    @pytest.mark.asyncio
    async def test_blocked_tool_sets_isolation_blocked(self):
        """被阻止的工具设置 isolation.blocked = True。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "ls"}}],
        })

        result = await guard.execute(ctx)

        contexts = result.state_updates.get("execution_contexts", [])
        blocked = [c for c in contexts if c.get("blocked")]
        assert len(blocked) == 1
        assert blocked[0]["tool_name"] == "bash_execute"

        assert result.state_updates.get("isolation.blocked") is True
        reason = result.state_updates.get("isolation.block_reason", "")
        assert "bash_execute" in reason


# ═══════════════════════════════════════════════════════════════
# P1: _workspace_merge_ops — worktree 残留清理（与降级无关，保留）
# ═══════════════════════════════════════════════════════════════


class _MockMergeOps:
    """测试用 Mock，模拟 _GitOpsMixin + _MergeOpsMixin 的最小接口。"""

    def __init__(self, workspace_root=None):
        self._config = {"workspace": {"root": str(workspace_root or "/tmp/test_ws")}}
        self._git_calls = []
        self._git_responses = {}
        self._ws_meta_store = {}
        self._merge_locks = {}
        self._global_lock = threading.Lock()

    def _run_git(self, *args, cwd=None, timeout=30):
        self._git_calls.append({"args": args, "cwd": str(cwd)})
        key = " ".join(args)
        if key in self._git_responses:
            return self._git_responses[key]
        return (0, "", "")

    def _git_add_commit_if_dirty(self, cwd, message):
        """Mock：记录 auto-save 提交调用。真实实现的成败由调用方用
        _git_responses['status --porcelain -uno'] 模拟残留脏改动体现。"""
        self._git_calls.append({"args": ("_git_add_commit_if_dirty", message), "cwd": str(cwd)})

    def _get_workspace_root(self):
        return Path(self._config.get("workspace", {}).get("root", "/tmp/test_ws"))

    def _get_merge_lock(self, project_root):
        with self._global_lock:
            if project_root not in self._merge_locks:
                self._merge_locks[project_root] = threading.Lock()
            return self._merge_locks[project_root]

    # 从 _MergeOpsMixin 导入方法
    from isolation._workspace_merge_ops import _MergeOpsMixin
    for _method_name in [
        "_cleanup_unstaged_changes", "on_eval_failed", "on_task_failed",
    ]:
        _method = getattr(_MergeOpsMixin, _method_name)
        locals()[_method_name] = _method

    # 从 _GitOpsMixin 导入 auto-save 安全校验（BUG-FIX-fix_20260627_autosave_silent_loss）
    from isolation._workspace_git_ops import _GitOpsMixin
    locals()["_autosave_before_worktree"] = _GitOpsMixin._autosave_before_worktree


class TestCleanupUnstagedChanges:
    """合并后 unstaged 修改处理。

    安全契约（BUG-FIX-fix_20260627_unstaged_data_loss）：
    合并后检测到 unstaged 变更时只记录警告，绝不调用 `git checkout -- .`
    丢弃——那些改动可能来自 task 运行期间用户/外部对 project_root 的修改，
    或 auto-save 提交失败残留的脏改动，丢弃即永久丢失。
    """

    def test_cleanup_unstaged_changes_does_not_discard(self, tmp_path):
        """有 unstaged 修改时只告警，不调用 git checkout 丢弃。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain"] = (
            0, " M src/main.py\n D src/old.py\n", ""
        )

        ops._cleanup_unstaged_changes(str(tmp_path))

        # 核心安全断言：任何情况下都不得调用 checkout 丢弃工作区改动
        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0
        # 只做了一次 status 检测，未触发任何写操作
        assert all(c["args"][0] != "checkout" for c in ops._git_calls)

    def test_cleanup_no_unstaged_skips_checkout(self, tmp_path):
        """无 unstaged 修改时不调用 git checkout。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain"] = (0, "", "")

        ops._cleanup_unstaged_changes(str(tmp_path))

        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0

    def test_cleanup_only_staged_skips_checkout(self, tmp_path):
        """只有 staged 修改（无 unstaged）时不调用 git checkout。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain"] = (
            0, "M  src/main.py\nA  src/new.py\n", ""
        )

        ops._cleanup_unstaged_changes(str(tmp_path))

        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0

    def test_cleanup_nonexistent_path_skips(self):
        """project_root 不存在时直接跳过。"""
        ops = _MockMergeOps()
        ops._cleanup_unstaged_changes("/nonexistent/path/xyz")
        assert len(ops._git_calls) == 0

    def test_cleanup_git_status_fails_skips(self, tmp_path):
        """git status 命令失败时跳过清理。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain"] = (-1, "", "error")

        ops._cleanup_unstaged_changes(str(tmp_path))

        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0


class TestAutosaveBeforeWorktree:
    """auto-save 安全校验（BUG-FIX-fix_20260627_autosave_silent_loss）。

    安全契约：worktree 创建前 auto-save 若未能把脏改动提交干净，
    必须中断 worktree 创建，绝不让未提交的改动随旧 HEAD 进入 task 分支
    而在合并后丢失。宁可任务失败，也不丢数据。
    """

    def test_abort_when_dirty_after_autosave(self, tmp_path):
        """auto-save 后仍残留已跟踪脏改动 → 抛 RuntimeError 中断。"""
        ops = _MockMergeOps()
        # 模拟 _git_add_commit_if_dirty 提交失败：已跟踪文件仍 dirty
        ops._git_responses["status --porcelain -uno"] = (
            0, " M src/main.py\n", ""
        )

        with pytest.raises(RuntimeError, match="auto-save 失败"):
            ops._autosave_before_worktree(tmp_path, "chore: auto-save", "task-001")

    def test_proceed_when_clean_after_autosave(self, tmp_path):
        """auto-save 后工作区干净 → 正常放行，不抛异常。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain -uno"] = (0, "", "")

        ops._autosave_before_worktree(tmp_path, "chore: auto-save", "task-001")  # 不抛

        # 确实触发了 auto-save 提交
        autosave_calls = [
            c for c in ops._git_calls
            if c["args"][0] == "_git_add_commit_if_dirty"
        ]
        assert len(autosave_calls) == 1

    def test_proceed_when_status_check_fails(self, tmp_path):
        """状态校验命令本身失败 → 放行不阻塞（避免 git 偶发故障放大成任务失败）。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain -uno"] = (-1, "", "error")

        ops._autosave_before_worktree(tmp_path, "chore: auto-save", "task-001")  # 不抛

    def test_abort_message_identifies_task(self, tmp_path):
        """中断信息包含 task_id 和路径，便于定位。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain -uno"] = (
            0, " M src/critical.py\n", ""
        )

        with pytest.raises(RuntimeError) as exc_info:
            ops._autosave_before_worktree(tmp_path, "msg", "task-ZZZ")

        msg = str(exc_info.value)
        assert "task-ZZZ" in msg
        assert "src/critical.py" in msg


class TestWorktreeDestroyOnlyAfterMerge:
    """回归测试：worktree 销毁只发生在评估通过+合并完成后。"""

    def test_on_task_cleanup_removed_from_class(self):
        """on_task_cleanup 方法已从 _MergeOpsMixin 删除。"""
        from isolation._workspace_merge_ops import _MergeOpsMixin
        assert not hasattr(_MergeOpsMixin, "on_task_cleanup"), (
            "on_task_cleanup 必须删除：它是引擎结束时无条件销毁 worktree 的入口"
        )

    def test_cleanup_orphaned_worktrees_removed_from_class(self):
        """_cleanup_orphaned_worktrees 方法已删除。"""
        from isolation._workspace_merge_ops import _MergeOpsMixin
        assert not hasattr(_MergeOpsMixin, "_cleanup_orphaned_worktrees"), (
            "_cleanup_orphaned_worktrees 必须删除：会误删失败任务的 worktree"
        )

    def test_executor_does_not_call_on_task_cleanup(self):
        """task_executor 的 _cleanup_after_engine 不再调用 on_task_cleanup。"""
        executor_src = Path(
            "src/infrastructure/task_executor.py"
        ).read_text(encoding="utf-8")
        assert ".on_task_cleanup(" not in executor_src, (
            "task_executor.py 不得再调用 lifecycle.on_task_cleanup(...)"
        )

    def test_failed_task_worktree_survives_for_retry(self, tmp_path):
        """失败任务的工作空间在引擎结束时必须保留，供重试复用。"""
        ops = _MockMergeOps()
        ws = tmp_path / "container_abc__wt_retry_me"
        ws.mkdir()
        ws_meta = {"mode": "worktree", "path": str(ws),
                   "project_root": str(tmp_path), "branch": "task/b1",
                   "max_retries": 3}

        result = ops.on_eval_failed("t1abc1234", str(ws), ws_meta)
        assert result["action"] == "retry"
        assert ws.exists(), "评估失败重试期间 worktree 必须保留"

        fail_result = ops.on_task_failed(str(ws), ws_meta)
        assert fail_result["action"] == "none"
        assert ws.exists(), "任务失败后 worktree 必须保留供重试"


# ═══════════════════════════════════════════════════════════════
# HostProvider — host 模式不再建 checkpoint
# ═══════════════════════════════════════════════════════════════


class TestHostProviderNoCheckpoint:
    """host 模式不再创建/恢复文件检查点。"""

    def test_host_provider_has_no_checkpoint_manager(self):
        """HostProvider 不再持有 CheckpointManager。"""
        from isolation.providers.host_provider import HostProvider
        provider = HostProvider()
        assert not hasattr(provider, "_checkpoint_manager"), (
            "HostProvider 不应再持有 _checkpoint_manager（host 模式不建 checkpoint）"
        )

    def test_host_provider_no_checkpoint_import(self):
        """host_provider.py 不再 import CheckpointManager。"""
        src = Path("src/isolation/providers/host_provider.py").read_text(
            encoding="utf-8"
        )
        assert "CheckpointManager" not in src, (
            "host_provider.py 不应再引用 CheckpointManager"
        )
