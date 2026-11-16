"""
逆操作器实现

提供各类操作的逆操作执行能力。

路由依据 = OperationLog 自带数据的能力判定（operation_type + before_state/
reverse_action 载荷），不依赖工具名清单：工具名表是与 manifests 活集合
漂移的平行真相，一旦漂移真实工具名将永远查不中。
"""

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from models import OperationLog, OperationType

logger = logging.getLogger(__name__)


class BaseReverser(ABC):
    """逆操作器基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """逆操作器名称"""

    @abstractmethod
    def supports_operation(self, operation: OperationLog) -> bool:
        """
        能力判定：本逆操作器能否逆该操作

        判定依据是 OperationLog 自带数据（operation_type / before_state /
        params / reverse_action），不是工具名——路由依据与 reverse() 执行
        依据同源，新工具无需登记即自动可路由。

        Args:
            operation: 操作日志

        Returns:
            是否支持该操作
        """

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
        return operation.reversible and self.supports_operation(operation)


class FileReverser(BaseReverser):
    """文件操作逆操作器"""

    # 文件态变更语义：这三类操作可用 before_state 快照执行状态逆操作
    _MUTATION_OPERATION_TYPES = frozenset(
        {OperationType.CREATE, OperationType.UPDATE, OperationType.DELETE}
    )

    @property
    def name(self) -> str:
        return "file_reverser"

    def supports_operation(self, operation: OperationLog) -> bool:
        """文件态变更（CREATE/UPDATE/DELETE）且未携带自定义逆操作定义。

        带 reverse_action 的操作由 APIReverser 按显式定义执行，不在此认领。
        """
        return (
            operation.reverse_action is None
            and operation.operation_type in self._MUTATION_OPERATION_TYPES
        )

    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """执行文件操作的逆操作"""
        try:
            op_type = operation.operation_type
            target = operation.target
            before_state = operation.before_state or {}

            if op_type == OperationType.CREATE:
                # 创建的逆操作：删除文件（目录保护：仅删本操作创建的）
                return await self._reverse_create(target, before_state)

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

    async def _reverse_create(self, target: str, before_state: dict[str, Any]) -> dict[str, Any]:
        """逆操作：删除创建的文件。

        F-GITREV-1 目录保护：仅删除「本操作创建的对象」，防爆炸半径——
        - 目录在操作前已存在（before_state.exists=True）→ 拒绝（不是本操作创建的）；
        - 非空目录且无 before_state → 拒绝（可能已混入无关文件）；
        - 空目录 / before_state.exists=False（本操作创建）→ 允许删除。
        """
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
            if before_state.get("exists") is True:
                return {
                    "success": False,
                    "message": f"目录在操作前已存在，拒绝删除: {target}",
                    "details": {"action": "refused"},
                }
            if not before_state:
                try:
                    next(path.iterdir())
                    return {
                        "success": False,
                        "message": f"非空目录且无 before_state，拒绝删除: {target}",
                        "details": {"action": "refused"},
                    }
                except StopIteration:
                    pass
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

    def supports_operation(self, operation: OperationLog) -> bool:
        """携带 git 逆操作所需载荷（commit_hash/stash_index/branch_name）即认领。

        git 逆操作的三种形态各有明确载荷位：commit_hash/stash_index 在
        before_state，branch_name 在 params——载荷即能力声明，与工具名无关。
        """
        before_state = operation.before_state or {}
        params = operation.params or {}
        return (
            operation.reverse_action is None
            and (
                "commit_hash" in before_state
                or "stash_index" in before_state
                or "branch_name" in params
            )
        )

    async def reverse(self, operation: OperationLog) -> dict[str, Any]:
        """执行 Git 操作的逆操作（按逆操作载荷分发）"""
        try:
            before_state = operation.before_state or {}
            params = operation.params or {}

            if "commit_hash" in before_state:
                return await self._reverse_commit(before_state)

            if "branch_name" in params:
                return await self._reverse_branch(params)

            if "stash_index" in before_state:
                return await self._reverse_stash(before_state)

            return {
                "success": False,
                "message": f"不支持的 Git 操作: {operation.tool_name}",
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
        """逆操作：精确回退到记录点 commit（业界成熟恢复点语义）。

        F-GITREV-1：
        - commit_hash 缺失 → 失败不静默（严禁 HEAD~1 盲目回退——有后续 commit 时会
          回退错误对象，连续回滚会退多个）；
        - 记录点须存在（rev-parse --verify）；
        - HEAD 不得早于记录点（merge-base --is-ancestor，防复活历史）。
        """
        commit_hash = before_state.get("commit_hash")

        if not commit_hash:
            return {
                "success": False,
                "message": "缺少 commit_hash，拒绝回退（不静默回退 HEAD~1）",
                "details": {"action": "failed"},
            }

        # 校验记录点存在
        rc, _, _ = await self._run_git_command("rev-parse", "--verify", f"{commit_hash}^{{commit}}")
        if rc != 0:
            return {
                "success": False,
                "message": f"记录点 commit 不存在: {commit_hash}",
                "details": {"action": "failed"},
            }

        # 校验 HEAD 不早于记录点（防复活历史）
        rc, _, _ = await self._run_git_command("merge-base", "--is-ancestor", commit_hash, "HEAD")
        if rc != 0:
            return {
                "success": False,
                "message": f"HEAD 早于记录点，拒绝回滚: {commit_hash}",
                "details": {"action": "refused"},
            }

        returncode, stdout, stderr = await self._run_git_command("reset", "--soft", commit_hash)

        if returncode != 0:
            return {
                "success": False,
                "message": f"Git reset 失败: {stderr}",
                "details": {"stderr": stderr},
            }

        return {
            "success": True,
            "message": "已回退 commit",
            "details": {"action": "reset", "target": commit_hash},
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

    def supports_operation(self, operation: OperationLog) -> bool:
        """携带自定义逆操作定义（reverse_action）即认领——定义即能力声明。"""
        return operation.reverse_action is not None

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
    """
    逆操作器注册表

    能力链路由：按注册顺序依次问询各逆操作器的 can_reverse，首个认领者
    胜出。默认注册顺序即优先级：显式逆操作定义（API）> git 逆操作载荷 >
    通用文件态操作——specific 者在前，catch-all 在后。
    """

    def __init__(self):
        self._reversers: dict[str, BaseReverser] = {}

        # 注册默认逆操作器
        self._register_defaults()

    def _register_defaults(self):
        """注册默认逆操作器（注册顺序 = 路由优先级）"""
        self.register(APIReverser())
        self.register(GitReverser())
        self.register(FileReverser())

    def register(self, reverser: BaseReverser) -> None:
        """
        注册逆操作器（同名覆盖，新注册者排在链尾）

        Args:
            reverser: 逆操作器实例
        """
        self._reversers[reverser.name] = reverser

    def get_reverser(self, operation: OperationLog) -> BaseReverser | None:
        """
        按操作能力获取逆操作器

        Args:
            operation: 操作日志

        Returns:
            首个认领该操作的逆操作器实例，无人认领返回 None
        """
        for reverser in self._reversers.values():
            if reverser.can_reverse(operation):
                return reverser
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


# 全局逆操作器注册表
_global_reverser_registry: ReverserRegistry | None = None


def get_reverser_registry() -> ReverserRegistry:
    """获取全局逆操作器注册表"""
    global _global_reverser_registry  # noqa: PLW0603
    if _global_reverser_registry is None:
        _global_reverser_registry = ReverserRegistry()
    return _global_reverser_registry
