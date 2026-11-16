# @feature: FP-0.2.二 隔离 工作空间路径布局 | @ci: python-coverage
"""ensure_isolation_path 双布局注入契约（拍平 ADR 2026-10-13 回归锚）。

isolation 包内模块用裸名兄弟导入（``from _workspace_git_ops import …``），
包目录必须在 sys.path。拍平后共享根下 isolation/ 直属，旧实现只注入
system/isolation（拍平树不存在）→ 工作空间服务实例化必炸（装机
No module named '_workspace_git_ops' 实证）。本文件锁双布局行为。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_REAL_SOURCE = (_SHARED_DIR / "isolation_path.py").read_text(encoding="utf-8")


def _load_from(source_text: str, root: Path, module_name: str):
    """把 isolation_path 源码落到 tmp 布局根并加载为独立模块。"""
    (root / "isolation_path.py").write_text(source_text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, root / "isolation_path.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def _restore_sys_path():
    saved = list(sys.path)
    yield
    sys.path[:] = saved


def _write_pkg(dir_path: Path, init: str = "") -> None:
    dir_path.mkdir(parents=True)
    (dir_path / "__init__.py").write_text(init, encoding="utf-8")


def test_flat_layout_adds_isolation_dir(tmp_path, _restore_sys_path):
    """拍平布局：注入 <根>/isolation，裸名兄弟导入可达（装机回归锚）。"""
    iso = tmp_path / "isolation"
    _write_pkg(iso)
    (iso / "_workspace_git_ops.py").write_text("MARKER = 'flat'\n", encoding="utf-8")
    mod = _load_from(_REAL_SOURCE, tmp_path, "iso_path_flat_case")

    mod.ensure_isolation_path()

    assert str(iso) in sys.path
    spec = importlib.util.find_spec("_workspace_git_ops")
    assert spec is not None
    assert Path(spec.origin).parent == iso


def test_legacy_layout_adds_system_chain(tmp_path, _restore_sys_path):
    """旧嵌套布局：注入 system/ 与 system/isolation，不注入不存在的平铺位。"""
    system = tmp_path / "system"
    iso = system / "isolation"
    _write_pkg(iso)
    (iso / "_workspace_git_ops.py").write_text("MARKER = 'legacy'\n", encoding="utf-8")
    mod = _load_from(_REAL_SOURCE, tmp_path, "iso_path_legacy_case")

    mod.ensure_isolation_path()

    assert str(system) in sys.path
    assert str(iso) in sys.path
    assert str(tmp_path / "isolation") not in sys.path
    spec = importlib.util.find_spec("_workspace_git_ops")
    assert spec is not None
    assert Path(spec.origin).parent == iso


def test_real_repo_layout_resolves_sibling_import(_restore_sys_path):
    """真实仓（拍平）：调用后仓内 isolation 包目录入列，兄弟模块 spec 可解析。"""
    spec = importlib.util.spec_from_file_location(
        "iso_path_real_case", _SHARED_DIR / "isolation_path.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.ensure_isolation_path()

    real_iso = _SHARED_DIR / "isolation"
    assert real_iso.is_dir()
    assert str(real_iso) in sys.path
    spec = importlib.util.find_spec("_workspace_git_ops")
    assert spec is not None
    assert Path(spec.origin).parent == real_iso


def test_idempotent_no_duplicate_entries(tmp_path, _restore_sys_path):
    """幂等：重复调用不产生重复 sys.path 条目（原契约保持）。"""
    iso = tmp_path / "isolation"
    _write_pkg(iso)
    mod = _load_from(_REAL_SOURCE, tmp_path, "iso_path_idem_case")

    mod.ensure_isolation_path()
    mod.ensure_isolation_path()

    assert sys.path.count(str(iso)) == 1


def test_nonexistent_layout_adds_nothing(tmp_path, _restore_sys_path):
    """无 isolation 目录的裸根：零注入（不塞不存在路径进 sys.path）。"""
    mod = _load_from(_REAL_SOURCE, tmp_path, "iso_path_empty_case")
    before = list(sys.path)

    mod.ensure_isolation_path()

    assert sys.path == before
