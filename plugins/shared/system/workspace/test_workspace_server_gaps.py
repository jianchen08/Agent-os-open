# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""workspace server.py 缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（行号语义经源码逐行确认）：
- server.py:323-324（get_file_content 的已合并 worktree 死路径重定位命中后
  以 project_root 副本继续读取）、
  613-615 与 620（open_workspace_in_ide 无 tool-executor 能力时走系统文件
  管理器兜底：成功与失败两态）、
  705（_resolve_workspace_path 项目登记通道的 sys.path 自举插入）、
  714-715（登记通道异常降级：记 warning 后继续后续通道）、
  1022-1024 与 1029（workspace.get_file_tree 无显式 base_path 的解析通道：
  workspace_status 如实失败 / 正常成树）

外部依赖边界（pipeline-state 读面 / task_service 镜像 / project_registry
登记 / 系统文件管理器调用）全部用替身或 sys.modules 注入；文件与目录内容
走真实 tmp_path。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODELS = None


def _load_flat(name: str) -> Any:
    """以裸名加载本目录模块（与运行时平铺 import 同构）。

    裸名跨插件同名（models.py / storage.py 等遍布各插件目录）：车道共跑时
    sys.modules 里可能已缓存**别的插件**的同名模块，直接复用会让本文件断言
    打到错误实现（单跑绿、共跑红的经典串扰）。故只复用来源确为本目录的缓存，
    否则逐出重载。
    """
    target = (_PLUGIN_DIR / f"{name}.py").resolve()
    cached = sys.modules.get(name)
    if cached is not None and Path(getattr(cached, "__file__", "")).resolve() == target:
        return cached
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_MODELS = _load_flat("models")
_WS = _load_flat("workspace_service")


def _load_server() -> Any:
    mod_name = "workspace_server_gaps_probe"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sdk_dir = Path(__file__).resolve().parents[4] / "sdk" / "src"
    if str(sdk_dir) not in sys.path:
        sys.path.insert(0, str(sdk_dir))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_state_reader() -> Any:
    """每个测试前后清空模块级 state 读面（防跨测试串扰）。"""
    _WS.set_state_reader(None)
    yield
    _WS.set_state_reader(None)


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _rebind_flat_modules() -> None:
    """夹具期重绑裸名实例（车道共跑自防御）。

    本文件在收集期经 _load_flat 绑定 _MODELS/_WS；车道共跑时其他文件可能
    逐出并重载同目录裸名模块，探针（_load_server）在夹具期绑到的是重载后的
    新实例——测试往收集期旧实例设 state 钩子、被测代码读新实例（恒 None），
    单跑绿、共跑红的实例分叉串扰。重绑让两者共享同一实例后再装载探针。
    """
    global _MODELS, _WS
    _MODELS = _load_flat("models")
    _WS = _load_flat("workspace_service")


@pytest.fixture
def srv() -> Any:
    """加载 server 并注入 state 读面（可被各测试覆盖）。"""
    _rebind_flat_modules()
    return _load_server()


# ═══════════════════════════════════════════════════════════
# get_file_content：已合并 worktree 死路径重定位（323-324）
# ═══════════════════════════════════════════════════════════


