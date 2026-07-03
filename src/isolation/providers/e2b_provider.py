"""
E2B MicroVM 隔离提供者

使用 Python subprocess 实现轻量级代码沙箱
"""

import asyncio
import logging
import os
import platform
import tempfile
from datetime import UTC, datetime
from typing import Any

from src.isolation.providers.base import IsolationProvider
from src.isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)

logger = logging.getLogger(__name__)


class E2BProvider(IsolationProvider):
    """E2B MicroVM 隔离提供者

    使用 Python subprocess 实现轻量级代码沙箱。
    注意：这是简化的实现，使用进程隔离代替真正的 MicroVM。
    """

    def __init__(
        self,
        template: str = "base-python",
        timeout: int = 30,
        memory_limit: str = "512m",
    ):
        """初始化 E2B 提供者

        Args:
            template: 沙箱模板名称（保留用于兼容性）
            timeout: 默认超时时间（秒）
            memory_limit: 内存限制（保留用于兼容性）
        """
        self._template = template
        self._timeout = timeout
        self._memory_limit = memory_limit
        self._environments: dict[str, IsolationEnvironment] = {}

    def get_level(self) -> IsolationLevel:
        """获取隔离级别"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查沙箱是否可用

        Returns:
            (是否可用, 不可用原因)
        """
        # 检查 Python 是否可用
        try:
            process = await asyncio.create_subprocess_exec(
                "python",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()

            if process.returncode != 0:
                return False, "Python 不可用"

            return True, None

        except Exception as e:
            return False, f"沙箱不可用: {str(e)}"

    async def create_environment(self, context: IsolationContext) -> IsolationEnvironment:
        """创建沙箱环境

        Args:
            context: 隔离上下文

        Returns:
            隔离环境

        Raises:
            RuntimeError: 如果沙箱不可用
        """
        # 检查可用性
        available, error = await self.is_available()
        if not available:
            raise RuntimeError(f"沙箱不可用: {error}")

        now = datetime.now(UTC)
        sandbox_id = f"e2b-{context.task_id}"

        # 创建临时工作目录
        work_dir = tempfile.mkdtemp(prefix=f"sandbox_{context.task_id}_")

        env = IsolationEnvironment(
            env_id=sandbox_id,
            level=IsolationLevel.CONTAINER,
            provider_type="e2b",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "sandbox_id": sandbox_id,
                "template": self._template,
                "work_dir": work_dir,
                "timeout": self._timeout,
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )

        self._environments[sandbox_id] = env
        logger.info(f"创建沙箱环境: {sandbox_id} (工作目录: {work_dir})")

        return env

    async def destroy_environment(self, env_id: str) -> None:
        """销毁沙箱

        Args:
            env_id: 环境 ID
        """
        try:
            env = self._environments.get(env_id)
            if env and "work_dir" in env.provider_info:
                work_dir = env.provider_info["work_dir"]

                # 清理临时目录
                try:
                    import shutil  # noqa: PLC0415

                    if os.path.exists(work_dir):  # noqa: PTH110
                        shutil.rmtree(work_dir)
                        logger.info(f"清理沙箱工作目录: {work_dir}")
                except Exception as e:
                    logger.warning(f"清理工作目录失败: {e}")

            self._environments.pop(env_id, None)

        except Exception as e:
            logger.error(f"销毁环境失败: {e}", exc_info=True)

    async def execute_in_environment(self, env_id: str, operation: dict[str, Any]) -> ExecutionResult:
        """在沙箱中执行操作

        Args:
            env_id: 环境 ID
            operation: 操作定义

        Returns:
            执行结果
        """
        env = self._environments.get(env_id)
        if not env:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"环境不存在: {env_id}",
            )

        try:
            op_type = operation.get("type")

            if op_type == "python_code":
                return await self._execute_python_code(env, operation)
            if op_type == "command":
                return await self._execute_command(env, operation)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"不支持的操作类型: {op_type}",
            )

        except Exception as e:
            logger.error(f"在沙箱中执行操作失败: {e}", exc_info=True)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行失败: {str(e)}",
            )

    async def _execute_python_code(self, env: IsolationEnvironment, operation: dict[str, Any]) -> ExecutionResult:
        """执行 Python 代码

        Args:
            env: 隔离环境
            operation: 操作定义

        Returns:
            执行结果
        """
        code = operation.get("code")
        if not code:
            return ExecutionResult(
                success=False,
                output=None,
                error="代码不能为空",
            )

        timeout = operation.get("timeout", self._timeout)
        work_dir = env.provider_info.get("work_dir", "")

        try:
            # 将代码写入临时文件
            code_file = os.path.join(work_dir, "code.py")
            with open(code_file, "w", encoding="utf-8") as f:
                f.write(code)

            # 构建执行命令
            python_cmd = "python" if platform.system() == "Windows" else "python3"

            command = [python_cmd, code_file]

            # 执行代码
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )

                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                success = process.returncode == 0

                return ExecutionResult(
                    success=success,
                    output={
                        "output": stdout_text,
                        "stderr": stderr_text,
                        "return_code": process.returncode,
                    },
                    error=None if success else stderr_text,
                )

            except TimeoutError:
                # 超时，终止进程
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

                return ExecutionResult(
                    success=False,
                    output=None,
                    error=f"代码执行超时（{timeout}秒）",
                )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行代码失败: {str(e)}",
            )

    async def _execute_command(self, env: IsolationEnvironment, operation: dict[str, Any]) -> ExecutionResult:
        """执行命令

        Args:
            env: 隔离环境
            operation: 操作定义

        Returns:
            执行结果
        """
        command = operation.get("command")
        if not command:
            return ExecutionResult(
                success=False,
                output=None,
                error="命令不能为空",
            )

        timeout = operation.get("timeout", self._timeout)
        work_dir = env.provider_info.get("work_dir", "")

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
                cwd=work_dir,
            )

            try:
                # 等待完成，带超时
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )

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
                # 超时，终止进程
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

                return ExecutionResult(
                    success=False,
                    output=None,
                    error=f"命令执行超时（{timeout}秒）",
                )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行命令失败: {str(e)}",
            )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取沙箱状态

        Args:
            env_id: 环境 ID

        Returns:
            沙箱状态
        """
        env = self._environments.get(env_id)
        if not env:
            return EnvironmentStatus.STOPPED

        # 检查工作目录是否存在
        work_dir = env.provider_info.get("work_dir")
        if work_dir and os.path.exists(work_dir):  # noqa: PTH110
            return EnvironmentStatus(env.status)
        return EnvironmentStatus.STOPPED
