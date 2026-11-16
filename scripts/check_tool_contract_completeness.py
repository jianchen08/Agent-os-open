#!/usr/bin/env python3
"""工具契约完备度闸（执行方案批次 4-2，评估 P1-3）。

AGENTS.md 铁律「工具声明即注册须带 output_schema + render」的机械执法：
扫描全部 plugin.json 的 capabilities.tools，缺项即红（基线锁只减不增，
收敛到 0 后恒 0——新工具声明漏键在 PR 阶段拦截）。

判定口径：
- output_schema / render 键存在且非空（dict 非空 / render.card 存在）
- input_schema 键存在且为非空 JSON Schema 对象（内核 K9 对缺失项以 {}
  补注册，LLM 侧 object 过滤恒不触发 = 只能盲调；零参工具须显式写
  {"type": "object", "properties": {}}，空对象视为未声明）
- 基线 .github/tool-contract-baseline.txt：missing_output_schema /
  missing_render / missing_input_schema 三计数只减不增；--init 收紧
  （收敛到 0 后删除基线行即恒 0 执法；基线未列 input_schema 键时按 0
  硬执法）

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


def _has_input_schema(tool: dict) -> bool:
    """input_schema 必须是显式非空 JSON Schema 对象（空对象等同未声明）。"""
    schema = tool.get("input_schema")
    return isinstance(schema, dict) and bool(schema)


def scan_manifest(manifest: dict, rel: str) -> tuple[int, int, int, list[str]]:
    """扫描单个 manifest，返回 (缺 output_schema, 缺 render, 缺 input_schema, 明细)。"""
    miss_schema = 0
    miss_render = 0
    miss_input = 0
    details: list[str] = []
    for tool in (manifest.get("capabilities") or {}).get("tools") or []:
        name = tool.get("name") or tool.get("id") or "?"
        if not tool.get("output_schema"):
            miss_schema += 1
            details.append(f"缺 output_schema: {rel} :: {name}")
        render = tool.get("render")
        if not (isinstance(render, dict) and render.get("card")):
            miss_render += 1
            details.append(f"缺 render: {rel} :: {name}")
        if not _has_input_schema(tool):
            miss_input += 1
            details.append(f"缺 input_schema: {rel} :: {name}")
    return miss_schema, miss_render, miss_input, details


def scan() -> tuple[int, int, int, list[str]]:
    """返回 (缺 output_schema 数, 缺 render 数, 缺 input_schema 数, 明细行)。"""
    miss_schema = 0
    miss_render = 0
    miss_input = 0
    details: list[str] = []
    for mf in sorted(tracked_manifests()):
        rel = str(mf.relative_to(ROOT))
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            details.append(f"解析失败 {rel}: {exc}")
            miss_schema += 1  # 解析失败按缺项计（fail-closed 方向）
            continue
        s, r, i, d = scan_manifest(manifest, rel)
        miss_schema += s
        miss_render += r
        miss_input += i
        details.extend(d)
    return miss_schema, miss_render, miss_input, details


def load_baseline() -> tuple[int, int, int]:
    vals = {"missing_output_schema": 0, "missing_render": 0, "missing_input_schema": 0}
    if not BASELINE_PATH.exists():
        return vals["missing_output_schema"], vals["missing_render"], vals["missing_input_schema"]
    for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        if key.strip() in vals:
            vals[key.strip()] = int(val)
    return vals["missing_output_schema"], vals["missing_render"], vals["missing_input_schema"]


def main() -> int:
    parser = argparse.ArgumentParser(description="工具契约完备度闸")
    parser.add_argument("--init", action="store_true", help="打印当前实测计数（收紧用，只减不增）")
    ns = parser.parse_args()

    miss_schema, miss_render, miss_input, details = scan()
    if ns.init:
        print(f"missing_output_schema={miss_schema}")
        print(f"missing_render={miss_render}")
        print(f"missing_input_schema={miss_input}")
        return 0

    base_schema, base_render, base_input = load_baseline()
    bad = False
    if miss_schema > base_schema:
        print(f"tool-contract: 缺 output_schema {miss_schema} > 基线 {base_schema}（只减不增）——新工具声明漏键，拦截。")
        bad = True
    if miss_render > base_render:
        print(f"tool-contract: 缺 render {miss_render} > 基线 {base_render}（只减不增）——新工具声明漏键，拦截。")
        bad = True
    if miss_input > base_input:
        print(
            f"tool-contract: 缺 input_schema {miss_input} > 基线 {base_input}（只减不增）——"
            '工具声明必须给 input_schema（零参写 {"type": "object", "properties": {}}），拦截。'
        )
        bad = True
    if bad:
        for d in details:
            print(f"  - {d}")
        return 1
    if miss_schema < base_schema or miss_render < base_render or miss_input < base_input:
        print(
            "tool-contract: 通过"
            f"（{miss_schema}/{miss_render}/{miss_input} < 基线 "
            f"{base_schema}/{base_render}/{base_input}，可 --init 收紧并 commit 留归因）。"
        )
    else:
        print(
            "tool-contract: 通过"
            f"（缺 output_schema {miss_schema} / 缺 render {miss_render} / "
            f"缺 input_schema {miss_input}，= 基线）。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
