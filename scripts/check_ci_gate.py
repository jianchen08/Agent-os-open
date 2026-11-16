#!/usr/bin/env python3
"""打包/部署 CI 门禁执法（用户裁定 2026-10-28「CI 不过不打包不部署」 +
2026-11-06「打包 CI 不跑全矩阵：改动相关 + 打包相关 + 一个功能验证」）。

裁定：装机版/开发版反复因环境问题翻车；任何「打包通过」「部署通过」的声明，
其前置必须是该 commit 的 CI 必经面全绿——机械执法 = 本脚本挂在打包链
（package.json `electron:build`）与发布管线（deploy/release_all.sh preflight）
链首。必经面按 kind 分派：

  package（2026-11-06 收敛）：
    - ci.yml        改动相关：workflow 内建 changes 车道分档（ci_changed_areas
                    → if: 跳过 irrelevant lanes），绿 = 改动相关车道绿；
    - env-gate.yml  打包相关：job 级只取 Pack Windows（打包链实测 + 装后冒烟），
                    deploy-env 属部署面不拦打包；
    - e2e.yml       功能验证：push 触发 smoke 车道（auth 全链 + 一轮
                    stub-LLM 管道 chat；全量套件收 nightly/dispatch）。
  deploy / release：ci.yml + env-gate.yml 全量（部署声明含 deploy-env）。

判定口径（paths 车道感知的「最近绿基线」算法）：
  - 在 HEAD 的祖先集（含自身）里取最近一次 conclusion=success 的运行作基线
    （paths 过滤导致中间提交无运行时，由更早的绿基线覆盖——车道语义等价）；
  - 基线之后任何 failure/cancelled/timed_out 运行未被更晚成功压过 → 红
    （该提交引入过环境破坏且未修复）；
  - 基线之后存在 in_progress/queued 等在跑运行 → 红「CI 在跑」（等绿再打包）；
  - 该 workflow 在分支上从未跑过 → 红（fail-closed：无法取证 = 不通过）。

取证源 = GitHub Actions runs API（公开仓匿名可读；GITHUB_TOKEN/GH_TOKEN 在场
则携带以避开限流）；祖先判定用本地 `git merge-base --is-ancestor`。API 网络失败
/ 非 git 仓 / 远端非 GitHub → 非零退出（exit 2），门恒不假绿。

CI 自跑豁免：env-gate.yml 的 pack-windows job 内 electron:build 链首会再调
本脚本——查自己正在跑的运行必得「在跑」假红。该 job 显式设
AGENTOS_CI_GATE_IN_RUN=1（见 workflow 注释）时本脚本打印 SELF-RUN 标记后
放行；本机伪造该变量 = 自违裁定，输出留 SELF-RUN 痕迹可审计。

本地取证模式（用户裁定 2026-10-30「CI 跟随执行空间」）：本地提交+本地打包
时 CI 就该在本地跑——`--local`（或 AGENTOS_CI_GATE_LOCAL=1）本地执法；
kind=package 走 PACKAGE-FAST：改动面分档（ci_changed_areas.compute_lanes）
→ 改动相关车道子集 + 恒跑面（--range 缺省 HEAD~1..HEAD，多提交推送传
A..B）；full 档（kernel//.github//scripts/ 等核心面变更）→ 全量 --mode ci。
打包相关 = electron:build 链内嵌闸自身；功能验证 = 远端 e2e smoke（或装后
真实聊天冒烟）。其余 kind 维持 --mode ci 全量。非零即拦（fail-closed 同源）；
输出留 LOCAL-RUN 痕迹可审计。

用法：
  python scripts/check_ci_gate.py --kind package            # 远端取证（推远端场景）
  python scripts/check_ci_gate.py --kind package --local    # 本地 package-fast 取证
  python scripts/check_ci_gate.py --kind deploy             # 发布/部署管线链首
退出码：0 = 全绿放行；1 = 门禁未绿（有红/在跑/未跑过/取证失败）；
2 = 环境不可判（非 git 仓/远端解析不出 GitHub slug/diff 范围不可解析）。
1 与 2 都阻断，分码便于上层记因。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GITHUB_API = "https://api.github.com"
#: 打包/部署声明的前置 workflow（全绿才算过；新增必经门时在此追加）。
#: deploy/release 缺省面（部署面含 env-gate 全量：deploy-env 也属部署声明前置）。
REQUIRED_WORKFLOWS = ("ci.yml", "env-gate.yml")
#: package 声明的必经面（用户裁定 2026-11-06「打包 CI 不跑全矩阵：改动相关 +
#: 打包相关 + 一个功能验证」）：
#:   - ci.yml      = 改动相关（workflow 内建 changes 车道分档，irrelevant
#:                   lanes 已被 if: 跳过——workflow 绿即改动相关车道绿）；
#:   - env-gate.yml= 打包相关（job 级只取 Pack Windows；deploy-env 属部署面
#:                   不拦打包，由 deploy/release 声明另行执法）；
#:   - e2e.yml     = 功能验证（push 触发 smoke 车道：kernel + 全插件 venv +
#:                   auth 全链 + 一轮 stub-LLM 管道 chat；全量套件收 nightly）。
PACKAGE_REQUIRED = ("ci.yml", "env-gate.yml", "e2e.yml")
#: env-gate.yml 内打包相关 job 名前缀（package kind 做 job 级取证）
PACKAGE_JOB_PREFIX = "Pack Windows"
#: runs API 单页上限；祖先集扫描取最近一页足够（打包前必有近期 push）
RUNS_PAGE_SIZE = 100
HTTP_TIMEOUT_S = 30
#: 非 completed 即「在跑」（Actions 状态集：requested/queued/in_progress/waiting…）
IN_FLIGHT_STATUSES = frozenset({"requested", "pending", "queued", "waiting", "in_progress"})
BAD_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})
SELF_RUN_ENV = "AGENTOS_CI_GATE_IN_RUN"
LOCAL_RUN_ENV = "AGENTOS_CI_GATE_LOCAL"


@dataclass(frozen=True)
class RunInfo:
    """一次 workflow run 的判定面字段（runs API 子集，ISO 时间戳可比序）。"""

    workflow: str
    head_sha: str
    status: str
    conclusion: str | None
    created_at: str
    html_url: str
    id: str = ""  # runs API run id（package kind 的 job 级取证需要）

    @classmethod
    def from_api(cls, workflow: str, d: dict) -> RunInfo:
        return cls(
            workflow=workflow,
            head_sha=str(d.get("head_sha") or ""),
            status=str(d.get("status") or ""),
            conclusion=d.get("conclusion"),
            created_at=str(d.get("created_at") or ""),
            html_url=str(d.get("html_url") or ""),
            id=str(d.get("id") or ""),
        )


@dataclass(frozen=True)
class Verdict:
    """单个 workflow 对目标 SHA 的判定结果。"""

    ok: bool
    reason: str
    baseline: RunInfo | None = None


# ── 本机取证 ─────────────────────────────────────────────────────────────
def parse_repo_slug(url: str) -> str | None:
    """git 远端 URL → GitHub owner/repo；非 github.com 远端返回 None。"""
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/.]+?)(?:\.git)?/?$", url.strip())
    return m.group("slug") if m else None


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=False, cwd=cwd)


def resolve_head(repo: Path | None) -> str:
    """目标 commit：--ref 显式指定，否则当前 HEAD；失败抛 RuntimeError。"""
    proc = _git("rev-parse", "HEAD", cwd=repo)
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve_repo_slug(repo: Path | None) -> str:
    proc = _git("remote", "get-url", "origin", cwd=repo)
    if proc.returncode != 0:
        raise RuntimeError(f"git remote get-url origin 失败：{proc.stderr.strip()}")
    slug = parse_repo_slug(proc.stdout.strip())
    if slug is None:
        raise RuntimeError(f"origin 非 GitHub 远端，无法取证 CI：{proc.stdout.strip()}")
    return slug


def make_is_ancestor(repo: Path | None):
    """返回 is_ancestor(a, b) -> bool（a 为 b 的祖先或与 b 相等）。"""

    def is_ancestor(a: str, b: str) -> bool:
        proc = _git("merge-base", "--is-ancestor", a, b, cwd=repo)
        return proc.returncode == 0

    return is_ancestor


# ── 远端取证 ─────────────────────────────────────────────────────────────
def resolve_token() -> str | None:
    """GITHUB_TOKEN/GH_TOKEN 优先，缺则借 gh CLI 登录态（私有仓匿名 API 404）。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    gh = shutil.which("gh")
    if gh:
        proc = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, check=False)
        token = proc.stdout.strip()
        if proc.returncode == 0 and token:
            return token
    return None


