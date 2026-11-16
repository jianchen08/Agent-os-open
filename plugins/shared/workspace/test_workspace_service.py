# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""workspace 插件（工作空间服务）单元测试。

覆盖（对齐 plugins/shared/workspace/）：
1. WorkspaceService —— 创建/查询/制品聚合/文件树/容器任务解析
2. 全局单例 get_workspace_service / reset_workspace_service
3. models —— Workspace 序列化往返

外部依赖（tasks.service_access / artifacts.artifact_service）全部用
sys.modules 注入伪模块替代，不依赖真实内核。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/workspace/
_SYSTEM_DIR = _PLUGIN_DIR.parent
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))


def _load_workspace_modules() -> dict[str, Any]:
    """以裸名加载本目录 {models,workspace_service}（与运行时平铺 import 同构）。

    workspace_service 内部 `from models import ...` 依赖插件目录在 sys.path——
    与 server.py 的 bootstrap_plugin 注入同构；全名 workspace.models 在合宿
    平铺环境被非包同名模块遮蔽（装载失败根因），不得回退。
    """
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    out: dict[str, Any] = {}
    for name in ("models", "workspace_service"):
        if name in sys.modules:
            out[name] = sys.modules[name]
            continue
        spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        out[name] = module
    return out


_MODS = _load_workspace_modules()
WorkspaceService = _MODS["workspace_service"].WorkspaceService
Workspace = _MODS["models"].Workspace
get_workspace_service = _MODS["workspace_service"].get_workspace_service
reset_workspace_service = _MODS["workspace_service"].reset_workspace_service


class _FakeTask:
    def __init__(self, task_id: str, parent_task_id: str | None = None, metadata: dict | None = None) -> None:
        self.id = task_id
        self.parent_task_id = parent_task_id
        self.metadata = metadata or {}


class _FakeTaskService:
    """伪 TaskService：get_task / list_subtasks。"""

    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.subtasks: dict[str, list[_FakeTask]] = {}

    def get_task(self, task_id: str) -> _FakeTask | None:
        return self.tasks.get(task_id)

    def list_subtasks(self, parent_id: str) -> list[_FakeTask]:
        return self.subtasks.get(parent_id, [])


class _FakeArtifactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_artifacts_by_task(self, task_id: str, limit: int = 100) -> dict:
        self.calls.append(task_id)
        return {"items": [{"task_id": task_id, "name": f"art-{task_id}"}], "total": 1}


def _inject_task_service(fake: _FakeTaskService) -> None:
    """注入伪 tasks.service_access（懒加载路径）。"""
    mod = types.ModuleType("tasks.service_access")
    mod.get_task_service = lambda: fake
    sys.modules["tasks.service_access"] = mod


def _inject_artifact_service(fake: _FakeArtifactService) -> None:
    mod = types.ModuleType("artifacts.artifact_service")
    mod.get_artifact_service = lambda: fake
    sys.modules["artifacts.artifact_service"] = mod


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# WorkspaceService：创建与查询
# ═══════════════════════════════════════════════════════════


class TestWorkspaceServiceCreate:
    def test_get_or_create_workspace(self) -> None:
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("task-001", session_id="s1", title="标题"))
        assert ws.container_task_id == "task-001"
        assert ws.session_id == "s1"
        assert ws.title == "标题"

    def test_get_or_create_workspace_idempotent(self) -> None:
        """同 container_task_id 二次获取返回同一实例。"""
        svc = WorkspaceService()
        ws1 = _run(svc.get_or_create_workspace("task-001"))
        ws2 = _run(svc.get_or_create_workspace("task-001", title="另一个标题"))
        assert ws1.id == ws2.id
        assert ws2.title == ws1.title  # 不覆盖已有

    def test_default_title_uses_task_id(self) -> None:
        """未传 title → 默认 f"工作空间-{id[:8]}"。"""
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("abcdefgh1234"))
        assert ws.title == "工作空间-abcdefgh"

    def test_get_workspace(self) -> None:
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("t1"))
        assert _run(svc.get_workspace("t1")) is ws
        assert _run(svc.get_workspace("missing")) is None


class TestWorkspaceServiceSingleton:
    def test_singleton_get_and_reset(self) -> None:
        reset_workspace_service()
        svc1 = get_workspace_service()
        svc2 = get_workspace_service()
        assert svc1 is svc2
        reset_workspace_service()
        assert get_workspace_service() is not svc1
        reset_workspace_service()


