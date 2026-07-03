"""
逆操作器实现

提供各类操作的逆操作执行能力
"""

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.rollback.models import OperationLog, OperationType

logger = logging.getLogger(__name__)


class BaseReverser(ABC):
    """逆操作器基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """逆操作器名称"""

    @property
    @abstractmethod
    def supported_tools(self) -> list:
        """支持的工具列表"""

    @abstractmethod
    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """
        执行逆操作

        Args:
            operation: 操作日志

        Returns:
            逆操作结果 {"success": bool, "message": str, "details": dict}
        """

    def can_reverse(self, operation: OperationLog) -> bool:
        """
        检查是否可以执行逆操作

        Args:
            operation: 操作日志

        Returns:
            是否可逆
        """
        return operation.reversible and operation.tool_name in self.supported_tools


class FileReverser(BaseReverser):
    """文件操作逆操作器"""

    @property
    def name(self) -> str:
        return "file_reverser"

    @property
    def supported_tools(self) -> list:
        return [
            "file_read",
            "file_write",
            "file_create",
            "file_delete",
            "file_update",
        ]

    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """执行文件操作的逆操作"""
        try:
            op_type = operation.operation_type
            target = operation.target
            before_state = operation.before_state or {}

            if op_type == OperationType.CREATE:
                # 创建的逆操作：删除文件
                return await self._reverse_create(target)

            if op_type == OperationType.UPDATE:
                # 更新的逆操作：恢复原内容
                return await self._reverse_update(target, before_state)

            if op_type == OperationType.DELETE:
                # 删除的逆操作：恢复文件
                return await self._reverse_delete(target, before_state)

            return {
                "success": False,
                "message": f"不支持的操作类型: {op_type}",
                "details": {},
            }

        except Exception as e:
            logger.error(f"文件逆操作失败: {e}")
            return {
                "success": False,
                "message": f"逆操作执行失败: {str(e)}",
                "details": {"error": str(e)},
            }

    async def _reverse_create(self, target: str) -> dict[str, Any]:
        """逆操作：删除创建的文件"""
        path = Path(target)

        if not path.exists():
            return {
                "success": True,
                "message": f"文件已不存在: {target}",
                "details": {"action": "skip"},
            }

        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

        return {
            "success": True,
            "message": f"已删除: {target}",
            "details": {"action": "delete", "path": target},
        }

    async def _reverse_update(self, target: str, before_state: dict[str, Any]) -> dict[str, Any]:
        """逆操作：恢复文件原内容"""
        path = Path(target)
        content = before_state.get("content")

        if content is None:
            return {
                "success": False,
                "message": "缺少原始内容，无法恢复",
                "details": {"action": "failed"},
            }

        # 确保父目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 恢复内容
        path.write_text(content, encoding="utf-8")

        return {
            "success": True,
            "message": f"已恢复文件内容: {target}",
            "details": {"action": "restore", "path": target},
        }

    async def _reverse_delete(self, target: str, before_state: dict[str, Any]) -> dict[str, Any]:
        """逆操作：恢复删除的文件"""
        path = Path(target)
        content = before_state.get("content")

        if content is None:
            return {
                "success": False,
                "message": "缺少原始内容，无法恢复",
                "details": {"action": "failed"},
            }

        # 确保父目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 恢复文件
        path.write_text(content, encoding="utf-8")

        return {
            "success": True,
            "message": f"已恢复文件: {target}",
            "details": {"action": "restore", "path": target},
        }


class GitReverser(BaseReverser):
    """Git 操作逆操作器"""

    def __init__(self, repo_path: str | None = None):
        """
        初始化 Git 逆操作器

        Args:
            repo_path: Git 仓库路径，默认为当前目录
        """
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()

    @property
    def name(self) -> str:
        return "git_reverser"

    @property
    def supported_tools(self) -> list:
        return ["git_commit", "git_branch", "git_stash", "git_checkout"]

    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """执行 Git 操作的逆操作"""
        try:
            tool_name = operation.tool_name
            before_state = operation.before_state or {}
            params = operation.params or {}

            if tool_name == "git_commit":
                return await self._reverse_commit(before_state)

            if tool_name == "git_branch":
                return await self._reverse_branch(params)

            if tool_name == "git_stash":
                return await self._reverse_stash(before_state)

            return {
                "success": False,
                "message": f"不支持的 Git 操作: {tool_name}",
                "details": {},
            }

        except Exception as e:
            logger.error(f"Git 逆操作失败: {e}")
            return {
                "success": False,
                "message": f"Git 逆操作执行失败: {str(e)}",
                "details": {"error": str(e)},
            }

    async def _run_git_command(self, *args: str) -> tuple:
        """
        执行 Git 命令

        Returns:
            (returncode, stdout, stderr)
        """
        cmd = ["git"] + list(args)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(), stderr.decode()

    async def _reverse_commit(self, before_state: dict[str, Any]) -> dict[str, Any]:
        """逆操作：回退 commit"""
        commit_hash = before_state.get("commit_hash")

        if commit_hash:
            # 回退到指定 commit
            returncode, stdout, stderr = await self._run_git_command("reset", "--soft", commit_hash)
        else:
            # 回退最近一次 commit
            returncode, stdout, stderr = await self._run_git_command("reset", "--soft", "HEAD~1")

        if returncode != 0:
            return {
                "success": False,
                "message": f"Git reset 失败: {stderr}",
                "details": {"stderr": stderr},
            }

        return {
            "success": True,
            "message": "已回退 commit",
            "details": {"action": "reset", "target": commit_hash or "HEAD~1"},
        }

    async def _reverse_branch(self, params: dict[str, Any]) -> dict[str, Any]:
        """逆操作：删除创建的分支"""
        branch_name = params.get("branch_name")

        if not branch_name:
            return {
                "success": False,
                "message": "缺少分支名称",
                "details": {},
            }

        # 删除分支
        returncode, stdout, stderr = await self._run_git_command("branch", "-D", branch_name)

        if returncode != 0:
            return {
                "success": False,
                "message": f"删除分支失败: {stderr}",
                "details": {"stderr": stderr},
            }

        return {
            "success": True,
            "message": f"已删除分支: {branch_name}",
            "details": {"action": "delete_branch", "branch": branch_name},
        }

    async def _reverse_stash(self, before_state: dict[str, Any]) -> dict[str, Any]:
        """逆操作：恢复 stash"""
        stash_index = before_state.get("stash_index", 0)

        # 应用 stash
        returncode, stdout, stderr = await self._run_git_command("stash", "pop", f"stash@{{{stash_index}}}")

        if returncode != 0:
            return {
                "success": False,
                "message": f"恢复 stash 失败: {stderr}",
                "details": {"stderr": stderr},
            }

        return {
            "success": True,
            "message": "已恢复 stash",
            "details": {"action": "stash_pop", "index": stash_index},
        }


class APIReverser(BaseReverser):
    """API 操作逆操作器"""

    @property
    def name(self) -> str:
        return "api_reverser"

    @property
    def supported_tools(self) -> list:
        return ["api_create", "api_update", "api_delete", "http_request"]

    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """执行 API 操作的逆操作"""
        # API 逆操作通常需要调用对应的删除/恢复接口
        # 这里提供基础框架，具体实现依赖于 reverse_action 定义

        reverse_action = operation.reverse_action

        if not reverse_action:
            return {
                "success": False,
                "message": "缺少逆操作定义",
                "details": {},
            }

        # 根据 reverse_action 执行逆操作
        action_type = reverse_action.get("type")

        if action_type == "http":
            return await self._execute_http_reverse(reverse_action)
        return {
            "success": False,
            "message": f"不支持的逆操作类型: {action_type}",
            "details": {},
        }

    async def _execute_http_reverse(self, reverse_action: dict[str, Any]) -> dict[str, Any]:
        """执行 HTTP 逆操作"""
        import aiohttp  # noqa: PLC0415

        method = reverse_action.get("method", "DELETE")
        url = reverse_action.get("url")
        headers = reverse_action.get("headers", {})
        body = reverse_action.get("body")

        if not url:
            return {
                "success": False,
                "message": "缺少 URL",
                "details": {},
            }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.request(method, url, headers=headers, json=body) as response,
            ):
                if response.status < 400:
                    return {
                        "success": True,
                        "message": f"API 逆操作成功: {method} {url}",
                        "details": {"status": response.status},
                    }
                return {
                    "success": False,
                    "message": f"API 逆操作失败: {response.status}",
                    "details": {"status": response.status},
                }
        except Exception as e:
            return {
                "success": False,
                "message": f"HTTP 请求失败: {str(e)}",
                "details": {"error": str(e)},
            }


class ReverserRegistry:
    """逆操作器注册表"""

    def __init__(self):
        self._reversers: dict[str, BaseReverser] = {}
        self._tool_mapping: dict[str, str] = {}  # tool_name -> reverser_name

        # 注册默认逆操作器
        self._register_defaults()

    def _register_defaults(self):
        """注册默认逆操作器"""
        self.register(FileReverser())
        self.register(GitReverser())
        self.register(APIReverser())

    def register(self, reverser: BaseReverser) -> None:
        """
        注册逆操作器

        Args:
            reverser: 逆操作器实例
        """
        self._reversers[reverser.name] = reverser

        # 建立工具到逆操作器的映射
        for tool_name in reverser.supported_tools:
            self._tool_mapping[tool_name] = reverser.name

    def get_reverser(self, tool_name: str) -> BaseReverser | None:
        """
        获取工具对应的逆操作器

        Args:
            tool_name: 工具名称

        Returns:
            逆操作器实例，不存在返回 None
        """
        reverser_name = self._tool_mapping.get(tool_name)
        if reverser_name:
            return self._reversers.get(reverser_name)
        return None

    def get_reverser_by_name(self, name: str) -> BaseReverser | None:
        """
        按名称获取逆操作器

        Args:
            name: 逆操作器名称

        Returns:
            逆操作器实例
        """
        return self._reversers.get(name)

    def list_reversers(self) -> list:
        """列出所有逆操作器"""
        return list(self._reversers.values())

    def is_tool_reversible(self, tool_name: str) -> bool:
        """
        检查工具是否支持逆操作

        Args:
            tool_name: 工具名称

        Returns:
            是否支持逆操作
        """
        return tool_name in self._tool_mapping


# 全局逆操作器注册表
_global_reverser_registry: ReverserRegistry | None = None


def get_reverser_registry() -> ReverserRegistry:
    """获取全局逆操作器注册表"""
    global _global_reverser_registry  # noqa: PLW0603
    if _global_reverser_registry is None:
        _global_reverser_registry = ReverserRegistry()
    return _global_reverser_registry
