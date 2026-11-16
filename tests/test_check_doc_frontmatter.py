# @feature: FP-0.2.〇 文档 frontmatter 强制闸 | @ci: python-coverage
"""P-01 执法器单测（提案 E2-1 验收判据：新增无 fm 文档的提交被红；存量不回填）。

不依赖真实 git 变更集——check() 纯函数全覆盖，main() 决策路径以 monkeypatch
注入已知变更集驱动。模块级 ROOT 重定向到 tmp_path 后按相对 posix 路径读文件。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_frontmatter.py"
_spec = importlib.util.spec_from_file_location("check_doc_frontmatter", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_doc_frontmatter"] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]

FULL_FM = (
    "---\ndoc_id: 20261030-demo-doc\ntype: design\nstatus: draft\n---\n\n# 正文\n"
)


def _doc(tmp_path: Path, rel: str, body: str) -> str:
    """写临时文档并把检查器 ROOT 重定向过去；返回 posix 相对路径。"""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    mod.ROOT = tmp_path
    return rel


def test_new_doc_without_frontmatter_is_red(tmp_path: Path) -> None:
    rel = _doc(tmp_path, "docs/working/无fm.md", "# 裸文档\n")
    assert "缺 frontmatter" in mod.check(rel)


def test_new_doc_with_valid_frontmatter_is_green(tmp_path: Path) -> None:
    rel = _doc(tmp_path, "docs/working/有fm.md", FULL_FM)
    assert mod.check(rel) is None


def test_new_doc_with_invalid_enum_is_red(tmp_path: Path) -> None:
    rel = _doc(
        tmp_path, "docs/working/坏枚举.md",
        "---\ndoc_id: bad_id\ntype: novel\nstatus: frozen\n---\n正文\n",
    )
    err = mod.check(rel)
    assert err and "doc_id 形态非法" in err and "type 非法" in err and "status 非法" in err


def test_scope_boundaries() -> None:
    """docs/**.md 在执法面；ADR 目录豁免；仓内其余 .md 与非 md 不涉。"""
    assert mod.in_scope("docs/working/some-ledger.md")
    assert not mod.in_scope("docs/decisions/2026-10-30-some-adr.md")
    assert not mod.in_scope("skills/code-implement/SKILL.md")
    assert not mod.in_scope("docs/notes/rst.rst")


def test_main_decision_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """聚合决策：无候选绿；候选含违规红；全部合规绿。"""
    monkeypatch.setattr(mod, "staged_new", lambda: set())
    monkeypatch.setattr(mod, "range_new", lambda base: set())
    assert mod.main([]) == 0

    bad = _doc(tmp_path, "docs/working/x.md", "# 裸\n")
    good = _doc(tmp_path, "docs/working/y.md", FULL_FM)
    monkeypatch.setattr(mod, "staged_new", lambda: {bad, good})
    assert mod.main([]) == 1
    assert "缺 frontmatter" in capsys.readouterr().out

    monkeypatch.setattr(mod, "staged_new", lambda: {good})
    assert mod.main([]) == 0
    assert "全部合规" in capsys.readouterr().out
