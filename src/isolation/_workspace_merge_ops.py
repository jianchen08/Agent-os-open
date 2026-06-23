"""工作空间合并操作 Mixin。

提供 WorkspaceLifecycleManager 的合并、验证和清理方法。
从 workspace_lifecycle.py 拆分而来。
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# 排除的目录（不参与场景检测和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache"})
_SKIP_EXTENSIONS = frozenset({".bak", ".pyc", ".pyo"})


def _force_rmtree(path: str) -> None:
    """强制删除目录树，兼容 Windows 下 .git 只读文件。

    Windows 上 git objects 文件为只读属性，shutil.rmtree 默认无法删除。
    通过 onerror 回调去除只读属性后重试。
    """
    def _on_error(func, filepath, exc_info):
        if os.name == "nt":
            os.chmod(filepath, stat.S_IWRITE)  # noqa: PTH101
            func(filepath)
        else:
            raise  # noqa: PLE0704

    try:
        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        shutil.rmtree(path, onerror=_on_error)


class _MergeOpsMixin:
    """合并/评估操作 Mixin：封装评估前保存、合并、验证、清理等方法。

    方法通过 self 访问实例属性和 _GitOpsMixin 提供的 Git 方法，
    这些由 WorkspaceLifecycleManager.__init__ 和 _GitOpsMixin 提供。
    """

    # ── 5. 评估前保存 ────────────────────────────────────────────

    def on_before_evaluate(self, workspace: str, ws_meta: dict | None = None) -> dict:
        """评估前保存：git add -A + git commit（plain 模式跳过 git 操作）"""
        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        mode = (ws_meta or {}).get("mode", "")
        if mode == "plain":
            return {"success": True, "commit_hash": None, "has_changes": True}
        if mode == "shared":
            return {"success": True, "commit_hash": None, "has_changes": True}
        self._ensure_git_user(ws_path)
        commit_hash = self._git_add_commit_if_dirty(ws_path, "checkpoint: before evaluate")
        rc, status, _ = self._run_git("status", "--porcelain", cwd=ws_path)
        return {"success": True, "commit_hash": commit_hash,
                "has_changes": bool(status and status.strip())}

    # ── 6. 评估通过 ──────────────────────────────────────────────

    def on_eval_passed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估通过后按 mode 分发合并逻辑，并发安全：按 project_root 粒度加锁

        BUG-FIX-fix_20260513_merge_verify:
        原逻辑仅在 git_merge 时验证合并结果，copy_merge 无验证直接清理 worktree，
        导致合并失败（文件未到达目标）时 worktree 被删除，任务文件丢失。
        修复: 不论哪种合并方式都验证文件是否到达目标，失败则重试最多2次，
        仍失败则保留 worktree 不清理，返回失败让任务标记为 failed。
        """
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.debug("[WorkspaceLifecycle] plain 模式，跳过合并: task_id=%s", task_id)
            return {"success": True, "action": "none"}
        project_root = ws_meta.get("project_root", "")
        lock = self._get_merge_lock(project_root)
        with lock:
            if mode == "worktree":
                max_retries = 2
                for attempt in range(1, max_retries + 1):
                    result = self._safe_merge(workspace, ws_meta)
                    if not result.get("success"):
                        logger.warning(
                            "[WorkspaceLifecycle] 合并失败 (attempt %d/%d)，跳过清理以保留文件: "
                            "task_id=%s, workspace=%s, error=%s",
                            attempt, max_retries, task_id, workspace, result.get("error", "unknown"))
                        if attempt < max_retries:
                            continue
                        return result
                    verified, verify_detail = self._verify_merge_result(
                        workspace, project_root, ws_meta, result)
                    if verified:
                        logger.debug(
                            "[WorkspaceLifecycle] 合并验证通过 (attempt %d): task_id=%s, method=%s",
                            attempt, task_id, result.get("method"))
                        self._cleanup_worktree(
                            workspace, ws_meta, tag_task_id=task_id,
                            merge_method=result.get("method", ""))
                        # P1: 合并成功后清理 project_root 中的 unstaged 残留
                        self._cleanup_unstaged_changes(project_root)
                        return result
                    logger.warning(
                        "[WorkspaceLifecycle] 合并验证失败 (attempt %d/%d): "
                        "task_id=%s, detail=%s",
                        attempt, max_retries, task_id, verify_detail)
                    if attempt < max_retries:
                        continue
                logger.error(
                    "[WorkspaceLifecycle] 合并重试耗尽，保留 worktree 不清理: "
                    "task_id=%s, workspace=%s", task_id, workspace)
                result["verify_error"] = verify_detail
                if "error" not in result:
                    result["error"] = f"合并验证失败(重试{max_retries}次): {verify_detail}"
                result["success"] = False
                return result
            if mode == "shared":
                logger.debug("[WorkspaceLifecycle] shared 模式，跳过合并: task_id=%s", task_id)
                return {"success": True, "action": "none"}
            logger.warning("[WorkspaceLifecycle] 未知 mode: %s, task_id=%s", mode, task_id)
            return {"success": False, "error": f"未知工作模式: {mode}"}

    def merge_worktree_before_complete(self, task_id: str) -> str | None:
        """任务标记 completed 前的合并门控（统一入口）。

        供 task_evaluate 工具与 _rerun_evaluation 恢复路径共同复用，
        确保所有"评估通过 → completed"的路径都经过合并检查。

        ws_meta 解析顺序：
        1. task.metadata["ws_meta"]（主路径，内存对象引用）
        2. restore_ws_meta + self._ws_meta_store 兜底（覆盖异步持久化延迟）

        Args:
            task_id: 任务 ID

        Returns:
            None = 合并成功或无需合并（plain/shared 模式）；
            str  = 合并失败原因（worktree 模式下读不到 ws_meta 或合并/验证失败），
                   调用方应据此将任务标记 failed。
        """
        ws_meta = None
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.metadata:
                ws_meta = task.metadata.get("ws_meta")
        except Exception as exc:
            logger.warning(
                "[WorkspaceLifecycle] merge_worktree_before_complete 读取 task 失败: "
                "task_id=%s, error=%s", task_id, exc,
            )
        if not ws_meta or not isinstance(ws_meta, dict):
            # 兜底: 异步持久化延迟可能让 task.metadata 暂时缺失，从 store 恢复
            self.restore_ws_meta(task_id)
            ws_meta = self._ws_meta_store.get(task_id)

        if not ws_meta or not isinstance(ws_meta, dict):
            # 无法确定 ws_meta，但任务确实跑过隔离逻辑 → 视为致命错误而非静默跳过。
            # 静默返回 None 会让 worktree 产出永远丢失（既往故障根因）。
            return f"无法获取任务 {task_id} 的 ws_meta，worktree 合并被跳过"

        mode = ws_meta.get("mode", "")
        if mode != "worktree":
            # plain / shared / project_root 等无需合并的模式
            return None

        workspace = ws_meta.get("path", "")
        if not workspace:
            return f"worktree 模式但 ws_meta.path 为空，task_id={task_id}"

        result = self.on_eval_passed(task_id, workspace, ws_meta)
        if result.get("success"):
            conflict_files = result.get("conflict_files", [])
            if conflict_files:
                logger.warning(
                    "[WorkspaceLifecycle] worktree 合并完成但有冲突文件: "
                    "task_id=%s, conflicts=%s", task_id, conflict_files,
                )
            return None

        error_parts = [result.get("error", "unknown")]
        if result.get("verify_error"):
            error_parts.append(f"验证详情: {result['verify_error']}")
        return ", ".join(error_parts)

    def _cleanup_worktree(
        self, workspace: str, ws_meta: dict, *, tag_task_id: str = "",
        merge_method: str = "",
    ):
        """清理 worktree：删 worktree → 条件打 tag → 删分支

        BUG-FIX-fix_20260512_worktree_base:
        原逻辑无论合并方式（git_merge 或 copy_merge）都打 tag。
        copy_merge 时 commit graph 上没有真正的 merge 关系，tag 指向的提交
        在分支删除后变成孤儿提交。修复: 只在 git_merge 成功时打 tag。
        """
        project_root = Path(ws_meta.get("project_root", ""))
        branch = ws_meta.get("branch", "")
        if project_root.exists():
            try:
                self._run_git("worktree", "remove", str(workspace), "--force", cwd=project_root)
            except Exception as e:
                logger.warning("[WorkspaceLifecycle] git worktree remove 失败: %s, %s", workspace, e)
                self._run_git("worktree", "prune", cwd=project_root)
            if branch:
                if tag_task_id and merge_method == "git_merge":
                    tag = f"task-merge/{tag_task_id[:8]}"
                    self._run_git("tag", tag, branch, cwd=project_root)
                    logger.debug("[WorkspaceLifecycle] 已打 tag: %s，可 git revert 回退", tag)
                self._run_git("worktree", "prune", cwd=project_root)
                self._run_git("branch", "-D", branch, cwd=project_root)
        ws_path = Path(workspace).resolve()
        if ws_path.exists() and "__wt_" in ws_path.name:
            try:
                _force_rmtree(str(ws_path))
                logger.debug("[WorkspaceLifecycle] 强制清理残留 worktree 目录: %s", workspace)
            except OSError as e:
                logger.warning("[WorkspaceLifecycle] 强制清理 worktree 目录失败: %s, %s", workspace, e)

    # ── 7. 评估失败 ──────────────────────────────────────────────

    def on_eval_failed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估失败：reject_count >= max_retries 时回滚，否则允许重试"""
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.debug("[WorkspaceLifecycle] plain 模式评估失败: task_id=%s", task_id)
            return {"success": True, "action": "none"}
        if mode == "shared":
            logger.debug("[WorkspaceLifecycle] shared 模式评估失败: task_id=%s", task_id)
            return {"success": True, "action": "none"}
        reject_count = ws_meta.get("reject_count", 0) + 1
        max_retries = ws_meta.get("max_retries", self._config.get("max_retries", 3))
        ws_meta["reject_count"] = reject_count
        self._ws_meta_store[task_id] = ws_meta
        if reject_count >= max_retries:
            logger.debug("[WorkspaceLifecycle] 评估失败超限，回滚: task_id=%s, count=%d", task_id, reject_count)
            return self.on_task_failed(workspace, ws_meta)
        logger.debug("[WorkspaceLifecycle] 评估失败，重试: task_id=%s, count=%d/%d", task_id, reject_count, max_retries)
        return {"success": True, "action": "retry", "reject_count": reject_count}

    # ── 8. 任务异常回滚 ──────────────────────────────────────────

    def on_task_failed(self, workspace: str, ws_meta: dict) -> dict:
        """任务异常/失败：不清理 worktree。

        修复: 任务失败不等于终态，如果还能重试，worktree 应该保留供重试使用。
        清理 worktree 的职责交给容器完成或手动调用。
        """
        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.debug("[WorkspaceLifecycle] plain 模式，跳过: %s", workspace)
            return {"success": True, "action": "none"}
        if mode == "shared":
            logger.debug("[WorkspaceLifecycle] shared 模式，跳过: %s", workspace)
            return {"success": True, "action": "none"}
        if mode == "worktree":
            logger.debug("[WorkspaceLifecycle] worktree 失败保留（不清理）: %s", workspace)
            return {"success": True, "action": "none"}
        logger.warning("[WorkspaceLifecycle] 未知 mode '%s'，跳过: %s", mode, workspace)
        return {"success": True, "action": "none"}

    # ── 9. 安全合并 ──────────────────────────────────────────────

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：通过 git merge 将 worktree 分支合并到项目根目录。

        BUG-FIX-fix_20260512_worktree_base:
        使用当前实际所在分支作为合并目标，不再硬找 main/master。

        BUG-FIX-fix_20260529_remove_copy_merge:
        移除 copy_merge 降级策略。worktree 分支从当前 HEAD 创建，
        git merge 应为 fast-forward 或干净合并。如果失败说明存在真正冲突，
        正确做法是保留 worktree 并报告失败，而非绕过 git 做文件复制。
        """
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        if not branch:
            return {"success": False,
                    "error": "缺少 branch 信息，ws_meta 不完整"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(
            ws_path, "chore: auto commit before merge")
        self._ensure_git_user(proj_path)
        self._git_add_tracked_and_commit(
            proj_path, "chore: auto-save before merge")
        rc, current_branch, _ = self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=proj_path)
        if rc != 0 or not current_branch.strip():
            return {"success": False,
                    "error": f"无法获取当前分支: rc={rc}, "
                             f"output={current_branch!r}"}
        rc_pre, pre_merge_head, _ = self._run_git(
            "rev-parse", "HEAD", cwd=proj_path)
        rc, _, stderr = self._run_git(
            "merge", branch, cwd=proj_path)
        if rc == 0:
            result = {"success": True, "action": "merged",
                       "method": "git_merge"}
            if rc_pre == 0 and pre_merge_head.strip():
                result["pre_merge_head"] = pre_merge_head.strip()
            return result
        self._run_git("merge", "--abort", cwd=proj_path)
        return {"success": False,
                "error": f"git merge 失败(branch={branch}): "
                         f"{stderr[:300] if stderr else 'unknown'}"}


    # def _copy_merge(self, workspace: str, target_dir: str,
    #                 ws_meta: dict | None = None) -> dict:
    #     ... (已注释，完整实现见 git history)

    # def _try_three_way_merge(...):
    #     ... (已注释，完整实现见 git history)

    # ── 10. 合并验证 ─────────────────────────────────────────────

    def _verify_merge_result(  # noqa: PLR0912
        self, workspace: str, project_root: str, ws_meta: dict, merge_result: dict,
    ) -> tuple[bool, str]:
        """统一验证合并是否成功：不论 git_merge 还是 copy_merge 都验证文件到达。

        BUG-FIX-fix_20260513_merge_verify:
        原逻辑仅 git_merge 时验证 commit graph，copy_merge 无验证。
        修复: 统一验证逻辑，包含 commit graph 验证 + 文件级验证。

        Returns:
            (verified: bool, detail: str) 验证结果和详情描述
        """
        branch = ws_meta.get("branch", "")
        method = merge_result.get("method", "")
        proj_path = Path(project_root)

        if not proj_path.exists():
            return False, f"project_root 不存在: {project_root}"

        if method == "git_merge" and branch and not self._verify_merge_in_main(branch, cwd=proj_path):
            return False, f"git_merge commit graph 验证失败: branch={branch}"

        merged_files = merge_result.get("merged_files", [])
        if method == "copy" and merged_files:
            missing = []
            for rel_str in merged_files:
                target_file = proj_path / rel_str
                if not target_file.exists():
                    missing.append(rel_str)
                if len(missing) >= 10:
                    break
            if missing:
                return False, f"copy_merge 文件验证失败: {len(missing)} 个文件未到达目标，前几个: {missing[:5]}"

        if method == "git_merge" and branch:
            rc, diff_out, _ = self._run_git(
                "-c", "core.quotepath=false",
                "diff", "--name-only", branch + "~1", branch, cwd=proj_path)
            if rc == 0 and diff_out.strip():
                branch_files = set(diff_out.strip().splitlines())
                missing = []
                for f in branch_files:
                    f_stripped = f.strip().strip('"')
                    target = proj_path / f_stripped
                    if not target.exists():
                        # 模糊匹配：在同名目录下搜索文件名包含目标名的文件
                        # 处理 git 输出编码不一致导致路径不完全匹配的情况
                        parent = target.parent
                        target_name = target.name
                        found = False
                        if parent.exists() and target_name:
                            try:
                                for existing in parent.iterdir():
                                    if existing.name == target_name:
                                        found = True
                                        break
                            except OSError:
                                pass
                        if not found:
                            missing.append(f_stripped)
                    if len(missing) >= 10:
                        break
                if missing:
                    return False, f"git_merge 文件验证失败: {len(missing)} 个文件未到达目标"

        return True, "验证通过"

    def _verify_merge_in_main(self, branch_name: str, cwd: Path | None = None) -> bool:
        """验证分支已合并到当前分支：git log HEAD..{branch} 应为空，不为空则阻止后续清理。

        BUG-FIX-fix_20260512_worktree_base:
        原逻辑使用 _resolve_main_branch 硬找 main/master 做验证，
        但 worktree 可能合并到 feature 分支，导致验证误判。
        修复: 使用当前 HEAD 所在分支做验证。
        """
        work_dir = cwd or self._base_path
        rc, log_output, _ = self._run_git("log", f"HEAD..{branch_name}", cwd=work_dir)
        if rc != 0:
            logger.warning("[WorkspaceLifecycle] 验证合并状态失败: branch=%s", branch_name)
            return False
        if log_output.strip():
            logger.warning("[WorkspaceLifecycle] 分支未完全合并: branch=%s, 未合并=%d",
                           branch_name, len(log_output.splitlines()))
            return False
        return True


    def _cleanup_unstaged_changes(self, project_root: str) -> None:
        """清理 project_root 中的 unstaged 变更。

        合并后可能残留 unstaged 的修改（如 merge 产生的备份文件），
        通过 git checkout 恢复到干净状态。
        """
        proj_path = Path(project_root)
        if not proj_path.exists():
            return

        rc, status, _ = self._run_git("status", "--porcelain", cwd=proj_path)
        if rc != 0 or not status.strip():
            return

        unstaged_lines = [
            line for line in status.splitlines()
            if len(line) >= 2 and line[1] in ("M", "D")
        ]
        if not unstaged_lines:
            return

        logger.debug(
            "[WorkspaceLifecycle] 清理 %d 个 unstaged 变更: project_root=%s",
            len(unstaged_lines), project_root,
        )
        # 恢复 unstaged 的修改到 HEAD 状态
        self._run_git("checkout", "--", ".", cwd=proj_path)

