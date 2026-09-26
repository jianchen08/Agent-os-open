#!/usr/bin/env python3
"""配置直读机械闸（ADR 2026-09-14-config-ownership-plugin-lifecycle §2.6）。

插件代码禁止直读 config/ 下配置文件——配置唯一合法入口是 manifest `config_files`
声明 + 内核注入（get_config 命名空间）或 owner 插件服务。本闸扫描 plugins/ 下
Python/Rust 源码中的硬编码配置路径字面量，新违规即红（存量进基线文件棘轮清偿）。

- 扫描目标：plugins/shared/**/*.py、plugins/sdk/src/**/*.py、
  plugins/shared/pipeline/**/src/**/*.rs（native cdylib）
- 判定：出现 `config/` 路径字面量（斜杠/分段拼接两种形态）
- 基线：.github/check_config_direct_reads_baseline.txt（每行 = 文件:出现次数），
  只减不增；基线外新增 → exit 1

用法：python scripts/check_config_direct_reads.py
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_ROOTS = [
    ROOT / "plugins" / "shared",
    ROOT / "plugins" / "sdk" / "src",
]
SCAN_SUFFIXES = {".py", ".rs"}
EXCLUDE_DIRS = {
    "__pycache__", "node_modules", "runtime", "target", ".venv", "venv",
    ".venv-hindsight", ".ai_workspaces",
    # 纯测试对直读回退的夹具/断言由对应插件测试车道治理，不进本闸
}

# `config/...` 斜杠形态（字符串内）；分段拼接形态 `"config", "xx"` / "config" / "xx"
SLASH_FORM = re.compile(r"config/[A-Za-z0-9_\-./]+\.(?:yaml|yml|json)")
SEGMENT_FORM = re.compile(r"""["']config["']\s*,\s*["'][^"']+["']""")
PATH_JOIN_FORM = re.compile(r"""["']config["']\s*/\s*["'][^"']+["']""")


def iter_sources() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        # 剪枝式遍历：排除目录整体不下钻（node_modules/runtime 体量巨大）
        stack = [root]
        while stack:
            cur = stack.pop()
            for p in cur.iterdir():
                if p.is_dir():
                    if p.name in EXCLUDE_DIRS:
                        continue
                    stack.append(p)
                elif p.suffix in SCAN_SUFFIXES:
                    out.append(p)
    return out


def count_violations(text: str) -> int:
    n = len(SLASH_FORM.findall(text))
    n += len(SEGMENT_FORM.findall(text))
    n += len(PATH_JOIN_FORM.findall(text))
    return n


def main() -> int:
    found: Counter[str] = Counter()
    for p in iter_sources():
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = count_violations(text)
        if n:
            found[str(p.relative_to(ROOT)).replace("\\", "/")] = n

    baseline_path = Path(__file__).resolve().parent.parent / ".github" / "check_config_direct_reads_baseline.txt"
    baseline: dict[str, int] = {}
    if baseline_path.exists():
        for line in baseline_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            path, _, cnt = line.rpartition(":")
            # fail-closed：基线键必须是仓库相对 posix 路径。历史缺陷实证——
            # 机器绝对路径键（D:/...）与相对路径 found 键永不命中 → 恒"新增
            # 违规"恒红且无人发现（检查器当时未注册车道）。格式错直接拒判。
            if (len(path) > 2 and path[1] == ":") or path.startswith("/") or path.startswith("\\"):
                print(
                    f"配置直读机械闸：基线含非相对路径键（历史缺陷形态）：{path}\n"
                    "基线键须为仓库相对 posix 路径；请以 --init 重生成基线。"
                )
                return 2
            baseline[path] = int(cnt)

    new_violations = {
        path: cnt
        for path, cnt in found.items()
        if baseline.get(path, 0) < cnt
    }

    if new_violations:
        print("配置直读机械闸：发现基线外新增违规（禁止直读 config/，走 config_files 注入）：")
        for path in sorted(new_violations):
            print(f"  {path}: {found[path]} 处（基线 {baseline.get(path, 0)}）")
        print("修复 = 改走注入/config_files 声明；确属合法通道则更新基线并注明豁免理由。")
        return 1

    # 基线收缩提示（只减不增棘轮）
    shrinkable = [p for p, c in baseline.items() if found.get(p, 0) < c]
    if shrinkable:
        print("提示：以下基线条目可收缩（棘轮只减不增）：")
        for path in sorted(shrinkable):
            print(f"  {path}: 基线 {baseline[path]} → 实测 {found.get(path, 0)}")
    print(f"配置直读机械闸通过（扫描 {len(iter_sources())} 文件，存量 {sum(found.values())} 处在基线内）。")
    return 0


def init_baseline() -> int:
    """以当前实测重建基线（仓库相对 posix 键；--init 须单独 commit 留归因）。"""
    found: Counter[str] = Counter()
    for p in iter_sources():
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = count_violations(text)
        if n:
            found[str(p.relative_to(ROOT)).replace("\\", "/")] = n
    lines = [
        "# 配置直读存量基线（棘轮只减不增）；格式 文件相对路径: 处数",
        "# 依据 ADR 2026-09-14-config-ownership-plugin-lifecycle §2.6",
        "# 存量为历史欠账：新增直读即红，存量随触碰逐文件清偿",
        "# 键必须是仓库相对 posix 路径（绝对路径键属历史缺陷形态，检查器拒判）",
        *sorted(f"{path}: {cnt}" for path, cnt in found.items()),
    ]
    baseline_path = Path(__file__).resolve().parent.parent / ".github" / "check_config_direct_reads_baseline.txt"
    baseline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"配置直读机械闸：基线已重建（{len(found)} 文件 / {sum(found.values())} 处）。")
    return 0


if __name__ == "__main__":
    if "--init" in sys.argv:
        sys.exit(init_baseline())
    sys.exit(main())
