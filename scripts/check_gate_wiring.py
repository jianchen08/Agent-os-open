#!/usr/bin/env python3
"""元门禁 gate-wiring：门禁体系的门禁（V1–V8，执行方案批次 M）。

「门禁本身也应该是门禁」的机械化。八项校验（红的条件）：

  V1 正向全集   wiring=="ci" 的门禁无任何 workflow --filter 引用；
                wiring!="ci" 而 wiring_reason 为空
  V2 反向防漂   workflow --filter 引用了 GATES 不存在的 id（typo/改名 PR 阶段红）
  V3 调用形态   CI 出现 run_gates --mode 调用（破坏穷尽集设计与静态可析性）
  V4 观察型棘轮 wiring=="observation" 数量超 .github/gate-wiring-baseline.txt
  V5 fast 预算  fast 门禁 est_seconds 总和 > FAST_BUDGET_SECONDS（未声明即红）
  V6 文件卫生   跟踪树内 *baseline* 文件不在 .github/ 下；或存在跟踪 *.bak
  V7 孤儿基线   基线文件无消费方（gate 命令 / gate 引用的 scripts 检查器 /
                workflow 文本 三方均不提及）
  V8 基线覆盖   ci-baseline-checklist 逐行核对：covered 行 satisfied_by 必须
                存在且接线；exempt 必须给理由；missing 计数超基线

自举：本检查器注册为 GATES 门禁 `gate-wiring`（fast），V1 同样要求它被
ci.yml 引用——未接线时红在自己身上。

数据源 = `run_gates.py --list --json`（CLI 合同，不 import 不重抄 GATES）。
实现说明：workflow 解析用行扫描而非 yaml.safe_load——元门禁自身零第三方
依赖，任何环境可跑；行扫描无解析失败态，方向恒 fail-closed。

用法：
    python scripts/check_gate_wiring.py            # 八项校验
    python scripts/check_gate_wiring.py --init     # 打印当前基线值（供收紧）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CHECKLIST_PATH = ROOT / ".github" / "ci-baseline-checklist.txt"
WIRING_BASELINE_PATH = ROOT / ".github" / "gate-wiring-baseline.txt"

FAST_BUDGET_SECONDS = 90  # V5：fast 档估时总和上限（改值 = 改代码 = commit 归因）

FILTER_RE = re.compile(r"run_gates\.py[^\n]*?--filter[ =]+([A-Za-z0-9_,\-]+)")
MODE_RE = re.compile(r"run_gates\.py[^\n]*?--mode\b")
JOB_RE = re.compile(r"^  ([A-Za-z][A-Za-z0-9_\-]*):\s*$", re.M)


# ── 数据源 ───────────────────────────────────────────────────────────
def load_gates() -> list[dict]:
    """经 CLI 合同取 GATES 全集（单一真值，不 import run_gates）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_gates.py"), "--list", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gate-wiring: run_gates --list --json 失败：{proc.stderr}")
    return json.loads(proc.stdout)


def parse_workflows() -> tuple[set[str], set[str], list[str], dict[str, str]]:
    """行扫描 workflows/：返回 (引用的 gate id, job 名, --mode 违规, 各文件文本)。"""
    referenced: set[str] = set()
    jobs: set[str] = set()
    mode_violations: list[str] = []
    texts: dict[str, str] = {}
    for wf in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        rel = str(wf.relative_to(ROOT)).replace("\\", "/")
        texts[rel] = text
        jobs.update(JOB_RE.findall(text))
        for line in text.splitlines():
            if "run_gates.py" not in line:
                continue
            m = FILTER_RE.search(line)
            if m:
                referenced.update(x.strip() for x in m.group(1).split(",") if x.strip())
            if MODE_RE.search(line):
                mode_violations.append(f"{rel}: {line.strip()[:120]}")
    return referenced, jobs, mode_violations, texts


def tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gate-wiring: git ls-files 失败：{proc.stderr}")
    return [p.replace("\\", "/") for p in proc.stdout.splitlines() if p.strip()]


def load_wiring_baseline() -> dict[str, int]:
    values: dict[str, int] = {}
    for raw in WIRING_BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, cnt = line.partition("=")
        values[key.strip()] = int(cnt)
    return values


