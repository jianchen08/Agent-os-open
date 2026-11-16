#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""密钥扫描（base/block）：高置信密钥特征入库即红。

只收低误报特征（§11 风险边界：扫描类门禁靠模式收紧而非降档对冲误报）——
泛化赋值形态（password=xxx）不查，那是无法机械判定的内容域。
"""

from __future__ import annotations

import re
import sys

from _common import input_files, read_text_limited, to_posix

# (名称, 编译态模式)——字面量写法保证本文件自身不被任何一条模式命中
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS AccessKey", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥块", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY( BLOCK)?-----")),
    ("Google APIKey", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
]


def main() -> int:
    findings: list[str] = []
    for rel in input_files():
        text = read_text_limited(rel, limit=1024 * 1024)
        if text is None:
            continue
        for name, pattern in PATTERNS:
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    findings.append(f"{to_posix(rel)}:{i}: 疑似{name}（真密钥请撤销并换注入；误报请改写示例串）")
                    break  # 同文件同模式报一处即可
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
