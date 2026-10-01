#!/usr/bin/env python3
"""插件配置写面机械门禁（2026-09-28 方案批次 E1，防回潮）。

公理（docs/working/LLM配置改不生效修复方案_20260928.md §3）：配置读写同源
落用户空间——插件禁止自拼路径直写 config/.env/data 配置面，读写必须走内核
单一配置面（/api/v1/plugins/{id}/config/{file_id}、/api/v1/config/env）或
用户空间解析器（plugins/shared/user_space.py）。

规则（锚定**写动作**，非函数名——_resolve_project_root 等解析器本身被
context_build/param_inject 等合法读面复用，按名扫会误伤）：
  同一文件同时命中
  a) 写动作调用（write_text/write_bytes/os.replace/shutil.copy*/atomic_write*/
     yaml.(safe_)dump/open(w 模式)）
  b) 配置面路径字面量（config/plugins、config/models、".env"、data/、permission_modes）
  且文件内无豁免标记 → 违规。

豁免：违规行同文件内存在 ``config-write-surface-exempt: <理由>`` 注释即放行
（须写明为什么该写面合法，如「真值在用户空间，字面量为不可用回落防御」）。

用法：
    python scripts/check_plugin_config_write_surface.py [--root plugins] [--verbose]
退出码：0 = 无违规；1 = 有违规或扫描异常。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# 写动作特征（锚定写，不锚定解析器名）
WRITE_RE = re.compile(
    r"\.write_text\(|\.write_bytes\(|os\.replace\(|shutil\.copy(?:2|tree)?\("
    r"|atomic_write|yaml\.(?:safe_)?dump\(|open\([^)]*['\"][wax]\+?['\"]"
)
# 配置/密钥/持久化数据面路径字面量
CONFIG_PATH_RE = re.compile(
    r"config/plugins|config/models|['\"]\.env['\"]|/ \* [\"']\.env"
    r"|['\"]data[/\\]|[/\\]data[/\\]|permission_modes"
)
# 豁免标记
EXEMPT_MARK = "config-write-surface-exempt:"

# 目录/文件名排除：生成物、第三方 vendored、测试（测试允许造临时配置现场）
EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "runtime",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    "htmlcov",
    ".wt-debug",
}
EXCLUDE_DIR_PREFIXES = (".venv", ".wt-")
EXCLude_test_re = re.compile(r"(^test_[^/\\]*\.py$|conftest\.py$)")


def iter_plugin_py_files(root: Path) -> list[Path]:
    """os.walk + 剪枝遍历（rglob 会先走完 .venv/node_modules 才过滤，慢到不可用）。"""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES and not d.startswith(EXCLUDE_DIR_PREFIXES)]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            if EXCLude_test_re.search(name):
                continue
            files.append(Path(dirpath) / name)
    return sorted(files)


def check_file(path: Path) -> tuple[bool, str]:
    """返回 (违规?, 说明)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return True, f"读取失败（应人工核查）: {exc}"
    has_write = bool(WRITE_RE.search(text))
    has_config_path = bool(CONFIG_PATH_RE.search(text))
    if not (has_write and has_config_path):
        return False, ""
    for line in text.splitlines():
        if EXEMPT_MARK in line:
            return False, f"豁免（{line.split(EXEMPT_MARK, 1)[1].strip()[:60]}）"
    return True, "文件同时含配置面路径字面量与写动作——配置写面必须收口用户空间单一解析器/内核端点"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="plugins", help="扫描根目录（默认 plugins）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[plugin-config-write] 扫描根不存在: {root}")
        return 1

    violations: list[tuple[Path, str]] = []
    exempted = 0
    scanned = 0
    for path in iter_plugin_py_files(root):
        scanned += 1
        bad, note = check_file(path)
        if not bad:
            if note.startswith("豁免"):
                exempted += 1
                if args.verbose:
                    print(f"[exempt] {path}: {note}")
            continue
        violations.append((path, note))

    print(f"[plugin-config-write] 扫描 {scanned} 文件 | 豁免 {exempted} | 违规 {len(violations)}")
    for path, note in violations:
        print(f"[violation] {path}: {note}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
