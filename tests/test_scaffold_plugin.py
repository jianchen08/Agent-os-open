# @feature: FP-0.2.dev_toolkit | @ci: python-coverage
"""scaffold_plugin.py 单测：工具四件套生成 / 管道模板渲染 / 拒绝面。

行为契约（断输入→输出）：
- tool kind 生成完整可跑目录，plugin.json 合法且工具名=id，实现可直接导入调用；
- core/input/output kind 按仓内真实模板渲染，占位符零残留；
- 拒绝面：非法 id、kind 越界、目标已存在、模板缺失、模板占位符不匹配，全部
  ScaffoldError（CLI 转退出码 1），不落半成品目录。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "skill-dev-toolkit" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("scaffold_plugin", SCRIPTS_DIR / "scaffold_plugin.py")
scaffold_plugin = importlib.util.module_from_spec(_spec)
sys.modules["scaffold_plugin"] = scaffold_plugin
_spec.loader.exec_module(scaffold_plugin)

pytestmark = pytest.mark.unit


def _load_impl(path: Path, plugin_id: str):
    spec = importlib.util.spec_from_file_location(f"scaffold_impl_{plugin_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_plugin_end_to_end(tmp_path: Path) -> None:
    created = scaffold_plugin.scaffold_plugin(
        REPO_ROOT, tmp_path, "sample_tool", "tool", "样例工具"
    )
    assert sorted(p.name for p in created) == [
        "plugin.json",
        "sample_tool.py",
        "server.py",
        "test_sample_tool.py",
    ]
    manifest = json.loads((tmp_path / "sample_tool" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "sample_tool"
    assert manifest["capabilities"]["tools"][0]["name"] == "sample_tool"
    for path in created:
        assert "$" not in path.read_text(encoding="utf-8"), f"占位符残留: {path}"
    impl = _load_impl(tmp_path / "sample_tool" / "sample_tool.py", "sample_tool")
    assert impl.sample_tool() == {"message": "hello from sample_tool"}
    assert impl.SAMPLE_TOOL_INPUT_SCHEMA["type"] == "object"


def test_pipeline_core_renders_real_template(tmp_path: Path) -> None:
    created = scaffold_plugin.scaffold_plugin(
        REPO_ROOT, tmp_path, "sample_core", "core", "样例核心"
    )
    content = created[0].read_text(encoding="utf-8")
    assert "{plugin_name}" not in content
    assert "{{" not in content
    assert "class SampleCore(ICorePlugin):" in content
    assert "样例核心" in content


@pytest.mark.parametrize("bad_id", ["Foo", "1abc", "hello-pack", "a b"])
def test_refuses_bad_plugin_ids(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(scaffold_plugin.ScaffoldError):
        scaffold_plugin.scaffold_plugin(REPO_ROOT, tmp_path, bad_id, "tool", "x")


def test_refuses_existing_target(tmp_path: Path) -> None:
    scaffold_plugin.scaffold_plugin(REPO_ROOT, tmp_path, "dup_tool", "tool", "x")
    with pytest.raises(scaffold_plugin.ScaffoldError, match="已存在"):
        scaffold_plugin.scaffold_plugin(REPO_ROOT, tmp_path, "dup_tool", "tool", "x")


def test_refuses_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(scaffold_plugin.ScaffoldError, match="kind"):
        scaffold_plugin.scaffold_plugin(REPO_ROOT, tmp_path, "odd_tool", "widget", "x")


def test_pipeline_template_missing(tmp_path: Path) -> None:
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()
    with pytest.raises(scaffold_plugin.ScaffoldError, match="模板缺失"):
        scaffold_plugin.scaffold_plugin(empty_repo, tmp_path, "orphan_core", "input", "x")


def test_pipeline_bad_placeholder_wrapped(tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake_repo"
    template_dir = fake_repo / "config" / "templates" / "plugin_scaffold"
    template_dir.mkdir(parents=True)
    (template_dir / "output_plugin.py").write_text(
        '"""{plugin_name} {unknown_key}"""\n', encoding="utf-8"
    )
    with pytest.raises(scaffold_plugin.ScaffoldError, match="占位符"):
        scaffold_plugin.scaffold_plugin(fake_repo, tmp_path, "bad_core", "output", "x")


def test_cli_success_and_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "out"
    rc = scaffold_plugin.main(
        ["cli_tool", "--one-line", "CLI 样例", "--target", str(target)]
    )
    assert rc == 0
    assert (target / "cli_tool" / "plugin.json").is_file()
    assert "后续必做" in capsys.readouterr().out

    rc_dup = scaffold_plugin.main(
        ["cli_tool", "--one-line", "CLI 样例", "--target", str(target)]
    )
    assert rc_dup == 1
    assert "脚手架失败" in capsys.readouterr().err


def test_default_repo_root_has_scaffold_templates() -> None:
    assert (scaffold_plugin.default_repo_root() / "config" / "templates" / "plugin_scaffold").is_dir()
