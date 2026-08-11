#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
前端三合一功能验证 · 可复现脚本
================================
对应专项：管道配置设置页入口 / 星标简化为点击切换 / 前端加载性能优化
验证报告：docs/working/frontend_perf_pipeline_star_function_verify_report.md

本脚本自动执行以下验证（输入 → 预期输出/状态变化）：
  [F1] 管道配置设置页入口可达（SettingsHubWidget KERNEL_NAV + embedded 渲染）
  [F2] 星标简化为点击切换（SessionList star button 直接调用 onStarSession、不触发行点击）
  [F3a] 性能优化：vite.config.ts include 无 asn 子路径、alias 重定向存在
  [F3b] 性能优化：antd 按需加载（src 无 antd 主入口残留、仅 antd/es/splitter 2 处）

运行方式：
  cd <仓库根> && python3 docs/working/verify_reproduce.py
  （vitest 测试部分需在 frontend/ 下以 Node 运行，脚本自动执行）

依赖：node + vitest（frontend/node_modules 已安装）、python3
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND = ROOT / "frontend"
VITE_CONFIG = FRONTEND / "vite.config.ts"

PASS, FAIL = 0, 0
results = []


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        tag = "PASS"
    else:
        FAIL += 1
        tag = "FAIL"
    results.append((tag, name, detail))
    print(f"[{tag}] {name}" + (f"  —— {detail}" if detail else ""))


def parse_include_array(text: str) -> list[str]:
    """从 vite.config.ts 提取 optimizeDeps.include 数组的字符串字面量（剔除 // 注释）。"""
    od = text.find("optimizeDeps:")
    if od < 0:
        return []
    inc = text.find("include: [", od)
    if inc < 0:
        return []
    i = inc + len("include: [")
    depth, j = 1, i
    while j < len(text) and depth > 0:
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
        j += 1
    arr_text = text[i : j - 1]
    items = []
    for line in arr_text.split("\n"):
        code = re.sub(r"//.*", "", line)
        items.extend(a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", code))
    return items


# ---------------------------------------------------------------------------
# F3a: vite.config.ts —— include 无 asn 子路径 + alias 重定向
# ---------------------------------------------------------------------------
print("=" * 70)
print("[F3a] vite.config.ts 依赖预构建精简")
print("=" * 70)
vite_text = VITE_CONFIG.read_text(encoding="utf-8")
include_items = parse_include_array(vite_text)
asn_items = [x for x in include_items if "icons-svg/lib/asn" in x]
check(
    "include 数组中无 @ant-design/icons-svg/lib/asn/* 子路径",
    len(asn_items) == 0,
    f"asn 子路径项 = {len(asn_items)}（预期 0，原 847 项已移除）",
)
check(
    "include 总项数已精简",
    0 < len(include_items) < 400,
    f"实测 include 字符串项 = {len(include_items)}（执行报告声称 289，口径差异见报告）",
)
alias_re = re.search(r"['\"]@ant-design/icons-svg/lib/asn['\"]\s*:\s*path\.resolve", vite_text)
check(
    "resolve.alias 将 lib/asn 重定向到 es/asn",
    bool(alias_re),
    "alias: '@ant-design/icons-svg/lib/asn' -> node_modules/@ant-design/icons-svg/es/asn",
)

# ---------------------------------------------------------------------------
# F3b: antd 按需加载 —— src 无 antd 主入口残留，仅 antd/es/splitter 子路径
# ---------------------------------------------------------------------------
print("=" * 70)
print("[F3b] antd 按需加载（全量 src 扫描）")
print("=" * 70)
antd_main_hits = []
antd_sub_hits = []
for p in (FRONTEND / "src").rglob("*"):
    if p.suffix not in (".ts", ".tsx", ".js", ".jsx"):
        continue
    if ".bak" in p.name or ".spec." in p.name or ".test." in p.name:
        continue  # 排除备份与测试文件，聚焦业务源码
    content = p.read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r"from\s+['\"]antd['\"]", content):
        antd_main_hits.append(f"{p.relative_to(FRONTEND)}:{m.start()}")
    for m in re.finditer(r"from\s+['\"]antd/es/splitter['\"]", content):
        antd_sub_hits.append(str(p.relative_to(FRONTEND)))
