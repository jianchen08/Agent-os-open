#!/usr/bin/env python
"""统一机械门禁入口（quality-gates）：每个可机械检查的承诺都有非零退出命令。

原则（移植自 DSH quality-gates / coverage-exempt-heavy-suites 决策，
落地说明见 docs/working/机械门禁统一入口与覆盖率豁免.md）：

1. **单一事实源**——所有门禁的命令在本文件定义一次；CI job 与本地开发者
   调用同一入口（--mode / --filter），不存在第二套口径。
2. **CI 跑穷尽集**；Git hooks / 本地只留廉价检查（--mode fast）。
3. **非零退出**——任何门禁失败、或因依赖失败被跳过 → 退出码 1
   （观察型 allow_failure 门禁除外）。
4. **有界并行**——按依赖图调度，AGENTOS_GATE_CONCURRENCY 控制上限；
   默认 cap 4（cargo/npm/pytest 同跑防内存爆）。

覆盖率豁免：plugins-coverage（插桩，阈值守护）与 plugins-heavy（免插桩，
94 插件子进程冒烟矩阵）拆两个并行 gate，全部测试仍执行——名单与配对
校验见 scripts/coverage_exempt.py（单一名单点）。

用法：
    python scripts/run_gates.py --list                    # 列出全部门禁
    python scripts/run_gates.py --mode fast               # 本地廉价检查
    python scripts/run_gates.py --mode all                # 穷尽集
    python scripts/run_gates.py --mode kernel|plugins|frontend
    python scripts/run_gates.py --filter kernel-fmt,plugins-coverage   # CI job 精确选择

环境变量：
    AGENTOS_GATE_CONCURRENCY=N   并行上限（默认 min(CPU, 4)）
    AGENTOS_GATE_VERBOSE=1       打印通过门禁的完整输出（CI 建议开）

Python 侧环境前置（与 CI 各 job 的 setup 一致，本地跑一次即可）：
    uv sync --frozen --extra dev --extra isolation
    uv pip install -e ./plugins/sdk
前端 / Electron：frontend/ 与仓库根各自 npm ci（electron 免下载二进制：
ELECTRON_SKIP_BINARY_DOWNLOAD=1）。
Rust 覆盖率门禁需 cargo-llvm-cov（cargo install cargo-llvm-cov 或
taiki-e/install-action）。

门禁归属（非本入口持有、但同为非零退出命令的承诺）：
    pre-commit   → .github/workflows/ci.yml pre-commit job（pre-commit/action）
    python-e2e   → .github/workflows/e2e.yml Python E2E job（需内核二进制 + LLM key，
                   nightly/手动/push main 触发，CI 专用车道）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import coverage_exempt  # noqa: E402 —— 豁免名单单一名单点，防两侧漂移

# 本地易用性：存在 .venv 时自动前置到 PATH（ruff/mypy/pytest 等门禁依赖）。
# CI 各 job 无 .venv（依赖由 setup 步骤装入系统 Python）→ 本行为无操作。
_VENV_BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
if _VENV_BIN.is_dir():
    os.environ["PATH"] = str(_VENV_BIN) + os.pathsep + os.environ.get("PATH", "")

# ── 公共环境 ──────────────────────────────────────────────────────────
# 与原 ci.yml 一致：插件运行时依赖 plugins（平铺模块）+ SDK 源码。
_PYTHONPATH = os.pathsep.join([str(ROOT / "plugins"), str(ROOT / "plugins" / "sdk" / "src")])
_PLUGINS_ENV = {"PYTHONPATH": _PYTHONPATH}


@dataclass(frozen=True)
class Gate:
    """一个机械门禁：命令 + 依赖元数据。

    command 与 shell 二选一：优先 argv 形态（无 shell、跨平台）；
    需要管道 / 后台进程的门禁（tee 捕获 + 基线锁、preview 服务）用
    bash -c 形态。
    """

    id: str
    label: str
    domain: str  # kernel | plugins | frontend | electron | cross
    command: tuple[str, ...] = ()
    shell: str = ""
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    fast: bool = False  # 廉价本地检查（--mode fast / Git hooks）
    allow_failure: bool = False  # 观察型：失败不阻塞


def _shell_join_pytest(args: list[str]) -> str:
    """把 pytest argv 拼成 shell 字符串；-m 的 marker 表达式含空格须加引号。"""
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-m" and i + 1 < len(args):
            out.append('-m "' + args[i + 1] + '"')
            i += 2
        else:
            out.append(args[i])
            i += 1
    return " ".join(out)


GATES: list[Gate] = [
    # ── 内核（Rust，kernel/）──────────────────────────────────────────
    Gate(
        id="kernel-fmt",
        label="cargo fmt --check",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "fmt", "--all", "--", "--check"),
        fast=True,
    ),
    Gate(
        id="kernel-clippy",
        label="cargo clippy -D warnings",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "clippy", "--all-targets", "--", "-D", "warnings"),
    ),
    Gate(
        id="kernel-build",
        label="cargo build (debug)",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "build", "--verbose"),
    ),
    Gate(
        id="kernel-build-release",
        label="cargo build (release)",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "build", "--release", "--verbose"),
        needs=("kernel-build",),  # 同一 target 目录，串行避免 cargo 锁空转
    ),
    Gate(
        id="kernel-test",
        label="cargo test + 失败数基线锁（只减不增）",
        domain="kernel",
        cwd="kernel",
        shell=(
            'T=$(mktemp); cargo test --all 2>&1 | tee "$T"; '
            'python ../scripts/check_rust_test_baseline.py --from-file "$T"'
        ),
        needs=("kernel-build",),
    ),
    Gate(
        id="kernel-coverage",
        label="cargo llvm-cov line% 基线锁（只升不降）",
        domain="kernel",
        cwd="kernel",
        shell=(
            "cargo llvm-cov --workspace --exclude agentos-integration-tests "
            "--lcov --output-path coverage.lcov --ignore-run-fail "
            "&& python ../scripts/check_rust_coverage_baseline.py --lcov coverage.lcov"
        ),
    ),
    Gate(
        # Rust 改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。scope 限
        # kernel/crates（库 crate，由各自单测驱动）；kernel/src 主进程 bin
        # 由 e2e 车道覆盖、build.rs 编译期运行不进 llvm-cov 口径，均不在 scope。
        id="kernel-diff-coverage",
        label="Rust 改动行覆盖率 100%（diff coverage）",
        domain="kernel",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "kernel/coverage.lcov",
            "--format",
            "lcov",
            "--scope",
            "kernel/crates",
            "--ext",
            ".rs",
            "--omit",
            r"build\.rs$",
        ),
        needs=("kernel-coverage",),
    ),
    # ── 插件 SDK（plugins/sdk，独立包）────────────────────────────────
    Gate(
        id="sdk-lint",
        label="SDK ruff",
        domain="plugins",
        cwd="plugins/sdk",
        command=("ruff", "check", "."),
        fast=True,
    ),
    Gate(
        id="sdk-mypy",
        label="SDK mypy",
        domain="plugins",
        cwd="plugins/sdk",
        command=("mypy", "src/agentos_plugin_sdk"),
        fast=True,
    ),
    Gate(
        id="sdk-test",
        label="SDK pytest",
        domain="plugins",
        cwd="plugins/sdk",
        command=("pytest", "-v"),
    ),
    # ── 插件体系（Python，仓库根，uv.lock 锁定环境）────────────────────
    Gate(
        id="plugins-coverage-pairing",
        label="豁免名单配对校验（--ignore ≡ positional）",
        domain="plugins",
        command=(sys.executable, "scripts/coverage_exempt.py", "--check"),
        fast=True,
    ),
    Gate(
        # C1 渠道合流防复发守卫（2026-08-20）：channel_common 模块名黑名单
        # 不得重回任何 channel_* 目录；负样本演示见
        # docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §1.3 第 6 步执行记录。
        id="channel-copy-guard",
        label="渠道共享拷贝守卫（channel_common 黑名单 0 命中）",
        domain="plugins",
        command=(sys.executable, "scripts/check_channel_copy_guard.py"),
        fast=True,
    ),
    Gate(
        id="plugins-coverage",
        label="插件测试（插桩，免豁免重型套件）+ 失败数基线锁 + 覆盖率基线锁",
        domain="plugins",
        shell=(
            "T=$(mktemp); ( uv run --frozen python -m pytest -v "
            + _shell_join_pytest(coverage_exempt.instrumented_args())
            + " --cov=plugins --cov-report=term-missing --cov-report=xml:coverage.xml"
            + ' 2>&1 || true ) | tee "$T"; '
            'python scripts/check_pytest_failure_baseline.py --lane plugins-coverage --from-file "$T" '
            "&& python scripts/check_python_coverage_baseline.py"
        ),
        env=_PLUGINS_ENV,
    ),
    Gate(
        # Python 改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。omit 与
        # pyproject [tool.coverage.run] omit 对齐（tests 自身/sdk/__init__ 不度量）。
        id="plugins-diff-coverage",
        label="Python 改动行覆盖率 100%（diff coverage）",
        domain="plugins",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "coverage.xml",
            "--format",
            "xml",
            "--scope",
            "plugins",
            "--ext",
            ".py",
            "--omit",
            "^plugins/sdk/",
            "--omit",
            "/tests/",
            "--omit",
            r"/test_[^/]+\.py$",
            "--omit",
            r"/__init__\.py$",
        ),
        needs=("plugins-coverage",),
        env=_PLUGINS_ENV,
    ),
    Gate(
        id="plugins-heavy",
        label="重型套件免插桩（94 插件子进程冒烟矩阵）+ 失败数基线锁",
        domain="plugins",
        shell=(
            "T=$(mktemp); ( uv run --frozen python -m pytest -v "
            + _shell_join_pytest(coverage_exempt.heavy_args())
            + ' 2>&1 || true ) | tee "$T"; '
            'python scripts/check_pytest_failure_baseline.py --lane plugins-heavy --from-file "$T"'
        ),
        env=_PLUGINS_ENV,
    ),
    Gate(
        id="plugins-mypy-baseline",
        label="plugins/shared mypy 基线锁（只减不增）",
        domain="plugins",
        command=("uv", "run", "--frozen", "python", "scripts/check_mypy_baseline.py"),
    ),
    Gate(
        id="timing-gate",
        label="时序不变量（-m timing，独立阻塞）",
        domain="plugins",
        command=(
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "pytest",
            "-m",
            "timing",
            "tests/test_isolation_docker_timeout.py",
            "tests/suites/core/test_pipeline_stability.py",
        ),
        env=_PLUGINS_ENV,
    ),
    # ── 前端（frontend/）──────────────────────────────────────────────
    Gate(
        id="frontend-typecheck",
        label="tsc --noEmit",
        domain="frontend",
        cwd="frontend",
        command=("npm", "run", "typecheck"),
        fast=True,
    ),
    Gate(
        id="frontend-lint",
        label="eslint + 基线锁（只减不增）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'T=$(mktemp); ( npm run lint 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_frontend_baseline.py --eslint-file "$T"'
        ),
    ),
    Gate(
        id="frontend-coverage",
        label="vitest 覆盖率%基线锁 + 失败数基线锁（只减不增/只升不降）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'T=$(mktemp); ( npm run test:coverage 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_frontend_baseline.py --vitest-file "$T"'
        ),
    ),
    Gate(
        # 前端改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。omit 与
        # frontend/vitest.config.ts coverage.exclude 对齐（测试自身/main.tsx/
        # d.ts 不度量）。lcov 产物 = frontend/coverage/lcov.info。
        id="frontend-diff-coverage",
        label="前端改动行覆盖率 100%（diff coverage）",
        domain="frontend",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "frontend/coverage/lcov.info",
            "--format",
            "lcov",
            "--scope",
            "frontend/src",
            "--ext",
            ".ts",
            "--ext",
            ".tsx",
            "--omit",
            "/__tests__/",
            "--omit",
            r"\.test\.(ts|tsx)$",
            "--omit",
            "^frontend/src/test/",
            "--omit",
            r"\.d\.ts$",
            "--omit",
            r"^frontend/src/main\.tsx$",
        ),
        needs=("frontend-coverage",),
    ),
    Gate(
        id="frontend-e2e-smoke",
        label="playwright 冒烟（vite preview + ci-smoke，零后端依赖）",
        domain="frontend",
        cwd="frontend",
        shell=(
            "npm run build && {\n"
            "  npm run preview -- --port 5188 --strictPort & PREVIEW_PID=$!\n"
            "  for i in $(seq 1 30); do curl -sf http://localhost:5188 >/dev/null && break; sleep 2; done\n"
            "  if ! curl -sf http://localhost:5188 >/dev/null; then\n"
            '    echo "preview 服务未就绪"; kill $PREVIEW_PID || true; exit 1\n'
            "  fi\n"
            "  npx playwright test e2e/specs/ci-smoke.spec.ts\n"
            "  RC=$?\n"
            "  kill $PREVIEW_PID || true\n"
            "  exit $RC\n"
            "}"
        ),
    ),
    # ── Electron 桌面壳（electron/，root npm 环境）────────────────────
    Gate(
        id="electron-compile",
        label="Electron 主进程 tsc 编译（electron:compile）",
        domain="electron",
        command=("npm", "run", "electron:compile"),
        fast=True,
    ),
    # ── 横切（仓库级）────────────────────────────────────────────────
    Gate(
        id="tdd-gate",
        label="TDD 合规（变更必有测试）",
        domain="cross",
        command=(sys.executable, "scripts/check_tdd_compliance.py"),
    ),
    Gate(
        id="traceability-gate",
        label="@feature 追溯闭环（非法标记硬失败 + 未标记基线锁）",
        domain="cross",
        command=(sys.executable, "scripts/check_test_traceability.py"),
        fast=True,
    ),
]

# all = 穷尽集（本入口持有的全部门禁；pre-commit/python-e2e 见模块 docstring 归属说明）
MODES: dict[str, list[str]] = {
    "fast": [g.id for g in GATES if g.fast],
    "kernel": [g.id for g in GATES if g.domain == "kernel"],
    "plugins": [g.id for g in GATES if g.domain == "plugins"],
    "frontend": [g.id for g in GATES if g.domain == "frontend"],
    "all": [g.id for g in GATES],
}


# ── 门禁图校验（重复 id / 未知依赖 / 环）──────────────────────────────
def validate_graph(gates: list[Gate]) -> None:
    ids = [g.id for g in gates]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"run_gates: 重复门禁 id: {dupes}")
    # 依赖可以指向选中集之外的门禁（CI job 用 --filter 拆开时，依赖关系
    # 由 workflow 的 job needs: 承担），只要它在全量清单里存在即可。
    known = {g.id for g in GATES}
    for gate in gates:
        for dep in gate.needs:
            if dep not in known:
                raise ValueError(f"run_gates: 门禁 {gate.id} 依赖未知门禁 {dep}")
    # Kahn 拓扑排序检环（仅选中集内部的边）
    in_set = set(ids)
    indegree = {g.id: 0 for g in gates}
    dependents: dict[str, list[str]] = {g.id: [] for g in gates}
    for gate in gates:
        for dep in gate.needs:
            if dep in in_set:
                indegree[gate.id] += 1
                dependents[dep].append(gate.id)
    queue = [gid for gid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        gid = queue.pop()
        seen += 1
        for nxt in dependents[gid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(gates):
        cyclic = sorted(gid for gid, deg in indegree.items() if deg > 0)
        raise ValueError(f"run_gates: 依赖环: {cyclic}")


# ── 执行 ─────────────────────────────────────────────────────────────
def resolve_exe(name: str) -> str:
    """解析可执行文件全路径（Windows 下 npm/cargo 等需展开 .cmd/.exe）。"""
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"run_gates: 找不到可执行文件 {name!r}（PATH 或环境前置缺失，见模块 docstring）")
    return resolved


def build_argv(gate: Gate) -> list[str]:
    if gate.command:
        argv = [resolve_exe(gate.command[0]), *gate.command[1:]]
    elif gate.shell:
        argv = [resolve_exe("bash"), "-c", gate.shell]
    else:
        raise ValueError(f"run_gates: 门禁 {gate.id} 未定义 command/shell")
    return argv


def run_gate(gate: Gate) -> tuple[str, int | None, str, float]:
    """执行一个门禁，返回 (状态, 退出码, 输出, 耗时秒)。"""
    started = time.perf_counter()
    try:
        argv = build_argv(gate)
    except (FileNotFoundError, ValueError) as exc:
        return "failed", None, f"{exc}\n", 0.0
    env = {**os.environ, **gate.env}
    try:
        proc = subprocess.run(  # noqa: S603 —— argv 已按白名单构造
            argv,
            cwd=str(ROOT / gate.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = proc.stdout or ""
        status = "passed" if proc.returncode == 0 else "failed"
        return status, proc.returncode, output, time.perf_counter() - started
    except OSError as exc:
        return "failed", None, f"启动失败: {exc}\n", time.perf_counter() - started


def _print_result(gate: Gate, status: str, seconds: float, output: str, exit_code: int | None) -> None:
    verbose = os.environ.get("AGENTOS_GATE_VERBOSE") == "1"
    tag = status.upper()
    if status == "passed":
        print(f"run_gates: PASS {gate.label} ({seconds:.1f}s)", flush=True)
        if verbose and output:
            print(output, end="", flush=True)
        return
    print(f"\n== {tag} {gate.label} ({seconds:.1f}s) ==", flush=True)
    print(f"run_gates: 门禁 {gate.id} 失败，完整输出：", flush=True)
    print(output, end="", flush=True)


def run_selected(gates: list[Gate], max_active: int) -> int:
    """按依赖图有界并行执行，返回进程退出码。"""
    validate_graph(gates)
    by_id = {g.id: g for g in gates}
    in_set = set(by_id)
    state: dict[str, str] = {g.id: "pending" for g in gates}
    results: dict[str, tuple[str, int | None, str, float]] = {}
    running: dict[Future, str] = {}

    def deps_passed(gate: Gate) -> bool:
        return all(state[d] == "passed" for d in gate.needs if d in in_set)

    def deps_failed(gate: Gate) -> bool:
        return any(state[d] in ("failed", "skipped") for d in gate.needs if d in in_set)

    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=max_active)
    started_at = time.perf_counter()
    try:
        while True:
            # 尽量填满并发槽（就绪 = pending 且选中集内依赖全部 passed）
            for gate in gates:
                if len(running) >= max_active:
                    break
                if state[gate.id] != "pending":
                    continue
                if deps_passed(gate):
                    state[gate.id] = "running"
                    print(f"run_gates: start {gate.label}", flush=True)
                    fut = pool.submit(run_gate, gate)
                    running[fut] = gate.id
            if not running:
                # 无在跑且无可启动：级联标记被失败依赖阻塞的剩余门禁为 skipped
                progressed = True
                while progressed:
                    progressed = False
                    for gate in gates:
                        if state[gate.id] != "pending":
                            continue
                        if deps_failed(gate):
                            state[gate.id] = "skipped"
                            results[gate.id] = ("skipped", None, "", 0.0)
                            print(f"run_gates: SKIP {gate.label}（依赖失败）", flush=True)
                            progressed = True
                break
            done, _ = wait(set(running), return_when=FIRST_COMPLETED)
            for fut in done:
                gid = running.pop(fut)
                status, exit_code, output, seconds = fut.result()
                state[gid] = status
                results[gid] = (status, exit_code, output, seconds)
                _print_result(by_id[gid], status, seconds, output, exit_code)
    finally:
        pool.shutdown(wait=True)

    total = time.perf_counter() - started_at
    passed = sum(1 for s, *_ in results.values() if s == "passed")
    failed = sum(1 for s, *_ in results.values() if s == "failed")
    skipped = sum(1 for s, *_ in results.values() if s == "skipped")
    print(f"\nrun_gates: {passed} passed, {failed} failed, {skipped} skipped in {total:.1f}s.", flush=True)

    blocking = [
        (by_id[gid], results[gid])
        for gid, (status, *_) in results.items()
        if status in ("failed", "skipped") and not by_id[gid].allow_failure
    ]
    if blocking:
        print("run_gates: 未通过门禁：", file=sys.stderr)
        for gate, (_, exit_code, _, seconds) in blocking:
            disposition = "" if not gate.allow_failure else "（观察型）"
            print(
                f"  - {gate.id}{disposition}: {gate.label} (exit={exit_code}, {seconds:.1f}s)",
                file=sys.stderr,
            )
        return 1
    return 0


def list_gates() -> int:
    print(f"{'门禁 id':<28} {'域':<10} {'fast':<5} {'needs':<20} 命令")
    for g in GATES:
        cmd = g.shell if g.shell else " ".join(g.command)
        needs = ",".join(g.needs) or "-"
        fast = "✓" if g.fast else "-"
        print(f"{g.id:<28} {g.domain:<10} {fast:<5} {needs:<20} {cmd}")
    print("\n模式：")
    for mode, ids in MODES.items():
        print(f"  {mode:<10} {len(ids)} 个门禁")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="统一机械门禁入口（quality-gates）")
    parser.add_argument("--list", action="store_true", help="列出全部门禁")
    parser.add_argument("--mode", choices=sorted(MODES), help="按模式选择门禁聚合")
    parser.add_argument("--filter", help="逗号分隔的门禁 id 精确选择（CI job 用）")
    ns = parser.parse_args()

    if ns.list:
        return list_gates()
    if bool(ns.mode) == bool(ns.filter):
        parser.error("必须且只能指定 --mode 或 --filter 其一")

    known = {g.id for g in GATES}
    if ns.filter:
        wanted = [x.strip() for x in ns.filter.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in known]
        if unknown:
            print(f"run_gates: 未知门禁 id: {unknown}（--list 查看）", file=sys.stderr)
            return 2
        selected = [g for g in GATES if g.id in set(wanted)]
    else:
        selected = [g for g in GATES if g.id in set(MODES[ns.mode])]

    if not selected:
        print("run_gates: 未选中任何门禁", file=sys.stderr)
        return 2

    raw = os.environ.get("AGENTOS_GATE_CONCURRENCY", "")
    if raw:
        max_active = int(raw)
        if max_active < 1:
            parser.error("AGENTOS_GATE_CONCURRENCY 须为正整数")
        source = "$AGENTOS_GATE_CONCURRENCY"
    else:
        max_active = min(4, os.cpu_count() or 1, len(selected)) or 1
        source = "默认 cap 4"
    print(f"run_gates: 运行 {len(selected)} 个门禁，并行 {max_active}（{source}）。", flush=True)
    return run_selected(selected, max_active)


if __name__ == "__main__":
    sys.exit(main())
