#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""AgentOS 项目 CI runner（packs/ci/base 出生装入 <项目>/.agentos/ci/run.py）。

语义（设计真值 docs/working/项目类型CI包设计_20261030.md §3.3）：
- 读工厂 gates.json + local/gates.json 并读；同 id → local 覆盖 + warn 横幅；
- 契约校验 fail-closed：缺 id/run、tier/lane 越枚举、checker 文件不存在 → 红 + 报字段；
- lane 过滤；tier block=红即断 / warn=只报不拦；
- checker 子进程隔离（崩溃 ≠ runner 崩溃，非 0/1/2 退出码记 error）；
- 退出码契约 0=绿 / 1=红 / 2=skip（工具缺席 fail-open，违规 fail-closed）；
- staged 清单经 stdin 传入（-z NUL 分隔或按行），全仓扫描型 checker 自行忽略；
- 结果写 last_run.json（时间/各门禁结论/耗时——修复监控的项目侧锚点）。

运行形态：run.py --lane commit [--staged]；仅 Python stdlib（项目环境不可假设三方库）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CI_DIR.parents[1]

LANES = ("commit", "task", "manual")
TIERS = ("block", "warn")
PYTHON_LAUNCHERS = {"python", "python3", "py"}
FACTORY_GATES = CI_DIR / "gates.json"
LOCAL_GATES = CI_DIR / "local" / "gates.json"
LAST_RUN = CI_DIR / "last_run.json"


def parse_staged(raw: str) -> list[str]:
    """解析 staged 清单：NUL 分隔（hook -z）或按行；空输入 → 空清单。"""
    if not raw:
        return []
    parts = raw.split("\0") if "\0" in raw else raw.splitlines()
    return [p for p in parts if p]


def _script_path_of(run: list[str], ci_dir: Path) -> Path | None:
    """若 run 指向本项目内 python 脚本，返回其路径（相对 ci_dir 解析）；否则 None。

    run[1] 以 "-" 开头 = 内联参数（如 -c），无文件可验，返回 None。
    """
    launcher = Path(run[0]).stem.lower()
    if launcher not in PYTHON_LAUNCHERS or len(run) < 2:
        return None
    script = Path(run[1])
    if script.is_absolute() or run[1].startswith("-"):
        return None
    return ci_dir / script


def validate_gates(entries: list[Any], face: str, ci_dir: Path) -> list[str]:
    """门禁声明契约校验（元 CI ①）：返回错误清单（空=合法），每条报出字段。"""
    errors: list[str] = []
    if not isinstance(entries, list):
        return [f"{face}: 门禁声明须为数组/gates 键，实得 {type(entries).__name__}"]
    for i, gate in enumerate(entries):
        at = f"{face}[{i}]"
        if not isinstance(gate, dict):
            errors.append(f"{at}: 条目须为对象")
            continue
        gate_id = gate.get("id")
        if not isinstance(gate_id, str) or not gate_id.strip():
            errors.append(f"{at}.id: 缺失或非字符串")
            gate_id = at
        run = gate.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(t, str) and t for t in run):
            errors.append(f"{at}.run: 须为非空字符串数组")
        elif run[0] and Path(run[0]).stem.lower() in PYTHON_LAUNCHERS and len(run) < 2:
            errors.append(f"{at}.run: python 启动器缺脚本参数")
        elif (script := _script_path_of(run, ci_dir)) is not None and not script.is_file():
            errors.append(f"{at}.run: checker 文件不存在: {run[1]}")
        if gate.get("tier") not in TIERS:
            errors.append(f"{at}.tier: 非法值 {gate.get('tier')!r}（允许 {'|'.join(TIERS)}）")
        if gate.get("lane") not in LANES:
            errors.append(f"{at}.lane: 非法值 {gate.get('lane')!r}（允许 {'|'.join(LANES)}）")
        est = gate.get("est_seconds")
        if not isinstance(est, (int, float)) or isinstance(est, bool) or est <= 0:
            errors.append(f"{at}.est_seconds: 须为正数")
    seen: set[str] = set()
    for gate in entries:
        if isinstance(gate, dict) and isinstance(gate.get("id"), str) and gate["id"].strip():
            if gate["id"] in seen:
                errors.append(f"{face}: 门禁 id 重复: {gate['id']}")
            seen.add(gate["id"])
    return errors


def _read_face(path: Path, face: str) -> tuple[list[Any], list[str]]:
    """读一个声明面：返回（条目, 错误）。文件缺失=空（local 可缺，工厂缺失由调用方报）。"""
    if not path.is_file():
        return [], ([f"{face}: 声明文件缺失: {path}"] if face == "factory" else [])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{face}: 声明文件不可解析: {exc}"]
    if isinstance(data, dict):
        data = data.get("gates")
    if not isinstance(data, list):
        return [], [f"{face}: 声明须为数组或含 gates 键"]
    return data, []


