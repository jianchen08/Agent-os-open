# @feature: FP-GATE graph 全景图 | @ci: graph-check
"""graph_aggregate.discover_files 模板件豁免单测（2026-11-17 doc_id 冲突实证）。

行为契约（断输入→输出，不钉实现）：
- 文件名含字面量 ``{{task_id}}`` 的 docs md 是任务书派发模板形态，不入提取源
  （模板 doc_id 与派生实例 doc_id 相撞会触发 graph-check doc_id 冲突红）；
- 替换后真实实例名（``<task_id>_后缀``）照常收集；
- 非 docs 面（plugin.json 等）不受豁免影响（路径本不含标记，行为不变）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import graph_aggregate  # noqa: E402

pytestmark = pytest.mark.unit


def test_template_named_doc_is_not_a_source(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "working"
    docs.mkdir(parents=True)
    (docs / "{{task_id}}_任务现场入口交互方案.md").write_text("---\ndoc_id: 20261117-x\ntype: plan\nstatus: draft\n---\n", encoding="utf-8")
    (docs / "abc123def_任务现场入口交互方案.md").write_text("---\ndoc_id: 20261117-x\ntype: plan\nstatus: draft\n---\n", encoding="utf-8")

    found = graph_aggregate.discover_files(tmp_path)

    assert any("abc123def_任务现场入口交互方案.md" in rel for rel in found), "真实实例必须收集"
    assert not any("{{task_id}}" in rel for rel in found), "字面量模板件必须豁免"


def test_non_doc_faces_unaffected_by_marker(tmp_path: Path) -> None:
    plugin = tmp_path / "plugins" / "shared" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text("{}", encoding="utf-8")

    found = graph_aggregate.discover_files(tmp_path)

    assert "plugins/shared/demo/plugin.json" in found
