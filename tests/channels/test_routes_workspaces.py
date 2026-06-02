"""工作空间 API 路由 - 集成测试。

测试范围：
1. POST /{container_task_id}/create-entry — 创建文件/文件夹
2. DELETE /{container_task_id}/entries — 删除文件/文件夹
3. POST /{container_task_id}/rename-entry — 重命名文件/文件夹
4. POST /{container_task_id}/move-entry — 移动文件/文件夹
5. GET/PUT /{container_task_id}/file-content — 文件读写回归测试
6. 路径穿越安全检查
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import require_auth
from channels.api.routes_workspaces import workspaces_router

# ============================================================
# 测试常量
# ============================================================

MOCK_TASK_ID = "test-container-task-001"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture()
def mock_auth():
    """覆盖认证依赖，模拟已登录用户。"""

    async def _mock_auth():
        return {"sub": "test_user", "username": "tester"}

    return _mock_auth


@pytest.fixture()
def workspace_tmp():
    """创建临时工作空间目录，测试结束后清理。"""
    tmp = tempfile.mkdtemp(prefix="ws_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def client(mock_auth, workspace_tmp):
    """创建 FastAPI TestClient，覆盖认证和路径解析。"""
    app = FastAPI()
    app.dependency_overrides[require_auth] = mock_auth
    app.include_router(workspaces_router)

    # patch _resolve_workspace_path 返回临时目录
    async def _fake_resolve(container_task_id: str):
        return workspace_tmp

    with patch(
        "channels.api.routes_workspaces._resolve_workspace_path",
        new=_fake_resolve,
    ):
        with TestClient(app) as c:
            yield c


def _url(endpoint: str, task_id: str = MOCK_TASK_ID) -> str:
    """构造 API URL。"""
    return f"/api/v1/workspaces/{task_id}/{endpoint}"


# ============================================================
# Test: POST /create-entry
# ============================================================


class TestCreateEntry:
    """创建文件或文件夹测试。"""

    def test_create_file_success(self, client, workspace_tmp):
        """正常创建文件应成功。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "new_file.py", "type": "file"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["path"] == "new_file.py"

        # 验证文件确实被创建
        fpath = Path(workspace_tmp) / "new_file.py"
        assert fpath.is_file()
        assert fpath.read_text(encoding="utf-8") == ""

    def test_create_directory_success(self, client, workspace_tmp):
        """正常创建文件夹应成功。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "new_dir", "type": "directory"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        dpath = Path(workspace_tmp) / "new_dir"
        assert dpath.is_dir()

    def test_create_nested_file_success(self, client, workspace_tmp):
        """在嵌套目录下创建文件应成功（自动创建父目录）。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "sub/deep/file.txt", "type": "file"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        fpath = Path(workspace_tmp) / "sub" / "deep" / "file.txt"
        assert fpath.is_file()

    def test_create_nested_directory_success(self, client, workspace_tmp):
        """创建嵌套目录应成功。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "a/b/c", "type": "directory"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        dpath = Path(workspace_tmp) / "a" / "b" / "c"
        assert dpath.is_dir()

    def test_create_file_already_exists_error(self, client, workspace_tmp):
        """创建已存在的文件应返回错误。"""
        # 先创建文件
        (Path(workspace_tmp) / "exist.txt").write_text("hello", encoding="utf-8")

        resp = client.post(
            _url("create-entry"),
            json={"path": "exist.txt", "type": "file"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "已存在" in data["message"]

    def test_create_directory_already_exists_error(self, client, workspace_tmp):
        """创建已存在的文件夹应返回错误。"""
        (Path(workspace_tmp) / "exist_dir").mkdir()

        resp = client.post(
            _url("create-entry"),
            json={"path": "exist_dir", "type": "directory"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "已存在" in data["message"]

    def test_create_entry_empty_path_error(self, client, workspace_tmp):
        """path 为空应返回错误。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "", "type": "file"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "path" in data["message"]

    def test_create_entry_invalid_type_error(self, client, workspace_tmp):
        """type 参数非法应返回错误。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "test.xyz", "type": "invalid"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "type" in data["message"]

    def test_create_entry_path_traversal_attack(self, client, workspace_tmp):
        """路径穿越攻击应被阻止。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "../../../etc/malicious", "type": "file"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]


