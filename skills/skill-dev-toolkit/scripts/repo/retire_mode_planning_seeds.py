#!/usr/bin/env python3
"""mode_planning 出厂种子用户空间副本一次性退役脚本（P4 裁定，ADR 2026-10-09
模式体系收口批 B3）。

mode_planning 包已从仓内出厂种子集删除；本脚本清理用户空间的残留副本与种子
账本条目，防副本成「手工副本」僵尸插件常驻：
- 删 `<user_root>/plugins/modes/mode_planning/` 整目录；
- 清 `<user_root>/plugins/modes/.seeds.json` 中 `mode_planning` 条目。

安全边界（P4：存量定制先提示差异再删）：
- 账本条目在场时逐文件对账（语义同 kernel user_space reconcile：账本清单内
  任一文件哈希不一致/缺失 = 已定制；账本外用户自加文件不参与判定）；
  已定制而未给 `--force` → 打印差异清单，退出码 2，不删任何东西；
- 账本无条目（手工放置副本，无基线可比）→ 视同已定制，同样要求 `--force`。

用法：
  python scripts/retire_mode_planning_seeds.py [--user-root PATH] [--force] [--dry-run]
缺省 --user-root = 仓内 dev user_root（<repo>/user_root）；装机版显式传
%APPDATA%\\agentos。本脚本只动用户空间，绝不写安装目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODE_ID = "mode_planning"
LEDGER_REL = Path("plugins/modes") / ".seeds.json"
COPY_REL = Path("plugins/modes") / MODE_ID


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_ledger(user_root: Path) -> dict:
    path = user_root / LEDGER_REL
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    ledger = json.loads(raw)
    if not isinstance(ledger, dict):
        raise ValueError(f"seeds ledger 形态非法（须为对象）: {path}")
    return ledger


def save_ledger(user_root: Path, ledger: dict) -> None:
    """原子写回账本（tmp + rename，同 kernel user_space 落盘形态）。"""
    path = user_root / LEDGER_REL
    body = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def collect_diffs(copy_dir: Path, entry: dict | None) -> list[str]:
    """账本基线对账：返回差异清单（空 = 未定制）。无账本条目 = 手工副本。"""
    if entry is None:
        return ["<账本无 mode_planning 条目（手工放置副本，无基线可比）>"]
    files = entry.get("files", {})
    diffs: list[str] = []
    for rel, expected in sorted(files.items()):
        current = copy_dir / rel
        if not current.is_file():
            diffs.append(f"[缺失] {rel}")
        elif sha256_file(current) != expected:
            diffs.append(f"[已改] {rel}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--user-root",
        default=str(REPO_ROOT / "user_root"),
        help="用户空间根（缺省 = 仓内 dev user_root；装机版传 %%APPDATA%%\\agentos）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="副本已定制时仍执行删除（须先看过差异清单）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    args = parser.parse_args(argv)

    user_root = Path(args.user_root).resolve()
    copy_dir = user_root / COPY_REL
    ledger_path = user_root / LEDGER_REL
    print(f"[target] 用户空间: {user_root}" + ("  [dry-run]" if args.dry_run else ""))

    if not copy_dir.exists() and not ledger_path.is_file():
        print(f"[ok] 无残留（副本与账本均缺席）: {COPY_REL}")
        return 0

    ledger = load_ledger(user_root)
    entry = ledger.get(MODE_ID)
    if copy_dir.exists():
        diffs = collect_diffs(copy_dir, entry)
        if diffs and not args.force:
            print(f"[held] 副本存在定制痕迹，须人工确认后加 --force 才删（{len(diffs)} 项）:")
            for line in diffs:
                print(f"  {line}")
            print("[held] 未删除任何文件；确认放弃定制后重跑并加 --force")
            return 2

    actions: list[str] = []
    if copy_dir.exists():
        actions.append(f"删副本目录 {COPY_REL}")
    if entry is not None:
        actions.append(f"清账本条目 {MODE_ID}（{ledger_path}）")
    if not actions:
        print(f"[ok] 账本已无 {MODE_ID} 条目且副本缺席，无事可做")
        return 0
    for action in actions:
        print(f"[plan] {action}")
    if args.dry_run:
        print("[dry-run] 未执行")
        return 0

    if copy_dir.exists():
        shutil.rmtree(copy_dir)
        print(f"[done] 已删除 {copy_dir}")
    if entry is not None:
        ledger.pop(MODE_ID, None)
        save_ledger(user_root, ledger)
        print(f"[done] 已清账本条目 {MODE_ID}")
    print("[ok] mode_planning 用户空间残留清理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
