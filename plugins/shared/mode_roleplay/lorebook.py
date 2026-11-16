"""世界书扫描注入（角色扮演模式包内物料面，SillyTavern World Info 语义子集）。

种子单元自包含：不 import 共享根模块，依赖仅标准库 + PyYAML；由 context_build
的模式物料面按真实导入名（mode_roleplay.lorebook）调用，不在本包内做 IO 编排。

条目字段（demo_world.yaml 为形态参照）：keys / secondary_keys / content /
enabled / insertion_order / constant / position / case_sensitive。

ST 字面扩展字段（批 1c，对齐 st-design-reference §5；导入归一/透传见
server.py _entry_from_st）：keysecondary + selectiveLogic（0=AND ANY 任一副
命中 / 1=NOT ALL ≥一副副键未命中 / 2=NOT ANY 全部副键未命中 / 3=AND ALL
全部副键命中。枚举编号与判定真值考据 SillyTavern release 分支
public/scripts/world-info.js 的 world_info_logic 与 checkWorldInfo 判定，
2026-10-11；st-design-reference §5.3 原表编号有误已随批 1c 纠偏修正）
/ disable（禁用） / order（注入排序，数字越大越后/越靠近对话） / probability +
useProbability（门开才按 percentage 掷骰） / caseSensitive / matchWholeWords。
字面键与旧存储键并存时字面键优先（导入双写同值，手写书单键）。

匹配契约：
- disable=True 过滤（字面禁用优先判定）；enabled=False 过滤（旧存储键）。
- constant=True 恒命中（不做关键词判断）。
- 扫描窗口 = texts[:scan_depth]（texts 为最近消息文本，scan_depth 为书级字段，
  默认 4）；窗口内任一文本含 keys 任一关键字的子串即主键命中，大小写按
  caseSensitive（字面，缺省回落 case_sensitive，默认不敏感）。
- keysecondary/selectiveLogic 非空副键按四态判定（语义见上，缺省 AND ANY）；
  主/副键可命中窗口内不同文本。
- probability + useProbability：门开才按 probability%（缺省 100）掷骰，作用于
  常驻与关键词命中；随机源为模块级 _random（测试注入固定 seed 实例做确定性
  重放，不 patch 全局 random）。
- matchWholeWords=True 词边界匹配（re \\b 语义）：键首尾为 ASCII 词字符才包
  \\b，CJK 键退化子串命中（\\b 对 CJK 词内位置无界）。
- 命中条目按注入序升序返回：order（字面，数字越大越后/越靠近对话，ST order
  语义）优先 → 回落 insertion_order（旧键，升序同向）→ 缺省 100；同序保持
  书内原序。

注入契约（render_injection）：
- 先按注入序稳定排序，形态 "## 世界设定\\n<content>\\n\\n<content>..."；按
  4 字符≈1 token 估算，表头与条目间 "\\n\\n" 分隔符计入预算；按序累加，首条
  超预算的条目起即停（低序/先到条目优先保留，不截断条目内容）；无内容可注入
  返回空串。

后置登记（本期不实现，RP 实现设计文档批 1c）：递归扫描（excludeRecursion 族）
/ Sticky/Cooldown/Delay / 向量检索 / Inclusion Group / position@depth（注入面
保持组装器 before/after 两注入口简化契约）。
"""
from __future__ import annotations

import random
import re

import yaml

DEFAULT_SCAN_DEPTH = 4
DEFAULT_TOKEN_BUDGET = 512
DEFAULT_INSERTION_ORDER = 100
DEFAULT_PROBABILITY = 100
CHARS_PER_TOKEN = 4
INJECTION_HEADER = "## 世界设定\n"
BLOCK_SEPARATOR = "\n\n"

# 模块级随机源（probability 掷骰）；测试注入固定 seed 实例（lorebook._random =
# random.Random(seed)）实现确定性重放。
_random = random.Random()

# ASCII 词字符：matchWholeWords 的 \b 词边界只对其有语义，CJK 键首尾不包
# \b（退化为子串命中）。
_ASCII_WORD_RE = re.compile(r"^[A-Za-z0-9_]$")


