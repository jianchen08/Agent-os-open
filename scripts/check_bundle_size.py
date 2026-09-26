#!/usr/bin/env python3
"""前端产物体积闸（执行方案批次 5，评估 R5/P1-2）。

两查（消费 vite build 日志 + dist 产物，fail-closed）：
1. vendor 体积：dist/assets/vendor-*.js 最大文件字节数 ≤ 基线
   （.github/bundle-size-baseline.txt: vendor_max_bytes，只减不增）；
2. 动态导入失效警告：日志 INEFFECTIVE_DYNAMIC_IMPORT 计数 ≤ 基线
   （ineffective_dynamic_imports；现存量 2 处均为避静态环的合法动态
   导入——tokenLifecycle/resync 注释自证，仅拆分意图被静态消费方击穿，
   新增警告即红）。

用法（门禁 shell 先 build | tee，再调本检查器）：
    python scripts/check_bundle_size.py --from-file build.log
    python scripts/check_bundle_size.py --from-file build.log --init
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".github" / "bundle-size-baseline.txt"
DIST_ASSETS = ROOT / "frontend" / "dist" / "assets"

WARNING_RE = re.compile(r"INEFFECTIVE_DYNAMIC_IMPORT")


def load_baseline() -> dict[str, int]:
    vals = {"vendor_max_bytes": 0, "ineffective_dynamic_imports": 0}
    if not BASELINE_PATH.exists():
        return vals
    for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        if key.strip() in vals:
            vals[key.strip()] = int(val)
    return vals


def vendor_max_bytes() -> int:
    files = list(DIST_ASSETS.glob("vendor-*.js"))
    if not files:
        raise RuntimeError(f"bundle-size: 未找到 vendor 产物（{DIST_ASSETS}）——须先 vite build。")
    return max(f.stat().st_size for f in files)


def main() -> int:
    parser = argparse.ArgumentParser(description="前端产物体积闸")
    parser.add_argument("--from-file", required=True, help="vite build 日志路径")
    parser.add_argument("--init", action="store_true", help="打印当前实测值（收紧用，只减不增）")
    ns = parser.parse_args()

    log = Path(ns.from_file).read_text(encoding="utf-8", errors="replace")
    warnings = len(WARNING_RE.findall(log))
    size = vendor_max_bytes()

    if ns.init:
        print(f"vendor_max_bytes={size}")
        print(f"ineffective_dynamic_imports={warnings}")
        return 0

    baseline = load_baseline()
    bad = False
    if baseline["vendor_max_bytes"] and size > baseline["vendor_max_bytes"]:
        print(
            f"bundle-size: vendor 体积 {size:,} > 基线 {baseline['vendor_max_bytes']:,} 字节"
            "（只减不增）——新增依赖/巨型包拦截。"
        )
        bad = True
    if warnings > baseline["ineffective_dynamic_imports"]:
        print(
            f"bundle-size: INEFFECTIVE_DYNAMIC_IMPORT {warnings} > 基线 "
            f"{baseline['ineffective_dynamic_imports']}（只减不增）——新增拆分失效导入，拦截。"
        )
        bad = True
    if bad:
        return 1
    print(
        f"bundle-size: 通过（vendor {size:,}B ≤ 基线；无效动态导入 {warnings} ≤ 基线）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
