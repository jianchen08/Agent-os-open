"""机械闸受保护清单检查器（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 2）。

"门禁护门禁"：门禁配置 / 基线棘轮 / 评估指标 / 文档 schema / 渲染规定源码列入
显式清单（.github/gate-protected-paths.txt），diff 命中即红——执法面被被执法面
修改 = 博弈入口。唯一放行 = gate-change 独立任务：人类签发 ADR + commit message
带 ``[gate-change:`` 标记 + 该提交跑全量档（CI 全量档天然成立）。

变更集解析三级回退（对齐 check_diff_coverage 机制）：
    --changed-files（pre-commit staged）> --range A..B >
    --base / GITHUB_BASE_REF 合并基 > HEAD^..HEAD 兜底

执法面分工：本地 pre-commit 用 --strict（提交阶段 message 未定，无豁免可认）；
CI 默认读 HEAD commit message 认标记放行（放行必留提示，禁静默降级）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTECTED_LIST = ROOT / ".github" / "gate-protected-paths.txt"
GATE_CHANGE_MARKER = "[gate-change:"


def parse_protected_list(text: str) -> list[str]:
    """解析清单文本：剔除注释行与空行，路径保序。

    pattern 以 ``/`` 结尾 = 目录前缀；否则 = 精确文件路径。
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def match_hits(changed: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """返回命中的变更文件（保序去重）。

    匹配语义：目录 pattern（尾 ``/``）按前缀命中；文件 pattern 精确相等。
    """
    hits: list[str] = []
    for f in changed:
        for p in patterns:
            if (p.endswith("/") and f.startswith(p)) or f == p:
                if f not in hits:
                    hits.append(f)
                break
    return hits


def _git_changed(range_: str | None, base: str | None) -> list[str]:
    """git diff 变更集：range_ 优先，其次 base 合并基三点，兜底 HEAD^..HEAD。

    裸目录（无 git 语义）返回空集并提示；其他 git 故障 fail-closed。
    """
    if range_:
        spec: list[str] = [range_]
    elif base:
        merged = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True, check=False)
        if merged.returncode != 0:
            print(
                f"gate-protected: merge-base({base}, HEAD) 失败: {merged.stderr.strip()}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        spec = [f"{merged.stdout.strip()}..HEAD"]
    else:
        spec = ["HEAD^..HEAD"]
    # -z：CJK 文件名不经 core.quotepath 转义（默认 true 会把
    # docs/schemas/文档类型规范.md 输出成 "docs/schemas/\346\226\207..."，
    # 带首引号即无法前缀命中 docs/schemas/ —— 与 check_pre_commit.staged_paths
    # 同形态）。NUL 分隔免一切转义。
    proc = subprocess.run(["git", "diff", "--name-only", "-z", *spec], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        low = err.lower()
        if "not a git repository" in low or "not a git command" in low:
            print("gate-protected: 非 git 目录，无变更集可查（放行）", file=sys.stderr)
            return []
        print(f"gate-protected: git diff 失败: {err}", file=sys.stderr)
        raise SystemExit(1)
    return [path for path in proc.stdout.split("\0") if path]


def resolve_changed_files(
    changed_files: Sequence[str] | None,
    range_: str | None,
    base: str | None,
) -> list[str]:
    """变更集三级回退：显式 --changed-files 优先，否则走 git。"""
    if changed_files:
        return list(changed_files)
    env_base = base or os.environ.get("GITHUB_BASE_REF") or None
    return _git_changed(range_, env_base)


def head_commit_message() -> str:
    """HEAD commit message（豁免标记载体）；git 不可用时返回空串。"""
    proc = subprocess.run(["git", "log", "-1", "--format=%B"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="机械闸受保护清单检查（门禁护门禁）")
    parser.add_argument(
        "--changed-files",
        nargs="*",
        help="显式变更文件列表（pre-commit 传 staged 变更集；缺省走 git diff）",
    )
    parser.add_argument("--range", help="显式 diff 范围 A..B（本地手工）")
    parser.add_argument("--base", help="基线分支（默认取 GITHUB_BASE_REF；CI PR 事件用）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="本地提交模式：忽略 HEAD message 的 gate-change 标记（提交时 message 未定）",
    )
    args = parser.parse_args(argv)

    changed = resolve_changed_files(args.changed_files, args.range, args.base)
    patterns = parse_protected_list(PROTECTED_LIST.read_text(encoding="utf-8"))
    hits = match_hits(changed, patterns)

    if not hits:
        print(f"gate-protected: {len(changed)} 个变更文件未命中受保护清单（通过）")
        return 0

    hit_text = "\n".join(f"  - {h}" for h in hits)
    if not args.strict and GATE_CHANGE_MARKER in head_commit_message():
        # 放行必须可感知：gate-change 提交须全量档佐证 + ADR 留痕（禁静默降级）
        print(
            f"gate-protected: [gate-change] 放行 {len(hits)} 个受保护文件——"
            "本次改动须全量档（--mode all）佐证 + ADR 留痕：\n" + hit_text
        )
        return 0

    print(
        "gate-protected: 以下变更命中机械闸受保护清单（执法面不可被普通任务改动）：\n"
        + hit_text
        + "\n唯一放行路径 = gate-change 独立任务：\n"
        "  1. 人类签发的 ADR（docs/decisions/）记录决策与被否方案；\n"
        "  2. commit message 附 [gate-change: <ADR路径>] 标记（仅 CI 层放行）；\n"
        "  3. 该提交强制跑全量档（python scripts/run_gates.py --mode all）。\n"
        "或将受保护文件从本次变更中拆出单独提交。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
