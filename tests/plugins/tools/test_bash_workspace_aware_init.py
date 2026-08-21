# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""bash/workspace_aware.py _init_workspace 分支单测（mypy 收紧批配套）。

意图（WHY）：
- 2026-08-21 治理批次清理 _workspace 重复注解（no-redef）后，四个分支行进入
  diff-coverage 度量面；tools/workspace_aware.py 已有同类测试，bash 拷贝无。
- 契约：显式 workspace > project_root > base_path > cwd 四级回退。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "tools" / "bash"


def _load_mixin():
    mod_name = "bash_workspace_aware_init_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "workspace_aware.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mixin()


class _Tool(_MOD.WorkspaceAwareMixin):  # type: ignore[misc, valid-type]
    """测试用最小工具：base_path 可选挂载。"""

    def __init__(self, base_path: Path | None = None) -> None:
        if base_path is not None:
            self.base_path = base_path


class TestInitWorkspaceBranches:
    def test_explicit_workspace_wins(self, tmp_path: Path) -> None:
        t = _Tool(base_path=Path("C:/base"))
        t._init_workspace({"workspace": str(tmp_path)})
        assert t._workspace == tmp_path

    def test_project_root_fallback(self, tmp_path: Path) -> None:
        t = _Tool()
        t._init_workspace({"project_root": str(tmp_path)})
        assert t._workspace == tmp_path

    def test_base_path_fallback(self) -> None:
        t = _Tool(base_path=Path("C:/some/base"))
        t._init_workspace({})
        assert t._workspace == Path("C:/some/base")

    def test_cwd_last_resort(self) -> None:
        t = _Tool()
        t._init_workspace({})
        assert t._workspace == Path.cwd()
