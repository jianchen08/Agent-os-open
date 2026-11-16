# @feature: FP-0.2.〇 RP批1c 世界书字面对齐一期子集 | @ci: python-coverage
"""角色扮演世界书 ST 字面字段匹配语义（mode_roleplay/lorebook.py 批 1c 扩展）。

- 字面字段：keysecondary+selectiveLogic 四态 / disable / order / probability+
  useProbability / caseSensitive / matchWholeWords（对齐 st-design-reference §5）；
- 旧字段（keys/secondary_keys/enabled/insertion_order/case_sensitive/constant）
  兼容态由 test_roleplay_lorebook.py 基线锁定（零改动全绿），本文件不重复；
- 随机源经模块级 lorebook._random 注入固定 seed 实例（依赖注入，禁全局
  patch random）；禁 mock 内部依赖——匹配面纯函数真跑。
"""
from __future__ import annotations

import importlib.util
import os
import random
import sys
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLEPLAY_DIR = os.path.join(MODES_DIR, "mode_roleplay")

pytestmark = pytest.mark.unit


def _load_lorebook_module():
    """按真实导入名（mode_roleplay.lorebook）装载包内模块并注册 sys.modules（防双实例）。"""
    name = "mode_roleplay.lorebook"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(ROLEPLAY_DIR, "lorebook.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lorebook = _load_lorebook_module()


def _write_book(tmp_path, entries: list[dict], name: str = "book.yaml") -> dict:
    payload = {"entries": entries}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return lorebook.load_book(str(path))


def _entry(**overrides: Any) -> dict:
    entry = {"keys": ["月语"], "content": "神殿设定"}
    entry.update(overrides)
    return entry


def _hit_contents(book: dict, texts: list[str]) -> list[str]:
    return [hit["content"] for hit in lorebook.match_entries(book, texts)]


# ── selectiveLogic 四态（keysecondary 字面直读；枚举真值 = SillyTavern release
#    分支 world_info_logic，见 st-design-reference §5.3 考据注记）─────────────────


@pytest.mark.parametrize(
    ("logic", "texts", "hit"),
    [
        (0, ["新九龙城", "义体黑市"], True),  # AND ANY：主 + 任一副
        (0, ["新九龙城", "随便逛逛"], False),  # AND ANY：副全缺
        (0, ["义体黑市"], False),  # AND ANY：主缺一票否决
        (3, ["新九龙城", "义体黑市", "诊所"], True),  # AND ALL：主 + 全副（可跨文本）
        (3, ["新九龙城", "义体黑市"], False),  # AND ALL：缺一副
        (3, ["新九龙城"], False),  # AND ALL：副全缺也不命中
        (1, ["新九龙城", "义体黑市", "诊所"], False),  # NOT ALL：全副命中即否决
        (1, ["新九龙城", "义体黑市"], True),  # NOT ALL：≥1 副未命中即放行
        (1, ["新九龙城", "随便逛逛"], True),  # NOT ALL：全副未命中（≥1 未命中的极端）
        (2, ["新九龙城", "义体黑市"], False),  # NOT ANY：任一副出现即否决
        (2, ["新九龙城", "随便逛逛"], True),  # NOT ANY：全部副未命中才放行
        (2, ["义体黑市"], False),  # NOT ANY：主缺否决
    ],
)
def test_selective_logic_four_states(tmp_path, logic, texts, hit):
    """四态×主副键组合：主键命中先决，selectiveLogic 决定副键集合判定。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["新九龙城"], keysecondary=["义体", "诊所"], selectiveLogic=logic)],
        name=f"logic{logic}.yaml",
    )
    assert (_hit_contents(book, texts) == ["神殿设定"]) is hit


@pytest.mark.parametrize(
    ("window", "not_all_hit", "not_any_hit"),
    [
        # 恰一副命中：NOT ALL（≥1 副未命中）放行 / NOT ANY（须全副未命中）否决
        (["新九龙城", "义体"], True, False),
        # 全副命中：两态均否决
        (["新九龙城", "义体与诊所"], False, False),
        # 全副未命中：两态均放行（区分度边界）
        (["新九龙城", "随便逛逛"], True, True),
    ],
)
def test_not_all_vs_not_any_discrimination(tmp_path, window, not_all_hit, not_any_hit):
    """NOT ALL(1) 与 NOT ANY(2) 最易混态区分：同一窗口两态判定互锁。"""
    hits: dict[int, bool] = {}
    for logic in (1, 2):
        book = _write_book(
            tmp_path,
            [_entry(keys=["新九龙城"], keysecondary=["义体", "诊所"], selectiveLogic=logic)],
            name=f"disc{logic}.yaml",
        )
        hits[logic] = _hit_contents(book, window) == ["神殿设定"]
    assert hits[1] is not_all_hit
    assert hits[2] is not_any_hit


def test_selective_logic_default_is_and_any(tmp_path):
    """selectiveLogic 缺省 + keysecondary 非空 = AND ANY（ST 缺省态）。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["新九龙城"], keysecondary=["义体", "诊所"])],
        name="default_logic.yaml",
    )
    assert _hit_contents(book, ["新九龙城", "义体"]) == ["神殿设定"]
    assert _hit_contents(book, ["新九龙城"]) == []


