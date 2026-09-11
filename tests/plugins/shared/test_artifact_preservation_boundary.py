# @feature: FP-0.2.〇 隔离工作区 工具执行 | @ci: python-coverage
"""产物保全边界测试——B5「shared 模式任务产物蒸发」修复后的契约守卫。

长稳测试 4732fb1c64d5 族实测（docs/working/长稳测试bug清单_20260904.md B5★）：
游戏任务 completed 后产物蒸发。定案根因有二（用户裁定 2026-09-05）：
1. 合并门控的「成功」不包含产物到达——auto-commit 静默失败 + 空合并
   （Already up to date）+ 空 diff 假验证 → 清理链照常执行；
2. 删除链（hard_delete/级联/资源清理）可触碰工作区目录，清理能力游离在
   合并流程之外。

被测不变量（产物保全契约）：
1. worktree 合并门控 = 前照快照 → 合并 → 逐项到达对比 → 全部到达才清理；
   空 = 平凡成功（执行与否归评估门控），失败 = fail-closed 保留现场；
2. 删除链不删工作区：只销毁容器与执行数据，工作区清理唯一合法点在门控。

用例矩阵（真实依赖，无内核；机制层用例见 test_worktree_merge.py）：
  - worktree 对照（绿）：真合并后产物落项目根且可从数据根检索；
  - 删除链豁免工作区（绿）：资源清理执行后共享工作区与产物原样保留。
外部依赖仅 git CLI 与 tmp 目录；容器腿经 get_isolation_manager 注入故障
（容器运行时=外部依赖，生产 docker 缺失时同走该 except 分支）。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

import os  # noqa: E402
import sys  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _d in (
    _REPO_ROOT / "plugins" / "shared",
    _REPO_ROOT / "plugins" / "shared" / "system",
    _REPO_ROOT / "plugins" / "shared" / "system" / "tasks",
    _REPO_ROOT / "plugins" / "shared" / "system" / "isolation",
):
    _s = str(_d)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)

import worktree_merge  # noqa: E402 — 依赖 conftest 的 sys.path 注入
import isolation.manager  # noqa: E402
from _task_cleanup import _TaskCleanupMixin  # noqa: E402

# 共享工作区根（workspace.root，.ai_workspaces 的测试替身）
PRODUCT_NAME = "main.gd"
PRODUCT_CONTENT = "# B5 artifact probe: scene tree of the game\n"


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.local", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _find_content_copies(root: Path, name: str, content: str) -> list[Path]:
    """在系统数据根内按文件名+内容检索产物副本（.git 内部除外）。"""
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if name in filenames:
            f = Path(dirpath) / name
            try:
                if f.read_text(encoding="utf-8") == content:
                    hits.append(f)
            except OSError:
                continue
    return hits


def _no_container_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器腿注入故障（容器运行时=外部依赖；生产 docker 缺失时同分支）。"""

    async def _raise(config_path: str | None = None) -> None:
        raise RuntimeError("test: 无容器运行时")

    monkeypatch.setattr(isolation.manager, "get_isolation_manager", _raise)


# ── 对照：worktree 模式「合并+清理一体」产物有保送 ───────────


class TestWorktreeEscortControl:
    def test_completed_products_merged_to_project_root(
        self, tmp_path: Path
    ) -> None:
        """worktree 对照（现行绿）：门控通过 = 产物落项目根 + worktree 清理。

        「合并+清理一体」的合并半边：产物到达验证通过后才清理。机制细节与
        新用例（空放行/commit 失败拦截/内容不一致拦截）见
        tests/plugins/shared/test_worktree_merge.py::TestRealMerge。
        """
        proj = tmp_path / "projects" / "game-x"
        proj.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=proj)
        (proj / "README.md").write_text("# game-x\n", encoding="utf-8")
        git("add", "-A", cwd=proj)
        git("commit", "-q", "-m", "init", cwd=proj)

        task_id = "749021ce02c4"
        wt = tmp_path / "ws" / f"game-x__wt_{task_id[:8]}"
        git("worktree", "add", "-b", f"task/{task_id}", str(wt), cwd=proj)
        # 产物不预提交：门控的 auto-commit 应兜住未提交工作（生产实态）
        (wt / PRODUCT_NAME).write_text(PRODUCT_CONTENT, encoding="utf-8")

        err = worktree_merge.merge_worktree_before_complete(
            task_id,
            {
                "mode": "worktree",
                "path": str(wt),
                "branch": f"task/{task_id}",
                "project_root": str(proj),
            },
        )
        assert err is None, f"worktree 门控应通过: {err}"
        assert not wt.exists(), "worktree 应被清理（一体的另一半）"
        assert (proj / PRODUCT_NAME).exists(), "产物应已合并到项目根"
        assert _find_content_copies(tmp_path, PRODUCT_NAME, PRODUCT_CONTENT), (
            "合并目标即持久副本：产物应可从系统数据根检索"
        )


# ── 删除链豁免工作区（产物保全契约第 2 条）──────────────────


class TestDeletionChainSparesWorkspaces:
    def test_resource_cleanup_keeps_shared_workspace_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """删除链不删工作区：资源清理执行后共享工作区与产物原样保留。

        B5 场景（会话空间里的 shared 任务产物）：清理能力已从删除链摘除，
        唯一合法清理点在评估通过门控（合并+清理一体）。
        """
        _no_container_runtime(monkeypatch)

        class _Svc(_TaskCleanupMixin):
            pass

        session_ws = tmp_path / "ws" / "sessions" / "thread-9ec61bac"
        session_ws.mkdir(parents=True)
        (session_ws / PRODUCT_NAME).write_text(PRODUCT_CONTENT, encoding="utf-8")

        result = asyncio.run(_Svc()._cleanup_task_resources("33e1ae2e98ed"))
        assert result["container_destroyed"] is False  # 容器腿注入故障
        assert session_ws.exists(), "删除链不得删除工作区目录"
        assert (session_ws / PRODUCT_NAME).read_text(encoding="utf-8") == PRODUCT_CONTENT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