# ═══════════════════════════════════════════════════════════
# 制品聚合 / 文件树 / 容器任务解析
# ═══════════════════════════════════════════════════════════


class TestWorkspaceArtifacts:
    def test_list_artifacts_no_workspace(self) -> None:
        svc = WorkspaceService()
        result = _run(svc.list_artifacts_by_workspace("missing"))
        assert result == {"items": [], "total": 0}

    def test_list_artifacts_aggregates_tasks(self, monkeypatch) -> None:
        """聚合项目名下子任务的制品（挂靠键 = state 行 task.parent_project_id）。"""
        svc = WorkspaceService()
        _run(svc.get_or_create_workspace("proj-1"))

        async def read_rows():
            return [
                {"pipeline_id": "proj-1"},
                {"pipeline_id": "child-1", "task.parent_project_id": "proj-1"},
                {"pipeline_id": "child-2", "task.parent_project_id": "proj-1"},
                {"pipeline_id": "other", "task.parent_project_id": "proj-2"},
            ]

        monkeypatch.setattr(svc, "_read_state_rows", read_rows, raising=False)
        art = _FakeArtifactService()
        _inject_artifact_service(art)

        result = _run(svc.list_artifacts_by_workspace("proj-1"))

        assert result["total"] == 3
        assert set(art.calls) == {"proj-1", "child-1", "child-2"}

    def test_list_artifacts_state_reader_missing_fails_closed(self) -> None:
        """state 读面未注入 → 仅容器任务自身（legacy 镜像回退已退役）。"""
        svc = WorkspaceService()
        _run(svc.get_or_create_workspace("root-task"))
        _reset_state_reader()
        _inject_artifact_service(_FakeArtifactService())
        result = _run(svc.list_artifacts_by_workspace("root-task"))
        assert result["total"] == 1


