"""Git 操作辅助模块

为 ResourceMergeTool 提供 Git 操作的共享模块。
包含：命令执行、仓库检查、状态查询、提交、差异查看、日志查看、合并中止等操作。

暴露接口：
- GitHelpers：Git 操作辅助类
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from tools.types import (
    ToolResult,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# git 命令执行超时时间（秒）
GIT_TIMEOUT = 30

__all__ = ["GitHelpers", "GIT_TIMEOUT"]


class GitHelpers:
    """Git 操作辅助类

    封装常用的 Git 异步操作，供 ResourceMergeTool 使用。

    公共方法：
    - run_git: 执行 git 命令
    - is_worktree: 检查 workspace 是否是 git worktree
    - ensure_project_repo: 确保 base_path 是 git 仓库
    - ensure_git_repo: 确保 workspace 目录存在且已初始化 git 仓库
    - git_status: 查看 workspace 的 git 状态
    - git_commit: 暂存并提交 workspace 中的变更
    - git_diff: 查看 workspace 中的变更
    - git_log: 查看 workspace 的提交历史
    - git_merge_abort: 中止当前正在进行的 git merge 操作
    """

    def __init__(self, base_path: Path) -> None:
        """初始化 Git 辅助类

        Args:
            base_path: 项目根目录路径
        """
        self.base_path = base_path

    async def run_git(
        self,
        *args: str,
        cwd: Path,
        timeout: int = GIT_TIMEOUT,
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

    async def is_worktree(self, workspace: Path) -> bool:
        """检查 workspace 是否是项目的 git worktree

        Args:
            workspace: workspace 目录路径

        Returns:
            是否是 worktree
        """
        git_file = workspace / ".git"
        if not git_file.exists():
            return False
        return bool(git_file.is_file())

    async def ensure_project_repo(self) -> ToolResult | None:
        """确保 base_path 是一个 git 仓库

        Returns:
            如果不是 git 仓库返回失败结果，否则返回 None
        """
        return_code, _, _ = await self.run_git(
            "rev-parse",
            "--git-dir",
            cwd=self.base_path,
        )
        if return_code != 0:
            return create_failure_result(
                error=f"项目目录不是 git 仓库: {self.base_path}",
                error_code="NOT_A_GIT_REPO",
            )
        return None

    async def ensure_git_repo(self, workspace: Path) -> ToolResult | None:
        """确保 workspace 目录存在且已初始化 git 仓库

        如果 workspace 是 worktree 则直接返回，否则执行 git init。

        Args:
            workspace: workspace 目录路径

        Returns:
            如果出错返回失败结果，成功返回 None
        """
        if await self.is_worktree(workspace):
            return None

        workspace.mkdir(parents=True, exist_ok=True)

        git_dir = workspace / ".git"
        if not git_dir.exists():
            return_code, stdout, stderr = await self.run_git(
                "init",
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git init 失败: {stderr}",
                    error_code="GIT_INIT_FAILED",
                )

        return None

    async def git_status(
        self,
        inputs: dict[str, Any],
        workspace: Path,
    ) -> ToolResult:
        """git_status 操作：查看 workspace 的 git 状态

        Args:
            inputs: 工具输入参数（此操作不使用额外参数）
            workspace: workspace 目录路径

        Returns:
            包含 staged/unstaged/untracked 文件列表的结果
        """
        try:
            if not await self.is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            return_code, stdout, stderr = await self.run_git(
                "status",
                "--porcelain",
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git status 失败: {stderr}",
                    error_code="GIT_STATUS_FAILED",
                )

            # 解析状态
            status_lines = stdout.splitlines() if stdout else []
            staged: list[str] = []
            unstaged: list[str] = []
            untracked: list[str] = []

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

    async def git_commit(
        self,
        inputs: dict[str, Any],
        workspace: Path,
    ) -> ToolResult:
        """git_commit 操作：暂存并提交 workspace 中的变更

        Args:
            inputs: 工具输入参数，包含 message（提交消息）
            workspace: workspace 目录路径

        Returns:
            包含 commit_hash 的成功结果
        """
        try:
            if not await self.is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            message = inputs.get("message", "chore: update workspace files")

            # 配置 git 用户信息
            await self.run_git(
                "config",
                "user.email",
                "resource-merge@agent.local",
                cwd=workspace,
            )
            await self.run_git(
                "config",
                "user.name",
                "Agent Resource Merge",
                cwd=workspace,
            )

            # 暂存所有变更
            return_code, _, stderr = await self.run_git(
                "add",
                "-A",
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git add 失败: {stderr}",
                    error_code="GIT_ADD_FAILED",
                )

            # 检查是否有变更需要提交
            return_code, status_output, _ = await self.run_git(
                "status",
                "--porcelain",
                cwd=workspace,
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
            return_code, _, stderr = await self.run_git(
                "commit",
                "-m",
                message,
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git commit 失败: {stderr}",
                    error_code="GIT_COMMIT_FAILED",
                )

            # 获取 commit hash
            return_code, commit_hash, _ = await self.run_git(
                "rev-parse",
                "HEAD",
                cwd=workspace,
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

    async def git_diff(
        self,
        inputs: dict[str, Any],
        workspace: Path,
    ) -> ToolResult:
        """git_diff 操作：查看 workspace 中的变更

        Args:
            inputs: 工具输入参数（此操作不使用额外参数）
            workspace: workspace 目录路径

        Returns:
            包含 diff 内容的结果
        """
        try:
            if not await self.is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            # 查看暂存区和工作区的变更
            return_code, stdout, stderr = await self.run_git(
                "diff",
                "HEAD",
                cwd=workspace,
            )
            if return_code != 0:
                # 可能是没有历史 commit，尝试查看暂存区变更
                return_code, stdout, stderr = await self.run_git(
                    "diff",
                    "--cached",
                    cwd=workspace,
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

    async def git_log(
        self,
        inputs: dict[str, Any],
        workspace: Path,
    ) -> ToolResult:
        """git_log 操作：查看 workspace 的提交历史

        Args:
            inputs: 工具输入参数（此操作不使用额外参数）
            workspace: workspace 目录路径

        Returns:
            包含提交历史列表的结果
        """
        try:
            if not await self.is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化",
                    error_code="NOT_INITIALIZED",
                )

            # 获取提交历史（最多 20 条）
            return_code, stdout, stderr = await self.run_git(
                "log",
                "--oneline",
                "--max-count=20",
                "--format=%H|%s|%ai",
                cwd=workspace,
            )
            if return_code != 0:
                return create_failure_result(
                    error=f"git log 失败: {stderr}",
                    error_code="GIT_LOG_FAILED",
                )

            # 解析提交历史
            commits: list[dict[str, str]] = []
            if stdout:
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append(
                            {
                                "hash": parts[0],
                                "message": parts[1],
                                "time": parts[2],
                            }
                        )

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

    async def git_merge_abort(
        self,
        inputs: dict[str, Any],
        workspace: Path,
    ) -> ToolResult:
        """中止当前正在进行的 git merge 操作

        在主仓库（base_path）中执行 git merge --abort，
        撤销所有合并相关的变更，恢复到合并前的状态。

        Args:
            inputs: 工具输入参数（此操作不使用额外参数）
            workspace: workspace 目录路径（保持接口一致）

        Returns:
            中止操作的结果
        """
        try:
            # 确保主仓库是 git 仓库
            error = await self.ensure_project_repo()
            if error:
                return error

            # 检查是否正在合并中
            return_code, stdout, _ = await self.run_git(
                "rev-parse",
                "--verify",
                "MERGE_HEAD",
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
            return_code, _, stderr = await self.run_git(
                "merge",
                "--abort",
                cwd=self.base_path,
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
