#!/usr/bin/env python
"""knip/jscpd 基线棘轮门禁（只减不增）——R6v2 收口（2026-09-13）。

与 check_frontend_any_baseline.py / check_pytest_failure_baseline.py 同构：
首跑数=基线，超基线即红（非零退出），低于基线提示收紧（--init）。

两条纯解析检查（解析工具 stdout，不重复跑工具）：
1. knip 发现数：解析 stdout 各 section 计数行（如 `Unused exports (62)`），
   总和对照 .github/knip-jscpd-baseline.txt 的 knip_findings，只减不增。
2. jscpd 克隆数：解析 stdout 汇总行 `Found N exact clones`，对照 jscpd_clones，只减不增。

解析失败（工具崩溃/输出截断/格式变更）→ 非零退出，大声失败，
不把"没数到"当"零发现"。

工具链口径：Windows 侧 pnpm（node_modules 为 Windows 安装；WSL 侧 native
binding 不匹配不可用，与 vite 同口径）。复跑命令见
docs/working/knip_jscpd_基线_20260913.md。

用法：
    python scripts/check_knip_jscpd_baseline.py --knip-file out.txt
    python scripts/check_knip_jscpd_baseline.py --jscpd-file out.txt
    python scripts/check_knip_jscpd_baseline.py --knip-file k.txt --jscpd-file j.txt --init

基线维护：只许减不许增；修复存量后用 --init 收紧并 commit 留归因。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "knip-jscpd-baseline.txt"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# knip section 行，如 `Unused exports (62)` / `Configuration hints (6)`
KNIP_SECTION_RE = re.compile(r"^(.+?)\s+\((\d+)\)\s*$", re.MULTILINE)
# jscpd 汇总行，如 `Found 558 exact clones with 7011(4.56%) duplicated lines in 809 (2 formats) files.`
JSCPD_CLONES_RE = re.compile(r"Found\s+(\d+)\s+exact\s+clones")
NO_ISSUES_RE = re.compile(r"no issues|no problems found", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def parse_knip(text: str) -> int | None:
    """解析 knip stdout 各 section 计数之和；解析不到返回 None（fail-loud）。"""
    text = strip_ansi(text)
    if not text.strip():
        return None  # 空输出视为异常（截断/崩溃），不当作零发现
    total = 0
    found = False
    for m in KNIP_SECTION_RE.finditer(text):
        total += int(m.group(2))
        found = True
    if not found:
        # knip 全干净时可能无 section 行——仅在明确无问题文案时接受 0
        if NO_ISSUES_RE.search(text):
            return 0
        return None
    return total


def parse_jscpd(text: str) -> int | None:
    """解析 jscpd stdout 汇总行克隆数；解析不到返回 None（fail-loud）。"""
    m = JSCPD_CLONES_RE.search(strip_ansi(text))
    return int(m.group(1)) if m else None


def read_baseline(key: str) -> int | None:
    if not BASELINE_FILE.exists():
        return None
    for raw in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return int(v.strip())
    return None


def write_baseline(key: str, count: int) -> None:
    """只替换本 key 的数值行，保留其余 key 与注释头（--init 收紧时文档不丢）。"""
    lines: list[str] = []
    if BASELINE_FILE.exists():
        for raw in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith(f"{key}="):
                continue
            lines.append(raw)
    lines.append(f"{key}={count}")
    BASELINE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check(key: str, current: int | None, label: str) -> bool:
    """棘轮比较：超基线红；持平/低于绿。返回是否失败。"""
    if current is None:
        print(
            f"❌ {label}：解析不到计数（工具崩溃/输出截断/格式变更）——按失败处理，不写入基线。",
            file=sys.stderr,
        )
        return True
    baseline = read_baseline(key)
    if baseline is None:
        print(
            f"❌ 基线文件缺少 {key} 条目（{BASELINE_FILE}）。首次接入请用 --init 写入实测值。",
            file=sys.stderr,
        )
        return True
    print(f"{label}: 当前={current}，基线={baseline}")
    if current > baseline:
        print(
            f"❌ {label} 超过基线（{current} > {baseline}，只减不增）——存在新增存量，拦截合并。",
            file=sys.stderr,
        )
        return True
    if current < baseline:
        print(f"💡 {label} 已低于基线，可运行 --init 收紧：{key}={current}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="knip/jscpd 基线棘轮门禁（只减不增）")
    parser.add_argument("--knip-file", help="knip stdout 捕获文件")
    parser.add_argument("--jscpd-file", help="jscpd stdout 捕获文件")
    parser.add_argument("--init", action="store_true", help="用当前实测写入/收紧基线（不可逆方向拒绝）")
    ns = parser.parse_args()

    if not ns.knip_file and not ns.jscpd_file:
        parser.error("至少提供 --knip-file 或 --jscpd-file 之一")

    if ns.init:
        if ns.knip_file:
            text = Path(ns.knip_file).read_text(encoding="utf-8", errors="replace")
            current = parse_knip(text)
            if current is None:
                print("❌ knip_findings：解析不到计数，拒绝写入基线。", file=sys.stderr)
                return 1
            old = read_baseline("knip_findings")
            if old is not None and current > old:
                print(
                    f"❌ --init 拒绝上调 knip_findings：实测 {current} > 现基线 {old}（棘轮不可逆）",
                    file=sys.stderr,
                )
                return 1
            write_baseline("knip_findings", current)
            print(f"基线写入：knip_findings={current}")
        if ns.jscpd_file:
            text = Path(ns.jscpd_file).read_text(encoding="utf-8", errors="replace")
            current = parse_jscpd(text)
            if current is None:
                print("❌ jscpd_clones：解析不到计数，拒绝写入基线。", file=sys.stderr)
                return 1
            old = read_baseline("jscpd_clones")
            if old is not None and current > old:
                print(
                    f"❌ --init 拒绝上调 jscpd_clones：实测 {current} > 现基线 {old}（棘轮不可逆）",
                    file=sys.stderr,
                )
                return 1
            write_baseline("jscpd_clones", current)
            print(f"基线写入：jscpd_clones={current}")
        return 0

    failed = False
    if ns.knip_file:
        text = Path(ns.knip_file).read_text(encoding="utf-8", errors="replace")
        failed = check("knip_findings", parse_knip(text), "knip 发现数") or failed
    if ns.jscpd_file:
        text = Path(ns.jscpd_file).read_text(encoding="utf-8", errors="replace")
        failed = check("jscpd_clones", parse_jscpd(text), "jscpd 克隆数") or failed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
