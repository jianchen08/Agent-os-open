#!/usr/bin/env python3
"""SQLite 主库快照备份（VACUUM INTO）+ 7 份轮转 + 完整性校验。

与内核 storage 解析同口径（kernel/crates/engine/src/storage_factory.rs）：
  db 路径 = AGENTOS_DB_PATH 环境变量 > config/kernel/storage.yaml sqlite.path > 项目根 agentos_kernel.db；
  driver  = AGENTOS_STORAGE_DRIVER > config/kernel/storage.yaml driver > sqlite。
driver 非 sqlite（memory/postgres）时显式报错拒绝——内存库无文件可备份，
备份不存在的库 = 假安全。快照机制与 db_routes.rs backup_before_clear 同源
（VACUUM INTO：一致性快照，对运行中的内核安全，不阻塞不改动源库）。

用法：
  uv run python scripts/backup_db.py            # 打一份快照到 data/backups/ 并轮转（保留最新 7 份）
  uv run python scripts/backup_db.py --verify   # 对最新快照做 PRAGMA integrity_check

触发方式为手动 / 外部 cron（建议每日一次）；内核启动时自动触发不在本刀
（涉内核文件，见 docs/guides/backup-restore.md）。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_DB_PATH = "agentos_kernel.db"
ENV_STORAGE_DRIVER = "AGENTOS_STORAGE_DRIVER"
ENV_DB_PATH = "AGENTOS_DB_PATH"
STORAGE_YAML = ROOT / "config" / "kernel" / "storage.yaml"
KEEP_SNAPSHOTS = 7
# 快照命名：<库名>-<UTC 时间戳 YYYYMMDDTHHMMSSmmm>.db（毫秒精度对齐 db_routes.rs 唯一性约定）
SNAPSHOT_RE = re.compile(r"^.+-(\d{8}T\d{6}\d{3})\.db$")


class BackupError(RuntimeError):
    """备份/校验失败（配置拒绝、源库缺失、快照校验不过等）。"""


def _read_storage_yaml() -> tuple[str | None, str | None]:
    """读 config/kernel/storage.yaml 的 (driver, sqlite.path)，缺文件/缺节返回 (None, None)。"""
    if not STORAGE_YAML.exists():
        return None, None
    import yaml

    doc = yaml.safe_load(STORAGE_YAML.read_text(encoding="utf-8")) or {}
    storage = doc.get("storage") or {}
    sqlite_section = storage.get("sqlite") or {}
    driver = storage.get("driver") or None
    path = sqlite_section.get("path") or None
    return driver, path


def resolve_target() -> Path:
    """解析主库路径与 driver；driver 非 sqlite 拒绝，路径相对项目根展开。"""
    env_driver = _env(ENV_STORAGE_DRIVER)
    yaml_driver, yaml_path = _read_storage_yaml()
    driver = env_driver or yaml_driver or "sqlite"
    if driver != "sqlite":
        raise BackupError(
            f"driver={driver!r}（{ENV_STORAGE_DRIVER}={env_driver!r}"
            f"{'' if env_driver else '，来自 config/kernel/storage.yaml'}）不是 sqlite，"
            "本工具只备份 SQLite 文件库——内存库无文件、其他 driver 未落地"
        )
    raw = _env(ENV_DB_PATH) or yaml_path or DEFAULT_DB_PATH
    if raw == ":memory:":
        raise BackupError("db 路径为 :memory:（内存库），无文件可备份")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise BackupError(f"主库文件不存在: {path}（{ENV_DB_PATH}={_env(ENV_DB_PATH)!r}）")
    return path


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def snapshot(db_path: Path) -> Path:
    """VACUUM INTO 打一致性快照，返回快照路径。源库只读连接，不触碰运行中内核。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]
    target = BACKUP_DIR / f"{db_path.stem}-{ts}.db"
    if target.exists():
        raise BackupError(f"快照目标已存在（毫秒时间戳撞车，重跑即可）: {target}")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?1", (str(target),))
    except sqlite3.Error as e:
        raise BackupError(f"VACUUM INTO 快照失败（源库 {db_path}）: {e}") from e
    finally:
        conn.close()
    return target


def _snapshots_newest_first() -> list[tuple[str, Path]]:
    """备份目录下全部快照，按名字内嵌时间戳新→旧。返回 (时间戳, 路径) 对。"""
    pairs = []
    for p in sorted(BACKUP_DIR.iterdir()):
        m = SNAPSHOT_RE.match(p.name)
        if m:
            pairs.append((m.group(1), p))
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    return pairs


def rotate() -> list[Path]:
    """按快照名内嵌时间戳轮转，只保留最新 KEEP_SNAPSHOTS 份，返回被删清单。"""
    snapshots = _snapshots_newest_first()
    removed = []
    for _, old in snapshots[KEEP_SNAPSHOTS:]:
        old.unlink()
        removed.append(old)
    return removed


def newest_snapshot() -> Path:
    if not BACKUP_DIR.is_dir():
        raise BackupError(f"备份目录不存在: {BACKUP_DIR}")
    snapshots = _snapshots_newest_first()
    if not snapshots:
        raise BackupError(f"{BACKUP_DIR} 下无快照可校验（先跑一次不带 --verify 的备份）")
    return snapshots[0][1]


def verify(snapshot_path: Path) -> str:
    """PRAGMA integrity_check 校验快照，返回结果原文（'ok' 之外一律抛错）。"""
    conn = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    result = "; ".join(r[0] for r in rows)
    if result != "ok":
        raise BackupError(f"快照完整性校验未通过: {snapshot_path} → {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 主库快照备份/校验（详见模块 docstring）")
    parser.add_argument("--verify", action="store_true", help="对最新快照做 PRAGMA integrity_check（不打新快照）")
    args = parser.parse_args()

    try:
        if args.verify:
            target = newest_snapshot()
            result = verify(target)
            print(f"integrity_check: {result} ({target.name})")
            return 0
        db_path = resolve_target()
        target = snapshot(db_path)
        removed = rotate()
        result = verify(target)
        size_kb = target.stat().st_size / 1024
        print(f"快照完成: {target}（{size_kb:.1f} KB，integrity_check={result}）")
        if removed:
            print("轮转删除: " + ", ".join(p.name for p in removed))
        return 0
    except BackupError as e:
        print(f"backup_db: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
