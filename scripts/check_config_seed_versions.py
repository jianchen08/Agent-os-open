#!/usr/bin/env python3
"""出厂播种版本门禁（ADR 2026-10-24-config-seed-version-arbitration 执法面）。

背景：装机播种（electron seedConfigTree）对受管出厂默认（config/pipelines/*.yaml
顶层 ``version:`` 点分整数）按版本裁决——用户空间副本版本不严格大于包内即覆盖，
治「用户空间陈旧副本遮蔽包内修复」（2026-10-24 评估通过空转事故：10-21 旧
autonomous.yaml 遮蔽 10-23 包内路由修复，装机内核 Config root 指用户空间）。

本闸静态前置两查（秒级，fail-closed）：

1. **在场声明**（[`missing_declarations`]）：受管区每个 yaml 必须有顶层点分整数
   ``version:``——缺声明 = 播种覆盖语义对该文件失效（copy-if-missing），陈旧
   副本可无限遮蔽后续修复；新文件进门即入受管面。
2. **变更必抬版本**（[`bump_violations`]）：diff 范围内受管文件内容有变而
   version 字符串未变 → 红。裁决规则是「不大于即覆盖」：版本持平的包内更新
   会静默覆盖用户侧等值版本上的改动，版本必须与内容同步单调。

范围解析复用 check_diff_coverage.resolve_range 现成机制（--range >
--base/GITHUB_BASE_REF > HEAD^..HEAD push 口径）。版本行识别与 electron
seedConfigTree.topLevelVersion 同口径（列首 ``version:`` + 点分整数 + 行尾
注释容忍），三面（播种/保存/Rust PUT 校验取值面）一致。

逃生口：range 内 commit message 含 ``[skip-seed-ver]``（[skip-diff-cov] 风格，
滥用必追责）。

用法：python scripts/check_config_seed_versions.py [--range A..B | --base REF]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
PIPELINES_DIR = ROOT / "config" / "pipelines"
SKIP_MARKER = "[skip-seed-ver]"

#: 与 electron seedConfigTree.topLevelVersion 同口径（列首 + 点分整数 +
#: 行尾注释容忍）。
VERSION_LINE_RE = re.compile(r"^version:\s*(\d+(?:\.\d+)*)\s*(?:#.*)?$")

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_diff_coverage import range_has_skip_marker, resolve_range  # noqa: E402


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout


def top_level_version(text: str) -> str | None:
    """顶层 version 提取（与 electron 播种侧逐字同口径；缺席/非点分 = None）。"""
    for line in text.splitlines():
        m = VERSION_LINE_RE.match(line)
        if m:
            return m.group(1)
    return None


def missing_declarations(managed: list[Path]) -> tuple[list[str], dict[str, str]]:
    """在场声明检查：受管区文件缺顶层点分整数 version 即违规。

    Returns:
        (违规清单, rel 路径 → version 索引；无声明文件不入索引)
    """
    errors: list[str] = []
    versions: dict[str, str] = {}
    for p in managed:
        v = top_level_version(p.read_text(encoding="utf-8"))
        if v is None:
            errors.append(
                f"受管出厂默认缺顶层 version 声明（点分整数）: {p.relative_to(ROOT)}"
                "——无声明则播种覆盖语义失效，用户空间陈旧副本可无限遮蔽包内修复"
                "（ADR 2026-10-24-config-seed-version-arbitration）"
            )
        else:
            versions[p.relative_to(ROOT).as_posix()] = v
    return errors, versions


def bump_violations(
    changed: list[str],
    head_versions: dict[str, str],
    base_version_of: Callable[[str], str | None],
    exists: Callable[[str], bool],
) -> list[str]:
    """变更必抬版本检查：range 内改动的受管文件 version 与基线相同即违规。

    head_versions 缺席的改动文件（已删除，或无声明——后者由在场声明查报）
    不在本查范围。
    """
    errors: list[str] = []
    for rel in changed:
        head_v = head_versions.get(rel)
        if head_v is None:
            continue
        if not exists(rel):
            continue
        if head_v == base_version_of(rel):
            errors.append(
                f"受管出厂默认内容变更未抬版本: {rel}（version 仍为 {head_v}）"
                "——持平版本覆盖会静默吃掉用户侧等值版本上的改动，改内容必须抬版本"
            )
    return errors


def _version_at(ref: str, rel: str) -> str | None:
    """指定提交时点的文件 version（文件不存在 = None，调用方按新增文件处理）。"""
    try:
        text = _git(["show", f"{ref}:{rel}"])
    except RuntimeError:
        return None
    return top_level_version(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="出厂播种版本门禁（受管配置 version 声明 + 变更必抬版本）")
    parser.add_argument("--range", dest="range_", metavar="A..B", help="显式 diff 范围（本地手工）")
    parser.add_argument("--base", metavar="REF", help="基线分支（默认 GITHUB_BASE_REF > HEAD^）")
    args = parser.parse_args()

    managed = sorted(p for p in PIPELINES_DIR.glob("*.yaml") if p.is_file())
    if not managed:
        print("seed-ver: config/pipelines/ 下无 yaml（异常，fail-closed）。", file=sys.stderr)
        return 1
    errors, disk_versions = missing_declarations(managed)

    range_checked = False
    rng = resolve_range(argparse.Namespace(range=args.range_, base=args.base), tag="[seed-ver]")
    if rng is None:
        print("seed-ver: 范围为空（根提交），跳过变更检查（在场声明已查）。")
    elif range_has_skip_marker(rng[0], rng[1], SKIP_MARKER):
        print(f"seed-ver: ⚠️ range 内含 {SKIP_MARKER} 逃生口，变更检查放行（在场声明仍执法）。")
    else:
        a, _b, _desc = rng
        changed = [
            line.strip()
            for line in _git(["diff", "--name-only", a, _b, "--", "config/pipelines"]).splitlines()
            if line.strip().endswith((".yaml", ".yml"))
        ]
        errors.extend(
            bump_violations(
                changed,
                disk_versions,
                lambda rel: _version_at(a, rel),
                lambda rel: (ROOT / rel).exists(),
            )
        )
        range_checked = True

    if errors:
        print(f"seed-ver: {len(errors)} 项违规：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    tail = "，range 内变更均已抬版本" if range_checked else ""
    print(f"seed-ver: 通过（{len(managed)} 个受管出厂默认全部声明 version{tail}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
