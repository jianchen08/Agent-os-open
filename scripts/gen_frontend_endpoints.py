#!/usr/bin/env python
"""前端插件端点投影生成器（plugin.json http_endpoints → endpoints.generated.ts）。

真值源唯一原则（ADR 2026-08-21 channel_api 退役：前端端点供给模型改生成式）：
- 插件端点 URL 的唯一真值源 = 各插件 plugin.json 的 http_endpoints 声明；
- 本脚本把该声明投影为 TypeScript 常量文件，前端 services/api 层一律
  import 生成常量，不手写任何 /ext/* 字面量；
- 生成物头部标注"勿手改"；漂移检查见
  scripts/check_frontend_endpoints_sync.py（CI 两道闸：重新生成 diff 空 +
  手写 /ext/ 字面量只减不增）。

跳过说明：channel_api 插件本身整体待退役（逐域拆除，最终整插件删除），
其 166 个端点**不生成**——它们不存在于终态，前端消费方随各批次
迁移/删除。

用法：
    python scripts/gen_frontend_endpoints.py            # 写默认输出文件
    python scripts/gen_frontend_endpoints.py --output X # 自定义输出（漂移闸用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
DEFAULT_OUTPUT = ROOT / "frontend" / "src" / "services" / "api" / "endpoints.generated.ts"

# channel_api 整体退役中（见 docs/working/channel_api插件拆迁方案_20260821.md），
# 其端点不投影进生成物；整插件删除后本名单自然失效。
SKIP_PLUGIN_IDS = {"channel_api"}

# 遍历时排除的目录（.venv 等重型目录不可进）
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_DIR_PREFIXES = "."


def iter_plugin_manifests() -> list[tuple[Path, dict]]:
    """返回 [(manifest_path, plugin_dict)]，按 manifest 路径排序保证确定性。"""
    manifests: list[tuple[Path, dict]] = []
    for root, dirs, files in os.walk(PLUGINS_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(EXCLUDE_DIR_PREFIXES)]
        if "plugin.json" in files:
            p = Path(root) / "plugin.json"
            parsed = json.loads(p.read_text(encoding="utf-8"))
            manifests.append((p, parsed))
    manifests.sort(key=lambda item: str(item[0]))
    return manifests


def ts_const_name(plugin_id: str) -> str:
    """插件 id → TS 常量名：大写 + 非字母数字转下划线。"""
    name = re.sub(r"[^A-Za-z0-9]+", "_", plugin_id).strip("_").upper()
    if not name:
        name = "PLUGIN"
    if name[0].isdigit():
        name = "_" + name
    return name


def build_export_block(plugin_id: str, plugin_name: str, endpoints: list[dict]) -> str:
    const_name = ts_const_name(plugin_id)
    lines = [
        "",
        f"  /** {plugin_id}（{plugin_name or '未命名'}）：plugin.json 声明 {len(endpoints)} 端点 */",
        f"  export const {const_name}_ENDPOINTS = {{",
    ]
    # 按 (path, route_id) 排序保证确定性输出
    for ep in sorted(endpoints, key=lambda e: (e.get("path", ""), e.get("route_id", ""))):
        path = ep.get("path", "")
        # 路径模板 {param} 原样保留（消费方自行替换），不做 TS 函数化
        lines.append(f"    '{ep.get('route_id', '')}': '{path}',")
    lines.append("  } as const")
    return "\n".join(lines)


def render_ts(plugin_groups: list[tuple[str, str, list[dict]]]) -> str:
    header = """/**
 * 生成物：插件 http_endpoints 声明的投影 —— 勿手改！
 *
 * 唯一真值源 = 各插件 plugin.json 的 http_endpoints 声明
 * （ADR 2026-08-21 channel_api 退役：前端端点供给模型改生成式）。
 * 路径模板 {param} 原样保留，消费方自行替换参数。
 * 改动插件 manifest 后执行再生成：
 *     python scripts/gen_frontend_endpoints.py
 * 漂移/手写回潮检查：
 *     python scripts/check_frontend_endpoints_sync.py
 */
/* eslint-disable */
"""
    blocks = [header]
    for plugin_id, plugin_name, endpoints in plugin_groups:
        blocks.append(build_export_block(plugin_id, plugin_name, endpoints))
    blocks.append("")
    return "\n".join(blocks)


def collect() -> list[tuple[str, str, list[dict]]]:
    """扫描全部 manifest，剔除待退役插件，返回按插件 id 排序的 [(id, name, endpoints)]。"""
    groups: dict[str, tuple[str, list[dict]]] = {}
    for path, manifest in iter_plugin_manifests():
        plugin_id = manifest.get("id")
        if not plugin_id:
            print(f"[gen-endpoints] ⚠️ {path}: 无 id 字段，跳过", file=sys.stderr)
            continue
        if plugin_id in SKIP_PLUGIN_IDS:
            print(f"[gen-endpoints] 跳过待退役插件 {plugin_id}（{path}）", file=sys.stderr)
            continue
        endpoints = manifest.get("http_endpoints")
        if not endpoints:
            continue
        groups[plugin_id] = (manifest.get("name") or "", list(endpoints))
    return [(plugin_id, name, eps) for plugin_id, (name, eps) in sorted(groups.items(), key=lambda kv: kv[0])]


def main() -> int:
    parser = argparse.ArgumentParser(description="插件 http_endpoints → endpoints.generated.ts")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    args = parser.parse_args()

    groups = collect()
    text = render_ts(groups)
    total = sum(len(eps) for _, _, eps in groups)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(
        f"[gen-endpoints] 已生成 {output}：{len(groups)} 个插件 / {total} 个端点"
        f"（channel_api 及其余 {len(SKIP_PLUGIN_IDS) - 1} 个待退役插件不生成）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