def fetch_runs(repo_slug: str, workflow: str, token: str | None) -> list[RunInfo]:
    url = f"{GITHUB_API}/repos/{repo_slug}/actions/workflows/{workflow}/runs?per_page={RUNS_PAGE_SIZE}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"runs API 404（{repo_slug} 的 {workflow} 不可达：私有仓需 "
                "GITHUB_TOKEN/GH_TOKEN 环境变量或 gh auth login）"
            ) from exc
        raise
    return [RunInfo.from_api(workflow, item) for item in payload.get("workflow_runs", [])]


def fetch_run_jobs(repo_slug: str, run_id: str, token: str | None) -> list[dict]:
    """某次 run 的 jobs 列表（package kind 对 env-gate 做 Pack Windows job 级取证）。"""
    url = f"{GITHUB_API}/repos/{repo_slug}/actions/runs/{run_id}/jobs?per_page={RUNS_PAGE_SIZE}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        payload = json.load(resp)
    return list(payload.get("jobs", []))


def evaluate_pack_job(jobs: list[dict]) -> Verdict:
    """package kind 的 env-gate job 级判定：只认 Pack Windows job（纯函数，可测）。

    deploy-env job 属部署面，不在打包声明前置内（deploy/release kind 走
    REQUIRED_WORKFLOWS 全量执法）。job 缺席 = fail-closed（老 run 无法证明
    打包链实测过 = 不通过）。
    """
    pack = [j for j in jobs if str(j.get("name", "")).startswith(PACKAGE_JOB_PREFIX)]
    if not pack:
        return Verdict(False, f"env-gate run 内无 {PACKAGE_JOB_PREFIX} job（无法证明打包链实测）")
    job = pack[0]
    status = str(job.get("status") or "")
    conclusion = job.get("conclusion")
    if status in IN_FLIGHT_STATUSES:
        return Verdict(False, f"{PACKAGE_JOB_PREFIX} 在跑（等绿再打包）")
    if conclusion != "success":
        return Verdict(False, f"{PACKAGE_JOB_PREFIX} 红：{conclusion} {job.get('html_url', '')}")
    return Verdict(True, f"{PACKAGE_JOB_PREFIX} 绿（打包链实测 + 装后冒烟过）")


