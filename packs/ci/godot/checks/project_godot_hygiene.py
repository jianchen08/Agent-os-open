#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""project.godot 卫生门（godot/block）：清单可解析 + [editor_plugins] 启用条目在位。

解析用最小手写 parser（stdlib）：Godot 清单非严格 INI（顶层键先于任何 section），
configparser 不适用。启用条目带 res:// 前缀，逐条核对文件在仓库内存在。
"""

from __future__ import annotations

import sys

from _common import PROJECT_ROOT, input_files, to_posix

PROJECT_FILE = "project.godot"


def parse_godot(text: str) -> dict[str, dict[str, str]]:
    """section → {key: value} 的最小解析；空行/注释跳过，坏行忽略。"""
    sections: dict[str, dict[str, str]] = {"": {}}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            sections.setdefault(current, {})[key.strip()] = value.strip()
    return sections


def enabled_entries(sections: dict[str, dict[str, str]]) -> list[str]:
    raw = sections.get("editor_plugins", {}).get("enabled", "")
    if "PackedStringArray" not in raw:
        return []
    return [e.strip().strip('"') for e in raw.split("(", 1)[-1].rstrip(")").split(",") if e.strip().strip('"')]


def main() -> int:
    findings: list[str] = []
    pf = PROJECT_ROOT / PROJECT_FILE
    if not pf.is_file():
        print(f"{PROJECT_FILE}: 缺失（godot 项目清单不在位）")
        return 1
    try:
        sections = parse_godot(pf.read_text(encoding="utf-8", errors="ignore"))
    except OSError as exc:
        print(f"{PROJECT_FILE}: 不可读: {exc}")
        return 1
    top_keys = sections.get("", {})
    if not top_keys and len(sections) <= 1:
        print(f"{PROJECT_FILE}: 不可解析（无任何 section/键值）")
        return 1
    tracked = {to_posix(p) for p in input_files()}
    for entry in enabled_entries(sections):
        rel = to_posix(entry.removeprefix("res://"))
        if rel not in tracked and not (PROJECT_ROOT / rel).is_file():
            findings.append(f"{PROJECT_FILE}: 启用条目不在位: {entry}（文件缺失或已失效）")
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
