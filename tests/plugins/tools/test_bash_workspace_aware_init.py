# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""workspace_aware _init_workspace 分支单测（mypy 收紧批配套）。

意图（WHY）：
- 治理批次清理 _workspace 重复注解（no-redef）后，四个分支行进入
  diff-coverage 度量面；bash/workspace_aware.py 副本已随单一真值源
  下沉删除，公共实现 = SDK ``agentos_plugin_sdk.workspace_aware``。
- 契约：显式 workspace > project_root > base_path > cwd 四级回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_plugin_sdk.workspace_aware import WorkspaceAwareMixin

pytestmark = pytest.mark.unit


class _Tool(WorkspaceAwareMixin):
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
