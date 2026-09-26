#!/usr/bin/env python3
"""工具契约完备度闸（执行方案批次 4-2，评估 P1-3）。

AGENTS.md 铁律「工具声明即注册须带 output_schema + render」的机械执法：
扫描全部 plugin.json 的 capabilities.tools，缺项即红（基线锁只减不增，
收敛到 0 后恒 0——新工具声明漏键在 PR 阶段拦截）。

判定口径：
- output_schema / render 键存在且非空（dict 非空 / render.card 存在）
- 基线 .github/tool-contract-baseline.txt：missing_output_schema / missing_render
  两计数只减不增；--init 收紧（收敛到 0 后删除基线行即恒 0 执法）

用法：
    python scripts/check_tool_contract_completeness.py
    python scripts/check_tool_contract_completeness.py --init
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".github" / "tool-contract-baseline.txt"


def tracked_manifests() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "*plugin.json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files 失败: {proc.stderr}")
    out = []
    for raw in proc.stdout.splitlines():
        rel = raw.strip()
        if rel.startswith("plugins/") and rel.endswith("plugin.json"):
            out.append(ROOT / rel)
    return out


def scan() -> tuple[int, int, list[str]]:
    """返回 (缺 output_schema 数, 缺 render 数, 明细行)。"""
    miss_schema = 0
    miss_render = 0
    details: list[str] = []
    for mf in sorted(tracked_manifests()):
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            details.append(f"解析失败 {mf.relative_to(ROOT)}: {exc}")
            miss_schema += 1  # 解析失败按缺项计（fail-closed 方向）
            continue
        for tool in (manifest.get("capabilities") or {}).get("tools") or []:
            name = tool.get("name") or tool.get("id") or "?"
            if not tool.get("output_schema"):
                miss_schema += 1
                details.append(f"缺 output_schema: {mf.relative_to(ROOT)} :: {name}")
            render = tool.get("render")
            if not (isinstance(render, dict) and render.get("card")):
                miss_render += 1
                details.append(f"缺 render: {mf.relative_to(ROOT)} :: {name}")
    return miss_schema, miss_render, details


def load_baseline() -> tuple[int, int]:
    vals = {"missing_output_schema": 0, "missing_render": 0}
    if not BASELINE_PATH.exists():
        return vals["missing_output_schema"], vals["missing_render"]
    for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        if key.strip() in vals:
            vals[key.strip()] = int(val)
    return vals["missing_output_schema"], vals["missing_render"]


def main() -> int:
    parser = argparse.ArgumentParser(description="工具契约完备度闸")
    parser.add_argument("--init", action="store_true", help="打印当前实测计数（收紧用，只减不增）")
    ns = parser.parse_args()

    miss_schema, miss_render, details = scan()
    if ns.init:
        print(f"missing_output_schema={miss_schema}")
        print(f"missing_render={miss_render}")
        return 0

    base_schema, base_render = load_baseline()
    bad = False
    if miss_schema > base_schema:
        print(f"tool-contract: 缺 output_schema {miss_schema} > 基线 {base_schema}（只减不增）——新工具声明漏键，拦截。")
        bad = True
    if miss_render > base_render:
        print(f"tool-contract: 缺 render {miss_render} > 基线 {base_render}（只减不增）——新工具声明漏键，拦截。")
        bad = True
    if bad:
        for d in details:
            print(f"  - {d}")
        return 1
    if miss_schema < base_schema or miss_render < base_render:
        print(f"tool-contract: 通过（{miss_schema}/{miss_render} < 基线 {base_schema}/{base_render}，可 --init 收紧并 commit 留归因）。")
    else:
        print(f"tool-contract: 通过（缺 output_schema {miss_schema} / 缺 render {miss_render}，= 基线）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