class TestWorkspaceFileTree:
    def test_get_file_tree_lists_root_layer(self, tmp_path: Path) -> None:
        """根层单层列举：文件/目录分类正确，不下钻（子目录内容不出现在本层）。"""
        svc = WorkspaceService()
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "sub" / "b.md").write_text("b", encoding="utf-8")
        # 隐藏目录/文件与 __pycache__ 被跳过
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".hidden").write_text("h", encoding="utf-8")

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))

        assert result["truncated"] is False
        assert [(n["name"], n["type"]) for n in result["entries"]] == [
            ("a.txt", "file"),
            ("sub", "directory"),
        ]
        # 单层性质：path 为自根起的 posix 相对路径，且不因下钻带出子层内容
        assert result["entries"][1]["path"] == "sub"
        assert all(n["path"].count("/") == 0 for n in result["entries"])
        assert all("b.md" not in n["path"] for n in result["entries"])

    def test_get_file_tree_sub_path_lists_that_layer(self, tmp_path: Path) -> None:
        """path= 指定子目录：只列该层，path 前缀带全链路（posix）。"""
        svc = WorkspaceService()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "inner").mkdir()
        (tmp_path / "src" / "main.py").write_text("m", encoding="utf-8")

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path), sub_path="src"))

        assert [(n["name"], n["type"], n["path"]) for n in result["entries"]] == [
            ("inner", "directory", "src/inner"),
            ("main.py", "file", "src/main.py"),
        ]

    @pytest.mark.parametrize(
        ("bad_path", "label"),
        [
            ("../escape", "父目录上跳"),
            ("a/../../escape", "嵌套上跳"),
            ("C:/outside", "盘符绝对路径"),
            ("/absolute", "根绝对路径"),
        ],
    )
    def test_get_file_tree_rejects_escaping_sub_path(
        self, tmp_path: Path, bad_path: str, label: str
    ) -> None:
        """输入侧逃逸（.. 上跳/绝对路径/盘符）→ error 信封而非越界列举。"""
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path), sub_path=bad_path))
        assert result["entries"] == []
        assert "error" in result

    def test_get_file_tree_backslash_and_dot_segments_normalized(self, tmp_path: Path) -> None:
        """反斜杠分隔与点段归一：src\\.\\inner 等价 src/inner（词法归一不误伤）。"""
        svc = WorkspaceService()
        (tmp_path / "src" / "inner").mkdir(parents=True)
        (tmp_path / "src" / "inner" / "x.txt").write_text("x", encoding="utf-8")

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path), sub_path="src\\.\\inner"))

        assert [n["path"] for n in result["entries"]] == ["src/inner/x.txt"]

    def test_get_file_tree_invalid_base_path(self) -> None:
        """base_path 不存在 → 空层。"""
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path="/no/such/dir"))
        assert result["entries"] == []
        assert result["truncated"] is False

    def test_get_file_tree_现扫不落模型_详情与专树同源(self, tmp_path: Path) -> None:
        """get_file_tree 每次现列且不写 Workspace 模型（B11-S7）：
        详情端点 to_dict 不含 file_tree 冻结镜像，目录变化即时反映。"""
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("t1"))
        (tmp_path / "x.txt").write_text("x", encoding="utf-8")

        first = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert [n["name"] for n in first["entries"]] == ["x.txt"]

        # 目录变化后再次列举 → 跟随现目录（不返回上次冻结值）
        (tmp_path / "x.txt").unlink()
        (tmp_path / "y.txt").write_text("y", encoding="utf-8")
        second = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert [n["name"] for n in second["entries"]] == ["y.txt"]

        # 模型面无 file_tree 字段：详情序列化与专树端点同源（专树现列）
        assert "file_tree" not in ws.to_dict()

    def test_layer_entry_cap_truncates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """单层条目数封顶：超限截断打 truncated，且上限收紧即变红（删值自检）。"""
        svc = WorkspaceService()
        for i in range(501):
            (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(type(svc), "_LAYER_MAX_ENTRIES", 500)

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))

        # 性质：可见条目数恒 ≤ 上限；截断标记与上限边界一致
        assert len(result["entries"]) <= 500
        assert result["truncated"] is True
        # 小目录不受截断（区分度对照组）
        small = tmp_path / "small"
        small.mkdir()
        (small / "only.txt").write_text("o", encoding="utf-8")
        small_result = _run(svc.get_file_tree("t1", base_path=str(small)))
        assert small_result["truncated"] is False
        assert len(small_result["entries"]) == 1

    def test_layer_hidden_entries_do_not_consume_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """封顶计数只对可见条目：隐藏条目不挤占预算，截断的是可见项序列。"""
        svc = WorkspaceService()
        (tmp_path / ".dot").mkdir()
        for i in range(3):
            (tmp_path / f"v{i}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(type(svc), "_LAYER_MAX_ENTRIES", 2)

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))

        assert result["truncated"] is True
        assert len(result["entries"]) == 2
        assert [n["name"] for n in result["entries"]] == ["v0.txt", "v1.txt"]

    def test_windows_reserved_names_skipped(self, tmp_path: Path) -> None:
        """Windows 保留名（CON 等）跳过，普通条目照常列出（区分度对照）。"""
        (tmp_path / "CON").write_text("x", encoding="utf-8")
        (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert [n["name"] for n in result["entries"]] == ["keep.txt"]

    def test_layer_permission_error_degrades(self, tmp_path: Path, monkeypatch) -> None:
        """os.listdir 抛 OSError → 空层降级（错误是值：不崩、不部分返回）。"""

        def boom(path):
            raise PermissionError("denied")

        monkeypatch.setattr(os, "listdir", boom)
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert result["entries"] == []
        assert result["truncated"] is False

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction 依赖 mklink /J")
    def test_junction_listed_as_link_and_expandable(self, tmp_path: Path) -> None:
        """junction（worktree 依赖链接同款）按 link 呈现，不作为普通目录下钻；
        按 path 展开列目标第一层由单层语义天然封顶。"""
        real = tmp_path / "real-deps"
        real.mkdir()
        (real / "pkg").mkdir()
        (real / "pkg" / "index.js").write_text("m", encoding="utf-8")
        svc = WorkspaceService()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(tmp_path / "node_modules"), str(real)],
            check=True,
            capture_output=True,
        )
        try:
            root = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
            assert [(n["name"], n["type"]) for n in root["entries"]] == [
                ("node_modules", "link"),
                ("real-deps", "directory"),
            ]

            # 展开链接：列出的是链接目标的第一层，路径仍以工作区相对形态表达
            layer = _run(svc.get_file_tree("t1", base_path=str(tmp_path), sub_path="node_modules"))
            assert [(n["name"], n["type"], n["path"]) for n in layer["entries"]] == [
                ("pkg", "directory", "node_modules/pkg")
            ]
        finally:
            shutil.rmtree(str(tmp_path / "node_modules"), ignore_errors=True)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink 语义")
    def test_symlink_listed_as_link(self, tmp_path: Path) -> None:
        """符号链接目录按 link 呈现、不被穿透为 directory（POSIX 侧）。

        根层单层列举返回全部非隐藏子项（服务契约），断言按 name→type 全集
        形态：链接条目类型 = link（islink 判定先于 isdir），普通目录照常呈现。
        """
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(real, tmp_path / "linked", target_is_directory=True)
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert {n["name"]: n["type"] for n in result["entries"]} == {
            "linked": "link",
            "real": "directory",
        }


