#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""addons 承诺门（godot/block）：出生承诺的 addons 在位且未失效。

承诺清单由出生组装烘焙进 run argv（--addons a,b，源自类型配方 addons 字段）；
未承诺（无参数）= 无物可查 → skip。失效判定：addons/<name>/plugin.cfg 存在且
含 [plugin] 段。
"""

from __future__ import annotations

import argparse
import sys

from _common import PROJECT_ROOT, to_posix


def main() -> int:
    parser = argparse.ArgumentParser(description="出生承诺的 addons 在位检查")
    parser.add_argument("--addons", default="", help="逗号分隔的 addon 名清单（出生组装烘焙）")
    args = parser.parse_args()
    names = [n.strip() for n in args.addons.split(",") if n.strip()]
    if not names:
        print("skip: 未承诺 addons（出生配方无 addons 字段）")
        return 2
    findings: list[str] = []
    for name in names:
        rel = to_posix(f"addons/{name}/plugin.cfg")
        cfg = PROJECT_ROOT / rel
        if not cfg.is_file():
            findings.append(f"{rel}: 承诺的 addon 缺失")
        elif "[plugin]" not in cfg.read_text(encoding="utf-8", errors="ignore"):
            findings.append(f"{rel}: plugin.cfg 失效（缺 [plugin] 段）")
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
