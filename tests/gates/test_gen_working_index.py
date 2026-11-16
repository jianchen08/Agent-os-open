# @feature: FP-GATE working索引生成器frontmatter(F3拆账②) | @ci: python-coverage
"""gen_working_index.py 输出契约回归锁（复核 9eaf19172 F3 缺口）。

行为契约（断输入→输出，不钉实现）：
- 生成输出以三字段 frontmatter 开头，doc_id/type/status 值正确且为 graph_model
  词表合法枚举（doc-frontmatter/graph 闸可解析，重生成不再剥手补字段）；
- 反例防拟合：frontmatter 之外正文仍含自动生成头注、篇数计数与真实条目
  （防只输出空壳 frontmatter）；private/ 域不导流；
- 幂等性质：同文件集两次渲染逐字节相同（含 frontmatter 行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_working_index as gwi  # noqa: E402
import graph_model as gm  # noqa: E402

pytestmark = pytest.mark.unit


def _build_working(root: Path) -> Path:
    """构造最小 docs/working 伪树：根层两件（一件有 H1 一件无）、子目录一件、private 一件。"""
    working = root / "docs" / "working"
    (working / "sub").mkdir(parents=True)
    (working / "a.md").write_text("# 根层文档甲\n\n正文。\n", encoding="utf-8")
    (working / "b.md").write_text("无标题文件\n", encoding="utf-8")
    (working / "sub" / "c.md").write_text("# 子目录文档丙\n", encoding="utf-8")
    (working / "private").mkdir()
    (working / "private" / "x.md").write_text("# 私密件不进索引\n", encoding="utf-8")
    return working


class TestFrontmatterContract:
    """生成输出头部 = 三字段 frontmatter（值固化、枚举合法、白名单精确）。"""

    def test_output_starts_with_three_field_frontmatter(self, tmp_path):
        working = _build_working(tmp_path)
        text = gwi.render(gwi.collect(working), working)
        assert text.startswith("---\n"), "生成输出必须以 frontmatter 块开头"
        fields, _body = gm.parse_frontmatter(text)
        assert fields["doc_id"] == "20261030-working-index"
        assert fields["type"] == "report"
        assert fields["status"] == "active"
        # 值与 graph_model 词表对应（doc-frontmatter/graph 闸可解析）
        assert fields["type"] in gm.DOC_TYPES
        assert fields["status"] in gm.DOC_STATUSES
        assert set(fields) == set(gm.FRONTMATTER_FIELDS), "只允许三字段白名单"

    def test_body_still_carries_note_counts_and_entries(self, tmp_path):
        # 反例：frontmatter 不得是空壳输出——正文仍含头注、篇数行与条目
        working = _build_working(tmp_path)
        text = gwi.render(gwi.collect(working), working)
        assert "<!-- 自动生成" in text
        assert "# docs/working 主题索引" in text
        assert "共 3 篇 .md、2 个目录层" in text
        assert "- [a.md](a.md) — 根层文档甲" in text
        assert "- [c.md](../sub/c.md) — 子目录文档丙" in text
        assert "x.md" not in text, "private/ 域文件不得进索引条目"

    def test_render_idempotent_including_frontmatter(self, tmp_path):
        # 性质断言：同文件集两次渲染逐字节相同（含 frontmatter 行）
        working = _build_working(tmp_path)
        first = gwi.render(gwi.collect(working), working)
        second = gwi.render(gwi.collect(working), working)
        assert first == second
