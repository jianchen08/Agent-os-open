"""todo 清单提取纯函数——todo_track 插件私有域逻辑。

模块归属判据 = 通用潜力而非导入数（用户裁定 2026-10-08）：本模块规则
（≥2 条成块、取末块权威、todo 语义）是 todo 域逻辑，无通用潜力，故留
插件目录内部不共享化。

GFM task list 契约（方案 r4 §1.2 + 2026-10-24 增量协议裁定）：
- 语法：行首 ``- [ ] `` / ``- [x] ``（仅 ``-``；完成态 ``x``/``X`` 宽容）；
- 块识别：连续 checkbox 行 ≥2 条构成一个块（单条不成块，防散文误报）；
  代码围栏（``` / ~~~）内不识别；
- 整表规则（2026-10-24 裁定退役）：统一增量合并——任何 checkbox 行
  （成块或散行）都是操作单位：``[x]`` 勾选匹配现有项、``[ ]`` 新增查重
  追加；首批计划清单 = 空清单上全部追加，自然覆盖；不再做整表替换猜测
  （两条连续新增曾误判为完整块而丢现有清单，实测歧义）；
- 已知简化：嵌套子项扁平提取（保序平铺，不建层级）。
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

# 行首（允许缩进=嵌套子项扁平提取）checkbox 行；完成态 x/X 宽容。
_ITEM_RE = re.compile(r"^\s*-\s+\[([xX ])\]\s+(.*)$")

# 状态行清单形态（2026-10-24）：模型不总用 GFM 语法，✅/⏳ 等状态 emoji +
# 编号/短文本行是实际常见形态（G2 会话实测）。完成态/待办态集合按首字符
# 匹配；连续 ≥3 行成块才算清单（防正文散行 ✅ 误报）。
_DONE_EMOJI = ("✅", "☑️", "☑", "✔", "[x]", "【完成】")
_TODO_EMOJI = ("⏳", "◻", "☐", "🔲", "🕒", "[ ]", "【进行中】", "【排队】")
_STATUS_LINE_RE = re.compile(r"^\s*(\S{1,3})\s*(\d+[.、)]\s*)?(.+)$")


def _status_line_state(line: str) -> bool | None:
    """状态行判定：完成/待办 emoji 开头返回对应态，非状态行返回 None。"""
    stripped = line.lstrip()
    for e in _DONE_EMOJI:
        if stripped.startswith(e):
            return True
    for e in _TODO_EMOJI:
        if stripped.startswith(e):
            return False
    return None


def has_status_list_shape(text: str | None) -> bool:
    """消息是否含"状态行清单块"（≥3 连续 ✅/⏳ 等状态行，允许散嵌普通行 ≤1）。

    仅作折叠打标判据：命中则消息交 todo_card 卡渲染（卡内解析折叠）；
    不参与数据面合并（自由形态不做勾选/新增操作，防误解析）。
    """
    if not text:
        return False
    run = 0
    for line in text.splitlines():
        if _status_line_state(line) is not None:
            run += 1
            if run >= 3:
                return True
        elif line.strip() == "":
            continue  # 空行不断块
        else:
            run = 0
    return False


def parse_todo_parts(text: str) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """解析文本为（完整块列表, 散行增量列表），保序。

    块 = 连续 checkbox 行聚合且行数 ≥2（整表权威单位）；不成块的单条
    checkbox 行 = 散行增量（勾选/新增操作单位）。代码围栏内与围栏行本身
    不参与；非勾选行终止当前块。
    """
    blocks: list[list[dict[str, Any]]] = []
    singles: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    in_fence = False

    def _flush() -> None:
        nonlocal current
        if not current:
            return
        if len(current) >= 2:
            blocks.append(current)
        else:
            singles.append(current[0])
        current = []

    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = False
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            _flush()
            continue
        m = _ITEM_RE.match(line)
        if m:
            current.append(
                {"text": m.group(2).strip(), "done": m.group(1).lower() == "x"}
            )
        elif current:
            _flush()
    _flush()
    return blocks, singles


def parse_todo_blocks(text: str) -> list[list[dict[str, Any]]]:
    """解析全部清单块（保序，兼容旧调用面）。"""
    return parse_todo_parts(text)[0]


def latest_items(text: str) -> list[dict[str, Any]] | None:
    """权威规则：最后一个完整块；无块返回 None（状态停更语义）。"""
    blocks = parse_todo_parts(text)[0]
    return blocks[-1] if blocks else None


def parse_single_ops(text: str) -> list[dict[str, Any]]:
    """解析全部 checkbox 行为操作序列（块行+散行扁平保序，统一增量协议）。"""
    blocks, singles = parse_todo_parts(text)
    ops: list[dict[str, Any]] = []
    for block in blocks:
        ops.extend(block)
    ops.extend(singles)
    return ops


# HTML 任务卡解析（2026-10-25 用户实测：模型会仿历史折叠卡形态把清单包进
# ```html 代码围栏输出——GFM 解析零 ops，勾选丢失且界面显示源码）。卡形态
# 特征（与 render_todo_fold_html 同族）：<details> + "任务清单" + 带状态
# 标记的 <li> 项。完成态/待办态标记集合与状态行形态同族，🔄（进行中）
# 归待办态（增量协议：未完成=新增查重追加，不误勾现有项）。
_HTML_DONE_GLYPHS = ("☑️", "☑", "✔", "✅", "✓")
_HTML_TODO_GLYPHS = ("☐", "◻", "🔲", "🕒", "⏳", "🔄", "○")
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_entities(s: str) -> str:
    from html import unescape

    return unescape(s)


def parse_html_card_ops(text: str) -> list[dict[str, Any]]:
    """解析 HTML 任务卡片（```html 围栏内）为操作序列；非卡形态返回 []。

    卡形态守卫：文本须含 <details 且 "任务清单"（render_todo_fold_html 同族
    特征）且带标记 <li> ≥2——防普通 HTML 文档误解析。标记判定按 li 内文本
    首个状态字形；li 文本 = 去标签 + 实体还原。
    """
    if not text or "<details" not in text or "任务清单" not in text:
        return []
    ops: list[dict[str, Any]] = []
    for li_html in _LI_RE.findall(text):
        inner = _strip_entities(_TAG_RE.sub("", li_html)).strip()
        glyph = inner[:2].strip() or inner[:1]
        if glyph and not (glyph in _HTML_DONE_GLYPHS or glyph in _HTML_TODO_GLYPHS):
            # 首两字符不成标记时回退单字符（☑️ 为两字符，☐ 为一字符）
            glyph = inner[:1]
        if glyph in _HTML_DONE_GLYPHS:
            done = True
        elif glyph in _HTML_TODO_GLYPHS:
            done = False
        else:
            continue
        # 剥前导标记（含两字符形）后取任务文本
        body = inner
        for g in sorted((*_HTML_DONE_GLYPHS, *_HTML_TODO_GLYPHS), key=len, reverse=True):
            if body.startswith(g):
                body = body[len(g):]
                break
        body = body.lstrip(" :：、.-—")
        if body:
            ops.append({"text": body, "done": done})
    return ops if len(ops) >= 2 else []


# 尾注剥离（只作用于匹配键空间，存储原文不动）：`→ 已终结`（模型勾选尾注）
# 与 `— \`任务ID\`（状态）`（派发汇报惯例重述，7fb38fe89e82 实测同条待办
# 2~3 份副本的根因形态）。ASCII 连字符不剥——正文含 video-shotcraft 类
# 连字符词，剥了截键误伤；只剥箭头/破折号族。
_TAIL_NOTE_RE = re.compile(r"\s*(?:→|->|[—–]).*$")
_MATCH_SIMILARITY = 0.86
_MATCH_MIN_CHARS = 8
_TRAILING_PUNCT = "：:;；,，.。、!！?？"