def load_book(path: str) -> dict:
    """读世界书 yaml 文件；损坏（解析失败或顶层非映射）抛带路径的 ValueError。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"世界书 yaml 损坏: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"世界书 yaml 顶层须为映射: {path}")
    return data


def _contains(needle: str, window: list[str], case_sensitive: bool, whole_words: bool = False) -> bool:
    """needle 是否命中窗口内任一文本。

    缺省子串路径（casefold 归一）；whole_words=True 走词边界：键首尾为 ASCII
    词字符才包 \\b（CJK 键退化子串），大小写不敏感经 re.IGNORECASE（近似
    casefold，非全 Unicode 等价）。
    """
    if whole_words:
        prefix = r"\b" if _ASCII_WORD_RE.match(needle[:1]) else ""
        suffix = r"\b" if _ASCII_WORD_RE.match(needle[-1:]) else ""
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(prefix + re.escape(needle) + suffix, flags)
        return any(pattern.search(text) is not None for text in window)
    if not case_sensitive:
        needle = needle.casefold()
        window = [text.casefold() for text in window]
    return any(needle in text for text in window)


def _case_sensitive(entry: dict) -> bool:
    """大小写敏感取值：字面 caseSensitive 存在即优先，回落旧键 case_sensitive。"""
    raw = entry["caseSensitive"] if "caseSensitive" in entry else entry.get("case_sensitive", False)
    return bool(raw)


def _entry_hit(entry: dict, window: list[str]) -> bool:
    """关键词命中判定：主键至少其一命中（先决）；副键非空时按 selectiveLogic
    四态判定副键集合（语义见模块 docstring，缺省 AND ANY）。"""
    case_sensitive = _case_sensitive(entry)
    whole_words = bool(entry.get("matchWholeWords", False))
    if not any(
        _contains(str(key), window, case_sensitive, whole_words) for key in entry.get("keys") or []
    ):
        return False
    secondary = entry.get("keysecondary") or entry.get("secondary_keys") or []
    if not secondary:
        return True
    results = [_contains(str(key), window, case_sensitive, whole_words) for key in secondary]
    logic = entry.get("selectiveLogic", 0)
    try:
        logic = int(logic)
    except (TypeError, ValueError):
        logic = 0
    if logic == 1:
        return not all(results)  # NOT ALL：≥1 副未命中
    if logic == 2:
        return not any(results)  # NOT ANY：全部副未命中
    if logic == 3:
        return all(results)  # AND ALL：全部副命中
    return any(results)  # 0/未知 = AND ANY


def _probability_pass(entry: dict) -> bool:
    """probability 门：useProbability=True 才按 probability%（缺省 100）掷骰；
    门未开恒通过；坏值（非数值）按缺省 100 处理。"""
    if not entry.get("useProbability", False):
        return True
    raw = entry.get("probability", DEFAULT_PROBABILITY)
    try:
        probability = float(raw)
    except (TypeError, ValueError):
        probability = float(DEFAULT_PROBABILITY)
    return _random.random() * 100.0 < probability


def _sort_key(entry: dict) -> float | int:
    """注入排序键：order（ST 字面，数字越大越后/越靠近对话）优先 → 回落
    insertion_order（旧键，升序同向）→ 缺省 100；非数值回落缺省。"""
    for field in ("order", "insertion_order"):
        value = entry.get(field)
        if isinstance(value, (int, float)):
            return value
    return DEFAULT_INSERTION_ORDER


def match_entries(book: dict, texts: list[str]) -> list[dict]:
    """扫描窗口内命中条目，按注入序升序返回（同序保持书内原序）；非 dict 行跳过。

    条目判定短路序：disable → enabled → constant/关键词命中 → probability 门
    （语义与后置登记项见模块 docstring）。
    """
    scan_depth = book.get("scan_depth")
    scan_depth = DEFAULT_SCAN_DEPTH if scan_depth is None else int(scan_depth)
    window = texts[: max(scan_depth, 0)]
    hits = [
        entry
        for entry in book.get("entries") or []
        if isinstance(entry, dict)
        and not entry.get("disable", False)
        and entry.get("enabled", True)
        and (entry.get("constant", False) or _entry_hit(entry, window))
        and _probability_pass(entry)
    ]
    return sorted(hits, key=_sort_key)


def render_injection(entries: list[dict], token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """命中条目拼接为注入块；先按注入序稳定排序；预算按 4 字符≈1 token 估算，
    超预算即停。"""
    budget_chars = max(int(token_budget), 0) * CHARS_PER_TOKEN
    total_chars = len(INJECTION_HEADER)
    blocks: list[str] = []
    for entry in sorted(entries, key=_sort_key):
        content = str(entry.get("content") or "")
        addition = len(content) + (len(BLOCK_SEPARATOR) if blocks else 0)
        if total_chars + addition > budget_chars:
            break
        blocks.append(content)
        total_chars += addition
    if not blocks:
        return ""
    return INJECTION_HEADER + BLOCK_SEPARATOR.join(blocks)


def scan_inject(book: dict, texts: list[str]) -> str:
    """match + render 组合便捷入口；token_budget 取书级字段（默认 512）。"""
    token_budget = book.get("token_budget")
    token_budget = DEFAULT_TOKEN_BUDGET if token_budget is None else int(token_budget)
    return render_injection(match_entries(book, texts), token_budget)
