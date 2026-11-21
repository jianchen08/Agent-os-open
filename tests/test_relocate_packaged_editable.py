# @feature: FP-0.2.三 宿主接入(打包布局) | @ci: python-coverage
"""relocate_packaged_editable 行为测试：打包后 editable 指向重定位。

覆盖（2026-11-12 第四犯后契约）：plain .pth 绝对路径行 → 相对 site-packages 的
相对路径行（包外路径经锚点重挂、包内打包位绝对路径原值相对化——两者装后都能
解析）/ finder 绝对字面量 fail-closed / 锚点目标缺失 fail-closed（退出码 1）/
相对行与 import 行不动 / dry-run 不写盘。

注：pyvenv.cfg home 不属本脚本职责（随包 venv 无防挪位形态，由
strip_packaged_venvs 剥离 + 装后 autoprovision 自建），home 面判据见
test_check_packaged_layout.py。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "relocate_packaged_editable", REPO / "scripts" / "relocate_packaged_editable.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
R = importlib.util.module_from_spec(_SPEC)
sys.modules["relocate_packaged_editable"] = R
_SPEC.loader.exec_module(R)

pytestmark = pytest.mark.unit

DEV_ROOT = Path(r"D:\buildmachine\legacy-repo")


def _make_package(root: Path) -> Path:
    """最小包结构：SDK 源码 + 共享 venv site-packages。"""
    sdk = root / "plugins" / "sdk" / "src"
    sdk.mkdir(parents=True)
    (sdk / "agentos_plugin_sdk").mkdir()
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    return sp


def test_relocate_plain_pth(tmp_path):
    """plain .pth 的开发机绝对路径行 → 相对 site-packages 的相对路径行。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.agentos_plugin_sdk-0.2.2.pth"
    target = DEV_ROOT / "plugins" / "sdk" / "src"
    pth.write_text(str(target), encoding="utf-8")

    assert R.main([str(root)]) == 0
    relative = os.path.relpath(root / "plugins" / "sdk" / "src", sp)
    assert pth.read_text(encoding="utf-8") == relative
    assert not Path(relative).is_absolute()
    assert (sp / relative).resolve() == (root / "plugins" / "sdk" / "src").resolve()


def test_in_package_absolute_relativized(tmp_path):
    """打包位绝对路径（第四犯形态）同样相对化——打包机上「原地对」不是合格态。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.x.pth"
    staging = root / "plugins" / "sdk" / "src"
    pth.write_text(str(staging), encoding="utf-8")

    assert R.main([str(root)]) == 0
    relative = os.path.relpath(staging, sp)
    assert pth.read_text(encoding="utf-8") == relative


def test_relative_and_import_lines_untouched(tmp_path):
    """相对路径行（重定位后形态）与 import 行不改写（幂等）。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.sdk.pth"
    relative = os.path.relpath(root / "plugins" / "sdk" / "src", sp)
    pth.write_text(relative, encoding="utf-8")

    assert R.main([str(root)]) == 0
    assert pth.read_text(encoding="utf-8") == relative


def test_in_package_missing_target_fails_closed(tmp_path):
    """包内绝对路径指向不存在的目标 → 未解决，退出码 1（布局门禁兜底拦截）。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.ghost.pth"
    pth.write_text(str(root / "plugins" / "sdk" / "absent"), encoding="utf-8")

    assert R.main([str(root)]) == 1
    assert "absent" in pth.read_text(encoding="utf-8")


def test_finder_absolute_literal_fails_closed(tmp_path):
    """finder 模块里的绝对字面量无可挪位改写形式 → fail-closed，退出码 1。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    finder = sp / "__editable___agentos_builtin_tools_0_2_0_finder.py"
    dev_pkg = DEV_ROOT / "plugins" / "shared" / "builtin_tools" / "agentos_builtin_tools"
    escaped = str(dev_pkg).replace("\\", "\\\\")
    original = f"MAPPING = {{'agentos_builtin_tools': '{escaped}'}}\n"
    finder.write_text(original, encoding="utf-8")

    assert R.main([str(root)]) == 1
    assert finder.read_text(encoding="utf-8") == original


def test_unresolvable_target_fails_closed(tmp_path):
    """锚点后的目标在包内不存在 → 未解决，退出码 1（由布局门禁兜底拦截）。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.missing.pth"
    pth.write_text(str(DEV_ROOT / "plugins" / "nowhere" / "src"), encoding="utf-8")

    assert R.main([str(root)]) == 1
    assert str(DEV_ROOT) in pth.read_text(encoding="utf-8")


def test_dry_run_does_not_write(tmp_path):
    """--dry-run 只报告不写盘。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.sdk.pth"
    original = str(DEV_ROOT / "plugins" / "sdk" / "src")
    pth.write_text(original, encoding="utf-8")

    assert R.main([str(root), "--dry-run"]) == 0
    assert pth.read_text(encoding="utf-8") == original


def test_import_line_ignored(tmp_path):
    """.pth 的 import 行不是路径行，不得被改写。"""
    root = tmp_path / "res"
    sp = _make_package(root)
    pth = sp / "__editable__.finder_style.pth"
    line = "import __editable___x_finder; __editable___x_finder.install()"
    pth.write_text(line, encoding="utf-8")

    assert R.main([str(root)]) == 0
    assert pth.read_text(encoding="utf-8") == line