# ── 判定（纯函数，可测）─────────────────────────────────────────────────
def evaluate(runs: list[RunInfo], target_sha: str, is_ancestor) -> Verdict:
    """「最近绿基线」判定：见模块 docstring 判定口径。"""
    ancestors = [r for r in runs if r.head_sha and is_ancestor(r.head_sha, target_sha)]
    done = [r for r in ancestors if r.status == "completed"]
    inflight = [r for r in ancestors if r.status in IN_FLIGHT_STATUSES]
    successes = sorted((r for r in done if r.conclusion == "success"), key=lambda r: r.created_at, reverse=True)
    baseline = successes[0] if successes else None
    if baseline is None:
        blocks = [r for r in done if r.conclusion in BAD_CONCLUSIONS]
        if blocks:
            latest = max(blocks, key=lambda r: r.created_at)
            return Verdict(False, f"从未绿过且最近红：{latest.conclusion} {latest.html_url}")
        if inflight:
            return Verdict(False, f"CI 在跑（尚无绿基线）：{inflight[0].html_url}")
        return Verdict(False, "该 workflow 无任何运行记录（未跑过 = 不通过）")
    late_blocks = [r for r in done if r.conclusion in BAD_CONCLUSIONS and r.created_at > baseline.created_at]
    if late_blocks:
        latest = max(late_blocks, key=lambda r: r.created_at)
        return Verdict(
            False,
            f"基线后存在未压过的红：{latest.conclusion} {latest.html_url}",
            baseline,
        )
    late_inflight = [r for r in inflight if r.created_at > baseline.created_at]
    if late_inflight:
        return Verdict(
            False,
            f"CI 在跑（等绿再打包）：{late_inflight[0].html_url}",
            baseline,
        )
    return Verdict(True, f"绿基线 {baseline.head_sha[:12]} {baseline.html_url}", baseline)


