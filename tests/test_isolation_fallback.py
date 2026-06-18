"""Docker 隔离安全修复测试。

覆盖修复点：
- P0：fallback 降级从 allow 改为 fail，container 模式下 Docker 不可用时直接报错
- P0：fallback:deny 对 bash_execute 生效，阻止宿主机执行
- P0：Docker daemon 可用性检查逻辑
- P1：worktree 残留自动清理
- P1：合并后 unstaged 修改清理

涉及模块：src/isolation/decider.py, executor.py, plugin.py, _workspace_merge_ops.py
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import threading

from isolation.decider import IsolationDecider, IsolationError
from isolation.executor import IsolationExecutor
from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.types import IsolationLevel
from pipeline.plugin import PluginContext


# ═══════════════════════════════════════════════════════════════
# P0: IsolationDecider — 禁止静默降级
# ═══════════════════════════════════════════════════════════════


class TestDeciderNoSilentFallback:
    """P0: decider 在无可用 provider 时不再静默降级到 HOST。"""

    @staticmethod
    def _make_decider(default_fallback="deny"):
        """创建使用空配置的 decider，可控 fallback。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._default = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
            fallback=default_fallback,
        )
        return IsolationDecider(policy_loader=loader), loader

    # ── 核心安全：无可用 provider 时，即使 fallback=allow 也必须报错 ──

    @pytest.mark.asyncio
    async def test_all_unavailable_fallback_allow_still_raises(self):
        """P0: container+host 都不可用 + fallback=allow → 抛 IsolationError（不静默降级）。"""
        decider, _ = self._make_decider(default_fallback="allow")
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: False}

        with pytest.raises(IsolationError, match="无可用降级目标"):
            await decider.decide("flexible_tool", available_providers=available)

    @pytest.mark.asyncio
    async def test_all_unavailable_fallback_deny_raises(self):
        """P0: container+host 都不可用 + fallback=deny → 抛 IsolationError。"""
        decider, _ = self._make_decider(default_fallback="deny")
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: False}

        with pytest.raises(IsolationError):
            await decider.decide("strict_tool", available_providers=available)

    # ── container 不可用时的降级决策 ──

    @pytest.mark.asyncio
    async def test_container_unavailable_deny_raises(self):
        """P0: container 不可用 + fallback=deny → 抛错（不降级到 host）。"""
        decider, _ = self._make_decider(default_fallback="deny")
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: True}

        with pytest.raises(IsolationError, match="禁止降级"):
            await decider.decide("bash_execute", available_providers=available)

    @pytest.mark.asyncio
    async def test_container_unavailable_allow_degrades(self):
        """container 不可用 + fallback=allow + host 可用 → 降级到 host。"""
        decider, _ = self._make_decider(default_fallback="allow")
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: True}

        policy = await decider.decide("safe_tool", available_providers=available)
        assert policy.isolation == IsolationLevel.HOST

    # ── bash_execute 专用策略 ──

    @pytest.mark.asyncio
    async def test_bash_execute_deny_by_tool_policy(self):
        """P0: bash_execute 配置 fallback=deny 在 container 不可用时阻止降级。"""
        decider, loader = self._make_decider()
        loader._tools["bash_execute"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
            fallback="deny",
        )
        available = {IsolationLevel.CONTAINER: False, IsolationLevel.HOST: True}

        with pytest.raises(IsolationError, match="禁止降级"):
            await decider.decide("bash_execute", available_providers=available)

    @pytest.mark.asyncio
    async def test_bash_execute_container_available(self):
        """bash_execute 在 container 可用时正常返回 container 策略。"""
        decider, loader = self._make_decider()
        loader._tools["bash_execute"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
            fallback="deny",
        )
        available = {IsolationLevel.CONTAINER: True, IsolationLevel.HOST: True}

        policy = await decider.decide("bash_execute", available_providers=available)
        assert policy.isolation == IsolationLevel.CONTAINER
        assert policy.fallback == "deny"

    # ── 无可用性检查时直返 ──

    @pytest.mark.asyncio
    async def test_no_availability_check_returns_original(self):
        """不做可用性检查时直接返回原始策略。"""
        decider, _ = self._make_decider()
        policy = await decider.decide("any_tool")
        assert policy.isolation == IsolationLevel.CONTAINER


# ═══════════════════════════════════════════════════════════════
# P0: IsolationExecutor — blocked context + 安全检查
# ═══════════════════════════════════════════════════════════════