def _normalize_match_text(text: str) -> str:
    """匹配归一化：NFKC、去空白、剥尾注（箭头/破折号族，见 _TAIL_NOTE_RE）。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _TAIL_NOTE_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    return s.strip(_TRAILING_PUNCT)


def _match_score(item_text: str, text: str) -> float:
    """匹配打分：2 精确 / 1.5 前缀或包含 / ≥0.86 相似 / 0 不匹配。"""
    a = _normalize_match_text(item_text)
    b = _normalize_match_text(text)
    if not a or not b:
        return 0.0
    if a == b:
        return 2.0
    if a.startswith(b) or (len(b) >= 6 and b in a):
        return 1.5
    if len(a) >= _MATCH_MIN_CHARS and len(b) >= _MATCH_MIN_CHARS:
        ratio = SequenceMatcher(None, a, b).ratio()
        if ratio >= _MATCH_SIMILARITY:
            return ratio
    return 0.0


def _matches(item: dict[str, Any], text: str) -> bool:
    """勾选目标匹配：归一化后精确 → 前缀 → 包含 → 相似（短文本不做包含匹配）。"""
    return _match_score(item.get("text", ""), text) > 0


def _is_known_item(
    merged: list[dict[str, Any]], key: str, existing_keys: set[str]
) -> bool:
    """新增判重（精确键之外识别「同一待办的重述形态」，1.2.32）：

    - 精确键已在集合（原有判重，收拢到本函数统一出口）；
    - 相似唯一最优（与勾选面同参：ratio ≥0.86 且与次优差 ≥0.05，双侧
      ≥_MATCH_MIN_CHARS 才参与）：措辞漂移变体（如差一个「的」）——只用
      ratio 分，不复用 _match_score（其前缀/包含 1.5 分无长度闸，会吞短
      真新条目）；
    - 双向包含（归一化短侧 ≥6 字）：裸标题短重述对长条目——短侧长度闸防
      3 字短行误吞真新条目（写测试 vs 写测试报告）。

    命中 = 已知项，跳过追加不再新开条目（7fb38fe89e82 实测多副本根因修复）。
    """
    if key in existing_keys:
        return True
    ratios: list[float] = []
    for item in merged:
        other = _normalize_match_text(item.get("text", ""))
        if not other:
            continue
        short, long_ = sorted((other, key), key=len)
        if len(short) >= 6 and short in long_:
            return True
        if len(other) >= _MATCH_MIN_CHARS and len(key) >= _MATCH_MIN_CHARS:
            ratios.append(SequenceMatcher(None, other, key).ratio())
    if ratios:
        ratios.sort(reverse=True)
        second = ratios[1] if len(ratios) > 1 else 0.0
        if ratios[0] >= _MATCH_SIMILARITY and ratios[0] - second >= 0.05:
            return True
    return False


def incremental_merge(
    current: list[dict[str, Any]], ops: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """散行增量合并（2026-10-24 用户裁定：勾选不按顺序/不重发全表，新增查重追加）。

    - 勾选（done=True）：按归一化后精确/前缀/包含/相似匹配现有未完成项置
      done；相似匹配要求唯一最优（与次优差 ≥0.05，防近重复项抢匹配）；无
      匹配且归一化文本不在清单 → 作为已完成项追加（首轮列清单带已完成
      阶段，不丢）；
    - 新增（done=False）：归一化精确键 + 匹配分兜底（_is_known_item：相似
      唯一最优 / 双向包含短侧 ≥6 字）都不命中才追加——模型按「标题 —
      `任务ID`（状态）」汇报惯例重述、措辞漂移变体不再产生副本（7fb38fe89e82
      实测同条 2~3 份副本根因）；不聚类、不做语义合并（无法靠匹配可靠聚类）；
    - 重复轮幂等：勾选已 done 无害；新增同文本不重复追加；
    - 无 ops 或零有效变更返回 None（停更语义，不刷 updated_at）。

    返回合并后的完整 items（新列表，不改入参）。
    """
    if not ops:
        return None
    merged = [dict(item) for item in current]
    changed = False
    existing_keys = {
        key
        for item in merged
        if (key := _normalize_match_text(item.get("text", "")))
    }
    for op in ops:
        text = str(op.get("text", "")).strip()
        if not text:
            continue
        key = _normalize_match_text(text)
        if op.get("done"):
            scores = sorted(
                (
                    (_match_score(item.get("text", ""), text), idx)
                    for idx, item in enumerate(merged)
                    if not item.get("done")
                ),
                key=lambda pair: (pair[0], -pair[1]),
                reverse=True,
            )
            best = scores[0] if scores else (0.0, -1)
            second = scores[1][0] if len(scores) > 1 else 0.0
            matched = best[0] >= 1.5 or (
                best[0] >= _MATCH_SIMILARITY and best[0] - second >= 0.05
            )
            if matched:
                merged[best[1]]["done"] = True
                changed = True
            elif key and key not in existing_keys:
                merged.append({"text": text, "done": True})
                existing_keys.add(key)
                changed = True
        elif key and not _is_known_item(merged, key, existing_keys):
            merged.append({"text": text, "done": False})
            existing_keys.add(key)
            changed = True
    return merged if changed else None


# ── 片段级清单折叠（2026-10-24 用户裁定：消息内识别到的文本片段单独
#    渲染，不是整条消息打标）── 原位包裹 ```todo fence（内含自包含
#    <details> 折叠 HTML，原生折叠零脚本），前端既有 content_cards 机制
#    （form=webview，srcdoc + CSP 允许内联样式）按 fence language 片段级
#    渲染，消息其余正文正常 markdown。零前端改动。

TODO_FENCE = "todo"
_FENCE_MARK = "```" + TODO_FENCE
# 状态行成块门槛：自由形态误报面大于 checkbox，要求 ≥3 连续行
_STATUS_BLOCK_MIN = 3


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_todo_fold_html(items: list[dict[str, Any]]) -> str:
    """清单项列表 → 自包含 <details> 折叠 HTML（原生折叠，内联样式）。"""
    done = sum(1 for it in items if it.get("done"))
    total = len(items)
    parts = [
        '<li style="margin:2px 0;{dc}"><span style="margin-right:6px;{mc}">{mark}</span>{text}</li>'.format(
            dc="color:#8b949e;text-decoration:line-through;" if it.get("done") else "",
            mc="color:#0969da;" if it.get("done") else "color:#8b949e;",
            mark="☑" if it.get("done") else "☐",
            text=_escape_html(str(it.get("text", ""))),
        )
        for it in items
    ]
    return (
        '<details style="margin:6px 0;font:13px/1.6 -apple-system,&#39;Segoe UI&#39;,'
        "'PingFang SC','Microsoft YaHei',sans-serif;\">"
        '<summary style="cursor:pointer;user-select:none;color:#0969da;font-size:12px;">'
        f"📋 任务清单 {done}/{total}</summary>"
        f'<ul style="margin:6px 0 2px;padding-left:20px;color:#1f2328;">{"".join(parts)}</ul>'
        "</details>"
    )


def _status_item(line: str) -> dict[str, Any] | None:
    """状态行 → 清单项（✅/⏳ 等；剥前缀 emoji 与编号）；非状态行 None。"""
    stripped = line.lstrip()
    for e in _DONE_EMOJI:
        if stripped.startswith(e):
            return {"text": _strip_lead(stripped[len(e):]), "done": True}
    for e in _TODO_EMOJI:
        if stripped.startswith(e):
            return {"text": _strip_lead(stripped[len(e):]), "done": False}
    return None


def _strip_lead(s: str) -> str:
    import re as _re

    return _re.sub(r"^\d+[.、)]\s*", "", s.strip()).strip()


def wrap_todo_blocks(text: str) -> str | None:
    """把消息 content 中的清单块原位包裹为 ```todo fence（含折叠 HTML）。

    识别面：checkbox 块（≥2 连续行）+ 状态行块（≥3 连续行，空行不断块
    但尾随空行回投原文）。非清单行原样保留；消息已含 todo fence（幂等）
    或无清单块返回 None（不改写）。
    """
    if not text or _FENCE_MARK in text:
        return None
    lines = text.splitlines()
    out: list[str] = []
    raw_tail: list[str] = []  # 块尾空行缓冲（未确认成块前回投原文）
    items: list[dict[str, Any]] = []
    kind: str | None = None  # "check" | "status"（块型不混）
    changed = False

    def _flush() -> None:
        nonlocal items, kind, raw_tail, changed
        out.extend(raw_tail)
        raw_tail = []
        if not items:
            kind = None
            return
        min_len = 2 if kind == "check" else _STATUS_BLOCK_MIN
        if len(items) >= min_len:
            out.append(_FENCE_MARK)
            out.append(render_todo_fold_html(items))
            out.append("```")
            changed = True
        else:
            for it in items:
                out.append(str(it.get("_raw", "")))
        items = []
        kind = None

    for line in lines:
        m = _ITEM_RE.match(line)
        st = None if m else _status_item(line)
        cur_kind = "check" if m else ("status" if st is not None else None)
        if cur_kind is not None:
            if kind and cur_kind != kind:
                _flush()
            if raw_tail:
                out.extend(raw_tail)
                raw_tail = []
            kind = cur_kind
            if m:
                items.append({"text": m.group(2).strip(), "done": m.group(1).lower() == "x", "_raw": line})
            else:
                assert st is not None
                items.append({**st, "_raw": line})
        elif line.strip() == "" and items:
            raw_tail.append(line)  # 块内空行缓冲
        else:
            _flush()
            out.append(line)
    _flush()
    return "\n".join(out) if changed else None
