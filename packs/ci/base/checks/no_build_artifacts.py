#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""构建产物闸（base/block）：构建/依赖缓存目录与产物扩展名不得入库。

模式表参数化（泛化自本仓同名 checker）：目录命中任一段、文件名命中任一 glob
即违规。退出码契约：0=绿 / 1=违规 / 2=保留（本 checker 不使用 skip）。
"""

from __future__ import annotations

import fnmatch
import sys

from _common import input_files, to_posix

ARTIFACT_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".godot",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".gradle",
    "target",
    "dist",
    "build",
}
ARTIFACT_NAME_GLOBS = ["*.pyc", "*.pyo", "*.class", "*.o"]


def main() -> int:
    findings: list[str] = []
    for rel in input_files():
        posix = to_posix(rel)
        hit_dir = next((seg for seg in posix.split("/")[:-1] if seg in ARTIFACT_DIR_NAMES), "")
        if hit_dir:
            findings.append(f"{posix}: 位于构建/缓存目录 {hit_dir}/（产物不入库）")
        elif any(fnmatch.fnmatch(posix.split("/")[-1], g) for g in ARTIFACT_NAME_GLOBS):
            findings.append(f"{posix}: 构建产物扩展名（{'|'.join(ARTIFACT_NAME_GLOBS)} 不入库）")
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
