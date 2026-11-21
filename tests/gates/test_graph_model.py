# @feature: FP-GATE 文档图化模型层 | @ci: python-coverage
"""graph_model 模型层回归锁：枚举矩阵 / 解析原语 / viewer 契约。

意图：graph_model.py 是全景图三件（extract/aggregate/check）的共用原语层，
此处锁定其对外契约——枚举与文档类型规范一致、PROJECT_NODE_ID 与 viewer
硬编码一致、解析器行为可预期。上层件改坏原语契约时，这些测试先红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import graph_model as gm  # noqa: E402


class TestEnumContracts:
    """枚举与词表 = 文档类型规范 §一/§二/§六 的机械投影（真值锁定）。"""

    def test_doc_types_match_spec(self):
        # 类型规范 §二：追溯链六种 + standard（文档治理方案 D1，Standards 层）
        assert gm.DOC_TYPES == frozenset(
            {"requirement", "plan", "adr", "design", "report", "ledger", "standard"}
        )
        assert "rule" not in gm.DOC_TYPES
        assert "test" not in gm.DOC_TYPES

    def test_doc_statuses_match_spec(self):
        # 文档类型规范 §六：状态五值（deprecated = 废弃三处置落位）
        assert gm.DOC_STATUSES == frozenset(
            {"draft", "active", "superseded", "archived", "deprecated"}
        )

    def test_frontmatter_fields_whitelist(self):
        # v3.0 元数据极简化：裁至三字段
        assert gm.FRONTMATTER_FIELDS == frozenset({"doc_id", "type", "status"})

    def test_project_node_id_is_viewer_contract(self):
        # viewer.html 三处硬编码 'proj'（复合容器豁免/直属归属边判定/折叠豁免），
        # 改此值必须同步 viewer——测试在此立桩防静默漂移。
        assert gm.PROJECT_NODE_ID == "proj"

    def test_doc_id_shape(self):
        # 形态：YYYYMMDD-小写连字符串
        assert gm.DOC_ID_RE.match("20261028-design-doc")
        assert gm.DOC_ID_RE.match("20261022-ai-coding-system-agentos-landing")
        assert not gm.DOC_ID_RE.match("20261028-")  # 空后缀
        assert not gm.DOC_ID_RE.match("design-doc")  # 缺日期
        assert not gm.DOC_ID_RE.match("20261028-Design")  # 大写
        assert not gm.DOC_ID_RE.match("doc_id")  # 纯占位


class TestParseFrontmatter:
    def test_three_fields_and_body(self):
        text = "---\ndoc_id: 20261028-x\ntype: plan\nstatus: draft\n---\n正文"
        fields, body = gm.parse_frontmatter(text)
        assert fields == {"doc_id": "20261028-x", "type": "plan", "status": "draft"}
        assert body == "正文"

    def test_quotes_stripped(self):
        text = '---\ndoc_id: "20261028-x"\ntype: \'plan\'\n---\n'
        fields, _ = gm.parse_frontmatter(text)
        assert fields["doc_id"] == "20261028-x"
        assert fields["type"] == "plan"

    def test_no_frontmatter_returns_original(self):
        text = "# 无 frontmatter\n正文"
        fields, body = gm.parse_frontmatter(text)
        assert fields == {}
        assert body == text

    def test_unclosed_fence_returns_original(self):
        # 只有开头 --- 没有结尾 ---：不是合法 frontmatter，原文返回
        text = "---\ndoc_id: 20261028-x\n正文"
        fields, body = gm.parse_frontmatter(text)
        assert fields == {}
        assert body == text

    def test_indented_and_comment_lines_skipped(self):
        text = "---\n# 注释\n  indented: x\nkey: v\n---\n"
        fields, _ = gm.parse_frontmatter(text)
        assert fields == {"key": "v"}


class TestWikilinks:
    def test_three_segment_link(self):
        links = gm.iter_wikilinks("见 [[20261028-x|真值|设计契约]] 一节")
        assert links == [("20261028-x", "真值", "设计契约")]

    def test_two_and_one_segment_links(self):
        links = gm.iter_wikilinks("[[a|依赖]] 和 [[b]]")
        assert links == [("a", "依赖", ""), ("b", "", "")]

    def test_link_inside_code_fence_ignored(self):
        # 方案 §4.2 的三段式示例就写在行内代码里——不算真实引用
        text = "```md\n[[目标|关系|说明]]\n```\n行内 `[[占位]]` 也不算\n[[real|依赖|x]]"
        links = gm.iter_wikilinks(text)
        assert links == [("real", "依赖", "x")]

    def test_empty_target_skipped(self):
        assert gm.iter_wikilinks("[[|关系|x]]") == []


class TestLooksLikeReference:
    def test_reference_shapes(self):
        assert gm.looks_like_reference("20261028-x")  # doc_id 形态
        assert gm.looks_like_reference("plugins/shared/a/plugin.json")  # 路径
        assert gm.looks_like_reference("kernel\\crate\\lib.rs")  # Windows 路径
        assert gm.looks_like_reference("src/main.py")  # 代码后缀

    def test_placeholder_shapes_not_reference(self):
        # 纯中文占位/字面词 = 文档正文写法示例，悬空判定不得误伤
        assert not gm.looks_like_reference("目标")
        assert not gm.looks_like_reference("路径")
        assert not gm.looks_like_reference("doc_id")


class TestSimpleYaml:
    def test_top_level_scalars(self):
        yaml = "mode: coding\nname: 编码模式\ndescription: 代码实现 # 注释"
        out = gm.parse_simple_yaml(yaml)
        assert out["mode"] == "coding"
        assert out["name"] == "编码模式"
        assert out["description"] == "代码实现"  # 注释截断

    def test_skips_list_and_indented_lines(self):
        yaml = "name: x\ntool_ids:\n  - a\n  - b\nother: y"
        out = gm.parse_simple_yaml(yaml)
        assert out == {"name": "x", "other": "y"}

    def test_empty_value_without_block_kept(self):
        # 空值键只在后随缩进块（列表/子映射父键）时跳过；裸空值按空串保留
        assert gm.parse_simple_yaml("a:\nb: 1") == {"a": "", "b": "1"}

    def test_nested_map_parent_skipped(self):
        yaml = "id: p\nstate:\n  reads: [x]\nnext: v"
        assert gm.parse_simple_yaml(yaml) == {"id": "p", "next": "v"}

    def test_top_level_list_extraction(self):
        yaml = "id: g\ntool_ids:\n- bash\n- human_interaction\nnext: v"
        assert gm.parse_simple_yaml_list(yaml, "tool_ids") == [
            "bash",
            "human_interaction",
        ]

    def test_missing_list_returns_empty(self):
        assert gm.parse_simple_yaml_list("id: g\n", "tool_ids") == []
        assert gm.parse_simple_yaml_list("tool_ids: []\n", "tool_ids") == []


class TestDataclasses:
    def test_node_defaults_match_viewer_optional_fields(self):
        # viewer 按需消费 path/status/content/owner，缺省即空串
        node = gm.Node(id="x", type="doc", label="X")
        assert node.path == "" and node.status == "" and node.content == ""

    def test_edge_direction_fields(self):
        # s=起点 t=终点 k=关系——viewer 双向建邻接，方向语义在本仓侧
        edge = gm.Edge(s="a", t="b", k="依赖", note="n")
        assert (edge.s, edge.t, edge.k, edge.note) == ("a", "b", "依赖", "n")

    def test_source_facts_default_factories(self):
        facts = gm.SourceFacts(rel="r", kind="doc")
        assert facts.nodes == [] and facts.links == []
        assert facts.services == [] and facts.crate_deps == [] and facts.tool_needs == []


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
