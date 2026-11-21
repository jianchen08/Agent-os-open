# @feature: FP-GATE 文档图化管线 | @ci: python-coverage
"""graph 管线端到端行为测试：提取 → 聚合 → 落盘 → 校验。

意图：验证全景图三合一命令（增量聚合 → graph.js 落盘 → 校验红绿）的
**可观察行为**——输入文件形态决定节点/边产出、双链解析决定悬空判定、
frontmatter 合法性决定校验红绿。全部走 tmp 真实文件系统（真实链路，
非 mock）。run_gates fast 档挂载也在本文件立桩（gate 缺失即红）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_scripts = str(ROOT / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import graph_check  # noqa: E402
import graph_extract  # noqa: E402
import graph_model as gm  # noqa: E402
from graph_aggregate import aggregate, build_graph, discover_files  # noqa: E402


# ── 微型合法仓库（tmp 真实文件；形态对照仓内真实抽查样本）──────────────


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _make_repo(root: Path) -> None:
    """构造一个"校验应全绿"的微型仓库：代码半边三类源 + 文档半边五份。"""
    _write(
        root,
        "plugins/shared/alpha/plugin.json",
        json.dumps(
            {
                "id": "alpha_service",
                "name": "Alpha Service",
                "requires_services": ["beta.svc"],
                "capabilities": {"services": [{"name": "alpha.run"}], "tools": []},
            },
            ensure_ascii=False,
        ),
    )
    _write(
        root,
        "plugins/shared/beta/plugin.json",
        json.dumps(
            {
                "id": "beta_service",
                "name": "Beta Service",
                "requires_services": [],
                "capabilities": {"services": [{"name": "beta.svc"}], "tools": []},
            },
            ensure_ascii=False,
        ),
    )
    _write(
        root,
        "plugins/shared/bash/plugin.json",
        json.dumps(
            {
                "id": "bash",
                "name": "Bash",
                "requires_services": [],
                "capabilities": {
                    "services": [],
                    "tools": [{"name": "bash", "description": "shell"}],
                },
            },
            ensure_ascii=False,
        ),
    )
    _write(
        root,
        "kernel/crates/alpha_c/Cargo.toml",
        '[package]\nname = "alpha_c"\nversion = "0.1.0"\n\n[dependencies]\n'
        'beta_c = { path = "../beta_c" }\nserde = "1.0"\n',
    )
    _write(root, "kernel/crates/beta_c/Cargo.toml", "[package]\nname = \"beta_c\"\n")
    _write(
        root,
        "config/agents/executor/general_agent.yaml",
        "name: 通用执行者\ndescription: 执行任务\ntool_ids:\n- bash\n",
    )
    _write(
        root,
        "plugins/shared/mode_coding/mode.yaml",
        "mode: coding\nname: 编码模式\ndescription: 代码实现类任务\n",
    )
    _write(
        root,
        "skills/alpha_skill/SKILL.md",
        "---\nname: Alpha 技能\ndescription: 测试技能\n---\n正文\n",
    )
    _write(root, "config/pipelines/auto.yaml", "name: 自动管道\nsteps: []\n")
    _write(
        root,
        "docs/req.md",
        "---\ndoc_id: 20261028-req-doc\ntype: requirement\nstatus: active\n---\n"
        "# 需求\n\n设计契约见 [[20261028-design-doc|真值|设计真值]]。\n"
        "服务由 [[plugins/shared/beta/plugin.json|依赖|清单文件]] 提供。\n"
        "写法示例 [[目标|关系|说明]] 不算引用。\n",
    )
    _write(
        root,
        "docs/design.md",
        "---\ndoc_id: 20261028-design-doc\ntype: design\nstatus: active\n---\n# 设计\n",
    )
    _write(
        root,
        "docs/old_plan.md",
        "---\ndoc_id: 20261028-old-plan\ntype: plan\nstatus: archived\n---\n"
        "被 [[20261028-new-plan|取代|新方案]] 取代。\n",
    )
    _write(
        root,
        "docs/new_plan.md",
        "---\ndoc_id: 20261028-new-plan\ntype: plan\nstatus: draft\n---\n新方案\n",
    )
    _write(root, "docs/legacy.md", "# 存量文档\n\n没有 frontmatter。\n")


def _load_graph_js(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("window.GRAPH_DATA"), "graph.js 必须是赋值形态（file:// 可开）"
    return json.loads(text.split("=", 1)[1].strip().rstrip(";\n"))


def _without_meta(data: dict) -> dict:
    return {"nodes": data["nodes"], "edges": data["edges"]}


def _node_ids(data: dict) -> set[str]:
    return {n["id"] for n in data["nodes"]}


def _edges(data: dict, kind: str) -> list[dict]:
    return [e for e in data["edges"] if e["k"] == kind]


# ── 提取层：输入形态 → 单文件事实 ────────────────────────────────────


class TestDiscoverFiles:
    def test_matches_only_under_literal_prefix_dir(self, tmp_path):
        # 模式锚定在字面前缀目录：同名子树出现在别处（node_modules 里的 docs/）
        # 不是提取源——这是"不遍历无关大树"的可观察语义面
        _write(tmp_path, "docs/a/b.md", "x")
        _write(tmp_path, "node_modules/pkg/docs/deep.md", "x")
        assert discover_files(tmp_path) == ["docs/a/b.md"]

    def test_material_pool_excluded(self, tmp_path):
        # RP 素材池是资料不是文档：模板语法会被误判双链，池内 md 全部不进图
        _write(tmp_path, "docs/working/plan.md", "x")
        _write(tmp_path, "docs/working/rp_prompt_material/mixed/m.md", "x")
        files = discover_files(tmp_path)
        assert "docs/working/plan.md" in files
        assert not [f for f in files if "rp_prompt_material" in f]

    def test_flat_layout_and_deep_dirs_discovered(self, tmp_path):
        # 平铺插件/manifest 与三层深 agent 配置都须在源集合内
        _write(tmp_path, "plugins/shared/alpha/plugin.json", "{}")
        _write(tmp_path, "kernel/crates/core/Cargo.toml", '[package]\nname = "c"\n')
        _write(tmp_path, "config/agents/a/b/deep.yaml", "name: x\n")
        files = discover_files(tmp_path)
        assert "plugins/shared/alpha/plugin.json" in files
        assert "kernel/crates/core/Cargo.toml" in files
        assert "config/agents/a/b/deep.yaml" in files


class TestExtractSource:
    def test_plugin_json_yields_node_and_service_need(self):
        text = json.dumps(
            {
                "id": "alpha_service",
                "requires_services": ["beta.svc"],
                "capabilities": {"services": [{"name": "alpha.run"}], "tools": []},
            }
        )
        facts = graph_extract.extract_source("plugins/shared/alpha/plugin.json", text)
        assert facts.kind == "plugin"
        assert facts.nodes[0].id == "plugin:alpha_service"
        assert facts.nodes[0].label == "alpha_service"
        assert [s.service for s in facts.services] == ["beta.svc"]

    def test_plugin_without_requires_yields_no_service(self):
        text = json.dumps({"id": "solo", "capabilities": {"services": [], "tools": []}})
        facts = graph_extract.extract_source("plugins/shared/solo/plugin.json", text)
        assert facts.services == []

    def test_cargo_toml_yields_node_and_internal_and_external_deps(self):
        text = (
            "[package]\nname = \"alpha_c\"\n\n[dependencies]\n"
            "beta_c = { path = \"../beta_c\" }\nserde = \"1.0\"\n"
        )
        facts = graph_extract.extract_source("kernel/crates/alpha_c/Cargo.toml", text)
        assert facts.kind == "crate"
        assert facts.nodes[0].id == "crate:alpha_c"
        # 内外部依赖都记为意图；边在装配阶段按"是否命中 crate 节点"生成
        assert sorted(d.dep for d in facts.crate_deps) == ["beta_c", "serde"]

    def test_agent_yaml_yields_node_and_tool_needs(self):
        text = "name: 通用执行者\ntool_ids:\n- bash\n- human_interaction\n"
        facts = graph_extract.extract_source(
            "config/agents/executor/general_agent.yaml", text
        )
        assert facts.kind == "agent"
        assert facts.nodes[0].label == "通用执行者"
        assert [t.tool_id for t in facts.tool_needs] == [
            "bash",
            "human_interaction",
        ]

    def test_agent_yaml_without_tool_ids(self):
        facts = graph_extract.extract_source(
            "config/agents/main/a.yaml", "name: 主控\n"
        )
        assert facts.tool_needs == []

    def test_mode_yaml_yields_mode_node(self):
        facts = graph_extract.extract_source(
            "plugins/shared/mode_coding/mode.yaml", "mode: coding\nname: 编码模式\n"
        )
        assert facts.kind == "mode"
        assert facts.nodes[0].id == "mode:coding"
        assert facts.nodes[0].label == "编码模式"

    def test_skill_md_yields_skill_node(self):
        facts = graph_extract.extract_source(
            "skills/alpha_skill/SKILL.md", "---\nname: Alpha 技能\n---\n正文"
        )
        assert facts.kind == "skill"
        assert facts.nodes[0].id == "skill:alpha_skill"

    def test_pipeline_yaml_yields_pipeline_node(self):
        facts = graph_extract.extract_source(
            "config/pipelines/auto.yaml", "name: 自动管道\n"
        )
        assert facts.kind == "pipeline"
        assert facts.nodes[0].id == "pipeline:auto"

    def test_doc_with_frontmatter_yields_node_link_meta(self):
        text = (
            "---\ndoc_id: 20261028-req\ntype: requirement\nstatus: active\n---\n"
            "见 [[20261028-design|真值|设计]]"
        )
        facts = graph_extract.extract_source("docs/req.md", text)
        assert facts.kind == "doc"
        assert facts.nodes[0].id == "20261028-req"
        assert facts.nodes[0].status == "active"
        assert facts.meta is not None
        assert (facts.meta.doc_id, facts.meta.type, facts.meta.status) == (
            "20261028-req",
            "requirement",
            "active",
        )
        assert [(l.target, l.relation) for l in facts.links] == [
            ("20261028-design", "真值")
        ]

    def test_doc_without_frontmatter_yields_path_id_and_no_meta(self):
        # 存量渐进补齐口径：无 frontmatter 仍入图（id=doc:<rel>），不产出校验元数据
        facts = graph_extract.extract_source("docs/legacy.md", "# 存量\n正文")
        assert facts.kind == "doc"
        assert facts.nodes[0].id == "doc:docs/legacy.md"
        assert facts.meta is None

    def test_long_doc_content_truncated(self):
        # 性质断言：内容截断保证数据文件"几 MB 级"（组件文档 CONTENT_MAX_CHARS）
        long_body = "x" * (graph_extract.CONTENT_MAX_CHARS * 3)
        facts = graph_extract.extract_source(
            "docs/legacy.md", long_body
        )
        assert len(facts.nodes[0].content) <= graph_extract.CONTENT_MAX_CHARS + 4

    def test_unknown_path_yields_empty_facts(self):
        # 非提取源（如普通源码）不产出节点——图只收清单与文档
        facts = graph_extract.extract_source("kernel/src/lib.rs", "fn main() {}")
        assert facts.kind == "" and facts.nodes == []


# ── 聚合层：多文件装配 → data + metas + dangling ─────────────────────


class TestBuildGraph:
    def test_code_half_nodes_and_belonging(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, metas, dangling = build_graph(root)
        ids = _node_ids(data)
        assert "proj" in ids
        assert {"plugin:alpha_service", "plugin:beta_service", "plugin:bash"} <= ids
        assert {"crate:alpha_c", "crate:beta_c"} <= ids
        assert "agent:general_agent" in ids
        assert "mode:coding" in ids
        assert "skill:alpha_skill" in ids
        assert "pipeline:auto" in ids
        # 归属边：代码半边节点 → proj（viewer 树骨干）
        belongs = {(e["s"], e["t"]) for e in _edges(data, gm.REL_BELONGS_TO)}
        assert ("plugin:alpha_service", "proj") in belongs
        assert ("crate:beta_c", "proj") in belongs

    def test_service_dependency_edge_prefix_match(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, _ = build_graph(root)
        dep_edges = {(e["s"], e["t"]) for e in _edges(data, gm.REL_DEPENDS_ON)}
        # requires_services "beta.svc" 首段匹配提供方 beta_service
        assert ("plugin:alpha_service", "plugin:beta_service") in dep_edges

    def test_tool_dependency_edge_via_provider(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, _ = build_graph(root)
        dep_edges = {(e["s"], e["t"]) for e in _edges(data, gm.REL_DEPENDS_ON)}
        # tool_ids bash → 提供方 bash 插件
        assert ("agent:general_agent", "plugin:bash") in dep_edges

    def test_crate_dependency_edge_internal_only(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, _ = build_graph(root)
        dep_edges = {(e["s"], e["t"]) for e in _edges(data, gm.REL_DEPENDS_ON)}
        assert ("crate:alpha_c", "crate:beta_c") in dep_edges
        # 外部 crate（serde）无节点 → 不生成边（依赖边不在校验三件内，不报悬空）
        assert not any(t == "serde" for _, t in dep_edges)

    def test_wikilink_resolution_doc_id_and_path(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, dangling = build_graph(root)
        edges = {
            (e["s"], e["t"], e["k"]) for e in data["edges"]
        }
        # doc_id 命中 doc 节点
        assert ("20261028-req-doc", "20261028-design-doc", "真值") in edges
        # 路径命中：目标文件本身是 plugin 节点源 → 连 plugin 节点
        assert (
            "20261028-req-doc",
            "plugin:beta_service",
            "依赖",
        ) in edges
        # 纯中文占位写法示例：忽略不报
        assert dangling == []

    def test_supersedes_relation_edge(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, _ = build_graph(root)
        sup = {(e["s"], e["t"]) for e in _edges(data, gm.REL_SUPERSEDES)}
        assert ("20261028-old-plan", "20261028-new-plan") in sup

    def test_dangling_doc_id_recorded(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        _write(
            root,
            "docs/broken.md",
            "---\ndoc_id: 20261028-broken\ntype: report\nstatus: active\n---\n"
            "引用 [[20261028-missing|依赖|x]]。\n",
        )
        data, _, dangling = build_graph(root)
        assert any("20261028-missing" in d for d in dangling)

    def test_unresolvable_path_reference_dangling(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        _write(
            root,
            "docs/broken2.md",
            "---\ndoc_id: 20261028-broken2\ntype: ledger\nstatus: active\n---\n"
            "见 [[docs/nope/missing.md|依据|x]]。\n",
        )
        _, _, dangling = build_graph(root)
        assert any("docs/nope/missing.md" in d for d in dangling)

    def test_build_idempotent(self, tmp_path):
        # 性质断言：同一输入聚合两次结果一致（装配无隐藏状态）
        root = tmp_path
        _make_repo(root)
        first = _without_meta(build_graph(root)[0])
        second = _without_meta(build_graph(root)[0])
        assert first == second

    def test_no_duplicate_edges(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, _, _ = build_graph(root)
        triples = [(e["s"], e["t"], e["k"]) for e in data["edges"]]
        assert len(triples) == len(set(triples))


# ── 落盘与增量：三合一入口 ────────────────────────────────────────────


class TestAggregate:
    def test_writes_graph_js_with_meta(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        out = root / "docs" / "graph" / "graph.js"
        state = root / "docs" / "graph" / ".graph_state.json"
        assert aggregate(root, out, state) == 0
        data = _load_graph_js(out)
        assert set(data) >= {"meta", "nodes", "edges"}
        assert "generated_at" in data["meta"]
        assert "commit" in data["meta"]  # tmp 无 git → 空串也必须有键

    def test_state_file_written(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        state = root / "docs" / "graph" / ".graph_state.json"
        aggregate(root, root / "g.js", state)
        assert state.exists()
        st = json.loads(state.read_text(encoding="utf-8"))
        assert "head" in st and "files" in st
        assert "docs/req.md" in st["files"]

    def test_incremental_matches_full_rebuild(self, tmp_path):
        # 增量正确性：改一个文件后再跑，结果必须与删 state 全量重建一致
        root = tmp_path
        _make_repo(root)
        out = root / "g.js"
        state = root / "s.json"
        assert aggregate(root, out, state) == 0

        _write(
            root,
            "docs/design.md",
            "---\ndoc_id: 20261028-design-doc\ntype: design\nstatus: active\n---\n"
            "# 设计\n\n新增 [[20261028-req-doc|依赖|回链]]。\n",
        )
        assert aggregate(root, out, state) == 0
        incremental = _load_graph_js(out)

        state.unlink()
        assert aggregate(root, out, state) == 0
        full = _load_graph_js(out)

        assert _without_meta(incremental) == _without_meta(full)
        # 新双链边在增量结果中可见（变更文件被重提取）
        edges = {
            (e["s"], e["t"], e["k"])
            for e in incremental["edges"]
        }
        assert ("20261028-design-doc", "20261028-req-doc", "依赖") in edges

    def test_deleted_file_drops_node(self, tmp_path):
        # 删除文件同步摘节点摘边（方案 §4.4 增量口径）
        root = tmp_path
        _make_repo(root)
        out = root / "g.js"
        state = root / "s.json"
        aggregate(root, out, state)
        before = _load_graph_js(out)
        assert "20261028-design-doc" in _node_ids(before)

        (root / "docs/design.md").unlink()
        # 剩余文档的双链指向已删节点 → 悬空照常判红；摘节点/摘边与校验红绿正交
        assert aggregate(root, out, state) == 1
        after = _load_graph_js(out)
        assert "20261028-design-doc" not in _node_ids(after)
        assert not [
            e for e in after["edges"] if "20261028-design-doc" in (e["s"], e["t"])
        ]

    def test_check_failure_exits_nonzero(self, tmp_path):
        # 校验红绿 = 门禁步骤本身：问题非空必须退出非 0（不静默）
        root = tmp_path
        _make_repo(root)
        _write(
            root,
            "docs/bad.md",
            "---\ndoc_id: bad_id\ntype: plan\nstatus: active\n---\n坏文档\n",
        )
        out = root / "g.js"
        state = root / "s.json"
        assert aggregate(root, out, state) == 1
        # 副产物仍落盘（校验与出图同一遍聚合）
        assert out.exists()

    def test_real_repo_aggregates_clean(self):
        # 真实链路兜底：对本仓跑全量聚合——数据契约与校验必须绿
        out = ROOT / "docs" / "graph" / "graph.js"
        state = ROOT / "docs" / "graph" / ".graph_state.json"
        assert aggregate(ROOT, out, state) == 0
        data = _load_graph_js(out)
        assert len(data["nodes"]) > 50, "真实仓节点量应在百级（600+ 文档 + 100+ 插件）"


# ── 校验层：机械执法（只执法有 frontmatter 的文档）───────────────────


def _meta(doc_id: str, type_: str, status: str, extra: tuple[str, ...] = ()) -> gm.DocMeta:
    return gm.DocMeta(doc_id=doc_id, type=type_, status=status, path="docs/x.md",
                      extra_fields=extra)


class TestCheckGraph:
    def test_clean_world_no_findings(self, tmp_path):
        root = tmp_path
        _make_repo(root)
        data, metas, dangling = build_graph(root)
        assert graph_check.check_graph(data, metas, dangling) == []

    @pytest.mark.parametrize(
        "meta,keyword",
        [
            (_meta("bad_id", "plan", "draft"), "doc_id"),
            (_meta("20261028-x", "rule", "draft"), "type"),
            (_meta("20261028-x", "plan", "active"), "status"),
            (_meta("20261028-x", "plan", "draft", extra=("domain",)), "domain"),
        ],
    )
    def test_schema_violations(self, meta, keyword, tmp_path):
        # 四类 schema 违例：doc_id 形态 / type 枚举 / status 枚举 / 字段白名单
        findings = graph_check.check_graph({"nodes": [], "edges": []}, [meta], [])
        assert any(keyword in f for f in findings), findings

    @pytest.mark.parametrize(
        "type_,status,ok",
        [
            ("plan", "draft", True),
            ("plan", "archived", True),
            ("plan", "active", False),
            ("plan", "superseded", False),
            ("plan", "deprecated", True),  # 墓碑件：plan 型文档废弃后留指针
            ("report", "deprecated", True),
            ("report", "active", True),
            ("report", "draft", False),
            ("requirement", "active", True),
            ("design", "superseded", True),
            # 2026-11-14 枚举扩展（文档治理方案 D1/D5）后的矩阵口径。
            # standard 的路径约束由 test_standard_must_live_in_standards_roots 单测
            # （本 parametrize 共用 _meta 的 docs/x.md 路径，不在此重测归属）。
            ("adr", "deprecated", False),  # ADR 退出语义 = superseded/archived
            ("design", "deprecated", True),
        ],
    )
    def test_type_status_matrix(self, type_, status, ok, tmp_path):
        # 类型规范 §六矩阵；superseded 的取代指向单独测
        meta = _meta("20261028-m", type_, status)
        edges = []
        if status == "superseded":
            edges = [{"s": "20261028-m", "t": "20261028-n", "k": gm.REL_SUPERSEDES}]
        data = {"nodes": [], "edges": edges}
        findings = graph_check.check_graph(data, [meta], [])
        assert (findings == []) is ok, findings

    def test_standard_must_live_in_standards_roots(self):
        """type=standard 的归宿只有 docs/guides/ 与 docs/schemas/（§二类型=链位）。"""
        inside = gm.DocMeta(
            doc_id="20261114-std", type="standard", status="active",
            path="docs/schemas/文档治理标准.md",
        )
        outside = gm.DocMeta(
            doc_id="20261114-std2", type="standard", status="active",
            path="docs/working/错位标准.md",
        )
        assert graph_check.check_graph({"nodes": [], "edges": []}, [inside], []) == []
        findings = graph_check.check_graph({"nodes": [], "edges": []}, [outside], [])
        assert any("type=standard" in f for f in findings), findings

    def test_adr_deprecated_rejected(self):
        """ADR 不得用 deprecated：退出语义只有被推翻(superseded)或停用(archived)。"""
        meta = _meta("20261028-old-adr", "adr", "deprecated")
        findings = graph_check.check_graph({"nodes": [], "edges": []}, [meta], [])
        assert any("deprecated" in f for f in findings), findings

    def test_superseded_requires_supersedes_link(self, tmp_path):
        meta = _meta("20261028-old", "plan", "superseded")
        findings = graph_check.check_graph({"nodes": [], "edges": []}, [meta], [])
        assert any("取代" in f for f in findings)

    def test_dangling_reported(self, tmp_path):
        findings = graph_check.check_graph(
            {"nodes": [], "edges": []}, [], ["docs/x.md: 双链目标 20261028-missing 未解析"]
        )
        assert any("20261028-missing" in f for f in findings)

    def test_doc_id_conflict_reported(self, tmp_path):
        m1 = gm.DocMeta(doc_id="20261028-dup", type="design", status="active",
                        path="docs/a.md")
        m2 = gm.DocMeta(doc_id="20261028-dup", type="design", status="active",
                        path="docs/b.md")
        findings = graph_check.check_graph({"nodes": [], "edges": []}, [m1, m2], [])
        assert any("20261028-dup" in f and ("冲突" in f or "b.md" in f) for f in findings)

    def test_meta_without_doc_id_ignored(self, tmp_path):
        # 无 doc_id 的 frontmatter（如 SKILL.md name/description）不在执法范围
        meta = gm.DocMeta(doc_id="", type="", status="", path="skills/s/SKILL.md")
        assert graph_check.check_graph({"nodes": [], "edges": []}, [meta], []) == []


class TestFailClosedInputs:
    """输入面残缺必须红（门禁不得退化成恒绿包装）。

    意图：门禁的红绿承诺是"文档面有没有问题"；根目录指错、空检出、docs/ 缺失
    三种形态下没有任何被执法者，此时"0 个问题"是假绿。故一律 fail-closed
    判红（退出码 2）且不产出数据文件（残留 graph.js 会被误当新鲜数据）。
    """

    def test_missing_root_fails_closed(self, tmp_path):
        out, state = tmp_path / "g.js", tmp_path / "s.json"
        assert aggregate(tmp_path / "no_such_root", out, state) == 2
        assert not out.exists(), "输入不可判定时不得产出数据文件"
        assert not state.exists()

    def test_empty_repo_fails_closed(self, tmp_path):
        # 空检出（稀疏/浅检出）→ 零提取源，不得放行
        out, state = tmp_path / "g.js", tmp_path / "s.json"
        assert aggregate(tmp_path, out, state) == 2
        assert not out.exists()

    def test_repo_without_docs_fails_closed(self, tmp_path):
        # 有源但文档面整体缺失（docs/ 被误删/指到非仓目录）→ 红
        _write(tmp_path, "plugins/shared/solo/plugin.json", json.dumps({"id": "solo"}))
        out, state = tmp_path / "g.js", tmp_path / "s.json"
        assert aggregate(tmp_path, out, state) == 2

    def test_param_error_is_nonzero(self):
        # 参数错误（argparse 退出 2）——不得被吞成绿
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "graph_aggregate.py"), "--bogus-arg"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2, f"参数错误必须非 0，实际 {proc.returncode}"

    def test_real_repo_guard_does_not_misfire(self):
        # 性质断言：守卫只拦"输入残缺"，不改变正常仓的判定语义——真实仓
        # 必须能走到校验阶段（0 绿 / 1 有 finding 都算走到了；2 = 守卫误判）。
        # 不断言 ==0：多会话并行仓的未跟踪残留可让校验合法变红（见 ADR 影响节）。
        root = Path(__file__).resolve().parents[2]
        out, state = root / ".zctmp" / "graph_guard_check.js", root / ".zctmp" / "graph_guard_state.json"
        assert aggregate(root, out, state) in (0, 1), "真实仓被守卫误判为输入残缺"


# ── 门禁挂载：run_gates fast 档 ───────────────────────────────────────


def test_run_gates_has_fast_graph_gate():
    import run_gates

    gates = [g for g in run_gates.GATES if g.id == "graph-check"]
    assert len(gates) == 1, "graph-check 门禁必须恰好挂载一次"
    g = gates[0]
    assert g.fast is True, "方案 §4.4：graph 步挂 fast 档"
    assert g.est_seconds is not None, "fast 门禁必须声明估时（V5）"
    cmd = g.shell if g.shell else " ".join(g.command)
    assert "graph_aggregate.py" in cmd, "三合一入口 = graph_aggregate.py"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
