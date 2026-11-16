#!/usr/bin/env python
"""render.card 卡型词汇表生成器——插件侧声明面 → 前端 TS 生成物（AR-M5 单源化）。

卡型词汇表的单一真值 = 插件侧声明面，两个产出面（本脚本全量扫描求并集）：

  1. plugins/shared/*/plugin.json 的 render.card 声明
     （capabilities.tools[].render.card，灵汐第一方工具面）；
  2. plugins/shared/dsh_adapter/translator.py 的 DSH_TOOLVIEW_COMPONENT_MAP
     卡型映射（DSH toolview → render card，经 contributes.renderers 面
     供前端 dshAdapter 装载；该表自述「前端组件翻译的单一事实源」）。

生成物（DO-NOT-EDIT，前端唯一词表来源）：

  - frontend/src/utils/renderCardVocabulary.generated.ts
    RENDER_CARD_VOCABULARY（声明面并集）+ DSH_RENDER_CARDS（第二面 DSH
    映射子集，按面投影——dshAdapter 贡献装载校验只认 DSH 面能产出的卡）。

消费形态：
  - frontend/src/utils/renderIntent.ts：
    RenderIntentCard = 生成物 RenderCardValue（类型面单源）；
    normalizeRenderIntent 声明校验用生成物 RENDER_CARD_VOCABULARY（运行时
    fail-closed：词表外卡型声明即弃，回落数据路由）。
  - frontend/src/services/dshAdapter/index.ts：contributes.renderers 贡献
    装载校验用生成物 DSH_RENDER_CARDS（原手抄 VALID_CARDS 已删，AR-M5
    遗留②清偿）。

执法：scripts/check_render_card_vocabulary_sync.py = 生成一致性闸（重渲染
与落盘文件逐字节比对；生成幂等 = 绿）。插件侧增删卡型后跑本脚本重写生成物
连同 commit；改声明忘再生成 → 闸红。单侧注入假卡型（词表外值）→ 未再生成
闸红；再生成则前端类型面扩张、tsc 在 payload 构造器缺位处红（fail-closed
双向，删值实验两向同构）。

扫描注意：只按 `plugins/shared/*/plugin.json` 定深 glob——禁递归 glob
（dsh_adapter 存在自嵌套 node_modules junction 会挂死，同
scripts/check_packaged_layout.py 口径）。

用法：
    python scripts/generate_render_card_vocabulary.py           # 写出生成物
    python scripts/generate_render_card_vocabulary.py --check   # 只校验，漂移退出 1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# ── 真源登记面（两个插件侧声明面；符号搬移时同刀改这里）───────────────
PLUGINS_DIR = ROOT / "plugins" / "shared"
DSH_TRANSLATOR_PY = ROOT / "plugins" / "shared" / "dsh_adapter" / "translator.py"
DSH_CARD_ATTRS = ("DSH_TOOLVIEW_COMPONENT_MAP", "_DSH_FALLBACK_MAPPING")

# ── 生成物登记面 ──────────────────────────────────────────────────────
FRONTEND_TS = ROOT / "frontend" / "src" / "utils" / "renderCardVocabulary.generated.ts"

_MARKER = "*** DO NOT EDIT — GENERATED FILE ***"

_CARD_RE = re.compile(r"^[a-z][a-z_]*$")

_HEADER = f"""\
/**
 * {_MARKER}
 *
 * render.card 卡型词汇表（审查总册 P1-20，AR-M5 单源化）。
 * 由 scripts/generate_render_card_vocabulary.py 扫描插件侧声明面生成：
 *   1. plugins/shared 下各插件 plugin.json 的 render.card 声明（第一方工具面）；
 *   2. plugins/shared/dsh_adapter/translator.py 的 DSH_TOOLVIEW_COMPONENT_MAP
 *      卡型映射（经 contributes.renderers 面供 dshAdapter 装载）。
 * 消费面：RENDER_CARD_VOCABULARY 供 renderIntent.ts（类型面 + 声明校验）；
 * DSH_RENDER_CARDS 供 dshAdapter/index.ts（贡献装载校验，translator 映射
 * 面子集）。
 * 手改必被 scripts/check_render_card_vocabulary_sync.py（重渲染逐字节一致
 * 闸）打回。增删卡型 = 改插件侧声明 → python
 * scripts/generate_render_card_vocabulary.py → 本生成物随动，连同 commit。
 */
