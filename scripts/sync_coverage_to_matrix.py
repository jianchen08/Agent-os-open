#!/usr/bin/env python
"""矩阵覆盖率列自动回填（阶段 5.3）：从阶段 2 产出的覆盖率报告自动汇总各功能点覆盖率。

阶段 2 已让 CI 产出三语言覆盖率报告：
  - Python: coverage.xml（python-plugins-test --cov-report=xml）
  - 前端:   coverage/coverage-summary.json（vitest v8 json-summary）
  - Rust:   kernel/coverage.lcov（cargo-llvm-cov --lcov）

本脚本读取这些报告（缺失则跳过），按功能点（FP-*）汇总 line%，生成
`docs/coverage_report.md`（自动生成、人工不编辑），供 docs/test_traceability.md
表 B/D 的覆盖率列参照——人工只调"目标列"，"现状列"由本脚本刷新。

用法：
    python scripts/sync_coverage_to_matrix.py                 # 读默认路径报告
    python scripts/sync_coverage_to_matrix.py --print-only    # 只打印不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_MD = ROOT / "docs" / "coverage_report.md"

# 默认报告路径（与阶段 2 CI 产出对齐）
PY_COVERAGE_XML = ROOT / "coverage.xml"
FE_SUMMARY_JSON = ROOT / "frontend" / "coverage" / "coverage-summary.json"
RUST_LCOV = ROOT / "kernel" / "coverage.lcov"

# 功能点 → 覆盖来源映射（模块路径片段 / 报告类型）
# Rust crate → lcov 中 SF 路径含片段；Python → coverage.xml package；前端 → 整体 src。


def lcov_line_pct(lcov: Path) -> float | None:
    covered = total = 0
    for line in lcov.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("DA:"):
            continue
        parts = line[3:].split(",")
        if len(parts) < 2:
            continue
        try:
            total += 1
            if int(parts[1]) > 0:
                covered += 1
        except ValueError:
            continue
    return None if total == 0 else covered / total * 100.0


def lcov_per_path_keyword(lcov: Path) -> dict[str, float]:
    """按 SF 路径关键词（crate 名）分组统计 line%。"""
    groups: dict[str, list[int]] = {}  # keyword -> [covered_count, total_count]
    current_file = ""
    keyword: str | None = None
    for line in lcov.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SF:"):
            current_file = line[3:].replace("\\", "/")
            keyword = _match_crate_keyword(current_file)
            if keyword:
                groups.setdefault(keyword, [0, 0])
        elif line.startswith("DA:") and keyword:
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    groups[keyword][1] += 1
                    if int(parts[1]) > 0:
                        groups[keyword][0] += 1
                except ValueError:
                    pass
        elif line == "end_of_record":
            keyword = None
    return {k: (cov / tot * 100.0 if tot else 0.0) for k, (cov, tot) in groups.items()}


# crate 路径片段 → 功能点
CRATE_TO_FP = {
    "engine": "FP-0.2.〇",
    "/core/": "FP-0.2.〇",
    "plugin-loader": "FP-0.2.一",
    "invoker": "FP-0.2.一",
    "/mcp/": "FP-0.2.一",
    "tenant": "FP-0.2.八",
    "session": "FP-0.2.八",
    "/api/": "FP-DB",
    "config": "FP-0.2.CFG",
}


def _match_crate_keyword(path: str) -> str | None:
    for frag in CRATE_TO_FP:
        if frag in path:
            return frag
    return None


def python_overall_line_pct(xml_path: Path) -> float | None:
    """coverage.xml 根 line-rate（0~1）→ 百分比。"""
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError):
        return None
    rate = root.attrib.get("line-rate")
    if rate is None:
        return None
    try:
        return float(rate) * 100.0
    except ValueError:
        return None


def frontend_overall_line_pct(json_path: Path) -> float | None:
    """vitest json-summary total.lines.pct。"""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    try:
        return float(data["total"]["lines"]["pct"])
    except (KeyError, TypeError, ValueError):
        return None


def gather() -> dict[str, str | float]:
    """收集各功能点的覆盖率。值缺失记为 'N/A'。"""
    result: dict[str, str | float] = {}

    # Rust：按 crate 分组（lcov）
    if RUST_LCOV.exists():
        per_kw = lcov_per_path_keyword(RUST_LCOV)
        # 按 FP 聚合（同 FP 多 crate 取平均，简单近似）
        fp_vals: dict[str, list[float]] = {}
        for kw, pct in per_kw.items():
            fp = CRATE_TO_FP.get(kw)
            if fp:
                fp_vals.setdefault(fp, []).append(pct)
        for fp, vals in fp_vals.items():
            result[fp] = round(sum(vals) / len(vals), 1)
        result["__rust_overall__"] = round(lcov_line_pct(RUST_LCOV) or 0.0, 1)

    # Python plugins 整体 → FP-0.2.二/六
    py = python_overall_line_pct(PY_COVERAGE_XML)
    if py is not None:
        result["FP-0.2.二/六"] = round(py, 1)

    # 前端整体 → FP-0.2.四/五
    fe = frontend_overall_line_pct(FE_SUMMARY_JSON)
    if fe is not None:
        result["FP-0.2.四/五"] = round(fe, 1)

    return result


FP_LABEL = {
    "FP-0.2.〇": "管道引擎（kernel engine/core）",
    "FP-0.2.一": "插件协议（plugin-loader/invoker/mcp）",
    "FP-0.2.二/六": "内部模块 manifest（Python plugins 整体）",
    "FP-0.2.四/五": "前端 Schema/审批（frontend src 整体）",
    "FP-0.2.八": "多租户（kernel tenant/session）",
    "FP-DB": "统一数据接口（kernel api）",
    "FP-0.2.CFG": "配置注入（kernel config）",
}


def render(data: dict[str, str | float]) -> str:
    lines = [
        "# 覆盖率自动报告",
        "",
        "> 由 `scripts/sync_coverage_to_matrix.py` 自动生成——**请勿手工编辑**。",
        "> 数据源：阶段 2 CI 产出的 coverage.xml（Python）/ coverage-summary.json（前端）" "/ coverage.lcov（Rust）。",
        "> 供 `docs/test_traceability.md` 表 B/D 的覆盖率列参照；人工只调目标列。",
        "",
        "| 功能点 | 范围 | 现状 line% |",
        "|--------|------|-----------|",
    ]
    fps = [k for k in data if k.startswith("FP-")]
    for fp in sorted(fps):
        val = data[fp]
        label = FP_LABEL.get(fp, fp)
        lines.append(f"| {fp} | {label} | {val} |")
    if "__rust_overall__" in data:
        lines.append(f"| (Rust 整体) | 全 workspace lcov | {data['__rust_overall__']} |")
    lines.append("")
    lines.append(
        "*缺失项 = 对应报告未生成（CI 未跑或路径不同）。运行 " "`python scripts/sync_coverage_to_matrix.py` 刷新。*"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="矩阵覆盖率列自动回填")
    parser.add_argument("--print-only", action="store_true", help="只打印不写文件")
    args = parser.parse_args()

    data = gather()
    md = render(data)
    if args.print_only or not data:
        print(md)
        if not data:
            print("(未找到任何覆盖率报告，请先生成 coverage.xml / coverage-summary.json / coverage.lcov)")
        return 0

    OUT_MD.write_text(md, encoding="utf-8")
    print(f"已写入 {OUT_MD.relative_to(ROOT)}")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
