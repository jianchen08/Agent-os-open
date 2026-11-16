#!/usr/bin/env python3
"""pre-commit 车道感知触发器（用户裁定 2026-10-30：所有提交都能触发 CI）。

原则「CI 跟随执行空间」的提交面执法：每个 git commit 都触发与其改动车道
对应的本地 CI——文档提交触发文档检查，kernel 提交触发 kernel 快检；构建
产物/超限大文件**爆红不让提交**（阻断式，非事后 CI 才发现）。分层：

- 本钩子（pre-commit，阻断式）：产物红闸（每次提交必跑）+ 改动车道的
  快门禁（run_gates 同一注册表，秒级到分钟级）；
- 打包链链首：run_gates --mode ci 全量 60 门禁（check_ci_gate --local）；
- 推远端：远端 Actions（push 触发，车道判定 changes job）。

车道映射（单源 = run_gates 注册表 id，与 CI 同一套门禁定义）：
  docs 面(.md)      → doc-baseline-sync, doc-paths, traceability-gate, graph-check, working-index-fresh
  kernel/           → kernel-fmt, kernel-clippy
  plugins/shared/**.py + plugins/sdk → plugins-lint
  scripts/          → scripts-lint
  config/           → config-static
  frontend/src      → frontend-lint

产物红闸单源 = check_no_build_artifacts（ADR 2026-10-29-build-artifact-
commit-guard 的 PATH_DENY_PATTERNS + MAX_TRACKED_BYTES），扫描对象从 HEAD
树换成**暂存区 blob**——入库前一刻拦，比 CI 事后发现早一步。

退出码：0 = 放行；1 = 产物红闸或车道快检红；2 = 环境不可判。
绕过：`git commit --no-verify`（真例外专用，绕过即自留无证记录）。

用法：
  python scripts/check_pre_commit.py [--repo <仓库根>] [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_scripts = str(ROOT / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import check_no_build_artifacts as guard  # noqa: E402

#: 车道 → 快门禁 id（run_gates 注册表子集，秒级~分钟级；全量留给打包链）。
LANE_GATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (车道名, 匹配前缀或后缀规则描述, 门禁 ids)
    (
        "docs",
        ".md",
        (
            "doc-baseline-sync",
            "doc-paths",
            "traceability-gate",
            "graph-check",
            "doc-frontmatter",
            "working-index-fresh",
        ),
    ),
    ("kernel", "kernel/", ("kernel-fmt", "kernel-clippy")),
    ("plugins", "plugins/shared/", ("plugins-lint",)),
    ("sdk", "plugins/sdk/", ("sdk-lint",)),
    ("scripts", "scripts/", ("scripts-lint",)),
    ("config", "config/", ("config-static",)),
    ("frontend", "frontend/src/", ("frontend-lint",)),
)

_DOC_SUFFIX = ".md"


def _git(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )


def staged_paths(repo: Path) -> list[str]:
    """暂存区文件清单（相对仓根，含新增/修改/删除；删除项 blob 为空）。"""
    proc = _git("diff", "--cached", "--name-only", "-z", repo=repo)
    if proc.returncode != 0:
        raise RuntimeError(f"git diff --cached 失败：{proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def staged_blob_size(repo: Path, path: str) -> int | None:
    """暂存 blob 字节数；路径在暂存区被删除（无 blob）返回 None。"""
    rev = _git("rev-parse", f":{path}", repo=repo)
    if rev.returncode != 0:
        return None  # 删除：暂存区无该 blob
    size = _git("cat-file", "-s", rev.stdout.strip(), repo=repo)
    if size.returncode != 0:
        return None
    return int(size.stdout.strip())


def classify_lanes(paths: list[str]) -> dict[str, tuple[str, ...]]:
    """按路径分车道 → 去重后的门禁 id 集（保序）。"""
    selected: list[str] = []
    for _name, rule, gate_ids in LANE_GATES:
        if rule.startswith("."):
            hit = any(p.endswith(rule) for p in paths)
        else:
            hit = any(p.startswith(rule) for p in paths)
        if hit:
            for g in gate_ids:
                if g not in selected:
                    selected.append(g)
    return {"gates": tuple(selected)}


def check_staged_artifacts(repo: Path, paths: list[str]) -> list[str]:
    """产物红闸（暂存区面）：路径命中禁入清单或 blob 超限 → 返回红项列表。"""
    reds: list[str] = []
    for path in paths:
        denied = guard.match_denied(path)
        if denied:
            reds.append(f"{path}（产物路径命中禁入清单：{denied}）")
            continue
        size = staged_blob_size(repo, path)
        if size is not None and size > guard.MAX_TRACKED_BYTES:
            reds.append(
                f"{path}（暂存 blob {size / (1024 * 1024):.1f}MB > 硬限 "
                f"{guard.MAX_TRACKED_BYTES / (1024 * 1024):.0f}MB）"
            )
    return reds


def run_lane_gates(gate_ids: tuple[str, ...], repo: Path) -> int:
    """跑车道快门禁（run_gates --filter，同一注册表单源）。"""
    if not gate_ids:
        return 0
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_gates.py"), "--filter", ",".join(gate_ids)],
        cwd=repo,
        check=False,
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pre-commit 车道感知 CI 触发器（产物红闸+车道快检）")
    parser.add_argument("--repo", help="仓库根（缺省脚本所在仓）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的检查，不实际执行")
    ns = parser.parse_args(argv)
    repo = Path(ns.repo).resolve() if ns.repo else ROOT

    try:
        paths = staged_paths(repo)
    except RuntimeError as exc:
        print(f"[pre-commit] FAIL 环境不可判：{exc}")
        return 2
    if not paths:
        print("[pre-commit] PASS 暂存区为空（无事可查）")
        return 0

    # ① 产物红闸（每次提交必跑，阻断式）
    reds = check_staged_artifacts(repo, paths)
    if reds:
        print("[pre-commit] ❌ 构建产物/超限大文件禁入提交（爆红）：")
        for r in reds:
            print(f"  - {r}")
        print("[pre-commit] 处置：产物不入库（gitignore/删出暂存区）；真例外走 --no-verify 自留无证记录")
        return 1

    # ② 车道快检
    gates = classify_lanes(paths)["gates"]
    print(f"[pre-commit] 产物红闸 PASS（{len(paths)} 个暂存路径）")
    if ns.dry_run:
        print(f"[pre-commit] DRY-RUN 将执行车道快检: {','.join(gates) if gates else '（无匹配车道）'}")
        return 0
    if not gates:
        print("[pre-commit] PASS 无匹配车道快检（产物红闸已覆盖）")
        return 0
    print(f"[pre-commit] 车道快检: {','.join(gates)}")
    rc = run_lane_gates(gates, repo)
    if rc != 0:
        print("[pre-commit] ❌ 车道快检红——按上面车道输出逐项清后再提交")
        return 1
    print("[pre-commit] PASS 车道快检全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