def parse_checklist() -> list[dict]:
    """行解析 ci-baseline-checklist.txt → [{capability, ids, status, note}]。"""
    rows: list[dict] = []
    for raw in CHECKLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            rows.append({"capability": line.strip()[:60], "ids": [], "status": "malformed", "note": ""})
            continue
        cap, ids_raw, status = parts[0], parts[1], parts[2]
        note = parts[3] if len(parts) > 3 else ""
        ids = [x.strip() for x in ids_raw.split(",") if x.strip() and x.strip() != "-"]
        rows.append({"capability": cap, "ids": ids, "status": status, "note": note})
    return rows


# ── 八项校验（纯函数，便于删值实验单测）─────────────────────────────
def v1_wiring(gates: list[dict], referenced: set[str]) -> list[str]:
    out: list[str] = []
    for g in gates:
        if g["wiring"] == "ci" and g["id"] not in referenced:
            out.append(f"V1 门禁 {g['id']} wiring=ci 但无任何 workflow --filter 引用（死门禁）")
        if g["wiring"] != "ci" and not g["wiring_reason"]:
            out.append(f"V1 门禁 {g['id']} wiring={g['wiring']} 但缺 wiring_reason")
    return out


def v2_reverse(gates: list[dict], referenced: set[str]) -> list[str]:
    known = {g["id"] for g in gates}
    return [f"V2 workflow 引用了不存在的门禁 id: {x}" for x in sorted(referenced - known)]


def v3_mode(mode_calls: list[str]) -> list[str]:
    return [f"V3 CI 出现 --mode 调用（须 --filter 点名）: {c}" for c in mode_calls]


def v4_observation(gates: list[dict], baseline: dict[str, int]) -> list[str]:
    n = sum(1 for g in gates if g["wiring"] == "observation")
    cap = baseline.get("observation_max", 0)
    if n > cap:
        return [f"V4 observation 门禁数 {n} > 基线 {cap}（降级须归因并收紧前先减存量）"]
    return []


def v5_fast_budget(gates: list[dict]) -> list[str]:
    out: list[str] = []
    undeclared = [g["id"] for g in gates if g["fast"] and g["est_seconds"] is None]
    if undeclared:
        out.append(f"V5 fast 门禁未声明 est_seconds: {sorted(undeclared)}")
    total = sum(g["est_seconds"] or 0 for g in gates if g["fast"])
    if total > FAST_BUDGET_SECONDS:
        out.append(f"V5 fast 档估时总和 {total}s > 预算 {FAST_BUDGET_SECONDS}s")
    return out


def _ledger_files(tracked: list[str]) -> list[str]:
    """基线账本族 = 文件名含 baseline 且为 .txt/.json（检查器/测试/文档的
    .py/.md/.yaml 不算账本——首跑实证误报 lint-baseline.md 等同名词面）。"""
    return [
        p
        for p in tracked
        if "baseline" in p.rsplit("/", 1)[-1] and p.rsplit(".", 1)[-1] in ("txt", "json")
    ]


def v6_files(tracked: list[str]) -> list[str]:
    out = []
    for p in _ledger_files(tracked):
        if not p.startswith(".github/"):
            out.append(f"V6 基线账本错位（必须在 .github/）: {p}")
    baks = [p for p in tracked if p.endswith(".bak")]
    if baks:
        out.append(f"V6 跟踪树存在 .bak 残留: {sorted(baks)}")
    return out


def v7_orphan_baseline(
    gates: list[dict], tracked: list[str], workflow_texts: dict[str, str]
) -> list[str]:
    baseline_files = _ledger_files(tracked)
    gate_cmds = [g.get("command", "") for g in gates]
    # 两跳链：gate 命令引用的 scripts 检查器，其源码消费基线
    script_sources: list[str] = []
    for p in tracked:
        if p.startswith("scripts/") and p.endswith(".py"):
            if any(p in cmd for cmd in gate_cmds):
                try:
                    script_sources.append((ROOT / p).read_text(encoding="utf-8"))
                except OSError:
                    pass
    wf_text = "\n".join(workflow_texts.values())
    out: list[str] = []
    for p in sorted(baseline_files):
        name = p.rsplit("/", 1)[-1]
        consumed = (
            any(name in cmd for cmd in gate_cmds)
            or any(name in src for src in script_sources)
            or (name in wf_text)
        )
        if not consumed:
            out.append(f"V7 基线文件无消费方（孤儿账本）: {p}")
    return out


