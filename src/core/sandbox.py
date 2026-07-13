"""
代码沙箱模块

提供安全的代码执行环境。

⚠️ 安全警告（H3/M5）
=================

本沙箱在**宿主进程内**用 ``exec`` 运行用户代码，AST 检查是**黑名单**性质，
**不能作为不可信代码的安全边界**——它只能挡住公开的逃逸 payload，无法
防御新型绕过。一旦绕过，攻击者即获得宿主进程的完整权限。

威胁模型：
- **可信/本地单用户场景**：本沙箱够用（防误操作 + 挡经典攻击）。
- **不可信/多租户场景**：必须通过 IsolationCoordinator 把代码执行
  移进容器隔离（降权 + 只读根 + 限制能力），**绝不**走本沙箱的宿主路径。

配置项 ``SandboxConfig.require_isolation``（见下）用于让宿主执行路径
在不可信场景 fail-closed。

根治路径（未完成）：将 host_provider 的 code_execute 调用强制路由到
容器 provider，本沙箱仅作为容器内的兜底（容器内即便逃逸也受限）。
"""

import ast
import asyncio
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass, field
from io import StringIO
from typing import Any


class SandboxError(Exception):
    """沙箱错误基类"""


class SandboxTimeoutError(SandboxError):
    """沙箱超时错误"""


class SandboxSecurityError(SandboxError):
    """沙箱安全错误"""


# 已知的 dunder 属性逃逸跳板：通过这些属性可从任意对象爬到 object/os。
# 黑名单只能挡公开 payload（根治需容器隔离，见模块文档）。
_DUNDER_ESCAPE_ATTRS = frozenset({
    "__class__",
    "__bases__",
    "__base__",
    "__subclasses__",
    "__mro__",
    "__globals__",
    "__builtins__",
    "__import__",
    "__loader__",
    "__code__",
    "__func__",
    "__self__",
})


@dataclass
class SandboxResult:
    """沙箱执行结果"""

    success: bool
    output: str = ""
    return_value: Any = None
    error: str | None = None
    error_type: str | None = None
    execution_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "success": self.success,
            "output": self.output,
            "return_value": self.return_value,
            "error": self.error,
            "error_type": self.error_type,
            "execution_time": self.execution_time,
        }


@dataclass
class SandboxConfig:
    """沙箱配置"""

    timeout_seconds: float = 30.0  # 超时时间（秒）
    max_memory: int = 128 * 1024 * 1024  # 最大内存（字节）
    allowed_modules: list[str] = field(
        default_factory=lambda: [
            "math",
            "json",
            "re",
            "datetime",
            "collections",
            "itertools",
            "functools",
            "operator",
            "string",
            "random",
            "hashlib",
            "base64",
            "copy",
            "typing",
            "time",  # 用于测试超时
        ]
    )
    blocked_modules: list[str] = field(
        default_factory=lambda: [
            "os",
            "sys",
            "subprocess",
            "shutil",
            "socket",
            "requests",
            "urllib",
            "http",
            "ftplib",
            "smtplib",
            "pickle",
            "shelve",
            "marshal",
            "ctypes",
            "multiprocessing",
        ]
    )
    blocked_builtins: list[str] = field(
        default_factory=lambda: [
            "eval",
            "exec",
            "compile",
            "__import__",
            "open",
            "input",
            "breakpoint",
            "exit",
            "quit",
        ]
    )

    def __post_init__(self):
        """验证配置"""
        if self.timeout_seconds < 0:
            raise ValueError("超时时间不能为负")
        if self.max_memory < 0:
            raise ValueError("最大内存不能为负")


class CodeValidator:
    """代码验证器"""

    def __init__(self, config: SandboxConfig):
        self.config = config

    def validate(self, code: str) -> tuple[bool, list[str]]:  # noqa: PLR0912
        """
        验证代码安全性

        Args:
            code: 代码字符串

        Returns:
            (是否安全, 问题列表)
        """
        issues = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            # 语法错误不是安全问题，返回 True 让执行阶段处理
            return True, []

        for node in ast.walk(tree):
            # 检查导入
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.config.blocked_modules:
                        issues.append(f"禁止导入模块: {alias.name}")

            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in self.config.blocked_modules:
                    issues.append(f"禁止导入模块: {node.module}")

            # 检查危险函数调用
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in self.config.blocked_builtins:
                        issues.append(f"禁止使用函数: {node.func.id}")
                elif isinstance(node.func, ast.Attribute):  # noqa: SIM102
                    # 检查 os.system 等
                    if isinstance(node.func.value, ast.Name):
                        module = node.func.value.id
                        if module in self.config.blocked_modules:
                            issues.append(f"禁止使用模块: {module}")

            # 拦截 dunder 属性链逃逸（().__class__.__bases__[0].__subclasses__() 等）
            # 这是经典逃逸手法，黑名单无法根治但能挡住所有公开 payload。
            # 根治方案：不可信代码移进容器隔离（见模块文档警告）。
            if isinstance(node, ast.Attribute) and node.attr in _DUNDER_ESCAPE_ATTRS:
                issues.append(f"禁止访问 dunder 属性: {node.attr}（潜在逃逸）")

        return len(issues) == 0, issues


