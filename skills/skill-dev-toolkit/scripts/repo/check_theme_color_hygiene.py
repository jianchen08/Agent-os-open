#!/usr/bin/env python3
"""theme-color-hygiene 门禁：前端与插件声明面的颜色必须消费主题变量。

规则（2026-10-25 用户裁定）：所有前端文字/图标/背景颜色一律随主题变化——
生产代码与插件声明模板中不得硬编码颜色字面量（hex / rgb() / rgba() /
hsl() / hsla() 实参字面量），必须写 var(--token)/hsl(var(--token)) 主题变量。

定义层豁免（这些文件的职责就是定义颜色本身）：
- frontend/src/index.css            ：:root 主题变量定义
- frontend/src/styles/**            ：design tokens 与皮肤定义层
- frontend/src/themes/**            ：动态主题 JSON 皮肤
- frontend/src/config/themes/**     ：预设主题 ThemeConfig 与颜色值定义常量
- frontend/src/services/themeService.ts：主题机制本体（色彩运算与变量推送）
- frontend/src/services/themeValuePolicy.ts：色彩运算机制（亮度/对比度求值）
- frontend/src/stores/__tests__/helpers/storeTestMocks.ts：测试夹具（mock 主题数据需字面值）
- *.test.ts / *.test.tsx            ：颜色行为断言需要字面值
- plugins/shared/dsh_adapter/plugin.json：DSH 皮肤声明负载（外部皮肤适配）

存量债走基线棘轮（.github/theme-color-baseline.txt，「路径 数量」逐件上限，
只减不增）：现文件命中数 > 基线上限 = 红（新增/恶化）；--baseline-monotonic
时基线上限 > 现命中数或文件已删 = 红（棘轮收紧强制，不许回松）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 扫描面：前端源码 + 插件声明（声明式组件的模板颜色同受此约束）。
SCAN_GLOBS = (
    "frontend/src/**/*.ts",
    "frontend/src/**/*.tsx",
    "frontend/src/**/*.css",
    "plugins/shared/*/plugin.json",
)

# 定义层豁免（前缀形态；见模块 docstring 归因）。
ALLOWLIST_PREFIXES = (
    "frontend/src/styles/",
    "frontend/src/themes/",
    "frontend/src/config/themes/",
)
ALLOWLIST_FILES = frozenset(
    {
        "frontend/src/index.css",
        "frontend/src/services/themeService.ts",
        "frontend/src/services/themeValuePolicy.ts",
        "frontend/src/stores/__tests__/helpers/storeTestMocks.ts",
        "plugins/shared/dsh_adapter/plugin.json",
    }
)
ALLOWLIST_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
)

HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNC_COLOR = re.compile(r"\b(?:rgba?|hsla?)\(\s*[0-9]")

DEFAULT_BASELINE = REPO / ".github" / "theme-color-baseline.txt"


def scan_file(path: Path) -> list[tuple[int, str]]:
    """单文件扫描：返回 (行号, 违规行预览) 列表。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if HEX_COLOR.search(line) or FUNC_COLOR.search(line):
            hits.append((lineno, line.strip()[:120]))
    return hits


def collect() -> dict[str, list[tuple[int, str]]]:
    """全量扫描 → {repo 相对路径: 命中列表}，豁免层已剔除。"""
    out: dict[str, list[tuple[int, str]]] = {}
    for pattern in SCAN_GLOBS:
        for path in sorted(REPO.glob(pattern)):
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWLIST_FILES or rel.endswith(ALLOWLIST_SUFFIXES):
                continue
            if any(rel.startswith(p) for p in ALLOWLIST_PREFIXES):
                continue
            hits = scan_file(path)
            if hits:
                out[rel] = hits
    return out


def load_baseline(path: Path) -> dict[str, int]:
    entries: dict[str, int] = {}
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2 or not parts[1].isdigit():
            raise SystemExit(f"基线行非法（应为「路径 数量」）：{raw!r}")
        entries[parts[0]] = int(parts[1])
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="基线文件（默认 .github/theme-color-baseline.txt）",
    )
    parser.add_argument(
        "--baseline-monotonic",
        action="store_true",
        help="棘轮收紧强制：基线上限>现命中或文件已删也判红（只减不增）",
    )
    args = parser.parse_args()

    current = collect()
    baseline = load_baseline(args.baseline)

    overages: dict[str, int] = {}
    for rel, hits in current.items():
        cap = baseline.get(rel, 0)
        if len(hits) > cap:
            overages[rel] = len(hits) - cap

    stale: dict[str, int] = {}
    if args.baseline_monotonic:
        for rel, cap in baseline.items():
            now = len(current.get(rel, ()))
            if rel not in current:
                stale[rel] = cap
            elif cap > now:
                stale[rel] = cap - now

    for rel, excess in overages.items():
        hits = current[rel]
        print(f"RED {rel}: 超 baseline {excess} 处（现 {len(hits)} / 上限 {baseline.get(rel, 0)}）")
        for lineno, preview in hits[:3]:
            print(f"    L{lineno}: {preview}")
    for rel, excess in stale.items():
        print(f"RED {rel}: 基线可收紧 {excess} 处（棘轮只减不增，请下调/删除该条目）")

    if overages or stale:
        print(f"\ntheme-color-hygiene：{len(overages)} 文件超限，{len(stale)} 条目待收紧")
        return 1
    total = sum(len(h) for h in current.values())
    print(f"theme-color-hygiene：绿（{len(current)} 文件 {total} 处存量债，均在基线内）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
