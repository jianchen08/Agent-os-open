#!/usr/bin/env python3
"""服务声明静态闸（T5.3，资源注册与索引收敛方案_20261016 §五）。

终态闭包（方案 §二）：每个插件的真实服务调用点（get_capability / 句柄 .call）
  ⊆ 该插件 requires_services ∪ 内核内建面（KERNEL_PROVIDED_SERVICES，表为单源）。
本闸执法第一层（CI 静态）；第二层 plugin_check 安装期（--plugin 单插件模式，
kernel/crates/plugin-loader/src/bin/plugin_check.rs 委托本检查器）；
第三层运行时 capability router unresolved 对账（kernel api crate）。

普查口径：
- **扫描面**：``--scan-root``（默认仓内 plugins/）下全部生产码（口径对齐
  check_shared_root_imports.py：tests/ 目录、test_*.py / *_test.py / conftest.py、
  node_modules/.venv/__pycache__ 等巨目录剪枝排除；语法解析失败 fail-closed exit 2）。
- **调用点识别（AST，字面优先）**：
  1. ``X.get_capability("ns")`` 及本仓惯用变体（attr 以 ``get_capability`` 结尾，
     如 channel_common 注入式 ``self._get_capability("ns")``）；
  2. ``get_capability(名字)``——模块级字符串常量单赋值解析
     （db_admin/user_admin/metrics_admin 的 ``_CAPABILITY`` 形态）；
  3. ``for cap in ("a", "b"): ... get_capability(cap)``——循环字面序列绑定
     （simple/media 形态；iter 元素可为字面量或模块常量）；
  4. 句柄 ``.call("ns.method", ...)`` / ``.notify(...)`` 全字面两段式（ns 提取）。
- **识别边界（动态形态不报不猜，运行时 T5.5 兜底）**：变量中转传参
  （approval 的能力助手 ``get_capability(name)``）、跨文件常量、
  ``import _root_api`` 后 attr 式消费。边界调用点计数打印，不做执法。
- **归属**：文件属主 = 所在插件目录（含 plugin.json 的最近祖先）；无主库文件
  （共享根裸模块、channel_common/_libs/_root_api/_host、sdk）的调用点按 import
  闭包归给消费插件（channel_* 的 ``from inbound_bridge import ...`` 裸名形态、
  ``from _root_api import <sym>`` 经 _EXPORTS/_MODULE_EXPORTS 映射）。
- **真源**：内核内建面 = 解析 kernel/crates/plugin-loader/src/registry.rs 的
  KERNEL_PROVIDED_SERVICES 字面量表（解析失败/哨兵 ns 缺位 → fail-closed exit 2；
  非仓内语料无该文件时内建面为空集并打印降级提示）；插件提供面 = plugin.json
  ``capabilities.services[].name`` + ``provides.capabilities[].namespace``
  （契约键 = namespace，非 wire tool_prefix）。
- **执法**：调用 ns ⊄ 声明 ∪ 内建 ∪ 自提供 → exit 1（违规行 ``plugin=<目录>
  file=<路径>:<行>`` 机器可归因 + 修复指引）；requires_services 声明无全仓
  调用点且非 "packaging"（平台依赖豁免，方案 §3.1）→ 警告不阻断。
- **粒度**：ns 角色级（``ns.method`` 精确对账属 G2 可用性闸职责，本闸不重复）。

用法：
    python scripts/check_service_declarations.py                    # 全仓执法（CI 闸）
    python scripts/check_service_declarations.py --census           # 普查报表（不执法）
    python scripts/check_service_declarations.py --plugin <dir>     # 单插件模式（T5.4）
    python scripts/check_service_declarations.py --scan-root <dir>  # 装
    # 机版用户空间等仓外语料（须同布局 <root>/shared/<id>/）
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = ROOT / "plugins"
REGISTRY_RS = ROOT / "kernel" / "crates" / "plugin-loader" / "src" / "registry.rs"

# 平台依赖豁免 ns（方案 §3.1：ADR 2026-08-18 决策 10，语言级依赖管理）。
PLATFORM_DEPENDENCY_NS = "packaging"

# 内建表哨兵（fail-closed 防永绿假闸：表解析出空/残表时必须红）。
TABLE_SENTINELS = {"pipeline-executor", "event-bus", "db-read", "chat", "db-admin"}

# 与 check_shared_root_imports.py 同一套目录排除口径（剪枝式遍历，不进巨目录）。
EXCLUDE_DIRS = {
    "__pycache__",
    "node_modules",
    "runtime",
    "target",
    ".venv",
    "venv",
    ".venv-hindsight",
    ".ai_workspaces",
    # 测试不属生产码：tests/ 目录整树豁免；平铺测试件按文件名判（下方）
    "tests",
}

# KERNEL_PROVIDED_SERVICES 表项（"ns", &[...方法字面量...])；方法平铺无嵌套。
_TABLE_ENTRY_RE = re.compile(r"\(\s*\"([^\"]+)\"\s*,\s*&\[(.*?)\]", re.DOTALL)
_STRING_LIT_RE = re.compile(r"\"([^\"]+)\"")
# _root_api 出口映射表项（"名字": "承载模块"）。
_ROOT_API_PAIR_RE = re.compile(r"\"([^\"]+)\"\s*:\s*\"([^\"]+)\"")


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
        # 扫描与活进程建/删目录竞态（同族）：entry 消失或无权限 → 跳过
        try:
            children = list(cur.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for p in children:
            if p.is_dir():
                if p.name in EXCLUDE_DIRS:
                    continue
                stack.append(p)
            elif p.suffix == ".py" and not is_test_file(p):
                out.append(p)
    return sorted(out)


def _dict_block(text: str, marker: str) -> str:
    """取 ``marker ... { ... }`` 的花括号块内容（marker 缺位/不闭合返回空串）。"""
    start = text.find(marker)
    if start < 0:
        return ""
    block_start = text.find("{", start)
    block_end = text.find("}", block_start)
    if block_start < 0 or block_end < 0:
        return ""
    return text[block_start + 1 : block_end]


def _bracket_block(text: str, marker: str) -> str:
    """取 ``marker ... = [ ... ]`` 赋值方括号平衡块内容（含嵌套内层数组）。

    起点锚在 ``=`` 之后（跳过 ``&[(&str, &[&str])]`` 型注解里的括号对）。
    """
    start = text.find(marker)
    if start < 0:
        return ""
    eq = text.find("=", start)
    if eq < 0:
        return ""
    block_start = text.find("[", eq)
    if block_start < 0:
        return ""
    depth = 0
    for i in range(block_start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[block_start + 1 : i]
    return ""


def parse_kernel_table(fail_closed: bool = True) -> dict[str, set[str]]:
    """解析 registry.rs 的 KERNEL_PROVIDED_SERVICES 字面量表。

    fail_closed（仓内语料默认）：解析失败/表为空/哨兵 ns 缺位 → exit 2
    （表是真源，残表不可作免声明依据）。fail_closed=False（装
    机版用户空间等仓外语料，registry.rs 不在语料树内）：打印降级提示返回
    空表——此时一切 ns 都须显式声明，执法只紧不松。
    """
    if not REGISTRY_RS.is_file():
        if fail_closed:
            print(f"fail-closed: {REGISTRY_RS}: 文件缺失", file=sys.stderr)
            raise SystemExit(2)
        print("提示: 仓外语料（无 registry.rs）→ 内建面按空集执法（一切 ns 须声明）")
        return {}
    text = REGISTRY_RS.read_text(encoding="utf-8")
    block = _bracket_block(text, "const KERNEL_PROVIDED_SERVICES")
    table: dict[str, set[str]] = {}
    for m in _TABLE_ENTRY_RE.finditer(block):
        ns, methods_raw = m.group(1), m.group(2)
        methods = set(_STRING_LIT_RE.findall(methods_raw))
        if not methods:
            print(
                f"fail-closed: {REGISTRY_RS}: 内建表 ns `{ns}` 方法集为空（残表）",
                file=sys.stderr,
            )
            raise SystemExit(2)
        table[ns] = methods
    missing = TABLE_SENTINELS - set(table)
    if not table or missing:
        print(
            f"fail-closed: {REGISTRY_RS}: 内建表解析异常（哨兵缺位 {sorted(missing)}，仅解析出 {len(table)} ns）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return table


def build_root_api_map(shared_dir: Path) -> dict[str, str]:
    """_root_api 出口映射（_EXPORTS + _MODULE_EXPORTS，名字 → 承载模块）。"""
    mapping: dict[str, str] = {}
    init = shared_dir / "_root_api" / "__init__.py"
    if not init.is_file():
        return mapping
    text = init.read_text(encoding="utf-8")
    for name in ("_EXPORTS", "_MODULE_EXPORTS"):
        for m in _ROOT_API_PAIR_RE.finditer(_dict_block(text, f"{name}: dict[str, str]")):
            mapping[m.group(1)] = m.group(2)
    return mapping


def load_manifests(shared_dir: Path) -> dict[str, dict]:
    """插件目录名 → manifest dict（<shared>/<dir>/plugin.json）。

    键 = **目录名**（文件归属的文件系统真值；manifest id 可与目录名不同，
    如 approval/ ↔ id=approval_service）。manifest 是契约面真源：解析失败
    fail-closed exit 2（与内核 G2 同口径）。
    """
    manifests: dict[str, dict] = {}
    for manifest_path in sorted(shared_dir.glob("*/plugin.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"fail-closed: {manifest_path}: 不可解析（{exc}）", file=sys.stderr)
            raise SystemExit(2) from exc
        manifests[manifest_path.parent.name] = data
    return manifests


def provided_namespaces(manifest: dict) -> set[str]:
    """插件提供面 ns 集合：capabilities.services[].name 的 ns 段 +
    provides.capabilities[].namespace（契约键）。"""
    out: set[str] = set()
    for svc in manifest.get("capabilities", {}).get("services", []):
        ns = svc.get("name", "").split(".", 1)[0]
        if ns:
            out.add(ns)
    for cap in (manifest.get("provides") or {}).get("capabilities", []):
        ns = cap.get("namespace", "")
        if ns:
            out.add(ns)
    return out


def required_services(manifest: dict) -> list[str]:
    return list(manifest.get("requires_services", []))


def import_keys_of(tree: ast.Module, root_api_map: dict[str, str]) -> set[str]:
    """文件 AST 中的库模块 import 键集合（归属归因用）。

    - ``import a.b`` / ``from a.b import x`` → 完整 dotted 与顶级段；
    - ``from a.b import x`` 追加 ``a.b.x``（x 为子模块形态）；
    - ``from _root_api import sym`` → 经 _EXPORTS/_MODULE_EXPORTS 映射承载模块；
    - 相对 import（level ≥ 1）= 插件包内部消费，不属跨库，跳过。
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                keys.add(alias.name)
                keys.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            keys.add(node.module)
            keys.add(node.module.split(".")[0])
            for alias in node.names:
                keys.add(f"{node.module}.{alias.name}")
            if node.module == "_root_api":
                for alias in node.names:
                    target = root_api_map.get(alias.name)
                    if target:
                        keys.add(target)
    return keys


