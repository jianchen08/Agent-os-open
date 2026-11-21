# @feature: FP-GATE 文档图化管线 | @ci: python-coverage
"""backlinks.json 反链投影测试：边集反转 → 反链侧栏数据源。

意图：验证 graph_aggregate 在既有三合一（聚合 → graph.js → 校验）之外多落一份
**精简反链投影**的可观察行为——不新增扫描（复用同一遍装配的边集）、doc_id 与
路径两种双链目标收敛到同一节点 id、悬空/占位不进索引、graph.js 三键契约不变。
全部走 tmp 真实文件系统（真实链路，非 mock）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_scripts = str(ROOT / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

from graph_aggregate import aggregate, build_backlinks  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _make_repo(root: Path) -> None:
    """微型仓库：A 分别以 doc_id 形态与路径形态引用 B，以路径引用无 frontmatter 的 C。"""
    _write(
        root,
        "docs/a.md",
        "---\ndoc_id: 20261121-a-doc\ntype: report\nstatus: active\n---\n"
        "# A\n\n真值见 [[20261121-b-doc|依据|设计真值]]，同件以路径引用 [[docs/b.md|取代|别名形态]]。\n"
        "清单见 [[plugins/shared/beta/plugin.json|依赖|清单文件]]。\n"
        "脚本面 [[scripts/helper.py|依据|脚本引用]]。占位 [[目标|关系|说明]] 不算引用。\n",
    )
    _write(
        root,
        "docs/b.md",
        "---\ndoc_id: 20261121-b-doc\ntype: design\nstatus: active\n---\n# B\n",
    )
    _write(root, "docs/c.md", "# C\n\n没有 frontmatter。\n")
    _write(root, "scripts/helper.py", "# helper\n")
    _write(
        root,
        "plugins/shared/beta/plugin.json",
        json.dumps({"id": "beta", "name": "Beta", "capabilities": {"services": [], "tools": []}}),
    )


def _run_aggregate(root: Path, tmp_path: Path) -> tuple[Path, Path, int]:
    out = tmp_path / "out" / "graph.js"
    state = tmp_path / "out" / ".graph_state.json"
    code = aggregate(root, out, state)
    return out, out.parent / "backlinks.json", code


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_graph_js(path: Path) -> dict:
    """graph.js 是 ``window.GRAPH_DATA = {...};`` 赋值形态（file:// 可开），非裸 JSON。"""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("window.GRAPH_DATA"), "graph.js 必须是赋值形态（file:// 可开）"
    return json.loads(text.split("=", 1)[1].strip().rstrip(";\n"))


class TestBuildBacklinks:
    def test_edges_reversed_grouped_by_target_with_source_facts(self):
        """边集反转：by_target 按目标节点 id 分组，条目带源节点 label/path 与关系/说明。"""
        data = {
            "meta": {"generated_at": "t", "commit": ""},
            "nodes": [
                {"id": "a-doc", "type": "doc", "label": "A 报告", "path": "docs/a.md",
                 "status": "active", "content": "", "owner": ""},
                {"id": "b-doc", "type": "doc", "label": "B 设计", "path": "docs/b.md",
                 "status": "active", "content": "", "owner": ""},
            ],
            "edges": [
                {"s": "a-doc", "t": "b-doc", "k": "依据", "note": "设计真值", "owner": ""},
                {"s": "a-doc", "t": "b-doc", "k": "取代", "note": "", "owner": ""},
            ],
        }
        bl = build_backlinks(data)
        entries = bl["by_target"]["b-doc"]
        assert [e["k"] for e in entries] == ["依据", "取代"]  # 排序确定性：(s,k,note)
        assert entries[0]["s"] == "a-doc"
        assert entries[0]["label"] == "A 报告"
        assert entries[0]["path"] == "docs/a.md"
        assert entries[0]["note"] == "设计真值"
        assert bl["path_to_id"] == {"docs/a.md": "a-doc", "docs/b.md": "b-doc"}
        # id_to_path 反查：doc_id → 真实仓库路径（前端 doc_id 形态双链点击解析的唯一真值，
        # 渲染层禁再发明 docs/decisions/ 目录约定——审查 MF-1 修复面）
        assert bl["id_to_path"]["b-doc"] == "docs/b.md"
        assert bl["id_to_path"]["a-doc"] == "docs/a.md"

    def test_empty_edges_yield_empty_projection(self):
        data = {"meta": {}, "nodes": [], "edges": []}
        bl = build_backlinks(data)
        assert bl["by_target"] == {}
        assert bl["path_to_id"] == {}
        assert bl["id_to_path"] == {}

    def test_projection_has_no_content_field(self):
        """投影精简契约：条目与 path_to_id 都不带正文——反链数据面不搬运 4KB 正文。"""
        data = {
            "meta": {},
            "nodes": [{"id": "d", "type": "doc", "label": "D", "path": "docs/d.md",
                       "status": "active", "content": "x" * 4000, "owner": ""}],
            "edges": [{"s": "d", "t": "proj", "k": "归属", "note": "", "owner": ""}],
        }
        bl = build_backlinks(data)
        assert all("content" not in json.dumps(v) for v in bl["by_target"].values())


