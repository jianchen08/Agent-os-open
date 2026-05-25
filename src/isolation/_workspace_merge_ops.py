"""工作空间合并操作 Mixin。

提供 WorkspaceLifecycleManager 的合并、验证和清理方法。
从 workspace_lifecycle.py 拆分而来。
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

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
            os.chmod(filepath, stat.S_IWRITE)
            func(filepath)
        else:
            raise

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
            if branch:
                # BUG-FIX: 只在 git_merge 成功时打 tag（commit graph 上有 merge 关系）
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

    # ── 7. 评估失败 ──────────────────────────────────────────────

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

    # ── 8. 任务异常回滚 ──────────────────────────────────────────

    def on_task_failed(self, workspace: str, ws_meta: dict) -> dict:
        """异常回滚：worktree 模式先 checkout+clean 再清理 worktree，其他模式直接回滚"""
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

    # ── 9. 安全合并 ──────────────────────────────────────────────

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：在当前分支上尝试 git merge，失败降级为文件复制。

        BUG-FIX-fix_20260512_worktree_base:
        原逻辑使用 _resolve_main_branch 硬找 main/master 分支做合并目标，
        但 worktree 可能基于 feature 分支创建 → 分支不匹配 → 降级 copy_merge
        → 旧文件无差别覆盖新文件。
        修复: 使用当前实际所在分支作为合并目标，不再硬找 main。
        同时在合并前提交项目根目录的脏文件，防止未提交修改被覆盖。
        """
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        # workspace 中提交所有变更
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(
            ws_path, "chore: auto commit before merge")
        # BUG-FIX: 合并前提交项目根目录已跟踪文件的修改
        # 注意：只提交已跟踪文件的变更，不 add 未跟踪文件，避免污染项目
        self._ensure_git_user(proj_path)
        self._git_add_tracked_and_commit(
            proj_path, "chore: auto-save before merge")
        # 获取项目根目录当前实际所在分支（不再硬找 main/master）
        rc, current_branch, _ = self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=proj_path)
        if rc != 0 or not current_branch.strip():
            logger.warning(
                "[WorkspaceLifecycle] 无法获取当前分支，降级为 copy_merge")
            return self._copy_merge(workspace, project_root, ws_meta)
        # 尝试 git merge（在当前分支上合并 worktree 分支）
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
        """通过文件复制方式合并变更（冲突降级策略），跳过排除目录。

        BUG-FIX-fix_20260512_worktree_base:
        原逻辑无差别复制 worktree 所有文件到目标目录，
        如果 worktree 基于旧版本创建，旧文件会覆盖目标中的新文件。
        修复: 通过 git diff 找出 worktree 分支实际修改的文件列表，
        只复制这些被修改的文件，避免旧版本覆盖新版本。

        BUG-FIX-fix_20260513_merge_verify:
        原逻辑 merged 为空时仍返回 success=True，导致空合并后 worktree 被清理。
        修复: merged 为空时返回失败，让上层重试或报错。
        """
        src, dst = Path(workspace), Path(target_dir)
        if not src.exists():
            return {"success": False, "error": f"源目录不存在: {workspace}",
                    "method": "copy", "merged_files": []}
        skip = self._effective_skip_dirs()
        changed_files: set[str] | None = None
        branch = ws_meta.get("branch", "") if ws_meta else ""
        project_root = ws_meta.get("project_root", "") if ws_meta else ""
        # BUG-FIX-fix_20260521_copy_merge_overwrite:
        # 保存 merge-base 信息供三路合并使用，避免整文件覆盖丢失已有改动
        merge_base = ""
        merge_proj_path: Path | None = None
        if branch and project_root:
            merge_proj_path = Path(project_root)
            rc, base, _ = self._run_git(
                "merge-base", branch, "HEAD", cwd=merge_proj_path)
            if rc == 0 and base.strip():
                merge_base = base.strip()
                rc2, diff_out, _ = self._run_git(
                    "diff", "--name-only", merge_base, branch,
                    cwd=merge_proj_path)
                if rc2 == 0 and diff_out.strip():
                    changed_files = set(diff_out.strip().splitlines())
                    logger.info(
                        "[WorkspaceLifecycle] copy_merge: 检测到 %d 个实际修改的文件",
                        len(changed_files))
        merged: list[str] = []
        conflict_files: list[str] = []
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            if any(p in skip for p in rel.parts):
                continue
            if item.suffix in _SKIP_EXTENSIONS:
                continue
            rel_str = str(rel).replace("\\", "/")
            if changed_files is not None and rel_str not in changed_files:
                continue
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            # BUG-FIX-fix_20260521_copy_merge_overwrite:
            # 目标文件已存在且有三路合并所需信息时，尝试内容级合并。
            # 三路合并冲突时保留冲突标记写入目标文件，不中止合并。
            # 二进制文件等无法合并的情况才整文件覆盖。
            if target_file.exists() and merge_base and merge_proj_path:
                merge_result = self._try_three_way_merge(
                    target_file, item, merge_base, rel_str, merge_proj_path)
                if merge_result == "success":
                    merged.append(str(rel))
                    continue
                if merge_result == "conflict":
                    # 冲突已写入目标文件，记录冲突但继续合并其他文件
                    merged.append(str(rel))
                    conflict_files.append(str(rel))
                    logger.warning(
                        "[WorkspaceLifecycle] copy_merge: 文件冲突已保留标记: %s",
                        rel_str)
                    continue
                # "fail"：二进制文件等无法三路合并，整文件覆盖
                logger.warning(
                    "[WorkspaceLifecycle] copy_merge: 三路合并失败，整文件覆盖: %s",
                    rel_str)
            shutil.copy2(str(item), str(target_file))
            merged.append(str(rel))
        if merged:
            self._ensure_git_user(dst)
            self._run_git("add", "-A", cwd=dst)
            commit_msg = f"merge: copy_merge ({len(merged)} files)"
            if conflict_files:
                commit_msg += f" ({len(conflict_files)} conflicts)"
            self._git_add_commit_if_dirty(dst, commit_msg)
            result = {"success": True, "action": "merged",
                      "method": "copy", "merged_files": merged}
            if conflict_files:
                result["conflict_files"] = conflict_files
            return result
        logger.warning("[WorkspaceLifecycle] copy_merge: 未合并任何文件")
        return {"success": False, "error": "copy_merge 未合并任何文件",
                "method": "copy", "merged_files": []}

    def _try_three_way_merge(
        self,
        target_file: Path,
        source_file: Path,
        merge_base: str,
        rel_str: str,
        proj_path: Path,
    ) -> str:
        """尝试使用 git merge-file 进行三路合并，避免整文件覆盖丢失已有改动。

        BUG-FIX-fix_20260521_copy_merge_overwrite:
        当多个 worktree 先后合并修改了同一文件的不同位置时，后合并的 worktree
        的文件版本基于原始代码（merge-base），不包含先合并的改动。
        使用 git merge-file 做内容级别的三路合并，保留所有改动。

        改进：冲突时不再返回失败，而是将带冲突标记的内容写入目标文件，
        返回 "conflict" 让调用方知道存在冲突但文件已写入。

        Args:
            target_file: 目标文件路径（已存在，包含先前合并的改动）
            source_file: 源文件路径（worktree 中的文件）
            merge_base: merge-base commit hash
            rel_str: 相对文件路径（用于 git show 获取 base 版本）
            proj_path: 项目根目录路径（用于 git 命令的 cwd）

        Returns:
            "success" 表示三路合并成功（无冲突）
            "conflict" 表示存在冲突，带冲突标记的内容已写入目标文件
            "fail" 表示无法合并（二进制文件等），需调用方处理
        """
        # 获取 base 版本的文件内容（merge-base 时的原始版本）
        # 使用二进制模式避免 text=True 导致的 UTF-8 解码损坏二进制/非UTF-8文件内容
        try:
            r = subprocess.run(
                ["git", "show", f"{merge_base}:{rel_str}"],
                cwd=str(proj_path), capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.debug(
                "[WorkspaceLifecycle] 三路合并: git show base 执行失败: %s",
                rel_str)
            return "fail"
        if r.returncode != 0:
            logger.debug(
                "[WorkspaceLifecycle] 三路合并: git show base 失败 "
                "(可能为新增文件): %s", rel_str)
            return "fail"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                base_tmp = tmp / "base"
                current_tmp = tmp / "current"

                # 写入 base 版本（保持原始字节，不经过文本编解码）
                base_tmp.write_bytes(r.stdout)
                # 复制当前目标版本（可能已包含先前 worktree 的改动）
                shutil.copy2(str(target_file), str(current_tmp))

                # 执行三路合并：git merge-file <current> <base> <other>
                # 成功返回 0，冲突返回正数（冲突数），错误返回负数（二进制等）
                rc_merge, _, stderr = self._run_git(
                    "merge-file", str(current_tmp), str(base_tmp),
                    str(source_file), cwd=proj_path)

                if rc_merge == 0:
                    # 合并成功，将合并结果覆盖目标文件
                    shutil.copy2(str(current_tmp), str(target_file))
                    logger.info(
                        "[WorkspaceLifecycle] 三路合并成功: %s", rel_str)
                    return "success"

                if rc_merge > 0:
                    # 冲突：将带冲突标记的内容写入目标文件，继续合并其他文件
                    shutil.copy2(str(current_tmp), str(target_file))
                    logger.warning(
                        "[WorkspaceLifecycle] 三路合并冲突 "
                        "(%d conflicts)，已保留冲突标记到目标文件: %s",
                        rc_merge, rel_str)
                    return "conflict"

                # rc_merge < 0：二进制文件等无法合并的情况
                logger.warning(
                    "[WorkspaceLifecycle] 三路合并失败 (rc=%d)，"
                    "可能是二进制文件: %s, stderr=%s",
                    rc_merge, rel_str,
                    stderr[:200] if stderr else "")
                return "fail"
        except Exception as exc:
            logger.warning(
                "[WorkspaceLifecycle] 三路合并异常: "
                "%s, error=%s", rel_str, exc)
            return "fail"

    # ── 10. 合并验证 ─────────────────────────────────────────────

    def _verify_merge_result(
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

    # ── 12. 工作空间清理 ──────────────────────────────────────────

    def cleanup_workspace(self, task_id: str) -> dict[str, Any]:
        """清理单个任务关联的工作空间（worktree/分支/目录），不递归子任务"""
        self.restore_ws_meta(task_id)
        meta = self._ws_meta_store.get(task_id)
        if not meta:
            return {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        mode = meta.get("mode", "")
        workspace = meta.get("path", "")
        result: dict[str, Any] = {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        if mode == "worktree":
            project_root = Path(meta.get("project_root", "")).resolve()
            branch = meta.get("branch", "")
            ws_path = Path(workspace).resolve()
            if project_root.exists():
                if ws_path.exists():
                    rc, _, _ = self._run_git("worktree", "remove", str(ws_path), "--force", cwd=project_root)
                    result["worktree_removed"] = rc == 0
                if branch:
                    rc, _, _ = self._run_git("branch", "-D", branch, cwd=project_root)
                    result["branch_deleted"] = rc == 0
            if ws_path.exists() and "__wt_" in ws_path.name:
                try:
                    _force_rmtree(str(ws_path))
                    result["dir_removed"] = True
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] cleanup_workspace rmtree 失败: %s, %s", workspace, e)
        elif mode == "plain":
            ws_path = Path(workspace)
            if not ws_path.is_absolute():
                ws_path = ws_path.resolve()
            if ws_path.exists():
                try:
                    _force_rmtree(str(ws_path))
                    result["dir_removed"] = True
                    logger.info("[WorkspaceLifecycle] 已清理 plain 工作空间: %s", ws_path)
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] cleanup_workspace plain rmtree 失败: %s, %s", workspace, e)

        self._ws_meta_store.pop(task_id, None)
        logger.info("[WorkspaceLifecycle] cleanup_workspace: task_id=%s, mode=%s, result=%s", task_id, mode, result)
        return result
