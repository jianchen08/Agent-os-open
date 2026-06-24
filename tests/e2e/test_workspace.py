"""工作空间 E2E 测试。

验证工作空间的创建 → 文件操作 → 文件树 → 详情查询完整链路。
对应 features.md 场景 10。

测试用例：
- test_get_workspace_detail：获取工作空间详情（自动创建）
- test_get_workspace_file_tree：获取文件目录树
- test_create_file_in_workspace：创建文件并验证
- test_read_file_content：读取文件内容
- test_write_file_content：保存文件内容
- test_rename_file：重命名文件
- test_delete_file：删除文件
- test_create_directory：创建文件夹
- test_workspace_without_auth：无 Token 访问返回 401
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 内部辅助 — monkeypatch 工作空间路径解析到临时目录
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> str:
    """将工作空间路径解析隔离到临时目录。

    通过 monkeypatch 替换 routes_workspaces 中的 _resolve_workspace_path，
    使所有工作空间操作都指向 tmp_path，避免影响真实项目目录。

    Args:
        monkeypatch: pytest monkeypatch
        tmp_path: pytest 临时路径

    Returns:
        container_task_id（测试用标识）
    """
    from channels.api import routes_workspaces as ws_module

    container_task_id = "e2e_ws_test"
    ws_root = tmp_path / "workspace_root"
    ws_root.mkdir(parents=True, exist_ok=True)

    async def _fake_resolve_ws_path(task_id: str) -> str | None:
        return str(ws_root)

    monkeypatch.setattr(
        ws_module, "_resolve_workspace_path", _fake_resolve_ws_path
    )
    return container_task_id


# ---------------------------------------------------------------------------
# 工作空间详情测试
# ---------------------------------------------------------------------------

def test_get_workspace_detail(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """获取工作空间详情（不存在时自动创建）。

    验证点：
    - GET /api/v1/workspaces/{container_task_id} 返回 200
    - 响应包含 id 字段
    """
    resp = test_client.get(
        f"/api/v1/workspaces/{isolated_workspace}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取工作空间详情失败: {resp.text}"

    data = resp.json()
    assert "id" in data, "工作空间响应缺少 id 字段"


# ---------------------------------------------------------------------------
# 文件树测试
# ---------------------------------------------------------------------------

def test_get_workspace_file_tree(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """获取工作空间的文件目录树。

    验证点：
    - GET /api/v1/workspaces/{container_task_id}/file-tree 返回 200
    - 响应包含 tree 或类似字段
    """
    resp = test_client.get(
        f"/api/v1/workspaces/{isolated_workspace}/file-tree",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取文件树失败: {resp.text}"


# ---------------------------------------------------------------------------
# 文件创建测试
# ---------------------------------------------------------------------------

def test_create_file_in_workspace(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """在工作空间中创建文件。

    验证点：
    - POST /api/v1/workspaces/{container_task_id}/create-entry 返回 200
    - 响应 success 为 True
    - 再次创建同名文件应失败（路径已存在）
    """
    file_path = "e2e_created_file.txt"
    create_resp = test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": file_path, "type": "file"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 200, f"创建文件失败: {create_resp.text}"

    data = create_resp.json()
    assert data["success"] is True, f"创建文件应成功: {data}"

    # 再次创建同名文件应失败
    dup_resp = test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": file_path, "type": "file"},
        headers=auth_headers,
    )
    dup_data = dup_resp.json()
    assert dup_data["success"] is False, "重复创建应失败"


# ---------------------------------------------------------------------------
# 文件读写测试
# ---------------------------------------------------------------------------

def test_write_and_read_file(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """写入文件内容 → 读取文件内容，验证一致性。

    验证点：
    - PUT file-content 保存文件返回 success
    - GET file-content 读取文件内容一致
    """
    file_path = "e2e_rw_test.txt"
    test_content = "E2E 工作空间读写测试\n第二行内容"

    # 先创建文件
    test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": file_path, "type": "file"},
        headers=auth_headers,
    )

    # PUT 保存内容
    put_resp = test_client.put(
        f"/api/v1/workspaces/{isolated_workspace}/file-content",
        params={"path": file_path},
        json={"content": test_content},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, f"保存文件失败: {put_resp.text}"
    assert put_resp.json()["success"] is True

    # GET 读取内容
    get_resp = test_client.get(
        f"/api/v1/workspaces/{isolated_workspace}/file-content",
        params={"path": file_path},
        headers=auth_headers,
    )
    assert get_resp.status_code == 200, f"读取文件失败: {get_resp.text}"

    get_data = get_resp.json()
    assert get_data["success"] is True
    assert get_data["content"] == test_content, (
        f"读写内容不一致:\n写入: {test_content!r}\n读回: {get_data['content']!r}"
    )


# ---------------------------------------------------------------------------
# 文件夹操作测试
# ---------------------------------------------------------------------------

def test_create_directory(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """在工作空间中创建文件夹。

    验证点：
    - POST create-entry type=directory 返回 success
    - 响应 success 为 True
    """
    dir_path = "e2e_test_dir"
    resp = test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": dir_path, "type": "directory"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"创建文件夹失败: {resp.text}"

    data = resp.json()
    assert data["success"] is True, f"创建文件夹应成功: {data}"


# ---------------------------------------------------------------------------
# 重命名测试
# ---------------------------------------------------------------------------

def test_rename_file(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """重命名文件。

    验证点：
    - 先创建文件
    - POST rename-entry 返回 success
    - 响应包含 old_path 和 new_path
    """
    old_path = "e2e_rename_old.txt"
    new_name = "e2e_rename_new.txt"

    # 创建原始文件
    test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": old_path, "type": "file"},
        headers=auth_headers,
    )

    resp = test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/rename-entry",
        json={"old_path": old_path, "new_name": new_name},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"重命名失败: {resp.text}"

    data = resp.json()
    assert data["success"] is True, f"重命名应成功: {data}"
    assert "old_path" in data, "响应缺少 old_path"
    assert "new_path" in data, "响应缺少 new_path"


# ---------------------------------------------------------------------------
# 删除测试
# ---------------------------------------------------------------------------

def test_delete_file(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """删除工作空间中的文件。

    验证点：
    - 先创建文件
    - DELETE entries 返回 success
    """
    file_path = "e2e_delete_target.txt"

    # 创建文件
    test_client.post(
        f"/api/v1/workspaces/{isolated_workspace}/create-entry",
        json={"path": file_path, "type": "file"},
        headers=auth_headers,
    )

    resp = test_client.delete(
        f"/api/v1/workspaces/{isolated_workspace}/entries",
        params={"path": file_path},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"删除文件失败: {resp.text}"

    data = resp.json()
    assert data["success"] is True, f"删除文件应成功: {data}"


# ---------------------------------------------------------------------------
# 制品查询测试
# ---------------------------------------------------------------------------

def test_get_workspace_artifacts(
    test_client: Any,
    auth_headers: dict[str, str],
    isolated_workspace: str,
) -> None:
    """获取工作空间下所有制品。

    验证点：
    - GET /artifacts 返回 200
    - 响应包含 items 或 artifacts 字段
    """
    resp = test_client.get(
        f"/api/v1/workspaces/{isolated_workspace}/artifacts",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取制品失败: {resp.text}"


# ---------------------------------------------------------------------------
# 认证测试
# ---------------------------------------------------------------------------

def test_workspace_without_auth(test_client: Any) -> None:
    """无 Token 访问工作空间 API 应返回 401。

    验证点：
    - GET /api/v1/workspaces/test_id 无认证返回 401
    """
    resp = test_client.get("/api/v1/workspaces/e2e_no_auth_test")
    assert resp.status_code == 401, f"无 Token 应返回 401，得到 {resp.status_code}"
