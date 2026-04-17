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
            description="基于 git worktree 的资源合并与回滚工具。"
            "适用场景：为任务创建隔离的 worktree 工作空间（含完整项目代码）、"
            "将工作空间中的变更合并到项目目录、回滚工作空间中的变更、查看文件变更历史。"
            "不适用场景：仅读取文件（使用 file_read）、执行命令（使用 bash_execute）。"
            "注意事项：需要系统已安装 git且项目为 git 仓库；"
            "workspace 目录需要有读写权限；cleanup 会移除 worktree 和分支且不可逆。",
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
        """merge 操作：将 workspace 中的变更合并到目标目录

        1. 通过 git diff 获取 worktree 分支相对于 base_commit 的变更文件
        2. 将 workspace 中的变更文件复制到 target_dir
        3. 生成变更报告（新增、修改、删除）

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

            if not await self._is_worktree(workspace):
                return create_failure_result(
                    error="workspace 未初始化，请先执行 prepare",
                    error_code="NOT_INITIALIZED",
                )

            base_commit = inputs.get("checkpoint_id")
            if not base_commit:
                base_commit = inputs.get("base_commit")

            changed_files = []
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

            return create_success_result(
                data={
                    "action": "merge",
                    "workspace": str(workspace),
                    "target_dir": str(target_dir),
                    "merged_files": merged_files,
                    "change_report": change_report,
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"merge 操作失败: {str(e)}",
                error_code="MERGE_FAILED",
            )

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
