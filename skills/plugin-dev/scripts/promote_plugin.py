#!/usr/bin/env python3
"""staging → 用户插件根迁移器（插件开发技能 skills/plugin-dev 自带脚本）。

五步流转管线第③步（ADR 2026-11-03-plugin-dev-skill-and-staging-verify-pipeline §D2）：
装载验证通过后，把 staging 目录迁移到用户插件根，迁移动作内嵌 BuiltinFirst 版本纪律
——目标已存在时，staging 副本版本号必须**严格大于**现役副本（持平=同步无效，倒退=拒绝）。
版本比较用 packaging.version.Version（成熟包，禁自造 semver 轮子）。

迁移策略：目标已存在时先把旧目录改名 .bak-<时间戳>（保留回滚），再复制新目录；
旧目录不自动删（回滚责任在调用方），报告里给出去向。

用法：
  python skills/plugin-dev/scripts/promote_plugin.py <staged_dir> \\
    --user-plugins-dir <用户插件根> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

try:
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - packaging 随 setuptools 常驻；缺包显式报错不静默
    Version = None  # type: ignore[assignment,misc]
    InvalidVersion = ValueError  # type: ignore[assignment,misc]


class PromoteError(Exception):
    """迁移失败（staging 非法/版本纪律不满足/目标不可写）。"""


def read_plugin_meta(plugin_dir: Path) -> dict[str, Any]:
    """读插件目录 plugin.json，返回 {id, version}；目录非法即抛 PromoteError。"""
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise PromoteError(f"staging 目录缺 plugin.json: {plugin_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromoteError(f"plugin.json 不可解析: {exc}") from exc
    plugin_id = manifest.get("id")
    version = manifest.get("version")
    if not plugin_id or not version:
        raise PromoteError(f"plugin.json 缺 id/version: {manifest_path}")
    return {"id": str(plugin_id), "version": str(version)}


def is_strictly_greater(candidate: str, current: str) -> bool:
    """BuiltinFirst 版本纪律：candidate 必须严格大于 current。

    无法解析为版本号的一律 False（fail-closed，不做字符串字典序猜测）。
    """
    if Version is None:
        raise PromoteError("缺少 packaging 库（pip install packaging）——版本比较拒绝降级")
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


def promote(
    staged_dir: Path, user_plugins_dir: Path, dry_run: bool = False
) -> dict[str, Any]:
    """staging → 用户插件根迁移。返回结果报告（promoted/version/from/to/backup）。"""
    meta = read_plugin_meta(staged_dir)
    target = user_plugins_dir / meta["id"]
    backup: str | None = None
    if target.exists():
        current = read_plugin_meta(target)["version"]
        if not is_strictly_greater(meta["version"], current):
            raise PromoteError(
                f"版本纪律不满足（BuiltinFirst）：staging {meta['version']} "
                f"未严格大于现役 {current}——持平=同步无效，倒退=拒绝。"
                f"请先在 staging 内 bump plugin.json version 再迁移。"
            )
        backup = f"{target.name}.bak-{time.strftime('%Y%m%d_%H%M%S')}"
    if not dry_run:
        user_plugins_dir.mkdir(parents=True, exist_ok=True)
        if backup is not None:
            target.rename(target.parent / backup)
        shutil.copytree(staged_dir, target,
                        ignore=shutil.ignore_patterns("__pycache__", ".venv", ".mypy_cache"))
    return {
        "promoted": not dry_run,
        "dry_run": dry_run,
        "plugin_id": meta["id"],
        "version": meta["version"],
        "from": str(staged_dir),
        "to": str(target),
        "backup": backup,
        "note": (
            "迁移完成：现役应用 watcher 热发现接管（零重启）；"
            "装机版生效另走 sync_installed_user_space.py（人工执行、先问用户）。"
            if not dry_run else
            "dry-run：仅校验未落盘。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="staging → 用户插件根迁移器")
    parser.add_argument("staged_dir", help="staging 内插件目录")
    parser.add_argument("--user-plugins-dir", required=True, help="用户插件根目录")
    parser.add_argument("--dry-run", action="store_true", help="仅校验不落盘")
    args = parser.parse_args(argv)
    try:
        report = promote(Path(args.staged_dir), Path(args.user_plugins_dir), args.dry_run)
    except PromoteError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
