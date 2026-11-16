#!/usr/bin/env python3
"""仓内开发/运维脚本镜像同步器（开发工具箱技能自带脚本）。

产品裁定（2026-10-19）：运营、开发脚本全部统一放进技能包，技能随任务工作空间
快照可达任意环境（含容器隔离下仓根不可达场景）。真源 = 仓内 ``scripts/``（改
脚本只改真源）；技能内 ``scripts/repo/`` = 本工具生成的精确镜像，**禁手改**。

用法：
  python skills/skill-dev-toolkit/scripts/sync_from_repo.py           # 同步（精确镜像，删差量）
  python skills/skill-dev-toolkit/scripts/sync_from_repo.py --check   # 只验一致；漂移→退出码 1
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import sys
from pathlib import Path

EXCLUDE_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    ".ai_workspaces",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDE_SUFFIXES = {".pyc", ".log"}


class SyncError(Exception):
    """镜像同步失败（真源缺失等）。"""


def default_repo_root() -> Path:
    """脚本位于 <repo>/skills/skill-dev-toolkit/scripts/，上溯三级即仓根。"""
    return Path(__file__).resolve().parents[3]


def _is_included(path: Path) -> bool:
    return not (path.suffix in EXCLUDE_SUFFIXES or any(part in EXCLUDE_DIRS for part in path.parts))


def _collect(root: Path, apply_exclusions: bool = True) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if p.is_file() and (not apply_exclusions or _is_included(p.relative_to(root)))
    }


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _file_map(root: Path) -> dict[str, str]:
    return {rel: _digest(path) for rel, path in _collect(root).items()}


def _force_unlink(path: Path) -> None:
    """删除文件；Windows 只读位（镜像里常见 .git/objects）先清位再删。"""
    try:
        path.unlink()
    except PermissionError:
        path.chmod(stat.S_IWRITE)
        path.unlink()


def iter_dirs_bottom_up(root: Path):
    """自底向上惰性产出 (dir, subdirs, files)；调用方边遍历边删空目录，访问时点的
    子目录计数才可靠（急切预计算会把刚清空的父目录误判为非空）。"""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            subdirs = [s.name for s in path.iterdir() if s.is_dir()]
            files = [f.name for f in path.iterdir() if f.is_file()]
            yield path, subdirs, files


def sync(source: Path, target: Path) -> dict[str, list[str]]:
    """真源 → 镜像精确同步；返回 {copied, removed} 相对路径清单。"""
    if not source.is_dir():
        raise SyncError(f"真源目录缺失: {source}")
    src_files = _collect(source)
    # 镜像侧不施加排除：目标是"恰好等于真源收录集"，镜像里混进的垃圾要能被识别并删除
    dst_files = _collect(target, apply_exclusions=False) if target.is_dir() else {}
    copied: list[str] = []
    for rel, src_path in src_files.items():
        dst_path = target / rel
        if rel not in dst_files or _digest(src_path) != _digest(dst_path):
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            copied.append(rel)
    removed: list[str] = []
    for rel in sorted(set(dst_files) - set(src_files)):
        _force_unlink(target / rel)
        removed.append(rel)
    for dirpath, dirnames, filenames in iter_dirs_bottom_up(target):
        if not dirnames and not filenames:
            try:
                dirpath.rmdir()
            except PermissionError:
                dirpath.chmod(stat.S_IWRITE)
                dirpath.rmdir()
    return {"copied": copied, "removed": removed}


def os_walk_bottom_up(root: Path) -> list[tuple[Path, list[str], list[str]]]:
    """rglob 自底向上收集 (dir, subdirs, files)，供空目录清理。"""
    entries = [(p, p.name) for p in sorted(root.rglob("*"), reverse=True) if p.is_dir()]
    result = []
    for path, _ in entries:
        subdirs = [s.name for s in path.iterdir() if s.is_dir()]
        files = [f.name for f in path.iterdir() if f.is_file()]
        result.append((path, subdirs, files))
    return result


def check(source: Path, target: Path) -> dict[str, list[str]]:
    """镜像一致性核验：missing / extra / differing（字节级）。"""
    if not source.is_dir():
        raise SyncError(f"真源目录缺失: {source}")
    if not target.is_dir():
        return {"missing": sorted(_file_map(source)), "extra": [], "differing": []}
    src_hashes = _file_map(source)
    dst_hashes = {
        rel: _digest(path) for rel, path in _collect(target, apply_exclusions=False).items()
    }
    return {
        "missing": sorted(set(src_hashes) - set(dst_hashes)),
        "extra": sorted(set(dst_hashes) - set(src_hashes)),
        "differing": sorted(rel for rel in set(src_hashes) & set(dst_hashes) if src_hashes[rel] != dst_hashes[rel]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仓内脚本 → 技能镜像同步器")
    parser.add_argument("--check", action="store_true", help="只验一致不落盘；漂移退出码 1")
    parser.add_argument("--source", default=None, help="真源目录（默认 <repo>/scripts）")
    parser.add_argument("--target", default=None, help="镜像目录（默认 <repo>/skills/skill-dev-toolkit/scripts/repo）")
    args = parser.parse_args(argv)
    repo_root = default_repo_root()
    source = Path(args.source) if args.source else repo_root / "scripts"
    target = Path(args.target) if args.target else repo_root / "skills" / "skill-dev-toolkit" / "scripts" / "repo"
    try:
        if args.check:
            diff = check(source, target)
            drift = {k: v for k, v in diff.items() if v}
            if drift:
                print(f"镜像漂移（先跑 sync_from_repo.py 同步后再提交）: {drift}", file=sys.stderr)
                return 1
            print(f"镜像一致（{len(_file_map(source))} 个文件）")
            return 0
        result = sync(source, target)
        total = len(_collect(target))
        print(f"已同步镜像 {target}（现 {total} 个文件）；本次复制 {len(result['copied'])}、删除 {len(result['removed'])}")
        for rel in result["copied"]:
            print(f"  + {rel}")
        for rel in result["removed"]:
            print(f"  - {rel}")
        return 0
    except SyncError as exc:
        print(f"镜像同步失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
