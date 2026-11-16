# @feature: FP-0.2.批C-a-2 出厂 skills/ 写区收敛（工具层兜底） | @ci: python-coverage
"""fs_tools 出厂 skills/ 写区收敛测试（ADR 2026-10-09 批 C-a 红线①，原则 7）。

工具层兜底语义（与管道层位置闸判定单源同源 zone_policy）：
- 仓库根一级 ``skills/`` 下写/删/move/copy 恒拒且不可授权——运行期唯一写
  路径 = 自进化提案管道；
- 项目根 skills/ 播种与会话工作空间 skills/ 快照不收敛（根锚内照常放行）；
- 读面不收敛（skills/ 是可读源码区）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import repo_anchor

from agentos_builtin_tools.fs_tools import (
    _check_workspace_path,
    delete_file,
    file_write,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def factory_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    """假仓库（含出厂 skills/）+ 独立项目根 + 空写区名单 + 缓存钉住/复位。"""
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    (repo / "skills").mkdir()
    project = tmp_path / "proj"
    (project / "skills").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    users_dir = tmp_path / "cfgusers" / "default"
    users_dir.mkdir(parents=True)
    (users_dir / "project_whitelist.yaml").write_text("entries: []\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir.parent))
    repo_anchor.reset_cache()
    yield {"repo": repo, "project": project, "ws": ws}
    repo_anchor.reset_cache()


async def test_write_into_factory_skills_rejected(factory_repo):
    """写出厂 skills/：工具层兜底拒，文件不落盘，原因指明提案管道。"""
    target = factory_repo["repo"] / "skills" / "new.md"
    result = await file_write(
        path=str(target), action="write", content="x", workspace=str(factory_repo["ws"])
    )
    assert result.success is False
    assert "出厂 skills/" in result.error
    assert "提案" in result.error
    assert not target.exists()


async def test_delete_from_factory_skills_rejected(factory_repo):
    """删出厂 skills/ 文件：同收敛（写类操作族），文件保留。"""
    target = factory_repo["repo"] / "skills" / "old.md"
    target.write_text("keep\n", encoding="utf-8")
    result = await delete_file(path=str(target), workspace=str(factory_repo["ws"]))
    assert result.success is False
    assert "出厂 skills/" in result.error
    assert target.exists()


async def test_factory_deny_precedes_root_anchor(factory_repo):
    """dev 场景（project_root = 仓库根）：根锚内快捷放行不遮蔽收敛。"""
    allowed, reason, _ = await _check_workspace_path(
        "skills/leak.md",
        workspace=str(factory_repo["ws"]),
        project_root=str(factory_repo["repo"]),
        operation="write",
    )
    assert allowed is False
    assert reason is not None and "出厂 skills/" in reason


async def test_write_project_skills_seeding_passes(factory_repo):
    """语义 2 正例：用户项目根 skills/ 播种写放行（不收敛）。"""
    target = factory_repo["project"] / "skills" / "seeded.md"
    result = await file_write(
        path=str(target),
        action="write",
        content="x",
        project_root=str(factory_repo["project"]),
        workspace=str(factory_repo["ws"]),
    )
    assert result.success is True
    assert target.read_text(encoding="utf-8") == "x"


async def test_write_workspace_skills_snapshot_passes(factory_repo):
    """语义 3 正例：工作空间 skills/ 快照写放行（workspace_lifecycle 写面）。"""
    target = factory_repo["ws"] / "skills" / "snapshot.md"
    result = await file_write(
        path=str(target), action="write", content="x", workspace=str(factory_repo["ws"])
    )
    assert result.success is True
    assert target.exists()


async def test_read_factory_skills_passes(factory_repo):
    """读面不收敛：出厂 skills/ 文件可读（可读源码区）。"""
    target = factory_repo["repo"] / "skills" / "doc.md"
    target.write_text("doc\n", encoding="utf-8")
    result = await file_write(  # 对照：写拒已测；此处验证 _check_workspace_path 读链放行
        path=str(target), action="write", content="x", workspace=str(factory_repo["ws"])
    )
    assert result.success is False  # 写仍拒
    allowed, reason, _ = await _check_workspace_path(
        str(target), workspace=str(factory_repo["ws"]), project_root=None, operation="read"
    )
    assert allowed is True
    assert reason == ""


async def test_generic_outside_zone_rejection_unchanged(factory_repo):
    """对照：仓库源码区非 skills 路径（锚外、空名单）仍走通用区外拒绝语义。"""
    allowed, reason, _ = await _check_workspace_path(
        str(factory_repo["repo"] / "docs" / "x.md"),
        workspace=str(factory_repo["ws"]),
        project_root=None,
        operation="write",
    )
    assert allowed is False
    assert reason is not None and "出厂 skills/" not in reason
