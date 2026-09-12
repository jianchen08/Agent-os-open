# @feature: FP-DB kernel_db | @vision: V3 可嵌入 | @ci: python-coverage
"""kernel_db 库路径解析测试：env 相对路径锚定项目根（与内核 storage_factory
同规则，两端 CWD 分叉不致读写不同库）+ 绝对路径/:memory: 原样保留 + 缺省兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

import kernel_db  # noqa: E402

pytestmark = pytest.mark.unit


def _project_root_via_public_api(monkeypatch: pytest.MonkeyPatch) -> Path:
    """经公共 API 推导项目根（缺省兜底 = 项目根/agentos_kernel.db）。

    不断言私有 _project_root——锚点从 kernel_db_path 自身的缺省行为推导。
    """
    monkeypatch.delenv("AGENTOS_DB_PATH", raising=False)
    return kernel_db.kernel_db_path().parent


def test_relative_env_path_anchors_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 相对路径 → 以项目根为锚解析并绝对化。"""
    project_root = _project_root_via_public_api(monkeypatch)
    monkeypatch.setenv("AGENTOS_DB_PATH", "data/sub/rel.db")
    got = kernel_db.kernel_db_path()
    expected = (project_root / "data" / "sub" / "rel.db").resolve()
    assert got.is_absolute(), "解析后必须绝对化（与进程 CWD 无关）"
    assert got == expected


def test_relative_env_path_independent_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一相对 env 路径在不同 CWD 下解析到同一物理库（双端分叉回归）。"""
    monkeypatch.setenv("AGENTOS_DB_PATH", "anchored/kernel.db")
    from_cwd = kernel_db.kernel_db_path()
    monkeypatch.chdir(tmp_path)
    from_other_cwd = kernel_db.kernel_db_path()
    assert from_cwd == from_other_cwd, "锚定项目根后与 CWD 无关"


def test_absolute_env_path_passthrough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env 绝对路径原样保留，不重锚定。"""
    abs_path = tmp_path / "elsewhere.db"
    monkeypatch.setenv("AGENTOS_DB_PATH", str(abs_path))
    assert kernel_db.kernel_db_path() == abs_path


def test_memory_alias_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """:memory: 别名原样保留（内核内存库约定）。"""
    monkeypatch.setenv("AGENTOS_DB_PATH", ":memory:")
    assert kernel_db.kernel_db_path() == Path(":memory:")


def test_no_env_defaults_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 未设置 → 项目根 agentos_kernel.db（父目录满足项目根目录指纹）。"""
    monkeypatch.delenv("AGENTOS_DB_PATH", raising=False)
    got = kernel_db.kernel_db_path()
    assert got.name == "agentos_kernel.db"
    assert (got.parent / "config").is_dir(), "锚点目录应满足项目根指纹（config/）"
    assert (got.parent / "config" / "models").is_dir(), "项目根指纹（config/models/）"
