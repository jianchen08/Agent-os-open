#!/usr/bin/env python3
"""内核库直连机械闸（审查总册 20261014 C2 建闸，2026-10-15 刀3 收紧终态口径）。

插件代码禁止自开 SQLite 连接读内核库——内核内部表结构被写死进插件、存储
driver 切换即静默断、租户谓词靠自觉、裸连接与内核写事务抢锁。唯一合法入口
= 内核 db-read 契约服务（HTTP ``POST /api/v1/db/read`` 或 capability 面
``db-read.query``，插件侧桥在 SDK ``agentos_plugin_sdk/db_read.py``）：执法在内核
Rust read_faces，DB 句柄只存在于内核进程，服务边界外只出行数据。

口径（刀3 终态）：
- **内核库直连零**：plugins/shared 生产码出现 ``sqlite3.connect`` 即欠账；
- **自有库显式豁免名单**（EXEMPT_FILES，带归因）：插件自有数据面/测试替身
  对自有 sqlite 文件的连接不属内核库直读，入名单显式登记；新增豁免须改
  此处并注明归因；
- 测试文件豁免（tests/ 目录与 test_*.py / *_test.py 不属生产码）；
- 基线（.github/check_kernel_db_direct_reads_baseline.txt）只容**在途欠账**
  （并行轮改动面，迁移后清偿归零），棘轮只减不增；基线外新增 → exit 1。

用法：python scripts/check_kernel_db_direct_reads.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_ROOTS = [
    ROOT / "plugins" / "shared",
]
SCAN_SUFFIXES = {".py"}
# 与 check_config_direct_reads 同一套目录排除口径（剪枝式遍历，不进巨目录）
EXCLUDE_DIRS = {
    "__pycache__",
    "node_modules",
    "runtime",
    "target",
    ".venv",
    "venv",
    ".venv-hindsight",
    ".ai_workspaces",
    # 测试不属生产码：tests/ 目录整树豁免；平铺测试件按文件名判（下方）
    "tests",
}
# 自有库显式豁免名单（非内核库直读；新增豁免须在此登记并注明归因）：
# - trace_ingestion/trace_ingest_store.py：E 事件汇聚云端存储自建
#   trace_events/trace_dead_letters 库（插件自有数据面，非内核库直读），
#   独立治理待该插件触碰批次裁定；
# - _libs/testing/db_read_fake.py：db-read 契约测试替身（plugins/shared/_libs
#   共享测试基建），替身对测试自有 sqlite 文件执行真实 SQL 仿真契约收窄；
#   收窄执法本体由内核 Rust 契约测试承担（read_faces/tests.rs）。
# - sync_relay/exec_token_store.py：执行凭据分库（ADR D13' 双钥分离——exec token
#   与 relay.db 分库，单钥泄露不连带），插件自有数据面非内核库直读。
EXEMPT_FILES = {
    "plugins/shared/trace_ingestion/trace_ingest_store.py",
    "plugins/shared/_libs/testing/db_read_fake.py",
    "plugins/shared/sync_relay/exec_token_store.py",
}

CONNECT_CALL = re.compile(r"\bsqlite3\s*\.\s*connect\s*\(")


def is_test_file(path: Path) -> bool:
    """平铺布局测试件：test_*.py / *_test.py。"""
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def iter_sources(shared_root: Path) -> list[Path]:
    out: list[Path] = []
    if not shared_root.is_dir():
        return out
    # 剪枝式遍历：排除目录整体不下钻（node_modules/runtime 体量巨大）
    stack = [shared_root]
    while stack:
        cur = stack.pop()
        # 扫描期间活进程（MCP 管理器等）可能在共享根建/删插件目录——entry 在
        # 父列表里、scandir 时目标已消失（或 dangling junction）→ 跳过该 entry
        # 而非整闸崩（2026-10-30 打包链实锤：mcp_windows_mcp 创建中竞态）。
        try:
            entries = list(cur.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for p in entries:
            if p.is_dir():
                if p.name in EXCLUDE_DIRS:
                    continue
                stack.append(p)
            elif p.suffix in SCAN_SUFFIXES and not is_test_file(p):
                out.append(p)
    return out


def count_violations(text: str) -> int:
    return len(CONNECT_CALL.findall(text))


def scan(shared_root: Path, key_root: Path) -> dict[str, int]:
    """扫共享根生产码，返回 key_root 相对 posix 路径 → 命中处数（>0）。

    main 以仓库根为 key_root（基线键 = 仓库相对路径，与 config 闸同口径）；
    自测以临时树为两者（命中样例红/干净样例绿不触真仓）。
    """
    found: Counter[str] = Counter()
    for p in iter_sources(shared_root):
        rel = str(p.relative_to(key_root)).replace("\\", "/")
        if rel in EXEMPT_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = count_violations(text)
        if n:
            found[rel] = n
    return dict(found)


def load_baseline(baseline_path: Path) -> dict[str, int]:
    baseline: dict[str, int] = {}
    if not baseline_path.exists():
        return baseline
    for raw in baseline_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, _, cnt = line.rpartition(":")
        # fail-closed：基线键必须是仓库相对 posix 路径（同 config 闸口径——
        # 机器绝对路径键与相对 found 键永不命中 → 恒红且无人发现）。格式错直接拒判。
        if (len(path) > 2 and path[1] == ":") or path.startswith("/") or path.startswith("\\"):
            raise ValueError(f"基线含非相对路径键（历史缺陷形态）：{path!r}；请以 --init 重建基线")
        baseline[path] = int(cnt)
    return baseline


def main() -> int:
    baseline_path = ROOT / ".github" / "check_kernel_db_direct_reads_baseline.txt"
    try:
        baseline = load_baseline(baseline_path)
    except ValueError as exc:
        print(f"内核库直连机械闸：{exc}")
        return 2
    found = scan(ROOT / "plugins" / "shared", ROOT)

    new_violations = {path: cnt for path, cnt in found.items() if baseline.get(path, 0) < cnt}
    if new_violations:
        print(
            "内核库直连机械闸：发现基线外新增直连（内核库直连零；读内核库走"
            " db-read 契约服务，自有库直连须入 EXEMPT_FILES 豁免名单并注明归因）："
        )
        for path in sorted(new_violations):
            print(f"  {path}: {found[path]} 处（基线 {baseline.get(path, 0)}）")
        print(
            "修复 = 改走 db-read 契约服务（HTTP POST /api/v1/db/read 或 "
            "capability 面，桥在 SDK agentos_plugin_sdk/db_read.py）。"
        )
        return 1

    # 基线收缩提示（只减不增棘轮）
    shrinkable = [p for p, c in baseline.items() if found.get(p, 0) < c]
    if shrinkable:
        print("提示：以下基线条目可收缩（棘轮只减不增）：")
        for path in sorted(shrinkable):
            print(f"  {path}: 基线 {baseline[path]} → 实测 {found.get(path, 0)}")
    print(
        f"内核库直连机械闸通过（扫描 {len(iter_sources(ROOT / 'plugins' / 'shared'))} 文件，"
        f"自有库豁免 {len(EXEMPT_FILES)} 件，"
        f"在途欠账 {sum(found.values())} 处在基线内）。"
    )
    return 0


def init_baseline() -> int:
    """以当前实测重建基线（仓库相对 posix 键；--init 须单独 commit 留归因）。"""
    found = scan(ROOT / "plugins" / "shared", ROOT)
    lines = [
        "# 内核库直连在途欠账基线（棘轮只减不增）；格式 文件相对路径: 处数",
        "# 口径（2026-10-15 刀3 终态）：内核库直连零 + 自有库显式豁免名单",
        "# （名单在检查器 EXEMPT_FILES 带归因）；基线只容在途欠账，新增直连即红。",
        "# 键必须是仓库相对 posix 路径（绝对路径键属历史缺陷形态，检查器拒判）",
        *sorted(f"{path}: {cnt}" for path, cnt in found.items()),
    ]
    baseline_path = ROOT / ".github" / "check_kernel_db_direct_reads_baseline.txt"
    baseline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"内核库直连机械闸：基线已重建（{len(found)} 文件 / {sum(found.values())} 处）。")
    return 0


if __name__ == "__main__":
    if "--init" in sys.argv:
        sys.exit(init_baseline())
    sys.exit(main())
