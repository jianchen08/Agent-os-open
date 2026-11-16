#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""文档链接完整性（base/block）：md 相对链接/图片引用必须可达（§7 文档=结构型）。

只管「链得通」，不管内容（J4 内容审查禁区）；外链（http/https/mailto）、纯锚点、
带 scheme 的引用一律不查——本地相对路径以所在 md 为基准解析。
"""

from __future__ import annotations

import re
import sys
from urllib.parse import unquote

from _common import PROJECT_ROOT, input_files, read_text_limited, to_posix

_INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _is_external(target: str) -> bool:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return True
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target))  # 其他 scheme（ftp: 等）


def check_doc(rel: str) -> list[str]:
    text = read_text_limited(rel)
    if text is None:
        return []
    findings: list[str] = []
    doc_path = PROJECT_ROOT / rel
    for match in _INLINE_LINK.finditer(text):
        raw = unquote(match.group(1))
        target = raw.split("#", 1)[0]
        if not target or _is_external(raw):
            continue
        resolved = (doc_path.parent / target).resolve()
        try:
            resolved.relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            continue  # 越出项目根的引用（../ 出仓）不归本门管
        suffix = "目录" if target.endswith("/") else "文件"
        if not resolved.exists():
            findings.append(f"{to_posix(rel)}: 断链（{suffix}不存在）: {raw}")
    return findings


def main() -> int:
    findings: list[str] = []
    for rel in input_files():
        if to_posix(rel).lower().endswith(".md"):
            findings.extend(check_doc(rel))
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