def library_module_keys(path: Path, shared_dir: Path) -> set[str]:
    """无主库文件的 import 键（消费方按这些名字 import 到该文件）。

    - 共享根裸模块（shared/db_read.py）→ ``db_read``；
    - 库目录直下件（channel_common/inbound_bridge.py）→ dotted + 裸名 stem
      （渠道插件 sys.path 注入后裸名 import 形态）；
    - 更深文件（_libs/pipeline_base/plugin.py）→ 仅全 dotted；
    - shared 外的（sdk）→ stem 兜底（无跨库 import 预期）。
    """
    try:
        rel = path.relative_to(shared_dir)
    except ValueError:
        return {path.stem}
    if len(rel.parts) == 1:
        return {rel.parts[0]}
    dotted = ".".join(rel.with_suffix("").parts)
    if len(rel.parts) == 2:
        return {dotted, path.stem}
    return {dotted}


def module_string_consts(tree: ast.Module) -> dict[str, str]:
    """模块级字符串常量单赋值（``_CAPABILITY = "db-admin"``，含 AnnAssign）。"""
    consts: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets, value = [node.targets[0]], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


def for_loop_bindings(tree: ast.Module, consts: dict[str, str]) -> list[tuple[str, list[str], int, int]]:
    """``for cap in ("a", "b", CONST):`` 字面序列绑定 → (名, ns 列表, 起行, 止行)。"""
    out: list[tuple[str, list[str], int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        if not isinstance(node.target, ast.Name) or not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        elems: list[str] = []
        for elt in node.iter.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                elems.append(elt.value)
            elif isinstance(elt, ast.Name) and elt.id in consts:
                elems.append(consts[elt.id])
        if elems:
            out.append((node.target.id, elems, node.lineno, node.end_lineno or node.lineno))
    return out


def resolve_call_arg(
    arg: ast.expr, consts: dict[str, str], loops: list[tuple[str, list[str], int, int]]
) -> list[str] | None:
    """get_capability 实参 → ns 列表；识别不到返回 None（动态边界）。"""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.Name):
        if arg.id in consts:
            return [consts[arg.id]]
        hits: list[str] = []
        for name, elems, start, end in loops:
            if name == arg.id and start <= arg.lineno <= end:
                hits.extend(elems)
        if hits:
            return hits
    return None


def call_sites_of(
    tree: ast.Module,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], int, set[str]]:
    """AST 调用点普查 → (角色级 ns 点, 端点 ns 点, 动态边界计数, 字符串常量集)。

    角色级点 = ``*get_capability("ns")`` 形态；端点点 = ``.call("ns.method"`` /
    ``.notify("ns.method"`` 全字面两段式。字符串常量集供反向对账作信封消费证据
    （isolation.create_env / llm.complete_stream 经 tool-executor 信封
    ``tool_name`` 嵌套调用，静态无直接 capability 点——方案 §3.1 保留样例）。
    """
    roles: list[tuple[int, str]] = []
    endpoints: list[tuple[int, str]] = []
    string_consts: set[str] = set()
    dynamic = 0
    consts = module_string_consts(tree)
    loops = for_loop_bindings(tree, consts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_consts.add(node.value)
            continue
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr.endswith("get_capability"):
            if node.args:
                resolved = resolve_call_arg(node.args[0], consts, loops)
                if resolved is None:
                    dynamic += 1
                else:
                    roles.extend((node.lineno, ns) for ns in resolved)
        elif attr in ("call", "notify") and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                ns = arg.value.split(".", 1)[0]
                if "." in arg.value and ns:
                    endpoints.append((node.lineno, ns))
    return roles, endpoints, dynamic, string_consts


def reaches(
    target: Path,
    importer_keys: set[str],
    lib_key_index: dict[str, set[Path]],
    lib_imports: dict[Path, set[str]],
) -> bool:
    """import 键集合是否（经库间传递 import）到达 target 库文件。"""
    frontier = set(importer_keys)
    visited: set[str] = set()
    while frontier:
        key = frontier.pop()
        if key in visited:
            continue
        visited.add(key)
        for hit in lib_key_index.get(key, ()):
            if hit == target:
                return True
            frontier |= lib_imports[hit]
    return False


def _display_path(path: Path, scan_root: Path) -> str:
    """违规行路径展示：scan_root 相对优先，退化为绝对 posix。"""
    try:
        return path.relative_to(scan_root).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="服务声明静态闸（T5.3）")
    parser.add_argument("--census", action="store_true", help="普查报表模式（不执法）")
    parser.add_argument(
        "--plugin",
        help="单插件模式（T5.4 安装期）：只报与该插件目录相关的违规（仍加载全仓语料做归属）",
    )
    parser.add_argument(
        "--scan-root",
        default=str(DEFAULT_SCAN_ROOT),
        help="扫描根（默认仓内 plugins/；装机版用户空间传 <user_root>/plugins）",
    )
    args = parser.parse_args()
    scan_root = Path(args.scan_root).resolve()
    shared_dir = scan_root / "shared" if (scan_root / "shared").is_dir() else scan_root

    kernel_table = parse_kernel_table(fail_closed=scan_root == DEFAULT_SCAN_ROOT)
    manifests = load_manifests(shared_dir)
    root_api_map = build_root_api_map(shared_dir)

    sources = iter_sources(scan_root)
    parsed: dict[Path, ast.Module] = {}
    for path in sources:
        try:
            parsed[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"fail-closed: {path}: 语法解析失败（{exc}）", file=sys.stderr)
            raise SystemExit(2) from exc

    def owner_of(path: Path) -> str | None:
        """文件属主插件目录名：文件到 shared 根沿途最近一个 manifest 目录。"""
        for parent in path.parents:
            if parent in (shared_dir, scan_root):
                return None
            if parent in manifest_dirs:
                return parent.name
        return None

    manifest_dirs = {p.parent.resolve() for p in shared_dir.glob("*/plugin.json")}

    # 库文件（无主）键索引与 import 边（归属归因用）
    lib_files = [p for p in sources if owner_of(p) is None]
    lib_key_index: dict[str, set[Path]] = {}
    lib_imports: dict[Path, set[str]] = {}
    for path in lib_files:
        for key in library_module_keys(path, shared_dir):
            lib_key_index.setdefault(key, set()).add(path)
        lib_imports[path] = import_keys_of(parsed[path], root_api_map)
    plugin_import_keys: dict[str, set[str]] = {}
    for path in sources:
        owner = owner_of(path)
        if owner is not None:
            plugin_import_keys.setdefault(owner, set()).update(import_keys_of(parsed[path], root_api_map))

    # 逐插件普查
    plugin_required: dict[str, set[str]] = {}
    total_dynamic = 0
    corpus_roles_ns: set[str] = set()
    corpus_endpoint_ns: set[str] = set()
    corpus_string_consts: set[str] = set()
    violation_lines: list[str] = []
    lib_ns_by_file: dict[Path, set[str]] = {}

    def violation(owner: str, path: Path, lineno: int, ns: str, detail: tuple[str, str]) -> None:
        via, advice = detail
        rel = _display_path(path, scan_root)
        violation_lines.append(
            f"VIOLATION: plugin={owner} file={rel}:{lineno} 调用未声明能力 `{ns}`（{via}）—— {advice}"
        )

    for path in sources:
        roles, endpoints, dynamic, string_consts = call_sites_of(parsed[path])
        corpus_string_consts |= string_consts
        total_dynamic += dynamic
        owner = owner_of(path)
        if owner is None:
            lib_ns_by_file[path] = {ns for _, ns in roles} | {ns for _, ns in endpoints}
            continue
        manifest = manifests[owner]
        allowed = (
            set(required_services(manifest))
            | {item.split(".", 1)[0] for item in required_services(manifest)}
            | set(kernel_table)
            | provided_namespaces(manifest)
        )
        req = plugin_required.setdefault(owner, set())
        for lineno, ns in roles:
            corpus_roles_ns.add(ns)
            req.add(ns)
            if ns not in allowed:
                violation(
                    owner,
                    path,
                    lineno,
                    ns,
                    (
                        "get_capability",
                        f"在 plugins/shared/{owner}/plugin.json 的 requires_services 补 "
                        f'"{ns}"（内核内建免声明，见 KERNEL_PROVIDED_SERVICES）',
                    ),
                )
        for lineno, ns in endpoints:
            corpus_endpoint_ns.add(ns)
            req.add(ns)
            if ns not in allowed:
                violation(
                    owner,
                    path,
                    lineno,
                    ns,
                    (
                        "句柄 .call 端点",
                        f'在 plugins/shared/{owner}/plugin.json 的 requires_services 补 "{ns}.<method>"',
                    ),
                )

    # 库文件的非内建调用点按 import 闭包归给消费插件（声明落消费方，channel_common 先例）
    for path in lib_files:
        non_kernel = {ns for ns in lib_ns_by_file.get(path, set()) if ns not in kernel_table}
        if not non_kernel:
            continue
        for owner, keys in plugin_import_keys.items():
            if not reaches(path, keys, lib_key_index, lib_imports):
                continue
            manifest = manifests[owner]
            allowed = {item.split(".", 1)[0] for item in required_services(manifest)} | provided_namespaces(manifest)
            for ns in sorted(non_kernel - allowed):
                violation_lines.append(
                    f"VIOLATION: plugin={owner} "
                    f"file={_display_path(path, scan_root)}:0 消费共享库的未声明能力 "
                    f'`{ns}` —— 在 plugin.json 的 requires_services 补 "{ns}"'
                    f"（channel_common 等共享模块的声明落各消费插件）"
                )

    if args.census:
        print(f"== 普查报表（扫描 {len(sources)} 文件，内建表 {len(kernel_table)} ns）==")
        print(f"内建 ns: {', '.join(sorted(kernel_table))}")
        print(f"全仓调用 ns（角色级）: {', '.join(sorted(corpus_roles_ns)) or '（无）'}")
        print(f"全仓调用 ns（端点级）: {', '.join(sorted(corpus_endpoint_ns)) or '（无）'}")
        for owner in sorted(manifests):
            req_sorted = sorted(plugin_required.get(owner, set()))
            declared = sorted(required_services(manifests[owner]))
            if req_sorted or [d for d in declared if d != PLATFORM_DEPENDENCY_NS]:
                print(f"{owner}: 调用={req_sorted} 声明={declared}")
        lib_lines = sorted(f"{_display_path(p, scan_root)} -> {sorted(ns)}" for p, ns in lib_ns_by_file.items() if ns)
        if lib_lines:
            print("无主库文件调用点（按 import 闭包归消费插件）:")
            for line in lib_lines:
                print(f"  {line}")
        print(f"动态边界调用点（运行时 T5.5 兜底）: {total_dynamic}")
        return 0

    # 反向对账：声明无全仓调用点（平台依赖豁免）→ 警告不阻断
    warnings: list[str] = []
    for owner in sorted(manifests):
        for item in required_services(manifests[owner]):
            ns = item.split(".", 1)[0]
            if ns == PLATFORM_DEPENDENCY_NS or ns in kernel_table:
                continue
            if ns not in corpus_roles_ns and ns not in corpus_endpoint_ns and item not in corpus_string_consts:
                warnings.append(
                    f'WARNING: plugin={owner} 声明 requires_services "{item}" '
                    f"但全仓无该能力调用点（平台依赖豁免除外；确认后可删）"
                )

    if args.plugin:
        target = Path(args.plugin).resolve().name
        violation_lines = [v for v in violation_lines if f"plugin={target} " in v + " "]

    if violation_lines:
        print(f"服务声明违规 {len(violation_lines)} 处：")
        for v in violation_lines:
            print(v)
        print("闭包口径见 docs/working/资源注册与索引收敛方案_20261016.md §二。")
        return 1
    for w in warnings:
        print(w)
    print(
        f"服务声明零违规（插件 {len(manifests)} 家 / 生产码 {len(sources)} 文件 / "
        f"内建 {len(kernel_table)} ns；动态边界 {total_dynamic} 处由运行时对账兜底）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