class TestAggregateWritesBacklinks:
    def test_backlinks_file_written_alongside_graph_js(self, tmp_path):
        root = tmp_path / "repo"
        _make_repo(root)
        out, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 0
        assert backlinks.is_file()
        graph = _load_graph_js(out)
        assert set(graph.keys()) == {"meta", "nodes", "edges"}  # graph.js 三键契约不变
        bl = _load_json(backlinks)
        assert set(bl.keys()) == {"meta", "by_target", "path_to_id", "id_to_path"}
        # id_to_path 反查随装配产出：doc_id → 真实路径（前端 doc_id 双链点击解析唯一真值）
        assert bl["id_to_path"]["20261121-b-doc"] == "docs/b.md"

    def test_doc_id_and_path_forms_converge_to_same_node(self, tmp_path):
        """[[doc_id]] 与 [[docs/b.md]] 两种写法解析到同一 doc 节点——反链按节点 id 收敛。"""
        root = tmp_path / "repo"
        _make_repo(root)
        _, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 0
        bl = _load_json(backlinks)
        assert bl["path_to_id"]["docs/b.md"] == "20261121-b-doc"
        entries = bl["by_target"]["20261121-b-doc"]
        assert len(entries) == 2
        assert {e["k"] for e in entries} == {"依据", "取代"}
        assert all(e["s"] == "20261121-a-doc" for e in entries)
        # 源节点事实随条目携带：前端侧栏显示来源文档名并可点击跳转
        # （label = 首个标题，graph_extract._extract_doc 契约）
        assert entries[0]["label"] == "A"
        assert entries[0]["path"] == "docs/a.md"

    def test_non_doc_file_target_lands_as_path_key(self, tmp_path):
        root = tmp_path / "repo"
        _make_repo(root)
        _, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 0
        bl = _load_json(backlinks)
        # 非 doc 面 file 目标（提取源之外的脚本文件）：file 节点 id = 仓库相对路径
        assert bl["path_to_id"]["scripts/helper.py"] == "scripts/helper.py"
        assert [e["k"] for e in bl["by_target"]["scripts/helper.py"]] == ["依据"]

    def test_code_target_indexed(self, tmp_path):
        root = tmp_path / "repo"
        _make_repo(root)
        _, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 0
        bl = _load_json(backlinks)
        assert bl["path_to_id"]["plugins/shared/beta/plugin.json"] == "plugin:beta"
        assert [e["k"] for e in bl["by_target"]["plugin:beta"]] == ["依赖"]

    def test_dangling_and_placeholder_not_indexed(self, tmp_path):
        """悬空目标（未解析）与占位写法示例都不进反链——投影只承载真实边。

        悬空引用触发校验红（退出码 1），backlinks 作为副产物仍落盘（与 graph.js
        "图永远有新鲜数据，红绿由退出码表达"同口径）。
        """
        root = tmp_path / "repo"
        _make_repo(root)
        p = root / "docs" / "a.md"
        p.write_text(
            p.read_text(encoding="utf-8") + "悬空 [[20260101-nonexistent|依赖|不存在]]。\n",
            encoding="utf-8",
        )
        _, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 1
        bl = _load_json(backlinks)
        assert "20260101-nonexistent" not in bl["by_target"]
        assert not [k for k in bl["by_target"] if "目标" in k]
        assert "20260101-nonexistent" not in bl["path_to_id"]

    def test_red_input_writes_no_backlinks(self, tmp_path):
        """输入不可判定（空仓）fail-closed：不产出数据文件（与 graph.js 同口径）。"""
        root = tmp_path / "empty"
        root.mkdir()
        out, backlinks, code = _run_aggregate(root, tmp_path)
        assert code == 2
        assert not out.exists()
        assert not backlinks.exists()
