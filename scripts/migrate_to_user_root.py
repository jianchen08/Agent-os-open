#!/usr/bin/env python3
"""存量部署迁移到用户空间（ADR 2026-09-13-unified-user-root）。

把仓内的用户可写资产搬到用户空间根：数据库、``data/``、``.env``、以及用户改过的
``config/`` 文件。目的是让用户资产脱离「工作区还原抹除」风险面（仓内 ``config/``
是 git 跟踪文件、``data/`` 与 ``.env`` 虽被 gitignore 但仍在仓整体替换半径内）。

除仓内旧物外，还收口旧布局用户插件根（``%APPDATA%\\agentos\\plugins``；R158/
BUG-48：该路径此前无迁移段，六个用户插件丢失）：**只迁 user_root 缺失的插件
目录**——已有同名目录不覆盖（user_root 是当前活跃副本，覆盖等于用旧快照冲掉
现状）；目录内 ``data/`` 用户数据随迁；``__pycache__``/``.venv``/``node_modules`` 不随迁
（.venv 由 launcher ``uv sync`` 重建），``uv.lock`` 保留（可复现构建）；
junction/symlink（如旧布局 ``_host``）跳过——跨根复制会被解引用成实体树。

**显式调用，不自动迁移**（与 ``tenant_data.migrate_legacy_data_to_default`` 同裁定）：
magic 迁移不可观测，失败/半途中断时状态不明。内核启动时检测到"老库还在、新库已在
用"的形态会打 WARN 指向本脚本（``warn_legacy_db_not_migrated``）。

幂等：目标已存在即跳过该项（不覆盖新库/新数据）。``--dry-run`` 预览。

用法：
  uv run python scripts/migrate_to_user_root.py --dry-run   # 预览将迁移什么
  uv run python scripts/migrate_to_user_root.py             # 执行

迁移后内核重启即按用户空间默认开库；若想继续用老路径，设 ``AGENTOS_DB_PATH``。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

import user_space  # noqa: E402

#: 数据库文件及其 SQLite 伴生文件（WAL 模式下的 -wal/-shm 必须与主库同迁，
#: 否则新库会丢掉尚未 checkpoint 的事务）。
_DB_SUFFIXES = ("", "-wal", "-shm")

#: config/ 下**内核保留段**——永不迁移（归属内核的准入/调度/鉴权配置，且
#: 按 ADR 2026-09-13 这些路径在用户层同样被 denylist 拒绝，迁过去只会得到
#: 一个永远读不到的文件）。与 config_service.rs 的 KERNEL_RESERVED_SEGMENTS 同源。
_KERNEL_RESERVED = {"kernel", "plugin_roots", "auth", "pipelines", "steps"}

#: 插件目录内不随迁的名字：``__pycache__``（字节码缓存）、``.venv``（sidecar
#: 虚拟环境，由 launcher ``uv sync`` 重建）、``node_modules``（前端依赖，可由
#: 包管理器重建）。``uv.lock`` **保留**——可复现构建；插件目录内 ``data/``
#: （用户数据，如 mode_learning/data/game_ledger.json）必须随迁——排除按名字
#: 精确匹配，不波及其他条目。
_PLUGIN_EXCLUDED_DIRS = ("__pycache__", ".venv", "node_modules")


def _resolve_targets() -> tuple[Path, Path]:
    """返回 (user_root, user_data_dir)；用户空间不可得时显式报错退出。"""
    root = user_space.user_root()
    data = user_space.user_data_dir()
    if root is None or data is None:
        print(
            "[migrate] 无法解析用户空间根（既无 AGENTOS_USER_ROOT，也取不到 OS 数据目录）。\n"
            "          请显式设置 AGENTOS_USER_ROOT 后重试。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return root, data


def _planned_moves(user_root: Path, user_data: Path) -> list[tuple[str, Path, Path]]:
    """构造 (标签, 源, 目标) 计划表。只列**实际存在**的源。"""
    moves: list[tuple[str, Path, Path]] = []

    # ① 数据库 + 伴生文件（逐文件迁，源缺失则跳过）
    for suffix in _DB_SUFFIXES:
        src = _REPO_ROOT / f"agentos_kernel.db{suffix}"
        if src.is_file():
            moves.append((f"数据库{suffix or '(主库)'}", src, user_data / src.name))

    # ② data/ 目录（多租户树、uploads、backups）
    legacy_data = _REPO_ROOT / "data"
    if legacy_data.is_dir():
        moves.append(("数据目录 data/", legacy_data, user_data))

    # ③ .env（密钥）
    legacy_env = _REPO_ROOT / ".env"
    if legacy_env.is_file():
        moves.append(("环境变量 .env", legacy_env, user_root / ".env"))

    # ④ 用户改过的 config/ 文件（仅"与 git HEAD 不一致"的那些——没改过的
    #    继续读 factory 即可，不必搬；搬过去反而制造与 factory 的漂移面）
    moves.extend(_dirty_config_moves(user_root))

    return moves


def _dirty_config_moves(user_root: Path) -> list[tuple[str, Path, Path]]:
    """用户改过的 config/ 文件 → 用户配置层（保留相对路径）。

    判定用 ``git status --porcelain``（仓库是 git 工作区时可用）；非 git 环境
    （发行包）返回空——那时也没有"用户改过 vs 出厂"的区别可言。
    """
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
            ["git", "status", "--porcelain", "--", "config/"],  # noqa: S607
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if out.returncode != 0:
        return []

    moves: list[tuple[str, Path, Path]] = []
    for line in out.stdout.splitlines():
        # porcelain v1: "XY <path>"（重命名形态 "XY <old> -> <new>"，取 new 侧）
        body = line[3:].strip() if len(line) > 3 else ""
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        if not body.startswith("config/"):
            continue
        rel = Path(body).relative_to("config")
        if any(seg in _KERNEL_RESERVED for seg in rel.parts):
            continue  # 内核保留段不迁（用户层同样被拒）
        src = _REPO_ROOT / body
        if src.is_file():
            moves.append((f"改过的配置 {body}", src, user_root / "config" / rel))
    return moves


def _same_file(a: Path, b: Path) -> bool:
    """源与目标是否已是同一物理文件（防自迁）。"""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _legacy_plugins_source() -> Path | None:
    """旧布局用户插件根 ``%APPDATA%\\agentos\\plugins``；不可得返回 None。

    源固定取 OS 位置而非 ``user_plugins_dir()``——dev 已把 user_root 钉到仓内
    ``user_root/``（ed81704e7），解析出的插件根是迁移目标本身，不再是源。
    """
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "agentos" / "plugins" if appdata else None


def _is_link_entry(p: Path) -> bool:
    """symlink 或 junction（Windows reparse 目录，如旧布局 plugins/_host）。

    ``Path.is_junction`` 是 3.12 才有的 API（运行基线 >=3.11），用 getattr 探测。
    copytree 会把链接解引用成实体树复制——跨根复制既膨胀又制造漂移副本，
    链接项一律不随迁。
    """
    if p.is_symlink():
        return True
    is_junction = getattr(p, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _plugin_dir_names(plugins_dir: Path) -> list[str]:
    """插件根下的一级子目录清单（核验对比用；目录不存在视为空）。"""
    if not plugins_dir.is_dir():
        return []
    return sorted(p.name for p in plugins_dir.iterdir() if p.is_dir())


def _legacy_plugin_plan(
    user_plugins_dir: Path,
) -> tuple[list[tuple[str, Path, Path]], list[str], list[str]]:
    """旧布局用户插件迁移计划：返回 (将迁移项, 已存在跳过名, 链接跳过名)。

    方向口径：user_root 已有同名插件目录时**不覆盖**（它是当前活跃副本；
    R158 教训——用陈旧快照覆盖会把用户侧修复冲掉），只补 user_root 缺失的
    插件目录。源即目标（user_root 未钉时二者同为 %APPDATA% 位置）则无计划。
    """
    src_root = _legacy_plugins_source()
    if src_root is None or not src_root.is_dir():
        return [], [], []
    if _same_file(src_root, user_plugins_dir):
        return [], [], []
    moves: list[tuple[str, Path, Path]] = []
    skipped_existing: list[str] = []
    skipped_links: list[str] = []
    for entry in sorted(src_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if _is_link_entry(entry):
            skipped_links.append(entry.name)
            continue
        dst = user_plugins_dir / entry.name
        if dst.exists():
            skipped_existing.append(entry.name)
        else:
            moves.append((entry.name, entry, dst))
    return moves, skipped_existing, skipped_links


def _migrate_plugin_dirs(
    moves: list[tuple[str, Path, Path]], user_plugins_dir: Path
) -> None:
    """执行插件目录迁移并输出迁移前后清单核验对比。

    moves 由 ``_legacy_plugin_plan`` 产出——目标必不存在（已有同名目录在计划期
    即被剔除），故本函数从不覆盖目标。复制而非移动：旧源保留可回滚（与库/
    data 同一保守口径）。
    """
    before = _plugin_dir_names(user_plugins_dir)
    for name, src, dst in moves:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_PLUGIN_EXCLUDED_DIRS))
        print(f"  [完成] 用户插件 {name}\n           {src}  ->  {dst}")
    after = _plugin_dir_names(user_plugins_dir)
    added = [n for n in after if n not in before]
    print("[migrate] 插件目录清单核验（迁移前 -> 迁移后）：")
    print(f"  迁移前 ({len(before)})：{', '.join(before) or '（空）'}")
    print(f"  迁移后 ({len(after)})：{', '.join(after) or '（空）'}")
    print(f"  本次新增 ({len(added)})：{', '.join(added) or '（无）'}")


#: 接管登记文件名（ADR 2026-09-14 §2.2；schema 与 Rust 侧 user_space.rs 同源）。
_OWNERSHIP_LEDGER = ".ownership.json"


def _backfill_ownership(user_root: Path, config_rels: list[str], dry_run: bool) -> None:
    """为已迁移的 config/ 文件补接管登记（ADR 2026-09-14 §2.5 存量收口）。

    基线 = ``git show HEAD:config/<rel>`` 的内容 SHA-256（出厂真值）；非 git
    环境或文件不在 HEAD 时记 null（漂移提示降级为弱提示）。账本已损坏则中止
    补登记并要求人工修复——**绝不静默重置**（重置 = 全部接管失去凭证）。
    """
    import hashlib
    import json
    import subprocess
    from datetime import UTC, datetime

    ledger = user_root / "config" / _OWNERSHIP_LEDGER
    entries: list[dict] = []
    if ledger.is_file():
        try:
            loaded = json.loads(ledger.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("top-level must be a list")
            entries = loaded
        except ValueError as exc:
            print(
                f"[migrate] 接管登记账本损坏，跳过补登记（请人工修复 {ledger}）: {exc}",
                file=sys.stderr,
            )
            return
    known = {
        str(e.get("path")).replace("\\", "/")
        for e in entries
        if isinstance(e, dict) and e.get("path")
    }

    def _factory_sha(rel: str) -> str | None:
        try:
            out = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
                ["git", "show", f"HEAD:config/{rel}"],  # noqa: S607
                cwd=_REPO_ROOT,
                capture_output=True,
                check=False,
            )
        except OSError:
            return None
        return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None

    added = 0
    for rel in config_rels:
        rel_norm = str(rel).replace("\\", "/")
        if rel_norm in known:
            continue
        sha = _factory_sha(rel_norm)
        entries.append(
            {
                "path": rel_norm,
                "seeded_from_sha256": sha,
                "seeded_at": datetime.now(UTC).isoformat(),
                "factory_version": None,
            }
        )
        added += 1
        if dry_run:
            print(
                f"  [登记] {rel_norm}（基线 sha256={'有' if sha else '未知，漂移提示将为弱提示'}）"
            )

    if dry_run or added == 0:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ledger)
    print(f"[migrate] 接管登记补录 {added} 条 → {ledger}")


def _config_rel_of(src: Path) -> str | None:
    """迁移源若是 config/ 下文件，返回相对 config 根的路径；否则 None。"""
    try:
        return str(src.relative_to(_REPO_ROOT / "config"))
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移存量部署到用户空间（ADR 2026-09-13）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不做任何改动")
    parser.add_argument(
        "--force",
        action="store_true",
        help="目标已存在时也覆盖（默认跳过；覆盖会丢弃目标侧现状，慎用）",
    )
    args = parser.parse_args()

    user_root, user_data = _resolve_targets()
    print(f"[migrate] 用户空间根: {user_root}")
    print(f"[migrate] 数据根:     {user_data}")

    moves = _planned_moves(user_root, user_data)
    user_plugins_dir = user_space.user_plugins_dir() or user_root / "plugins"
    plugin_moves, plugin_existing, plugin_links = _legacy_plugin_plan(user_plugins_dir)
    plugin_src = _legacy_plugins_source()

    if not moves and not plugin_moves:
        print("[migrate] 没有需要迁移的内容（源均不存在或已迁过）。")
        return 0

    if args.dry_run:
        print(f"\n[migrate] --dry-run：计划迁移 {len(moves)} 项（未做任何改动）")
        for label, src, dst in moves:
            mark = "覆盖" if dst.exists() and args.force else ("跳过(目标已存在)" if dst.exists() else "迁移")
            print(f"  [{mark}] {label}\n            {src}  ->  {dst}")
        if plugin_moves or plugin_existing or plugin_links:
            print(
                f"\n[migrate] 用户插件（源 {plugin_src} → {user_plugins_dir}；"
                "已有同名目录不覆盖，保留 user_root 现状）："
            )
            for name, src, dst in plugin_moves:
                print(f"    [迁移] {name}\n            {src}  ->  {dst}")
            if plugin_existing:
                print(f"  已存在跳过 {len(plugin_existing)} 个：{', '.join(plugin_existing)}")
            if plugin_links:
                print(
                    f"  跳过链接项 {len(plugin_links)} 个（junction/symlink 不随迁）："
                    f"{', '.join(plugin_links)}"
                )
        config_rels = [r for r in (_config_rel_of(src) for _, src, _ in moves) if r]
        if config_rels:
            print(f"\n[migrate] 迁移后将补接管登记 {len(config_rels)} 条（ADR 2026-09-14）：")
            _backfill_ownership(user_root, config_rels, dry_run=True)
        print("\n去掉 --dry-run 执行。")
        return 0

    moved = skipped = 0
    moved_config_rels: list[str] = []
    print()
    for label, src, dst in moves:
        if _same_file(src, dst):
            print(f"  [跳过] {label}（源与目标同一文件）")
            skipped += 1
            continue
        if dst.exists() and not args.force:
            print(f"  [跳过] {label}（目标已存在：{dst}；如需覆盖加 --force）")
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 库文件用 copy 而非 move：SQLite 主库在迁移瞬间可能仍被运行中的内核持有，
        # 直接 move 会让在跑的进程指向已消失的路径。copy 后保留源（用户确认无虞
        # 后自行删除），是"可回滚"的保守选择（失败/误判都能退回旧状态）。
        shutil.copy2(src, dst)
        rel = _config_rel_of(src)
        if rel:
            moved_config_rels.append(rel)
        print(f"  [完成] {label}\n           {src}  ->  {dst}")
        moved += 1

    if moved_config_rels:
        _backfill_ownership(user_root, moved_config_rels, dry_run=False)

    if plugin_moves:
        print()
        print(
            f"[migrate] 用户插件迁移：{len(plugin_moves)} 个目录"
            f"（源 {plugin_src}，已有同名不覆盖）"
        )
        _migrate_plugin_dirs(plugin_moves, user_plugins_dir)
        print(f"[migrate] 旧插件目录已保留（未删除）：{plugin_src}")

    summary = f"\n[migrate] 完成：迁移 {moved} 项，跳过 {skipped} 项"
    if plugin_moves:
        summary += f"；用户插件迁移 {len(plugin_moves)} 个"
    print(summary + "。")
    print(
        "[migrate] 源文件已保留（未删除）——确认新位置工作正常后，可自行清理：\n"
        f"          {_REPO_ROOT / 'agentos_kernel.db'}*、{_REPO_ROOT / 'data'}、{_REPO_ROOT / '.env'}"
    )
    print("[migrate] 重启内核即按用户空间默认开库；想继续用老库则设 AGENTOS_DB_PATH 指向它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
