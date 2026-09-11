#!/usr/bin/env python
"""前端 any 基线棘轮 + >1000 行文件冻结门禁（批次F 2026-09-08）。

两条纯扫描检查（零外部依赖，不跑 Node）：
1. any 只增不减护栏：frontend/src 生产码（排除 __tests__/ *.test.* /
   endpoints.generated.ts）中 `: any` / `as any` 命中行数，对照
   .github/frontend-any-baseline.txt 只减不增；超基线非零退出并打印命中位置，
   低于基线提示下调（下调走 commit 留归因）。存量 any 消化另行立项，本闸只防增。
2. >1000 行文件冻结：巨型文件拆分在活跃演化期需独立行为验证门槛（第三/四轮
   全仓扫描裁定：单独立项分批），本轮以冻结基线管控——
   .github/frontend-large-files-baseline.txt 清单内文件行数增长即红、
   新出现 >1000 行文件即红；缩短仅提示下调（不红）。

用法：
    python scripts/check_frontend_any_baseline.py           # 两条检查
    python scripts/check_frontend_any_baseline.py --init    # 实测写入/收紧基线
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "frontend" / "src"
ANY_BASELINE_FILE = ROOT / ".github" / "frontend-any-baseline.txt"
LARGE_FILES_BASELINE_FILE = ROOT / ".github" / "frontend-large-files-baseline.txt"

LINE_LIMIT = 1000
ANY_PATTERN = re.compile(r": any|as any")
GENERATED_FILE = "endpoints.generated.ts"


def iter_production_files(scan_dir: Path) -> Iterator[Path]:
    """枚举前端生产码文件（.ts/.tsx，排除测试与生成物）。"""
    for dirpath, dirnames, filenames in os.walk(scan_dir):
        dirnames[:] = [d for d in dirnames if d != "__tests__"]
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            if ".test." in name or name == GENERATED_FILE:
                continue
            yield Path(dirpath) / name


def count_any_hits(scan_dir: Path) -> list[tuple[Path, int, str]]:
    """返回 `: any` / `as any` 命中行（路径, 行号, 行文本），行号从 1 起。"""
    hits: list[tuple[Path, int, str]] = []
    for path in iter_production_files(scan_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ANY_PATTERN.search(line):
                hits.append((path, lineno, line.strip()))
    return hits


def read_any_baseline(path: Path) -> int:
    """读 any 基线（`frontend_any=N` 行）。基线缺失 = 度量链断裂，fail-loud。"""
    if not path.exists():
        raise RuntimeError(f"any 基线文件缺失：{path}（护栏被绕开即失效，拒绝放行）")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("frontend_any="):
            return int(line.split("=", 1)[1])
    raise RuntimeError(f"any 基线文件无 frontend_any= 行：{path}")


def read_large_files_baseline(path: Path) -> dict[str, int]:
    """读冻结清单（`相对路径 行数`）。基线缺失 = 度量链断裂，fail-loud。"""
    if not path.exists():
        raise RuntimeError(f"大文件冻结基线缺失：{path}（护栏被绕开即失效，拒绝放行）")
    frozen: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, n = line.rpartition(" ")
        frozen[rel] = int(n)
    return frozen


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def check_any_baseline(scan_dir: Path, baseline_file: Path) -> bool:
    """any 棘轮：超基线红（打印命中位置）；低于基线提示下调。返回是否失败。"""
    baseline = read_any_baseline(baseline_file)
    hits = count_any_hits(scan_dir)
    current = len(hits)
    print(f"\n[any 棘轮] 基线 {baseline} / 当前 {current}")
    if current > baseline:
        print(f"❌ any 命中数超过基线（{baseline} → {current}，+{current - baseline}），新增位置：")
        for path, lineno, text in hits:
            print(f"  {path.relative_to(ROOT)}:{lineno}: {text[:120]}")
        print("请消掉新增 any（存量消化见批次F报告），禁止上调基线。")
        return True
    if current < baseline:
        print(f"✅ any 命中数低于基线（{baseline} → {current}）：请 --init 收紧并 commit 留归因")
        return False
    print("✅ 与基线持平，无新增 any")
    return False


def check_large_files(scan_dir: Path, baseline_file: Path) -> bool:
    """巨型文件冻结：清单内增长红、新入列 >1000 行文件红；缩短仅提示。"""
    frozen = read_large_files_baseline(baseline_file)
    failed = False

    for path in iter_production_files(scan_dir):
        rel = path.relative_to(ROOT).as_posix()
        lines = count_lines(path)
        if lines <= LINE_LIMIT:
            continue
        if rel not in frozen:
            print(f"❌ 新增 >{LINE_LIMIT} 行文件未入冻结清单：{rel}（{lines} 行）")
            print("   拆分需独立行为验证门槛（单独立项分批）；入清单 = 承认新债，需 ADR 归因。")
            failed = True
        elif lines > frozen[rel]:
            print(f"❌ 冻结文件行数增长：{rel} {frozen[rel]} → {lines}（+{lines - frozen[rel]}）")
            print("   巨型文件只许缩小不许增长；拆分单独立项分批（第三/四轮裁定）。")
            failed = True

    shrunk = []
    for rel, cap in frozen.items():
        abs_path = ROOT / rel
        now = count_lines(abs_path) if abs_path.exists() else 0
        if now < cap:
            shrunk.append((rel, cap, now))
    if shrunk:
        print("✅ 以下冻结文件已缩短/移除，请 --init 收紧清单并 commit 留归因：")
        for rel, cap, now in shrunk:
            print(f"  {rel} {cap} → {now}")
    if not failed and not shrunk:
        print(f"\n[大文件冻结] 清单 {len(frozen)} 项全部受控（≤{LINE_LIMIT} 行冻结上限内）")
    return failed


def write_baselines(scan_dir: Path, any_file: Path, large_file: Path) -> None:
    """实测写入/收紧：any 计数只许持平或下降；冻结上限只许持平或下降。"""
    old_any = read_any_baseline(any_file) if any_file.exists() else None
    hits = len(count_any_hits(scan_dir))
    if old_any is not None and hits > old_any:
        print(f"❌ --init 拒绝上调 any 基线：实测 {hits} > 现基线 {old_any}（棘轮不可逆）")
        raise SystemExit(1)
    any_file.write_text(
        "# 前端 any 基线（frontend/src 生产码 ': any'/'as any' 命中行数，只减不增）。\n"
        "# 见 scripts/check_frontend_any_baseline.py；下调后 commit 留归因。\n"
        f"frontend_any={hits}\n",
        encoding="utf-8",
    )

    old_frozen = read_large_files_baseline(large_file) if large_file.exists() else {}
    frozen: dict[str, int] = {}
    for p in iter_production_files(scan_dir):
        lines = count_lines(p)
        if lines > LINE_LIMIT:
            frozen[p.relative_to(ROOT).as_posix()] = lines
    raised = {
        rel: lines
        for rel, lines in frozen.items()
        if rel in old_frozen and lines > old_frozen[rel]
    }
    if raised:
        for rel, lines in raised.items():
            print(f"❌ --init 拒绝放宽冻结上限：{rel} {old_frozen[rel]} → {lines}（冻结不可逆）")
        raise SystemExit(1)
    lines_out = [
        "# 前端 >1000 行文件冻结基线（清单内行数只减不增；新入列 >1000 行文件即红）。\n"
        "# 巨型文件拆分在活跃演化期需独立行为验证门槛，单独立项分批（第三/四轮裁定），\n"
        "# 本清单即管控机制。见 scripts/check_frontend_any_baseline.py。\n",
    ]
    lines_out += [f"{rel} {n}\n" for rel, n in sorted(frozen.items())]
    large_file.write_text("".join(lines_out), encoding="utf-8")
    print(f"✅ 基线已写入：frontend_any={hits}；冻结清单 {len(frozen)} 项")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="前端 any 基线棘轮 + >1000 行文件冻结门禁")
    parser.add_argument("--init", action="store_true", help="用当前实测写入/收紧基线（不可逆方向拒绝）")
    args = parser.parse_args(argv)

    if args.init:
        write_baselines(SCAN_DIR, ANY_BASELINE_FILE, LARGE_FILES_BASELINE_FILE)
        return 0

    failed = check_any_baseline(SCAN_DIR, ANY_BASELINE_FILE)
    failed = check_large_files(SCAN_DIR, LARGE_FILES_BASELINE_FILE) or failed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