# ── 本地 package-fast：改动面分档 → 车道子集（用户裁定 2026-11-06「只跑
# 相关的，恒不全量」）────────────────────────────────────────────────
#: lane → run_gates --filter 映射。与 ci.yml job 接线同源（改 ci.yml 车道
#: 必同步本表；key 集 = ci_changed_areas.compute_lanes 输出键，由
#: test_check_ci_gate 的机械锁钉住）。full 档不再映射全量：按改动文件前缀
#: 二次细分（kernel→kernel 车道、scripts→scripts 车道、workflows→接线面），
#: 「相关」语义恒成立——全量 --mode ci 仅显式 --full-mode 才跑。
LANE_FILTERS: dict[str, str] = {
    "python_full": (
        "sdk-lint,sdk-mypy,config-direct-reads,kernel-db-direct-reads,scripts-lint,"
        "plugins-lint,tool-contract-completeness,tool-schema-contract,agent-tool-ids,"
        "step-declaration-parity,config-static,config-seed-ver,plugin-venv-health,"
        "enforcement-tier,sdk-test,plugins-coverage-pairing,channel-copy-guard,"
        "plugins-coverage,channel-per-file-coverage,plugins-diff-coverage,"
        "plugins-mypy-baseline,plugins-heavy,timing-gate"
    ),
    "related": "plugins-related-tests",
    "endpoints": "frontend-theme-color-hygiene",
    "frontend": (
        "frontend-typecheck,frontend-lint,frontend-coverage,frontend-diff-coverage,"
        "frontend-any-baseline,frontend-theme-color-hygiene,frontend-knip-baseline,"
        "frontend-jscpd-baseline"
    ),
    "electron": "electron-compile,electron-test,tdd-gate,gate-wiring",
    "mobile": "shell-config-sync",
    "deploy": "deploy-env",
}
#: full 档内按改动前缀的细分车道（「相关」语义：kernel 改动跑 kernel 面，
#: 不拖全矩阵）。
FULL_PREFIX_FILTERS: tuple[tuple[str, str], ...] = (
    ("kernel/", "kernel-fmt,kernel-clippy,kernel-test,kernel-metrics-smoke"),
    (
        "scripts/",
        "scripts-lint,plugins-lint,sdk-lint,sdk-mypy,plugin-venv-health",
    ),
    (
        ".github/workflows/",
        "gate-wiring,traceability-gate",
    ),
    ("app-mobile/", "shell-config-sync"),
    ("deploy/", "deploy-env"),
)
#: 恒跑面（ci.yml 无 if 的 job 承载的 filter 串）：traceability 恒跑 job +
#: rust-deny。pre-commit 由本地钩子承载（staged 面），不在此列。
ALWAYS_FILTERS = (
    "rust-deny,traceability-gate,doc-baseline-sync,doc-paths,doc-frontmatter,"
    "working-index-fresh,kernel-port-sync,openapi-drift,file-length-baseline,"
    "render-card-sync,shell-config-sync,frontend-plugin-pages-sync,"
    "shared-root-imports,service-declaration,observability-config,gate-protected"
)
#: 显式全量档（仅 --full-mode 传入；缺省任何路径都不跑全量）
FULL_MODE = "--mode ci"


def compute_package_filters(lanes: dict[str, bool], changed_files: list[str] | None = None) -> str:
    """改动面分档 → 本地打包取证的车道串（纯函数，可测）。

    full=True 时按 changed_files 前缀二次细分（FULL_PREFIX_FILTERS），命中
    哪个域跑哪个域的车道 + 恒跑面——「只跑相关的，恒不全量」。full 且无
    文件清单可细分时保守取 kernel 面（改动含核心面的最常见形态）。
    """
    if lanes.get("full"):
        parts = {ALWAYS_FILTERS}
        if changed_files:
            for prefix, filters in FULL_PREFIX_FILTERS:
                if any(f.startswith(prefix) for f in changed_files):
                    parts.add(filters)
        else:
            parts.add(FULL_PREFIX_FILTERS[0][1])
        return ",".join(sorted(parts))
    parts = [ALWAYS_FILTERS]
    for lane, filters in LANE_FILTERS.items():
        if lanes.get(lane):
            parts.append(filters)
    return ",".join(parts)


# ── 取证缓存（用户裁定 2026-11-06「跑过了就不要重复跑」）────────────────
#: 同一 HEAD 的绿车道集合落盘（.zctmp/ gitignored，丢失=重跑，安全方向）；
#: 后续取证取差集（目标车道 ∖ 已绿），差集空直接放行——多段打包
# （desktop/mobile/linux 同链）各段取证只跑不一样的部分。
ATTEST_DIR_NAME = ".zctmp/ci-attest"