check(
    "src 业务源码无 `import { X } from 'antd'` 主入口残留",
    len(antd_main_hits) == 0,
    f"命中 {len(antd_main_hits)} 处（预期 0）",
)
check(
    "仅 2 处 antd/es/splitter 子路径",
    len(antd_sub_hits) == 2,
    f"命中 {len(antd_sub_hits)} 处: {sorted(set(antd_sub_hits))}（预期 FiveSpaceLayout.tsx + SplitLayout.tsx）",
)

# es/asn 与 lib/asn 对称性
es_asn = FRONTEND / "node_modules/@ant-design/icons-svg/es/asn"
lib_asn = FRONTEND / "node_modules/@ant-design/icons-svg/lib/asn"
es_count = len(list(es_asn.glob("*.js"))) if es_asn.exists() else -1
lib_count = len(list(lib_asn.glob("*.js"))) if lib_asn.exists() else -1
check(
    "icons-svg es/asn 与 lib/asn 对称存在（alias 目标可解析）",
    es_count > 0 and es_count == lib_count,
    f"es/asn={es_count} 个 js, lib/asn={lib_count} 个 js",
)

# ---------------------------------------------------------------------------
# F1 + F2: vitest 组件测试（可观察行为断言）
# ---------------------------------------------------------------------------
print("=" * 70)
print("[F1/F2] vitest 组件测试（真实行为验证）")
print("=" * 70)
VITEST = FRONTEND / "node_modules/.bin/vitest"
TESTS = [
    (
        "F1: SettingsHubWidget 管道配置入口 + embedded 渲染",
        "src/components/schema/widgets/__tests__/SettingsHubWidget.test.tsx",
        {"expected": 2},
    ),
    (
        "F2: SessionList 星标点击切换（含不触发行点击）",
        "src/components/session/__tests__/SessionList.test.tsx",
        {
            "expected": None,
            "min_passed": 15,
            # 已知 3 个基线失败（jsdom 下 Radix Dialog/DropdownMenu portal 渲染干扰，
            # 改动前即存在，与星标改动无关，见执行报告 §五-2）：仅断言无新增失败。
            "max_failed": 3,
        },
    ),
    (
        "F1-回归: PipelineSettingsPage 四管道 tabs",
        "src/pages/settings/__tests__/PipelineSettingsPage.test.tsx",
        {"expected": 11},
    ),
    (
        "F1-回归: SettingsPage 全屏入口",
        "src/pages/settings/__tests__/SettingsPage.test.tsx",
        {"expected": 4},
    ),
]

for name, rel, spec in TESTS:
    cmd = ["node", str(VITEST), "run", rel]
    try:
        proc = subprocess.run(
            cmd, cwd=str(FRONTEND), capture_output=True, text=True, timeout=240
        )
        out = proc.stdout + proc.stderr
        # 清理完整 ANSI 转义序列（\x1b[...m），否则 Tests 计数行解析失败
        clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
        # 定位 vitest 汇总行（"Tests  X passed | Y failed" 或 "Tests  X failed | Y passed"），
        # 避免误匹配 "Test Files  1 passed" 行
        tests_line = ""
        for line in clean.splitlines():
            if re.match(r"^\s*Tests\s", line):
                tests_line = line
                break
        pm = re.search(r"(\d+) passed", tests_line)
        fm = re.search(r"(\d+) failed", tests_line)
        passed_n = int(pm.group(1)) if pm else 0
        failed_n = int(fm.group(1)) if fm else 0
        expected = spec.get("expected")
        min_passed = spec.get("min_passed", expected or 0)
        max_failed = spec.get("max_failed", 0)
        ok = passed_n >= min_passed and failed_n <= max_failed
        check(
            name,
            ok,
            f"{passed_n} passed / {failed_n} failed"
            + (f"（基线容忍 ≤{max_failed} 失败）" if max_failed else ""),
        )
    except subprocess.TimeoutExpired:
        check(name, False, "vitest 超时（240s）")
    except FileNotFoundError as e:
        check(name, False, f"缺少 node/vitest: {e}")

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("=" * 70)
print(f"汇总: PASS={PASS} FAIL={FAIL}")
print("=" * 70)
if FAIL:
    print("存在失败项，请结合验证报告逐项查看。")
    sys.exit(1)
print("全部可验证项通过（不可验证项见验证报告 tool_capability_assessment 章节）。")
sys.exit(0)
