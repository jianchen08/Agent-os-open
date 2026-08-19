#!/usr/bin/env python3
"""插件 uv 单轨迁移脚手架（包管理体系 P1，2026-08-18）

目标：把 plugins/shared 下所有 Python 插件迁为 **uv 发行包**（pyproject.toml +
uv.lock，单轨，不留 plain 兼容轨；合同见
`docs/working/插件依赖与包管理体系完善方案_20260818.md` §P1/P4/P5）。

本脚本只做**契约语料**层：为缺少 pyproject 的 Python 插件生成最小
pyproject.toml 并 `uv lock` 产出 uv.lock（`uv lock` 不创建 .venv、零磁盘负担），
使每个插件的打包声明显式化、可锁版本、可被 `python_packager`（packaging.python.*
服务）消费。

**边界（诚实标注）**：
- 运行面（invoker 从 `python server.py` 切换到 per-plugin venv/uv run）**不在本
  脚本**——那需要 boot 可验环境 + SDK editable 安装策略，是单独 go/no-go 批次；
  未切换前 plain 运行径照常可用，本批不破坏任何现有启动。
- dependencies 默认只含 `agentos-plugin-sdk>=0.2.0`（样板同 builtin_tools）；
  第三方依赖用静态 import 扫描提示（stdlib/agentos_plugin_sdk/本地路径过滤），
  **人工确认后填入**——盲摘依赖 = 运行面切换时 96 个插件可能一起坏。

用法：
    python scripts/migrate_plugins_to_uv.py [--apply] [--skip-lock] [--root plugins/shared]
默认 dry-run：打印将生成的插件与新 import 提示，不写任何文件。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 loader 语料扫描同款 SKIP。
# .ai_workspaces：未跟踪运行时工作区（workspace_lifecycle 插件目录内实测存在，
# skills 脚本含 yaml/sqlalchemy import，曾致 dry-run 复扫误报——批 B §11.4.3）。
# .venv-hindsight：hindsight_memory 第二 venv（批 C 双 venv 设计，site-packages 勿扫）。
SKIP_DIRS = {
    "node_modules", "__pycache__", "target", "runtime", "data", "dsh_plugins",
    ".venv", ".venv-hindsight", "src", ".ai_workspaces",
}

# 不再重复生成的样板（已自持 pyproject 的插件）
ALREADY_UV = {"builtin_tools"}

# 标准库 + SDK + 本地白名单（不当作第三方依赖提示）
STDLIB = {
    "abc", "argparse", "asyncio", "base64", "collections", "contextlib", "copy",
    "csv", "datetime", "enum", "functools", "hashlib", "hmac", "html", "http",
    "importlib", "inspect", "io", "itertools", "json", "logging", "math", "mimetypes",
    "os", "pathlib", "queue", "random", "re", "select", "shlex", "shutil", "signal",
    "socket", "socketserver", "sqlite3", "ssl", "stat", "string", "struct",
    "subprocess", "sys", "tempfile", "threading", "time", "tomllib", "traceback",
    "types", "typing", "unittest", "urllib", "uuid", "warnings", "weakref", "zipfile",
    "zoneinfo", "platform", "gc", "dataclasses", "ast", "textwrap", "statistics",
}
WELL_KNOWN_THIRD_PARTY = {
    "yaml": "PyYAML", "requests": "requests", "aiohttp": "aiohttp",
    "mcp": "mcp", "pydantic": "pydantic", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "websockets": "websockets", "pytest": "pytest", "bs4": "beautifulsoup4",
    "PIL": "pillow", "numpy": "numpy", "defusedxml": "defusedxml",
}


def find_python_plugins(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "plugin.json" not in filenames:
            continue
        d = json.load(open(os.path.join(dirpath, "plugin.json"), encoding="utf-8"))
        pid = d.get("id", "")
        if d.get("language") != "python":
            continue
        if pid in ALREADY_UV or Path(dirpath, "pyproject.toml").exists():
            continue
        if (Path(dirpath, "server.py").exists() or Path(dirpath, "src").exists()):
            out.append(Path(dirpath))
    return sorted(out)


def imported_third_party(dirpath: Path) -> list[str]:
    """静态扫描模块源码的 import，过滤 stdlib/SDK/本地，返回疑似第三方模块名。
    用带剪枝的 os.walk（rglob 会钻进 dsh_adapter runtime 的递归 node_modules 卡死）。"""
    found: list[str] = []
    for cur, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                tree = ast.parse(Path(cur, fn).read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Import):
                    name = node.names[0].name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    name = node.module.split(".")[0]
                if name and name not in STDLIB and name != "agentos_plugin_sdk":
                    if name not in found:
                        found.append(name)
    return found


def sdk_rel_path(plugin_dir: Path) -> str:
    """插件目录 → plugins/sdk 的相对路径（[tool.uv.sources] editable 映射用）。

    插件嵌套深度不一（tools/<name> 为 3 级、pipeline/input/<name> 为 4 级），
    必须逐目录计算；Windows relpath 产出反斜杠，TOML 字符串里是转义符，
    统一替换为正斜杠（uv/TOML 均接受）。
    """
    return os.path.relpath(ROOT / "plugins" / "sdk", plugin_dir).replace(os.sep, "/")


def pyproject_body(pid: str, version: str, deps: list[str], sdk_rel: str) -> str:
    name = "agentos-plugin-" + pid.replace("_", "-")
    dep_lines = "".join(f'    "{d}",\n' for d in deps)
    return (
        "[project]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "Lingxi AgentOS 0.2 plugin — {pid}（uv 单轨迁移样板）"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n"
        f"{dep_lines}]\n"
        "\n"
        "# 本地 SDK 源映射（uv lock 解析必需——agentos-plugin-sdk 不在任何 registry）：\n"
        "# editable 链接 plugins/sdk/src 源码目录（判据 2 案 A）。\n"
        "[tool.uv.sources]\n"
        f'agentos-plugin-sdk = {{ path = "{sdk_rel}", editable = true }}\n'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="写 pyproject.toml 并 uv lock（默认 dry-run）")
    ap.add_argument("--skip-lock", action="store_true", help="--apply 时只写 pyproject 不跑 uv lock")
    ap.add_argument("--root", default=str(ROOT / "plugins" / "shared"))
    args = ap.parse_args()

    plugins = find_python_plugins(Path(args.root))
    print(f"将迁移的 Python 插件（无 pyproject）：{len(plugins)}")
    third_party_hits: dict[str, list[str]] = {}
    for p in plugins:
        pid = json.load(open(p / "plugin.json", encoding="utf-8"))["id"]
        tps = imported_third_party(p)
        if tps:
            third_party_hits[pid] = tps
        print(f"- {pid:44s} <- {p.relative_to(ROOT)}")
        if tps:
            mapped = [WELL_KNOWN_THIRD_PARTY.get(t, t) for t in tps]
            print(f"    ⚠ 疑似第三方依赖（人工确认后填入 pyproject）：{mapped}")

    if not args.apply:
        print("\n[dry-run] 未写任何文件；--apply 后为每个插件写 pyproject.toml + uv.lock")
        if plugins:
            sample = plugins[0]
            sample_meta = json.load(open(sample / "plugin.json", encoding="utf-8"))
            print(f"\n[dry-run] 样板预览（{sample_meta['id']}，sdk_rel={sdk_rel_path(sample)}）：")
            print(pyproject_body(sample_meta["id"], sample_meta.get("version", "0.0.0"),
                                 ["agentos-plugin-sdk>=0.2.0"], sdk_rel_path(sample)))
        return 0

    written = 0
    for p in plugins:
        d = json.load(open(p / "plugin.json", encoding="utf-8"))
        pid, version = d["id"], d.get("version", "0.0.0")
        deps = ["agentos-plugin-sdk>=0.2.0"]
        (p / "pyproject.toml").write_text(
            pyproject_body(pid, version, deps, sdk_rel_path(p)), encoding="utf-8"
        )
        written += 1
        if args.skip_lock:
            continue
        proc = subprocess.run(
            ["uv", "lock", "--project", str(p)], capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            print(f"    ✗ uv lock 失败 {pid}: {(proc.stderr or proc.stdout).strip()[:200]}")
    print(f"\n[apply] 已写 pyproject.toml：{written}（uv.lock 由 uv lock 生成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