"""

_BODY = """\

/** 卡型词汇表（插件侧两声明面并集，字典序；声明校验与类型面的单源） */
export const RENDER_CARD_VOCABULARY = [
{entries}
] as const

/** 卡型联合类型（renderIntent 的 RenderIntentCard 单源） */
export type RenderCardValue = (typeof RENDER_CARD_VOCABULARY)[number]

/** DSH 适配器面卡型子集（translator 卡型映射面，字典序；dshAdapter 贡献装载校验用） */
export const DSH_RENDER_CARDS = [
{dsh_entries}
] as const
"""


def _scan_plugin_json(plugins_dir: Path) -> set[str]:
    """扫描 plugins/shared/*/plugin.json 的 render.card 声明集合。

    递归走查 JSON，凡 dict 含 render.card 字符串值即收录（实测 0.2 平铺
    布局下全部落在 capabilities.tools[].render.card）。目录缺失/无一声明
    属扫描面破坏，fail-closed 抛错——绝不静默产出空词表。
    """
    cards: set[str] = set()
    manifests = sorted(plugins_dir.glob("*/plugin.json"))
    if not manifests:
        raise ValueError(f"插件清单扫描面为空: {plugins_dir}")
    for path in manifests:
        with open(path, encoding="utf-8") as f:
            data: Any = json.load(f)
        _walk_render_cards(data, cards)
    if not cards:
        raise ValueError(f"插件清单未声明任何 render.card: {plugins_dir}")
    return cards


