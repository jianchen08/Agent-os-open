# @feature: FP-0.2.〇 worktree 未跟踪碰撞自愈 | @ci: python-coverage
"""未跟踪碰撞自愈测试（2026-10-23 用户裁定语义，bc546d970306 连败根因修复）。

区分度输入三组：本地未跟踪副本与分支将带入文件「逐字一致 / 内容不一致 /
路径不在分支」；另钉一条既有契约：真内容冲突仍失败（自愈不越界到冲突解决）。
全真 git 仓夹具，无 mock。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_TE_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _TE_DIR.parents[0]
_SDK_SRC = _TE_DIR.parents[1] / "sdk" / "src"

for _d in (_TE_DIR, _SHARED_ROOT, str(_SDK_SRC)):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from worktree_merge import WorktreeMerger  # noqa: E402 — 依赖上方 sys.path 注入

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return r.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """主仓：main 分支含 base.txt；返回项目根。

    钉 ``core.autocrlf=true``：自愈判等走 ``hash-object --path``（clean 过滤
    归一口径，见 worktree_merge 同注释），归一是否生效取决于仓库 autocrlf
    配置——开发机常由全局/system 配置开启、CI Linux 默认关，不钉则
    identical/divergent 两组行尾用例随机绑定宿主 git 配置。
    """
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "core.autocrlf", "true")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root


def _branch_adds(root: Path, branch: str, rel: str, content: str) -> None:
    _git(root, "checkout", "-b", branch)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", f"add {rel}")
    _git(root, "checkout", "main")


def _merge(repo: Path, branch: str) -> dict[str, Any]:
    merger = WorktreeMerger()
    result = merger._safe_merge(str(repo), {"project_root": str(repo), "branch": branch}, {})
    return {"result": result, "merger": merger}


def test_identical_untracked_copy_removed_and_merged(repo: Path) -> None:
    content = "台账 R1-R4\n"
    _branch_adds(repo, "task/x", "docs/working/台账.md", content)
    local = repo / "docs" / "working" / "台账.md"
    local.parent.mkdir(parents=True)
    # write_bytes 避开 Windows write_text 的 \n→os.linesep 翻译（否则 \r\n 会变 \r\r\n，
    # 那是真内容差异而非行尾差异）；此处仅行尾不同，clean 归一后应判一致
    local.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))

    out = _merge(repo, "task/x")

    assert out["result"]["success"] is True
    landed = local.read_text(encoding="utf-8").strip()
    assert landed == content.strip()
    assert not list(local.parent.glob("*.merge-backup-*"))
    report = out["merger"].last_collision_report
    assert report and report[0]["path"] == "docs/working/台账.md"
    assert report[0]["action"] == "removed_identical"
    assert out["result"]["untracked_collisions"] == report


def test_divergent_untracked_copy_backed_up_in_main_space(repo: Path) -> None:
    branch_content = "分支版：108 行\n"
    local_content = "主空间版：R1-R4 推进记录（独有内容）\n"
    _branch_adds(repo, "task/y", "docs/working/台账.md", branch_content)
    local = repo / "docs" / "working" / "台账.md"
    local.parent.mkdir(parents=True)
    local.write_text(local_content, encoding="utf-8")

    out = _merge(repo, "task/y")

    assert out["result"]["success"] is True
    # 分支版本落地
    assert local.read_text(encoding="utf-8").strip() == branch_content.strip()
    # 本地独有内容备份于主空间同目录，上级可见
    backups = list(local.parent.glob("台账.md.merge-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").strip() == local_content.strip()
    report = out["merger"].last_collision_report
    assert report and report[0]["path"] == "docs/working/台账.md"
    assert report[0]["action"].startswith("backed_up_to=")


def test_untracked_path_not_in_branch_untouched(repo: Path) -> None:
    _branch_adds(repo, "task/z", "other.txt", "other\n")
    stranger = repo / "scratch.md"
    stranger.write_text("与分支无关的未跟踪文件\n", encoding="utf-8")

    out = _merge(repo, "task/z")

    assert out["result"]["success"] is True
    assert stranger.exists()
    assert out["merger"].last_collision_report == []
    assert "untracked_collisions" not in out["result"]


def test_real_content_conflict_still_fails(repo: Path) -> None:
    _branch_adds(repo, "task/c", "base.txt", "分支改了 base\n")
    (repo / "base.txt").write_text("主空间也改了 base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main-side edit")

    out = _merge(repo, "task/c")

    assert out["result"]["success"] is False
    assert "git merge 失败" in out["result"]["error"]
