# @feature: FP-0.2.三 宿主接入(打包布局) | @ci: python-coverage
"""strip_packaged_venvs 行为测试：随包 venv 出包剥离。

覆盖：_host 与平铺插件 .venv 全剥离 / 无 venv 幂等（退出 0）/ dry-run 只报告
不删除 / 资源根缺失退出 1。junction 摘点分支（Windows reparse 形态）不入单测
——测试环境跨平台无 mklink，该分支由 _is_link_dir 守卫承载（rmdir 摘点，
rmtree 禁穿透）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "strip_packaged_venvs", REPO / "scripts" / "strip_packaged_venvs.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
S = importlib.util.module_from_spec(_SPEC)
sys.modules["strip_packaged_venvs"] = S
_SPEC.loader.exec_module(S)

pytestmark = pytest.mark.unit


def _make_product(root: Path) -> Path:
    """最小产物：_host venv + 两个平铺插件 venv（含深层内容）。"""
    host_venv = root / "plugins" / "shared" / "_host" / ".venv" / "Scripts"
    host_venv.mkdir(parents=True)
    (host_venv / "python.exe").write_text("", encoding="utf-8")
    for name in ("bash", "trace_ingestion"):
        venv = root / "plugins" / "shared" / name / ".venv" / "Lib"
        venv.mkdir(parents=True)
        (venv / "x.txt").write_text("x", encoding="utf-8")
    return root


def test_strips_host_and_plugin_venvs(tmp_path):
    """_host 与平铺插件 .venv 全部剥离，退出码 0。"""
    root = _make_product(tmp_path / "res")

    assert S.main([str(root)]) == 0
    assert not (root / "plugins" / "shared" / "_host" / ".venv").exists()
    assert not (root / "plugins" / "shared" / "bash" / ".venv").exists()
    assert not (root / "plugins" / "shared" / "trace_ingestion" / ".venv").exists()
    # 插件本体目录不动（只删 .venv）
    assert (root / "plugins" / "shared" / "bash").is_dir()


def test_no_venvs_idempotent(tmp_path):
    """产物内无 .venv：退出 0（幂等，剥离后复跑形态）。"""
    root = tmp_path / "res"
    (root / "plugins" / "shared" / "_host").mkdir(parents=True)
    assert S.main([str(root)]) == 0


def test_dry_run_keeps_venvs(tmp_path):
    """--dry-run 只报告不删除。"""
    root = _make_product(tmp_path / "res")

    assert S.main([str(root), "--dry-run"]) == 0
    assert (root / "plugins" / "shared" / "_host" / ".venv" / "Scripts" / "python.exe").is_file()
    assert (root / "plugins" / "shared" / "bash" / ".venv" / "Lib" / "x.txt").is_file()


def test_missing_root_fails(tmp_path):
    """资源根缺失：退出 1（fail-closed，不得静默放过）。"""
    assert S.main([str(tmp_path / "nope")]) == 1


def test_link_dir_removed_without_traversal(tmp_path, monkeypatch):
    """junction/symlink 形态：只摘点不穿透（monkeypatch 注入链接判定，验 rmdir 路径）。"""
    root = tmp_path / "res"
    target = tmp_path / "real_venv"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    junction_dir = root / "plugins" / "shared" / "_host" / ".venv"
    junction_dir.mkdir(parents=True)  # 普通目录冒充链接位
    (junction_dir / "inner.txt").write_text("x", encoding="utf-8")  # 非空：rmdir 必败
    monkeypatch.setattr(S, "_is_link_dir", lambda _path: True)

    removed, failures = S.strip_venvs(root, dry_run=False)
    # 非空目录 rmdir 报错 → 记失败不穿透；目录原样保留（未走 rmtree），目标树不受影响
    assert failures
    assert not removed
    assert (junction_dir / "inner.txt").is_file()
    assert (target / "keep.txt").is_file()