def attest_path(repo: Path, target: str) -> Path:
    return repo / ATTEST_DIR_NAME / f"{target[:12]}.json"


def load_green(repo: Path, target: str) -> set[str]:
    try:
        data = json.loads(attest_path(repo, target).read_text(encoding="utf-8"))
        green = data.get("green")
        if isinstance(green, list) and data.get("sha", "").startswith(target[:12]):
            return {str(g) for g in green}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return set()


def save_green(repo: Path, target: str, green: set[str]) -> None:
    path = attest_path(repo, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sha": target[:12], "green": sorted(green)}, ensure_ascii=False),
        encoding="utf-8",
    )


def plan_delta(target_lanes: set[str], green: set[str]) -> set[str]:
    """待跑车道 = 目标 ∖ 已绿（纯函数，可测）。"""
    return target_lanes - green


# ── 入口 ─────────────────────────────────────────────────────────────────
def run_local_gate(kind: str, repo: Path, target: str, diff_range: str = "") -> int:
    """本地取证模式：CI 跟随执行空间（用户裁定 2026-10-30）。

    本地提交+本地打包 → 在本地跑 CI 取证；推远端才由远端 Actions 执法。
    非零退出即拦（fail-closed）；输出留 LOCAL-RUN 痕迹可审计（伪造跳过 =
    自违裁定）。

    kind=package（用户裁定 2026-11-06「打包 CI 不跑全矩阵」+「只跑相关的，
    恒不全量」）：按改动面分档（ci_changed_areas.compute_lanes）取「改动
    相关」车道子集 + 恒跑面；full 档按改动前缀二次细分（kernel→kernel 面、
    scripts→scripts 面、workflows→接线面），任何路径都不自动跑全量——全量
    仅显式 --full-mode。打包相关 = electron:build 链内嵌闸自身（prereqs/
    cdylibs/sanitize/freshness/clean-tree/frontend build/tsc/layout/place）；
    功能验证 = 远端 e2e smoke 车道（本地打包场景由装后真实聊天冒烟承载）。
    其余 kind（deploy/release）按改动面同款分档（非全量）。
    """
    gates_py = Path(__file__).resolve().parent / "run_gates.py"
    from ci_changed_areas import compute_lanes  # noqa: PLC0415 同目录单源

    rng = diff_range or "HEAD~1..HEAD"
    proc = _git("diff", "--name-only", rng, cwd=repo)
    if proc.returncode != 0:
        print(f"[ci-gate] FAIL 环境不可判：git diff {rng} 失败：{proc.stderr.strip()}")
        return 2
    files = [ln.strip().replace("\\", "/") for ln in proc.stdout.splitlines() if ln.strip()]
    lanes = compute_lanes(files)
    if kind == "package":
        filter_arg = compute_package_filters(lanes, files)
    else:
        # deploy/release：同样「只跑相关的」——改动域车道 + 恒跑面
        filter_arg = compute_package_filters(lanes, files)
    lane_desc = " ".join(f"{k}={'T' if v else 'F'}" for k, v in lanes.items())
    print(f"[ci-gate] LOCAL-RUN(PACKAGE-FAST)：改动相关取证 @ {target[:12]}（{rng}，{len(files)} 文件）")
    print(f"[ci-gate] lanes: {lane_desc}")

    # 增量取证（「跑过了就不要重复跑」）：目标车道 ∖ 同 HEAD 已绿 = 待跑。
    target_set = {x.strip() for x in filter_arg.split(",") if x.strip()}
    green = load_green(repo, target)
    todo = sorted(plan_delta(target_set, green))
    reused = sorted(target_set & green)
    if reused:
        print(f"[ci-gate] 缓存复用（已绿不重跑）: {','.join(reused)}")
    if not todo:
        print(f"[ci-gate] 门禁放行（{kind} @ {target[:12]}，目标车道全部命中缓存绿基线）")
        return 0
    print(f"[ci-gate] 车道: {','.join(todo)}")
    cmd = [sys.executable, str(gates_py), "--filter", ",".join(todo)]
    proc2 = subprocess.run(cmd, cwd=repo, check=False)
    if proc2.returncode != 0:
        print("[ci-gate] 门禁拦截：改动相关车道未绿不许声明打包通过（红车道逐个清再打包）")
        return 1
    save_green(repo, target, green | set(todo))
    print(f"[ci-gate] 门禁放行（{kind} @ {target[:12]}，改动相关车道绿；打包面=链内嵌闸；功能验证=e2e smoke）")
    return 0