def _walk_render_cards(node: Any, out: set[str]) -> None:
    """递归收录 render.card 字符串值（render 父键守卫，避免误收无关 card 键）。"""
    if isinstance(node, dict):
        render = node.get("render")
        if isinstance(render, dict):
            card = render.get("card")
            if isinstance(card, str):
                out.add(card)
        for value in node.values():
            _walk_render_cards(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_render_cards(item, out)


def _scan_dsh_translator(translator_py: Path) -> set[str]:
    """装载 dsh_adapter 翻译器，读 DSH 卡型映射的 card 值集合（contributes
    renderers 面的声明真源）。模块/属性缺失 fail-closed 抛错。"""
    if not translator_py.is_file():
        raise ValueError(f"DSH 翻译器缺失: {translator_py}")
    spec = importlib.util.spec_from_file_location("_dsh_translator_cards", translator_py)
    if spec is None or spec.loader is None:
        raise ValueError(f"DSH 翻译器不可装载: {translator_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cards: set[str] = set()
    for attr in DSH_CARD_ATTRS:
        mapping = getattr(module, attr, None)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError(f"DSH 翻译器卡型映射缺失或为空: {attr} @ {translator_py}")
        if isinstance(mapping.get("card"), str):
            # 扁平形态（_DSH_FALLBACK_MAPPING）：映射本身即一条映射，card 是键
            cards.add(mapping["card"])
            continue
        for entry in mapping.values():
            card = entry.get("card") if isinstance(entry, dict) else None
            if not isinstance(card, str) or not card:
                raise ValueError(f"DSH 卡型映射条目缺 card: {attr}")
            cards.add(card)
    return cards


def collect_cards(plugins_dir: Path = PLUGINS_DIR, translator_py: Path = DSH_TRANSLATOR_PY) -> list[str]:
    """两声明面求并集 → 字典序词表（生成确定性）。值合法性由渲染边界
    （render_ts）fail-closed 执法。"""
    return sorted(_scan_plugin_json(plugins_dir) | _scan_dsh_translator(translator_py))


def collect_dsh_cards(translator_py: Path = DSH_TRANSLATOR_PY) -> list[str]:
    """第二声明面（DSH 卡型映射，contributes.renderers 面真值）单独投影 →
    字典序子集词表。dshAdapter 贡献装载校验只认 DSH 面能产出的卡——
    全量词表里有而 DSH 面没有的卡（如 form 族）不得放行。"""
    return sorted(_scan_dsh_translator(translator_py))


def render_ts(cards: list[str], dsh_cards: list[str]) -> str:
    """按前端工程风格渲染生成物正文（纯函数；同词表同输出 = 生成确定性）。
    两个词表值校验 fail-closed：空表 / 非 `^[a-z][a-z_]*$` 值拒绝渲染（插件
    侧坏声明在生成期即红，绝不产出坏生成物放行进前端类型面）。"""
    if not cards or not dsh_cards:
        raise ValueError("词表为空，拒绝渲染（声明面扫描失效）")
    for card in [*cards, *dsh_cards]:
        if not _CARD_RE.match(card):
            raise ValueError(f"卡型值非法: {card!r}（须 ^[a-z][a-z_]*$）")
    entries = "\n".join(f"  '{c}'," for c in cards)
    dsh_entries = "\n".join(f"  '{c}'," for c in dsh_cards)
    return _HEADER + _BODY.format(entries=entries, dsh_entries=dsh_entries)


def write_outputs(
    frontend_ts: Path = FRONTEND_TS,
    plugins_dir: Path = PLUGINS_DIR,
    translator_py: Path = DSH_TRANSLATOR_PY,
) -> list[str]:
    """扫描插件侧声明面并写出生成物（LF 字节；幂等——同声明重复写出零 diff）。

    返回并集词表（供测试断言随动）。"""
    cards = collect_cards(plugins_dir, translator_py)
    frontend_ts.parent.mkdir(parents=True, exist_ok=True)
    with open(frontend_ts, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_ts(cards, collect_dsh_cards(translator_py)))
    return cards


def check_files(
    frontend_ts: Path = FRONTEND_TS,
    plugins_dir: Path = PLUGINS_DIR,
    translator_py: Path = DSH_TRANSLATOR_PY,
) -> int:
    """生成一致性检查：落盘文件与按当前插件侧声明重渲染逐字节比对（读侧
    按通用换行归一，CRLF 检出任无谓红）；漂移/缺失 → 1 并给出再生成指引
    （fail-closed）。"""
    cards = collect_cards(plugins_dir, translator_py)
    dsh_cards = collect_dsh_cards(translator_py)
    expected = render_ts(cards, dsh_cards)
    try:
        actual = frontend_ts.read_text(encoding="utf-8")
    except OSError:
        print(f"[render-card-sync] FAIL: 生成物缺失: {frontend_ts}")
        return 1
    if actual != expected:
        print("[render-card-sync] FAIL: 生成物与插件侧声明重渲染不一致")
        print(
            "[render-card-sync] 手改生成物 / 插件侧增删卡型未再生成均属漂移；"
            "跑 python scripts/generate_render_card_vocabulary.py 重写后连同 commit。"
        )
        return 1
    print(f"[render-card-sync] OK: 生成物 ≡ 插件侧声明重渲染 (cards={', '.join(cards)}; dsh={', '.join(dsh_cards)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render.card 卡型词汇表生成器（插件侧声明面 → 前端 TS 生成物）")
    parser.add_argument("--check", action="store_true", help="只校验不写盘；漂移退出 1（闸用）")
    args = parser.parse_args(argv)
    if args.check:
        return check_files()
    cards = write_outputs()
    dsh_cards = collect_dsh_cards()
    print(f"[render-card-sync] 已生成: {FRONTEND_TS}")
    print(f"[render-card-sync] 词表({len(cards)}): {', '.join(cards)}")
    print(f"[render-card-sync] DSH 面子集({len(dsh_cards)}): {', '.join(dsh_cards)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
