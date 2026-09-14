#!/usr/bin/env python3
"""存量部署迁移到用户空间（ADR 2026-09-13-unified-user-root）。

把仓内的用户可写资产搬到用户空间根：数据库、``data/``、``.env``、以及用户改过的
``config/`` 文件。目的是让用户资产脱离「工作区还原抹除」风险面（仓内 ``config/``
是 git 跟踪文件、``data/`` 与 ``.env`` 虽被 gitignore 但仍在仓整体替换半径内）。

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
    if not moves:
        print("[migrate] 没有需要迁移的内容（源均不存在或已迁过）。")
        return 0

    if args.dry_run:
        print(f"\n[migrate] --dry-run：计划迁移 {len(moves)} 项（未做任何改动）")
        for label, src, dst in moves:
            mark = "覆盖" if dst.exists() and args.force else ("跳过(目标已存在)" if dst.exists() else "迁移")
            print(f"  [{mark}] {label}\n            {src}  ->  {dst}")
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

    print(f"\n[migrate] 完成：迁移 {moved} 项，跳过 {skipped} 项。")
    print(
        "[migrate] 源文件已保留（未删除）——确认新位置工作正常后，可自行清理：\n"
        f"          {_REPO_ROOT / 'agentos_kernel.db'}*、{_REPO_ROOT / 'data'}、{_REPO_ROOT / '.env'}"
    )
    print("[migrate] 重启内核即按用户空间默认开库；想继续用老库则设 AGENTOS_DB_PATH 指向它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