def test_selective_logic_bad_value_falls_back_to_default(tmp_path):
    """selectiveLogic 坏值（非数值）按缺省 AND ANY 处理（fail-open）。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["新九龙城"], keysecondary=["义体", "诊所"], selectiveLogic="任意")],
        name="bad_logic.yaml",
    )
    assert _hit_contents(book, ["新九龙城", "义体"]) == ["神殿设定"]
    assert _hit_contents(book, ["新九龙城"]) == []


# ── disable：字面禁用过滤（优先于 enabled 旧键）──────────────────────────────────


@pytest.mark.parametrize(
    ("disable", "enabled", "hit"),
    [
        (True, True, False),  # disable 压过 enabled=True（字面优先）
        (True, False, False),
        (True, None, False),  # 仅字面键（手写书形态）
        (False, True, True),
        (False, None, True),
        (None, True, True),  # 无字面键 = 旧行为
        (None, False, False),  # 旧键仍一票否决
    ],
)
def test_disable_filters_entry(tmp_path, disable, enabled, hit):
    """disable=True 不参与匹配；与 enabled 并存时两键各自否决。"""
    entry = _entry()
    if disable is not None:
        entry["disable"] = disable
    if enabled is not None:
        entry["enabled"] = enabled
    book = _write_book(tmp_path, [entry], name=f"disable_{disable}_{enabled}.yaml")
    assert (_hit_contents(book, ["月语当空"]) == ["神殿设定"]) is hit


def test_disable_constant_entry_filtered(tmp_path):
    """disable 压过 constant：常驻条目被字面禁用后不注入。"""
    book = _write_book(tmp_path, [_entry(constant=True, disable=True)], name="dc.yaml")
    assert lorebook.scan_inject(book, ["任意文本"]) == ""


# ── order：注入排序（数字越大越后/越靠近对话，ST order 语义）────────────────────


def test_order_ascending_later_is_bigger(tmp_path):
    """order 升序注入：数字越大越靠近对话尾（ST order 语义）；match 返回同序。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["甲"], order=10, content="A10"),
            _entry(keys=["乙"], order=5, content="B05"),
            _entry(keys=["丙"], order=20, content="C20"),
        ],
        name="order.yaml",
    )
    texts = ["甲", "乙", "丙"]
    assert _hit_contents(book, texts) == ["B05", "A10", "C20"]
    assert lorebook.scan_inject(book, texts) == "## 世界设定\nB05\n\nA10\n\nC20"


