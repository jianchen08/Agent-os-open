#!/usr/bin/env python3
"""裸名导入共享根防漂移闸（审查总册 E2，2026-10-16 拍板「API 化+死代码清理」）。

plugins/shared 根直下 *.py 裸模块的公共 API 已收口到显式出口
``plugins/shared/_root_api/``（__init__.py 逐模块 re-export + __all__，
R2 API 化刀，见 docs/working/E2共享根分诊_台账.md）。生产码消费方一律
``from _root_api import ...``；裸名 import 实现模块 = 绕过出口的漂移欠账。

口径：
- **禁区动态枚举**：共享根直下 ``*.py`` 即禁（新落根模块未出口即对消费方
  违规，无需改本脚本）；出口名单由 ``_root_api`` 自持，本闸不校验出口内容。
- **豁免（显式面）**：
  - 共享根直下文件本体——实现区兄弟 import（tenant_data→user_space 先例）；
  - ``plugins/shared/_root_api/**``——出口本体（唯一合法裸名 import 点）；
  - 测试文件（tests/ 目录、test_*.py / *_test.py、conftest.py）——白盒消费，
    conftest 自推 sys.path 既有形态（台账 R3 遗留③）。
- **扫描面**：plugins/ 与 scripts/ 全部生产码 .py（剪枝式遍历排除巨目录）；
  AST 解析，注释/文档串不误报；扫描树语法错 fail-closed exit 2。
- **违规输出**：逐条 file:line + 裸名真名 + 出口指引，exit 1。

用法：python scripts/check_shared_root_imports.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SHARED_ROOT = ROOT / "plugins" / "shared"
OUTLET_DIR_NAME = "_root_api"
SCAN_ROOTS = (ROOT / "plugins", ROOT / "scripts")

# 与 check_kernel_db_direct_reads 同一套目录排除口径（剪枝式遍历，不进巨目录）
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
SCAN_SUFFIX = ".py"


def is_test_file(path: Path) -> bool:
    """平铺布局测试件：test_*.py / *_test.py / conftest.py。"""
    return path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py"


def banned_modules() -> list[str]:
    """禁区模块名 = 共享根直下全部 .py 裸模块（动态枚举，新落即管）。"""
    if not SHARED_ROOT.is_dir():
        return []
    return sorted(p.stem for p in SHARED_ROOT.iterdir() if p.suffix == SCAN_SUFFIX)


def is_exempt(path: Path) -> bool:
    """豁免面：共享根本体（直下文件）、出口包、测试文件。"""
    rel = path.relative_to(ROOT)
    if rel.parent == Path("plugins/shared"):
        return True
    if rel.parts[:3] == ("plugins", "shared", OUTLET_DIR_NAME):
        return True
    return is_test_file(path)


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
            elif p.suffix == SCAN_SUFFIX and not is_test_file(p) and not is_exempt(p):
                out.append(p)
    return out


def bare_root_imports(path: Path, banned: set[str]) -> list[tuple[int, str]]:
    """AST 解析单文件，返回 (行号, 裸名共享根模块) 列表。"""
    hits: list[tuple[int, str]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"fail-closed: {path}: 语法解析失败（{exc}）", file=sys.stderr)
        raise SystemExit(2) from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in banned:
                    hits.append((node.lineno, top))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in banned:
                    hits.append((node.lineno, top))
    return hits


def main() -> int:
    banned = set(banned_modules())
    violations: list[str] = []
    for scan_root in SCAN_ROOTS:
        for path in iter_sources(scan_root):
            for lineno, mod in bare_root_imports(path, banned):
                rel = path.relative_to(ROOT).as_posix()
                violations.append(
                    f"{rel}:{lineno}: 裸名导入共享根模块 `{mod}` —— "
                    f"改从显式出口导入：`from _root_api import ...`"
                    f"（E2 闸；出口名单见 plugins/shared/{OUTLET_DIR_NAME}/__init__.py）"
                )
    if violations:
        print(f"共享根裸名导入违规 {len(violations)} 处：")
        for v in violations:
            print(f"  {v}")
        return 1
    print(f"共享根裸名导入零违规（禁区 {len(banned)} 件，出口 plugins/shared/{OUTLET_DIR_NAME}/）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
