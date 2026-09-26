#!/usr/bin/env python3
"""基线总表生成器 + 一致性闸（执行方案批次 6-2，评估 R6）。

单一真值 = .github/*baseline*.txt 各基线文件；本脚本从它们生成
docs/working/基线总表.md（自动生成，禁手编）。一致性闸 = 重新生成并与
已提交版本逐字节比对——手改生成物或基线值变更未回写，任一即红。

根治对象：AGENTS.md 覆盖率/基线段手写漂移（87.0/90.0/69.33 vs 实际值，
评估 §8.1 实证）——活文档一律引用本总表，不再手抄数字。

用法：
    python scripts/gen_baseline_table.py            # 生成 + 比对（门禁形态）
    python scripts/check_doc_baseline_sync.py       # 同上（别名入口）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "working" / "基线总表.md"

# 展示顺序与说明（键名 → 一句话用途）
LANES: list[tuple[str, str]] = [
    ("mypy-baseline.txt", "Python 类型检查错误数（plugins/shared + sdk）"),
    ("pytest-failure-baseline.txt", "pytest 失败数基线锁（plugins-coverage / plugins-heavy）"),
    ("rust-test-baseline.txt", "Rust 测试失败数基线锁"),
    ("rust-coverage-baseline.txt", "Rust 内核行覆盖率执法线（D1：目标 90，内核目标 100）"),
    ("python-coverage-baseline.txt", "Python 行覆盖率执法线（D1：目标 90）"),
    ("frontend-baseline.txt", "前端三键：vitest 失败/eslint 错误/覆盖率%"),
    ("frontend-any-baseline.txt", "前端 any 计数 + >1000 行文件冻结"),
    ("frontend-large-files-baseline.txt", "前端 >1000 行文件冻结清单"),
    ("knip-jscpd-baseline.txt", "前端死代码/重复率基线（CI 转正待跨平台验证）"),
    ("ruff-plugins-baseline.txt", "插件主体 ruff 违规基线（含 mcp-servers）"),
    ("ruff-scripts-baseline.txt", "scripts/ ruff 违规基线"),
    ("traceability-baseline.txt", "测试 @feature 未标记数基线"),
    ("tool-contract-baseline.txt", "工具契约缺项基线（output_schema/render）"),
    ("bundle-size-baseline.txt", "前端 vendor 字节 + 无效动态导入计数"),
    ("gate-wiring-baseline.txt", "元门禁：观察型/missing 能力计数上限"),
    ("check_config_direct_reads_baseline.txt", "配置直读存量（文件:处数）"),
    ("gitleaks-baseline.json", "密钥扫描存量命中（gitleaks 原生 json）"),
]


def _values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        if val:
            out[key.strip()] = val.strip()
    return out


def generate() -> str:
    lines = [
        "# 机器基线总表（自动生成，禁手编）",
        "",
        "- 生成器：`scripts/gen_baseline_table.py`；一致性闸随跑随比对。",
        "- 单一真值 = `.github/*baseline*.txt` 各基线文件；收紧一律改基线文件并 commit 留归因。",
        "- 活文档（AGENTS.md/README/ARCHITECTURE）引用本表，不再手抄数字。",
        "",
        "| 基线文件 | 键值 | 用途 |",
        "|---|---|---|",
    ]
    for name, usage in LANES:
        vals = _values(ROOT / ".github" / name)
        if not vals:
            lines.append(f"| {name} | （无键值行） | {usage} |")
            continue
        rendered = "；".join(f"`{k}={v}`" for k, v in vals.items())
        lines.append(f"| {name} | {rendered} | {usage} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    content = generate()
    old = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else None
    if old != content:
        OUT_PATH.write_text(content, encoding="utf-8", newline="\n")
        if old is None:
            print(f"基线总表已生成：{OUT_PATH.name}")
            return 0
        print("基线总表与已提交版本不一致——基线值变更未回写或生成物被手改，拦截。")
        return 1
    print(f"基线总表一致（{len(LANES)} 车道）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