class TestProjectChildAggregation:
    def test_child_ids_by_parent_project_key(self, monkeypatch) -> None:
        """子链聚合：挂靠键 = state 行 task.parent_project_id（单跳，不递归）。"""
        svc = WorkspaceService()
        rows = [
            {"pipeline_id": "proj-1"},
            {"pipeline_id": "child-1", "task.parent_project_id": "proj-1"},
            {"pipeline_id": "child-2", "task.parent_project_id": "proj-1"},
            {"pipeline_id": "other", "task.parent_project_id": "proj-2"},
            {"pipeline_id": "no-project"},
        ]

        async def read_rows():
            return rows

        monkeypatch.setattr(svc, "_read_state_rows", read_rows, raising=False)
        assert _run(svc._get_child_task_ids("proj-1")) == {"child-1", "child-2"}

    def test_child_ids_state_reader_missing_fails_closed(self, caplog) -> None:
        """state 读面未注入 → fail-closed 空集并留痕。"""
        import logging

        _reset_state_reader()
        svc = WorkspaceService()
        with caplog.at_level(logging.WARNING):
            assert _run(svc._get_child_task_ids("proj-1")) == set()
        assert any("读面未注入" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# resolve_workspace_from_state：pipeline_id 工作区解析通道（R3 关联底座）
# ═══════════════════════════════════════════════════════════


def _reset_state_reader() -> None:
    """清掉模块级 state reader（防跨测试串扰）。"""
    _MODS["workspace_service"].set_state_reader(None)


class TestResolveWorkspaceFromState:
    """resolve_workspace_from_state 返回 (workspace_path, owner_sub, is_task_row) 元组。"""

    def test_reader_not_injected_returns_none(self) -> None:
        _reset_state_reader()
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("", "", False)

    def test_row_owner_submitted_by_returned(self) -> None:
        """同行 task.submitted_by 随坐标同源返回（归属闸权威字段）。"""
        rows = [
            {
                "pipeline_id": "p1",
                "workspace": "D:/ws/owned",
                "task.submitted_by": "user-a",
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("D:/ws/owned", "user-a", True)
        _reset_state_reader()

    def test_row_ws_meta_path_wins_over_project_root(self) -> None:
        """命中行取 ws_meta.path（worktree 坐标），不取 project_root。"""
        rows = [
            {"pipeline_id": "other"},
            {
                "pipeline_id": "p1",
                "ws_meta": {"path": "D:/ws/worktree-1", "project_root": "D:/proj"},
            },
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("D:/ws/worktree-1", "", False)
        _reset_state_reader()

    def test_row_workspace_scalar_fallback(self) -> None:
        rows = [{"pipeline_id": "p1", "workspace": "D:/ws/plain"}]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("D:/ws/plain", "", False)
        _reset_state_reader()

    def test_row_hit_without_workspace_keys_returns_none(self) -> None:
        rows = [{"pipeline_id": "p1", "task.status": "running"}]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("", "", False)
        _reset_state_reader()

    def test_no_matching_row_returns_none(self) -> None:
        rows = [{"pipeline_id": "p2", "workspace": "D:/ws/p2"}]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("", "", False)
        _reset_state_reader()

    def test_async_reader_supported(self) -> None:
        """注入约定 sync/async 均可（生产为 async handle.call 包装）。"""

        async def _read() -> list[dict]:
            return [{"pipeline_id": "p1", "workspace": "D:/ws/async"}]

        _MODS["workspace_service"].set_state_reader(_read)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("D:/ws/async", "", False)
        _reset_state_reader()

    def test_reader_exception_returns_none(self) -> None:
        def _boom() -> list[dict]:
            raise RuntimeError("bridge down")

        _MODS["workspace_service"].set_state_reader(_boom)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == ("", "", False)
        _reset_state_reader()

    def test_task_pipeline_prefers_task_ws_meta_and_relocates_merged_worktree(
        self, tmp_path: Path
    ) -> None:
        """任务管道 ws_meta 被会话投影污染（plain 会话目录）时，任务域镜像
        task.ws_meta 优先；worktree 副本已删（合并清理）且 project_root 存在
        → 重定位到合并目标，不再打开会话默认文件夹。"""
        session_dir = tmp_path / "sessions" / "thread-a4d3c62b"
        wt = tmp_path / "proj__wt_deadbeef"  # 已合并删除，不创建
        project = tmp_path / "proj"
        project.mkdir()
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {
                    "mode": "plain",
                    "path": str(session_dir),
                    "session_id": "thread-a4d3c62b",
                },
                "task.ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == (str(project), "", True)
        _reset_state_reader()

    def test_task_pipeline_worktree_alive_returns_worktree_path(self, tmp_path: Path) -> None:
        """运行中任务：worktree 副本存在 → 返回副本路径（真实工作区），不重定位。"""
        wt = tmp_path / "proj__wt_alive01"
        wt.mkdir()
        project = tmp_path / "proj"
        project.mkdir()
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {"mode": "plain", "path": str(tmp_path / "sessions")},
                "task.ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == (str(wt), "", True)
        _reset_state_reader()

    def test_session_pipeline_without_task_mirror_keeps_ws_meta(self, tmp_path: Path) -> None:
        """主会话管道（无 task.ws_meta 镜像）→ 回退 ws_meta，行为不变。"""
        session_dir = tmp_path / "sessions" / "thread-x"
        session_dir.mkdir(parents=True)
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {"mode": "plain", "path": str(session_dir)},
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_workspace_from_state("p1")) == (str(session_dir), "", False)
        _reset_state_reader()


# ═══════════════════════════════════════════════════════════
# resolve_meta_workspace_path：ws_meta 字典 → 可用工作区坐标
# ═══════════════════════════════════════════════════════════


class TestResolveMetaWorkspacePath:
    resolve_meta = staticmethod(_MODS["workspace_service"].resolve_meta_workspace_path)

    def test_plain_mode_returns_path(self, tmp_path: Path) -> None:
        assert self.resolve_meta({"mode": "plain", "path": str(tmp_path)}) == str(tmp_path)

    def test_worktree_alive_returns_copy_path(self, tmp_path: Path) -> None:
        """副本目录存在 → 返回副本（运行中任务的真实工作区）。"""
        wt = tmp_path / "wt"
        wt.mkdir()
        assert (
            self.resolve_meta(
                {"mode": "worktree", "path": str(wt), "project_root": str(tmp_path / "p")}
            )
            == str(wt)
        )

    def test_worktree_merged_relocates_to_project_root(self, tmp_path: Path) -> None:
        """副本已删（合并清理）而 project_root 存在 → 返回 project_root。"""
        project = tmp_path / "p"
        project.mkdir()
        assert (
            self.resolve_meta(
                {
                    "mode": "worktree",
                    "path": str(tmp_path / "proj__wt_gone"),
                    "project_root": str(project),
                }
            )
            == str(project)
        )

    @pytest.mark.parametrize(
        ("meta", "case"),
        [
            ({"mode": "worktree", "path": "", "project_root": "D:/p"}, "空 path"),
            (
                {
                    "mode": "worktree",
                    "path": "D:/nowhere__wt_1",
                    "project_root": "D:/nowhere_proj",
                },
                "副本与 project_root 都不存在 → 原样返回不伪造",
            ),
        ],
    )
    def test_edge_cases_return_path_verbatim_or_none(self, meta: dict, case: str) -> None:
        result = self.resolve_meta(meta)
        if case.startswith("空 path"):
            assert result is None, case
        else:
            assert result == meta["path"], case


# ═══════════════════════════════════════════════════════════
# resolve_merged_worktree_target：已合并 worktree 死路径重定位
# ═══════════════════════════════════════════════════════════


class TestResolveMergedWorktreeTarget:
    def test_dead_worktree_path_maps_to_project_root(self, tmp_path: Path) -> None:
        """worktree 副本内死路径 → project_root + 相对后缀（合并后副本即删的补偿）。"""
        project = tmp_path / "proj"
        wt = tmp_path / "proj__wt_deadbeef"
        target = wt / "sub" / "out.txt"
        rows = [
            {"pipeline_id": "other"},
            {
                "pipeline_id": "p1",
                "task.ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            },
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(target))) == str(
            project / "sub" / "out.txt"
        )
        _reset_state_reader()

    def test_task_ws_meta_single_key_hits(self, tmp_path: Path) -> None:
        """坐标单键 task.ws_meta（出生/创建时一次写全）命中重定位。"""
        project = tmp_path / "proj"
        wt = tmp_path / "proj__wt_cafe01"
        rows = [
            {
                "pipeline_id": "p1",
                "task.ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(wt / "a.txt"))) == str(
            project / "a.txt"
        )
        _reset_state_reader()

    def test_target_equal_to_worktree_root_returns_none(self, tmp_path: Path) -> None:
        """请求路径恰为 worktree 根本身（目录，非文件产物）→ 不映射。"""
        project = tmp_path / "proj"
        wt = tmp_path / "proj__wt_equal1"
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(wt))) is None
        _reset_state_reader()

    def test_path_outside_all_worktrees_returns_none(self, tmp_path: Path) -> None:
        """路径不在任何已登记 worktree 之内 → None（含子串撞车场景：__wt_ 出现在
        文件名但路径前缀不匹配）。"""
        project = tmp_path / "proj"
        wt = tmp_path / "proj__wt_deadbeef"
        rows = [
            {
                "pipeline_id": "p1",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(wt),
                    "project_root": str(project),
                },
            }
        ]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        stranger = tmp_path / "other__wt_x" / "f.txt"
        assert _run(svc.resolve_merged_worktree_target(str(stranger))) is None
        _reset_state_reader()

    @pytest.mark.parametrize(
        ("meta", "case"),
        [
            ({"mode": "plain", "path": "D:/ws/plain"}, "plain 模式非副本坐标"),
            ({"mode": "worktree", "path": "D:/ws/x__wt_1"}, "worktree 缺 project_root"),
        ],
    )
    def test_non_redirectable_meta_skipped(self, tmp_path: Path, meta: dict, case: str) -> None:
        """plain 元数据 / 缺 project_root 的 worktree 行 → 跳过，不产出悬空映射。"""
        rows = [{"pipeline_id": "p1", "task.ws_meta": meta}]
        _MODS["workspace_service"].set_state_reader(lambda: rows)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(tmp_path / "f.txt"))) is None, case
        _reset_state_reader()

    def test_path_without_wt_marker_short_circuits(self, tmp_path: Path) -> None:
        """绝对路径不含 __wt_ 段 → 不触读面直接 None。"""

        def _fail() -> list[dict]:
            raise AssertionError("不应触读 state")

        _MODS["workspace_service"].set_state_reader(_fail)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(tmp_path / "note.txt"))) is None
        _reset_state_reader()

    def test_relative_path_short_circuits(self) -> None:
        """相对路径（非 worktree 产物形态）→ 短路 None。"""

        def _fail() -> list[dict]:
            raise AssertionError("不应触读 state")

        _MODS["workspace_service"].set_state_reader(_fail)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target("rel/a__wt_b/f.txt")) is None
        _reset_state_reader()

    def test_reader_not_injected_returns_none(self, tmp_path: Path) -> None:
        _reset_state_reader()
        svc = WorkspaceService()
        assert (
            _run(svc.resolve_merged_worktree_target(str(tmp_path / "a__wt_b" / "f.txt")))
            is None
        )

    def test_async_reader_supported(self, tmp_path: Path) -> None:
        """注入约定 sync/async 均可（与 resolve_workspace_from_state 同口径）。"""
        project = tmp_path / "proj"
        wt = tmp_path / "proj__wt_async01"

        async def _read() -> list[dict]:
            return [
                {
                    "pipeline_id": "p1",
                    "task.ws_meta": {
                        "mode": "worktree",
                        "path": str(wt),
                        "project_root": str(project),
                    },
                }
            ]

        _MODS["workspace_service"].set_state_reader(_read)
        svc = WorkspaceService()
        assert _run(svc.resolve_merged_worktree_target(str(wt / "b.txt"))) == str(
            project / "b.txt"
        )
        _reset_state_reader()