def test_order_literal_wins_over_insertion_order(tmp_path):
    """order 与 insertion_order 并存（导入双写形态）：字面 order 优先。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["甲"], order=200, insertion_order=1, content="字面"),
            _entry(keys=["乙"], order=100, insertion_order=999, content="次要"),
        ],
        name="dual.yaml",
    )
    assert _hit_contents(book, ["甲", "乙"]) == ["次要", "字面"]


def test_order_mixed_with_legacy_insertion_order(tmp_path):
    """order（新）与 insertion_order（旧）混排：统一升序。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["甲"], insertion_order=15, content="旧15"),
            _entry(keys=["乙"], order=10, content="新10"),
        ],
        name="mixed.yaml",
    )
    assert _hit_contents(book, ["甲", "乙"]) == ["新10", "旧15"]


def test_order_tie_keeps_book_order(tmp_path):
    """同 order 稳定排序：保持书内出现序。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["甲"], order=100, content="先"),
            _entry(keys=["乙"], order=100, content="后"),
        ],
        name="tie.yaml",
    )
    assert lorebook.scan_inject(book, ["甲", "乙"]) == "## 世界设定\n先\n\n后"


def test_render_injection_sorts_direct_input_by_order():
    """render 侧按 order 稳定排序：直喂乱序列表也按 order 升序拼接。"""
    entries = [
        {"content": "B", "order": 2},
        {"content": "A", "order": 1},
        {"content": "C"},
    ]
    assert lorebook.render_injection(entries, token_budget=100) == "## 世界设定\nA\n\nB\n\nC"


# ── probability + useProbability：seed 注入可测（模块级 _random 依赖注入）────────


def test_probability_full_and_zero_bounds(tmp_path):
    """100% 恒触发 / 0% 恒不触发（多次重放，不依赖具体 seed 取值）。"""
    hit_book = _write_book(
        tmp_path,
        [_entry(keys=["月语"], useProbability=True, probability=100)],
        name="p100.yaml",
    )
    miss_book = _write_book(
        tmp_path,
        [_entry(keys=["月语"], useProbability=True, probability=0)],
        name="p0.yaml",
    )
    lorebook._random = random.Random(7)
    assert all(_hit_contents(hit_book, ["月语"]) == ["神殿设定"] for _ in range(20))
    assert all(_hit_contents(miss_book, ["月语"]) == [] for _ in range(20))


def test_probability_midpoint_bounded_and_reproducible(tmp_path):
    """50%：固定 seed 下多次重放触发率有界（非全触发/全不触发）且同 seed 序列可复现。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["月语"], useProbability=True, probability=50)],
        name="p50.yaml",
    )
    lorebook._random = random.Random(20261011)
    first = [_hit_contents(book, ["月语"]) == ["神殿设定"] for _ in range(200)]
    assert 0 < sum(first) < 200, "中间值触发率必须有界"

    lorebook._random = random.Random(20261011)
    replay = [_hit_contents(book, ["月语"]) == ["神殿设定"] for _ in range(200)]
    assert replay == first, "同 seed 重放序列必须逐位一致"


def test_probability_gate_requires_use_probability(tmp_path):
    """useProbability 未开（缺省/False）→ 不掷骰恒命中，probability 孤立存在不生效。"""
    for extra in ({"probability": 0}, {"probability": 0, "useProbability": False}):
        book = _write_book(tmp_path, [_entry(**extra)], name=f"gate{len(extra)}.yaml")
        assert _hit_contents(book, ["月语"]) == ["神殿设定"]
    # useProbability=True + probability 缺省 = 默认 100 恒触发
    book = _write_book(tmp_path, [_entry(useProbability=True)], name="gate_default.yaml")
    lorebook._random = random.Random(1)
    assert _hit_contents(book, ["月语"]) == ["神殿设定"]


def test_probability_applies_to_constant_entries(tmp_path):
    """概率门作用于常驻条目：constant=True + 0% 不注入（ST 语义：常驻也过概率）。"""
    book = _write_book(
        tmp_path,
        [_entry(constant=True, useProbability=True, probability=0)],
        name="const_p0.yaml",
    )
    assert lorebook.scan_inject(book, ["任意文本"]) == ""


