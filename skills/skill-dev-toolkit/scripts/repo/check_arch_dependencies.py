#!/usr/bin/env python3
"""架构依赖巡检车道（P-18，方案 §9.2 步骤 1 实施项最小落位）。

真源与口径：
- 方案 §9.2「架构测试扫描深度」：扫描必须覆盖四种导入形态——函数体内导入 /
  条件导入 / 动态导入 / TYPE_CHECKING（v3 §3.3 同款要求），堵住「架构边界
  违规经动态/惰性导入绕过 lint 面」的绕过通道。
- 规则面（v3 §3.3 原文「扫描非法导入私有接口」直接映射 + PEP 8 单下划线
  私有惯例）：**R1 跨包私有接口导入禁令**——plugins/shared 域内，导入方包
  消费另一包的私有接口（导入路径中段 `_` 前缀子模块，或 from-import 的
  `_` 前缀名；dunder 与 `__future__` 豁免）即违规。
- 跨包**公共**接口依赖不在此闸（现库跨包公共 import 448 处属既有惯例，
  非成文边界；infra→biz 方向禁令因 _libs/pipeline_base/active_children.py
  的 tasks 惰性导入系 docstring 明文回退链设计亦未立禁，均留观察）。

平铺布局歧义消解（现库校准）：插件平铺模块经 ``sys.path`` 注入消费本包
直下裸名模块（isolation/server.py 的 ``from workspace import ...`` 实况），
顶层名在导入方包目录直下存在 ``<top>.py`` / ``<top>/__init__.py`` 时判同包
内部导入，不入 R1 判定。

扫描深度实现：AST 递归遍历带位置上下文，静态 Import/ImportFrom 按
顶层 / 函数体内 / 条件 / TYPE_CHECKING 标形态；``importlib.import_module``
与 ``__import__`` 调用（函数体内与顶层均计）按动态形态收集：常量字符串实参
解析为导入目标入 R1 同查；非常量实参静态不可达目标，计入
``non_constant_dynamic_imports`` 显性留痕、不判违规。

豁免面（对齐 check_shared_root_imports 惯例）：测试文件（tests/ 目录整树、
test_*.py / *_test.py / conftest.py）白盒消费不扫；巨目录剪枝不下钻。

假设声明（外部对接面，提案原文方向性描述→最合理形态）：
- 脚本名未见于提案原文，按派发任务书默认 ``check_arch_dependencies.py``；
- 本车道为 Python 域先行；前端域 dependency-cruiser 选型确认与 run_gates
  接线按提案明文随触发节奏（「配置确认后挂 run_gates；触发节奏随下一批
  架构面改造」），本件不接线、不阻塞 CI。

用法：python scripts/check_arch_dependencies.py [--root <plugins/shared 替代根>]
退出码：0 零违规；1 有违规；2 fail-closed（扫描面语法错拒判）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = ROOT / "plugins" / "shared"

# 与 check_shared_root_imports 同一套目录排除口径（剪枝式遍历，不进巨目录）
EXCLUDE_DIRS = {
    "__pycache__",
    "node_modules",
    "runtime",
    "target",
    ".venv",
    "venv",
    ".venv-hindsight",
    ".ai_workspaces",
    "tests",
}
SCAN_SUFFIX = ".py"

FORM_TOP = "顶层"
FORM_FUNCTION = "函数体内"
FORM_CONDITIONAL = "条件"
FORM_TYPE_CHECKING = "TYPE_CHECKING"
FORM_DYNAMIC = "动态"

_CONTROL_FLOW = (
    ast.Try,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.With,
    ast.AsyncWith,
    ast.ExceptHandler,
    ast.match_case,
)


@dataclass(frozen=True)
class Violation:
    """单条架构依赖违规（file:line + 形态 + 规则描述）。"""

    rel: str
    lineno: int
    form: str
    message: str


def is_test_file(path: Path) -> bool:
    """平铺布局测试件：test_*.py / *_test.py / conftest.py。"""
    return path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py"


def iter_sources(scan_root: Path) -> list[Path]:
    """剪枝式遍历扫描面生产码（排除目录整体不下钻，测试件不入列）。"""
    out: list[Path] = []
    if not scan_root.is_dir():
        return out
    stack = [scan_root]
    while stack:
        cur = stack.pop()
        for p in cur.iterdir():
            if p.is_dir():
                if p.name in EXCLUDE_DIRS:
                    continue
                stack.append(p)
            elif p.suffix == SCAN_SUFFIX and not is_test_file(p):
                out.append(p)
    return out


def is_type_checking_test(test: ast.expr) -> bool:
    """if 测试是否为 TYPE_CHECKING 判定（裸名或 ``typing.TYPE_CHECKING``）。"""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def is_dynamic_import_call(call: ast.Call) -> bool:
    """是否为动态导入调用（importlib.import_module / import_module / __import__）。"""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    ) or (isinstance(func, ast.Name) and func.id in ("import_module", "__import__"))


def dynamic_import_target(call: ast.Call) -> str | None:
    """动态导入调用的静态可解析目标；实参非常量（不可解析）返回 None。"""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def collect_import_sites(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """带位置上下文的导入位点收集：返回 (形态标签, 节点) 列表。

    形态判定：函数体优先（进入 def/lambda 后子树恒函数体内）；顶层遇控制
    流块降为条件（TYPE_CHECKING 块特判）；动态导入调用恒标动态。
    """
    sites: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, form: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_form = form
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                child_form = FORM_FUNCTION
            elif isinstance(child, ast.If):
                if is_type_checking_test(child.test):
                    child_form = FORM_TYPE_CHECKING
                elif form == FORM_TOP:
                    child_form = FORM_CONDITIONAL
            elif isinstance(child, _CONTROL_FLOW) and form == FORM_TOP:
                child_form = FORM_CONDITIONAL
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                sites.append((child_form, child))
                continue
            if isinstance(child, ast.Call) and is_dynamic_import_call(child):
                sites.append((FORM_DYNAMIC, child))
            walk(child, child_form)

    walk(tree, FORM_TOP)
    return sites


def _private_name(name: str) -> bool:
    """私有导入名判定：``_`` 前缀且非 dunder、非 ``__future__``。"""
    if name == "__future__":
        return False
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def classify_violation(
    importer_pkg: str,
    pkg_dir: Path,
    target: str,
    imported_names: list[str],
) -> str | None:
    """R1 判定：跨包私有接口导入返回违规描述，合法返回 None。

    平铺消解：顶层名在本包直下可解析（``<top>.py`` / ``<top>/__init__.py``）
    → 同包内部导入；相对导入由调用方先行排除（恒同包）。
    """
    segments = target.split(".")
    top = segments[0]
    if top == importer_pkg:
        return None
    if (pkg_dir / (top + SCAN_SUFFIX)).exists() or (pkg_dir / top / "__init__.py").exists():
        return None
    if any(seg.startswith("_") for seg in segments[1:]):
        return f"跨包导入私有模块路径 `{target}`"
    for name in imported_names:
        if _private_name(name):
            return f"跨包导入私有名 `{name}`（from {target}）"
    return None


def _import_pairs(node: ast.AST) -> list[tuple[str, list[str]]]:
    """静态导入节点展开为 (目标路径, from 导入名列表) 对。"""
    if isinstance(node, ast.Import):
        return [(alias.name, []) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level > 0 or not node.module:
            return []  # 相对导入恒同包
        return [(node.module, [alias.name for alias in node.names])]
    return []


def scan_file(
    path: Path,
    scan_root: Path,
    pkg_names: set[str],
    form_counts: dict[str, int],
) -> tuple[list[Violation], int]:
    """扫单文件：返回 (违规列表, 不可解析动态导入计数)。语法错 fail-closed。

    形态计数随位点收集累加进 form_counts（调用方持有汇总）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"fail-closed: {path}: 语法解析失败（{exc}）", file=sys.stderr)
        raise SystemExit(2) from exc
    importer_pkg = path.relative_to(scan_root).parts[0]
    violations: list[Violation] = []
    unresolved = 0
    for form, node in collect_import_sites(tree):
        form_counts[form] += 1
        if isinstance(node, ast.Call):
            target = dynamic_import_target(node)
            if target is None:
                unresolved += 1  # 非常量实参：静态不可达目标，显性计数留痕、不判违规
                continue
            message = classify_violation(importer_pkg, path.parent, target, [])
            if message:
                violations.append(Violation(path.relative_to(scan_root).as_posix(), node.lineno, form, message))
            continue
        for target, names in _import_pairs(node):
            if target.split(".")[0] not in pkg_names:
                continue  # shared 域外（标准库/第三方/本包平铺裸名），不入 R1
            message = classify_violation(importer_pkg, path.parent, target, names)
            if message:
                violations.append(Violation(path.relative_to(scan_root).as_posix(), node.lineno, form, message))
    return violations, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="架构依赖巡检（P-18，四种导入形态全覆盖）")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_SCAN_ROOT,
        help="扫描根（默认仓内 plugins/shared）",
    )
    args = parser.parse_args(argv)
    scan_root = args.root.resolve()

    pkg_names = {p.name for p in scan_root.iterdir() if p.is_dir()} if scan_root.is_dir() else set()
    all_violations: list[Violation] = []
    form_counts = dict.fromkeys((FORM_TOP, FORM_FUNCTION, FORM_CONDITIONAL, FORM_TYPE_CHECKING, FORM_DYNAMIC), 0)
    unresolved_total = 0
    scanned = 0
    for path in iter_sources(scan_root):
        scanned += 1
        violations, unresolved = scan_file(path, scan_root, pkg_names, form_counts)
        unresolved_total += unresolved
        all_violations.extend(violations)

    if all_violations:
        print(f"架构依赖违规 {len(all_violations)} 处（R1 跨包私有接口导入禁令，v3 §3.3）：")
        for v in all_violations:
            print(f"  {v.rel}:{v.lineno} [{v.form}] {v.message}")
        print(
            "修复指引：跨包消费走目标包公共出口（如 __init__.py 显式 re-export 或"
            " _root_api）；私有面属包内实现细节，禁止外部依赖。"
        )
        _print_summary(scanned, form_counts, unresolved_total)
        return 1
    print(f"架构依赖巡检零违规（R1 跨包私有接口导入；扫描 {scanned} 文件，{len(pkg_names)} 包，四种导入形态全覆盖）。")
    _print_summary(scanned, form_counts, unresolved_total)
    return 0


def _print_summary(scanned: int, form_counts: dict[str, int], unresolved_total: int) -> None:
    detail = "、".join(f"{name} {count}" for name, count in form_counts.items())
    print(
        f"形态计数（导入位点）：{detail}；non_constant_dynamic_imports={unresolved_total}"
        f"（非常量实参动态导入位点，显性计数留痕、不判违规）。"
    )


if __name__ == "__main__":
    raise SystemExit(main())