# ═══════════════════════════════════════════════════════════
# server.get_file_tree：无工作区/目录缺失如实报错（不再折叠成空树）
# ═══════════════════════════════════════════════════════════


def _load_workspace_server() -> Any:
    """动态加载 server.py（SDK 可导入；flat workspace_service 模块随之注册）。"""
    plugin_dir = _PLUGIN_DIR
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sdk_dir = Path(__file__).resolve().parents[3] / "sdk" / "src"
    if str(sdk_dir) not in sys.path:
        sys.path.insert(0, str(sdk_dir))
    mod_name = "workspace_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _server_state_mod(server_mod: Any) -> Any:
    """server.py flat 导入的 workspace_service 模块（读面注入目标）。"""
    mod = sys.modules.get("workspace_service")
    assert mod is not None, "server.py 应已 flat 导入 workspace_service"
    return mod


class TestServerGetFileTreeStatus:
    def test_no_workspace_returns_status(self) -> None:
        """解析无果（无 reader）→ no_workspace 状态而非空树。"""
        server_mod = _load_workspace_server()
        state_mod = _server_state_mod(server_mod)
        state_mod.set_state_reader(None)

        result = _run(server_mod.get_file_tree("ghost-task", caller={"sub": "user-a"}))
        assert result["workspace_status"] == "no_workspace"
        assert result["entries"] == []
        assert "无工作区" in result["error"]

    def test_dir_missing_returns_status(self) -> None:
        """坐标在、目录不在 → dir_missing 且 error 携带路径。"""
        server_mod = _load_workspace_server()
        state_mod = _server_state_mod(server_mod)
        missing = "D:/ws/definitely-missing-20260824"
        state_mod.set_state_reader(
            lambda: [
                {
                    "pipeline_id": "p1",
                    "workspace": missing,
                    "task.submitted_by": "user-a",
                }
            ]
        )

        result = _run(server_mod.get_file_tree("p1", caller={"sub": "user-a"}))
        assert result["workspace_status"] == "dir_missing"
        assert missing in result["error"]
        state_mod.set_state_reader(None)

    def test_existing_dir_returns_tree(self, tmp_path: Path) -> None:
        """目录存在 → 正常扫描，无 workspace_status 字段。"""
        (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
        server_mod = _load_workspace_server()
        state_mod = _server_state_mod(server_mod)
        state_mod.set_state_reader(
            lambda: [
                {
                    "pipeline_id": "p1",
                    "workspace": str(tmp_path),
                    "task.submitted_by": "user-a",
                }
            ]
        )

        result = _run(server_mod.get_file_tree("p1", caller={"sub": "user-a"}))
        assert "workspace_status" not in result
        names = [n.get("name") for n in result["entries"]]
        assert "hello.txt" in names
        state_mod.set_state_reader(None)

    def test_task_service_mirror_channel_retired(self, tmp_path: Path) -> None:
        """镜像通道删除回归：state 桥无命中时不再回退 task_service metadata
        （镜像属双写歧路）——镜像有任务也如实 no_workspace。"""
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        server_mod = _load_workspace_server()
        state_mod = _server_state_mod(server_mod)
        state_mod.set_state_reader(None)
        task_svc = _FakeTaskService()
        task_svc.tasks["t1"] = _FakeTask(
            "t1", metadata={"ws_meta": {"path": str(tmp_path)}, "user_id": "user-a"}
        )
        _inject_task_service(task_svc)

        result = _run(server_mod.get_file_tree("t1", caller={"sub": "user-a"}))
        assert result["workspace_status"] == "no_workspace"
        assert result["entries"] == []


class TestWorkspaceOwnershipGate:
    """U6 归属闸：未认证 / 跨任务归属 → WorkspaceAccessDenied（404 语义）。"""

    def _make_server_with_row(self, tmp_path: Path, submitted_by: str) -> Any:
        """构造任务行（task.id 在场）：闸门按 submitted_by 有无分 fail-closed /
        跨归属拒绝两态（会话域行无任务身份，不经归属闸，见 http 车道 BUG-24 用例）。"""
        (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
        server_mod = _load_workspace_server()
        state_mod = _server_state_mod(server_mod)
        row: dict[str, Any] = {"pipeline_id": "p1", "task.id": "p1", "workspace": str(tmp_path)}
        if submitted_by:
            row["task.submitted_by"] = submitted_by
        state_mod.set_state_reader(lambda: [row])
        return server_mod

    def test_unauthenticated_caller_denied(self, tmp_path: Path) -> None:
        """caller 缺失 / 无 sub → 一律拒绝（防存在性探测）。"""
        server_mod = self._make_server_with_row(tmp_path, "user-a")
        try:
            with pytest.raises(server_mod.WorkspaceAccessDenied):
                _run(server_mod.get_file_tree("p1"))
            with pytest.raises(server_mod.WorkspaceAccessDenied):
                _run(server_mod.get_file_tree("p1", caller={"sub": ""}))
        finally:
            _server_state_mod(server_mod).set_state_reader(None)

    def test_cross_owner_denied(self, tmp_path: Path) -> None:
        """归属不一致 → 拒绝；行缺归属元数据 → fail-closed 拒绝。"""
        server_mod = self._make_server_with_row(tmp_path, "user-a")
        try:
            with pytest.raises(server_mod.WorkspaceAccessDenied):
                _run(server_mod.get_file_tree("p1", caller={"sub": "user-b"}))

            no_owner_mod = self._make_server_with_row(tmp_path, "")
            with pytest.raises(no_owner_mod.WorkspaceAccessDenied):
                _run(no_owner_mod.get_file_tree("p1", caller={"sub": "user-a"}))
        finally:
            _server_state_mod(server_mod).set_state_reader(None)


class TestResolveCallerTokenFormats:
    """内核 token 自解析兼容性（GUI 黑盒测试 2026-09-11）。

    30d1b0959 起登录 token 追加 HMAC 签名尾巴（exp 后还有两段），旧
    split(":", 3) 解析恒败 → caller 恒空 → 文件预览等归属闸端点全量 404。
    """

    @staticmethod
    def _token(payload: str) -> str:
        import base64

        return base64.b64encode(payload.encode("utf-8")).decode("ascii")

    def test_legacy_4part_token_resolves(self) -> None:
        server_mod = _load_workspace_server()
        caller = server_mod._resolve_caller(
            {"authorization": "Bearer " + self._token("access:user-1:admin:9999999999")}
        )
        assert caller == {"sub": "user-1", "username": "admin"}

    def test_hmac_signed_token_resolves(self) -> None:
        """新格式（30d1b0959 后实测形态）：base64(payload).base64(signature)
        两段式带点号——payload 内 exp 后还有 :hmac:sign 两段。点号必须先切掉，
        否则签名段混入解码会破坏 UTF-8（GUI 实测文件预览全量 404 的根因）。"""
        import base64

        server_mod = _load_workspace_server()
        payload = self._token(
            "access:user-1:admin:9999999999:8a7228c9b9d7484bb1ecf53682af04d0:6d0f0536"
        )
        signature = base64.b64encode(b"fake-signature-bytes").decode("ascii")
        token = payload + "." + signature
        caller = server_mod._resolve_caller({"authorization": "Bearer " + token})
        assert caller == {"sub": "user-1", "username": "admin"}

    def test_expired_signed_token_rejected(self) -> None:
        server_mod = _load_workspace_server()
        caller = server_mod._resolve_caller(
            {
                "authorization": "Bearer "
                + self._token("access:user-1:admin:1:deadbeef:cafe")
            }
        )
        assert caller == {}

    def test_garbage_token_rejected(self) -> None:
        server_mod = _load_workspace_server()
        assert server_mod._resolve_caller({"authorization": "Bearer !!!notb64"}) == {}
        assert server_mod._resolve_caller({"authorization": "Bearer " + self._token("x")}) == {}


# ═══════════════════════════════════════════════════════════
# models：序列化往返
# ═══════════════════════════════════════════════════════════


class TestModels:
    def test_workspace_roundtrip(self) -> None:
        ws = Workspace(
            container_task_id="t1",
            session_id="s1",
            title="标题",
        )
        restored = Workspace.from_dict(ws.to_dict())
        assert restored.container_task_id == "t1"
        assert restored.title == "标题"

    def test_workspace_from_dict_defaults(self) -> None:
        ws = Workspace.from_dict({})
        assert ws.id
        assert ws.container_task_id == ""


# ═══════════════════════════════════════════════════════════
# 子任务聚合失败可观测（兜底反模式审查 P15，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestChildTaskAggregationFailureWarns:
    def test_state_path_failure_warns_and_empty_set(self, caplog, monkeypatch) -> None:
        """P15：state 路径聚合异常 → 空集 + warning（制品列表不完整可见）。"""
        import asyncio
        import logging

        svc = WorkspaceService()

        async def boom():
            raise RuntimeError("state bridge broke")

        monkeypatch.setattr(svc, "_read_state_rows", boom, raising=False)
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(svc._get_child_task_ids("container-1"))
        assert result == set(), "降级语义保持（空集）"
        assert any("子任务聚合失败" in r.getMessage() for r in caplog.records)

    def test_reader_missing_fails_closed_warns_and_empty_set(self, caplog, monkeypatch) -> None:
        """P15：读面未注入 fail-closed 同款留痕（legacy 镜像回退已退役）。"""
        import asyncio
        import logging

        svc = WorkspaceService()

        async def rows_none():
            return None  # state 读面未注入

        monkeypatch.setattr(svc, "_read_state_rows", rows_none, raising=False)
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(svc._get_child_task_ids("container-2"))
        assert result == set()
        assert any("读面未注入" in r.getMessage() and "子任务聚合失败" in r.getMessage() for r in caplog.records)
