# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""角色扮演世界书扫描注入（mode_roleplay/lorebook.py）单元测试。

- load_book 以 tmp_path 真实 yaml 文件驱动（损坏/顶层非映射/缺文件三态），不全 mock
- 匹配矩阵：关键词/constant/enabled/secondary_keys/insertion_order/scan_depth/
  case_sensitive 各行为 ≥2 组区分度输入（可枚举面 parametrize 展开）
- 端到端：仓库真实 demo_world.yaml 驱动 scan_inject，断言注入形态与关键词命中
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLEPLAY_DIR = os.path.join(MODES_DIR, "mode_roleplay")
DEMO_BOOK_PATH = os.path.join(ROLEPLAY_DIR, "lorebooks", "demo_world.yaml")

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


def _write_book(tmp_path, payload: Any, name: str = "book.yaml") -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return str(path)


def _entry(**overrides: Any) -> dict:
    entry = {
        "keys": ["月语"],
        "secondary_keys": [],
        "content": "神殿设定",
        "enabled": True,
        "insertion_order": 100,
        "constant": False,
        "position": "before_char",
        "case_sensitive": False,
    }
    entry.update(overrides)
    return entry


# ── load_book：真实文件三态（损坏/顶层非映射/缺文件）+ 正常解析 ──────────────────


def test_load_book_parses_demo_shape(tmp_path):
    """正常 yaml → 原样 dict；条目与书级字段不丢。"""
    path = _write_book(
        tmp_path,
        {"name": "书", "scan_depth": 3, "token_budget": 256, "entries": [_entry()]},
    )
    book = lorebook.load_book(path)
    assert book["name"] == "书"
    assert book["scan_depth"] == 3
    assert book["entries"][0]["keys"] == ["月语"]


