# @feature: FP-0.2.CFG 配置读写单源化 | @ci: python-test
"""check_plugin_config_write_surface.py 单测（2026-09-28 方案批次 E1 防回潮）。

行为契约（断输入→输出，不钉实现）：
- 同文件同时命中「配置面路径字面量 + 写动作」→ 违规；
- 豁免标记（config-write-surface-exempt: 理由）→ 放行；
- 只有读（无写动作）或只有写（无配置路径字面量）→ 不违规（避免误伤
  合法读面与用户数据/工作区写面）；
- 测试文件与排除目录（.venv/node_modules/target 等）不入扫；
- 负控制：恒真匹配器必须被构造反例打红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_plugin_config_write_surface", SCRIPTS / "check_plugin_config_write_surface.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_config_literal_plus_write_flags_violation(tmp_path: Path) -> None:
    bad = tmp_path / "bad_plugin"
    bad.mkdir()
    target = bad / "plugin.py"
    target.write_text(
        'import yaml\nPATH = "config/plugins/bad/x.yaml"\nyaml.safe_dump({"a": 1})\n',
        encoding="utf-8",
    )

    bad_flag, _note = mod.check_file(target)

    assert bad_flag is True


def test_exempt_marker_passes_with_reason(tmp_path: Path) -> None:
    bad = tmp_path / "bad_plugin"
    bad.mkdir()
    target = bad / "plugin.py"
    target.write_text(
        "# config-write-surface-exempt: 真值在用户空间，字面量为不可用回落防御\n"
        'import yaml\nPATH = "config/plugins/bad/x.yaml"\nyaml.safe_dump({"a": 1})\n',
        encoding="utf-8",
    )

    bad_flag, note = mod.check_file(target)

    assert bad_flag is False
    assert "豁免" in note


def test_read_only_config_literal_does_not_flag(tmp_path: Path) -> None:
    """只读不违规（合法读面：context_build/evaluation 等不误伤）。"""
    ok = tmp_path / "reader"
    ok.mkdir()
    target = ok / "reader.py"
    target.write_text(
        'DATA = "config/plugins/reader/rules.yaml"\n',
        encoding="utf-8",
    )

    bad_flag, _note = mod.check_file(target)

    assert bad_flag is False


def test_workspace_write_without_config_literal_does_not_flag(tmp_path: Path) -> None:
    """用户数据/工作区写面（无配置路径字面量）不违规。"""
    ok = tmp_path / "writer"
    ok.mkdir()
    target = ok / "writer.py"
    target.write_text(
        'from pathlib import Path\nPath(ws) / "out.txt".write_text("x")\n',
        encoding="utf-8",
    )

    bad_flag, _note = mod.check_file(target)

    assert bad_flag is False


def test_test_files_and_venv_dirs_excluded_from_scan(tmp_path: Path) -> None:
    (tmp_path / "plugin_a").mkdir()
    (tmp_path / "plugin_a" / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "plugin_a" / "test_plugin.py").write_text('open("config/plugins/t", "w")\n', encoding="utf-8")
    venv = tmp_path / "plugin_a" / ".venv"
    venv.mkdir()
    (venv / "leak.py").write_text('open("config/plugins/t", "w")\n', encoding="utf-8")

    files = mod.iter_plugin_py_files(tmp_path)

    assert [f.name for f in files] == ["plugin.py"]
