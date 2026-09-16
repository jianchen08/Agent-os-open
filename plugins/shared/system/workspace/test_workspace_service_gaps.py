# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""workspace_service.py 分支补测（state 行形状兜底 / 读面降级 / 扫描守卫）。

行为契约（断输入→输出/副作用，不钉实现）：
- resolve_workspace_from_state：读面返回非列表 → ("", "", False)；行非字典跳过，
  命中行照常解析（坐标 + task.submitted_by 归属 + 行域标记同源返回）
- resolve_merged_worktree_target：非列表读面 → None；非字典行跳过后命中行
  照常重定位；worktree 元数据缺 path / 缺 project_root 的行不产出悬空映射；
  读面异常按无命中处理（None + warning 留痕）
- _read_state_rows：sync/async 读面均消费；非列表 → None；读面异常 →
  None + warning（经 list_artifacts_by_workspace 公开面观察聚合结果）
- _scan_directory：深度安全兜底（max_depth=0 即空树）；Windows 设备路径
  （\\\\.\\ 前缀）与跨驱动器条目跳过，其余条目照常成树

外部依赖（artifacts 服务）沿用 sys.modules 注入伪模块；state 读面走
set_state_reader 正门注入（生产 on_load 同通道），不触内核。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_flat_module(name: str) -> Any:
    """以裸名加载本目录模块（与运行时平铺 import 同构，见 test_workspace_service.py）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_MODELS = _load_flat_module("models")
_WS = _load_flat_module("workspace_service")
WorkspaceService = _WS.WorkspaceService


@pytest.fixture(autouse=True)
def _clean_state_reader():
    """每个测试前后清空模块级 state 读面（防跨测试串扰）。"""
    _WS.set_state_reader(None)
    yield
    _WS.set_state_reader(None)


class _FakeArtifactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_artifacts_by_task(self, task_id: str, limit: int = 100) -> dict:
        self.calls.append(task_id)
        return {"items": [{"task_id": task_id}], "total": 1}


def _inject_artifact_service(fake: _FakeArtifactService) -> None:
    mod = types.ModuleType("artifacts.artifact_service")
    mod.get_artifact_service = lambda: fake
    sys.modules["artifacts.artifact_service"] = mod


# ═══════════════════════════════════════════════════════════
# resolve_workspace_from_state：读面行形状兜底
# ═══════════════════════════════════════════════════════════


class TestResolveWorkspaceDegenerateRows:
    @pytest.mark.parametrize("bad_rows", ["not-a-list", 3.14, {"pipeline_id": "p1"}])
    def test_non_list_rows_fail_closed(self, bad_rows: Any) -> None:
        _WS.set_state_reader(lambda: bad_rows)
        assert asyncio.run(WorkspaceService().resolve_workspace_from_state("p1")) == ("", "", False)

    @pytest.mark.parametrize("non_dict_rows", [["junk"], [1337], [None, ["nested"]]])
    def test_non_dict_rows_yield_no_hit(self, non_dict_rows: list[Any]) -> None:
        _WS.set_state_reader(lambda: non_dict_rows)
        assert asyncio.run(WorkspaceService().resolve_workspace_from_state("p1")) == ("", "", False)

    def test_non_dict_rows_skipped_and_valid_row_still_resolved(self) -> None:
        """脏行跳过不阻断：后续命中行照常解析（含同行归属字段与行域标记）。"""
        rows = [
            "junk",
            {"pipeline_id": "p2", "workspace": "D:/ws/other"},
            {"pipeline_id": "p1", "workspace": "D:/ws/hit", "task.submitted_by": "user-a"},
        ]
        _WS.set_state_reader(lambda: rows)
        result = asyncio.run(WorkspaceService().resolve_workspace_from_state("p1"))
        assert result == ("D:/ws/hit", "user-a", True)

    def test_session_row_without_task_identity_is_not_task_row(self) -> None:
        """会话投影行（ws_meta 带 session_id，无任务身份键）→ is_task_row=False。"""
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {"mode": "plain", "path": "D:/ws/sessions/thread-x", "session_id": "thread-x"},
            }
        ]
        _WS.set_state_reader(lambda: rows)
        result = asyncio.run(WorkspaceService().resolve_workspace_from_state("p1"))
        assert result == ("D:/ws/sessions/thread-x", "", False)


# ═══════════════════════════════════════════════════════════
# resolve_merged_worktree_target：读面行形状兜底与元数据守卫
# ═══════════════════════════════════════════════════════════


class TestResolveMergedWorktreeDegenerateRows:
    def _wt_target(self, tmp_path: Path) -> str:
        return str(tmp_path / "proj__wt_deadbeef" / "out.txt")

    @pytest.mark.parametrize("bad_rows", ["not-a-list", 42])
    def test_non_list_rows_no_redirect(self, tmp_path: Path, bad_rows: Any) -> None:
        _WS.set_state_reader(lambda: bad_rows)
        assert asyncio.run(WorkspaceService().resolve_merged_worktree_target(self._wt_target(tmp_path))) is None

    def test_non_dict_rows_skipped_and_valid_row_redirects(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        rows: list[Any] = [
            "junk",
            None,
            {
                "pipeline_id": "p1",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(tmp_path / "proj__wt_deadbeef"),
                    "project_root": str(project),
                },
            },
        ]
        _WS.set_state_reader(lambda: rows)
        result = asyncio.run(WorkspaceService().resolve_merged_worktree_target(self._wt_target(tmp_path)))
        assert result == str(project / "out.txt")

    @pytest.mark.parametrize(
        ("meta", "case"),
        [
            ({"mode": "worktree", "path": "", "project_root": "D:/proj"}, "缺副本 path"),
            ({"mode": "worktree", "path": "D:/ws/proj__wt_x"}, "缺 project_root"),
        ],
    )
    def test_incomplete_worktree_meta_not_redirectable(
        self, tmp_path: Path, meta: dict, case: str
    ) -> None:
        """目标含 __wt_ 且为绝对路径（不再短路），缺坐标的 worktree 行跳过。"""
        _WS.set_state_reader(lambda: [{"pipeline_id": "p1", "ws_meta": meta}])
        result = asyncio.run(WorkspaceService().resolve_merged_worktree_target(self._wt_target(tmp_path)))
        assert result is None, case

    def test_reader_exception_treated_as_no_hit(self, tmp_path: Path, caplog) -> None:
        def _boom() -> list[dict]:
            raise RuntimeError("bridge down")

        _WS.set_state_reader(_boom)
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                WorkspaceService().resolve_merged_worktree_target(self._wt_target(tmp_path))
            )
        assert result is None
        assert any("重定位失败" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# _read_state_rows：读面消费四形态（经 list_artifacts_by_workspace 公开面）
# ═══════════════════════════════════════════════════════════


class TestReadStateRowsShapes:
    def _setup(self) -> tuple[Any, _FakeArtifactService]:
        """容器工作区 + 伪制品服务（聚合结果经制品调用集合观察）。"""
        art = _FakeArtifactService()
        _inject_artifact_service(art)
        svc = WorkspaceService()
        asyncio.run(svc.get_or_create_workspace("proj"))
        return svc, art

    def test_sync_reader_rows_aggregated(self) -> None:
        svc, art = self._setup()
        _WS.set_state_reader(
            lambda: [
                {"pipeline_id": "proj"},
                {"pipeline_id": "child-1", "task.parent_project_id": "proj"},
                "junk",  # 非字典行在读面出口即被过滤
            ]
        )
        result = asyncio.run(svc.list_artifacts_by_workspace("proj"))
        assert result["total"] == 2
        assert set(art.calls) == {"proj", "child-1"}

    def test_async_reader_rows_aggregated(self) -> None:
        svc, art = self._setup()

        async def _read() -> list[dict]:
            return [{"pipeline_id": "child-a", "task.parent_project_id": "proj"}]

        _WS.set_state_reader(_read)
        result = asyncio.run(svc.list_artifacts_by_workspace("proj"))
        assert set(art.calls) == {"proj", "child-a"}

    def test_non_list_reader_rows_fail_closed(self) -> None:
        """读面返回非列表 → 视为桥未就绪：仅容器任务自身，制品列表留痕不完整。"""
        svc, art = self._setup()
        _WS.set_state_reader(lambda: "corrupted")
        result = asyncio.run(svc.list_artifacts_by_workspace("proj"))
        assert set(art.calls) == {"proj"}

    def test_reader_exception_degrades_to_container_only(self, caplog) -> None:
        svc, art = self._setup()

        def _boom() -> list[dict]:
            raise RuntimeError("state bridge broke")

        _WS.set_state_reader(_boom)
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(svc.list_artifacts_by_workspace("proj"))
        assert set(art.calls) == {"proj"}
        assert any("state 聚合读取失败" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# _scan_directory：深度兜底与特殊条目守卫
# ═══════════════════════════════════════════════════════════


class TestScanDirectoryGuards:
    def test_depth_cap_zero_returns_empty_tree(self, tmp_path: Path) -> None:
        """深度兜底生效：max_depth=0 直接空树；同目录默认深度照常成树（性质对照）。"""
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        svc = WorkspaceService()
        assert svc._scan_directory(str(tmp_path), str(tmp_path), max_depth=0) == []
        nodes = svc._scan_directory(str(tmp_path), str(tmp_path))
        assert [n.name for n in nodes] == ["a.txt"]

    @pytest.mark.skipif(os.name != "nt", reason="\\\\.\\ 设备路径守卫为 Windows 语义")
    def test_device_path_entry_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """设备命名空间条目（\\\\.\\ 前缀）不进文件树，普通条目照常成树。"""
        device_entry = "\\\\.\\PhysicalDrive1"

        def fake_listdir(path: str) -> list[str]:
            return ["a.txt", device_entry]

        monkeypatch.setattr(os, "listdir", fake_listdir)
        svc = WorkspaceService()
        nodes = svc._scan_directory(str(tmp_path), str(tmp_path))
        assert [(n.name, n.type) for n in nodes] == [("a.txt", "file")]

    @pytest.mark.skipif(os.name != "nt", reason="跨驱动器 relpath ValueError 为 Windows 语义")
    def test_cross_drive_entry_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """relpath 跨驱动器 ValueError → 条目跳过（不产出悬空相对路径）。"""

        def fake_listdir(path: str) -> list[str]:
            return ["b.txt", "Q:\\outside.txt"]

        monkeypatch.setattr(os, "listdir", fake_listdir)
        svc = WorkspaceService()
        nodes = svc._scan_directory(str(tmp_path), str(tmp_path))
        assert [(n.name, n.type) for n in nodes] == [("b.txt", "file")]
