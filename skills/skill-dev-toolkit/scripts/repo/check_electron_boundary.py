#!/usr/bin/env python
"""Electron 主/渲染进程导入边界闸（ADR 2026-11-16-lint-error-ratchet-electron-boundary-gate）。

背景：Electron 桌面壳把主进程代码导入渲染进程会阻塞渲染线程（同型事故在
Cursor 客户端被实证，Grok Bot 团队对照审查引入本闸）。本仓现状双向零导入，
从零起步的零容忍集合规则（无基线，同 shared-root-imports）。

规则（全部零容忍，任何一处即红）：
- R1 目录互导禁令：electron/** 与 frontend/src/** 双向禁止互相导入
  （相对路径先经词法归一再判归属，路径段含 "electron" 字样但归一后落在
  frontend 树的（如 ``../types/electron``）不算）；
- R2 渲染侧 electron 包运行时引用禁令：frontend/src 生产码禁
  ``from "electron"`` / ``require("electron")`` / ``import("electron")`` /
  ``electron:*`` 子路径 / ``@electron/remote`` / ``window.require(...)``——
  唯一合法耦合面是 preload 经 contextBridge 暴露的 window.electronAPI
  （类型走 src/types/electron.d.ts 环境声明）；``import type`` /
  ``export type`` 形态豁免（构建期擦除，无运行时耦合）；
- R3 preload 最小面禁令：electron/preload.ts 禁 import 本地兄弟模块——
  预加载脚本只允许 "electron" 包与 Node 内置，主进程能力一律经 ipcMain
  通道下发（现状 preload 仅 import "electron"，与 main 共享常量靠注释锚定）。

扫描形态覆盖（防绕过清单，同 check_arch_dependencies 四形态口径）：静态
import/export-from、副作用 import "x"、require()、动态 import()；type-only
语句先于通用 from 匹配收集豁免区间。注释（块/行）先剥离，防止文档注释里
的示例路径误报；字符串字面量里形似 import 的片段不匹配本闸上下文模式。

退出码：0 = 零违规；1 = 存在违规；2 = fail-closed（扫描根缺失/文件不可读）。
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
ELECTRON_DIR = "electron"
FRONTEND_SRC_DIR = "frontend/src"
PRELOAD_REL = "electron/preload.ts"

# 扫描剪枝：测试面（白盒消费不扫）与生成/依赖目录
EXCLUDE_DIR_PARTS = ("__tests__", "node_modules", "prereqs", "dist", "coverage")

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)//[^\n]*")
# type-only 语句（import type / export type ... from "x"）：构建期擦除，豁免。
# 量词封顶 400 字符容忍多行 specifier 列表；超长语句按运行时导入处理（偏保守）。
_TYPE_ONLY_FROM = re.compile(
    r"\b(?:import|export)\s+type\b[\s\S]{0,400}?\bfrom\s*[\"']([^\"']+)[\"']"
)
# 通用 from 形态：静态 import ... from / export ... from / export * from
_FROM = re.compile(r"\bfrom\s*[\"']([^\"']+)[\"']")
# require 形态（含 window.require 的参数面；window.require 本体另判）
_REQUIRE = re.compile(r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
# 动态 import() 形态
_DYNAMIC = re.compile(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
# 副作用 import "x" 形态（import 后紧跟引号，不与 import() 混淆）
_SIDE_EFFECT = re.compile(r"\bimport\s*[\"']([^\"']+)[\"']")
# window.require 本体（渲染侧 Node 集成后门，任何 specifier 都算 R2）
_WINDOW_REQUIRE = re.compile(r"\bwindow\s*\.\s*require\s*\(")

ELECTRON_PACKAGE_SPECS = ("electron:", "@electron/")


def strip_comments(text: str) -> str:
    """剥离块/行注释（导入模式的误报源；URL 字符串受损不影响本闸模式）。"""
    text = _BLOCK_COMMENT.sub(lambda m: " " * len(m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), text)


def iter_scan_files(root: Path, subdir: str) -> list[Path]:
    """枚举扫描面：electron/ 全树（ts）与 frontend/src/（ts/tsx），测试面剪枝。"""
    base = root / subdir
    if not base.is_dir():
        return []
    files: list[Path] = []
    for p in sorted(base.rglob("*.ts*")):
        if not p.is_file():
            continue
        name = p.name
        if name.endswith((".test.ts", ".test.tsx", ".d.ts")):
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in EXCLUDE_DIR_PARTS for part in rel_parts[:-1]):
            continue
        files.append(p)
    return files


def resolve_spec(spec: str, file_rel_posix: str) -> str:
    """相对导入词法归一为仓根 POSIX 路径；裸名/包名原样返回。"""
    if not spec.startswith("./") and not spec.startswith("../"):
        return spec
    base_dir = posixpath.dirname(file_rel_posix)
    return posixpath.normpath(posixpath.join(base_dir, spec))


def in_tree(resolved: str, tree: str) -> bool:
    return resolved == tree or resolved.startswith(tree + "/")


def collect_specifiers(text: str) -> list[tuple[str, int]]:
    """收集全部导入 specifier 与行号（type-only 豁免区间先采集后跳过）。"""
    clean = strip_comments(text)
    exempt_spans = [(m.start(), m.end()) for m in _TYPE_ONLY_FROM.finditer(clean)]
    hits: list[tuple[str, int]] = []

    def line_of(pos: int) -> int:
        return clean.count("\n", 0, pos) + 1

    for m in _FROM.finditer(clean):
        if any(s <= m.start() < e for s, e in exempt_spans):
            continue
        hits.append((m.group(1), line_of(m.start(1))))
    for pattern in (_REQUIRE, _DYNAMIC, _SIDE_EFFECT):
        for m in pattern.finditer(clean):
            hits.append((m.group(1), line_of(m.start(1))))
    return hits


def classify_file(root: Path, path: Path) -> tuple[list[str], int]:
    """扫描单文件，返回 (违规列表, 导入条数)。文件不可读抛 OSError。"""
    file_rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="strict")
    violations: list[str] = []
    specs = collect_specifiers(text)
    is_frontend = in_tree(file_rel, FRONTEND_SRC_DIR)
    is_preload = file_rel == PRELOAD_REL

    if is_frontend:
        for m in _WINDOW_REQUIRE.finditer(strip_comments(text)):
            violations.append(
                f"R2 {file_rel}:{clean_line(text, m.start())} window.require() 渲染侧 Node 后门"
                "（唯一合法面 = preload 暴露的 window.electronAPI）"
            )

    for spec, line in specs:
        resolved = resolve_spec(spec, file_rel)
        # 树归属判定只对相对路径做词法归一：裸包名 "electron" 是 npm 包而非
        # electron/ 目录（测试实红修正：裸名判树会把 R2 误升 R1）
        is_relative = spec.startswith("./") or spec.startswith("../")
        if is_relative and in_tree(resolved, FRONTEND_SRC_DIR) and in_tree(file_rel, ELECTRON_DIR):
            violations.append(
                f"R1 {file_rel}:{line} electron 主进程树导入 frontend（{spec}）——"
                "渲染代码进主进程树/反向皆禁，共享逻辑抽独立包或经 IPC"
            )
        elif is_relative and in_tree(resolved, ELECTRON_DIR) and is_frontend:
            violations.append(
                f"R1 {file_rel}:{line} frontend 导入 electron 树（{spec}）——"
                "主进程代码进渲染 bundle 会阻塞渲染线程；走 window.electronAPI"
            )
        elif is_frontend and (
            spec == "electron" or spec.startswith(ELECTRON_PACKAGE_SPECS)
        ):
            violations.append(
                f"R2 {file_rel}:{line} 渲染侧运行时引用 electron 包（{spec}）——"
                "仅 preload 可用；类型走 src/types/electron.d.ts（import type 豁免）"
            )
        elif is_preload and (spec.startswith("./") or spec.startswith("../")):
            violations.append(
                f"R3 {file_rel}:{line} preload 导入本地模块（{spec}）——"
                "预加载保持最小面，主进程能力经 ipcMain 通道下发"
            )
    return violations, len(specs)


def clean_line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Electron 导入边界闸（R1/R2/R3 零容忍）")
    parser.add_argument("--root", default=str(ROOT_DEFAULT), help="仓库根（默认脚本上级）")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    scan_trees = (ELECTRON_DIR, FRONTEND_SRC_DIR)
    missing = [t for t in scan_trees if not (root / t).is_dir()]
    if missing:
        print(f"electron-boundary: 扫描根缺失 fail-closed: {missing}")
        return 2

    total_files = 0
    total_specs = 0
    violations: list[str] = []
    for tree in scan_trees:
        for path in iter_scan_files(root, tree):
            try:
                file_violations, spec_count = classify_file(root, path)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"electron-boundary: 文件不可读 fail-closed: {path} ({exc})")
                return 2
            total_files += 1
            total_specs += spec_count
            violations.extend(file_violations)

    if violations:
        print(f"electron-boundary: {len(violations)} 处边界违规（扫描 {total_files} 文件）：\n")
        for v in sorted(violations):
            print(f"  - {v}")
        return 1

    print(
        f"electron-boundary: 通过（{total_files} 文件 / {total_specs} 导入条目，"
        "R1 目录互导 / R2 渲染侧 electron 包 / R3 preload 最小面 全部零违规）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
