#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""单文件体积上限（base/block）：待入库文件超上限即红。

上限可用 run argv 声明式覆盖（--max-bytes N），缺省 10 MiB。
"""

from __future__ import annotations

import argparse
import sys

from _common import PROJECT_ROOT, input_files, to_posix

DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description="单文件体积上限")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    findings: list[str] = []
    for rel in input_files():
        path = PROJECT_ROOT / rel
        try:
            size = path.stat().st_size
        except OSError:
            continue  # 清单与磁盘短暂不一致（已删除的暂存项）不构成违规
        if size > args.max_bytes:
            findings.append(f"{to_posix(rel)}: {size} 字节 > 上限 {args.max_bytes}（大文件走版本化发行位，不入库）")
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
