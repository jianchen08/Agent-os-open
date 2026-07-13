"""H4 安全回归：5 个文件工具的 workspace 越界写防护。

漏洞：delete_file/copy_file/move_file/create_directory/list_directory 都只调
resolve_path，没调 check_path_allowed。resolve_path 对绝对路径直接返回不限制根。
对比 file_write 有完整 check_path_allowed 调用——证明这层防护本就是设计的一部分，
这 5 个工具漏接。可删/复制/移动任意路径。

修复：5 个工具在 resolve_path 后、实际操作前，统一调 check_path_allowed；
workspace_aware mixin 新增 _init_agent_level 辅助方法。

本测试用 L2 子任务策略（write 限 workspace）验证：
- 破坏性写操作（delete/copy dest/move/create_dir）越界 → PATH_NOT_ALLOWED 拒绝
- workspace 内操作 → 正常放行

注意：读越界（copy source/list_directory）因 read.scope=project 策略放行，
属既有设计决策（信息泄露 vs 数据破坏），不在本测试覆盖范围。
"""
from __future__ import annotations

import pytest


def _setup_workspace(tmp_path):
    """构造一个有内容的 workspace 和一个 workspace 外目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    inner_file = ws / "inner.txt"
    inner_file.write_text("x")
    return ws, outside, inner_file


# 所有测试用 L2 子任务（parent_agent_level=2），触发 subtask 策略：write 限 workspace
_AGENT_LEVEL = 2


class TestDeleteFileWorkspaceGuard:
    """H4: delete_file 越界写被拒。"""

    @pytest.mark.asyncio
    async def test_delete_outside_workspace_blocked(self, tmp_path) -> None:
        from tools.builtin.delete_file.tool import DeleteFileTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        secret = outside / "secret.txt"
        secret.write_text("s")

        t = DeleteFileTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "path": str(secret),
        })
        assert not r.success
        assert r.error_code == "PATH_NOT_ALLOWED" or "越界" in (r.error or "")
        # 文件必须还在（没被删）
        assert secret.exists(), "越界文件竟被删除了！"

    @pytest.mark.asyncio
    async def test_delete_inside_workspace_allowed(self, tmp_path) -> None:
        from tools.builtin.delete_file.tool import DeleteFileTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = DeleteFileTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "path": str(inner_file),
        })
        assert r.success, f"workspace 内删除应放行: {r.error}"


class TestCopyFileWorkspaceGuard:
    """H4: copy_file 越界 dest（写）被拒。"""

    @pytest.mark.asyncio
    async def test_copy_to_outside_blocked(self, tmp_path) -> None:
        from tools.builtin.copy_file.tool import CopyFileTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = CopyFileTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "source": str(inner_file),
            "destination": str(outside / "stolen.txt"),
        })
        assert not r.success
        assert r.error_code == "PATH_NOT_ALLOWED" or "越界" in (r.error or "")
        assert not (outside / "stolen.txt").exists(), "越界写出竟成功了！"

    @pytest.mark.asyncio
    async def test_copy_inside_workspace_allowed(self, tmp_path) -> None:
        from tools.builtin.copy_file.tool import CopyFileTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = CopyFileTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "source": str(inner_file),
            "destination": str(ws / "copied.txt"),
        })
        assert r.success, f"workspace 内复制应放行: {r.error}"


class TestMoveFileWorkspaceGuard:
    """H4: move_file 越界写被拒。"""

    @pytest.mark.asyncio
    async def test_move_to_outside_blocked(self, tmp_path) -> None:
        from tools.builtin.move_file.tool import MoveFileTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = MoveFileTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "source": str(inner_file),
            "destination": str(outside / "moved.txt"),
        })
        assert not r.success
        assert r.error_code == "PATH_NOT_ALLOWED" or "越界" in (r.error or "")
        # move 的 source 也是 write 校验，越界应被拦
        assert inner_file.exists(), "source 竟被移走了！"


class TestCreateDirectoryWorkspaceGuard:
    """H4: create_directory 越界写被拒。"""

    @pytest.mark.asyncio
    async def test_create_dir_outside_blocked(self, tmp_path) -> None:
        from tools.builtin.create_directory.tool import CreateDirectoryTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = CreateDirectoryTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "path": str(outside / "newdir"),
        })
        assert not r.success
        assert r.error_code == "PATH_NOT_ALLOWED" or "越界" in (r.error or "")
        assert not (outside / "newdir").exists()

    @pytest.mark.asyncio
    async def test_create_dir_inside_allowed(self, tmp_path) -> None:
        from tools.builtin.create_directory.tool import CreateDirectoryTool

        ws, outside, inner_file = _setup_workspace(tmp_path)
        t = CreateDirectoryTool()
        r = await t.execute({
            "workspace": str(ws),
            "project_root": str(ws),
            "parent_agent_level": _AGENT_LEVEL,
            "path": str(ws / "newinner"),
        })
        assert r.success, f"workspace 内建目录应放行: {r.error}"
