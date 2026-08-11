"""
宿主机隔离提供者

暴露接口：
- get_level(self) -> IsolationLevel：get_level功能
- HostProvider：HostProvider类
"""

import asyncio
import logging
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from isolation.providers.base import IsolationProvider
from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)

logger = logging.getLogger(__name__)


class HostProvider(IsolationProvider):
    """宿主机隔离提供者

    在本地直接执行命令和操作，提供：
    - workspace 权限检查：只能写入指定的工作目录

    host 模式不建文件检查点：host 执行本就在编排进程内，工作区由 git 托管
    （评估前由 on_before_evaluate 提交 commit 作回滚锚点），无需额外文件备份。
    旧实现的 checkpoint 会落到进程 CWD 对应的容器根，导致跨容器串台，已移除。

    注意：HOST 模式需要人工审批才能使用。
    """

    def __init__(self, project_root: str = "."):
        """初始化宿主机提供者"""
        self._environments: dict[str, IsolationEnvironment] = {}
        self._project_root = Path(project_root).resolve()

    def get_level(self) -> IsolationLevel:
        """获取隔离级别"""
        return IsolationLevel.HOST

    async def is_available(self) -> tuple[bool, str | None]:
        """检查宿主机提供者是否可用

        宿主机提供者始终可用
        """
        return True, None

    async def create_environment(self, context: IsolationContext) -> IsolationEnvironment:
        """创建虚拟环境"""
        now = datetime.now(UTC)

        env = IsolationEnvironment(
            env_id=f"host-{context.task_id}",
            level=IsolationLevel.HOST,
            provider_type="host",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "platform": platform.system(),
                "platform_release": platform.release(),
                "platform_version": platform.version(),
                "architecture": platform.machine(),
                "hostname": platform.node(),
                "processor": platform.processor(),
                "project_root": str(self._project_root),
                "workspace": context.workspace,
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )

        self._environments[env.env_id] = env
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁虚拟环境

        host 模式不维护文件检查点，无需清理/恢复；工作区回滚由 git 层负责。
        host 无底层容器，销毁恒成功，返回 True。
        """
        env = self._environments.get(env_id)
        if not env:
            return True

        if not success:
            logger.warning(f"[HostProvider] host 任务失败，工作区回滚由 git 层处理 | task_id={env.context.task_id}")

        self._environments.pop(env_id, None)
        return True

    async def execute_in_environment(self, env_id: str, operation: dict[str, Any]) -> ExecutionResult:
        """在宿主机上执行操作"""
        # 获取环境上下文
        env = self._environments.get(env_id)
        context = env.context if env else None

        op_type = operation.get("type")

        if op_type == "command":
            return await self._execute_command(operation, context)
        if op_type == "file_operation":
            return await self._execute_file_op(operation, context)
        if op_type == "python_code":
            return await self._execute_python_code(operation)
        return ExecutionResult(
            success=False,
            output=None,
            error=f"不支持的操作类型: {op_type}",
        )

    async def _execute_command(  # noqa: PLR0911
        self, operation: dict[str, Any], context: IsolationContext | None = None
    ) -> ExecutionResult:
        """执行Shell命令"""
        command = operation.get("command")
        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")

        timeout = operation.get("timeout", 30)
        working_dir = operation.get("working_dir")

        # workspace 权限检查
        if working_dir and context and context.workspace:
            allowed, error = self._check_workspace_permission(working_dir, context.workspace)
            if not allowed:
                return ExecutionResult(success=False, output=None, error=error)

        try:
            # 确定平台特定的执行方式
            is_windows = platform.system() == "Windows"

            # 如果是 Windows，使用 cmd /c
            full_command = f'cmd /c "{command}"' if is_windows else command

            # 创建进程
            process = await asyncio.create_subprocess_shell(
                full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )

            try:
                # 等待完成，带超时
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

                # 解码输出
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                # 检查返回码
                return_code = process.returncode
                success = return_code == 0

                return ExecutionResult(
                    success=success,
                    output={
                        "stdout": stdout_text,
                        "stderr": stderr_text,
                        "return_code": return_code,
                        "command": command,
                    },
                    error=None if success else stderr_text,
                )

            except TimeoutError:
                # 超时，整树杀（防 cargo/rustc 后代变孤儿继续跑）。
                # 旧实现 process.kill() 只杀单进程(cmd/c 壳)，孙子进程被 init
                # 收养继续运行，是 host 路径进程泄漏的根因。改用 LocalProcessBackend
                # 的 psutil 递归整树杀。
                try:
                    from tools.builtin.bash.types import WorkUnit
                    from tools.builtin.bash.process_manager import _get_local_backend

                    await _get_local_backend().kill(
                        WorkUnit(pid=process.pid, command=command), force=True
                    )
                    await process.wait()
                except Exception:
                    pass

                return ExecutionResult(
                    success=False,
                    output=None,
                    error=f"命令执行超时（{timeout}秒）",
                )

        except PermissionError:
            return ExecutionResult(
                success=False,
                output=None,
                error="权限不足，无法执行命令",
            )
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                output=None,
                error="命令或程序不存在",
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行命令失败: {str(e)}",
            )

    async def _execute_file_op(  # noqa: PLR0911
        self, operation: dict[str, Any], context: IsolationContext | None = None
    ) -> ExecutionResult:
        """执行文件操作"""
        op = operation.get("operation")
        path = operation.get("path")

        # workspace 权限检查（写入和删除操作）
        if op in ["write", "delete"] and context and context.workspace:
            allowed, error = self._check_workspace_permission(path, context.workspace)
            if not allowed:
                return ExecutionResult(success=False, output=None, error=error)

        try:
            if op == "read":
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                return ExecutionResult(success=True, output=content)

            if op == "write":
                content = operation.get("content")
                # 确保目录存在
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                # newline="\n"：强制 LF 行尾。Windows 文本模式默认 \n→\r\n，
                # 写出的脚本喂给容器 /bin/sh 会报 "Illegal option -"。
                # 见 tests/tools/builtin/file_write/test_line_endings.py。
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
                return ExecutionResult(success=True, output=None)

            if op == "delete":
                os.remove(path)  # noqa: PTH107
                return ExecutionResult(success=True, output=None)

            if op == "exists":
                exists = os.path.exists(path)  # noqa: PTH110
                return ExecutionResult(success=True, output={"exists": exists})

            return ExecutionResult(
                success=False,
                output=None,
                error=f"不支持的文件操作: {op}",
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"文件操作失败: {str(e)}",
            )

    async def _execute_python_code(self, operation: dict[str, Any]) -> ExecutionResult:
        """执行 Python 代码"""
        try:
            from src.core.sandbox import CodeSandbox  # noqa: PLC0415

            code = operation.get("code")
            context = operation.get("context")
            timeout = operation.get("timeout", 30)

            if not code:
                return ExecutionResult(
                    success=False,
                    output=None,
                    error="代码不能为空",
                )

            # 创建沙箱
            from src.core.sandbox import SandboxConfig  # noqa: PLC0415

            config = SandboxConfig(timeout_seconds=timeout)
            sandbox = CodeSandbox(config)

            # 执行代码
            result = await sandbox.execute(code, context=context)

            return ExecutionResult(
                success=result.success,
                output={
                    "output": result.output,
                    "return_value": result.return_value,
                },
                error=result.error,
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行 Python 代码失败: {str(e)}",
            )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取环境状态"""
        env = self._environments.get(env_id)
        if not env:
            return EnvironmentStatus.STOPPED
        return EnvironmentStatus(env.status)

    def _check_workspace_permission(self, path: str, workspace: str) -> tuple[bool, str | None]:
        """检查路径是否在 workspace 范围内"""
        try:
            # 标准化路径
            abs_path = Path(path).resolve()
            workspace_path = (self._project_root / workspace).resolve()

            # 检查是否在 workspace 内
            try:
                abs_path.relative_to(workspace_path)
                return True, None
            except ValueError:
                pass

            # 不在 workspace 内
            error_msg = (
                f"权限拒绝：路径 '{path}' 不在工作目录 '{workspace}' 内。HOST 模式只能操作指定工作目录下的文件。"
            )
            logger.warning(f"[HostProvider] 权限检查失败 | path={path} | workspace={workspace}")
            return False, error_msg

        except Exception as e:
            error_msg = f"权限检查失败: {str(e)}"
            logger.error(f"[HostProvider] 权限检查异常 | error={e}")
            return False, error_msg