def test_load_book_corrupt_yaml_raises_value_error_with_path(tmp_path):
    """解析失败 → ValueError 且消息带路径。"""
    path = tmp_path / "bad.yaml"
    path.write_text("entries: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match="bad\\.yaml"):
        lorebook.load_book(str(path))


def test_load_book_top_level_non_mapping_raises_with_path(tmp_path):
    """顶层标量（非映射）→ ValueError 且消息带路径。"""
    path = _write_book(tmp_path, "just-a-string", name="scalar.yaml")
    with pytest.raises(ValueError, match="scalar\\.yaml"):
        lorebook.load_book(path)


def test_load_book_missing_file_propagates_os_error(tmp_path):
    """缺文件不吞：OSError（FileNotFoundError）原样传播。"""
    with pytest.raises(FileNotFoundError, match="nope\\.yaml"):
        lorebook.load_book(str(tmp_path / "nope.yaml"))


# ── 关键词命中：注入形态 + 命中条目返回 ──────────────────────────────────────────


def test_keyword_hit_injects_content_block(tmp_path):
    """关键词在文本中 → 注入块为 '## 世界设定\\n<content>'，match 返回条目原样。"""
    path = _write_book(tmp_path, {"entries": [_entry(keys=["月语", "月光"], content="神殿设定")]})
    book = lorebook.load_book(path)
    out = lorebook.scan_inject(book, ["昨晚我们抵达月光神殿"])
    assert out == "## 世界设定\n神殿设定"

    hits = lorebook.match_entries(book, ["月语招牌的铺子"])
    assert [h["content"] for h in hits] == ["神殿设定"]
    assert hits[0]["position"] == "before_char"


# ── constant / enabled 矩阵（含非 dict 行容错） ──────────────────────────────────


def test_constant_and_enabled_matrix(tmp_path):
    """constant 无关键词恒命中；enabled=False 一票否决（压过 constant）；垃圾行跳过。"""
    path = _write_book(
        tmp_path,
        {
            "entries": [
                _entry(keys=["月语"], constant=True, content="常驻物料"),
                _entry(keys=["月语"], enabled=False, content="停用物料"),
                _entry(constant=True, enabled=False, content="常驻但停用"),
                "garbage",
            ]
        },
    )
    book = lorebook.load_book(path)
    hits = lorebook.match_entries(book, ["完全没有关键词的一句"])
    assert [h["content"] for h in hits] == ["常驻物料"]
    out = lorebook.scan_inject(book, ["完全没有关键词的一句"])
    assert "常驻物料" in out
    assert "停用物料" not in out
    assert "常驻但停用" not in out


def test_enabled_true_keyword_entry_still_needs_keyword(tmp_path):
    """非 constant 条目缺关键词不命中（与 constant 恒命中构成相反组）。"""
    path = _write_book(tmp_path, {"entries": [_entry(content="神殿设定")]})
    book = lorebook.load_book(path)
    assert lorebook.scan_inject(book, ["今天天气不错"]) == ""


# ── secondary_keys：主/副键门控 ──────────────────────────────────────────────────


def test_secondary_keys_gate(tmp_path):
    """主命中副缺不注入；主副都中注入（可跨窗口内不同文本）；仅副中不注入。"""
    path = _write_book(
        tmp_path,
        {
            "entries": [
                _entry(
                    keys=["新九龙城"],
                    secondary_keys=["义体", "诊所"],
                    content="城寨设定",
                )
            ]
        },
    )
    book = lorebook.load_book(path)

    assert lorebook.match_entries(book, ["我们进了新九龙城逛逛"]) == []
    assert lorebook.scan_inject(book, ["我们进了新九龙城逛逛"]) == ""

    hits = lorebook.match_entries(book, ["我们进了新九龙城", "直奔义体黑市"])
    assert [h["content"] for h in hits] == ["城寨设定"]
    assert "城寨设定" in lorebook.scan_inject(book, ["我们进了新九龙城", "直奔义体黑市"])

    assert lorebook.match_entries(book, ["那里有家义体店"]) == []


# ── insertion_order 升序拼接（同序保持书内原序） ─────────────────────────────────


def test_insertion_order_ascending_join(tmp_path):
    """多条命中按 insertion_order 升序拼接；match 返回序列同为升序。"""
    path = _write_book(
        tmp_path,
        {
            "entries": [
                _entry(keys=["高"], insertion_order=200, content="C200"),
                _entry(keys=["中"], insertion_order=100, content="C100"),
                _entry(keys=["低"], insertion_order=-5, content="Cneg"),
            ]
        },
    )
    book = lorebook.load_book(path)
    texts = ["高", "中", "低"]
    assert [h["content"] for h in lorebook.match_entries(book, texts)] == ["Cneg", "C100", "C200"]
    assert lorebook.scan_inject(book, texts) == "## 世界设定\nCneg\n\nC100\n\nC200"


def test_insertion_order_tie_keeps_book_order(tmp_path):
    """insertion_order 相同 → 保持书内出现序（稳定排序）。"""
    path = _write_book(
        tmp_path,
        {
            "entries": [
                _entry(constant=True, insertion_order=100, content="先"),
                _entry(constant=True, insertion_order=100, content="后"),
            ]
        },
    )
    book = lorebook.load_book(path)
    assert lorebook.scan_inject(book, ["任意文本"]) == "## 世界设定\n先\n\n后"


# ── token_budget：超预算即停（不截断条目、后位裁掉） ─────────────────────────────


def test_token_budget_stops_at_overflow():
    """40 字条目 ×3：预算 20 token 只留 1 条、30 token 留 2 条；性质断言全长 ≤ 4×预算。"""
    entries = [{"content": "甲" * 40}, {"content": "乙" * 40}, {"content": "丙" * 40}]

    out20 = lorebook.render_injection(entries, token_budget=20)
    assert "甲" * 40 in out20
    assert "乙" * 40 not in out20
    assert "丙" * 40 not in out20
    assert len(out20) <= 20 * 4

    out30 = lorebook.render_injection(entries, token_budget=30)
    assert "甲" * 40 in out30
    assert "乙" * 40 in out30
    assert "丙" * 40 not in out30
    assert len(out30) <= 30 * 4


def test_token_budget_boundaries():
    """恰好在预算内保留；预算连首条都容不下 → 空串（不截断条目）。"""
    exact = [{"content": "丁" * 72}]  # 8(表头) + 72 = 80 字 = 20 token 恰好
    out = lorebook.render_injection(exact, token_budget=20)
    assert out == "## 世界设定\n" + "丁" * 72

    assert lorebook.render_injection([{"content": "甲" * 40}], token_budget=1) == ""


def test_render_empty_entries_returns_empty():
    """无条目 → 空串。"""
    assert lorebook.render_injection([], token_budget=512) == ""


# ── scan_depth：窗口 = texts[:scan_depth] ────────────────────────────────────────


@pytest.mark.parametrize(
    ("scan_depth", "texts", "hit"),
    [
        (None, ["一", "二", "三", "四", "月语在这里"], False),  # 默认 4，第 5 条在窗外
        (5, ["一", "二", "三", "四", "月语在这里"], True),  # 书级 scan_depth=5 覆盖
        (None, ["一", "二", "三", "月语在这里", "无关"], True),  # 第 4 条在默认窗口内
        (0, ["月语在这里"], False),  # 窗口为空
    ],
)
def test_scan_depth_window(tmp_path, scan_depth, texts, hit):
    """扫描窗口取 texts[:scan_depth]（书级字段，默认 4）。"""
    payload: dict = {"entries": [_entry(content="神殿设定")]}
    if scan_depth is not None:
        payload["scan_depth"] = scan_depth
    book = lorebook.load_book(_write_book(tmp_path, payload, name=f"d{scan_depth}.yaml"))
    out = lorebook.scan_inject(book, texts)
    assert ("神殿设定" in out) is hit


# ── case_sensitive：默认不敏感，显式敏感后大小写必须精确 ─────────────────────────


@pytest.mark.parametrize(
    ("case_sensitive", "texts", "hit"),
    [
        (None, ["the dragon sleeps"], True),  # 默认不敏感
        (True, ["the dragon sleeps"], False),  # 敏感后小写不命中
        (True, ["The DRAGON sleeps"], True),  # 敏感 + 精确大小写命中
        (False, ["THE DRAGON SLEEPS"], True),  # 显式不敏感全大写也命中
    ],
)
def test_case_sensitivity_matrix(tmp_path, case_sensitive, texts, hit):
    payload: dict = {"entries": [_entry(keys=["DRAGON"], content="龙之设定")]}
    if case_sensitive is not None:
        payload["entries"][0]["case_sensitive"] = case_sensitive
    book = lorebook.load_book(_write_book(tmp_path, payload, name=f"c{case_sensitive}.yaml"))
    out = lorebook.scan_inject(book, texts)
    assert ("龙之设定" in out) is hit


# ── scan_inject 便捷入口：书级 token_budget 透传 ─────────────────────────────────


def test_scan_inject_reads_book_token_budget(tmp_path):
    """scan_inject 取书级 token_budget（缺省 512）传给 render。"""
    payload = {
        "token_budget": 20,
        "entries": [
            _entry(constant=True, content="甲" * 40),
            _entry(constant=True, content="乙" * 40),
        ],
    }
    book = lorebook.load_book(_write_book(tmp_path, payload))
    out = lorebook.scan_inject(book, ["任意文本"])
    assert "甲" * 40 in out
    assert "乙" * 40 not in out


# ── 端到端：仓库真实 demo_world.yaml ─────────────────────────────────────────────


def test_demo_world_end_to_end():
    """真实物料：月光神殿命中月神神殿设定；停用 constant 条目不出现；副键门控生效。"""
    book = lorebook.load_book(DEMO_BOOK_PATH)

    out = lorebook.scan_inject(book, ["我们启程前往月光神殿"])
    assert out.startswith("## 世界设定\n")
    assert "月神神殿" in out
    # 停用的说明条目（constant=True + enabled=False）不得出现
    assert "出厂演示物料" not in out

    # 新九龙城主键命中 + 义体副键跨文本命中 → 赛博线设定注入
    cyber_hit = lorebook.scan_inject(book, ["我们潜入了新九龙城", "直奔义体黑市"])
    assert "铁手" in cyber_hit
    # 主键命中但副键缺失 → 不注入
    assert lorebook.scan_inject(book, ["我们潜入了新九龙城", "随便逛逛"]) == ""
