"""安全审查模块。

对生成的代码进行全方位安全审查，包括：
- 静态分析：检测危险导入、危险模式、代码注入风险
- 沙箱执行：在受限环境中执行代码，限制时间和资源
- 权限检查：分析代码声明的权限需求
- 资源限制：检查死循环、大内存分配等问题

暴露接口：
- static_analysis(code) -> list[dict]
- sandbox_execute(code, timeout) -> dict
- check_permissions(artifact) -> list[str]
- check_resource_limits(artifact) -> list[str]
- review(artifact) -> SecurityReport
- SecurityReviewer: 安全审查器类
"""

from __future__ import annotations

import ast
import logging
import sys
from typing import Any

from evolution.types import GeneratedArtifact, SecurityReport

logger = logging.getLogger(__name__)

# 危险导入列表
DANGEROUS_IMPORTS: set[str] = {
    "os.system",
    "subprocess",
    "eval",
    "exec",
    "__import__",
    "compile",
    "os.popen",
    "os.spawn",
    "ctypes",
    "multiprocessing",
    "threading",
    "socket",
    "http.server",
    "xmlrpc",
    "telnetlib",
    "ftplib",
    "smtplib",
}

# 危险函数调用列表
DANGEROUS_CALLS: set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "breakpoint",
    "os.system",
    "os.popen",
    "os.spawn",
}

# 允许的导入列表（白名单）
ALLOWED_IMPORTS: set[str] = {
    "typing",
    "dataclasses",
    "json",
    "logging",
    "datetime",
    "collections",
    "abc",
    "enum",
    "pathlib",
    "re",
    "math",
    "functools",
    "itertools",
    "copy",
    "hashlib",
    "uuid",
    "tools.builtin.base",
    "tools.types",
    "core.results",
}


