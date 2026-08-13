"""一次性数据迁移：把全局共享的 ``data/{memory,multimodal,tasks,...}`` 迁入 ``data/default/``。

F-TENANT-B（方案 B 目录隔离）上线迁移：
多租户数据根咽喉点 ``tenant_data_root`` 的默认路径是 ``data/{tenant_id}/xxx``，
default 租户读 ``data/default/xxx``。0.2 迁移前落盘的存量数据在 ``data/`` 平铺
（直接子目录/散落文件）→ 不迁移则 default 租户读不到存量。

迁移规则（幂等，可重复运行，与 tenant_data.migrate_legacy_data_to_default 一致）：
  - ``data/default/`` 已存在 → 视为已迁移，直接跳过（不移动任何项）；
  - 否则创建 ``data/default/``，把 ``data/`` 下除 ``default`` 外的所有直接子项
    （目录与文件）移入 ``data/default/``。

安全性：仅文件系统移动（同卷 rename），不删除/不覆盖。运行前建议备份 data/。

用法:
  python scripts/migrate_legacy_data_to_default.py             # dry-run（只打印将迁移项）
  python scripts/migrate_legacy_data_to_default.py --apply     # 实际迁移
  python scripts/migrate_legacy_data_to_default.py --data-dir <path>   # 指定数据根（测试/部署）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 仓库根（scripts/ 上一级），供导入 plugins/shared/tenant_data.py
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared"
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))

from tenant_data import migrate_legacy_data_to_default  # noqa: E402


def main() -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        description="把 data/ 平铺存量数据幂等迁入 data/default/（方案 B 多租户上线迁移）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际执行迁移（缺省 dry-run：仅打印将迁移项，不移动任何文件）",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="数据根目录（缺省为仓库 data/，或 env AGENTOS_DATA_DIR）",
    )
    args = parser.parse_args()

    data_root = Path(args.data_dir) if args.data_dir else None
    if data_root is None:
        from tenant_data import _default_data_base

        data_root = _default_data_base()
    data_root = data_root.resolve()

    print(f"[migrate_legacy_data_to_default] 数据根: {data_root}")
    if (data_root / "default").exists():
        print("[migrate_legacy_data_to_default] data/default/ 已存在，跳过（幂等）")
        return 0
    if not data_root.exists():
        print(f"[migrate_legacy_data_to_default] 数据根不存在: {data_root}，无操作")
        return 0

    legacy_items = sorted(
        p.name for p in data_root.iterdir() if p.name != "default"
    )
    if not legacy_items:
        print("[migrate_legacy_data_to_default] data/ 为空，无需迁移")
        return 0

    print(f"[migrate_legacy_data_to_default] 将迁移 {len(legacy_items)} 项:")
    for name in legacy_items:
        print(f"  - {name}")

    if not args.apply:
        print("\n[dry-run] 未执行移动。确认无误后加 --apply 实际迁移。")
        return 0

    moved = migrate_legacy_data_to_default(data_root=data_root)
    print(f"[migrate_legacy_data_to_default] 已迁移 {len(moved)} 项 → {data_root / 'default'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
