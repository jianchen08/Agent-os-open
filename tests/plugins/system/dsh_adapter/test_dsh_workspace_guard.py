# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""dsh_read/dsh_glob 工作区边界锚定测试（B-9 路径直通封口）。

缺陷（08-19 安全审查 B-9）：dsh_read 的 file_path / dsh_glob 的 path 直通
Node 桥，可读宿主任意路径。修复契约（照 builtin_tools fs_tools
_check_workspace_path 范式）：路径以 workspace/project_root 为锚——相对路径
锚定到根解析，绝对路径必须落在根内；无注入锚点一律 fail-closed 拒绝；
越界返回桥同构失败信封（success=False），Node 桥零调用。

外部依赖（Node runtime 桥）以 fake bridge 替身记录调用参数。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.unit

import server as dsh_server  # noqa: E402 - 裸名导入经 conftest sys.path 注入


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeBridge:
    """记录 call_tool 入参的桥替身（不触碰 Node runtime）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "read":
            data: dict[str, Any] = {
                "path": args.get("file_path"),
                "offset": 1,
                "lines": [],
                "totalLines": 0,
            }
        else:
            data = {"root": args.get("path") or "", "paths": []}
        return {"success": True, "data": data, "error": None, "duration_ms": 0.0}


@pytest.fixture
def fake_bridge(monkeypatch: pytest.MonkeyPatch) -> _FakeBridge:
    bridge = _FakeBridge()
    monkeypatch.setattr(dsh_server, "get_bridge", lambda: bridge)
    return bridge


@pytest.fixture
def ws_dir(tmp_path: Any) -> str:
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "ws-evil").mkdir()
    return str(ws)


class TestDshReadWorkspaceGuard:
    def test_read_outside_workspace_denied(self, fake_bridge: _FakeBridge, tmp_path: Any, ws_dir: str) -> None:
        outside = tmp_path / "outside" / "secret.txt"
        result = _run(dsh_server.dsh_read(str(outside), workspace=ws_dir))
        assert result["success"] is False
        assert "超出工作空间" in result["error"]
        assert fake_bridge.calls == []

    def test_read_sibling_prefix_denied(self, fake_bridge: _FakeBridge, tmp_path: Any, ws_dir: str) -> None:
        """…/ws-evil 不得因字符串前缀命中 …/ws 边界。"""
        sibling = tmp_path / "ws-evil" / "x.txt"
        result = _run(dsh_server.dsh_read(str(sibling), workspace=ws_dir))
        assert result["success"] is False
        assert fake_bridge.calls == []

    def test_read_absolute_inside_workspace_passes_anchored(
        self, fake_bridge: _FakeBridge, tmp_path: Any, ws_dir: str
    ) -> None:
        target = tmp_path / "ws" / "note.md"
        result = _run(dsh_server.dsh_read(str(target), workspace=ws_dir))
        assert result["success"] is True
        assert len(fake_bridge.calls) == 1
        name, args = fake_bridge.calls[0]
        assert name == "read"
        assert args["file_path"] == str(target.resolve())

    def test_read_relative_path_anchored_to_workspace(self, fake_bridge: _FakeBridge, ws_dir: str) -> None:
        result = _run(dsh_server.dsh_read("note.md", workspace=ws_dir))
        assert result["success"] is True
        _, args = fake_bridge.calls[0]
        assert args["file_path"] == str((__import__("pathlib").Path(ws_dir) / "note.md").resolve())

    def test_read_without_anchor_fail_closed(self, fake_bridge: _FakeBridge, tmp_path: Any) -> None:
        """无 workspace/project_root 注入 → 一律拒绝（不做 cwd 兜底）。"""
        result = _run(dsh_server.dsh_read(str(tmp_path / "any.txt")))
        assert result["success"] is False
        assert fake_bridge.calls == []

    def test_read_project_root_anchor_when_no_workspace(self, fake_bridge: _FakeBridge, tmp_path: Any) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        result = _run(dsh_server.dsh_read("src/a.py", project_root=str(root)))
        assert result["success"] is True
        _, args = fake_bridge.calls[0]
        assert args["file_path"] == str((root / "src" / "a.py").resolve())


class TestDshGlobWorkspaceGuard:
    def test_glob_outside_workspace_denied(self, fake_bridge: _FakeBridge, tmp_path: Any, ws_dir: str) -> None:
        result = _run(dsh_server.dsh_glob("**/*.ts", path=str(tmp_path / "outside"), workspace=ws_dir))
        assert result["success"] is False
        assert fake_bridge.calls == []

    def test_glob_inside_workspace_anchored(self, fake_bridge: _FakeBridge, tmp_path: Any, ws_dir: str) -> None:
        sub = tmp_path / "ws" / "src"
        result = _run(dsh_server.dsh_glob("**/*.ts", path=str(sub), workspace=ws_dir))
        assert result["success"] is True
        name, args = fake_bridge.calls[0]
        assert name == "glob"
        assert args["path"] == str(sub.resolve())

    def test_glob_default_path_anchors_to_workspace(self, fake_bridge: _FakeBridge, ws_dir: str) -> None:
        """缺省 path 时以工作空间根为搜索目录（桥缺省语义不可达任意目录）。"""
        result = _run(dsh_server.dsh_glob("**/*.ts", workspace=ws_dir))
        assert result["success"] is True
        _, args = fake_bridge.calls[0]
        assert args["path"] == ws_dir

    def test_glob_without_anchor_fail_closed(self, fake_bridge: _FakeBridge) -> None:
        result = _run(dsh_server.dsh_glob("**/*.ts"))
        assert result["success"] is False
        assert fake_bridge.calls == []
