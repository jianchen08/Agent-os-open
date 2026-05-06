"""
资源合并与回滚工具

基于 git 的资源合并与回滚工具，用于替代断链的 rollback_task 工具。
通过在 workspace 中维护 git 仓库，实现文件变更的追踪、合并和回滚。

暴露接口：
- get_tool_definition() -> Tool：获取工具定义
- ResourceMergeTool：资源合并与回滚工具类
"""

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# git 命令执行超时时间（秒）
_GIT_TIMEOUT = 30


class ResourceMergeTool(BuiltinTool):
    """
    资源合并与回滚工具

    基于 git worktree 实现的 workspace 文件管理工具：
    - prepare: 基于项目仓库创建 worktree 分支，workspace 拥有完整项目代码
    - merge: 将 workspace 中的变更合并到目标目录
    - rollback: 在 worktree 中恢复到分支初始状态
    - git_status/git_commit/git_diff/git_log: git 操作
    - cleanup: 移除 worktree 并删除分支
    """

    _BRANCH_PREFIX = "task/"

    def __init__(self, base_path: str | None = None):
        """初始化资源合并工具

        Args:
            base_path: 项目根目录路径，默认为当前工作目录
        """
        self.base_path = Path(base_path) if base_path else Path.cwd()

    def _get_branch_name(self, workspace: Path) -> str:
        """根据 workspace 路径生成分支名

        Args:
            workspace: workspace 目录路径

        Returns:
            分支名称
        """
        dir_name = workspace.name
        return f"{self._BRANCH_PREFIX}{dir_name}"

    async def _is_worktree(self, workspace: Path) -> bool:
        """检查 workspace 是否是项目的 git worktree

        Args:
            workspace: workspace 目录路径

        Returns:
            是否是 worktree
        """
        git_file = workspace / ".git"
        if not git_file.exists():
            return False
        if git_file.is_file():
            return True
        return False

    async def _ensure_project_repo(self) -> ToolResult | None:
        """确保 base_path 是一个 git 仓库

        Returns:
            如果不是 git 仓库返回失败结果，否则返回 None
        """
        return_code, _, _ = await self._run_git(
            "rev-parse", "--git-dir", cwd=self.base_path,
        )
        if return_code != 0:
            return create_failure_result(
                error=f"项目目录不是 git 仓库: {self.base_path}",
                error_code="NOT_A_GIT_REPO",
            )
        return None

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="resource_merge",
            description="基于 git worktree 的资源合并与回滚工具（通常由系统自动调用）。"
            "大多数情况下不需要手动使用。"
            "需要手动查看变更(git_diff)、回滚(rollback)时可以使用。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "prepare",
                            "merge",
                            "rollback",
                            "git_status",
                            "git_commit",
                            "git_diff",
                            "git_log",
                            "git_merge_abort",
                            "cleanup",
                        ],
                        "description": "操作类型："
                        "prepare(创建 worktree 分支作为工作空间)、"
                        "merge(合并工作空间变更到目标目录)、"
                        "rollback(恢复工作空间到初始状态)、"
                        "git_status(查看git状态)、"
                        "git_commit(提交变更)、"
                        "git_diff(查看变更详情)、"
                        "git_log(查看提交历史)、"
                        "git_merge_abort(中止当前的 git merge 操作)、"
                        "cleanup(移除 worktree 和分支)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "workspace 目录路径（绝对路径或相对于项目根目录的路径）",
                    },
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "目标文件路径列表，用于 prepare（复制到workspace）和 merge（确定合并范围）操作",
                    },
                    "target_dir": {
                        "type": "string",
                        "description": "目标目录路径，用于 merge 操作，workspace 中的文件将合并到此目录",
                    },
                    "message": {
                        "type": "string",
                        "description": "commit 消息，用于 git_commit 操作",
                    },
                    "checkpoint_id": {
                        "type": "string",
                        "description": "指定回滚到的 commit hash，用于 rollback 操作。不指定则回滚到上一个 commit",
                    },
                    "merge_strategy": {
                        "type": "string",
                        "enum": ["copy", "git_merge", "git_merge_no_ff"],
                        "default": "copy",
                        "description": "合并策略 — copy(文件复制)、git_merge(快进合并)、git_merge_no_ff(非快进合并)",
                    },
                },
                "required": ["action", "workspace"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.ALL,
            tags=["git", "merge", "rollback", "resource"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行工具

        根据 action 参数分派到对应的处理方法。

        Args:
            inputs: 工具输入参数

        Returns:
            工具执行结果
        """
        action = inputs.get("action")
        workspace_str = inputs.get("workspace")

        if not action:
            return create_failure_result(
                error="action 参数不能为空",
                error_code="MISSING_ACTION",
            )

        if not workspace_str:
            return create_failure_result(
                error="workspace 参数不能为空",
                error_code="MISSING_WORKSPACE",
            )

        # 解析 workspace 路径
        workspace = self._resolve_path(workspace_str)

        # 分派到对应的 action 处理方法
        action_map = {
            "prepare": self._prepare,
            "merge": self._merge,
            "rollback": self._rollback,
            "git_status": self._git_status,
            "git_commit": self._git_commit,
            "git_diff": self._git_diff,
            "git_log": self._git_log,
            "git_merge_abort": self._git_merge_abort,
            "cleanup": self._cleanup,
        }

        handler = action_map.get(action)
        if handler is None:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

        return await handler(inputs, workspace)

    def _resolve_path(self, path_str: str) -> Path:
        """解析路径为绝对路径

        支持绝对路径和相对于项目根目录的相对路径。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的绝对路径
        """
        path = Path(path_str)
        if not path.is_absolute():
            path = self.base_path / path
        return path.resolve()

    async def _run_git(
        self,
        *args: str,
        cwd: Path,
        timeout: int = _GIT_TIMEOUT,
    ) -> tuple[int, str, str]:
        """执行 git 命令

        使用 asyncio.create_subprocess_exec 执行 git 命令，
        捕获 stdout 和 stderr。

        Args:
            *args: git 命令参数（不含 "git" 本身）
            cwd: 工作目录
            timeout: 超时时间（秒）

        Returns:
            (退出码, stdout, stderr) 元组
        """
        cmd = ["git"] + list(args)
        logger.debug(f"[resource_merge] 执行命令: {' '.join(cmd)}, cwd={cwd}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            return process.returncode or 0, stdout, stderr
        except asyncio.TimeoutError:
            return -1, "", f"命令执行超时（{timeout}秒）"
        except FileNotFoundError:
            return -1, "", "未找到 git 命令，请确保系统已安装 git"

    async def _ensure_git_repo(self, workspace: Path) -> ToolResult | None:
        """确保 workspace 目录存在且已初始化 git 仓库

        如果 workspace 是 worktree 则直接返回，否则执行 git init。

        Args:
            workspace: workspace 目录路径

        Returns:
            如果出错返回失败结果，成功返回 None
        """
        if await self._is_worktree(workspace):
            return None

        workspace.mkdir(parents=True, exist_ok=True)

        git_dir = workspace / ".git"
        if not git_dir.exists():
            return_code, stdout, stderr = await self._run_git("init", cwd=workspace)
            if return_code != 0:
                return create_failure_result(
                    error=f"git init 失败: {stderr}",
                    error_code="GIT_INIT_FAILED",
                )

        return None

    async def _prepare(self, inputs: dict[str, Any], workspace: Path) -> ToolResult:
        """prepare 操作：基于项目仓库创建 worktree 分支

        1. 检查项目目录是 git 仓库
        2. 执行 git worktree add 创建新分支
        3. workspace 中拥有完整项目代码

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径

        Returns:
            包含 branch_name 和 workspace 路径的成功结果
        """
        try:
            error = await self._ensure_project_repo()
            if error:
                return error

            branch_name = self._get_branch_name(workspace)

            if await self._is_worktree(workspace):
                return create_success_result(
                    data={
                        "action": "prepare",
                        "workspace": str(workspace),
                        "branch_name": branch_name,
                        "message": "workspace 已是 worktree，无需重复创建",
                    },
                )

            return_code, stdout, stderr = await self._run_git(
                "worktree", "add",
                "-b", branch_name,
                str(workspace),
                "HEAD",
                cwd=self.base_path,
            )

            if return_code != 0:
                return create_failure_result(
                    error=f"git worktree add 失败: {stderr}",
                    error_code="WORKTREE_ADD_FAILED",
                )

            return_code, commit_hash, _ = await self._run_git(
                "rev-parse", "HEAD", cwd=workspace,
            )

            return create_success_result(
                data={
                    "action": "prepare",
                    "workspace": str(workspace),
                    "branch_name": branch_name,
                    "base_commit": commit_hash.strip() if return_code == 0 else None,
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"prepare 操作失败: {str(e)}",
                error_code="PREPARE_FAILED",
            )

    async def _merge(self, inputs: dict[str, Any], workspace: Path) -> ToolResult:
        """merge 操作：根据合并策略将 workspace 中的变更合并到目标目录

        根据 merge_strategy 参数分派到不同的合并方式：
        - copy: 通过文件复制合并（原有逻辑）
        - git_merge / git_merge_no_ff: 通过 git merge 合并

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径

        Returns:
            包含合并结果和变更报告的成功结果
        """
        merge_strategy = inputs.get("merge_strategy", "copy")

        if merge_strategy == "copy":
            return await self._merge_copy(inputs, workspace)
        elif merge_strategy in ("git_merge", "git_merge_no_ff"):
            return await self._git_merge(inputs, workspace)
        else:
            return create_failure_result(
                error=f"不支持的合并策略: {merge_strategy}",
                error_code="INVALID_MERGE_STRATEGY",
            )

    async def _merge_copy(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """通过文件复制方式将 workspace 变更合并到目标目录

        支持两种模式：
        1. git worktree 模式：通过 git diff 获取变更文件列表
        2. 直接复制模式：当 workspace 不是 worktree 时，遍历文件直接复制

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径

        Returns:
            包含合并结果和变更报告的成功结果
        """
        try:
            target_dir_str = inputs.get("target_dir")
            target_files = inputs.get("target_files", [])

            if target_dir_str:
                target_dir = self._resolve_path(target_dir_str)
            else:
                target_dir = self.base_path

            is_worktree = await self._is_worktree(workspace)

            changed_files = []
            if is_worktree:
                base_commit = inputs.get("checkpoint_id")
                if not base_commit:
                    base_commit = inputs.get("base_commit")

                if target_files:
                    changed_files = target_files
                else:
                    if base_commit:
                        return_code, diff_output, _ = await self._run_git(
                            "diff", "--name-status", base_commit, "HEAD",
                            cwd=workspace,
                        )
                    else:
                        return_code, diff_output, _ = await self._run_git(
                            "diff", "--name-status", "HEAD", cwd=workspace,
                        )

                    if return_code == 0 and diff_output:
                        for line in diff_output.splitlines():
                            parts = line.strip().split("\t", 1)
                            if len(parts) == 2:
                                changed_files.append(parts[1])
            else:
                if target_files:
                    changed_files = target_files
                else:
                    changed_files = self._scan_workspace_files(workspace)

            merged_files = []
            change_report: dict[str, list[str]] = {
                "added": [],
                "modified": [],
                "deleted": [],
            }

            for file_rel_path in changed_files:
                src = workspace / file_rel_path
                dst = target_dir / file_rel_path

                if src.exists():
                    dst_already_exists = dst.exists()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                    merged_files.append(file_rel_path)

                    if dst_already_exists:
                        change_report["modified"].append(file_rel_path)
                    else:
                        change_report["added"].append(file_rel_path)
                else:
                    if dst.exists():
                        dst.unlink()
                        change_report["deleted"].append(file_rel_path)

            logger.info(
                "ResourceMerge merge: mode=%s, workspace=%s, target=%s, merged=%d files",
                "worktree" if is_worktree else "direct",
                workspace, target_dir, len(merged_files),
            )

            return create_success_result(
                data={
                    "action": "merge",
                    "workspace": str(workspace),
                    "target_dir": str(target_dir),
                    "merged_files": merged_files,
                    "change_report": change_report,
                    "mode": "worktree" if is_worktree else "direct",
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"merge 操作失败: {str(e)}",
                error_code="MERGE_FAILED",
            )

    async def _git_merge(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """通过 git merge 策略将 workspace 分支合并到主仓库

        流程：
        1. 在 workspace 中 git add -A + git commit 提交所有变更
        2. 在主仓库（base_path）中执行 git merge 合并 workspace 分支
        3. 成功则返回 merge commit hash
        4. 失败则检测冲突，有冲突时执行 git merge --abort 并返回冲突文件列表

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径

        Returns:
            包含合并结果的成功结果，或冲突/失败的错误结果
        """
        try:
            merge_strategy = inputs.get("merge_strategy", "git_merge")
            branch_name = self._get_branch_name(workspace)

            # 确保 workspace 是 worktree
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="git_merge 策略要求 workspace 为 git worktree，请先执行 prepare",
                    error_code="NOT_A_WORKTREE",
                )

            # 确保主仓库是 git 仓库
            error = await self._ensure_project_repo()
            if error:
                return error

            # 配置 workspace 的 git 用户信息
            await self._run_git(
                "config", "user.email", "resource-merge@agent.local",
                cwd=workspace,
            )
            await self._run_git(
                "config", "user.name", "Agent Resource Merge",
                cwd=workspace,
            )

            # 在 workspace 中暂存所有变更
            return_code, _, stderr = await self._run_git(
                "add", "-A", cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git add 失败: {stderr}",
                    error_code="GIT_ADD_FAILED",
                )

            # 检查是否有变更需要提交
            return_code, status_output, _ = await self._run_git(
                "status", "--porcelain", cwd=workspace,
            )
            if status_output.strip():
                # 有变更，执行 commit
                commit_msg = f"auto: merge from {branch_name}"
                return_code, _, stderr = await self._run_git(
                    "commit", "-m", commit_msg, cwd=workspace,
                )
                if return_code != 0:
                    return create_failure_result(
                        error=f"git commit 失败: {stderr}",
                        error_code="GIT_COMMIT_FAILED",
                    )

            # 配置主仓库的 git 用户信息
            await self._run_git(
                "config", "user.email", "resource-merge@agent.local",
                cwd=self.base_path,
            )
            await self._run_git(
                "config", "user.name", "Agent Resource Merge",
                cwd=self.base_path,
            )

            # 在主仓库中执行 git merge
            merge_args = ["merge", branch_name]
            if merge_strategy == "git_merge_no_ff":
                merge_args.append("--no-ff")

            return_code, stdout, stderr = await self._run_git(
                *merge_args, cwd=self.base_path,
            )

            if return_code == 0:
                # 合并成功，获取 merge commit hash
                _, commit_hash, _ = await self._run_git(
                    "rev-parse", "HEAD", cwd=self.base_path,
                )
                logger.info(
                    "[resource_merge] git merge 成功: branch=%s, commit=%s",
                    branch_name, commit_hash.strip(),
                )
                return create_success_result(
                    data={
                        "action": "merge",
                        "workspace": str(workspace),
                        "target_dir": str(self.base_path),
                        "merge_strategy": merge_strategy,
                        "branch_name": branch_name,
                        "merge_commit": commit_hash.strip(),
                        "mode": "git_merge",
                    },
                )

            # 合并失败，检查是否存在冲突
            return_code_diff, diff_output, _ = await self._run_git(
                "diff", "--name-only", "--diff-filter=U", cwd=self.base_path,
            )

            conflict_files = []
            if return_code_diff == 0 and diff_output.strip():
                conflict_files = [
                    line.strip() for line in diff_output.splitlines() if line.strip()
                ]

            # 执行 git merge --abort 撤销合并
            await self._run_git("merge", "--abort", cwd=self.base_path)

            if conflict_files:
                logger.warning(
                    "[resource_merge] git merge 冲突: branch=%s, conflicts=%s",
                    branch_name, conflict_files,
                )
                return create_failure_result(
                    error=f"合并冲突，已自动中止。冲突文件: {', '.join(conflict_files)}",
                    error_code="MERGE_CONFLICT",
                    metadata={
                        "action": "merge",
                        "workspace": str(workspace),
                        "merge_strategy": merge_strategy,
                        "branch_name": branch_name,
                        "conflict_files": conflict_files,
                        "mode": "git_merge",
                    },
                )

            # 其他类型的合并失败
            logger.error(
                "[resource_merge] git merge 失败: branch=%s, stderr=%s",
                branch_name, stderr,
            )
            return create_failure_result(
                error=f"git merge 失败: {stderr}",
                error_code="MERGE_FAILED",
                metadata={
                    "action": "merge",
                    "workspace": str(workspace),
                    "merge_strategy": merge_strategy,
                    "branch_name": branch_name,
                    "mode": "git_merge",
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git merge 操作失败: {str(e)}",
                error_code="MERGE_FAILED",
            )

    @staticmethod
    def _scan_workspace_files(workspace: Path) -> list[str]:
        """扫描工作空间中所有文件，返回相对路径列表

        跳过隐藏目录（.git, .ai_workspaces 等）和 __pycache__ 目录。

        Args:
            workspace: 工作空间目录路径

        Returns:
            相对路径字符串列表
        """
        skip_dirs = {".git", ".ai_workspaces", "__pycache__", ".pytest_cache", "node_modules"}
        result = []
        try:
            for item in workspace.rglob("*"):
                if not item.is_file():
                    continue
                parts = item.relative_to(workspace).parts
                if any(p in skip_dirs for p in parts):
                    continue
                result.append(str(item.relative_to(workspace)))
        except Exception:
            pass
        return result

    async def _rollback(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """rollback 操作：在 worktree 中恢复到分支初始状态

        通过 git checkout -- . 恢复所有文件到 HEAD 状态。

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径

        Returns:
            包含回滚结果的成功结果
        """
        try:
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化，请先执行 prepare",
                    error_code="NOT_INITIALIZED",
                )

            return_code, _, stderr = await self._run_git(
                "checkout", "--", ".", cwd=workspace,
            )

            if return_code != 0:
                return create_failure_result(
                    error=f"git checkout 失败: {stderr}",
                    error_code="GIT_CHECKOUT_FAILED",
                )

            return_code, _, stderr = await self._run_git(
                "clean", "-fd", cwd=workspace,
            )

            return create_success_result(
                data={
                    "action": "rollback",
                    "workspace": str(workspace),
                    "message": "已恢复到分支初始状态",
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"rollback 操作失败: {str(e)}",
                error_code="ROLLBACK_FAILED",
            )

    async def _git_status(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """git_status 操作：查看 workspace 的 git 状态"""
        try:
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            return_code, stdout, stderr = await self._run_git(
                "status", "--porcelain", cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git status 失败: {stderr}",
                    error_code="GIT_STATUS_FAILED",
                )

            # 解析状态
            status_lines = stdout.splitlines() if stdout else []
            staged = []
            unstaged = []
            untracked = []

            for line in status_lines:
                if not line.strip():
                    continue
                status_code = line[:2]
                file_path = line[3:].strip()

                if status_code.startswith("?"):
                    untracked.append(file_path)
                elif status_code[0] in ("A", "M", "D", "R"):
                    staged.append(file_path)
                elif status_code[1] in ("M", "D"):
                    unstaged.append(file_path)
                else:
                    staged.append(file_path)

            return create_success_result(
                data={
                    "action": "git_status",
                    "workspace": str(workspace),
                    "staged": staged,
                    "unstaged": unstaged,
                    "untracked": untracked,
                    "total_changes": len(status_lines),
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git_status 操作失败: {str(e)}",
                error_code="GIT_STATUS_FAILED",
            )

    async def _git_commit(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """git_commit 操作：暂存并提交 workspace 中的变更"""
        try:
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            message = inputs.get("message", "chore: update workspace files")

            # 配置 git 用户信息
            await self._run_git(
                "config", "user.email", "resource-merge@agent.local",
                cwd=workspace,
            )
            await self._run_git(
                "config", "user.name", "Agent Resource Merge",
                cwd=workspace,
            )

            # 暂存所有变更
            return_code, _, stderr = await self._run_git(
                "add", "-A", cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git add 失败: {stderr}",
                    error_code="GIT_ADD_FAILED",
                )

            # 检查是否有变更需要提交
            return_code, status_output, _ = await self._run_git(
                "status", "--porcelain", cwd=workspace,
            )
            if not status_output.strip():
                return create_success_result(
                    data={
                        "action": "git_commit",
                        "workspace": str(workspace),
                        "message": "没有需要提交的变更",
                    },
                )

            # 提交变更
            return_code, _, stderr = await self._run_git(
                "commit", "-m", message, cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git commit 失败: {stderr}",
                    error_code="GIT_COMMIT_FAILED",
                )

            # 获取 commit hash
            return_code, commit_hash, _ = await self._run_git(
                "rev-parse", "HEAD", cwd=workspace,
            )

            return create_success_result(
                data={
                    "action": "git_commit",
                    "workspace": str(workspace),
                    "commit_hash": commit_hash if return_code == 0 else None,
                    "message": message,
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git_commit 操作失败: {str(e)}",
                error_code="GIT_COMMIT_FAILED",
            )

    async def _git_diff(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """git_diff 操作：查看 workspace 中的变更"""
        try:
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            # 查看暂存区和工作区的变更
            return_code, stdout, stderr = await self._run_git(
                "diff", "HEAD", cwd=workspace,
            )
            if return_code != 0:
                # 可能是没有历史 commit，尝试查看暂存区变更
                return_code, stdout, stderr = await self._run_git(
                    "diff", "--cached", cwd=workspace,
                )
                if return_code != 0:
                    return create_failure_result(
                        error=f"git diff 失败: {stderr}",
                        error_code="GIT_DIFF_FAILED",
                    )

            return create_success_result(
                data={
                    "action": "git_diff",
                    "workspace": str(workspace),
                    "diff": stdout,
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git_diff 操作失败: {str(e)}",
                error_code="GIT_DIFF_FAILED",
            )

    async def _git_log(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """git_log 操作：查看 workspace 的提交历史"""
        try:
            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            # 获取提交历史（最多 20 条）
            return_code, stdout, stderr = await self._run_git(
                "log", "--oneline", "--max-count=20",
                "--format=%H|%s|%ai",
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git log 失败: {stderr}",
                    error_code="GIT_LOG_FAILED",
                )

            # 解析提交历史
            commits = []
            if stdout:
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append({
                            "hash": parts[0],
                            "message": parts[1],
                            "time": parts[2],
                        })

            return create_success_result(
                data={
                    "action": "git_log",
                    "workspace": str(workspace),
                    "commits": commits,
                    "count": len(commits),
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git_log 操作失败: {str(e)}",
                error_code="GIT_LOG_FAILED",
            )

    async def _git_merge_abort(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """中止当前正在进行的 git merge 操作

        在主仓库（base_path）中执行 git merge --abort，
        撤销所有合并相关的变更，恢复到合并前的状态。

        Args:
            inputs: 工具输入参数
            workspace: workspace 目录路径（此操作不直接使用，但保持接口一致）

        Returns:
            中止操作的结果
        """
        try:
            # 确保主仓库是 git 仓库
            error = await self._ensure_project_repo()
            if error:
                return error

            # 检查是否正在合并中
            return_code, stdout, _ = await self._run_git(
                "rev-parse", "--verify", "MERGE_HEAD",
                cwd=self.base_path,
            )
            if return_code != 0:
                return create_success_result(
                    data={
                        "action": "git_merge_abort",
                        "workspace": str(workspace),
                        "message": "当前没有正在进行的 merge 操作",
                    },
                )

            # 执行 merge --abort
            return_code, _, stderr = await self._run_git(
                "merge", "--abort", cwd=self.base_path,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git merge --abort 失败: {stderr}",
                    error_code="MERGE_ABORT_FAILED",
                )

            return create_success_result(
                data={
                    "action": "git_merge_abort",
                    "workspace": str(workspace),
                    "message": "已成功中止 merge 操作",
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"git_merge_abort 操作失败: {str(e)}",
                error_code="MERGE_ABORT_FAILED",
            )

    async def _cleanup(
        self, inputs: dict[str, Any], workspace: Path
    ) -> ToolResult:
        """cleanup 操作：移除 worktree 并删除分支"""

        def _remove_readonly_func(func, path, _):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        try:
            branch_name = self._get_branch_name(workspace)
            is_worktree = await self._is_worktree(workspace)

            if is_worktree:
                return_code, _, stderr = await self._run_git(
                    "worktree", "remove", str(workspace), "--force",
                    cwd=self.base_path,
                )
                if return_code != 0:
                    logger.warning(
                        "[resource_merge] worktree remove 失败: %s, 尝试手动删除", stderr
                    )
                    try:
                        shutil.rmtree(str(workspace), onexc=_remove_readonly_func)
                    except Exception as e:
                        logger.warning("[resource_merge] 手动删除 workspace 失败: %s", e)

                return_code, _, stderr = await self._run_git(
                    "branch", "-D", branch_name,
                    cwd=self.base_path,
                )
                if return_code != 0:
                    logger.warning(
                        "[resource_merge] branch delete 失败: %s", stderr
                    )
            elif workspace.exists():
                git_dir = workspace / ".git"
                if git_dir.exists():
                    shutil.rmtree(str(git_dir), onexc=_remove_readonly_func)

            return create_success_result(
                data={
                    "action": "cleanup",
                    "workspace": str(workspace),
                    "branch_name": branch_name,
                    "message": "已清理 worktree 和分支" if is_worktree else "无需清理",
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"cleanup 操作失败: {str(e)}",
                error_code="CLEANUP_FAILED",
            )
