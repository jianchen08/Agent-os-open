"""工作空间合并操作 Mixin。

提供 WorkspaceLifecycleManager 的合并、验证和清理方法。
从 workspace_lifecycle.py 拆分而来。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _MergeOpsMixin:
    """合并操作 Mixin，提供安全合并、copy_merge、验证和清理方法。

    要求宿主类提供以下方法（来自 _GitOpsMixin）：
    - self._run_git(...)
    - self._ensure_git_user(...)
    - self._git_add_commit_if_dirty(...)
    - self._get_merge_lock(...)
    - self._effective_skip_dirs(...)
    - self._guard_root_branch(...)
    - self._worktree_add_with_repair(...)
    - self._setup_sparse_worktree(...)
    - self._calc_project_size(...)
    """

    # ── 评估前保存 ────────────────────────────────────────────────

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

    # ── 评估通过 ──────────────────────────────────────────────

    def on_eval_passed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估通过后按 mode 分发合并逻辑，并发安全：按 project_root 粒度加锁"""
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.info("[WorkspaceLifecycle] plain 模式，跳过合并: task_id=%s", task_id)
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
                        logger.info(
                            "[WorkspaceLifecycle] 合并验证通过 (attempt %d): task_id=%s, method=%s",
                            attempt, task_id, result.get("method"))
                        self._cleanup_worktree(
                            workspace, ws_meta, tag_task_id=task_id,
                            merge_method=result.get("method", ""))
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
                return result
            if mode == "shared":
                logger.info("[WorkspaceLifecycle] shared 模式，跳过合并: task_id=%s", task_id)
                return {"success": True, "action": "none"}
            logger.warning("[WorkspaceLifecycle] 未知 mode: %s, task_id=%s", mode, task_id)
            return {"success": False, "error": f"未知工作模式: {mode}"}

    def _cleanup_worktree(
        self, workspace: str, ws_meta: dict, *, tag_task_id: str = "",
        merge_method: str = "",
    ):
        """清理 worktree：删 worktree → 条件打 tag → 删分支"""
        from isolation._workspace_git_ops import _force_rmtree

        project_root = Path(ws_meta.get("project_root", ""))
        branch = ws_meta.get("branch", "")
        if project_root.exists():
            try:
                self._run_git("worktree", "remove", str(workspace), "--force", cwd=project_root)
            except Exception as e:
                logger.warning("[WorkspaceLifecycle] git worktree remove 失败: %s, %s", workspace, e)
            if branch:
                if tag_task_id and merge_method == "git_merge":
                    tag = f"task-merge/{tag_task_id[:8]}"
                    self._run_git("tag", tag, branch, cwd=project_root)
                    logger.info("[WorkspaceLifecycle] 已打 tag: %s，可 git revert 回退", tag)
                self._run_git("branch", "-D", branch, cwd=project_root)
        ws_path = Path(workspace).resolve()
        if ws_path.exists() and "__wt_" in ws_path.name:
            try:
                _force_rmtree(str(ws_path))
                logger.info("[WorkspaceLifecycle] 强制清理残留 worktree 目录: %s", workspace)
            except OSError as e:
                logger.warning("[WorkspaceLifecycle] 强制清理 worktree 目录失败: %s, %s", workspace, e)

    # ── 评估失败 ──────────────────────────────────────────────

    def on_eval_failed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估失败：reject_count >= max_retries 时回滚，否则允许重试"""
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.info("[WorkspaceLifecycle] plain 模式评估失败: task_id=%s", task_id)
            return {"success": True, "action": "none"}
        if mode == "shared":
            logger.info("[WorkspaceLifecycle] shared 模式评估失败: task_id=%s", task_id)
            return {"success": True, "action": "none"}
        reject_count = ws_meta.get("reject_count", 0) + 1
        max_retries = ws_meta.get("max_retries", self._config.get("max_retries", 3))
        ws_meta["reject_count"] = reject_count
        self._ws_meta_store[task_id] = ws_meta
        if reject_count >= max_retries:
            logger.info("[WorkspaceLifecycle] 评估失败超限，回滚: task_id=%s, count=%d", task_id, reject_count)
            return self.on_task_failed(workspace, ws_meta)
        logger.info("[WorkspaceLifecycle] 评估失败，重试: task_id=%s, count=%d/%d", task_id, reject_count, max_retries)
        return {"success": True, "action": "retry", "reject_count": reject_count}

    # ── 任务异常回滚 ──────────────────────────────────────────

    def on_task_failed(self, workspace: str, ws_meta: dict) -> dict:
        """异常回滚：worktree 模式先 checkout+clean 再清理 worktree，其他模式直接回滚"""
        from isolation._workspace_git_ops import _force_rmtree

        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        mode = ws_meta.get("mode", "")
        if mode == "plain":
            logger.info("[WorkspaceLifecycle] plain 模式，跳过回滚: %s", workspace)
            return {"success": True, "action": "none"}
        if mode == "shared":
            logger.info("[WorkspaceLifecycle] shared 模式，跳过回滚: %s", workspace)
            return {"success": True, "action": "none"}
        if mode == "worktree":
            self._run_git("checkout", "--", ".", cwd=ws_path)
            self._run_git("clean", "-fd", cwd=ws_path)
            if not ws_path.exists():
                logger.warning(
                    "[WorkspaceLifecycle] worktree 目录在回滚过程中已不存在，跳过清理: %s",
                    workspace)
                return {"success": True, "action": "rollback_worktree"}
            self._cleanup_worktree(workspace, ws_meta)
            logger.info("[WorkspaceLifecycle] worktree 回滚并清理: %s", workspace)
            return {"success": True, "action": "rollback_worktree"}
        logger.warning("[WorkspaceLifecycle] 未知 mode '%s'，拒绝执行破坏性操作: %s", mode, workspace)
        return {"success": False, "error": f"未知工作模式: {mode}"}

    # ── 安全合并 ──────────────────────────────────────────────

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：在当前分支上尝试 git merge，失败降级为文件复制。"""
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(
            ws_path, "chore: auto commit before merge")
        self._ensure_git_user(proj_path)
        self._git_add_commit_if_dirty(
            proj_path, "chore: auto-save before merge")
        rc, current_branch, _ = self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=proj_path)
        if rc != 0 or not current_branch.strip():
            logger.warning(
                "[WorkspaceLifecycle] 无法获取当前分支，降级为 copy_merge")
            return self._copy_merge(workspace, project_root, ws_meta)
        if branch:
            rc, _, stderr = self._run_git(
                "merge", branch, cwd=proj_path)
            if rc == 0:
                return {"success": True, "action": "merged",
                        "method": "git_merge"}
            logger.warning(
                "[WorkspaceLifecycle] git merge 失败，"
                "降级为 copy_merge: branch=%s, error=%s",
                branch, stderr[:200] if stderr else "(empty)")
            self._run_git("merge", "--abort", cwd=proj_path)
            return self._copy_merge(workspace, project_root, ws_meta)
        return self._copy_merge(workspace, project_root, ws_meta)

    def _copy_merge(self, workspace: str, target_dir: str,
                    ws_meta: dict | None = None) -> dict:
        """通过文件复制方式合并变更（冲突降级策略），跳过排除目录。"""
        src, dst = Path(workspace), Path(target_dir)
        if not src.exists():
            return {"success": False, "error": f"源目录不存在: {workspace}",
                    "method": "copy", "merged_files": []}
        skip = self._effective_skip_dirs()
        changed_files: set[str] | None = None
        branch = ws_meta.get("branch", "") if ws_meta else ""
        project_root = ws_meta.get("project_root", "") if ws_meta else ""
        if branch and project_root:
            proj_path = Path(project_root)
            rc, base, _ = self._run_git(
                "merge-base", branch, "HEAD", cwd=proj_path)
            if rc == 0 and base.strip():
                rc2, diff_out, _ = self._run_git(
                    "diff", "--name-only", base.strip(), branch,
                    cwd=proj_path)
                if rc2 == 0 and diff_out.strip():
                    changed_files = set(diff_out.strip().splitlines())
                    logger.info(
                        "[WorkspaceLifecycle] copy_merge: 检测到 %d 个实际修改的文件",
                        len(changed_files))
        merged: list[str] = []
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            if any(p in skip for p in rel.parts):
                continue
            if item.suffix in set({".bak", ".pyc", ".pyo"}):
                continue
            rel_str = str(rel).replace("\\", "/")
            if changed_files is not None and rel_str not in changed_files:
                continue
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target_file))
            merged.append(str(rel))
        if merged:
            self._ensure_git_user(dst)
            self._run_git("add", "-A", cwd=dst)
            self._git_add_commit_if_dirty(dst, f"merge: copy_merge ({len(merged)} files)")
            return {"success": True, "action": "merged", "method": "copy", "merged_files": merged}
        logger.warning("[WorkspaceLifecycle] copy_merge: 未合并任何文件")
        return {"success": False, "error": "copy_merge 未合并任何文件",
                "method": "copy", "merged_files": []}

    # ── 合并验证 ──────────────────────────────────────────────

    def _verify_merge_result(
        self, workspace: str, project_root: str, ws_meta: dict, merge_result: dict,
    ) -> tuple[bool, str]:
        """统一验证合并是否成功：不论 git_merge 还是 copy_merge 都验证文件到达。"""
        branch = ws_meta.get("branch", "")
        method = merge_result.get("method", "")
        proj_path = Path(project_root)

        if not proj_path.exists():
            return False, f"project_root 不存在: {project_root}"

        if method == "git_merge" and branch:
            if not self._verify_merge_in_main(branch, cwd=proj_path):
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
                "diff", "--name-only", branch + "~1", branch, cwd=proj_path)
            if rc == 0 and diff_out.strip():
                branch_files = set(diff_out.strip().splitlines())
                missing = []
                for f in branch_files:
                    if not (proj_path / f).exists():
                        missing.append(f)
                    if len(missing) >= 10:
                        break
                if missing:
                    return False, f"git_merge 文件验证失败: {len(missing)} 个文件未到达目标"

        return True, "验证通过"

    def _verify_merge_in_main(self, branch_name: str, cwd: Path | None = None) -> bool:
        """验证分支已合并到当前分支。"""
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
