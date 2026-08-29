"""worktree 合并 git 机制——shared 根共享模块（0.1 机制原样 + 0.2 数据源适配）。

分层契约：本模块是**纯 git 机制**（merge/verify、冲突检测即失败、每命令超时
30s），不碰任务状态、不做裁决——何时合并、结果如何改 task.status 由调用方
（task_evaluate 合并门控）决策。工作空间域（workspace_lifecycle）清理收尾
后续复用同一机制，单一归属零复制。

机制逻辑与 0.1（plugins/shared/system/isolation/_workspace_merge_ops.py 合并
部分 + _workspace_git_ops.py git 操作）保持一致，随任务域迁入 0.2 进程内执行
——不经内核 capability、不依赖跨进程服务。

数据源适配（0.2）：ws_meta 由调用方从管道 state 聚合行解析后传入——0.2 任务
=管道，ws_meta 落 state 单一真值（0.1 经 task_tree/task.metadata 读取，该通路
随进程隔离失效）。

进程内单例：模块级 merge_worktree_before_complete 持有共享实例，_get_merge_lock
按 project_root 粒度串行化并发合并（0.1 manager 进程单例语义）。

铁律：全部 git 命令为同步 subprocess（_GIT_TIMEOUT=30s/命令，合并成功时
add/commit/merge/verify 叠加可阻塞数分钟）——调用方必须丢线程池
（asyncio.to_thread）执行，不得在事件循环内直调。
冲突不做自动解决：冲突 = 合并失败（_safe_merge 抓取冲突清单后 merge --abort，
失败原因原样返回调用方裁决）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30  # git 命令执行超时（秒）


def _force_rmtree(path: str) -> None:
    """强制删除目录树，兼容 Windows 下 .git 只读文件。

    Windows 上 git objects 文件为只读属性，shutil.rmtree 默认无法删除。
    通过 onerror 回调去除只读属性后重试。
    """

    def _on_error(func, filepath, exc_info):
        if os.name == "nt":
            os.chmod(filepath, stat.S_IWRITE)  # noqa: PTH101  # pragma: no cover —— Windows 只读文件修复路径（Linux 不触 Handler）
            func(filepath)
        else:
            raise  # noqa: PLE0704  # pragma: no cover —— 非 Windows 平台分支

    try:
        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        shutil.rmtree(path, onerror=_on_error)


class WorktreeMerger:
    """worktree 合并执行体：合并/验证/清理与冲突判定（0.1 判定原样）。"""

    def __init__(self) -> None:
        # 按 project_root 粒度的并发锁（同仓库任务合并串行化）
        self._merge_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    # ── git 命令封装（自 _workspace_git_ops.py 原样移植）─────────

    def _run_git(self, *args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
        """执行 git 命令（同步，使用 subprocess）"""
        cmd = ["git"] + list(args)
        try:
            r = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if r.returncode != 0:
                err_parts = []
                if r.stderr.strip():
                    err_parts.append(f"stderr={r.stderr[:200]}")
                if r.stdout.strip():
                    err_parts.append(f"stdout={r.stdout[:200]}")
                detail = " | ".join(err_parts) if err_parts else "(no output)"
                logger.warning("[WorktreeMerge] git %s failed (rc=%d): %s", " ".join(args), r.returncode, detail)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"命令执行超时（{timeout}秒）"
        except FileNotFoundError:
            return -1, "", "未找到 git 命令"
        except OSError as e:
            # Windows 上 cwd 不存在时 subprocess.run 抛 NotADirectoryError [WinError 267]。
            # 这里返回错误码而非抛异常，让上层按 rc!=0 走合并失败分支
            # （complete_evaluation(passed=False)），避免合并门控一路崩溃。
            return -1, "", f"git 工作目录无效或不存在: {cwd} ({e})"

    def _get_merge_lock(self, project_root: str) -> threading.Lock:
        """获取指定 project_root 的合并锁，确保同一项目目录的合并操作串行执行"""
        with self._global_lock:
            if project_root not in self._merge_locks:
                self._merge_locks[project_root] = threading.Lock()
            return self._merge_locks[project_root]

    def _ensure_git_user(self, cwd: Path):
        """配置 workspace 的 git 用户信息"""
        self._run_git("config", "user.email", "agent@agent-os.local", cwd=cwd)
        self._run_git("config", "user.name", "Agent OS", cwd=cwd)

    def _remove_index_lock(self, cwd: Path) -> bool:
        """Remove stale git index.lock if it exists. Returns True if a lock was removed."""
        lock_path = cwd / ".git" / "index.lock"
        if lock_path.exists():
            try:
                lock_path.unlink()
                logger.info("[WorktreeMerge] Removed stale index.lock: %s", lock_path)
                return True
            except OSError as e:
                logger.warning("[WorktreeMerge] Failed to remove index.lock %s: %s", lock_path, e)
                return False
        return False

    def _git_add_commit_if_dirty(self, cwd: Path, message: str) -> str | None:
        """暂存并提交变更（如果有），返回 commit hash 或 None。

        先用 git status --porcelain 检查是否有变更，无变更直接返回，避免无条件
        执行 git add -A（遍历整个项目添加所有文件到 index，大项目耗时 5-15s）。
        有变更时才执行 git add -A + commit。
        """
        self._remove_index_lock(cwd)

        rc, status, _ = self._run_git("status", "--porcelain", cwd=cwd)
        if rc != 0 or not status.strip():
            return None

        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            logger.warning("[WorktreeMerge] .gitignore 不存在，生成最小保护版本: %s", gitignore)
            with contextlib.suppress(OSError):
                gitignore.write_text(
                    "data/\n__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\nnode_modules/\n.env\n*.log\n*.bak\n",
                    encoding="utf-8",
                )

        rc, _, _ = self._run_git("add", "-A", cwd=cwd)
        if rc != 0:
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-A", cwd=cwd)
            if rc != 0:
                return None

        commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
        if commit_rc != 0:
            self._remove_index_lock(cwd)
            commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
            if commit_rc != 0:
                return None
        _, h, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
        return h.strip() if h else None

    def _git_add_tracked_and_commit(self, cwd: Path, message: str) -> str | None:
        """只提交已跟踪文件的修改，不添加未跟踪文件。返回 commit hash 或 None。"""
        self._remove_index_lock(cwd)
        rc, _, _ = self._run_git("add", "-u", cwd=cwd)
        if rc != 0:
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-u", cwd=cwd)
            if rc != 0:
                return None
        rc, status, _ = self._run_git("status", "--porcelain", "-uno", cwd=cwd)
        if rc == 0 and status.strip():
            commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
            if commit_rc != 0:
                self._remove_index_lock(cwd)
                commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
                if commit_rc != 0:
                    return None
            _, h, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
            return h.strip() if h else None
        return None

    # ── 合并门控（自 _workspace_merge_ops.py 原样移植，判定不变）──

    def merge_worktree_before_complete(self, task_id: str, ws_meta: Any) -> str | None:
        """任务标记 completed 前的合并门控（统一入口）。

        数据源适配：ws_meta 由调用方从管道 state 解析传入（0.1 从
        task_tree/task.metadata 读取并带 store 兜底——0.2 该通路不存在，
        state 是唯一真值，读不到即失败显式暴露）。

        Returns:
            None 表示合并成功或不需要合并（plain/shared 模式），
            str 表示合并失败原因，调用方应据此标记任务 failed。
        """
        if not ws_meta or not isinstance(ws_meta, dict):
            # 无法确定 ws_meta，但任务确实跑过隔离逻辑 → 视为致命错误而非静默跳过。
            # 静默返回 None 会让 worktree 产出永远丢失（既往故障根因）。
            return f"无法获取任务 {task_id} 的 ws_meta，worktree 合并被跳过"

        mode = ws_meta.get("mode", "")
        if mode != "worktree":
            # plain / shared 等无需合并的模式
            return None

        workspace = ws_meta.get("path", "")
        if not workspace:
            return f"worktree 模式但 ws_meta.path 为空，task_id={task_id}"

        result = self.on_eval_passed(task_id, workspace, ws_meta)
        if result.get("success"):
            conflict_files = result.get("conflict_files", [])
            if conflict_files:
                logger.warning(
                    "[WorktreeMerge] worktree 合并完成但有冲突文件: task_id=%s, conflicts=%s",
                    task_id,
                    conflict_files,
                )
            return None

        error_parts = [result.get("error", "unknown")]
        if result.get("verify_error"):
            error_parts.append(f"验证详情: {result['verify_error']}")
        return ", ".join(error_parts)

    def on_eval_passed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估通过后的 worktree 合并（并发安全：按 project_root 粒度加锁）。

        0.1 版还分流 plain/shared 的 no-op 分支——本入口上方已按 mode 过滤，
        仅 worktree 路径可达，故只保留 worktree 合并逻辑（判定原样）。
        """
        project_root = ws_meta.get("project_root", "")
        lock = self._get_merge_lock(project_root)
        with lock:
            max_retries = 2
            verify_detail = ""
            result: dict[str, Any] = {}
            for attempt in range(1, max_retries + 1):
                result = self._safe_merge(workspace, ws_meta)
                if not result.get("success"):
                    logger.warning(
                        "[WorktreeMerge] 合并失败 (attempt %d/%d)，跳过清理以保留文件: "
                        "task_id=%s, workspace=%s, error=%s",
                        attempt,
                        max_retries,
                        task_id,
                        workspace,
                        result.get("error", "unknown"),
                    )
                    if attempt < max_retries:
                        continue
                    return result
                verified, verify_detail = self._verify_merge_result(workspace, project_root, ws_meta, result)
                if verified:
                    logger.debug(
                        "[WorktreeMerge] 合并验证通过 (attempt %d): task_id=%s, method=%s",
                        attempt,
                        task_id,
                        result.get("method"),
                    )
                    self._cleanup_worktree(
                        workspace, ws_meta, tag_task_id=task_id, merge_method=result.get("method", "")
                    )
                    # P1: 合并成功后清理 project_root 中的 unstaged 残留
                    self._cleanup_unstaged_changes(project_root)
                    return result
                logger.warning(
                    "[WorktreeMerge] 合并验证失败 (attempt %d/%d): task_id=%s, detail=%s",
                    attempt,
                    max_retries,
                    task_id,
                    verify_detail,
                )
                if attempt < max_retries:
                    continue
            logger.error(
                "[WorktreeMerge] 合并重试耗尽，保留 worktree 不清理: task_id=%s, workspace=%s",
                task_id,
                workspace,
            )
            result["verify_error"] = verify_detail
            if "error" not in result:
                result["error"] = f"合并验证失败(重试{max_retries}次): {verify_detail}"
            result["success"] = False
            return result

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：通过 git merge 将 worktree 分支合并到项目根目录。"""
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        if not branch:
            return {"success": False, "error": "缺少 branch 信息，ws_meta 不完整"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(ws_path, "chore: auto commit before merge")
        self._ensure_git_user(proj_path)
        self._git_add_tracked_and_commit(proj_path, "chore: auto-save before merge")
        rc, current_branch, _ = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=proj_path)
        if rc != 0 or not current_branch.strip():
            return {"success": False, "error": f"无法获取当前分支: rc={rc}, output={current_branch!r}"}
        # 校验待合并分支存在：worktree 模式下 branch 来自 ws_meta，
        # 子任务 inherit 父任务工作空间时可能复用已被清理的分支引用
        # （分支已删但元数据仍在），此时 git merge 会报模糊的
        # "not something we can merge"，提前校验给出明确根因。
        rc_v, _, verify_err = self._run_git("rev-parse", "--verify", f"{branch}^{{commit}}", cwd=proj_path)
        if rc_v != 0:
            return {
                "success": False,
                "error": f"待合并分支不存在(branch={branch})，"
                f"可能继承自已清理的父任务 worktree: "
                f"{verify_err[:200] if verify_err else 'unknown'}",
            }
        rc_pre, pre_merge_head, _ = self._run_git("rev-parse", "HEAD", cwd=proj_path)
        rc, stdout, stderr = self._run_git("merge", branch, cwd=proj_path)
        if rc == 0:
            result = {"success": True, "action": "merged", "method": "git_merge"}
            if rc_pre == 0 and pre_merge_head.strip():
                result["pre_merge_head"] = pre_merge_head.strip()
            return result
        # 合并失败诊断：git merge 把 CONFLICT 行写到 stdout（不是 stderr），
        # 仅读 stderr 会得到空字符串 → 上层 fallback 成 "unknown"，丢失所有冲突信息。
        # 这里在 merge --abort 前用 git diff --name-only --diff-filter=U 抓取冲突文件清单，
        # 再连同 stdout/stderr 一起拼进 error，确保失败原因可定位、可诊断。
        rc_conflict, conflict_files, _ = self._run_git(
            "diff", "--name-only", "--diff-filter=U", cwd=proj_path
        )
        self._run_git("merge", "--abort", cwd=proj_path)
        detail_parts = []
        if stdout.strip():
            detail_parts.append(f"stdout={stdout[:300]}")
        if stderr.strip():
            detail_parts.append(f"stderr={stderr[:300]}")
        if rc_conflict == 0 and conflict_files.strip():
            files_list = ", ".join(conflict_files.strip().splitlines()[:10])
            detail_parts.append(f"冲突文件={files_list}")
        detail = " | ".join(detail_parts) if detail_parts else "无输出(可能是文件系统错误)"
        return {"success": False, "error": f"git merge 失败(branch={branch}): {detail}"}

    # ── 合并验证（自 _workspace_merge_ops.py 原样移植）──────────

    @staticmethod
    def _first_missing_paths(proj_path: Path, expected: list[str]) -> list[str]:
        """按声明序找出前若干个未落地的目标文件（上限 10 个早退）。"""
        missing: list[str] = []
        for rel_str in expected:
            if not (proj_path / rel_str).exists():
                missing.append(rel_str)
            if len(missing) >= 10:
                break
        return missing

    @staticmethod
    def _same_name_sibling_exists(target: Path) -> bool:
        """模糊匹配：同名目录下搜索同名文件（git 输出编码不一致时路径不完全匹配）。"""
        parent = target.parent
        target_name = target.name
        if not (parent.exists() and target_name):
            return False
        try:
            return any(existing.name == target_name for existing in parent.iterdir())
        except OSError:
            return False

    def _missing_branch_diff_files(self, branch: str, proj_path: Path) -> list[str]:
        """对 git_merge 分支 diff 应达清单做磁盘核验，返回前 10 个缺失项。

        --diff-filter=AMRC 只校验应到达目标的文件（新增/修改/重命名新路径/复制），
        排除删除(D)。否则任务正确删除的废弃文件合并后本就不存在，
        会被 exists() 误判为「文件未到达目标」，导致重组/清理类任务必然合并失败。
        """
        rc, diff_out, _ = self._run_git(
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-only",
            "--diff-filter=AMRC",
            branch + "~1",
            branch,
            cwd=proj_path,
        )
        if rc != 0 or not diff_out.strip():
            return []
        branch_files = set(diff_out.strip().splitlines())
        missing: list[str] = []
        for f in branch_files:
            f_stripped = f.strip().strip('"')
            target = proj_path / f_stripped
            if not target.exists() and not self._same_name_sibling_exists(target):
                missing.append(f_stripped)
            if len(missing) >= 10:
                break
        return missing

    def _verify_merge_result(
        self,
        workspace: str,
        project_root: str,
        ws_meta: dict,
        merge_result: dict,
    ) -> tuple[bool, str]:
        """统一验证合并是否成功：不论 git_merge 还是 copy_merge 都验证文件到达。"""
        branch = ws_meta.get("branch", "")
        method = merge_result.get("method", "")
        proj_path = Path(project_root)

        if not proj_path.exists():
            return False, f"project_root 不存在: {project_root}"

        if method == "git_merge" and branch and not self._verify_merge_in_main(branch, cwd=proj_path):
            return False, f"git_merge commit graph 验证失败: branch={branch}"

        merged_files = merge_result.get("merged_files", [])
        if method == "copy" and merged_files:
            missing = self._first_missing_paths(proj_path, merged_files)
            if missing:
                return False, f"copy_merge 文件验证失败: {len(missing)} 个文件未到达目标，前几个: {missing[:5]}"

        if method == "git_merge" and branch:
            missing = self._missing_branch_diff_files(branch, proj_path)
            if missing:
                return False, f"git_merge 文件验证失败: {len(missing)} 个文件未到达目标"

        return True, "验证通过"

    def _verify_merge_in_main(self, branch_name: str, cwd: Path) -> bool:
        """验证分支已合并到当前分支：git log HEAD..{branch} 应为空，不为空则阻止后续清理。"""
        rc, log_output, _ = self._run_git("log", f"HEAD..{branch_name}", cwd=cwd)
        if rc != 0:
            logger.warning("[WorktreeMerge] 验证合并状态失败: branch=%s", branch_name)
            return False
        if log_output.strip():
            logger.warning(
                "[WorktreeMerge] 分支未完全合并: branch=%s, 未合并=%d", branch_name, len(log_output.splitlines())
            )
            return False
        return True

    def _cleanup_worktree(
        self,
        workspace: str,
        ws_meta: dict,
        *,
        tag_task_id: str = "",
        merge_method: str = "",
    ):
        """清理 worktree：删 worktree → 条件打 tag → 删分支

        project_root 缺失时不能静默跳过清理，否则 worktree 目录与 task 分支会泄漏堆积。
        因此缺失时显式 warning，并用 worktree 目录自身反查仓库根
        （git -C <workspace> rev-parse --show-toplevel）兜底；仍定位不到仓库才放弃，
        并记录错误。
        """
        project_root = Path(ws_meta.get("project_root", ""))
        branch = ws_meta.get("branch", "")

        # project_root 缺失时，从 worktree 目录自身反查仓库根兜底，避免静默跳过
        if not project_root.exists():
            logger.warning(
                "[WorktreeMerge] project_root 无效或缺失: %r，尝试从 worktree 反查仓库根: %s",
                str(project_root),
                workspace,
            )
            ws_path_probe = Path(workspace)
            if ws_path_probe.exists():
                rc, out, err = self._run_git(
                    "rev-parse",
                    "--show-toplevel",
                    cwd=ws_path_probe,
                )
                if rc == 0 and out.strip():
                    project_root = Path(out.strip())
                    logger.debug("[WorktreeMerge] 已反查仓库根: %s", project_root)
                else:
                    logger.warning(
                        "[WorktreeMerge] 反查仓库根失败(rc=%s): %s，放弃清理: %s",
                        rc,
                        err.strip(),
                        workspace,
                    )
                    return
            else:
                logger.warning(
                    "[WorktreeMerge] worktree 目录不存在，跳过清理: %s",
                    workspace,
                )
                return

        try:
            self._run_git("worktree", "remove", str(workspace), "--force", cwd=project_root)
        except Exception as e:
            logger.warning("[WorktreeMerge] git worktree remove 失败: %s, %s", workspace, e)
            self._run_git("worktree", "prune", cwd=project_root)
        if branch:
            if tag_task_id and merge_method == "git_merge":
                tag = f"task-merge/{tag_task_id[:8]}"
                self._run_git("tag", tag, branch, cwd=project_root)
                logger.debug("[WorktreeMerge] 已打 tag: %s，可 git revert 回退", tag)
            self._run_git("worktree", "prune", cwd=project_root)
            self._run_git("branch", "-D", branch, cwd=project_root)
        ws_path = Path(workspace).resolve()
        if ws_path.exists() and "__wt_" in ws_path.name:
            try:
                _force_rmtree(str(ws_path))
                logger.debug("[WorktreeMerge] 强制清理残留 worktree 目录: %s", workspace)
            except OSError as e:
                logger.warning("[WorktreeMerge] 强制清理 worktree 目录失败: %s, %s", workspace, e)

    def _cleanup_unstaged_changes(self, project_root: str) -> None:
        """检测合并后 project_root 的 unstaged 变更，只记录警告，绝不自动丢弃。"""
        proj_path = Path(project_root)
        if not proj_path.exists():
            return

        rc, status, _ = self._run_git("status", "--porcelain", cwd=proj_path)
        if rc != 0 or not status.strip():
            return

        unstaged_lines = [line for line in status.splitlines() if len(line) >= 2 and line[1] in ("M", "D")]
        if not unstaged_lines:
            return

        # 安全契约：此处只告警不修改工作区，避免丢失用户未提交的改动。
        logger.warning(
            "[WorktreeMerge] 合并后检测到 %d 个 unstaged 变更，已保留未丢弃（避免数据丢失）: "
            "project_root=%s, 文件=%s",
            len(unstaged_lines),
            project_root,
            [line.strip() for line in unstaged_lines[:10]],
        )


# 进程内单例（0.1 manager 单例语义：合并锁跨调用/跨任务有效）。
_shared_merger = WorktreeMerger()


def merge_worktree_before_complete(task_id: str, ws_meta: Any) -> str | None:
    """模块级统一入口：completed 前的 worktree 合并门控。

    Args:
        task_id: 任务 ID（tag 命名与日志定位用）。
        ws_meta: 任务工作空间元数据（调用方从管道 state 解析；None/非 dict
            按失败处理，不静默跳过）。

    Returns:
        None = 合并成功或无需合并（plain/shared）；str = 失败原因。
    """
    return _shared_merger.merge_worktree_before_complete(task_id, ws_meta)