def v8_checklist(
    gates: list[dict],
    referenced: set[str],
    job_names: set[str],
    rows: list[dict],
    baseline: dict[str, int],
) -> list[str]:
    out: list[str] = []
    by_id = {g["id"]: g for g in gates}
    caps_seen: set[str] = set()
    missing = 0
    for row in rows:
        cap, ids, status, note = row["capability"], row["ids"], row["status"], row["note"]
        if cap in caps_seen:
            out.append(f"V8 能力行重复: {cap}")
        caps_seen.add(cap)
        if status not in ("covered", "missing", "exempt"):
            out.append(f"V8 能力行 {cap} 状态非法: {status!r}（covered|missing|exempt）")
            continue
        if status == "covered":
            if not ids:
                out.append(f"V8 covered 行无 satisfied_by: {cap}")
            for x in ids:
                if x.startswith("ci:"):
                    if x[3:] not in job_names:
                        out.append(f"V8 {cap}: ci job 不存在: {x}")
                elif x not in by_id:
                    out.append(f"V8 {cap}: satisfied_by 门禁不存在: {x}")
                elif by_id[x]["wiring"] == "ci" and x not in referenced:
                    out.append(f"V8 {cap}: satisfied_by 门禁未接线: {x}")
        elif status == "exempt" and not note:
            out.append(f"V8 exempt 行无理由: {cap}")
        elif status == "missing":
            missing += 1
    cap_max = baseline.get("missing_max", 0)
    if missing > cap_max:
        out.append(f"V8 missing 能力数 {missing} > 基线 {cap_max}（covered↔missing 回退属倒退）")
    return out


# ── 汇总 ─────────────────────────────────────────────────────────────
def run_all() -> tuple[list[str], list[str]]:
    gates = load_gates()
    referenced, job_names, mode_calls, wf_texts = parse_workflows()
    tracked = tracked_files()
    baseline = load_wiring_baseline()
    rows = parse_checklist()

    violations: list[str] = []
    violations += v1_wiring(gates, referenced)
    violations += v2_reverse(gates, referenced)
    violations += v3_mode(mode_calls)
    violations += v4_observation(gates, baseline)
    violations += v5_fast_budget(gates)
    violations += v6_files(tracked)
    violations += v7_orphan_baseline(gates, tracked, wf_texts)
    violations += v8_checklist(gates, referenced, job_names, rows, baseline)

    table = [f"{'门禁 id':<28} {'wiring':<12} 接线判定"]
    for g in gates:
        ok = "✓" if (g["wiring"] != "ci" or g["id"] in referenced) else "✗"
        table.append(f"{g['id']:<28} {g['wiring']:<12} {ok}")
    table.append(f"fast 估时 {sum(g['est_seconds'] or 0 for g in gates if g['fast'])}s / {FAST_BUDGET_SECONDS}s；"
                 f"missing 能力 {sum(1 for r in rows if r['status'] == 'missing')}")
    return violations, table


def main() -> int:
    parser = argparse.ArgumentParser(description="元门禁 gate-wiring（V1–V8）")
    parser.add_argument("--init", action="store_true", help="打印当前基线值（收紧用，禁止上调）")
    ns = parser.parse_args()

    if ns.init:
        gates = load_gates()
        rows = parse_checklist()
        obs = sum(1 for g in gates if g["wiring"] == "observation")
        missing = sum(1 for r in rows if r["status"] == "missing")
        print(f"observation_max={obs}")
        print(f"missing_max={missing}")
        return 0

    violations, table = run_all()
    print("\n".join(table))
    if violations:
        print(f"\ngate-wiring: {len(violations)} 项违规：", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("\ngate-wiring: 八项校验全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
