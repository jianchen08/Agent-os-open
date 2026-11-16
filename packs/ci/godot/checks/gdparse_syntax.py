#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""gdscript 语法快检（godot/block）：调用 gdtoolkit 的 gdparse（声明式语言槽位）。

gdparse 缺席 = skip（exit 2，fail-open）；语法错误 = 红（fail-closed）。
待检集：staged 清单中的 .gd（无清单时全仓 tracked .gd）；无 .gd 平凡绿。
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from _common import PROJECT_ROOT, input_files, to_posix

BATCH = 50


def main() -> int:
    gd_files = [to_posix(p) for p in input_files() if to_posix(p).lower().endswith(".gd")]
    if not gd_files:
        return 0
    gdparse = shutil.which("gdparse")
    if not gdparse:
        print("skip: gdparse（gdtoolkit）未安装——语法门本提交跳过")
        return 2
    findings: list[str] = []
    for i in range(0, len(gd_files), BATCH):
        chunk = gd_files[i : i + BATCH]
        try:
            out = subprocess.run(
                [gdparse, *chunk],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append(f"gdparse 运行失败: {exc}")
            break
        if out.returncode != 0:
            text = out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")
            tail = [ln for ln in text.strip().splitlines() if ln.strip()]
            findings.extend(tail[-20:] or [f"gdparse 退出码 {out.returncode}"])
            break  # 一批失败即报，避免同源错误刷屏
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
