#!/usr/bin/env python3
"""仓内插件单向同步到用户空间（repo ``plugins/shared`` → ``<user_root>/plugins``）。

用户裁决（2026-09-19）：开发者只改仓内，仓内更新由 launcher 启动期自动带到
用户空间；方向**单向**（repo→user_root），绝不反向覆盖、绝不删除 user_root
独有内容——用户数据与用户自有插件（如 mode_learning）是活跃副本，不是仓的
从属物（R158 教训）。user_root 下非插件内容（config/data/.env）不触碰：
本脚本只写 ``<user_root>/plugins/<插件名>/`` 子树。

同步规则：
- user_root 缺失的插件目录 → 整目录复制（排 ``__pycache__``/``.venv``）；
- 已存在 → 逐文件按 mtime/size 增量覆盖**仓内较新**的文件（copy2 保留
  mtime，同步后两侧一致，重复执行零复制=幂等）；用户侧较新文件不被仓内
  旧版本冲掉；
- 目标为 junction/symlink 的插件跳过（不写入链接目标树）；
- 仓内跨类别同名插件目录按排序取首个，其余记入 ``duplicate_names``。

**写目标必须显式**（``--user-root`` > ``AGENTOS_USER_PLUGINS_DIR`` >
``AGENTOS_USER_ROOT``）：均未给定时拒绝执行——OS 默认数据目录（
``%APPDATA%\\agentos``）是休眠旧用户空间 + 装机版用户空间，静默写入违反
装机/开发零共享。``--dry-run`` 不受限（预览可按 OS 默认位置展示，不落盘）。

幂等：同步后 dst mtime == src mtime 且 size 相同 ⇒ 下次判定为最新跳过。
日志逐文件输出本次复制的目标路径。

用法：
  python scripts/sync_user_root.py                  # launcher 启动期调用（env 已钉）
  python scripts/sync_user_root.py --user-root U    # 显式指定用户空间根
  python scripts/sync_user_root.py --dry-run        # 只列计划不落盘
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

import user_space  # noqa: E402

#: 复制与发现都排除的目录名：``__pycache__``（字节码缓存）、``.venv``
#: （sidecar 虚拟环境，由 launcher ``uv sync`` 重建）。按名字精确匹配，
#: 不波及其他条目（插件内 ``data/`` 用户数据照常同步）。
EXCLUDED_DIR_NAMES = ("__pycache__", ".venv")

#: 插件目录标记文件；发现只认含此文件的目录。
_PLUGIN_MARKER = "plugin.json"

#: 发现深度上限：``shared/<n>``（1 层）、``shared/<cat>/<n>``（2 层）、
#: ``shared/pipeline/<phase>/<n>``（3 层）——即 launcher venv 步记录的三种布局。
_MAX_DISCOVERY_DEPTH = 3


@dataclass
class SyncOutcome:
    """一次同步的可观测结果（日志与测试断言的载体）。"""

    #: 本次实际复制（dry_run 时为计划写入）的目标文件绝对路径
    copied: list[str] = field(default_factory=list)
    #: 目标为 junction/symlink 而跳过的插件名
    link_skipped: list[str] = field(default_factory=list)
    #: 跨类别同名冲突中未同步的插件名（首个已同步）
    duplicate_names: list[str] = field(default_factory=list)


def _is_link_entry(p: Path) -> bool:
    """symlink 或 junction（Windows reparse 目录，如 user_root/plugins/_host）。

    向链接目录内复制会写入被指目标树——链接项一律不落笔。
    ``Path.is_junction`` 是 3.12 才有的 API（运行基线 >=3.11），用 getattr 探测。
    """
    is_junction = getattr(p, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    return p.is_symlink()


def _same_file(a: Path, b: Path) -> bool:
    """两路径是否指向同一目录（源即目标时同步无意义）。"""
    if not (a.exists() and b.exists()):
        return False
    return bool(os.path.samefile(a, b))


def iter_plugin_dirs(shared_root: Path) -> list[Path]:
    """``plugins/shared`` 下的插件目录（含 ``plugin.json``，深度 ≤3），排序确定。

    目录含标记文件即视为插件根（不再下钻）；不含则继续下探（覆盖
    ``system|tools/<n>`` 与 ``pipeline/<phase>/<n>`` 布局）。隐藏目录与排除项
    不下钻——发现只找插件根，不做内容遍历。
    """
    found: list[Path] = []

    def walk(dir_path: Path, depth: int) -> None:
        if depth > _MAX_DISCOVERY_DEPTH:
            return
        for entry in sorted(dir_path.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or _is_link_entry(entry):
                continue
            if entry.name.startswith(".") or entry.name in EXCLUDED_DIR_NAMES:
                continue
            if (entry / _PLUGIN_MARKER).is_file():
                found.append(entry)
            else:
                walk(entry, depth + 1)

    walk(shared_root, 1)
    return found


def _copy_newer(
    src_file: Path, dst_file: Path, copied: list[str], dry_run: bool
) -> None:
    """仓内较新则覆盖：size 不同或 src mtime 严格更新才复制（copy2 保留 mtime）。"""
    if dst_file.is_file():
        s_stat, d_stat = src_file.stat(), dst_file.stat()
        if s_stat.st_size == d_stat.st_size and s_stat.st_mtime <= d_stat.st_mtime:
            return
    copied.append(str(dst_file))
    if dry_run:
        return
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dst_file)


def sync_tree(
    src_dir: Path, dst_dir: Path, copied: list[str], dry_run: bool
) -> None:
    """把 ``src_dir`` 增量并入 ``dst_dir``：只覆盖仓内较新文件，**永不删除**。

    dst 独有的文件/子目录一律原样保留（R158 教训：用户侧是活跃副本）。
    链接项不随迁（解引用复制会制造漂移实体树）。
    """
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name):
        if _is_link_entry(entry) or entry.name in EXCLUDED_DIR_NAMES:
            continue
        target = dst_dir / entry.name
        if entry.is_dir():
            sync_tree(entry, target, copied, dry_run)
        elif entry.is_file():
            _copy_newer(entry, target, copied, dry_run)


def sync_repo_plugins_to_user_root(
    repo_shared: Path, user_plugins: Path, *, dry_run: bool = False
) -> SyncOutcome:
    """执行一次单向同步，返回可观测结果。源即目标（同目录）时无操作。

    ``dry_run=True`` 只列计划不落盘（``copied`` 为将写入清单）。
    """
    outcome = SyncOutcome()
    if _same_file(repo_shared, user_plugins):
        return outcome
    seen: set[str] = set()
    for src in iter_plugin_dirs(repo_shared):
        if src.name in seen:
            outcome.duplicate_names.append(src.name)
            continue
        seen.add(src.name)
        dst = user_plugins / src.name
        if dst.exists() and _is_link_entry(dst):
            outcome.link_skipped.append(src.name)
            continue
        sync_tree(src, dst, outcome.copied, dry_run)
    return outcome


def _env_plugins_dir() -> Path | None:
    """环境变量解析的写目标：``AGENTOS_USER_PLUGINS_DIR`` > ``AGENTOS_USER_ROOT/plugins``。

    与 ``user_space`` 的 trim/空白语义一致，但**不做 OS 默认回退**——写操作
    必须显式目标（装机/开发零共享，用户裁定 2026-09-19）。
    """
    direct = os.environ.get(user_space.USER_PLUGINS_DIR_ENV)
    if direct and direct.strip():
        return Path(direct.strip())
    root = os.environ.get(user_space.USER_ROOT_ENV)
    if root and root.strip():
        return Path(root.strip()) / "plugins"
    return None


def _resolve_user_plugins_dir(
    user_root: Path | None, dry_run: bool
) -> tuple[Path | None, bool]:
    """解析写目标；返回 (目标, 是否 OS 默认回退)。

    优先级：``--user-root`` > 环境变量 > OS 默认（仅 ``dry_run`` 预览可得）。
    全部不可得时目标为 ``None``。
    """
    if user_root is not None:
        return user_root / "plugins", False
    explicit = _env_plugins_dir()
    if explicit is not None:
        return explicit, False
    if dry_run:
        return user_space.user_plugins_dir(), True
    return None, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="repo plugins/shared → user_root/plugins 单向增量同步（永不删除用户侧内容）。"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="仓根（默认取本脚本所在仓）",
    )
    parser.add_argument(
        "--user-root",
        type=Path,
        default=None,
        help="用户空间根（写目标必须显式；优先级高于 AGENTOS_USER_PLUGINS_DIR/AGENTOS_USER_ROOT）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列计划不落盘；写目标未显式给定时按 OS 默认位置展示",
    )
    args = parser.parse_args(argv)

    repo_shared = args.repo_root / "plugins" / "shared"
    if not repo_shared.is_dir():
        print(f"[sync-user-root] [ERROR] 仓内插件根不存在：{repo_shared}")
        return 1
    user_plugins, is_os_default = _resolve_user_plugins_dir(
        args.user_root, args.dry_run
    )
    if user_plugins is None:
        print(
            "[sync-user-root] [ERROR] 未显式指定写目标（--user-root、"
            "AGENTOS_USER_PLUGINS_DIR、AGENTOS_USER_ROOT 均未设置）。"
        )
        print(
            "          OS 默认数据目录（%APPDATA%\\agentos）是休眠旧用户空间 + "
            "装机版用户空间，禁止静默写入（装机/开发零共享）。"
        )
        print("          请设置 AGENTOS_USER_ROOT（或传 --user-root）；仅预览可加 --dry-run。")
        return 1

    outcome = sync_repo_plugins_to_user_root(
        repo_shared, user_plugins, dry_run=args.dry_run
    )
    fallback_note = "（OS 默认回退，仅预览）" if is_os_default else ""
    print(f"[sync-user-root] 源 {repo_shared} → 目标 {user_plugins}{fallback_note}")
    action = "将写入" if args.dry_run else "写入"
    for target in outcome.copied:
        print(f"  [{action}] {target}")
    for name in outcome.link_skipped:
        print(f"  [跳过] {name}：目标为链接（junction/symlink），不写入链接目标树")
    for name in outcome.duplicate_names:
        print(f"  [跳过] {name}：跨类别同名插件目录，已按排序取首个同步")
    if not outcome.copied:
        print("[sync-user-root] 完成：用户空间已是最新，无文件变动。")
    elif args.dry_run:
        print(
            f"[sync-user-root] 完成：计划写入 {len(outcome.copied)} 个文件"
            "（--dry-run，未落盘）。"
        )
    else:
        print(
            f"[sync-user-root] 完成：写入 {len(outcome.copied)} 个文件"
            "（未删除任何用户侧内容）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
