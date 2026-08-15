"""代码沙箱模块（F-SANDBOX-2 白名单模式）。

⚠️ 非安全边界：本沙箱为轻量代码执行环境，**仅用于已审批 HOST 模式下的受信代码**。
采用白名单模式（allowlist, fail-closed）——只允许明确列出的安全内置与基础语法，
一切未列出项默认拒绝。不可信代码必须走容器隔离，不可依赖本沙箱保证安全。
"""

import ast
import asyncio
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

# F-SANDBOX-2 白名单模式（allowlist, fail-closed）：仅允许下列纯函数内置，
# 一切未列出项默认拒绝。本沙箱非安全边界，仅用于已审批 HOST 模式受信代码；
# 不可信代码必须走容器隔离。
ALLOWED_BUILTINS: frozenset[str] = frozenset(
    {
        "abs", "all", "any", "bool", "bytes", "dict", "divmod", "enumerate",
        "filter", "float", "format", "frozenset", "int", "isinstance", "iter",
        "len", "list", "map", "max", "min", "next", "pow", "range", "repr",
        "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
    }
)


class SandboxError(Exception):
    """沙箱错误基类"""


class SandboxTimeoutError(SandboxError):
    """沙箱超时错误"""


class SandboxSecurityError(SandboxError):
    """沙箱安全错误"""


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

    def validate(self, code: str) -> tuple[bool, list[str]]:
        """
        验证代码安全性（F-SANDBOX-2 白名单模式，fail-closed）。

        仅放行：白名单内置调用、普通（非 dunder）方法调用、基础语法。
        一律拒绝：import 语句、未列入白名单的调用、dunder 方法调用、
        非调用形式的属性访问。
        """
        issues = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            # 语法错误不是安全问题，返回 True 让执行阶段处理
            return True, []

        # 收集"作为 Call.func 的 Attribute 节点"——这些是方法调用（在 Call
        # 分支处理），不应被独立属性访问检查误拒。
        call_func_attr_ids = {
            id(n.func)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

        for node in ast.walk(tree):
            # import 一律禁止（白名单模式：模块只能由宿主预载）
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                issues.append("白名单模式禁止 import 语句")
                continue

            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    # 内置调用：必须在白名单
                    if func.id not in ALLOWED_BUILTINS:
                        issues.append(f"禁止调用未列入白名单的函数: {func.id}")
                elif isinstance(func, ast.Attribute):
                    # 方法调用：dunder（下划线开头属性）一律拒，普通方法放行
                    if func.attr.startswith("_"):
                        issues.append(f"禁止调用 dunder 方法: {func.attr}")

            elif isinstance(node, ast.Attribute) and id(node) not in call_func_attr_ids:
                # 非调用形式的属性访问（如 x.real、[].count）一律拒（fail-closed）
                issues.append(f"禁止属性访问（白名单模式）: {node.attr}")

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
        """准备全局变量（F-SANDBOX-2 白名单模式）。"""
        import builtins  # noqa: PLC0415

        # F-SANDBOX-2 白名单：仅允许 ALLOWED_BUILTINS 中的纯函数内置；
        # 不提供 __import__（代码侧 import 已被 AST 拒，运行时也不给）。
        safe_builtins = {}
        for name in ALLOWED_BUILTINS:
            with suppress(AttributeError):
                safe_builtins[name] = getattr(builtins, name)

        globals_dict = {
            "__builtins__": safe_builtins,
        }

        # 预加载允许的模块（宿主预载；代码侧 import 已被 AST 拒）
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