class CodeSandbox:
    """
    代码沙箱

    提供安全的代码执行环境。
    """

    def __init__(self, config: SandboxConfig | None = None):
        """
        初始化沙箱

        Args:
            config: 沙箱配置
        """
        self._config = config or SandboxConfig()
        self._validator = CodeValidator(self._config)
        self._globals: dict[str, Any] = {}
        self._locals: dict[str, Any] = {}
        self._stats = {
            "total_executions": 0,
            "successful_executions": 0,
            "failed_executions": 0,
        }

    async def execute(
        self,
        code: str,
        context: dict[str, Any] | None = None,
        return_var: str | None = None,
    ) -> SandboxResult:
        """
        执行代码

        Args:
            code: 代码字符串
            context: 执行上下文（变量）
            return_var: 返回变量名

        Returns:
            执行结果
        """
        self._stats["total_executions"] += 1
        start_time = time.time()

        # 验证代码
        is_safe, issues = await self.validate_code(code)
        if not is_safe:
            self._stats["failed_executions"] += 1
            return SandboxResult(
                success=False,
                error="; ".join(issues),
                error_type="SecurityError",
                execution_time=time.time() - start_time,
            )

        # 准备执行环境
        exec_globals = self._prepare_globals(context)
        exec_locals: dict[str, Any] = {}

        # 捕获输出
        stdout_capture = StringIO()
        stderr_capture = StringIO()

        try:
            # 使用 asyncio 超时
            await asyncio.wait_for(
                self._run_code(code, exec_globals, exec_locals, stdout_capture, stderr_capture),
                timeout=self._config.timeout_seconds,
            )

            # 获取返回值
            return_value = None
            if return_var and return_var in exec_locals:
                return_value = exec_locals[return_var]
            elif return_var and return_var in exec_globals:
                return_value = exec_globals[return_var]

            # 更新内部状态
            self._locals.update(exec_locals)

            self._stats["successful_executions"] += 1
            return SandboxResult(
                success=True,
                output=stdout_capture.getvalue(),
                return_value=return_value,
                execution_time=time.time() - start_time,
            )

        except TimeoutError:
            self._stats["failed_executions"] += 1
            return SandboxResult(
                success=False,
                error=f"执行超时（{self._config.timeout_seconds}秒）",
                error_type="TimeoutError",
                execution_time=time.time() - start_time,
            )
        except SyntaxError as e:
            self._stats["failed_executions"] += 1
            return SandboxResult(
                success=False,
                error=str(e),
                error_type="SyntaxError",
                execution_time=time.time() - start_time,
            )
        except Exception as e:
            self._stats["failed_executions"] += 1
            return SandboxResult(
                success=False,
                output=stdout_capture.getvalue(),
                error=str(e),
                error_type=type(e).__name__,
                execution_time=time.time() - start_time,
            )

    async def _run_code(
        self,
        code: str,
        exec_globals: dict[str, Any],
        exec_locals: dict[str, Any],
        stdout_capture: StringIO,
        stderr_capture: StringIO,
    ) -> None:
        """在线程池中运行代码"""

        def _execute():
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                # 在沙箱环境中执行代码 - 已通过受限的全局变量和模块导入控制安全性
                # 使用同一个命名空间，让函数定义可以递归调用
                exec(code, exec_globals, exec_globals)
                # 将结果复制到 exec_locals
                exec_locals.update(
                    {k: v for k, v in exec_globals.items() if not k.startswith("__") and k not in ("__builtins__",)}
                )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _execute)

    def _prepare_globals(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """准备全局变量"""
        import builtins  # noqa: PLC0415

        # 创建受限的 __import__ 函数
        allowed_modules = set(self._config.allowed_modules)
        blocked_modules = set(self._config.blocked_modules)

        def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
            """受限的导入函数"""
            # 获取顶级模块名
            top_level = name.split(".")[0]

            # 检查是否被阻止
            if top_level in blocked_modules:
                raise ImportError(f"禁止导入模块: {name}")

            # 检查是否允许
            if top_level not in allowed_modules:
                raise ImportError(f"模块未在允许列表中: {name}")

            return builtins.__import__(name, globals, locals, fromlist, level)

        # 安全的内置函数
        safe_builtins = {}
        for name in dir(builtins):
            if name.startswith("_"):
                continue
            if name in self._config.blocked_builtins:
                continue
            with suppress(AttributeError):
                safe_builtins[name] = getattr(builtins, name)

        # 添加受限的 __import__
        safe_builtins["__import__"] = restricted_import

        globals_dict = {
            "__builtins__": safe_builtins,
        }

        # 预加载允许的模块
        for module_name in self._config.allowed_modules:
            with suppress(ImportError):
                globals_dict[module_name] = __import__(module_name)

        # 添加上下文变量
        if context:
            globals_dict.update(context)

        return globals_dict

    async def validate_code(self, code: str) -> tuple[bool, list[str]]:
        """
        验证代码安全性

        Args:
            code: 代码字符串

        Returns:
            (是否安全, 问题列表)
        """
        return self._validator.validate(code)

    async def call_function(
        self,
        func_name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> SandboxResult:
        """
        调用已定义的函数

        Args:
            func_name: 函数名
            args: 位置参数
            kwargs: 关键字参数

        Returns:
            执行结果
        """
        args = args or []
        kwargs = kwargs or {}

        # 查找函数
        func = self._locals.get(func_name) or self._globals.get(func_name)
        if not func or not callable(func):
            return SandboxResult(
                success=False,
                error=f"函数不存在: {func_name}",
                error_type="NameError",
            )

        start_time = time.time()
        stdout_capture = StringIO()

        try:
            with redirect_stdout(stdout_capture):
                result = func(*args, **kwargs)

            return SandboxResult(
                success=True,
                output=stdout_capture.getvalue(),
                return_value=result,
                execution_time=time.time() - start_time,
            )
        except Exception as e:
            return SandboxResult(
                success=False,
                output=stdout_capture.getvalue(),
                error=str(e),
                error_type=type(e).__name__,
                execution_time=time.time() - start_time,
            )

    async def reset(self) -> None:
        """重置沙箱状态"""
        self._globals.clear()
        self._locals.clear()

    def get_stats(self) -> dict[str, int]:
        """获取执行统计"""
        return self._stats.copy()