class SecurityReviewer:
    """安全审查器。

    对生成的代码进行多层次安全审查：
    1. 静态分析（AST 级别）
    2. 沙箱执行（受限进程）
    3. 权限检查
    4. 资源限制检查

    Attributes:
        _allowed_imports: 允许的导入白名单
        _dangerous_imports: 危险导入黑名单
        _dangerous_calls: 危险函数调用黑名单
    """

    def __init__(
        self,
        allowed_imports: set[str] | None = None,
        allowed_permissions: set[str] | None = None,
    ) -> None:
        """初始化安全审查器。

        Args:
            allowed_imports: 允许的导入白名单（覆盖默认值）
            allowed_permissions: 允许的权限列表
        """
        self._allowed_imports = allowed_imports or ALLOWED_IMPORTS
        self._allowed_permissions = allowed_permissions or set()
        self._dangerous_imports = DANGEROUS_IMPORTS
        self._dangerous_calls = DANGEROUS_CALLS

    def static_analysis(self, code: str) -> list[dict[str, Any]]:
        """对代码进行静态分析。

        使用 AST 解析检测：
        - 危险导入（os.system, subprocess, eval, exec 等）
        - 危险模式（文件系统直接操作、网络请求、环境变量读取）
        - 代码注入风险

        Args:
            code: 待分析的代码字符串

        Returns:
            问题列表，每项包含 severity, category, message, line
        """
        issues: list[dict[str, Any]] = []

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            issues.append({
                "severity": "critical",
                "category": "syntax_error",
                "message": f"代码存在语法错误: {exc}",
                "line": getattr(exc, "lineno", 0),
            })
            return issues

        # 检查导入语句
        issues.extend(self._check_imports(tree))

        # 检查函数调用
        issues.extend(self._check_calls(tree))

        # 检查文件系统操作
        issues.extend(self._check_filesystem(tree))

        # 检查网络操作
        issues.extend(self._check_network(tree))

        # 检查环境变量读取
        issues.extend(self._check_env_access(tree))

        return issues

    def sandbox_execute(
        self,
        code: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """在受限沙箱环境中执行代码。

        当前实现仅做 AST 解析验证，通过子进程运行解析检查，
        不会实际 import 或执行生成代码中的业务逻辑。
        对于 BuiltinTool 代码，仅验证模块级别的安全性。

        Args:
            code: 待执行的代码
            timeout: 超时时间（秒）

        Returns:
            执行结果，包含 success, output, error, timed_out
        """
        # 构建安全的测试代码（只做 AST 解析和简单检查）
        test_code = (
            "import ast\n"
            "import sys\n"
            f"code = {repr(code)}\n"
            "try:\n"
            "    tree = ast.parse(code)\n"
            "    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]\n"
            "    functions = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]\n"
            "    print(f'AST parse OK: classes={classes}, functions={functions}')\n"
            "except SyntaxError as e:\n"
            "    print(f'SyntaxError: {e}')\n"
            "    sys.exit(1)\n"
        )

        try:
            # 使用 subprocess 执行受限代码
            import subprocess

            result = subprocess.run(
                [sys.executable, "-c", test_code],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return {
                "success": result.returncode == 0,
                "output": result.stdout.strip(),
                "error": result.stderr.strip() if result.returncode != 0 else "",
                "timed_out": False,
                "return_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            logger.warning("[SecurityReviewer] 沙箱执行超时: timeout=%.1fs", timeout)
            return {
                "success": False,
                "output": "",
                "error": f"执行超时（{timeout}秒）",
                "timed_out": True,
                "return_code": -1,
            }
        except Exception as exc:
            logger.warning("[SecurityReviewer] 沙箱执行异常: %s", exc)
            return {
                "success": False,
                "output": "",
                "error": f"执行异常: {exc}",
                "timed_out": False,
                "return_code": -1,
            }

    def check_permissions(self, artifact: GeneratedArtifact) -> list[str]:
        """检查代码声明的权限需求。

        使用 AST 精确分析代码中的危险操作声明，对比允许的权限列表。

        Args:
            artifact: 生成的代码产物

        Returns:
            权限问题列表
        """
        issues: list[str] = []
        code = artifact.code

        try:
            tree = ast.parse(code)
        except SyntaxError:
            issues.append("代码存在语法错误，无法进行权限分析")
            return issues

        # 使用 AST 精确查找 "dangerous_operations" 字符串常量
        has_dangerous_ops = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "dangerous_operations" in node.value:
                    has_dangerous_ops = True
                    break

        if not has_dangerous_ops:
            # 再检查赋值目标中是否包含 dangerous_operations
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        target_name = None
                        if isinstance(target, ast.Name):
                            target_name = target.id
                        elif isinstance(target, ast.Attribute):
                            target_name = target.attr
                        if target_name == "dangerous_operations":
                            has_dangerous_ops = True
                            break
                if has_dangerous_ops:
                    break

        if has_dangerous_ops:
            dangerous_keywords = [
                "file_write", "file_delete", "network", "subprocess",
                "system", "execute", "eval", "import",
            ]
            # 精确提取 dangerous_operations 中的字符串常量
            declared_ops: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in dangerous_keywords:
                        declared_ops.add(node.value)

            for keyword in declared_ops:
                if self._allowed_permissions and keyword not in self._allowed_permissions:
                    issues.append(
                        f"代码声明了未授权的危险操作: {keyword}"
                    )

        return issues

    def check_resource_limits(self, artifact: GeneratedArtifact) -> list[str]:
        """检查代码是否有明显的资源消耗问题。

        检测：
        - 死循环（while True 无 break）
        - 大内存分配（大列表/字典创建）
        - 递归调用（可能导致栈溢出）

        Args:
            artifact: 生成的代码产物

        Returns:
            资源限制违规列表
        """
        violations: list[str] = []
        code = artifact.code

        try:
            tree = ast.parse(code)
        except SyntaxError:
            violations.append("代码存在语法错误，无法进行资源检查")
            return violations

        # 检查 while True 循环（可能的死循环）
        for node in ast.walk(tree):
            if isinstance(node, ast.While):
                # 检查条件是否为 True 常量
                if isinstance(node.test, ast.Constant) and node.test.value is True:
                    # 检查是否有 break 语句
                    has_break = False
                    for child in ast.walk(node):
                        if isinstance(child, ast.Break):
                            has_break = True
                            break
                    if not has_break:
                        violations.append(
                            "检测到无 break 的 while True 循环，可能导致死循环"
                        )

        # 检查大列表创建（通过 range 调用）
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "range":
                    # 检查参数是否为大数值常量
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                            if arg.value > 10000:
                                violations.append(
                                    f"检测到大范围迭代: range({arg.value})，可能消耗大量内存"
                                )

        # 检查递归调用
        violations.extend(self._check_recursion(tree))

        return violations

    def review(self, artifact: GeneratedArtifact) -> SecurityReport:
        """执行综合安全审查。

        依次执行：静态分析 → 沙箱执行 → 权限检查 → 资源限制检查。
        任何 critical 级别的问题都会导致审查失败。

        Args:
            artifact: 待审查的代码产物

        Returns:
            完整的安全审查报告
        """
        logger.info(
            "[SecurityReviewer] 开始安全审查: type=%s, file=%s",
            artifact.generation_type.value,
            artifact.file_path,
        )

        # Step 1: 静态分析
        static_issues = self.static_analysis(artifact.code)

        # Step 2: 沙箱执行
        sandbox_result = self.sandbox_execute(artifact.code)

        # Step 3: 权限检查
        permission_issues = self.check_permissions(artifact)

        # Step 4: 资源限制检查
        resource_violations = self.check_resource_limits(artifact)

        # 综合评估
        critical_count = sum(
            1 for issue in static_issues
            if issue.get("severity") == "critical"
        )
        high_count = sum(
            1 for issue in static_issues
            if issue.get("severity") == "high"
        )

        # 确定风险等级
        if critical_count > 0 or sandbox_result.get("timed_out", False):
            overall_risk = "critical"
            passed = False
        elif high_count > 2 or not sandbox_result.get("success", False):
            overall_risk = "high"
            passed = False
        elif high_count > 0 or len(permission_issues) > 0:
            overall_risk = "medium"
            passed = len(permission_issues) == 0 and high_count == 0
        else:
            overall_risk = "low"
            passed = True

        report = SecurityReport(
            passed=passed,
            static_analysis_issues=static_issues,
            sandbox_result=sandbox_result,
            permission_issues=permission_issues,
            resource_violations=resource_violations,
            overall_risk=overall_risk,
        )

        logger.info(
            "[SecurityReviewer] 安全审查完成: passed=%s, risk=%s, "
            "static_issues=%d, perm_issues=%d, resource_violations=%d",
            passed,
            overall_risk,
            len(static_issues),
            len(permission_issues),
            len(resource_violations),
        )
        return report

    # -- 内部方法 --------------------------------------------------------

    def _check_imports(self, tree: ast.Module) -> list[dict[str, Any]]:
        """检查危险导入。

        Args:
            tree: AST 解析树

        Returns:
            问题列表
        """
        issues: list[dict[str, Any]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    if self._is_dangerous_import(module_name):
                        issues.append({
                            "severity": "critical",
                            "category": "dangerous_import",
                            "message": f"检测到危险导入: {module_name}",
                            "line": node.lineno,
                        })

            elif isinstance(node, ast.ImportFrom):
                if node.module and self._is_dangerous_import(node.module):
                    issues.append({
                        "severity": "critical",
                        "category": "dangerous_import",
                        "message": f"检测到危险导入: from {node.module}",
                        "line": node.lineno,
                    })

        return issues

    def _check_calls(self, tree: ast.Module) -> list[dict[str, Any]]:
        """检查危险函数调用。

        Args:
            tree: AST 解析树

        Returns:
            问题列表
        """
        issues: list[dict[str, Any]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name in self._dangerous_calls:
                    issues.append({
                        "severity": "high",
                        "category": "dangerous_call",
                        "message": f"检测到危险函数调用: {func_name}",
                        "line": getattr(node, "lineno", 0),
                    })

        return issues

    def _check_filesystem(self, tree: ast.Module) -> list[dict[str, Any]]:
        """检查文件系统直接操作。

        Args:
            tree: AST 解析树

        Returns:
            问题列表
        """
        issues: list[dict[str, Any]] = []
        fs_functions = {
            "open", "os.remove", "os.rmdir", "os.rename",
            "os.mkdir", "os.makedirs", "shutil.rmtree",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name in fs_functions:
                    issues.append({
                        "severity": "medium",
                        "category": "filesystem",
                        "message": f"检测到文件系统操作: {func_name}",
                        "line": getattr(node, "lineno", 0),
                    })

        return issues

    def _check_network(self, tree: ast.Module) -> list[dict[str, Any]]:
        """检查网络操作。

        Args:
            tree: AST 解析树

        Returns:
            问题列表
        """
        issues: list[dict[str, Any]] = []
        network_functions = {
            "urllib.request.urlopen", "requests.get", "requests.post",
            "http.client", "socket.connect",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name in network_functions:
                    issues.append({
                        "severity": "high",
                        "category": "network",
                        "message": f"检测到网络操作: {func_name}",
                        "line": getattr(node, "lineno", 0),
                    })

        return issues

    def _check_env_access(self, tree: ast.Module) -> list[dict[str, Any]]:
        """检查环境变量读取。

        Args:
            tree: AST 解析树

        Returns:
            问题列表
        """
        issues: list[dict[str, Any]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name in ("os.getenv", "os.environ"):
                    issues.append({
                        "severity": "medium",
                        "category": "env_access",
                        "message": f"检测到环境变量访问: {func_name}",
                        "line": getattr(node, "lineno", 0),
                    })

        return issues

    def _check_recursion(self, tree: ast.Module) -> list[str]:
        """检查递归调用。

        Args:
            tree: AST 解析树

        Returns:
            违规列表
        """
        violations: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        call_name = self._get_call_name(child)
                        if call_name == func_name:
                            violations.append(
                                f"函数 '{func_name}' 存在递归调用，可能导致栈溢出"
                            )

        return violations

    def _is_dangerous_import(self, module_name: str) -> bool:
        """判断模块是否为危险导入。

        Args:
            module_name: 模块名称

        Returns:
            是否为危险导入
        """
        # 检查完全匹配
        for dangerous in self._dangerous_imports:
            if module_name == dangerous or module_name.startswith(dangerous + "."):
                return True

        # 提取顶级模块名，检查黑名单
        top_module = module_name.split(".")[0]
        dangerous_tops = {
            "subprocess", "ctypes", "multiprocessing",
            "socket", "telnetlib", "ftplib", "smtplib",
            "xmlrpc",
        }
        return top_module in dangerous_tops

    @staticmethod
    def _get_call_name(node: ast.Call) -> str:
        """获取函数调用的名称。

        Args:
            node: Call AST 节点

        Returns:
            函数调用的字符串表示
        """
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts: list[str] = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return ""
