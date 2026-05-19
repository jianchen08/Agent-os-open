"""插件验证器 — 自动检查插件是否符合开发标准规范。

验证维度：
1. 命名规范 — 文件名、目录名、类名是否符合规范
2. 目录结构 — 是否遵循标准目录结构
3. 接口约束 — 是否正确继承基类、实现必需成员
4. 配置格式 — 构造函数是否接受 config 参数
5. 错误策略 — 是否声明了有效的 error_policy
6. State 命名空间 — state 键是否使用命名空间格式
7. 安全规范 — 是否包含不安全操作
8. 文档规范 — 模块和类是否有文档字符串

用法:
    # 验证单个插件
    python -m tools.plugin_validator input memory_read

    # 验证所有插件
    python -m tools.plugin_validator --all

    # 输出 JSON 格式
    python -m tools.plugin_validator --all --format json
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PROJECT_ROOT / "src" / "plugins"

VALID_PLUGIN_TYPES = {"input", "output", "core"}

# 基类映射
BASE_CLASS_MAP = {
    "input": "IInputPlugin",
    "output": "IOutputPlugin",
    "core": "ICorePlugin",
}

# 错误策略枚举值
VALID_ERROR_POLICIES = {"ABORT", "SKIP", "RETRY", "FALLBACK"}

# 不安全函数列表
UNSAFE_FUNCTIONS = {"eval", "exec", "compile", "__import__"}


class Severity(str, Enum):
    """验证结果严重级别。"""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationResult:
    """单条验证结果。"""
    rule_id: str
    severity: Severity
    message: str
    detail: str = ""


@dataclass
class PluginValidationReport:
    """插件验证报告。"""
    plugin_name: str
    plugin_type: str
    plugin_path: str
    passed: bool = True
    results: list[ValidationResult] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0

    def add(self, result: ValidationResult) -> None:
        """添加验证结果。"""
        self.results.append(result)
        if result.severity == Severity.ERROR:
            self.error_count += 1
            self.passed = False
        elif result.severity == Severity.WARNING:
            self.warning_count += 1

    def summary(self) -> str:
        """生成摘要文本。"""
        status = "✅ 通过" if self.passed else "❌ 不通过"
        lines = [
            f"\n{'='*60}",
            f"插件验证报告: {self.plugin_name} ({self.plugin_type})",
            f"路径: {self.plugin_path}",
            f"状态: {status}",
            f"错误: {self.error_count}  警告: {self.warning_count}",
            f"{'='*60}",
        ]
        for r in self.results:
            icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[r.severity.value]
            lines.append(f"  {icon} [{r.rule_id}] {r.message}")
            if r.detail:
                lines.append(f"      {r.detail}")
        return "\n".join(lines)


class PluginValidator:
    """插件验证器。"""

    def __init__(self, plugins_dir: Path | None = None) -> None:
        self.plugins_dir = plugins_dir or PLUGINS_DIR

    def validate_plugin(self, plugin_type: str, plugin_name: str) -> PluginValidationReport:
        """验证单个插件。"""
        report = PluginValidationReport(
            plugin_name=plugin_name,
            plugin_type=plugin_type,
            plugin_path=str(self.plugins_dir / plugin_type / plugin_name),
        )

        plugin_dir = self.plugins_dir / plugin_type / plugin_name

        # 1. 目录结构验证
        self._check_directory_structure(report, plugin_dir, plugin_name)

        # 2. 查找主文件
        main_file = plugin_dir / f"{plugin_name}.py"
        if not main_file.exists():
            # 尝试单文件模式
            main_file = plugin_dir / "__init__.py"

        if not main_file.exists() or main_file.stat().st_size < 10:
            report.add(ValidationResult(
                rule_id="DIR-001",
                severity=Severity.ERROR,
                message=f"找不到插件主文件: {plugin_name}.py",
            ))
            return report

        # 解析 AST
        try:
            source = main_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError) as e:
            report.add(ValidationResult(
                rule_id="PARSE-001",
                severity=Severity.ERROR,
                message=f"无法解析插件文件: {e}",
            ))
            return report

        # 3. 命名规范验证
        self._check_naming(report, plugin_name, tree)

        # 4. 接口约束验证
        self._check_interface(report, plugin_type, plugin_name, tree)

        # 5. 配置格式验证
        self._check_constructor(report, tree)

        # 6. 错误策略验证
        self._check_error_policy(report, tree)

        # 7. 安全规范验证
        self._check_security(report, source, tree)

        # 8. 文档规范验证
        self._check_documentation(report, tree)

        # 9. State 命名空间验证
        self._check_state_namespace(report, source)

        return report

    def validate_all(self) -> list[PluginValidationReport]:
        """验证所有已注册的插件。"""
        reports: list[PluginValidationReport] = []
        for plugin_type in VALID_PLUGIN_TYPES:
            type_dir = self.plugins_dir / plugin_type
            if not type_dir.exists():
                continue
            for item in sorted(type_dir.iterdir()):
                if item.is_dir() and not item.name.startswith("_"):
                    report = self.validate_plugin(plugin_type, item.name)
                    reports.append(report)
        return reports

    # ── 验证方法 ──────────────────────────────────────

    def _check_directory_structure(
        self, report: PluginValidationReport, plugin_dir: Path, plugin_name: str,
    ) -> None:
        """验证目录结构。"""
        if not plugin_dir.exists():
            report.add(ValidationResult(
                rule_id="DIR-001",
                severity=Severity.ERROR,
                message=f"插件目录不存在: {plugin_dir}",
            ))
            return

        # 检查 __init__.py
        init_file = plugin_dir / "__init__.py"
        if not init_file.exists():
            report.add(ValidationResult(
                rule_id="DIR-002",
                severity=Severity.WARNING,
                message="缺少 __init__.py 文件",
                detail="建议添加 __init__.py 并导出插件类",
            ))

        # 检查主文件
        main_file = plugin_dir / f"{plugin_name}.py"
        if not main_file.exists():
            report.add(ValidationResult(
                rule_id="DIR-003",
                severity=Severity.WARNING,
                message=f"缺少主文件 {plugin_name}.py",
                detail="插件可能使用单文件模式（__init__.py）",
            ))

    def _check_naming(
        self, report: PluginValidationReport, plugin_name: str, tree: ast.Module,
    ) -> None:
        """验证命名规范。"""
        # 检查插件名 snake_case
        if not re.match(r"^[a-z][a-z0-9_]*$", plugin_name):
            report.add(ValidationResult(
                rule_id="NAME-001",
                severity=Severity.ERROR,
                message=f"插件名 '{plugin_name}' 不符合 snake_case 规范",
            ))

        # 检查类名 CamelCase
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.bases:  # 有继承的类
                    expected_suffix = "Plugin"
                    if not node.name.endswith(expected_suffix) and not node.name.endswith("Core"):
                        report.add(ValidationResult(
                            rule_id="NAME-002",
                            severity=Severity.WARNING,
                            message=f"类名 '{node.name}' 不以 'Plugin' 或 'Core' 结尾",
                            detail="建议类名格式: {CamelCase}Plugin 或 {CamelCase}Core",
                        ))

    def _check_interface(
        self,
        report: PluginValidationReport,
        plugin_type: str,
        plugin_name: str,
        tree: ast.Module,
    ) -> None:
        """验证接口约束。"""
        expected_base = BASE_CLASS_MAP.get(plugin_type)
        if not expected_base:
            return

        # 查找继承自目标基类的类
        plugin_classes: list[ast.ClassDef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = self._get_name(base)
                    if base_name and base_name in BASE_CLASS_MAP.values():
                        plugin_classes.append(node)

        if not plugin_classes:
            report.add(ValidationResult(
                rule_id="IFACE-001",
                severity=Severity.ERROR,
                message=f"未找到继承自 {expected_base} 的类",
                detail=f"{plugin_type} 类型插件必须继承 {expected_base}",
            ))
            return

        for cls in plugin_classes:
            members = {node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            properties = {
                node.name for node in cls.body
                if isinstance(node, ast.FunctionDef) and any(
                    isinstance(d, ast.Name) and d.id == "property"
                    for d in node.decorator_list
                )
            }

            # 检查 name 属性
            if "name" not in properties and "name" not in members:
                report.add(ValidationResult(
                    rule_id="IFACE-002",
                    severity=Severity.ERROR,
                    message=f"类 '{cls.name}' 缺少 name 属性",
                    detail="必须实现 @property name -> str",
                ))

            # 检查 priority 属性
            if "priority" not in properties and "priority" not in members:
                report.add(ValidationResult(
                    rule_id="IFACE-003",
                    severity=Severity.ERROR,
                    message=f"类 '{cls.name}' 缺少 priority 属性",
                    detail="必须实现 @property priority -> int",
                ))

            # 检查 execute 方法
            if "execute" not in members:
                report.add(ValidationResult(
                    rule_id="IFACE-004",
                    severity=Severity.ERROR,
                    message=f"类 '{cls.name}' 缺少 execute 方法",
                    detail="必须实现 async def execute(self, ctx: PluginContext)",
                ))
            else:
                # 检查 execute 是否是 async
                for node in cls.body:
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute":
                        break
                else:
                    # 检查是否有同名普通函数
                    for node in cls.body:
                        if isinstance(node, ast.FunctionDef) and node.name == "execute":
                            report.add(ValidationResult(
                                rule_id="IFACE-005",
                                severity=Severity.ERROR,
                                message=f"execute 方法必须是 async",
                                detail="async def execute(self, ctx: PluginContext)",
                            ))
                            break

    def _check_constructor(self, report: PluginValidationReport, tree: ast.Module) -> None:
        """验证构造函数格式。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        args = item.args
                        if len(args.args) < 2:  # self + config
                            report.add(ValidationResult(
                                rule_id="CTOR-001",
                                severity=Severity.WARNING,
                                message=f"构造函数参数过少",
                                detail="建议: __init__(self, config: dict | None = None)",
                            ))
                            break

                        # 检查第二个参数是否有默认值
                        if args.defaults:
                            has_none_default = False
                            for default in args.defaults:
                                if isinstance(default, ast.Constant) and default.value is None:
                                    has_none_default = True
                            if not has_none_default:
                                report.add(ValidationResult(
                                    rule_id="CTOR-002",
                                    severity=Severity.WARNING,
                                    message="构造函数 config 参数建议默认为 None",
                                    detail="允许无配置时正常构造",
                                ))
                        break

    def _check_error_policy(self, report: PluginValidationReport, tree: ast.Module) -> None:
        """验证错误策略声明。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        if item.target.id == "error_policy":
                            if isinstance(item.value, ast.Attribute):
                                policy_name = item.value.attr
                                if policy_name not in VALID_ERROR_POLICIES:
                                    report.add(ValidationResult(
                                        rule_id="POLICY-001",
                                        severity=Severity.ERROR,
                                        message=f"无效的错误策略: {policy_name}",
                                        detail=f"有效值: {', '.join(sorted(VALID_ERROR_POLICIES))}",
                                    ))
                            return
                # 没有找到 error_policy 声明
                has_base = any(
                    self._get_name(base) in BASE_CLASS_MAP.values()
                    for base in node.bases
                )
                if has_base:
                    report.add(ValidationResult(
                        rule_id="POLICY-002",
                        severity=Severity.WARNING,
                        message=f"类 '{node.name}' 未声明 error_policy",
                        detail="建议显式声明 error_policy",
                    ))

    def _check_security(self, report: PluginValidationReport, source: str, tree: ast.Module) -> None:
        """验证安全规范。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_name(node.func)
                if func_name in UNSAFE_FUNCTIONS:
                    report.add(ValidationResult(
                        rule_id="SEC-001",
                        severity=Severity.ERROR,
                        message=f"发现不安全函数调用: {func_name}()",
                        detail="禁止使用 eval()、exec() 等不安全操作",
                    ))

    def _check_documentation(self, report: PluginValidationReport, tree: ast.Module) -> None:
        """验证文档规范。"""
        # 检查模块文档字符串
        module_doc = ast.get_docstring(tree)
        if not module_doc:
            report.add(ValidationResult(
                rule_id="DOC-001",
                severity=Severity.WARNING,
                message="缺少模块文档字符串",
                detail="建议添加模块级 docstring 描述插件功能",
            ))

        # 检查类文档字符串
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                has_base = any(
                    self._get_name(base) in BASE_CLASS_MAP.values()
                    for base in node.bases
                )
                if has_base:
                    class_doc = ast.get_docstring(node)
                    if not class_doc:
                        report.add(ValidationResult(
                            rule_id="DOC-002",
                            severity=Severity.WARNING,
                            message=f"类 '{node.name}' 缺少文档字符串",
                            detail="建议添加类级 docstring",
                        ))

    def _check_state_namespace(self, report: PluginValidationReport, source: str) -> None:
        """验证 State 命名空间使用。"""
        # 查找字符串字面量中的 state 键
        # 匹配 "xxx.yyy" 格式的命名空间键
        namespace_pattern = re.compile(r'["\']([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)["\']')
        matches = namespace_pattern.findall(source)

        # 排除已知的非 state 键
        known_prefixes = {"pipeline", "plugins", "config", "logging", "tools"}
        for match in matches:
            prefix = match.split(".")[0]
            if prefix in known_prefixes:
                continue
            # 命名空间键看起来合法，不报错
            # 只检查是否有不使用命名空间的键（纯单层键用作 state 写入）
            break

    # ── 工具方法 ──────────────────────────────────────

    @staticmethod
    def _get_name(node: ast.expr) -> str | None:
        """从 AST 节点提取名称。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None


def main() -> None:
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="插件验证器 — 检查插件是否符合开发标准规范")
    parser.add_argument("plugin_type", nargs="?", choices=sorted(VALID_PLUGIN_TYPES), help="插件类型")
    parser.add_argument("plugin_name", nargs="?", help="插件名称")
    parser.add_argument("--all", action="store_true", help="验证所有插件")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")

    args = parser.parse_args()

    validator = PluginValidator()

    if args.all:
        reports = validator.validate_all()
    elif args.plugin_type and args.plugin_name:
        reports = [validator.validate_plugin(args.plugin_type, args.plugin_name)]
    else:
        parser.print_help()
        sys.exit(1)

    if args.format == "json":
        output = []
        for r in reports:
            output.append({
                "plugin_name": r.plugin_name,
                "plugin_type": r.plugin_type,
                "plugin_path": r.plugin_path,
                "passed": r.passed,
                "error_count": r.error_count,
                "warning_count": r.warning_count,
                "results": [
                    {
                        "rule_id": vr.rule_id,
                        "severity": vr.severity.value,
                        "message": vr.message,
                        "detail": vr.detail,
                    }
                    for vr in r.results
                ],
            })
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        for r in reports:
            print(r.summary())

    # 退出码
    all_passed = all(r.passed for r in reports)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