def main() -> int:
    # Windows 控制台默认 cp1252，CI label 含 CJK 时 print 直接
    # UnicodeEncodeError（2026-11-06 env-gate pack-windows 实证）——入口
    # 重配置 UTF-8，与 run_gates.py 同刀。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="打包/部署 CI 门禁执法（CI 不过不打包不部署）")
    parser.add_argument(
        "--kind", choices=("package", "deploy", "release"), required=True, help="声明类型（仅用于输出语义，判定面相同）"
    )
    parser.add_argument("--repo", help="GitHub owner/repo（缺省从 origin 解析）")
    parser.add_argument("--ref", help="目标 commit（缺省 HEAD）")
    parser.add_argument(
        "--workflow",
        action="append",
        dest="workflows",
        help="追加必经 workflow（可重复；缺省按 kind：package=ci+env-gate+e2e，其余=ci+env-gate）",
    )
    parser.add_argument("--cwd", help="git 仓根（缺省脚本所在仓）")
    parser.add_argument(
        "--range",
        dest="diff_range",
        default="",
        help="本地 package-fast 的改动面范围（缺省 HEAD~1..HEAD；多提交推送显式传 A..B）",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="本地取证：跑 run_gates --mode ci 替代远端 Actions 取证（本地提交+本地打包场景；"
        f"也可用环境变量 {LOCAL_RUN_ENV}=1）",
    )
    args = parser.parse_args()

    if os.environ.get(SELF_RUN_ENV) == "1":
        print(f"[ci-gate] SELF-RUN：CI 环境内自跑（{SELF_RUN_ENV}=1），链首执法由本 run 自身承载")
        return 0

    repo = Path(args.cwd).resolve() if args.cwd else Path(__file__).resolve().parent.parent
    try:
        target = args.ref if args.ref else resolve_head(repo)
    except RuntimeError as exc:
        print(f"[ci-gate] FAIL 环境不可判：{exc}")
        return 2

    if args.local or os.environ.get(LOCAL_RUN_ENV) == "1":
        return run_local_gate(args.kind, repo, target, args.diff_range)

    try:
        slug = args.repo if args.repo else resolve_repo_slug(repo)
    except RuntimeError as exc:
        print(f"[ci-gate] FAIL 环境不可判：{exc}")
        return 2

    token = resolve_token()
    if args.workflows:
        workflows = tuple(args.workflows)
    else:
        workflows = PACKAGE_REQUIRED if args.kind == "package" else REQUIRED_WORKFLOWS
    is_ancestor = make_is_ancestor(repo)

    all_ok = True
    for workflow in workflows:
        try:
            runs = fetch_runs(slug, workflow, token)
        except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            print(f"[ci-gate] FAIL {workflow}：取证失败（fail-closed）：{exc}")
            all_ok = False
            continue
        verdict = evaluate(runs, target, is_ancestor)
        # package kind 的 env-gate 叠加 job 级取证：workflow 绿基线之上还须
        # 该基线 run 内 Pack Windows job 绿（deploy-env 不拦打包，见
        # PACKAGE_REQUIRED 注）。
        if verdict.ok and args.kind == "package" and workflow == "env-gate.yml" and verdict.baseline is not None:
            try:
                jobs = fetch_run_jobs(slug, verdict.baseline.id, token)
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                print(f"[ci-gate] FAIL env-gate job 取证失败（fail-closed）：{exc}")
                all_ok = False
                continue
            job_verdict = evaluate_pack_job(jobs)
            print(
                f"[ci-gate] {'PASS' if job_verdict.ok else 'FAIL'} env-gate/{PACKAGE_JOB_PREFIX}：{job_verdict.reason}"
            )
            verdict = Verdict(
                verdict.ok and job_verdict.ok,
                f"{verdict.reason}；{job_verdict.reason}",
                verdict.baseline,
            )
        tag = "PASS" if verdict.ok else "FAIL"
        print(f"[ci-gate] {tag} {workflow} @ {target[:12]}：{verdict.reason}")
        all_ok = all_ok and verdict.ok

    if all_ok:
        print(f"[ci-gate] 门禁放行（{args.kind} @ {target[:12]}，CI 全绿）")
        return 0
    print("[ci-gate] 门禁拦截：CI 未绿不许声明打包/部署通过（按 kind 等必经面全绿再打包）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
