#!/usr/bin/env python3
"""出厂播种版本门禁（ADR 2026-10-24-config-seed-version-arbitration +
ADR 2026-11-09-seed-version-content-assets 执法面）。

背景：装机播种（electron seedConfigTree）对受管出厂资产按版本裁决——用户空间
副本版本不严格大于包内即覆盖，治「用户空间陈旧副本遮蔽包内修复」（2026-10-24
评估通过空转事故：10-21 旧 autonomous.yaml 遮蔽 10-23 包内路由修复；2026-11-09
规则 md 同类事故：10-19 规则修复被用户空间旧副本永久遮蔽）。受管面自声明：
yaml 顶层 ``version:`` / md 首行 ``<!-- seed-version: N -->``，点分整数。

本闸静态前置两查（秒级，fail-closed）：

1. **在场声明**（[`missing_declarations`]）：受管区每个资产必须有版本声明——
   缺声明 = 播种覆盖语义对该文件失效（copy-if-missing），陈旧副本可无限遮蔽
   后续修复；新文件进门即入受管面。
2. **变更必抬版本**（[`bump_violations`]）：diff 范围内受管文件内容有变而
   version 字符串未变 → 红。裁决规则是「不大于即覆盖」：版本持平的包内更新
   会静默覆盖用户侧等值版本上的改动，版本必须与内容同步单调。

受管区（必须声明）=
``config/{pipelines,agents,rules,tools}``、``config/plugins/{security_check,evaluation}``
下的 yaml/yml/md + 点名的出厂策略文件（``config/plugins/isolation/isolation_policy.yaml``）。
这些是出厂资产（安全策略 / 隔离策略 / 评估指标 / 工具声明），陈旧副本会直接
改变运行时行为或安全边界，必须随包播种覆盖。用户数据真值（kernel/storage、
models 密钥、plugins 下 llm/settings 等用户配置、users 名单等）不在受管区，
维持 copy-if-missing。变更抬版本检查覆盖 ``config/`` 下**所有**自声明版本的
资产（含 templates/kernel 等），未声明文件不在此查。

模式种子包（``plugins/shared/mode_*/``）走另一条既有机制：内核启动
``reconcile_mode_seeds`` 按包 ``plugin.json`` version 对账，**版本不抬则用户
副本永远收不到新种子**（ADR 2026-11-07-plugin-tiered-single-copy-model；实证
2026-11-07 bash 更名改了 mode_* agent yaml 未抬版本，装机用户副本继续持有
bash_execute 直至工具面漂移）。本闸对 diff 范围内模式包内容变更（排除
tests/caches/pyc）同样执行「变更必抬版本」。

范围解析复用 check_diff_coverage.resolve_range 现成机制（--range >
--base/GITHUB_BASE_REF > HEAD^..HEAD push 口径）。版本识别与 electron
seedConfigTree.assetVersion 同口径（yaml 列首 ``version:`` + 点分整数 + 行尾
注释容忍；md 首个非空行 HTML 注释标记）。

逃生口：range 内 commit message 含 ``[skip-seed-ver]``（[skip-diff-cov] 风格，
滥用必追责）。

用法：python scripts/check_config_seed_versions.py [--range A..B | --base REF]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_ROOT = ROOT / "config"
SKIP_MARKER = "[skip-seed-ver]"

#: 必须声明版本的出厂资产目录（受管面自声明；目录只决定"必须声明"面）。
MANAGED_ROOTS = (
    CONFIG_ROOT / "pipelines",
    CONFIG_ROOT / "agents",
    CONFIG_ROOT / "rules",
    CONFIG_ROOT / "tools",
    CONFIG_ROOT / "plugins" / "security_check",
    CONFIG_ROOT / "plugins" / "evaluation",
)
MANAGED_SUFFIXES = (".yaml", ".yml", ".md")
#: 受管面单文件（同目录含用户数据配置，不能整目录纳入；如 isolation_config.yaml
#: 是硬件/工作区用户配置，isolation_policy.yaml 才是出厂安全策略）。
MANAGED_FILES = (
    CONFIG_ROOT / "plugins" / "isolation" / "isolation_policy.yaml",
)

#: 模式种子包前缀；内容变更必须抬 plugin.json version。
MODE_PACKAGE_PREFIX = "mode_"
#: 不算"种子内容"的路径段/文件名（测试与缓存不随种子语义生效）。
MODE_SEED_EXCLUDED_DIRS = frozenset({
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "tests",
    "node_modules", "target",
})
MODE_SEED_EXCLUDED_FILE_SUFFIXES = (".pyc",)

#: 与 electron seedConfigTree.topLevelVersion 同口径（列首 + 点分整数 +
#: 行尾注释容忍）。
VERSION_LINE_RE = re.compile(r"^version:\s*(\d+(?:\.\d+)*)\s*(?:#.*)?$")
#: 与 electron seedConfigTree.markdownSeedVersion 同口径（首个非空行）。
MD_VERSION_RE = re.compile(r"^<!--\s*seed-version:\s*(\d+(?:\.\d+)*)\s*-->$")

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


def markdown_version(text: str) -> str | None:
    """md 首个非空行的 seed-version 标记提取（缺席/非点分 = None）。"""
    text = text.replace("\ufeff", "", 1)
    for line in text.splitlines():
        if line.strip():
            m = MD_VERSION_RE.match(line.strip())
            return m.group(1) if m else None
    return None


def asset_version(path: Path, text: str) -> str | None:
    """资产版本提取：yaml 顶层 ``version:`` / md 首行 seed-version；其他 None。"""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return top_level_version(text)
    if suffix == ".md":
        return markdown_version(text)
    return None


def managed_files() -> list[Path]:
    """受管区全部资产（yaml/yml/md；README 文档除外；排序稳定）。"""
    files: list[Path] = []
    seen: set[Path] = set()
    for root in MANAGED_ROOTS:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MANAGED_SUFFIXES:
                continue
            if path.name.lower().startswith("readme"):
                continue
            if path not in seen:
                seen.add(path)
                files.append(path)
    for path in MANAGED_FILES:
        if (
            path.is_file()
            and path.suffix.lower() in MANAGED_SUFFIXES
            and path not in seen
        ):
            seen.add(path)
            files.append(path)
    return files


def declared_versions() -> dict[str, str]:
    """config 树下所有自声明版本的资产（含非受管目录，供变更抬版本检查）。"""
    versions: dict[str, str] = {}
    for path in sorted(CONFIG_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MANAGED_SUFFIXES:
            continue
        v = asset_version(path, path.read_text(encoding="utf-8"))
        if v is not None:
            versions[path.relative_to(ROOT).as_posix()] = v
    return versions


def missing_declarations(managed: list[Path]) -> tuple[list[str], dict[str, str]]:
    """在场声明检查：受管区文件缺版本声明即违规（yaml 顶层 / md 首行标记）。

    Returns:
        (违规清单, rel 路径 → version 索引；无声明文件不入索引)
    """
    errors: list[str] = []
    versions: dict[str, str] = {}
    for p in managed:
        rel = p.relative_to(ROOT)
        v = asset_version(Path(rel), p.read_text(encoding="utf-8"))
        if v is None:
            errors.append(
                f"受管出厂资产缺版本声明: {rel}"
                "——yaml 需顶层 version:（点分整数），md 需首行"
                " `<!-- seed-version: N -->`；无声明则播种覆盖语义失效，"
                "用户空间陈旧副本可无限遮蔽包内修复"
                "（ADR 2026-11-09-seed-version-content-assets）"
            )
        else:
            versions[rel.as_posix()] = v
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
    return asset_version(Path(rel), text)


# ── 模式种子包：内容变更必抬 plugin.json version ─────────────────────


def _mode_package_of(rel: str) -> str | None:
    """``plugins/shared/mode_X/...`` → 包相对路径；非模式种子路径 → None。"""
    parts = Path(rel).parts
    if len(parts) < 4:
        return None
    if parts[0] != "plugins" or parts[1] != "shared":
        return None
    if not parts[2].startswith(MODE_PACKAGE_PREFIX):
        return None
    return f"plugins/shared/{parts[2]}"


def _is_mode_seed_content(rel: str) -> bool:
    """该改动是否属于"种子语义"内容（排除测试与缓存）。"""
    parts = Path(rel).parts
    if parts[-1].endswith(MODE_SEED_EXCLUDED_FILE_SUFFIXES):
        return False
    for part in parts[:-1]:
        if part in MODE_SEED_EXCLUDED_DIRS:
            return False
        if part == ".venv" or part.startswith(".venv-"):
            return False
    return True


def mode_seed_changed_packages(changed: list[str]) -> dict[str, list[str]]:
    """diff 内受影响的模式包 → 其种子语义变更文件清单（排序稳定）。"""
    packages: dict[str, list[str]] = {}
    for rel in changed:
        pkg = _mode_package_of(rel)
        if pkg is None or not _is_mode_seed_content(rel):
            continue
        packages.setdefault(pkg, []).append(rel)
    return {pkg: sorted(files) for pkg, files in sorted(packages.items())}


def mode_seed_bump_violations(
    changed: list[str],
    head_version_of: Callable[[str], str | None],
    base_version_of: Callable[[str], str | None],
) -> list[str]:
    """模式种子内容变更必须抬包版本；版本持平/缺失即违规。

    head_version_of(base_version_of) 入参为包相对路径（``plugins/shared/mode_X``）。
    """
    errors: list[str] = []
    for pkg, files in mode_seed_changed_packages(changed).items():
        head_v = head_version_of(pkg)
        if head_v is None:
            errors.append(
                f"模式种子包缺 version 声明: {pkg}/plugin.json"
                "——内核启动对账按版本裁决，无版本则用户副本永远收不到新种子"
            )
            continue
        if head_v == base_version_of(pkg):
            shown = "、".join(files[:3]) + ("…" if len(files) > 3 else "")
            errors.append(
                f"模式种子内容变更未抬版本: {pkg}（version 仍为 {head_v}；"
                f"改动 {len(files)} 件：{shown}）——不抬版本则装机用户副本"
                "永远收不到新种子（ADR 2026-11-07-plugin-tiered-single-copy-model）"
            )
    return errors


def _mode_seed_gate(base_ref: str, head_ref: str = "HEAD") -> list[str]:
    """模式种子包变更检查：diff 范围 + 包 version 对比。"""
    changed = [
        line.strip()
        for line in _git(["diff", "--name-only", base_ref, head_ref, "--", "plugins/shared"]).splitlines()
        if line.strip()
    ]
    return mode_seed_bump_violations(
        changed,
        lambda pkg: _manifest_version_at(head_ref, pkg),
        lambda pkg: _manifest_version_at(base_ref, pkg),
    )


def _manifest_version_at(ref: str, package_rel: str) -> str | None:
    """模式包 plugin.json 在指定提交的 version（JSON 形态；缺失/不可解析 = None）。"""
    rel = f"{package_rel}/plugin.json"
    try:
        text = _git(["show", f"{ref}:{rel}"])
    except RuntimeError:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    if isinstance(version, int):
        return str(version)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="出厂播种版本门禁（受管资产 version 声明 + 变更必抬版本）")
    parser.add_argument("--range", dest="range_", metavar="A..B", help="显式 diff 范围（本地手工）")
    parser.add_argument("--base", metavar="REF", help="基线分支（默认 GITHUB_BASE_REF > HEAD^）")
    args = parser.parse_args()

    managed = managed_files()
    if not managed:
        print("seed-ver: 受管区无任何资产（异常，fail-closed）。", file=sys.stderr)
        return 1
    errors, _curated_versions = missing_declarations(managed)
    # 变更抬版本面：config 下所有自声明资产（含 templates/kernel 等），
    # 未声明文件不进索引（copy-if-missing，不在此查）。
    disk_versions = declared_versions()

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
            for line in _git(["diff", "--name-only", a, _b, "--", "config"]).splitlines()
            if line.strip().endswith(MANAGED_SUFFIXES)
        ]
        errors.extend(
            bump_violations(
                changed,
                disk_versions,
                lambda rel: _version_at(a, rel),
                lambda rel: (ROOT / rel).exists(),
            )
        )
        # 模式种子包（plugins/shared/mode_*）：内容变更必抬 plugin.json version，
        # 否则内核启动 reconcile_mode_seeds 判 Current/no-op，用户副本永不升级。
        errors.extend(_mode_seed_gate(a, _b))
        range_checked = True

    if errors:
        print(f"seed-ver: {len(errors)} 项违规：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    tail = "，range 内变更均已抬版本" if range_checked else ""
    print(
        f"seed-ver: 通过（受管区 {len(managed)} 个资产全部声明版本；"
        f"config 自声明 {len(disk_versions)} 个{tail}）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
