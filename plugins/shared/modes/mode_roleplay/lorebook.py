"""世界书扫描注入（角色扮演模式包内物料面，SillyTavern World Info 语义子集）。

种子单元自包含：不 import 共享根模块，依赖仅标准库 + PyYAML；由 context_build
的模式物料面按真实导入名（mode_roleplay.lorebook）调用，不在本包内做 IO 编排。

条目字段（demo_world.yaml 为形态参照）：keys / secondary_keys / content /
enabled / insertion_order / constant / position / case_sensitive。

匹配契约：
- enabled=False 的条目跳过；constant=True 恒命中（不做关键词判断）。
- 扫描窗口 = texts[:scan_depth]（texts 为最近消息文本，scan_depth 为书级字段，
  默认 4）；窗口内任一文本含 keys 任一关键字的子串即主键命中，大小写按
  case_sensitive（默认不敏感）。
- secondary_keys 非空时须主键命中且副键至少一个也命中（主/副键可命中窗口内
  不同文本）。
- 命中条目按 insertion_order 升序返回（同序保持书内原序）。

注入契约（render_injection）：
- 形态 "## 世界设定\\n<content>\\n\\n<content>..."；按 4 字符≈1 token 估算，
  表头与条目间 "\\n\\n" 分隔符计入预算；按序累加，首条超预算的条目起即停
  （低序/先到条目优先保留，不截断条目内容）；无内容可注入返回空串。
"""
from __future__ import annotations

import yaml

DEFAULT_SCAN_DEPTH = 4
DEFAULT_TOKEN_BUDGET = 512
DEFAULT_INSERTION_ORDER = 100
CHARS_PER_TOKEN = 4
INJECTION_HEADER = "## 世界设定\n"
BLOCK_SEPARATOR = "\n\n"


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


def _contains(needle: str, window: list[str], case_sensitive: bool) -> bool:
    if not case_sensitive:
        needle = needle.casefold()
    return any(needle in text for text in window)


def _entry_hit(entry: dict, window: list[str]) -> bool:
    """关键词命中判定：主键至少其一命中；副键非空时副键至少其一也要命中。"""
    case_sensitive = bool(entry.get("case_sensitive", False))
    if not case_sensitive:
        window = [text.casefold() for text in window]
    if not any(_contains(str(key), window, case_sensitive) for key in entry.get("keys") or []):
        return False
    secondary = entry.get("secondary_keys") or []
    if not secondary:
        return True
    return any(_contains(str(key), window, case_sensitive) for key in secondary)


def match_entries(book: dict, texts: list[str]) -> list[dict]:
    """扫描窗口内命中条目，按 insertion_order 升序返回；非 dict 行跳过。"""
    scan_depth = book.get("scan_depth")
    scan_depth = DEFAULT_SCAN_DEPTH if scan_depth is None else int(scan_depth)
    window = texts[: max(scan_depth, 0)]
    hits = [
        entry
        for entry in book.get("entries") or []
        if isinstance(entry, dict)
        and entry.get("enabled", True)
        and (entry.get("constant", False) or _entry_hit(entry, window))
    ]
    return sorted(
        hits,
        key=lambda entry: (
            entry["insertion_order"]
            if entry.get("insertion_order") is not None
            else DEFAULT_INSERTION_ORDER
        ),
    )


def render_injection(entries: list[dict], token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """命中条目拼接为注入块；预算按 4 字符≈1 token 估算，超预算即停。"""
    budget_chars = max(int(token_budget), 0) * CHARS_PER_TOKEN
    total_chars = len(INJECTION_HEADER)
    blocks: list[str] = []
    for entry in entries:
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
