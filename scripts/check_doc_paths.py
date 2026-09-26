#!/usr/bin/env python3
"""活文档路径存在性闸（执行方案批次 6-3，评估 §2.4/R6）。

背景实锤：评估发现 4 处配置路径文档与代码系统性错位（storage.yaml 实为
config/kernel/storage.yaml 等），按文档找不到配置。历史记录层（working/
决策归档）不强制回溯改写——本闸只扫**活文档**（当前维护的入口文档），
保证"照着活文档能找到文件"。

范围（LIVE_DOCS）：AGENTS.md、README.md、docs/README.md、
docs/ARCHITECTURE.md、docs/vision.md、docs/plugin-protocol.md、docs/guides/*.md。
判定：文中 `config/...`、`plugins/...`、`docs/...` 形态的仓库相对路径
（反引号内），逐个验证文件/目录存在；不存在即红。

用法：python scripts/check_doc_paths.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LIVE_DOCS = [
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "vision.md",
    ROOT / "docs" / "guides" / "plugin-protocol.md",
    *sorted((ROOT / "docs" / "guides").glob("*.md")),
]

# 反引号内仓库相对路径（config/ plugins/ docs/ scripts/ 开头）
PATH_RE = re.compile(r"`((?:config|plugins|docs|scripts)/[A-Za-z0-9_\-./]+\.(?:yaml|yml|json|md|py|rs|ts|txt))`")


def main() -> int:
    errors: list[str] = []
    checked = 0
    for doc in LIVE_DOCS:
        if not doc.exists():
            errors.append(f"活文档缺失: {doc.relative_to(ROOT)}")
            continue
        text = doc.read_text(encoding="utf-8")
        for m in PATH_RE.finditer(text):
            rel = m.group(1)
            checked += 1
            if not (ROOT / rel).exists():
                errors.append(f"{doc.relative_to(ROOT)}: 路径不存在 `{rel}`")

    if errors:
        print(f"doc-paths: {len(errors)} 项违规（checked {checked}）：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"doc-paths: 通过（活文档 {len(LIVE_DOCS)} 篇，路径引用 {checked} 处全部存在）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