def merge_gates(ci_dir: Path) -> tuple[list[dict], list[str], list[str]]:
    """工厂 + local 并读合并 + 契约校验：返回（合并门禁, warn 横幅, 契约错误）。"""
    errors: list[str] = []
    factory, err = _read_face(ci_dir / "gates.json", "factory")
    errors.extend(err)
    local, err = _read_face(ci_dir / "local" / "gates.json", "local")
    errors.extend(err)
    errors.extend(validate_gates(factory, "factory", ci_dir))
    errors.extend(validate_gates(local, "local", ci_dir))
    warnings: list[str] = []

    def _executable(gate: Any) -> bool:
        return (
            isinstance(gate, dict)
            and isinstance(gate.get("id"), str)
            and bool(gate.get("id", "").strip())
            and isinstance(gate.get("run"), list)
            and bool(gate["run"])
            and gate.get("lane") in LANES
            and gate.get("tier") in TIERS
        )

    valid_factory = [g for g in factory if _executable(g)]
    valid_local = [g for g in local if _executable(g)]
    local_ids = {g["id"] for g in valid_local}
    for gid in sorted(local_ids & {g["id"] for g in valid_factory}):
        warnings.append(f"local 覆盖工厂门禁: {gid}（项目自治优先于出厂默认）")
    merged = [g for g in valid_factory if g["id"] not in local_ids] + valid_local
    for gate in valid_factory:
        gate.setdefault("_source", "factory")
    for gate in valid_local:
        gate["_source"] = "local"
    return merged, warnings, errors


def resolve_launcher(run: list[str]) -> list[str]:
    """python 系启动器换绑当前解释器（runner 活着则解释器必然存在）。"""
    if Path(run[0]).stem.lower() in PYTHON_LAUNCHERS:
        return [sys.executable, *run[1:]]
    return list(run)


def run_gate(gate: dict, staged_text: str) -> dict:
    """跑单门（子进程隔离）：status ∈ pass/fail/skip/error。

    子进程注入 PYTHONUTF8=1 并按字节捕获（UTF-8 解码），不依赖机器 locale。
    """
    gate_id = str(gate.get("id", "?"))
    est = float(gate.get("est_seconds") or 5)
    timeout = max(30.0, est * 5)
    started = time.monotonic()
    exit_code: int | None = None
    stdout_text = stderr_text = ""
    status = "error"
    reason = ""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # 禁字节码落盘：checker 的 __pycache__ 残留在项目内会被产物闸自己拦下
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            resolve_launcher(list(gate["run"])),
            cwd=str(CI_DIR),
            check=False,
            input=staged_text.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        exit_code = completed.returncode
        stdout_text = completed.stdout.decode("utf-8", "replace")
        stderr_text = completed.stderr.decode("utf-8", "replace")
        status = {0: "pass", 1: "fail", 2: "skip"}.get(exit_code, "error")
        if status == "error":
            reason = f"异常退出码 {exit_code}（契约 0/1/2）"
    except subprocess.TimeoutExpired:
        reason = f"超时（>{timeout:.0f}s）"
    except OSError as exc:
        reason = f"无法启动: {exc}"
    findings = [ln for ln in stdout_text.splitlines() if ln.strip()]
    if status == "error" and stderr_text.strip():
        findings.append(stderr_text.strip().splitlines()[-1])
    if status == "fail" and not findings and stderr_text.strip():
        # fail 且零输出=checker 异常崩溃而非真违规——透出 stderr 尾行可诊断
        findings.append(f"checker 异常退出（stderr 尾行）: {stderr_text.strip().splitlines()[-1]}")
    if status == "skip" and not reason:
        reason = "；".join(findings) or "checker 报告跳过"
    return {
        "id": gate_id,
        "source": gate.get("_source", "factory"),
        "tier": gate.get("tier"),
        "status": status,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "findings": findings,
        "reason": reason,
    }


def execute_lane(lane: str, staged_mode: bool) -> int:
    """跑一个 lane：返回进程退出码（0=绿 1=红），并写 last_run.json。"""
    gates, warnings, errors = merge_gates(CI_DIR)
    if errors:
        for line in errors:
            print(f"[agentos-ci] 契约校验红: {line}")
    for line in warnings:
        print(f"[agentos-ci] warn: {line}")

    staged_text = ""
    if staged_mode and not sys.stdin.isatty():
        staged_text = sys.stdin.read()
    selected = [g for g in gates if g.get("lane") == lane]

    results: list[dict] = []
    for gate in selected:
        results.append(run_gate(gate, staged_text))

    red = bool(errors)
    for result in results:
        tier = str(result.get("tier") or "block")
        if result["status"] in ("fail", "error"):
            if tier == "block":
                red = True
            print(
                f"[agentos-ci] 门禁 {result['id']} ({result['source']}/{tier}): "
                f"{'FAIL' if result['status'] == 'fail' else 'ERROR'}"
            )
        elif result["status"] == "skip":
            print(f"[agentos-ci] 门禁 {result['id']}: SKIP（{result['reason']}）")
        else:
            print(f"[agentos-ci] 门禁 {result['id']} ({result['source']}/{tier}): PASS")
        for finding in result["findings"]:
            print(f"    {finding}")
    print(
        f"[agentos-ci] 汇总: {'红 — 提交被拦，修完上述发现再提（真例外用 --no-verify）' if red else '绿'}"
        f"（{lane} lane：{len(results)} 门）"
    )

    LAST_RUN.write_text(
        json.dumps(
            {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "lane": lane,
                "staged": len(parse_staged(staged_text)) if staged_mode else 0,
                "red": red,
                "warnings": warnings,
                "contract_errors": errors,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 1 if red else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentOS 项目 CI runner")
    parser.add_argument("--lane", choices=LANES, default="commit")
    parser.add_argument("--staged", action="store_true", help="从 stdin 读 staged 清单传给 checker")
    args = parser.parse_args(argv)
    return execute_lane(args.lane, args.staged)


if __name__ == "__main__":
    sys.exit(main())
