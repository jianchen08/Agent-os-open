# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""enhanced_search 遍历剪枝测试：缓存/运行时垃圾目录族全深度跳过。

背景（2026-11-18 fa76afdbbe07 空结果归因）：仓根起搜时 DFS 要先开扫
.mypy_cache/.ruff_cache/.task_runtime/.test_kernel 等 ~23 万缓存/运行时
文件才走到 plugins/，60s 墙钟预算耗尽在缓存堆里 → 位于其后子树里的
关键词恒不可达（空结果 + 截断标注）。剪枝名单扩容后垃圾目录全深度跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_builtin_tools.search_tool import _walk_prune_dirs, enhanced_search

pytestmark = pytest.mark.unit

# 新增剪枝目录族（精确 basename，全深度生效；显式 path 指入其中仍可搜）。
_JUNK_DIRS = [
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".task_runtime",
    ".test_kernel",
    "target-clippy-lint",
    "user_root",
]


class TestWalkPruneJunkDirs:
    def test_prune_skips_junk_dirs_keeps_legit(self) -> None:
        """垃圾目录族按 basename 全深度剪枝，合法目录原样保留。"""
        dirs = ["plugins", ".mypy_cache", "docs", "user_root", "src"]
        kept = _walk_prune_dirs(str(Path("root")), dirs, prune=set())
        assert kept == ["plugins", "docs", "src"]

    def test_prune_is_exact_name_match(self) -> None:
        """非精确同名不误剪：target2 / mypy_cache（无点前缀）保留，
        target-clippy-lint（精确名单成员）剪除。"""
        dirs = ["target2", "mypy_cache", "target-clippy-lint", "src"]
        kept = _walk_prune_dirs(str(Path("root")), dirs, prune=set())
        assert kept == ["target2", "mypy_cache", "src"]

    async def test_junk_dirs_not_scanned_but_repo_tree_reachable(
        self, tmp_path: Path
    ) -> None:
        """端到端双断言（防拟合）：垃圾目录内同名关键词不入结果，
        合法子树正常命中。"""
        ws = tmp_path / "repo"
        for junk in _JUNK_DIRS:
            junk_dir = ws / junk
            junk_dir.mkdir(parents=True)
            (junk_dir / "decoy.py").write_text(
                "TODO_INSTRUCTIONS = 1", encoding="utf-8"
            )
        target = ws / "plugins" / "shared" / "todo_track"
        target.mkdir(parents=True)
        (target / "plugin.py").write_text(
            "TODO_INSTRUCTIONS = 'real'", encoding="utf-8"
        )

        result = await enhanced_search(
            query="TODO_INSTRUCTIONS", path=str(ws), workspace=str(ws)
        )
        assert result.success is True, result.error
        files = [r["file_path"] for r in result.output["results"]]
        assert files == [str(target / "plugin.py")]
