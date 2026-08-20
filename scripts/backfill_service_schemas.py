#!/usr/bin/env python3
"""服务契约自动补齐（Phase 1-C5）：服务是谁提供的就在谁的 plugin.json 补 schema。

原理：`capabilities.services[].name` 即提供方 sidecar 实际暴露的 MCP 工具名
（如 approval_service 的 `approval.create_choice`），SDK 的 `tools/list` 已含
`input_schema` + `output_schema`（见 plugins/sdk/.../server.py _on_list_tools）。
本脚本对每个声明 services 的插件 spawn 其 sidecar → `tools/list` → 把真实
schema 写回**该提供方自己的 plugin.json**。

裁决（2026-08-18 契约定型）：
- input_schema：必填，auto-backfill 自提供方真实工具 schema（写不出一律当场报错）；
- output_schema：提供方在代码里声明了才填（不伪造）；未声明则省略；
- 声明了 services 但提供方 sidecar 无同名工具 → **真实漂移**，报错退出
  （"声明了却不生效"，正是校验器要抓的问题，不允许静默吞掉）；
- spawn/initialize 失败 → 报错退出（不自动生成就不可能补，宁可失败不可假装）。

用法：
    python scripts/backfill_service_schemas.py            # 应用（写回 plugin.json）
    python scripts/backfill_service_schemas.py --dry-run  # 只报告不写
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARED = REPO / "plugins" / "shared"


async def fetch_tools(plugin_dir: Path, entry: str) -> tuple[list[dict], str | None]:
    """spawn 提供方 sidecar，经 MCP stdio 拉 tools/list，返回 (tools, error)。"""
    # 对齐内核 invoker 的 PYTHONPATH 注入：项目根 + SDK src（server.py 用
    # `from agentos_plugin_sdk ...` / `from src.*` 风格 import）。
    env = dict(os.environ)
    extra = [str(REPO), str(REPO / "plugins" / "sdk" / "src")]
    env["PYTHONPATH"] = os.pathsep.join(extra + [env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    cmd = entry.split()
    if not cmd:
        return [], "entry 为空，无法启动提供方"
    proc_argv = cmd
    proc_cwd = str(plugin_dir)
    try:
        from mcp import ClientSession, StdioServerParameters, stdio_client
    except ImportError as e:  # pragma: no cover
        return [], f"缺少 mcp 客户端依赖: {e}"

    async def _run() -> list[dict]:
        params = StdioServerParameters(command=proc_argv[0], args=proc_argv[1:], cwd=proc_cwd, env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.list_tools()
            tools = []
            for t in res.tools:
                tools.append(
                    {
                        "name": t.name,
                        "input_schema": getattr(t, "input_schema", None),
                        "output_schema": getattr(t, "output_schema", None),
                    }
                )
            return tools

    try:
        return await asyncio.wait_for(_run(), timeout=60), None
    except Exception as e:  # noqa: BLE001 一次性迁移工具，宽捕获便于报告全量
        return [], f"spawn/list_tools 失败: {e}"


def find_plugins() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(SHARED):
        dirnames[:] = [
            d for d in dirnames if d not in ("node_modules", ".venv", "__pycache__", "dsh_plugins", "runtime")
        ]
        if "plugin.json" in filenames:
            out.append(Path(dirpath) / "plugin.json")
    return sorted(out)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写回")
    args = ap.parse_args()

    backfilled, drifted, failed, skipped = [], [], [], []
    for pf in find_plugins():
        d = json.loads(pf.read_text(encoding="utf-8"))
        svc = d.get("capabilities", {}).get("services") or []
        if not svc:
            continue
        if d.get("host_type") != "sidecar":
            skipped.append((d.get("id"), "非 sidecar，跳过"))
            continue
        entry = d.get("entry", "")
        tools, err = await fetch_tools(pf.parent, entry)
        if err:
            failed.append((d.get("id"), err))
            continue
        by_name = {t["name"]: t for t in tools}
        new_svc, plugin_drift = [], []
        for s in svc:
            tool = by_name.get(s["name"])
            if tool is None:
                plugin_drift.append(s["name"])
                continue
            new_s = dict(s)
            # 提供方真实 input_schema 优先；已显式声明且与真实一致则不覆盖
            declared_input = s.get("input_schema")
            if declared_input and declared_input != tool["input_schema"] or declared_input is None:
                new_s["input_schema"] = tool["input_schema"]
            if not new_s.get("output_schema") and tool.get("output_schema") is not None:
                new_s["output_schema"] = tool["output_schema"]
            new_svc.append(new_s)
        if plugin_drift:
            drifted.append((d.get("id"), sorted(plugin_drift)))
            # 有漂移=声明不生效：不写回（保留原样交给人工裁决），报告为错
            continue
        if new_svc != svc:
            backfilled.append(d.get("id"))
            if not args.dry_run:
                d["capabilities"]["services"] = new_svc
                pf.write_text(
                    json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    print(f"=== 服务契约 auto-backfill 报表（{'dry-run' if args.dry_run else '已写回'}）===")
    print(f"已补齐插件: {len(backfilled)}  {backfilled}")
    print(f"漂移（声明无对应提供方工具，拒绝写回）: {len(drifted)}")
    for pid, names in drifted:
        print(f"  !! {pid}: {names}")
    print(f"启动失败（无法自动生成）: {len(failed)}")
    for pid, e in failed:
        print(f"  !! {pid}: {e}")
    print(f"跳过: {len(skipped)}  {[s[0] for s in skipped]}")
    # 校验器不得空转：漂移/失败任一存在，脚本即以失败退出
    if drifted or failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