def test_probability_bad_value_falls_back_to_full(tmp_path):
    """probability 坏值（非数值）按缺省 100 恒触发（fail-open，与 seed 无关）。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["月语"], useProbability=True, probability="多半")],
        name="p_bad.yaml",
    )
    lorebook._random = random.Random(3)
    assert all(_hit_contents(book, ["月语"]) == ["神殿设定"] for _ in range(10))


# ── caseSensitive：字面键，存在即优先于旧键 case_sensitive ──────────────────────


@pytest.mark.parametrize(
    ("literal_sensitive", "texts", "hit"),
    [
        (True, ["the dragon sleeps"], False),  # 敏感：小写文本不命中大写键
        (True, ["The DRAGON sleeps"], True),  # 敏感：精确大小写命中
        (None, ["the dragon sleeps"], True),  # 无字面键 = 缺省不敏感
        (False, ["THE DRAGON SLEEPS"], True),  # 显式不敏感
    ],
)
def test_case_sensitive_literal(tmp_path, literal_sensitive, texts, hit):
    entry: dict = {"keys": ["DRAGON"], "content": "龙之设定"}
    if literal_sensitive is not None:
        entry["caseSensitive"] = literal_sensitive
    book = _write_book(tmp_path, [entry], name=f"cs{literal_sensitive}.yaml")
    assert (_hit_contents(book, texts) == ["龙之设定"]) is hit


def test_case_sensitive_literal_wins_over_legacy(tmp_path):
    """caseSensitive=True 与 case_sensitive=False 并存（导入双写形态）：字面优先。"""
    book = _write_book(
        tmp_path,
        [_entry(keys=["DRAGON"], case_sensitive=False, caseSensitive=True)],
        name="cs_dual.yaml",
    )
    assert _hit_contents(book, ["the dragon sleeps"]) == []


# ── matchWholeWords：词边界（re \b 语义；CJK 键退化子串）────────────────────────


@pytest.mark.parametrize(
    ("whole_words", "text", "hit"),
    [
        (True, "the dragon sleeps", True),  # 整词命中
        (True, "the dragons sleep", False),  # 半词（复数粘连）不命中
        (True, "dragondust", False),  # 前缀粘连不命中
        (False, "the dragons sleep", True),  # 关闭 = 子串半词也命中（现状）
        (None, "dragondust", True),  # 缺省 = 现状子串
    ],
)
def test_match_whole_words_english(tmp_path, whole_words, text, hit):
    entry: dict = {"keys": ["dragon"], "content": "龙之设定"}
    if whole_words is not None:
        entry["matchWholeWords"] = whole_words
    book = _write_book(tmp_path, [entry], name=f"ww{whole_words}.yaml")
    assert (_hit_contents(book, [text]) == ["龙之设定"]) is hit


def test_match_whole_words_cjk_degrades_to_substring(tmp_path):
    """CJK 键词边界退化：\b 对 CJK 词内位置无界，退化为子串命中（docstring 契约）。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["月语"], matchWholeWords=True),
            _entry(keys=["龙"], matchWholeWords=True),
        ],
        name="ww_cjk.yaml",
    )
    assert _hit_contents(book, ["月语神殿的传说"]) == ["神殿设定"]
    assert _hit_contents(book, ["屠龙者来了"]) == ["神殿设定"]


# ── 零命中零注入契约（新字段各否决路径归零）──────────────────────────────────────


def test_zero_hit_zero_inject_with_new_fields(tmp_path):
    """禁用/概率 0/副键否决 → scan_inject 空串（零命中零注入不回退）。"""
    book = _write_book(
        tmp_path,
        [
            _entry(keys=["甲"], disable=True),
            _entry(keys=["乙"], useProbability=True, probability=0),
            _entry(keys=["丙"], keysecondary=["副"], selectiveLogic=0),
        ],
        name="zero.yaml",
    )
    # 副键「副」不入窗口：丙主键命中但 AND ANY 副缺仍不注入
    assert lorebook.scan_inject(book, ["甲", "乙", "丙"]) == ""
