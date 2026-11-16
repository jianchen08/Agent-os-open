"""项目初始化脚手架（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 3）。

裸项目一键出 CI 骨架：run_gates 两档配置（fast/all + --changed-files 变更模块
过滤 + 路径→守护前缀映射）、受保护清单 + 检查器、frontmatter 模板 + 文档规范
占位、域规范占位（按项目类型选编码/研究/写作域包）、hooks 安装。产物入库即受
受保护清单护住（gate-change 独立任务放行）。

幂等：已存在的文件跳过不覆盖（防人工修改被冲掉），报告 created/skipped。
git 目标自动设置 core.hooksPath=scripts/hooks（非 git 目录跳过并提示）。

用法：
    python skills/skill-dev-toolkit/scripts/scaffold_project.py <target> \\
        --name "项目名" --domain coding|research|writing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DOMAIN_TITLES = {"coding": "编码", "research": "研究", "writing": "写作"}

RUN_GATES_TEMPLATE = '''#!/usr/bin/env python3
"""统一机械门禁入口（scaffold 生成骨架）：fast 本地廉价检查 / all 穷尽集。

fast 档支持 --changed-files 变更模块过滤：paths 为空的门禁恒跑（cross-cutting），
非空的仅当变更文件命中守护路径前缀才跑（秒级无感；过滤只缩不扩防漏检）。
项目接手后在本文件 GATES 中登记各模块门禁（单一事实源，CI 与本地同入口）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Gate:
    id: str
    label: str
    command: tuple
    paths: tuple = ()


GATES = [
    Gate(
        id="scripts-syntax",
        label="scripts/ 语法编译闸",
        command=(sys.executable, "-m", "compileall", "-q", "scripts"),
        paths=("scripts/",),
    ),
]


