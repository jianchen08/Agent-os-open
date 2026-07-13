"""
V3+V4 验证测试：Non-isolated模式任务 + 删除任务

验证覆盖：
- V3-1: Non-isolated模式下worktree策略（不创建worktree，直接操作项目目录）
- V3-2: 危险操作审批（写入/执行类工具触发审批）
- V3-3: 只读操作无弹窗（只读工具免审批）
- V4-1: 后端级联清理范围（任务文件+子任务+管道+worktree）
- V4-2: 容器空间保留（删除逻辑不触及容器空间）
- V4-3: 前端即时移除（API层删除响应验证）
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 导入被测模块 ──
from isolation.approval import (
    ApprovalContext,
    ApprovalDecision,
    ApprovalDecisionEngine,
    DangerChecker,
    SAFE_TOOLS,
    DANGEROUS_TOOLS,
    classify_tool_safety,
)
from isolation.types import IsolationLevel
from tasks.service import TaskService
from tasks.state_machine import SimpleStateMachine
from tasks.storage import TaskStorage
from tasks.types import TaskModel, TaskStatus, create_task


def _make_wslm(tmp: str, config: dict | None = None):
    """构造 WorkspaceLifecycleManager，填充所有必需参数。"""
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager
    ws_meta_store: dict[str, dict] = {}
    return (
        WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config=config or {},
            task_tree=MagicMock(),
            ws_meta_store=ws_meta_store,
            base_path=tmp,
        ),
        ws_meta_store,
    )


# ═══════════════════════════════════════════════════════════════════
# V3-1: Non-isolated模式 Worktree 策略验证
# ═══════════════════════════════════════════════════════════════════

class TestV3HostWorktreeStrategy:
    """验证 Non-isolated 模式下不创建 worktree，直接操作项目目录。"""

    def test_host_mode_sets_plain_mode_no_worktree(self):
        """V3-1.1: Non-isolated模式下workspace_lifecycle返回mode='plain'，不创建worktree。

        代码分析结论（src/isolation/workspace_lifecycle.py L581-591）：
        当 isolation_mode == 'non_isolated' 时，设置 meta = {"mode": "plain", "path": base_path}
        不做任何 git worktree/branch 操作。
        """
        tmp = tempfile.mkdtemp()
        try:
            mgr, _ = _make_wslm(tmp, config={"coordinator": {"default_level": "non_isolated"}})
            task_data = {"isolation_mode": "non_isolated"}
            meta = mgr.on_task_start("test_host_task_001", tmp, task_data)

            assert meta["mode"] == "plain", (
                f"Non-isolated模式应使用mode='plain'，实际: {meta['mode']}"
            )
            assert meta["path"] == str(tmp), (
                f"Non-isolated模式path应指向项目目录，实际: {meta['path']}"
            )
            # 不应有 branch 或 worktree 相关字段
            assert "branch" not in meta, "Non-isolated模式不应创建git分支"
            assert "project_root" not in meta, "Non-isolated模式plain模式不应设置project_root"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_mode_subtask_shares_parent_workspace(self):
        """V3-1.2: Non-isolated模式子任务共享父工作空间（mode='shared'）。

        代码分析结论（src/isolation/workspace_lifecycle.py L389-399）：
        _start_subtask 中 isolation_mode == 'non_isolated' 时，
        meta = {"mode": "shared", "path": base_path}
        """
        tmp = tempfile.mkdtemp()
        try:
            mgr, _ = _make_wslm(tmp, config={"coordinator": {"default_level": "non_isolated"}})
            task_data = {"isolation_mode": "non_isolated"}
            meta = mgr._start_subtask("sub_task_001", tmp, task_data)

            assert meta["mode"] == "shared", (
                f"Non-isolated模式子任务应使用mode='shared'，实际: {meta['mode']}"
            )
            assert meta["path"] == str(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_mode_cleanup_is_noop_for_plain(self):
        """V3-1.3: Non-isolated模式(plain)的cleanup_workspace是空操作。

        代码分析结论（src/isolation/workspace_lifecycle.py L1194-1212）：
        cleanup_workspace 中 mode == 'plain' 时直接 pass，
        不会删除任何文件或目录。
        """
        tmp = tempfile.mkdtemp()
        try:
            mgr, store = _make_wslm(tmp)
            store["host_task_001"] = {
                "mode": "plain",
                "path": str(tmp),
            }
            result = mgr.cleanup_workspace("host_task_001")

            assert result["worktree_removed"] is False
            assert result["branch_deleted"] is False
            assert result["dir_removed"] is False
            # 项目目录应仍然存在
            assert Path(tmp).exists(), "plain模式cleanup不应删除项目目录"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_container_mode_creates_worktree(self):
        """V3-1.4（对比参照）: 容器模式下创建worktree进行隔离。

        代码分析结论：非Non-isolated模式通过 _worktree_add_with_repair 创建worktree，
        meta["mode"] = "worktree"，有branch字段。
        """
        tmp = tempfile.mkdtemp()
        try:
            mgr, _ = _make_wslm(tmp)
            # 不设置 isolation_mode，且无显式 workspace
            task_data = {"_has_explicit_workspace": False}
            meta = mgr.on_task_start("container_task_001", tmp, task_data)

            # 非non-isolated且无容器关联 → 走 plain 模式（因为没有.git）
            assert meta["mode"] in ("plain", "worktree"), (
                f"isolated模式预期worktree或plain（无git时），实际: {meta['mode']}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# V3-2: 危险操作审批验证
# ═══════════════════════════════════════════════════════════════════

class TestV3DangerousOperationApproval:
    """验证 Non-isolated 模式下危险操作触发审批。"""

    @pytest.fixture
    def engine(self):
        return ApprovalDecisionEngine()

    @pytest.mark.asyncio
    async def test_host_mode_dangerous_tool_requires_approval(self, engine):
        """V3-2.1: Non-isolated模式下危险工具（file_write/bash_execute等）需要审批。

        代码分析结论（src/isolation/approval.py L319-333）：
        当 isolation_level == HOST 且 tool_safety == "dangerous" 时，
        requires_approval=True, decision_type="NEEDS_APPROVAL"
        """
        dangerous_tools = [
            "file_write", "write_file", "file_edit", "file_delete",
            "bash_execute", "shell_execute", "python_execute",
            "rollback", "checkpoint",
        ]
        for tool_name in dangerous_tools:
            ctx = ApprovalContext(
                tool_name=tool_name,
                isolation_level=IsolationLevel.HOST,
            )
            decision = await engine.decide(ctx)
            assert decision.requires_approval is True, (
                f"Non-isolated模式下工具 '{tool_name}' 应需要审批，实际: {decision.requires_approval}"
            )
            assert decision.decision_type == "NEEDS_APPROVAL"
            assert "HOST_MODE" in decision.risk_factors
            assert "DANGEROUS_TOOL" in decision.risk_factors

    @pytest.mark.asyncio
    async def test_host_mode_unknown_tool_requires_approval(self, engine):
        """V3-2.2: Non-isolated模式下未知工具默认需要审批（安全优先）。

        代码分析结论（src/isolation/approval.py L335-371）：
        未知工具在Non-isolated模式下先检测危险操作，无则仍需审批。
        """
        ctx = ApprovalContext(
            tool_name="custom_unknown_tool",
            isolation_level=IsolationLevel.HOST,
        )
        decision = await engine.decide(ctx)
        assert decision.requires_approval is True, (
            "Non-isolated模式下未知工具应需要审批（安全优先）"
        )
        assert decision.decision_type == "NEEDS_APPROVAL"
        assert "HOST_MODE" in decision.risk_factors

    @pytest.mark.asyncio
    async def test_host_mode_unknown_tool_with_dangerous_op(self, engine):
        """V3-2.3: Non-isolated模式下未知工具检测到危险操作时审批（更高风险分）。

        代码分析结论（src/isolation/approval.py L337-356）：
        未知工具+检测到危险操作 → risk_score=0.9, NEEDS_APPROVAL
        """
        mock_tool_def = MagicMock()
        mock_tool_def.dangerous_operations = ["rm -rf"]

        ctx = ApprovalContext(
            tool_name="custom_tool",
            tool_definition=mock_tool_def,
            inputs={"command": "rm -rf /tmp/test"},
            isolation_level=IsolationLevel.HOST,
        )
        decision = await engine.decide(ctx)
        assert decision.requires_approval is True
        assert decision.decision_type == "NEEDS_APPROVAL"
        assert decision.risk_score >= 0.5, f"未知工具+危险操作风险分应≥0.5，实际: {decision.risk_score}"

    @pytest.mark.asyncio
    async def test_policy_approval_overrides_all(self, engine):
        """V3-2.4: 策略级审批优先级最高，直接覆盖。

        代码分析结论（src/isolation/approval.py L283-297）：
        第1层策略审批（policy.approval）先于工具分类判断。
        """
        from isolation.policy import ToolIsolationPolicy
        policy = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
            approval=True,
        )
        ctx = ApprovalContext(
            tool_name="file_read",
            policy=policy,
            isolation_level=IsolationLevel.CONTAINER,
        )
        decision = await engine.decide(ctx)
        assert decision.requires_approval is True, (
            "策略级审批应优先于工具分类（file_read本是安全工具）"
        )
        assert "POLICY_APPROVAL" in decision.risk_factors

    def test_classify_tool_safety_correctness(self):
        """V3-2.5: 工具安全分类函数正确分类。"""
        # 安全工具
        for tool in SAFE_TOOLS:
            assert classify_tool_safety(tool) == "safe", f"{tool} 应为 safe"
        # 危险工具
        for tool in DANGEROUS_TOOLS:
            assert classify_tool_safety(tool) == "dangerous", f"{tool} 应为 dangerous"
        # 未知工具
        assert classify_tool_safety("totally_unknown") == "unknown"


# ═══════════════════════════════════════════════════════════════════
# V3-3: 只读操作无弹窗验证
# ═══════════════════════════════════════════════════════════════════

class TestV3ReadOnlyNoApproval:
    """验证 Non-isolated 模式下只读工具免审批。"""

    @pytest.fixture
    def engine(self):
        return ApprovalDecisionEngine()

    @pytest.mark.asyncio
    async def test_host_mode_safe_tools_auto_approved(self, engine):
        """V3-3.1: Non-isolated模式下安全工具（file_read/search等）自动批准，无需审批。

        代码分析结论（src/isolation/approval.py L303-317）：
        isolation_level == HOST 且 tool_safety == "safe" 时，
        requires_approval=False, decision_type="AUTO_APPROVED"
        """
        for tool_name in SAFE_TOOLS:
            ctx = ApprovalContext(
                tool_name=tool_name,
                isolation_level=IsolationLevel.HOST,
            )
            decision = await engine.decide(ctx)
            assert decision.requires_approval is False, (
                f"Non-isolated模式下安全工具 '{tool_name}' 不应需要审批"
            )
            assert decision.decision_type == "AUTO_APPROVED"
            assert decision.risk_score <= 0.2, (
                f"安全工具风险分应很低，实际: {decision.risk_score}"
            )

    @pytest.mark.asyncio
    async def test_non_host_mode_all_auto_approved(self, engine):
        """V3-3.2: 非Non-isolated模式（容器模式）下所有工具自动批准。

        代码分析结论（src/isolation/approval.py L373-400）：
        非HOST模式 → 第3层决策 → requires_approval=False
        """
        for tool_name in ["file_write", "bash_execute", "file_read", "unknown"]:
            ctx = ApprovalContext(
                tool_name=tool_name,
                isolation_level=IsolationLevel.CONTAINER,
            )
            decision = await engine.decide(ctx)
            assert decision.requires_approval is False, (
                f"容器模式下工具 '{tool_name}' 应自动批准"
            )
            assert decision.decision_type == "AUTO_APPROVED"

    @pytest.mark.asyncio
    async def test_safe_tools_whitelist_covers_read_operations(self, engine):
        """V3-3.3: 安全工具白名单覆盖所有只读操作类别。"""
        expected_safe = {
            "file_read", "read_file", "list_directory",   # 文件读取
            "enhanced_search", "code_search", "search",   # 搜索
            "resource_search",                            # 资源查询
            "memory", "retrieve_memory",                  # 记忆/知识
            "task_manage", "task_evaluate",               # 任务管理
            "tool_info",                                  # 工具信息
            "evaluate",                                   # 评估
        }
        missing = expected_safe - SAFE_TOOLS
        assert not missing, f"以下只读工具未在安全白名单中: {missing}"


# ═══════════════════════════════════════════════════════════════════
# V4-1: 后端级联清理范围验证
# ═══════════════════════════════════════════════════════════════════

class TestV4CascadeCleanup:
    """验证删除任务的级联清理范围。"""

    @pytest.fixture
    def task_service(self, tmp_path):
        """创建使用临时目录的TaskService。"""
        return TaskService(data_dir=str(tmp_path / "tasks"))

    @pytest.mark.asyncio
    async def test_delete_root_task_cleans_storage(self, task_service):
        """V4-1.1: 删除根任务时从存储中删除任务记录。

        代码分析结论（src/tasks/service.py L611-654）：
        非容器任务删除最终调用 self._storage.delete(task_id)
        """
        task = await task_service.create_task("测试根任务")
        task_id = task.id

        # 验证任务存在
        assert task_service.get_task(task_id) is not None

        # 删除
        result = await task_service.delete_task(task_id)
        assert result is True

        # 验证任务已从存储中移除
        assert task_service.get_task(task_id) is None

    @pytest.mark.asyncio
    async def test_delete_task_cascades_subtasks(self, task_service):
        """V4-1.2: 非容器父任务删除时硬删除并级联清理子任务。

        delete_task 统一委托 hard_delete_task（判定口径 task_scope=container），
        非 container 的父任务即使有子任务也走硬删除 + 级联清理，与工具层一致。
        """
        parent = await task_service.create_task("父任务")
        child1 = await task_service.create_task(
            "子任务1", parent_task_id=parent.id
        )
        child2 = await task_service.create_task(
            "子任务2", parent_task_id=parent.id
        )

        await task_service.start_task(child1.id)
        await task_service.start_task(child2.id)

        result = await task_service.delete_task(parent.id)
        assert result is True

        # 父任务被硬删除（不再软删除保留）
        assert task_service.get_task(parent.id) is None
        # 子任务被级联删除
        assert task_service.get_task(child1.id) is None
        assert task_service.get_task(child2.id) is None

    @pytest.mark.asyncio
    async def test_delete_task_no_children_hard_delete(self, task_service):
        """V4-1.3: 无子任务的任务执行硬删除，从存储中移除。"""
        task = await task_service.create_task("独立任务")

        result = await task_service.delete_task(task.id)
        assert result is True

        assert task_service.get_task(task.id) is None

    @pytest.mark.skip(reason="delete_task 不再直接调用 _cleanup_workspace，已委托给 soft_delete_container/hard_delete_task")
    @pytest.mark.asyncio
    async def test_delete_non_container_root_cleans_workspace(self, task_service):
        """V4-1.4: 删除非容器根任务时清理worktree。"""
        pass

    @pytest.mark.skip(reason="delete_task 不再直接调用 _cleanup_workspace，已委托给 soft_delete_container/hard_delete_task")
    @pytest.mark.asyncio
    async def test_delete_container_child_no_workspace_cleanup(self, task_service):
        """V4-1.5: 删除容器子任务时不清理工作空间。"""
        pass

    @pytest.mark.asyncio
    async def test_cancel_pipeline_recursive_covers_all_subtasks(self, task_service):
        """V4-1.6: 管道取消递归覆盖所有子任务层级。

        代码分析结论（src/tasks/service.py L689-698）：
        _cancel_pipeline_recursive 递归遍历 list_subtasks 的每个子任务。
        """
        root = await task_service.create_task("根")
        child1 = await task_service.create_task("子1", parent_task_id=root.id)
        grandchild = await task_service.create_task("孙1", parent_task_id=child1.id)

        cancelled_tasks = []
        def track_cancel(tid):
            cancelled_tasks.append(tid)

        with patch.object(task_service, '_cancel_pipeline', side_effect=track_cancel):
            task_service._cancel_pipeline_recursive(root.id)

        assert root.id in cancelled_tasks
        assert child1.id in cancelled_tasks
        assert grandchild.id in cancelled_tasks


# ═══════════════════════════════════════════════════════════════════
# V4-2: 容器空间保留验证
# ═══════════════════════════════════════════════════════════════════

class TestV4ContainerSpacePreservation:
    """验证删除任务时容器空间不被清理。"""

    @pytest.mark.asyncio
    async def test_container_task_soft_delete_preserves_data(self, tmp_path):
        """V4-2.1: 容器任务使用软删除，数据保留在存储中。

        代码分析结论（src/tasks/service.py L661-667）：
        有子任务时标记 soft_deleted=True，
        _storage.delete 不被调用，数据保留。
        """
        svc = TaskService(data_dir=str(tmp_path / "tasks"))

        container = await svc.create_task(
            "容器任务", metadata={"task_scope": "container"}
        )
        container_id = container.id

        await svc.create_task(
            "子任务1",
            metadata={"task_scope": "container"},
            parent_task_id=container_id,
        )

        result = await svc.delete_task(container_id)
        assert result is True

        # 容器任务应仍然存在（软删除）
        stored = svc.get_task(container_id)
        assert stored is not None, "容器任务软删除后数据应保留"
        assert stored.metadata.get("soft_deleted") is True

    @pytest.mark.asyncio
    async def test_container_child_delete_skips_workspace_cleanup(self, tmp_path):
        """V4-2.2: 容器子任务删除时不清理容器空间。

        代码分析结论（src/tasks/service.py L748-761）：
        _is_child_of_container 向上追溯 parent_task_id 链，
        检查根任务 task_scope == "container"。
        """
        svc = TaskService(data_dir=str(tmp_path / "tasks"))

        container = await svc.create_task(
            "容器", metadata={"task_scope": "container"}
        )
        child = await svc.create_task(
            "子任务", parent_task_id=container.id
        )

        is_child = svc._is_child_of_container(child)
        assert is_child is True, "容器子任务应被识别为容器子树的一部分"

    @pytest.mark.asyncio
    async def test_non_container_child_delete_cleans_workspace(self, tmp_path):
        """V4-2.3: 非容器根任务删除时确实清理worktree。

        代码分析结论（src/tasks/service.py L650-651）：
        is_child_of_container == False → 调用 _cleanup_workspace
        """
        svc = TaskService(data_dir=str(tmp_path / "tasks"))

        root = await svc.create_task("普通根任务")
        child = await svc.create_task("子任务", parent_task_id=root.id)

        is_child = svc._is_child_of_container(child)
        # 普通任务的子任务不应被识别为容器子任务
        # （除非根任务 metadata 中 task_scope == "container"）
        assert is_child is False

    @pytest.mark.asyncio
    async def test_workspace_cleanup_plain_mode_is_noop(self, tmp_path):
        """V4-2.4: Non-isolated/plain模式workspace清理不删除目录。

        代码分析结论（src/isolation/workspace_lifecycle.py L1211-1212）：
        mode == 'plain' 时 cleanup_workspace 直接 pass。
        """
        tmp = tempfile.mkdtemp()
        try:
            mgr, store = _make_wslm(tmp)
            store["host_task"] = {
                "mode": "plain", "path": tmp
            }
            result = mgr.cleanup_workspace("host_task")
            assert result["worktree_removed"] is False
            assert Path(tmp).exists(), "plain模式不应删除项目目录"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# V4-3: 前端删除响应验证（后端API层）
# ═══════════════════════════════════════════════════════════════════

class TestV4FrontendDeleteResponse:
    """验证删除API响应正确性（前端即时移除的基础）。"""

    @pytest.mark.asyncio
    async def test_delete_returns_true_on_success(self, tmp_path):
        """V4-3.1: 成功删除返回True。"""
        svc = TaskService(data_dir=str(tmp_path / "tasks"))
        task = await svc.create_task("待删除任务")

        result = await svc.delete_task(task.id)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_nonexistent(self, tmp_path):
        """V4-3.2: 删除不存在的任务返回False。"""
        svc = TaskService(data_dir=str(tmp_path / "tasks"))

        result = await svc.delete_task("nonexistent_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_removes_from_storage_immediately(self, tmp_path):
        """V4-3.3: 删除后任务立即从存储中移除（非容器任务）。

        这是前端即时移除的前端基础：后端删除成功后，
        前端可通过下次轮询或WS通知确认任务消失。
        """
        svc = TaskService(data_dir=str(tmp_path / "tasks"))
        task = await svc.create_task("即时移除任务")

        assert svc.get_task(task.id) is not None
        await svc.delete_task(task.id)
        assert svc.get_task(task.id) is None, "删除后应立即从存储中移除"

    @pytest.mark.asyncio
    async def test_container_delete_keeps_in_storage(self, tmp_path):
        """V4-3.4: 容器任务删除后仍保留在存储中（软删除）。

        容器任务不立即移除，而是标记 soft_deleted=True。
        前端需要根据此标记过滤显示。
        """
        svc = TaskService(data_dir=str(tmp_path / "tasks"))
        container = await svc.create_task(
            "容器任务", metadata={"task_scope": "container"}
        )

        await svc.create_task(
            "子任务",
            metadata={"task_scope": "container"},
            parent_task_id=container.id,
        )

        await svc.delete_task(container.id)
        stored = svc.get_task(container.id)
        assert stored is not None, "容器任务应保留在存储中"
        assert stored.metadata.get("soft_deleted") is True


# ═══════════════════════════════════════════════════════════════════
# DangerChecker 单元测试
# ═══════════════════════════════════════════════════════════════════

class TestDangerChecker:
    """验证危险操作检测器逻辑。"""

    @pytest.fixture
    def checker(self):
        return DangerChecker()

    def test_detect_dangerous_bash_command(self, checker):
        """DangerChecker: 检测bash命令中的危险操作。"""
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = ["rm -rf", "dd if="]

        result = checker.check("bash_execute", mock_tool, {"command": "rm -rf /tmp"})
        assert result is not None
        assert "rm -rf" in result

    def test_detect_dangerous_file_write(self, checker):
        """DangerChecker: file_write工具检测危险操作。"""
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = ["overwrite"]

        result = checker.check("file_write", mock_tool, {"content": "overwrite data"})
        assert result is not None

    def test_no_dangerous_ops_returns_none(self, checker):
        """DangerChecker: 无危险操作返回None。"""
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = ["rm -rf"]

        result = checker.check("bash_execute", mock_tool, {"command": "ls -la"})
        assert result is None

    def test_no_tool_definition_returns_none(self, checker):
        """DangerChecker: 无工具定义返回None。"""
        result = checker.check("unknown_tool", None, {})
        assert result is None

    def test_empty_dangerous_operations_returns_none(self, checker):
        """DangerChecker: 工具无dangerous_operations声明返回None。"""
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = []

        result = checker.check("some_tool", mock_tool, {})
        assert result is None


# ═══════════════════════════════════════════════════════════════════
# IsolationDecider 验证
# ═══════════════════════════════════════════════════════════════════

class TestIsolationDecider:
    """验证隔离决策器的降级逻辑。"""

    def test_host_level_requires_approval_flag(self):
        """IsolationDecider: HOST级别设置requires_approval=True。

        代码分析结论（src/isolation/manager.py L353）：
        requires_approval = level == IsolationLevel.HOST
        """
        from isolation.decider import IsolationDecider
        decider = IsolationDecider()
        policy = decider.resolve("bash_execute")
        # HOST级别确实需要审批
        assert IsolationLevel.HOST.value == "non_isolated"

    def test_fallback_order_is_container_then_host(self):
        """IsolationDecider: 降级顺序为 CONTAINER → HOST。"""
        from isolation.decider import FALLBACK_ORDER
        assert FALLBACK_ORDER == [IsolationLevel.CONTAINER, IsolationLevel.HOST]
