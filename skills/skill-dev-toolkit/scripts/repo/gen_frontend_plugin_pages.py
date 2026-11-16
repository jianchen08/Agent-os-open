#!/usr/bin/env python
"""前端插件页面声明投影生成器（plugin.json contributes.pages → pluginPages.generated.ts）。

壳层导航软绑定声明面化（审查总册 E6）：
- 壳层导航引用的唯一真值源 = 各插件 plugin.json 的 contributes.pages 声明；
- 本脚本把该声明投影为 TypeScript 常量（PLUGIN_DECLARED_PAGES 嵌套键 +
  DeclaredPluginPageRef 判别联合），壳层只引用声明键——引用未声明页 = tsc
  构建期红（断链场景构建期报错）；
- 运行时导航解析不读本文件：走 contributionRegistry 活注册表
  （/api/v1/schema 启动装载 + 刷新即随动），本生成物只承载壳层可引用的声明键；
- 漂移检查见 scripts/check_frontend_plugin_pages_sync.py（重渲染逐字节一致闸）。

用法：
    python scripts/gen_frontend_plugin_pages.py            # 写默认输出文件
    python scripts/gen_frontend_plugin_pages.py --output X # 自定义输出（漂移闸用）
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
DEFAULT_OUTPUT = ROOT / "frontend" / "src" / "services" / "schema" / "pluginPages.generated.ts"

# 遍历时排除的目录（.venv 等重型目录不可进）
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_DIR_PREFIXES = "."

# 合法 JS 标识符（非标识符的键加引号）
IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


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


def ts_key(name: str) -> str:
    """键名：合法标识符原样，否则加引号。"""
    return name if IDENTIFIER_RE.match(name) else f"'{name}'"


def collect() -> list[tuple[str, list[dict]]]:
    """扫描全部 manifest，返回按插件 id 排序的 [(plugin_id, pages)]。

    同插件内重复 page id 视为声明缺陷，直接报错（静默去重会让后声明页
    无声消失，与注册表 fail-closed 口径相反）。
    """
    groups: dict[str, list[dict]] = {}
    for path, manifest in iter_plugin_manifests():
        plugin_id = manifest.get("id")
        if not plugin_id:
            print(f"[gen-plugin-pages] ⚠️ {path}: 无 id 字段，跳过", file=sys.stderr)
            continue
        pages = (manifest.get("contributes") or {}).get("pages") or []
        if not pages:
            continue
        seen: set[str] = set()
        for page in pages:
            page_id = page.get("id")
            if not page_id or not isinstance(page_id, str):
                print(f"[gen-plugin-pages] ⚠️ {path}: contributes.pages 条目缺 id，跳过", file=sys.stderr)
                continue
            if page_id in seen:
                raise SystemExit(f"[gen-plugin-pages] ✗ {path}: 插件 {plugin_id} 页面 id 重复声明: {page_id}")
            seen.add(page_id)
            groups.setdefault(plugin_id, []).append(page)
    return sorted(groups.items(), key=lambda kv: kv[0])


def render_page_entry(plugin_id: str, page: dict) -> str:
    page_id = page["id"]
    path = page.get("path")
    path_part = f", path: '{path}'" if isinstance(path, str) and path else ""
    return f"    {ts_key(page_id)}: {{ pluginId: '{plugin_id}', pageId: '{page_id}'{path_part} }},"


def render_ts(groups: list[tuple[str, list[dict]]]) -> str:
    header = """/**
 * 生成物：插件 contributes.pages 声明的投影 —— 勿手改！
 *
 * 唯一真值源 = 各插件 plugin.json 的 contributes.pages 声明
 * （审查总册 E6：壳层导航软绑定声明面化）。
 * 壳层导航只引用本文件的声明键（PLUGIN_DECLARED_PAGES[pluginId][pageId]），
 * 引用未声明页 = tsc 构建期红；运行时解析走 contributionRegistry 活注册表
 * （schema 刷新即随动），本生成物不承载运行时数据。
 * 改动插件页面声明后执行再生成：
 *     python scripts/gen_frontend_plugin_pages.py
 * 漂移检查（断链场景构建期报错）：
 *     python scripts/check_frontend_plugin_pages_sync.py
 */
/* eslint-disable */

/**
 * 壳层可引用的声明页键（嵌套 pluginId → pageId，与注册表 pluginId+id
 * 去重键同构）。path 为声明直连路由（文档面；运行时以注册表活值为准）。
 */
export const PLUGIN_DECLARED_PAGES = {"""
    blocks = [header]
    for plugin_id, pages in groups:
        entries = "\n".join(render_page_entry(plugin_id, page) for page in pages)
        blocks.append(
            "\n".join(
                [
                    f"  {ts_key(plugin_id)}: {{",
                    entries,
                    "  },",
                ]
            )
        )
    blocks.append(
        "} as const\n"
        "\n"
        "/**\n"
        " * 已声明页面引用（判别联合）：pluginId+pageId 须均为声明成员，否则 tsc 红。\n"
        " * 壳层告警动作/菜单项等引用面统一收窄到本类型。\n"
        " */\n"
        "export type DeclaredPluginPageRef = {\n"
        "  [K in keyof typeof PLUGIN_DECLARED_PAGES & string]: {\n"
        "    [P in keyof (typeof PLUGIN_DECLARED_PAGES)[K] & string]: { pluginId: K; pageId: P }\n"
        "  }[keyof (typeof PLUGIN_DECLARED_PAGES)[K] & string]\n"
        "}[keyof typeof PLUGIN_DECLARED_PAGES & string]\n"
    )
    blocks.append("")
    return "\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser(description="插件 contributes.pages → pluginPages.generated.ts")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    args = parser.parse_args()

    groups = collect()
    text = render_ts(groups)
    total = sum(len(pages) for _, pages in groups)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"[gen-plugin-pages] 已生成 {output}：{len(groups)} 个插件 / {total} 个声明页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
