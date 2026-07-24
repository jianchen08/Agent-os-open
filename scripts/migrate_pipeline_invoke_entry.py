#!/usr/bin/env python3
"""P6 命名治理（ADR 附录 D④）：批量迁移 pipeline 插件 manifest。

把 pipeline 插件的 MCP 入口名从 capabilities.tools[0].name 挪到顶层 invoke_entry，
并清空 capabilities.tools（这些不是给 LLM 的工具，是管道自调用入口）。

不变：
  - system 类型 tools 双语义未评估（D.6），保留不动。
  - tool 类型 tools 是真工具，不动。
  - sidecar 的 server.py @plugin.tool 声明不动（MCP 传输层，ADR D.3）。

用法：
  python migrate_pipeline_invoke_entry.py --root <plugins/shared> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def migrate_manifest(path: Path, dry_run: bool) -> tuple[str, str | None]:
    """迁移单个 manifest。返回 (status, detail)。

    status: migrated | skipped | error
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return ("error", f"parse failed: {e}")

    if data.get("plugin_type") != "pipeline":
        return ("skipped", f"plugin_type={data.get('plugin_type')} (not pipeline)")

    # 已经迁过（有 invoke_entry 且 tools 空）→ 跳过（幂等）
    if data.get("invoke_entry") and not data.get("capabilities", {}).get("tools"):
        return ("skipped", "already migrated (has invoke_entry, empty tools)")

    tools = data.get("capabilities", {}).get("tools", [])
    if not tools:
        return ("error", "pipeline plugin has no capabilities.tools[0] to migrate")

    entry_name = tools[0].get("name")
    if not entry_name:
        return ("error", "capabilities.tools[0].name is empty")

    if dry_run:
        return ("migrated(dry)", f"{data.get('id')}: tools[0].name={entry_name!r} -> invoke_entry")

    # 迁移：清空 tools，加 invoke_entry（紧跟 entry 字段后，保持可读顺序）
    data["capabilities"]["tools"] = []
    data["invoke_entry"] = entry_name

    # 重排键：把 invoke_entry 放到 entry 后面（与 ADR 示例一致），其余保持原序
    # Python 3.7+ dict 保序；重建一个新 dict
    ordered: dict = {}
    invoke_inserted = False
    for k, v in data.items():
        ordered[k] = v
        if k == "entry" and "invoke_entry" in data and not invoke_inserted:
            ordered["invoke_entry"] = data["invoke_entry"]
            invoke_inserted = True
    # 若没有 entry 字段（理论不会），兜底追加
    if not invoke_inserted:
        ordered["invoke_entry"] = data["invoke_entry"]

    path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    return ("migrated", f"{data.get('id')}: invoke_entry={entry_name!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="plugins/shared root dir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: root not a dir: {root}", file=sys.stderr)
        return 2

    files = sorted(root.rglob("plugin.json"))
    counts = {"migrated": 0, "migrated(dry)": 0, "skipped": 0, "error": 0}
    for f in files:
        status, detail = migrate_manifest(f, args.dry_run)
        counts[status] = counts.get(status, 0) + 1
        print(f"[{status}] {f.relative_to(root)}: {detail}")

    print("\n=== Summary ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0 if counts.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
