# @feature: FP-0.2.多设备目录树 fs-tree 端点单层列举契约 | @ci: python-coverage
# @feature: 多端目录树 fs-tree 索引端点 | @ci: python-coverage
"""fs-tree 多端目录树索引端点单测（server.py fs_tree / _fs_root_layer / _fs_list_layer）。

契约（与 workspace file-tree 单层语义同构，前端 FileTreeWidget 懒加载/诚实错误
渲染零改动复用）：
- 根层（path 空）= 本地盘（Windows 盘符枚举 / POSIX 单根 `/`）；设备分支不特判
  （R26b 定版：盘根下设备名挂载锚目录，下一层单层列举自然呈现）
- 非根层 = 任意绝对目录的一层子项（`{name, path(绝对), type}`；type 链接判定
  优先于目录——junction/symlink 是设备分支的预期形态）
- 单层条目数封顶（超限 truncated=true）；隐藏条目过滤（点前缀/__pycache__）
- 业务级错误走 200 信封 `workspace_status=fs_error`（组件错误态契约字段）
- http.handle 分发：GET /ext/workspace_service/workspaces/fs-tree（query: path=）

文件与目录内容走真实 tmp_path；不可读分支经 monkeypatch 注入 OSError；
平台分支经 monkeypatch sys.platform 驱动（根层枚举无其他平台差异调用）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from test_workspace_server_gaps import _load_server  # noqa: E402


@pytest.fixture
def srv() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _unwrap(resp: Any) -> Any:
    """解 http.handle 的 ok 信封（success/data.body base64 → JSON 载荷）。"""
    import base64
    import json

    assert resp["success"] is True
    return json.loads(base64.b64decode(resp["data"]["body"]))


def _make_tree(tmp_path: Path) -> Path:
    """真实目录树：子目录 + 普通文件 + 点前缀隐藏项 + __pycache__。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.md").write_text("x", encoding="utf-8")
    (tmp_path / "note.txt").write_text("y", encoding="utf-8")
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "junk.pyc").write_text("j", encoding="utf-8")
    return sub


class TestFsTreeRootLayer:
    """根层 = 本地盘枚举；设备分支不特判（盘根下一层自然呈现）。"""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="盘符枚举是 Windows 专属根层形态：win32 分支逐盘 Path.exists() 查真实文件系统，POSIX 宿主无盘符可枚举",
    )
    def test_win32_root_lists_existing_drives(self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        result = _run(srv.fs_tree(""))
        entries = result["entries"]
        assert result["truncated"] is False
        drive_paths = {e["path"] for e in entries}
        # tmp_path 所在盘必在枚举内（真实存在的盘）
        assert f"{tmp_path.drive}\\" in drive_paths
        for e in entries:
            assert e["type"] == "directory"
            assert e["path"].endswith("\\")

    def test_posix_root_is_single_slash(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        result = _run(srv.fs_tree(""))
        assert result == {
            "entries": [{"name": "/", "path": "/", "type": "directory"}],
            "truncated": False,
        }

    def test_root_layer_by_missing_path_param(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """dispatch 侧 path 缺参 = 根层（http.handle query 透传空串）。"""
        monkeypatch.setattr(sys, "platform", "linux")
        resp = _run(srv.http_handle(
            path="/ext/workspace_service/workspaces/fs-tree",
            method="GET",
            query={},
        ))
        assert _unwrap(resp)["entries"] == [{"name": "/", "path": "/", "type": "directory"}]


class TestFsTreeLayer:
    """非根层 = 任意绝对目录单层列举（条目形态/过滤/绝对 path 回传）。"""

    def test_lists_single_layer_with_absolute_paths(self, srv: Any, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        result = _run(srv.fs_tree(str(tmp_path)))
        by_name = {e["name"]: e for e in result["entries"]}
        # 单层：子目录内容不递归出现
        assert "inner.md" not in by_name
        # 条目形态：绝对 path + 类型判定
        assert by_name["sub"] == {
            "name": "sub",
            "type": "directory",
            "path": str(tmp_path / "sub"),
        }
        assert by_name["note.txt"]["type"] == "file"
        # 隐藏条目过滤（与工作区文件树同口径）
        assert ".hidden" not in by_name
        assert "__pycache__" not in by_name
        assert result["truncated"] is False

    def test_dispatch_routes_fs_tree(self, srv: Any, tmp_path: Path) -> None:
        """http.handle 分发到 fs_tree（query: path= 绝对目录）。"""
        _make_tree(tmp_path)
        resp = _run(srv.http_handle(
            path="/ext/workspace_service/workspaces/fs-tree",
            method="GET",
            query={"path": str(tmp_path)},
        ))
        names = {e["name"] for e in _unwrap(resp)["entries"]}
        assert {"sub", "note.txt"} <= names

    def test_truncation_marks_truncated_flag(self, srv: Any, tmp_path: Path) -> None:
        """超单层封顶（500）截断并如实打 truncated。"""
        for i in range(505):
            (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
        result = _run(srv.fs_tree(str(tmp_path)))
        assert len(result["entries"]) == 500
        assert result["truncated"] is True


class TestFsTreeErrors:
    """业务级错误 = 200 信封 workspace_status=fs_error（组件错误态契约字段）。"""

    def test_missing_dir_reports_fs_error(self, srv: Any, tmp_path: Path) -> None:
        result = _run(srv.fs_tree(str(tmp_path / "nope")))
        assert result["entries"] == []
        assert result["workspace_status"] == "fs_error"
        assert "nope" in result["error"]

    def test_relative_path_rejected(self, srv: Any) -> None:
        result = _run(srv.fs_tree("rel/dir"))
        assert result["workspace_status"] == "fs_error"
        assert "绝对" in result["error"]

    def test_file_path_rejected_as_not_directory(self, srv: Any, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        result = _run(srv.fs_tree(str(f)))
        assert result["workspace_status"] == "fs_error"

    def test_unreadable_dir_reports_fs_error(self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """目录不可读（PermissionError/OSError）= 如实报错，不伪装空目录。"""
        def _boom(_target: str) -> list[str]:
            raise PermissionError("denied")

        monkeypatch.setattr("os.listdir", _boom)
        result = _run(srv.fs_tree(str(tmp_path)))
        assert result["workspace_status"] == "fs_error"
        assert result["entries"] == []
