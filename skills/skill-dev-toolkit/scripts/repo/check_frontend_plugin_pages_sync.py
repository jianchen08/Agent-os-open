#!/usr/bin/env python
"""前端插件页面声明一致性门禁（审查总册 E6：真值源 = plugin.json contributes.pages）。

漂移闸：重新运行 gen_frontend_plugin_pages.py 生成到临时文件，与仓库内
frontend/src/services/schema/pluginPages.generated.ts 逐字节 diff——
diff 非空 → exit 1（生成物与插件页面声明漂移；改 contributes.pages 后必须
重新生成并提交生成物）。断链场景由此在构建期报错：壳层引用的声明键随
plugin.json 改名后，过期生成物过不了本闸；已过闸生成物中的过期引用由
tsc 按 DeclaredPluginPageRef 判别联合报错。

用法：
    python scripts/check_frontend_plugin_pages_sync.py
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN_SCRIPT = ROOT / "scripts" / "gen_frontend_plugin_pages.py"
GENERATED_FILE = ROOT / "frontend" / "src" / "services" / "schema" / "pluginPages.generated.ts"


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
            print(f"[plugin-pages-sync] ❌ 生成器运行失败：{proc.stderr.strip()}", file=sys.stderr)
            return 1
        generated = Path(tmp_path)
        if not GENERATED_FILE.exists():
            print(
                f"[plugin-pages-sync] ❌ 生成物缺失：{GENERATED_FILE}（首次生成请运行生成器）",
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
        print("[plugin-pages-sync] ✅ 漂移闸：重新生成与仓库内生成物一致")
        return 0
    print("[plugin-pages-sync] ❌ 漂移闸：生成物与插件页面声明不一致——", file=sys.stderr)
    print(
        "    改过 plugin.json contributes.pages 后必须运行"
        " python scripts/gen_frontend_plugin_pages.py 并提交生成物。"
        " diff 预览（+ 为新生成）：",
        file=sys.stderr,
    )
    for line in difflib.unified_diff(current.splitlines(), fresh.splitlines(), "repo", "regenerated", lineterm=""):
        print(f"    {line}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="前端插件页面声明一致性门禁（漂移闸）")
    parser.parse_args()
    return drift_check()


if __name__ == "__main__":
    sys.exit(main())