class TestFileContentWorktreeRelocation:
    """死路径重定位命中后按 project_root 副本读取（返回体保留原请求路径）。"""

    def _setup(self, srv: Any, tmp_path: Path) -> tuple[str, Path, Path]:
        """构造 worktree 元数据 + project_root 中真实存在的文件。"""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "out.txt").write_text("重定位后的内容", encoding="utf-8")
        wt_root = tmp_path / "proj__wt_deadbeef"
        dead = wt_root / "out.txt"  # 副本已删 → 死路径
        _WS.set_state_reader(lambda: [
            {
                "pipeline_id": "p-dead",
                "workspace": str(wt_root),
                "task.submitted_by": "user-a",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(wt_root),
                    "project_root": str(project),
                },
            }
        ])
        return str(dead), project, wt_root

    def test_dead_worktree_path_content_served_from_project_root(
        self, srv: Any, tmp_path: Path
    ) -> None:
        dead_path, project, wt_root = self._setup(srv, tmp_path)
        # 请求路径的归属边界取自 worktree 元数据（重定位锚定 state 元数据）
        result = _run(
            srv.get_file_content("p-dead", dead_path, caller={"sub": "user-a"})
        )
        assert result["success"] is True
        assert result["content"] == "重定位后的内容"
        assert result["path"] == dead_path, "返回体保留原请求路径（前端契约不变）"
        assert result["size"] == len("重定位后的内容".encode())

    def test_unmapped_dead_path_still_reports_missing(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """对照组：边界内死路径但无 worktree 元数据可命中 → 如实报文件不存在。"""
        project = tmp_path / "proj"
        project.mkdir()
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-plain", "workspace": str(project), "task.submitted_by": "user-a"}
        ])
        result = _run(
            srv.get_file_content(
                "p-plain", str(project / "ghost.txt"), caller={"sub": "user-a"}
            )
        )
        assert result["success"] is False
        assert "文件不存在" in result["message"]

    def test_in_boundary_dead_path_relocated_after_file_check(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """边界内死路径（副本是项目根子目录）→ 经 315-324 的后置重定位读取。

        与上例的分支差异：路径落在归属边界内，故不进 283-296 的界外放行，
        而是在 `full_path.is_file()` 为假后由 319-324 兜住。
        """
        project = tmp_path / "proj"
        project.mkdir()
        (project / "report.md").write_text("报告正文", encoding="utf-8")
        wt_root = project / "proj__wt_inside"  # 副本落项目根子目录内
        dead = wt_root / "report.md"
        _WS.set_state_reader(lambda: [
            {
                "pipeline_id": "p-in",
                "workspace": str(project),
                "task.submitted_by": "user-a",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(wt_root),
                    "project_root": str(project),
                },
            }
        ])

        result = _run(srv.get_file_content("p-in", str(dead), caller={"sub": "user-a"}))

        assert result["success"] is True
        assert result["content"] == "报告正文"
        assert result["path"] == str(dead), "返回体保留原请求路径"

    def test_in_boundary_dead_path_without_meta_reports_missing(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """对照组：边界内死路径且无 worktree 元数据 → 如实报文件不存在。"""
        project = tmp_path / "proj"
        project.mkdir()
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-nometa", "workspace": str(project), "task.submitted_by": "user-a"}
        ])
        result = _run(
            srv.get_file_content(
                "p-nometa",
                str(project / "proj__wt_x" / "ghost.md"),
                caller={"sub": "user-a"},
            )
        )
        assert result["success"] is False
        assert "文件不存在" in result["message"]

    def test_remapped_target_missing_on_disk_reports_missing(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """副本已删且 project_root 无同名文件 → 重定位不产出假成功，仍报不存在。"""
        project = tmp_path / "proj"
        project.mkdir()
        wt_root = tmp_path / "proj__wt_missing"
        _WS.set_state_reader(lambda: [
            {
                "pipeline_id": "p-gone",
                "workspace": str(project),
                "task.submitted_by": "user-a",
                "ws_meta": {
                    "mode": "worktree",
                    "path": str(wt_root),
                    "project_root": str(project),
                },
            }
        ])
        result = _run(
            srv.get_file_content(
                "p-gone", str(wt_root / "never.txt"), caller={"sub": "user-a"}
            )
        )
        assert result["success"] is False
        assert "路径超出工作空间范围" in result["message"]


# ═══════════════════════════════════════════════════════════
# open_workspace_in_ide：文件管理器兜底两态（613-615 / 620）
# ═══════════════════════════════════════════════════════════


def _seed_state_workspace(srv: Any, tmp_path: Path, pipeline_id: str = "p-open") -> Path:
    """经 state 读面种一个带坐标与归属的任务行，返回工作空间路径。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    _WS.set_state_reader(lambda: [
        {
            "pipeline_id": pipeline_id,
            "workspace": str(ws),
            "task.submitted_by": "user-a",
        }
    ])
    return ws


class TestOpenWorkspaceFileManagerFallback:
    """无 tool-executor 能力（_connector_caller is None）时的兜底分支。"""

    def _workspace(self, srv: Any, tmp_path: Path) -> Path:
        return _seed_state_workspace(srv, tmp_path)

    def test_file_manager_success_path(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """兜底成功 → success=True + 宿主机路径（613-615）。"""
        ws = self._workspace(srv, tmp_path)
        srv._connector_caller = None
        monkeypatch.setattr(srv, "_open_in_system_file_manager", lambda p: True)

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert "系统文件管理器" in result["message"]
        assert result["path"] == str(ws)

    def test_file_manager_failure_path(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """兜底失败 → success=False 且如实说明（620），不谎报已打开。"""
        ws = self._workspace(srv, tmp_path)
        srv._connector_caller = None
        monkeypatch.setattr(srv, "_open_in_system_file_manager", lambda p: False)

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is False
        assert "tool-executor 能力未授予" in result["message"]
        assert result["path"] == str(ws)

    def test_caller_receives_container_path(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """兜底调用收到容器路径（宿主机转换只在返回体上，有区分度断言）。"""
        ws = self._workspace(srv, tmp_path)
        srv._connector_caller = None
        seen: list[str] = []
        def _fake_open(p: str) -> bool:
            seen.append(p)
            return True

        monkeypatch.setattr(srv, "_open_in_system_file_manager", _fake_open)
        _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))
        assert seen == [str(ws)]

    def test_real_file_manager_rejects_missing_dir(self, srv: Any, tmp_path: Path) -> None:
        """真实实现（非替身）：目录不存在 → False，不启动进程。"""
        assert srv._open_in_system_file_manager(str(tmp_path / "ghost")) is False


class TestOpenWorkspaceConnectorEnvelope:
    """tool-executor 信封形状下的连接器结果消费（no_connector 双位读取）。

    真机 2026-09-21 断链：tool-executor 轴返回内核 ToolExecutionResult 信封
    （success/error 在顶层、工具返回嵌 data），消费面只读顶层标记 → 连接器
    未连接时系统文件管理器兜底成死路径，打开文件夹恒报「连接器执行失败」。
    """

    def _caller_returning(self, srv: Any, envelope: dict[str, Any]) -> None:
        async def _fake(action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
            return envelope

        srv._connector_caller = _fake

    def test_no_connector_marker_in_data_falls_back(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """标记在 data 内（tool-executor 轴经归一层的现实形状）→ 兜底打开。"""
        ws = _seed_state_workspace(srv, tmp_path)
        self._caller_returning(srv, {
            "success": False,
            "data": {"no_connector": True, "action_type": "open_folder"},
            "error": "没有已连接的连接器支持动作: open_folder",
        })
        monkeypatch.setattr(srv, "_open_in_system_file_manager", lambda p: True)

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert "系统文件管理器" in result["message"]
        assert result["path"] == str(ws)

    def test_top_level_no_connector_still_falls_back(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """顶层标记（服务轴直调形状）→ 兜底不回退。"""
        _seed_state_workspace(srv, tmp_path)
        self._caller_returning(srv, {
            "success": False,
            "no_connector": True,
            "data": None,
            "error": "没有已连接的连接器支持动作: open_folder",
        })
        monkeypatch.setattr(srv, "_open_in_system_file_manager", lambda p: True)

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True

    def test_business_failure_does_not_fall_back(self, srv: Any, tmp_path: Path, monkeypatch) -> None:
        """业务失败（无标记）→ 不兜底，如实报连接器执行失败。"""
        _seed_state_workspace(srv, tmp_path)
        self._caller_returning(srv, {"success": False, "data": None, "error": "IDE 未就绪"})
        opened: list[str] = []
        monkeypatch.setattr(srv, "_open_in_system_file_manager", lambda p: opened.append(p))

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is False
        assert "连接器执行失败" in result["message"]
        assert "IDE 未就绪" in result["message"]
        assert opened == []

    def test_success_connector_type_read_from_data(self, srv: Any, tmp_path: Path) -> None:
        """成功路径 connector_type 在 data 内（信封 ②-b(B) 内嵌形状）→ 消息可见。"""
        _seed_state_workspace(srv, tmp_path)
        self._caller_returning(srv, {
            "success": True,
            "data": {"success": True, "connector_type": "vscode"},
        })

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert "vscode" in result["message"]


class TestOpenFileConnectorEnvelope:
    """open_file_in_ide 的信封形状消费（无兜底面：如实区分未连接与执行失败）。"""

    def test_no_connector_marker_in_data_reported_as_unconnected(self, srv: Any) -> None:
        async def _fake(action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
            return {
                "success": False,
                "data": {"no_connector": True, "action_type": "open_file"},
                "error": "没有已连接的连接器支持动作: open_file",
            }

        srv._connector_caller = _fake
        result = _run(srv.open_file_in_ide({"file_path": "D:/x/a.py"}))
        assert result["success"] is False
        assert "没有可用的 IDE 连接器" in result["message"]

    def test_business_failure_reported_as_execute_failure(self, srv: Any) -> None:
        async def _fake(action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "data": None, "error": "IDE 未就绪"}

        srv._connector_caller = _fake
        result = _run(srv.open_file_in_ide({"file_path": "D:/x/a.py"}))
        assert result["success"] is False
        assert "连接器执行失败" in result["message"]


# ═══════════════════════════════════════════════════════════
# _resolve_workspace_path：项目登记通道（705 / 714-715）
# ═══════════════════════════════════════════════════════════


class TestProjectRegistryChannel:
    """项目登记通道：sys.path 自举（705）与异常降级（714-715）。"""

    def test_registry_hit_returns_project_path(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """登记命中 → 归属校验通过后返回项目文件夹路径。"""
        project_dir = tmp_path / "registered-project"
        project_dir.mkdir()
        registry_mod = types.ModuleType("project_registry")
        registry_mod.load_project_paths = lambda: {"proj-1": str(project_dir)}
        monkeypatch.setitem(sys.modules, "project_registry", registry_mod)

        class _Project:
            submitted_by = "user-a"

        class _Registry:
            def get(self, project_id: str) -> Any:
                return _Project() if project_id == "proj-1" else None

        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            types.SimpleNamespace(get_project_registry=lambda: _Registry()),
        )
        monkeypatch.setattr(srv, "_assert_project_ownership", lambda pid, sub: None)

        resolved = _run(srv._resolve_workspace_path("proj-1", caller={"sub": "user-a"}))
        assert resolved == str(project_dir)

    def test_registry_channel_bootstraps_shared_root_on_demand(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """登记通道内 _SHARED_ROOT 不在 sys.path 时按需注入（705）。

        合宿进程里共享根通常已入列（短路），此处显式摘除以覆盖注入分支。
        """
        project_dir = tmp_path / "registered-project"
        project_dir.mkdir()
        registry_mod = types.ModuleType("project_registry")
        registry_mod.load_project_paths = lambda: {"proj-boot": str(project_dir)}
        monkeypatch.setitem(sys.modules, "project_registry", registry_mod)
        monkeypatch.setattr(srv, "_assert_project_ownership", lambda pid, sub: None)

        shared_root = str(Path(srv.__file__).resolve().parents[3])
        monkeypatch.setattr(
            sys, "path", [p for p in sys.path if p != shared_root]
        )
        assert shared_root not in sys.path

        resolved = _run(srv._resolve_workspace_path("proj-boot", caller={"sub": "user-a"}))

        assert resolved == str(project_dir)
        assert shared_root in sys.path, "登记通道应把共享根注入 sys.path"

    def test_registry_lookup_failure_degrades_to_next_channel(
        self, srv: Any, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """登记通道抛非归属异常 → warning + 继续走 state 通道（714-715）。"""
        fallback_ws = tmp_path / "state-ws"
        fallback_ws.mkdir()
        _WS.set_state_reader(lambda: [
            {
                "pipeline_id": "p-degrade",
                "workspace": str(fallback_ws),
                "task.submitted_by": "user-a",
            }
        ])

        def _boom() -> dict[str, str]:
            raise RuntimeError("registry file corrupted")

        registry_mod = types.ModuleType("project_registry")
        registry_mod.load_project_paths = _boom
        monkeypatch.setitem(sys.modules, "project_registry", registry_mod)

        with caplog.at_level("WARNING", logger="workspace"):
            resolved = _run(
                srv._resolve_workspace_path("p-degrade", caller={"sub": "user-a"})
            )

        assert resolved == str(fallback_ws), "登记通道失败不阻断后续通道"
        assert any("项目登记通道解析失败" in r.getMessage() for r in caplog.records)

    def test_registry_denied_is_not_swallowed(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """归属拒绝（WorkspaceAccessDenied）必须上抛，不被降级吞掉。"""
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-deny", "workspace": str(tmp_path), "task.submitted_by": "user-a"}
        ])
        registry_mod = types.ModuleType("project_registry")
        registry_mod.load_project_paths = lambda: {"p-deny": str(tmp_path)}
        monkeypatch.setitem(sys.modules, "project_registry", registry_mod)

        def _deny(pid: str, sub: str) -> None:
            raise srv.WorkspaceAccessDenied(srv._DENIED_MESSAGE)

        monkeypatch.setattr(srv, "_assert_project_ownership", _deny)

        with pytest.raises(srv.WorkspaceAccessDenied):
            _run(srv._resolve_workspace_path("p-deny", caller={"sub": "user-b"}))

    def test_registry_absent_key_falls_through(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """登记表无该 id → 不进入归属校验，继续后续通道（对照组）。"""
        ws = tmp_path / "ws-fall"
        ws.mkdir()
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-fall", "workspace": str(ws), "task.submitted_by": "user-a"}
        ])
        registry_mod = types.ModuleType("project_registry")
        registry_mod.load_project_paths = lambda: {}
        monkeypatch.setitem(sys.modules, "project_registry", registry_mod)

        called: list[str] = []
        monkeypatch.setattr(
            srv, "_assert_project_ownership", lambda pid, sub: called.append(pid)
        )
        resolved = _run(srv._resolve_workspace_path("p-fall", caller={"sub": "user-a"}))
        assert resolved == str(ws)
        assert called == [], "未命中登记行不应做归属校验"


# ═══════════════════════════════════════════════════════════
# workspace.get_file_tree 工具面：解析通道（1022-1024 / 1029）
# ═══════════════════════════════════════════════════════════


class TestToolGetFileTreeResolutionChannel:
    """MCP 工具面：无 base_path 时经 get_file_tree 解析通道（1022-1024 / 1029）。

    工具 handler 签名不含 caller（工具面不携带身份），故归属闸由解析通道注入面
    替代——与 tests/plugins/system/workspace/test_workspace_http.py 同一接缝。
    """

    @staticmethod
    def _inject_service(srv: Any) -> Any:
        service = _WS.get_workspace_service()
        srv._service = service
        return service

    @staticmethod
    def _inject_boundary(srv: Any, path: str | None) -> None:
        """控制 _resolve_workspace_path 的坐标解析结果（None = 无工作区坐标）。"""

        async def _fake(container_task_id: str, caller: Any = None) -> str | None:
            return path

        srv._resolve_workspace_path = _fake

    def test_workspace_status_failure_is_reported(self, srv: Any) -> None:
        """无工作区坐标 → success=False + workspace_status 透传（1022-1024）。"""
        self._inject_service(srv)
        self._inject_boundary(srv, None)

        result = _run(srv.workspace_get_file_tree("ghost-task"))

        assert result["success"] is False
        assert result["workspace_status"] == "no_workspace"
        assert result["error"], "失败时应携带可读原因（前端据此渲染）"

    def test_dir_missing_status_is_reported(self, srv: Any, tmp_path: Path) -> None:
        """坐标在但目录不在 → dir_missing 透传（有区分度输入）。"""
        self._inject_service(srv)
        self._inject_boundary(srv, str(tmp_path / "vanished"))

        result = _run(srv.workspace_get_file_tree("p-miss"))

        assert result["success"] is False
        assert result["workspace_status"] == "dir_missing"

    def test_existing_workspace_returns_tree(self, srv: Any, tmp_path: Path) -> None:
        """正常工作区 → success=True + 真实文件树（1029）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "hello.txt").write_text("hi", encoding="utf-8")
        (ws / "sub").mkdir()
        (ws / "sub" / "nested.txt").write_text("n", encoding="utf-8")
        self._inject_service(srv)
        self._inject_boundary(srv, str(ws))

        result = _run(srv.workspace_get_file_tree("p-ok"))

        assert result["success"] is True
        names = _collect_names(result["tree"])
        assert {"hello.txt", "sub", "nested.txt"} <= names
        assert "workspace_status" not in result

    def test_explicit_base_path_bypasses_resolution(self, srv: Any, tmp_path: Path) -> None:
        """对照组：显式 base_path → 直接扫描该目录（不经解析通道）。"""
        base = tmp_path / "explicit"
        base.mkdir()
        (base / "listed.txt").write_text("x", encoding="utf-8")
        self._inject_service(srv)

        result = _run(srv.workspace_get_file_tree("any-task", base_path=str(base)))

        assert result["success"] is True
        assert "listed.txt" in _collect_names(result["tree"])

    def test_service_uninitialized_guard(self, srv: Any) -> None:
        """服务未初始化 → 显式失败（守卫先于解析通道）。"""
        srv._service = None
        result = _run(srv.workspace_get_file_tree("t"))
        assert result["success"] is False
        assert result["error"] == "服务未初始化"


def _collect_names(tree: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    stack = list(tree)
    while stack:
        node = stack.pop()
        names.add(node.get("name", ""))
        stack.extend(node.get("children") or [])
    return names
