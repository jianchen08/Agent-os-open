#!/usr/bin/env python
"""前端端点供给一致性门禁（ADR 2026-08-21：真值源 = plugin.json，杜绝手写回潮）。

两道检查：
  a) 漂移闸：重新运行 gen_frontend_endpoints.py 生成到临时文件，与仓库内
     frontend/src/services/api/endpoints.generated.ts 逐字节 diff——
     diff 非空 → exit 1（生成物与插件 manifests 漂移，改 manifest 后必须
     重新生成并提交生成物）。
  b) 手写棘轮闸：统计 frontend/src 下 /ext/ 字面量出现次数（排除生成物
     自身与 *.test.* 测试文件），与脚本内嵌 baseline 比较，只减不增——
     增多 → exit 1（手写回潮）；减少 → 通过但提示更新 baseline（走 commit
     留归因，随 channel_api 批次 1-5 逐域清零）。

用法：
    python scripts/check_frontend_endpoints_sync.py
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN_SCRIPT = ROOT / "scripts" / "gen_frontend_endpoints.py"
GENERATED_FILE = ROOT / "frontend" / "src" / "services" / "api" / "endpoints.generated.ts"
FRONTEND_SRC = ROOT / "frontend" / "src"

# ── 手写棘轮闸基线 ─────────────────────────────────────────────────
# 口径：grep -rno "/ext/" frontend/src --include=*.ts --include=*.tsx
#       排除 endpoints.generated.ts 与 *.test.*（测试断言随所属域批次同步改，
#       不计入本闸；批次 5 收尾闸再锁零命中）。
# 实测 2026-08-21（channel_api 退役收尾后）：
# 9 处 = 3 处合法运行时动态拼接（WebviewWidget /ext/${pluginId}、
#   pluginStyles /ext/{pluginId} 声明驱动消费——真值源不在前端）
#   + 6 处注释/声明示例（WebviewWidget 白名单注释、ContributionRegistry
#   CSS 拼接规则、workspaces 模板注释、WizardWidget 声明示例）。
HANDWRITTEN_EXT_BASELINE = 9

TEST_FILE_RE = re.compile(r"\.(test|spec)\.")
TS_FILE_SUFFIXES = (".ts", ".tsx")


def count_handwritten_ext(frontend_src: Path) -> tuple[int, list[str]]:
    """统计 /ext/ 字面量出现次数（含每文件的命中数明细，便于排查回潮来源）。"""
    total = 0
    per_file: list[str] = []
    for path in sorted(frontend_src.rglob("*")):
        if not path.is_file() or not path.name.endswith(TS_FILE_SUFFIXES):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == GENERATED_FILE.relative_to(ROOT).as_posix():
            continue
        if TEST_FILE_RE.search(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        n = text.count("/ext/")
        if n:
            total += n
            per_file.append(f"    {rel}: {n}")
    return total, per_file


def drift_check() -> int:
    """重新生成并与仓库内生成物对比；diff 非空返回 1。"""
    fd, tmp_path = tempfile.mkstemp(suffix=".generated.ts")
    try:
        proc = subprocess.run(
            [sys.executable, str(GEN_SCRIPT), "--output", tmp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            print(f"[endpoints-sync] ❌ 生成器运行失败：{proc.stderr.strip()}", file=sys.stderr)
            return 1
        generated = Path(tmp_path)
        if not GENERATED_FILE.exists():
            print(
                f"[endpoints-sync] ❌ 生成物缺失：{GENERATED_FILE}（首次生成请运行生成器）",
                file=sys.stderr,
            )
            return 1
        current = GENERATED_FILE.read_text(encoding="utf-8")
        fresh = generated.read_text(encoding="utf-8")
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    if current == fresh:
        print("[endpoints-sync] ✅ 漂移闸：重新生成与仓库内生成物一致")
        return 0
    print("[endpoints-sync] ❌ 漂移闸：生成物与插件 manifests 不一致——", file=sys.stderr)
    print(
        "    改过 plugin.json http_endpoints 后必须运行"
        " python scripts/gen_frontend_endpoints.py 并提交生成物。"
        " diff 预览（+ 为新生成）：",
        file=sys.stderr,
    )
    for line in difflib.unified_diff(
        current.splitlines(), fresh.splitlines(), "repo", "regenerated", lineterm=""
    ):
        print(f"    {line}", file=sys.stderr)
    return 1


def ratchet_check() -> int:
    count, per_file = count_handwritten_ext(FRONTEND_SRC)
    print(f"[endpoints-sync] 手写 /ext/ 字面量实测 {count} 处（baseline {HANDWRITTEN_EXT_BASELINE}）")
    if count > HANDWRITTEN_EXT_BASELINE:
        print(
            f"[endpoints-sync] ❌ 手写棘轮闸：{count} > baseline {HANDWRITTEN_EXT_BASELINE}"
            "——/ext/ 手写字面量回潮（新代码须 import 生成物 endpoints.generated.ts），"
            "明细：",
            file=sys.stderr,
        )
        for line in per_file:
            print(line, file=sys.stderr)
        return 1
    if count < HANDWRITTEN_EXT_BASELINE:
        print(
            f"[endpoints-sync] ⚠️ 手写引用减少 {HANDWRITTEN_EXT_BASELINE - count} 处"
            f"（当前通过）——请把 scripts/check_frontend_endpoints_sync.py 的"
            " HANDWRITTEN_EXT_BASELINE 下调到实测值并随删除 commit 一起提交（归因）。"
        )
    else:
        print("[endpoints-sync] ✅ 手写棘轮闸：等于 baseline，无回潮")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="前端端点供给一致性门禁（漂移闸 + 手写棘轮闸）")
    parser.add_argument(
        "--skip-drift",
        action="store_true",
        help="只跑手写棘轮闸（跳过重新生成对比；一般不用，CI 与本地跑全量）",
    )
    args = parser.parse_args()

    rc = 0
    if not args.skip_drift:
        rc = drift_check()
    rc = ratchet_check() or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())