# ============================================================
# Test: DELETE /entries
# ============================================================


class TestDeleteEntry:
    """删除文件或文件夹测试。"""

    def test_delete_file_success(self, client, workspace_tmp):
        """正常删除文件应成功。"""
        fpath = Path(workspace_tmp) / "to_delete.txt"
        fpath.write_text("delete me", encoding="utf-8")
        assert fpath.exists()

        resp = client.delete(
            _url("entries"),
            params={"path": "to_delete.txt"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert not fpath.exists()

    def test_delete_directory_with_children_success(self, client, workspace_tmp):
        """删除含子内容的文件夹应递归删除。"""
        dpath = Path(workspace_tmp) / "dir_with_children"
        dpath.mkdir()
        (dpath / "child1.txt").write_text("c1", encoding="utf-8")
        (dpath / "sub").mkdir()
        (dpath / "sub" / "child2.txt").write_text("c2", encoding="utf-8")

        resp = client.delete(
            _url("entries"),
            params={"path": "dir_with_children"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert not dpath.exists()

    def test_delete_empty_directory_success(self, client, workspace_tmp):
        """删除空文件夹应成功。"""
        dpath = Path(workspace_tmp) / "empty_dir"
        dpath.mkdir()

        resp = client.delete(
            _url("entries"),
            params={"path": "empty_dir"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert not dpath.exists()

    def test_delete_nonexistent_error(self, client, workspace_tmp):
        """删除不存在的文件应返回错误。"""
        resp = client.delete(
            _url("entries"),
            params={"path": "no_such_file.txt"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "不存在" in data["message"]

    def test_delete_root_directory_forbidden(self, client, workspace_tmp):
        """禁止删除工作空间根目录。"""
        # 用 "." 或 "" 代表根目录 — 实际 resolve 后等于 workspace_tmp 自身
        resp = client.delete(
            _url("entries"),
            params={"path": "."},
        )
        data = resp.json()
        assert data["success"] is False
        assert "根目录" in data["message"]

    def test_delete_path_traversal_attack(self, client, workspace_tmp):
        """路径穿越攻击应被阻止。"""
        resp = client.delete(
            _url("entries"),
            params={"path": "../../important_file"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]


# ============================================================
# Test: POST /rename-entry
# ============================================================


class TestRenameEntry:
    """重命名文件或文件夹测试。"""

    def test_rename_file_success(self, client, workspace_tmp):
        """正常重命名文件应成功。"""
        old = Path(workspace_tmp) / "old_name.txt"
        old.write_text("content", encoding="utf-8")

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "old_name.txt", "new_name": "new_name.txt"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["old_path"] == "old_name.txt"
        assert data["new_path"] == "new_name.txt"
        assert not old.exists()
        assert (Path(workspace_tmp) / "new_name.txt").is_file()
        assert (Path(workspace_tmp) / "new_name.txt").read_text(encoding="utf-8") == "content"

    def test_rename_directory_success(self, client, workspace_tmp):
        """正常重命名文件夹应成功（含子内容）。"""
        old = Path(workspace_tmp) / "old_dir"
        old.mkdir()
        (old / "inner.txt").write_text("inner", encoding="utf-8")

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "old_dir", "new_name": "new_dir"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert not old.exists()
        new = Path(workspace_tmp) / "new_dir"
        assert new.is_dir()
        assert (new / "inner.txt").read_text(encoding="utf-8") == "inner"

    def test_rename_nested_file_success(self, client, workspace_tmp):
        """重命名嵌套目录中的文件应成功。"""
        subdir = Path(workspace_tmp) / "sub"
        subdir.mkdir()
        old = subdir / "file_a.py"
        old.write_text("print('a')", encoding="utf-8")

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "sub/file_a.py", "new_name": "file_b.py"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert Path(data["new_path"]).as_posix() == "sub/file_b.py"
        assert not old.exists()
        assert (subdir / "file_b.py").is_file()

    def test_rename_target_already_exists_error(self, client, workspace_tmp):
        """目标名已存在时应返回错误。"""
        (Path(workspace_tmp) / "source.txt").write_text("s", encoding="utf-8")
        (Path(workspace_tmp) / "target.txt").write_text("t", encoding="utf-8")

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "source.txt", "new_name": "target.txt"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "已存在" in data["message"]

    def test_rename_source_not_exist_error(self, client, workspace_tmp):
        """源文件不存在时应返回错误。"""
        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "nonexistent.txt", "new_name": "whatever.txt"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "不存在" in data["message"]

    def test_rename_empty_params_error(self, client, workspace_tmp):
        """参数为空应返回错误。"""
        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "", "new_name": "new.txt"},
        )
        data = resp.json()
        assert data["success"] is False

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "old.txt", "new_name": ""},
        )
        data = resp.json()
        assert data["success"] is False

    def test_rename_path_separator_in_new_name_error(self, client, workspace_tmp):
        """new_name 包含路径分隔符应返回错误（防路径穿越）。"""
        (Path(workspace_tmp) / "src.txt").write_text("x", encoding="utf-8")

        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "src.txt", "new_name": "../../evil.txt"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "路径分隔符" in data["message"]

    def test_rename_path_traversal_in_old_path_error(self, client, workspace_tmp):
        """old_path 路径穿越应被阻止。"""
        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "../../../etc/passwd", "new_name": "hacked"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]


# ============================================================
# Test: POST /move-entry
# ============================================================


class TestMoveEntry:
    """移动文件或文件夹测试。"""

    def test_move_file_to_directory_success(self, client, workspace_tmp):
        """移动文件到指定目录应成功。"""
        src = Path(workspace_tmp) / "file.txt"
        src.write_text("move me", encoding="utf-8")
        dest_dir = Path(workspace_tmp) / "target_dir"
        dest_dir.mkdir()

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "file.txt", "destination_dir": "target_dir"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert Path(data["destination_path"]).as_posix() == "target_dir/file.txt"
        assert not src.exists()
        assert (dest_dir / "file.txt").read_text(encoding="utf-8") == "move me"

    def test_move_directory_success(self, client, workspace_tmp):
        """移动文件夹到另一目录应成功。"""
        src = Path(workspace_tmp) / "move_me"
        src.mkdir()
        (src / "inner.txt").write_text("inner", encoding="utf-8")
        dest = Path(workspace_tmp) / "destination"
        dest.mkdir()

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "move_me", "destination_dir": "destination"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert not src.exists()
        moved = dest / "move_me"
        assert moved.is_dir()
        assert (moved / "inner.txt").read_text(encoding="utf-8") == "inner"

    def test_move_target_has_same_name_error(self, client, workspace_tmp):
        """目标位置已存在同名文件应返回错误。"""
        src = Path(workspace_tmp) / "collision.txt"
        src.write_text("src", encoding="utf-8")
        dest = Path(workspace_tmp) / "dest"
        dest.mkdir()
        (dest / "collision.txt").write_text("dest", encoding="utf-8")

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "collision.txt", "destination_dir": "dest"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "同名" in data["message"]

    def test_move_source_not_exist_error(self, client, workspace_tmp):
        """源路径不存在应返回错误。"""
        dest = Path(workspace_tmp) / "dest"
        dest.mkdir()

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "no_file.txt", "destination_dir": "dest"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "不存在" in data["message"]

    def test_move_dest_not_directory_error(self, client, workspace_tmp):
        """目标不是目录应返回错误。"""
        src = Path(workspace_tmp) / "src.txt"
        src.write_text("s", encoding="utf-8")
        dest_file = Path(workspace_tmp) / "not_a_dir.txt"
        dest_file.write_text("d", encoding="utf-8")

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "src.txt", "destination_dir": "not_a_dir.txt"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "不是目录" in data["message"]

    def test_move_to_self_subdirectory_error(self, client, workspace_tmp):
        """移动目录到自身子目录应返回错误。"""
        parent = Path(workspace_tmp) / "parent_dir"
        parent.mkdir()
        child = parent / "child_dir"
        child.mkdir()

        resp = client.post(
            _url("move-entry"),
            json={
                "source_path": "parent_dir",
                "destination_dir": "parent_dir/child_dir",
            },
        )
        data = resp.json()
        assert data["success"] is False
        assert "自身子目录" in data["message"]

    def test_move_empty_params_error(self, client, workspace_tmp):
        """参数为空应返回错误。"""
        resp = client.post(
            _url("move-entry"),
            json={"source_path": "", "destination_dir": "dest"},
        )
        data = resp.json()
        assert data["success"] is False

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "src.txt", "destination_dir": ""},
        )
        data = resp.json()
        assert data["success"] is False

    def test_move_path_traversal_attack(self, client, workspace_tmp):
        """路径穿越攻击应被阻止。"""
        resp = client.post(
            _url("move-entry"),
            json={"source_path": "../../etc/passwd", "destination_dir": "."},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

        resp = client.post(
            _url("move-entry"),
            json={"source_path": "a.txt", "destination_dir": "../../tmp"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]


# ============================================================
# Test: GET/PUT file-content（回归测试）
# ============================================================


class TestFileContentRegression:
    """确保原有 file-content 端点正常工作。"""

    def test_get_file_content_success(self, client, workspace_tmp):
        """GET 读取文件内容应成功。"""
        fpath = Path(workspace_tmp) / "readme.md"
        fpath.write_text("# Hello World", encoding="utf-8")

        resp = client.get(
            _url("file-content"),
            params={"path": "readme.md"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["content"] == "# Hello World"
        assert data["path"] == "readme.md"
        assert data["size"] > 0

    def test_get_file_content_not_found(self, client, workspace_tmp):
        """GET 不存在的文件应返回错误。"""
        resp = client.get(
            _url("file-content"),
            params={"path": "nonexistent.md"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_get_file_content_path_traversal(self, client, workspace_tmp):
        """GET 路径穿越应被阻止。"""
        resp = client.get(
            _url("file-content"),
            params={"path": "../../../etc/passwd"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_put_file_content_success(self, client, workspace_tmp):
        """PUT 保存文件内容应成功。"""
        resp = client.put(
            _url("file-content"),
            params={"path": "saved.txt"},
            json={"content": "saved content"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["path"] == "saved.txt"

        fpath = Path(workspace_tmp) / "saved.txt"
        assert fpath.read_text(encoding="utf-8") == "saved content"

    def test_put_file_content_path_traversal(self, client, workspace_tmp):
        """PUT 路径穿越应被阻止。"""
        resp = client.put(
            _url("file-content"),
            params={"path": "../../evil.txt"},
            json={"content": "bad"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_put_and_get_roundtrip(self, client, workspace_tmp):
        """PUT 后 GET 应返回相同内容（完整回环测试）。"""
        content = "line1\nline2\nline3 🎉"
        client.put(
            _url("file-content"),
            params={"path": "roundtrip.txt"},
            json={"content": content},
        )

        resp = client.get(
            _url("file-content"),
            params={"path": "roundtrip.txt"},
        )
        assert resp.json()["content"] == content


# ============================================================
# Test: WorkspaceService 回归测试
# ============================================================


class TestWorkspaceServiceRegression:
    """WorkspaceService 核心方法回归测试。"""

    def test_get_or_create_workspace(self):
        """get_or_create_workspace 应正确创建和获取工作空间。"""
        import asyncio
        from workspace.workspace_service import WorkspaceService

        service = WorkspaceService()
        ws = asyncio.new_event_loop().run_until_complete(
            service.get_or_create_workspace("task-001")
        )
        assert ws is not None
        assert ws.container_task_id == "task-001"
        assert ws.id  # id 非空

    def test_get_or_create_workspace_idempotent(self):
        """重复调用 get_or_create_workspace 应返回同一实例。"""
        import asyncio
        from workspace.workspace_service import WorkspaceService

        loop = asyncio.new_event_loop()
        service = WorkspaceService()
        ws1 = loop.run_until_complete(service.get_or_create_workspace("task-002"))
        ws2 = loop.run_until_complete(service.get_or_create_workspace("task-002"))
        assert ws1.id == ws2.id

    def test_get_or_create_workspace_with_params(self):
        """get_or_create_workspace 支持自定义 title 和 description。"""
        import asyncio
        from workspace.workspace_service import WorkspaceService

        service = WorkspaceService()
        ws = asyncio.new_event_loop().run_until_complete(
            service.get_or_create_workspace(
                "task-003",
                session_id="sess-001",
                title="测试工作空间",
                description="用于测试",
            )
        )
        assert ws.session_id == "sess-001"
        assert ws.title == "测试工作空间"
        assert ws.description == "用于测试"

    def test_get_file_tree_with_real_directory(self):
        """get_file_tree 扫描真实目录应返回正确的文件树。"""
        import asyncio
        import tempfile
        import shutil

        from workspace.workspace_service import WorkspaceService

        tmp = tempfile.mkdtemp(prefix="ws_tree_test_")
        try:
            # 创建目录结构
            (Path(tmp) / "file1.py").write_text("print(1)", encoding="utf-8")
            (Path(tmp) / "dir1").mkdir()
            (Path(tmp) / "dir1" / "file2.txt").write_text("hello", encoding="utf-8")

            loop = asyncio.new_event_loop()
            service = WorkspaceService()
            # 先创建 workspace 以便 get_file_tree 更新缓存
            loop.run_until_complete(service.get_or_create_workspace("tree-task"))

            result = loop.run_until_complete(
                service.get_file_tree("tree-task", base_path=tmp)
            )

            assert "tree" in result
            tree = result["tree"]
            # 应至少有 file1.py 和 dir1
            names = [n["name"] for n in tree]
            assert "file1.py" in names
            assert "dir1" in names

            # dir1 应有子节点 file2.txt
            dir1_node = next(n for n in tree if n["name"] == "dir1")
            assert dir1_node["type"] == "directory"
            child_names = [c["name"] for c in dir1_node.get("children", [])]
            assert "file2.txt" in child_names
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scan_directory_skips_hidden_and_pycache(self):
        """_scan_directory 应跳过隐藏文件和 __pycache__。"""
        import tempfile
        import shutil

        from workspace.workspace_service import WorkspaceService

        tmp = tempfile.mkdtemp(prefix="ws_scan_test_")
        try:
            (Path(tmp) / "visible.txt").write_text("v", encoding="utf-8")
            (Path(tmp) / ".hidden").write_text("h", encoding="utf-8")
            (Path(tmp) / "__pycache__").mkdir()

            service = WorkspaceService()
            nodes = service._scan_directory(tmp, tmp)

            names = [n.name for n in nodes]
            assert "visible.txt" in names
            assert ".hidden" not in names
            assert "__pycache__" not in names
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scan_directory_max_depth(self):
        """_scan_directory 应遵守最大深度限制。"""
        import tempfile
        import shutil

        from workspace.workspace_service import WorkspaceService

        tmp = tempfile.mkdtemp(prefix="ws_depth_test_")
        try:
            # 创建 3 层嵌套
            deep = Path(tmp) / "l1" / "l2" / "l3"
            deep.mkdir(parents=True)
            (deep / "deep.txt").write_text("d", encoding="utf-8")

            service = WorkspaceService()
            # max_depth=2 应看不到 l3
            nodes = service._scan_directory(tmp, tmp, max_depth=2)
            # l1 存在，但 l3 内容不在
            assert len(nodes) > 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_workspace_not_found_returns_empty_artifacts(self):
        """不存在的工作空间获取制品应返回空列表。"""
        import asyncio
        from workspace.workspace_service import WorkspaceService

        service = WorkspaceService()
        result = asyncio.new_event_loop().run_until_complete(
            service.list_artifacts_by_workspace("nonexistent-task")
        )
        assert result["items"] == []
        assert result["total"] == 0

    def test_scan_directory_handles_oserror(self):
        """_scan_directory 遇到 OSError 时应安全返回空列表而非抛出异常。

        验证修复: os.listdir 的异常捕获从 PermissionError 扩展为 (PermissionError, OSError)。
        """
        from unittest.mock import patch
        from workspace.workspace_service import WorkspaceService

        service = WorkspaceService()

        with patch("os.listdir", side_effect=OSError("Windows 设备路径错误")):
            nodes = service._scan_directory("C:\\some\\path", "C:\\some\\path")
            assert nodes == []

    def test_scan_directory_handles_permission_error(self):
        """_scan_directory 遇到 PermissionError 时应安全返回空列表（回归测试）。"""
        from unittest.mock import patch
        from workspace.workspace_service import WorkspaceService

        service = WorkspaceService()

        with patch("os.listdir", side_effect=PermissionError("权限不足")):
            nodes = service._scan_directory("/root/secret", "/root/secret")
            assert nodes == []

    def test_scan_directory_skips_windows_device_path(self):
        """_scan_directory 应跳过以 \\\\.\\ 开头的 Windows 设备路径。

        验证修复: os.path.isdir 对设备路径可能返回 True 导致递归异常。
        """
        import shutil
        import tempfile
        from unittest.mock import patch
        from workspace.workspace_service import WorkspaceService

        tmp = tempfile.mkdtemp(prefix="ws_device_test_")
        try:
            (Path(tmp) / "normal.txt").write_text("ok", encoding="utf-8")
            (Path(tmp) / "subdir").mkdir()

            service = WorkspaceService()

            # 模拟 os.path.join 生成一个设备路径前缀的 full_path
            # 通过在遍历过程中让某个 entry 的 full_path 以 \\\\.\\ 开头
            original_join = os.path.join

            def mock_join(*args):
                if args[-1] == "device_entry":
                    return "\\\\.\\COM1"
                return original_join(*args)

            # 模拟 listdir 返回包含设备路径的条目
            with patch("os.listdir", return_value=["normal.txt", "subdir", "device_entry"]):
                with patch("os.path.join", side_effect=mock_join):
                    nodes = service._scan_directory(tmp, tmp)

            names = [n.name for n in nodes]
            assert "normal.txt" in names
            assert "subdir" in names
            # device_entry 应被跳过，不在结果中
            assert "device_entry" not in names
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# Test: 路径穿越综合安全测试
# ============================================================


class TestPathTraversalSecurity:
    """综合路径穿越安全测试。"""

    def test_create_with_double_dot(self, client, workspace_tmp):
        """create-entry: ../ 路径穿越应被阻止。"""
        resp = client.post(
            _url("create-entry"),
            json={"path": "../outside.txt", "type": "file"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_delete_with_double_dot(self, client, workspace_tmp):
        """delete entries: ../ 路径穿越应被阻止。"""
        resp = client.delete(
            _url("entries"),
            params={"path": "../outside.txt"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_rename_with_double_dot_in_new_name(self, client, workspace_tmp):
        """rename-entry: new_name 包含路径分隔符应被阻止。"""
        resp = client.post(
            _url("rename-entry"),
            json={"old_path": "a.txt", "new_name": "../evil.txt"},
        )
        data = resp.json()
        assert data["success"] is False

    def test_move_with_double_dot_in_source(self, client, workspace_tmp):
        """move-entry: source_path 路径穿越应被阻止。"""
        resp = client.post(
            _url("move-entry"),
            json={"source_path": "../etc/passwd", "destination_dir": "."},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_move_with_double_dot_in_dest(self, client, workspace_tmp):
        """move-entry: destination_dir 路径穿越应被阻止。"""
        (Path(workspace_tmp) / "src.txt").write_text("s", encoding="utf-8")
        resp = client.post(
            _url("move-entry"),
            json={"source_path": "src.txt", "destination_dir": "../../tmp"},
        )
        data = resp.json()
        assert data["success"] is False
        assert "超出工作空间范围" in data["message"]

    def test_no_files_created_outside_workspace(self, client, workspace_tmp):
        """确保路径穿越不会在工作空间外创建文件。"""
        # 尝试多种穿越模式
        attack_paths = [
            "../../../tmp/evil.txt",
            "..%2F..%2Fevil.txt",
            "sub/../../../evil.txt",
        ]
        for attack in attack_paths:
            client.post(
                _url("create-entry"),
                json={"path": attack, "type": "file"},
            )

        # 确认 workspace_tmp 的上级目录没有 evil.txt
        parent = Path(workspace_tmp).parent
        assert not (parent / "evil.txt").exists()