class TestExecutorSecurity:
    """P0: 执行器安全检查 — blocked context 和无 context 拒绝。"""

    @staticmethod
    def _make_executor(docker_available=False):
        mock_provider = MagicMock()
        mock_provider.is_available = AsyncMock(
            return_value=(docker_available, "test reason"),
        )
        executor = IsolationExecutor(docker_provider=mock_provider)
        executor._docker_available = docker_available
        return executor

    @pytest.mark.asyncio
    async def test_blocked_context_returns_error(self):
        """P0: 被策略阻止（blocked=True）的工具直接返回错误，不执行。"""
        executor = self._make_executor(docker_available=False)
        state = {
            "execution_contexts": [{
                "tool_name": "bash_execute",
                "provider": "denied",
                "blocked": True,
                "reason": "force_host_denied_by_policy",
            }],
        }

        result = await executor.execute_tool(
            state=state,
            tool_name="bash_execute",
            tool_args={"command": "rm -rf /"},
            tool_func=lambda args: {"output": "should not reach here"},
            timeout=10,
        )

        assert result["success"] is False
        assert "被隔离策略阻止" in result["error"]
        assert result["tool_name"] == "bash_execute"

    @pytest.mark.asyncio
    async def test_host_provider_executes_on_host(self):
        """P0: provider=host 时在宿主机执行（IsolationGuard 允许降级的场景）。"""
        executor = self._make_executor(docker_available=False)
        state = {
            "execution_contexts": [{
                "tool_name": "file_read",
                "provider": "host",
                "level": "host",
                "reason": "policy_fallback",
            }],
        }

        def mock_tool(args):
            return {"content": "file content"}

        result = await executor.execute_tool(
            state=state,
            tool_name="file_read",
            tool_args={"path": "/tmp/test.txt"},
            tool_func=mock_tool,
            timeout=10,
        )

        assert result["success"] is True
        assert result["data"]["content"] == "file content"

    @pytest.mark.asyncio
    async def test_no_context_defaults_to_host(self):
        """无执行上下文时默认 host 执行（IsolationGuard 未为该工具生成 context）。"""
        executor = self._make_executor(docker_available=False)
        state = {"execution_contexts": []}

        result = await executor.execute_tool(
            state=state,
            tool_name="unknown_tool",
            tool_args={},
            tool_func=lambda args: {"ok": True},
            timeout=10,
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_docker_unavailable_provider_docker_with_context_falls_to_host(self):
        """Docker 不可用 + context provider=host → 在宿主机执行成功。

        重构后 executor 按 context 的 provider 字段决定执行路径：
        - provider=host → 走宿主机
        - provider=docker → 走容器（不自动降级，降级由 isolation_guard 决策）
        """
        executor = self._make_executor(docker_available=False)
        state = {
            "execution_contexts": [{
                "tool_name": "safe_tool",
                "provider": "host",  # isolation_guard 已降级为 host
                "level": "host",
                "reason": "policy_fallback",
            }],
        }

        result = await executor.execute_tool(
            state=state,
            tool_name="safe_tool",
            tool_args={},
            tool_func=lambda args: {"executed": True},
            timeout=10,
        )

        # provider=host 时在宿主机执行成功
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_blocked_context_does_not_call_tool_func(self):
        """P0: blocked context 时工具函数完全不被调用。"""
        executor = self._make_executor(docker_available=False)
        call_count = 0

        def counting_tool(args):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        state = {
            "execution_contexts": [{
                "tool_name": "dangerous_tool",
                "provider": "denied",
                "blocked": True,
                "reason": "policy_fallback_denied",
            }],
        }

        result = await executor.execute_tool(
            state=state,
            tool_name="dangerous_tool",
            tool_args={"command": "dangerous"},
            tool_func=counting_tool,
            timeout=10,
        )

        assert result["success"] is False
        assert call_count == 0, "工具函数不应被调用"


# ═══════════════════════════════════════════════════════════════
# BUG-FIX-fix_20260615_eval_pipeline_not_end:
# 问题根因: 工具返回 ToolExecutionResult 时，IsolationExecutor 的 host 路径
#           把裸 pydantic 对象原样塞进 result["data"]（executor.py:289），
#           既不 to_dict 也不补顶层 metadata。而 stop_check 插件
#           (_check_task_evaluate_result) 用 isinstance(data, dict) 守卫，
#           pydantic 对象不是 dict → 跳过 → 评估通过后 end 信号永不发出，
#           管道只能靠每 3 轮的 TaskService 轮询兜底才停（滞后数轮 LLM 调用）。
# 修复方案: host 路径对 ToolExecutionResult 走与 tool_core._execute_single_tool
#           相同的归一化（to_dict(slim=True)）并把 metadata 拷到顶层，
#           使 stop_check 能读到 data["metadata"]["result"]。
# ═══════════════════════════════════════════════════════════════


class TestExecutorResultNormalization:
    """host 路径返回 ToolExecutionResult 时必须归一化，让下游插件可消费。

    覆盖契约：result["data"] 必须是 dict（slim to_dict），
    且顶层有 metadata 键，使 stop_check._check_task_evaluate_result 命中。
    """

    @staticmethod
    def _make_executor(docker_available=False):
        mock_provider = MagicMock()
        mock_provider.is_available = AsyncMock(
            return_value=(docker_available, "test reason"),
        )
        executor = IsolationExecutor(docker_provider=mock_provider)
        executor._docker_available = docker_available
        return executor

    @pytest.mark.asyncio
    async def test_tool_execution_result_normalized_to_dict(self):
        """ToolExecutionResult 必须被归一化为 dict，data 是 dict 而非 pydantic 对象。"""
        from tools.types import create_success_result

        executor = self._make_executor(docker_available=False)
        state = {"execution_contexts": []}

        def tool_func(args):
            return create_success_result(
                data={"task_id": "t1", "overall_passed": True, "metrics": []},
                metadata={"action": "auto_complete", "result": "completed",
                          "message": "ok"},
            )

        result = await executor.execute_tool(
            state=state, tool_name="task_evaluate", tool_args={},
            tool_func=tool_func, timeout=10,
        )

        assert result["success"] is True
        # 守卫：stop_check 用 isinstance(data, dict) 判定，必须是 dict
        assert isinstance(result["data"], dict), \
            "data 必须是 dict，否则 stop_check 会跳过此条 tool_result"

    @pytest.mark.asyncio
    async def test_metadata_result_reachable_by_stop_check(self):
        """顶层必须有 metadata 键，且 metadata.result='completed'，
        使 stop_check._check_task_evaluate_result 命中并发 end。"""
        from tools.types import create_success_result

        executor = self._make_executor(docker_available=False)
        state = {"execution_contexts": []}

        def tool_func(args):
            return create_success_result(
                data={"task_id": "t1", "overall_passed": True, "metrics": []},
                metadata={"action": "auto_complete", "result": "completed",
                          "message": "评估通过，任务已完成"},
            )

        result = await executor.execute_tool(
            state=state, tool_name="task_evaluate", tool_args={},
            tool_func=tool_func, timeout=10,
        )

        # stop_check 读 data.get("metadata", {}).get("result")
        metadata = result["data"].get("metadata", {})
        assert metadata.get("result") == "completed", \
            "stop_check 读不到 metadata.result，评估通过后不会发 end 信号"

    @pytest.mark.asyncio
    async def test_plain_dict_tool_unchanged(self):
        """普通 dict 返回值不变（不破坏 file_read 等现有工具）。"""
        executor = self._make_executor(docker_available=False)
        state = {"execution_contexts": []}

        def tool_func(args):
            return {"content": "file content"}

        result = await executor.execute_tool(
            state=state, tool_name="file_read", tool_args={"path": "/tmp/x"},
            tool_func=tool_func, timeout=10,
        )

        assert result["success"] is True
        assert result["data"] == {"content": "file content"}
        # 普通 dict 无 metadata，不应凭空生成顶层 metadata 键
        assert "metadata" not in result


# ═══════════════════════════════════════════════════════════════
# P0: IsolationGuard — force_host 和 metadata 路径加固
# ═══════════════════════════════════════════════════════════════


def _make_guard(docker_available=False, force_host=False):
    """创建 IsolationGuard 实例，使用空策略配置。"""
    from plugins.input.isolation_guard.plugin import IsolationGuard

    guard = IsolationGuard(config={
        "docker_available": docker_available,
        "force_host": force_host,
    })
    # 替换 decider 为使用空配置的实例
    loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
    loader._default = ToolIsolationPolicy(
        isolation=IsolationLevel.CONTAINER,
        fallback="deny",
    )
    # bash_execute 策略：容器隔离 + 禁止降级
    loader._tools["bash_execute"] = ToolIsolationPolicy(
        isolation=IsolationLevel.CONTAINER,
        fallback="deny",
    )
    # file_read 策略：容器隔离 + 允许降级
    loader._tools["file_read"] = ToolIsolationPolicy(
        isolation=IsolationLevel.CONTAINER,
        fallback="allow",
    )
    guard._decider = IsolationDecider(policy_loader=loader)
    return guard


def _make_ctx(state=None):
    """创建最小 PluginContext。"""
    return PluginContext(state=state or {}, _services={})


class TestGuardForceHostBlocked:
    """P0: force_host 路径不能绕过 fallback:deny 策略。"""

    def test_force_host_denies_bash_execute(self):
        """P0: force_host=True + bash_execute(fallback:deny) → blocked。"""
        guard = _make_guard(docker_available=False, force_host=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"
        assert result["tool_name"] == "bash_execute"

    def test_force_host_allows_fallback_allow_tool(self):
        """force_host=True + file_read(fallback:allow) → host 执行。"""
        guard = _make_guard(docker_available=False, force_host=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("file_read", ctx)

        assert result.get("blocked") is not True
        assert result["provider"] == "host"

    def test_force_host_allows_unknown_deny_tool(self):
        """force_host=True + 未知工具(默认 fallback:deny) → blocked。"""
        guard = _make_guard(docker_available=False, force_host=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("unknown_dangerous_tool", ctx)

        # 默认策略是 fallback:deny，所以 force_host 也应该被阻止
        assert result["blocked"] is True


class TestGuardMetadataPath:
    """P0: metadata 路径在 Docker 不可用时检查策略。"""

    def test_metadata_container_docker_unavailable_deny_blocked(self):
        """P0: metadata 要求 container + Docker 不可用 + fallback:deny → blocked。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={"task_id": "test-task"})
        # Mock _get_task_metadata 返回 container 隔离
        guard._get_task_metadata = lambda c: {"isolation_level": "container"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"
        assert "fallback_denied" in result["reason"]

    def test_metadata_container_docker_unavailable_allow_host(self):
        """metadata 要求 container + Docker 不可用 + fallback:allow → host。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "container"}

        result = guard._decide_isolation("file_read", ctx)

        assert result.get("blocked") is not True
        assert result["provider"] == "host"
        assert "fallback" in result["reason"]

    def test_metadata_container_docker_available(self):
        """metadata 要求 container + Docker 可用 → docker。"""
        guard = _make_guard(docker_available=True)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "container"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "docker"
        assert result.get("blocked") is not True

    def test_metadata_host(self):
        """metadata 要求 host → 直接 host（不检查 Docker 可用性）。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={"task_id": "test-task"})
        guard._get_task_metadata = lambda c: {"isolation_level": "host"}

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "host"
        assert result.get("blocked") is not True


class TestGuardPolicyPath:
    """P0: 策略路径（无 metadata 覆盖）的降级逻辑。"""

    def test_policy_container_docker_unavailable_deny(self):
        """P0: 策略 container + Docker 不可用 + fallback:deny → blocked。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx()
        # 不设置 task metadata → 走策略路径

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["blocked"] is True
        assert result["provider"] == "denied"

    def test_policy_container_docker_unavailable_allow(self):
        """策略 container + Docker 不可用 + fallback:allow → host。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx()

        result = guard._decide_isolation("file_read", ctx)

        assert result.get("blocked") is not True
        assert result["provider"] == "host"

    def test_policy_container_docker_available(self):
        """策略 container + Docker 可用 → docker。"""
        guard = _make_guard(docker_available=True)
        ctx = _make_ctx()

        result = guard._decide_isolation("bash_execute", ctx)

        assert result["provider"] == "docker"
        assert result.get("blocked") is not True


class TestGuardBlockedPropagates:
    """P0: blocked 工具在 execute 方法中正确传播到 isolation.blocked。"""

    @pytest.mark.asyncio
    async def test_blocked_tool_sets_security_decision(self):
        """P0: 被阻止的工具设置 isolation.blocked = True。"""
        guard = _make_guard(docker_available=False)
        ctx = _make_ctx(state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "rm -rf /"}}],
        })

        result = await guard.execute(ctx)

        # 检查 execution_contexts 中有 blocked 标记
        contexts = result.state_updates.get("execution_contexts", [])
        blocked = [c for c in contexts if c.get("blocked")]
        assert len(blocked) == 1
        assert blocked[0]["tool_name"] == "bash_execute"

        # 重构后 isolation_guard 写 isolation.blocked，不再写 security.decision
        assert result.state_updates.get("isolation.blocked") is True
        reason = result.state_updates.get("isolation.block_reason", "")
        assert "bash_execute" in reason


# ═══════════════════════════════════════════════════════════════
# P1: _workspace_merge_ops — worktree 残留清理
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

    def _get_workspace_root(self):
        return Path(self._config.get("workspace", {}).get("root", "/tmp/test_ws"))

    def _get_merge_lock(self, project_root):
        with self._global_lock:
            if project_root not in self._merge_locks:
                self._merge_locks[project_root] = threading.Lock()
            return self._merge_locks[project_root]

    # 从 _MergeOpsMixin 导入方法
    from isolation._workspace_merge_ops import _MergeOpsMixin
    # 手动绑定方法
    # BUG-FIX-fix_20260619_worktree_destroyed_on_retry:
    # on_task_cleanup / _cleanup_orphaned_worktrees 已删除（在引擎 finally
    # 无条件销毁 worktree，导致失败重试无法复用）。
    # 保留 on_eval_failed / on_task_failed：失败时明确保留 worktree 供重试。
    for _method_name in [
        "_cleanup_unstaged_changes", "on_eval_failed", "on_task_failed",
    ]:
        _method = getattr(_MergeOpsMixin, _method_name)
        locals()[_method_name] = _method


class TestCleanupUnstagedChanges:
    """P1: 合并后 unstaged 修改清理。"""

    def test_cleanup_unstaged_changes_calls_git_checkout(self, tmp_path):
        """P1: 有 unstaged 修改时调用 git checkout 恢复。"""
        ops = _MockMergeOps()
        # git status --porcelain 返回 unstaged 修改
        ops._git_responses["status --porcelain"] = (
            0, " M src/main.py\n D src/old.py\n", ""
        )

        ops._cleanup_unstaged_changes(str(tmp_path))

        # 验证调用了 git checkout
        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 1

    def test_cleanup_no_unstaged_skips_checkout(self, tmp_path):
        """P1: 无 unstaged 修改时不调用 git checkout。"""
        ops = _MockMergeOps()
        # git status 返回空（干净状态）
        ops._git_responses["status --porcelain"] = (0, "", "")

        ops._cleanup_unstaged_changes(str(tmp_path))

        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0

    def test_cleanup_only_staged_skips_checkout(self, tmp_path):
        """P1: 只有 staged 修改（无 unstaged）时不调用 git checkout。"""
        ops = _MockMergeOps()
        # 'M' 在第一列是 staged，第二列空表示无 unstaged
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
        """P1: project_root 不存在时直接跳过。"""
        ops = _MockMergeOps()
        ops._cleanup_unstaged_changes("/nonexistent/path/xyz")
        assert len(ops._git_calls) == 0

    def test_cleanup_git_status_fails_skips(self, tmp_path):
        """P1: git status 命令失败时跳过清理。"""
        ops = _MockMergeOps()
        ops._git_responses["status --porcelain"] = (-1, "", "error")

        ops._cleanup_unstaged_changes(str(tmp_path))

        checkout_calls = [
            c for c in ops._git_calls
            if c["args"] == ("checkout", "--", ".")
        ]
        assert len(checkout_calls) == 0


class TestWorktreeDestroyOnlyAfterMerge:
    """回归测试：worktree 销毁只发生在评估通过+合并完成后。

    BUG-FIX-fix_20260619_worktree_destroyed_on_retry:
    原实现 on_task_cleanup 在引擎结束时无条件销毁 worktree，
    导致任务失败后重试时 worktree 已被销毁、无法复用。
    修复后销毁点收敛到 on_eval_passed → _cleanup_worktree 唯一一条链路。
    """

    def test_on_task_cleanup_removed_from_class(self):
        """on_task_cleanup 方法已从 _MergeOpsMixin 删除。"""
        from isolation._workspace_merge_ops import _MergeOpsMixin
        assert not hasattr(_MergeOpsMixin, "on_task_cleanup"), (
            "on_task_cleanup 必须删除：它是引擎结束时无条件销毁 worktree 的入口"
        )

    def test_cleanup_orphaned_worktrees_removed_from_class(self):
        """_cleanup_orphaned_worktrees 方法已删除。

        它扫删所有 __wt_ 孤儿目录，会把失败任务的 worktree 误删。
        """
        from isolation._workspace_merge_ops import _MergeOpsMixin
        assert not hasattr(_MergeOpsMixin, "_cleanup_orphaned_worktrees"), (
            "_cleanup_orphaned_worktrees 必须删除：会误删失败任务的 worktree"
        )

    def test_executor_does_not_call_on_task_cleanup(self):
        """task_executor 的 _cleanup_after_engine 不再调用 on_task_cleanup。"""
        from pathlib import Path
        executor_src = Path(
            "src/infrastructure/task_executor.py"
        ).read_text(encoding="utf-8")
        # 检查调用形式（.on_task_cleanup(...)）不存在，而非字符串字面量
        # （注释里说明 bug 时会提到该方法名）。
        assert ".on_task_cleanup(" not in executor_src, (
            "task_executor.py 不得再调用 lifecycle.on_task_cleanup(...)"
        )

    def test_failed_task_worktree_survives_for_retry(self, tmp_path):
        """失败任务的工作空间在引擎结束时必须保留，供重试复用。

        验证：on_eval_failed（未超限时）返回 retry 且不销毁 worktree；
        on_task_failed 同样保留 worktree。合并链路 on_eval_passed 仍是唯一销毁点。
        """
        ops = _MockMergeOps()
        # worktree 目录必须仍然存在
        ws = tmp_path / "container_abc__wt_retry_me"
        ws.mkdir()
        ws_meta = {"mode": "worktree", "path": str(ws),
                   "project_root": str(tmp_path), "branch": "task/b1",
                   "max_retries": 3}

        # 评估失败但未超限 → 应保留 worktree 供重试
        result = ops.on_eval_failed("t1abc1234", str(ws), ws_meta)
        assert result["action"] == "retry"
        assert ws.exists(), "评估失败重试期间 worktree 必须保留"

        # 任务异常/失败 → on_task_failed 明确不清理 worktree
        fail_result = ops.on_task_failed(str(ws), ws_meta)
        assert fail_result["action"] == "none"
        assert ws.exists(), "任务失败后 worktree 必须保留供重试"


# ═══════════════════════════════════════════════════════════════
# 回归测试：降级路径安全矩阵
# ═══════════════════════════════════════════════════════════════


class TestFallbackSecurityMatrix:
    """回归测试：验证修复后的降级路径安全矩阵。"""

    @pytest.mark.parametrize(
        "tool_name,docker_available,force_host,expect_blocked",
        [
            # bash_execute (fallback:deny) + Docker 不可用 → 所有路径都 blocked
            ("bash_execute", False, False, True),
            ("bash_execute", False, True, True),   # force_host 也被阻止
            # file_read (fallback:allow) + Docker 不可用 → host 执行
            ("file_read", False, False, False),
            ("file_read", False, True, False),     # force_host 允许
            # Docker 可用 → 正常 container 执行
            ("bash_execute", True, False, False),
            ("file_read", True, False, False),
        ],
    )
    def test_guard_fallback_matrix(
        self, tool_name, docker_available, force_host, expect_blocked,
    ):
        """回归: 验证工具/配置组合的隔离决策。"""
        guard = _make_guard(docker_available=docker_available, force_host=force_host)
        ctx = _make_ctx()

        result = guard._decide_isolation(tool_name, ctx)

        if expect_blocked:
            assert result.get("blocked") is True, (
                f"期望 {tool_name} 被阻止 (docker={docker_available}, "
                f"force_host={force_host})，但未阻止"
            )
        else:
            assert result.get("blocked") is not True, (
                f"期望 {tool_name} 不被阻止 (docker={docker_available}, "
                f"force_host={force_host})，但被阻止了"
            )

    @pytest.mark.parametrize(
        "tool_name,docker_available,expect_provider",
        [
            ("bash_execute", True, "docker"),
            ("bash_execute", False, "denied"),     # blocked → provider=denied
            ("file_read", True, "docker"),
            ("file_read", False, "host"),          # fallback:allow → host
        ],
    )
    def test_metadata_path_matrix(self, tool_name, docker_available, expect_provider):
        """回归: metadata 路径的隔离决策。"""
        guard = _make_guard(docker_available=docker_available)
        ctx = _make_ctx(state={"task_id": "test"})
        guard._get_task_metadata = lambda c: {"isolation_level": "container"}

        result = guard._decide_isolation(tool_name, ctx)
        assert result["provider"] == expect_provider, (
            f"期望 {tool_name} provider={expect_provider} "
            f"(docker={docker_available})，实际={result['provider']}"
        )