def filter_by_changed(gates, changed):
    if changed is None:
        return list(gates)
    kept = []
    for g in gates:
        if not g.paths or any(f.startswith(p) for f in changed for p in g.paths):
            kept.append(g)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="统一机械门禁入口")
    parser.add_argument("--mode", choices=["fast", "all"], default="fast")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--changed-files", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.list:
        for g in GATES:
            print(f"{g.id:<28} paths={'/'.join(g.paths) or '-'} :: {g.label}")
        return 0

    changed = args.changed_files if args.mode == "fast" else None
    selected = filter_by_changed(GATES, changed)
    if not selected:
        print("run_gates: 变更不命中任何模块门禁，跳过")
        return 0
    if args.dry_run:
        for g in selected:
            print(g.id)
        return 0

    failed = []
    for g in selected:
        print(f"[gate] {g.id}: {g.label}", flush=True)
        proc = subprocess.run(list(g.command), cwd=str(ROOT))
        if proc.returncode != 0:
            failed.append(g.id)
    if failed:
        print(f"run_gates: 失败门禁 {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

PRE_COMMIT_TEMPLATE = '''#!/usr/bin/env bash
# 提交即闸（scaffold 生成，方案 §3.6）：fast 档拒绝式。
# 编排：受保护清单（--strict，提交阶段 message 未定无豁免）→ fast 档仅变更模块。
# 安装：python scripts/install_hooks.py（core.hooksPath → scripts/hooks，幂等）。
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 1

PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
  echo "[hooks] 找不到 python 解释器，拒绝提交（fail-closed）" >&2
  exit 1
fi

mapfile -d '' CHANGED < <(git diff --cached --name-only -z --diff-filter=ACMR)
if (( ${#CHANGED[@]} == 0 )); then
  exit 0
fi

"$PY" scripts/check_gate_protected.py --strict --changed-files "${CHANGED[@]}" || exit 1
"$PY" scripts/run_gates.py --mode fast --changed-files "${CHANGED[@]}" || exit 1
exit 0
'''

COMMIT_MSG_TEMPLATE = '''#!/usr/bin/env bash
# commit-msg 委托（scaffold 生成）：项目接入 pre-commit 框架后由此转发检查。
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 1
if [[ -f .pre-commit-config.yaml ]] && command -v pre-commit >/dev/null 2>&1; then
  pre-commit run --hook-stage commit-msg "$@" || exit 1
fi
exit 0
'''

INSTALL_HOOKS_TEMPLATE = '''#!/usr/bin/env python3
"""git hooks 幂等安装器（scaffold 生成）：core.hooksPath → scripts/hooks。

受管工作区还原后钩子失联，一条命令重装（幂等，重复执行无害）。
"""

from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_REL = "scripts/hooks"


def install(repo: Path) -> int:
    required = repo / HOOKS_REL / "pre-commit"
    if not required.is_file():
        print(f"install_hooks: 钩子文件缺失: {required}", file=sys.stderr)
        return 1
    for hook in (repo / HOOKS_REL).glob("*"):
        if hook.is_file():
            try:
                hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", HOOKS_REL],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"install_hooks: git config 失败: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    verify = subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath"], capture_output=True, text=True
    )
    print(f"install_hooks: core.hooksPath = {verify.stdout.strip()}（幂等安装完成）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="git hooks 幂等安装")
    parser.add_argument("--repo", default=str(ROOT), help="目标仓库根（默认本仓）")
    args = parser.parse_args()
    return install(Path(args.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
'''

CHECK_PROTECTED_TEMPLATE = '''#!/usr/bin/env python3
"""机械闸受保护清单检查器（scaffold 生成精简版，方案 §3.6 件 2）。

执法面 diff 命中清单即红；唯一放行 = gate-change 独立任务
（ADR + commit message [gate-change: 标记 + 全量档佐证）。
本地 pre-commit 用 --strict（提交阶段 message 未定，无豁免可认）。
非 git 目录（无变更集可查）放行并提示——门禁由 hooks/CI 驱动后生效。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTECTED_LIST = ROOT / ".github" / "gate-protected-paths.txt"
MARKER = "[gate-change:"


def parse_protected_list(text: str) -> list:
    return [
        s.strip()
        for s in text.splitlines()
        if s.strip() and not s.strip().startswith("#")
    ]


def match_hits(changed: list, patterns: list) -> list:
    hits = []
    for f in changed:
        for p in patterns:
            if (p.endswith("/") and f.startswith(p)) or f == p:
                if f not in hits:
                    hits.append(f)
                break
    return hits


def git_changed(changed_files, range_, base):
    if changed_files:
        return list(changed_files)
    if range_:
        spec = [range_]
    else:
        b = base or os.environ.get("GITHUB_BASE_REF")
        if b:
            mb = subprocess.run(
                ["git", "merge-base", b, "HEAD"], capture_output=True, text=True
            )
            if mb.returncode != 0:
                print(
                    f"gate-protected: merge-base({b}) 失败: {mb.stderr.strip()}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            spec = [mb.stdout.strip() + "..HEAD"]
        else:
            spec = ["HEAD^..HEAD"]
    proc = subprocess.run(
        ["git", "diff", "--name-only", *spec], capture_output=True, text=True
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        # git 的报错文案是 "Not a git repository"（大写 N）——小写匹配漏接，
        # 非 git 目录从「放行并提示」变 exit 1（Windows git 实测）。
        if "not a git repository" in err.lower():
            print("gate-protected: 非 git 目录，无变更集可查（放行）", file=sys.stderr)
            return []
        print(f"gate-protected: git diff 失败: {err}", file=sys.stderr)
        raise SystemExit(1)
    return [line for line in proc.stdout.splitlines() if line]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="机械闸受保护清单检查")
    parser.add_argument("--changed-files", nargs="*")
    parser.add_argument("--range")
    parser.add_argument("--base")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    changed = git_changed(args.changed_files, args.range, args.base)
    patterns = parse_protected_list(PROTECTED_LIST.read_text(encoding="utf-8"))
    hits = match_hits(changed, patterns)
    if not hits:
        print(f"gate-protected: {len(changed)} 个变更文件未命中受保护清单（通过）")
        return 0

    hit_text = "\\n".join(f"  - {h}" for h in hits)
    if not args.strict:
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"], capture_output=True, text=True
        )
        if msg.returncode == 0 and MARKER in msg.stdout:
            print(
                f"gate-protected: [gate-change] 放行——本次改动须全量档佐证 + ADR 留痕：\\n{hit_text}"
            )
            return 0
    print(
        f"gate-protected: 命中受保护清单（执法面不可被普通任务改动）：\\n{hit_text}\\n"
        "唯一放行 = gate-change 独立任务：ADR + commit message [gate-change: 标记 + 全量档。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

CHECK_TIER_TEMPLATE = '''#!/usr/bin/env python3
"""规范执法档位检查器（scaffold 生成精简版，方案 §3.6 件 4）。

每条规范立项必须标三档执法位之一：mechanical（机械闸）/ metric（评估指标）/
review（审查清单）——无档位的规范不得入库。项目接手后可升级为主仓完整版
（防悬空 gate/指标引用校验）。注册表采用逐条 path/tier 的受限格式。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALID_TIERS = {"mechanical", "metric", "review", "mixed"}


def parse_registry(text: str) -> list:
    """极简解析：仅支持 '- path: X' 开条 + 'tier: Y' 归属的固定形态。"""
    rules: list = []
    cur = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- path:"):
            cur = {"path": s.split(":", 1)[1].strip()}
            rules.append(cur)
        elif s.startswith("tier:") and cur is not None:
            cur["tier"] = s.split(":", 1)[1].strip()
    return rules


def check(rules: list, rules_dir: Path, baseline: set) -> list:
    errors: list = []
    registered: set = set()
    for r in rules:
        path = str(r.get("path", ""))
        tier = str(r.get("tier", ""))
        if tier not in VALID_TIERS:
            errors.append(f"非法执法档位 tier={tier!r}: {path}")
        if path in registered:
            errors.append(f"注册表重复登记: {path}")
        registered.add(path)
    actual = {p.relative_to(rules_dir).as_posix() for p in rules_dir.rglob("*.md")}
    for f in sorted(actual - registered - baseline):
        errors.append(f"新规则文件未登记执法档位（注册表或基线均无）: {f}")
    for f in sorted(registered & baseline):
        errors.append(f"基线棘轮违规：已登记路径须从基线移除（只减不增）: {f}")
    for f in sorted(baseline - actual - registered):
        errors.append(f"基线失效条目（规则文件已不存在）: {f}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="规范执法档位检查（无档位不入库）")
    parser.add_argument(
        "--rules-dir", default=str(ROOT / "config" / "rules"), help="规则文件目录"
    )
    parser.add_argument(
        "--registry",
        default=str(ROOT / "config" / "rules" / "enforcement_tiers.yaml"),
    )
    parser.add_argument(
        "--baseline",
        default=str(ROOT / ".github" / "enforcement-tier-baseline.txt"),
    )
    parser.add_argument(
        "--init", action="store_true", help="打印未登记文件清单（生成基线用）"
    )
    args = parser.parse_args(argv)

    registry_path = Path(args.registry)
    rules = (
        parse_registry(registry_path.read_text(encoding="utf-8"))
        if registry_path.exists()
        else []
    )
    registered = {str(r.get("path", "")) for r in rules}
    baseline_path = Path(args.baseline)
    baseline_text = (
        baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else ""
    )
    baseline = {
        s.strip()
        for s in baseline_text.splitlines()
        if s.strip() and not s.strip().startswith("#")
    }

    if args.init:
        rules_dir = Path(args.rules_dir)
        actual = {p.relative_to(rules_dir).as_posix() for p in rules_dir.rglob("*.md")}
        for f in sorted(actual - registered):
            print(f)
        return 0

    errors = check(rules, Path(args.rules_dir), baseline)
    if errors:
        print(
            f"check_rule_enforcement_tier: {len(errors)} 项违规：",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(
        f"check_rule_enforcement_tier: {len(rules)} 条已登记 + "
        f"{len(baseline)} 条基线豁免（棘轮），档位全部合法（通过）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

CI_YML_TEMPLATE = """# CI（scaffold 生成骨架；项目接手后按需扩充 job）
name: ci
on: [push, pull_request]
jobs:
  gates:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: 全量门禁（穷尽集）
        run: python scripts/run_gates.py --mode all
"""

PROTECTED_LIST_TEMPLATE = """# 机械闸受保护清单（scaffold 生成种子；执法器 scripts/check_gate_protected.py）
# diff 命中即红；唯一放行 = gate-change 独立任务（ADR + [gate-change: 标记 + 全量档）。
# 语义：行尾 / = 目录前缀整护；否则精确文件。本清单自身在列（自举保护）。
scripts/run_gates.py
scripts/hooks/
scripts/install_hooks.py
scripts/check_gate_protected.py
scripts/check_rule_enforcement_tier.py
.github/
config/rules/enforcement_tiers.yaml
docs/schemas/
"""

TIER_REGISTRY_TEMPLATE = """# 规范执法档位注册表（scaffold 生成种子；执法器 scripts/check_rule_enforcement_tier.py）
# 每条规范必须标三档执法位之一：mechanical（须 gates）/ metric（须 metrics）/ review。
# path 相对 config/rules/。存量未登记文件走 .github/enforcement-tier-baseline.txt 棘轮。
rules:
  - path: per_domain/__DOMAIN___domain_rules.md
    tier: review
    note: 域规范占位——项目接手后逐条标档；mechanical 条目须给出真实执法命令。
"""

FRONTMATTER_TEMPLATE = """# 文档 frontmatter 模板

新建文档在首行 frontmatter 声明三字段（机械提取与项目图的元数据源，LLM 零参与）：

    ---
    doc_id: <唯一 id，日期前缀 + 短横线 slug，如 20261028-my-slug>
    type: <adr|design|report|spec|...>
    status: <draft|current|archived>
    ---

- `doc_id`：全仓唯一；
- `type`：文档类型（决定归位与渲染路由）；
- `status`：生命周期状态（归档件不再作为现行真值被引用）。
"""

DOMAIN_PLACEHOLDER_TEMPLATE = """# __TITLE__域规范（scaffold 占位）

> 制度：每条规范立项时必须标注三档执法位之一——mechanical（机械闸）/
> metric（评估指标）/ review（审查清单），并在 `config/rules/enforcement_tiers.yaml`
> 登记档位与执法载体。无执法档位的规范不得入库。

## 规范条目

（项目接手后按域填充；每条格式示例：

- 条目内容（tier: mechanical，执法命令：`python scripts/run_gates.py --filter <gate-id>`）
- 条目内容（tier: review，执法面：代码审查 AI 法定检查项）
）
"""


def build_plan(target: Path, name: str, domain: str) -> dict:
    """组装产物清单：相对路径 → 文件内容（__DOMAIN__/__TITLE__ 占位替换）。"""
    del name  # 保留参数位：项目名后续用于生成的 README/文档标题（当前产物无需）
    title = DOMAIN_TITLES[domain]
    plan = {
        "scripts/run_gates.py": RUN_GATES_TEMPLATE,
        "scripts/hooks/pre-commit": PRE_COMMIT_TEMPLATE,
        "scripts/hooks/commit-msg": COMMIT_MSG_TEMPLATE,
        "scripts/install_hooks.py": INSTALL_HOOKS_TEMPLATE,
        "scripts/check_gate_protected.py": CHECK_PROTECTED_TEMPLATE,
        "scripts/check_rule_enforcement_tier.py": CHECK_TIER_TEMPLATE,
        ".github/gate-protected-paths.txt": PROTECTED_LIST_TEMPLATE,
        ".github/enforcement-tier-baseline.txt": "",
        ".github/workflows/ci.yml": CI_YML_TEMPLATE,
        "docs/schemas/文档frontmatter模板.md": FRONTMATTER_TEMPLATE,
        "config/rules/enforcement_tiers.yaml": TIER_REGISTRY_TEMPLATE.replace(
            "__DOMAIN__", domain
        ),
        f"config/rules/per_domain/{domain}_domain_rules.md": (
            DOMAIN_PLACEHOLDER_TEMPLATE.replace("__TITLE__", title)
        ),
    }
    return plan


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="裸项目 CI 骨架一键生成（提交即闸 + 受保护清单 + 执法档位制度）"
    )
    parser.add_argument("target", help="目标项目根目录（须已存在）")
    parser.add_argument("--name", required=True, help="项目名（写入占位文档标题）")
    parser.add_argument(
        "--domain",
        choices=sorted(DOMAIN_TITLES),
        default="coding",
        help="域包选择：coding 编码 / research 研究 / writing 写作",
    )
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"scaffold_project: 目标目录不存在: {target}", file=sys.stderr)
        return 1

    plan = build_plan(target, args.name, args.domain)
    created: list[str] = []
    skipped: list[str] = []
    for rel, content in plan.items():
        path = target / rel
        if path.exists():
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        created.append(rel)

    # git 目标自动装钩（非 git 目录跳过并提示——hooks 待 git init 后重跑）
    if (target / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(target), "config", "core.hooksPath", "scripts/hooks"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(
                f"scaffold_project: git config core.hooksPath 失败: {proc.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        print("scaffold_project: core.hooksPath = scripts/hooks（钩子已生效）")
    else:
        print(
            "scaffold_project: 目标非 git 仓库，跳过钩子安装（git init 后重跑本命令）"
        )

    print(
        f"scaffold_project: created {len(created)}, skipped {len(skipped)}（幂等："
        "已存在文件不覆盖）"
    )
    for rel in created:
        print(f"  + {rel}")
    for rel in skipped:
        print(f"  = skipped {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
