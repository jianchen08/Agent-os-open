# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""workspace server.py 缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（以函数名为准，行号随源码演进漂移）：
- server.py get_file_content 的已合并 worktree 死路径重定位命中后
  以 project_root 副本继续读取（后置 is_file 兜底位）、
  路径形态契约（绝对直读/相对守卫，用户裁定 2026-09-28）、
  open_workspace_in_ide / open_file_in_ide 宿主原生直开
  （Windows os.startfile / 其余平台 webbrowser.open，用户裁定 2026-10-08）：
  两平台分支、路径不存在与 OS 调用失败两态、
  项目登记显式入口（_resolve_project_workspace_path）的 sys.path 自举
  与异常按非项目实体降级、
  _resolve_container_path 路由（登记故障落 state 通道 / 归属拒绝上抛 /
  未登记键落 state 通道）、
  workspace.get_file_tree 无显式 base_path 的解析通道：
  workspace_status 如实失败 / 正常成树

外部依赖边界（pipeline-state 读面 / project_registry 登记 / 宿主打开调用
os.startfile 与 webbrowser）全部用替身或 sys.modules 注入；文件与目录内容
走真实 tmp_path。
"""

from __future__ import annotations

import importlib.util
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
    assert spec is not None
    assert spec.loader is not None
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
    sdk_dir = Path(__file__).resolve().parents[3] / "sdk" / "src"
    if str(sdk_dir) not in sys.path:
        sys.path.insert(0, str(sdk_dir))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
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
                "task.ws_meta": {
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
                "task.ws_meta": {
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
                "task.ws_meta": {
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
        assert "文件不存在" in result["message"]


# ═══════════════════════════════════════════════════════════
# get_file_content：绝对路径直读 / 相对路径锚定定位（裁定 2026-09-28 + 2026-10-24 延伸）
# ═══════════════════════════════════════════════════════════


class TestFileContentPathFormContract:
    """路径形态契约：绝对路径直读，相对路径锚定**仅定位不拒绝**
    （用户裁定 2026-10-24 延伸：打开面不通过路径限制用户访问；
    写面 save_file_content 守卫不变）。

    删值实验锚点：若打开面重新套上越界拒绝，三条（界外直读 ×2 +
    ../ 穿越）必红。
    """

    def _seed_workspace(self, tmp_path: Path, pipeline_id: str = "p-direct") -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        _WS.set_state_reader(lambda: [
            {"pipeline_id": pipeline_id, "workspace": str(ws), "task.submitted_by": "user-a"}
        ])
        return ws

    def test_existing_file_outside_workspace_reads_directly(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """工作空间兄弟目录下的既有文件 → 直读成功（界外拒绝已废）。"""
        self._seed_workspace(tmp_path)
        outside = tmp_path / "elsewhere" / "docs" / "项目介绍问答.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("界外内容", encoding="utf-8")

        result = _run(srv.get_file_content("p-direct", str(outside), caller={"sub": "user-a"}))

        assert result["success"] is True
        assert result["content"] == "界外内容"
        assert result["size"] == len("界外内容".encode())

    def test_existing_file_sharing_workspace_prefix_reads_directly(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """与工作空间同名前缀的界外文件（字符串前缀易误判形态）→ 直读成功。"""
        ws = self._seed_workspace(tmp_path)
        sibling = tmp_path / "ws-backup.md"  # 与 ws 同前缀、非界内
        sibling.write_text("前缀相邻", encoding="utf-8")

        result = _run(srv.get_file_content("p-direct", str(sibling), caller={"sub": "user-a"}))

        assert result["success"] is True
        assert result["content"] == "前缀相邻"
        assert result["size"] == len("前缀相邻".encode())
        assert ws.is_dir()

    def test_relative_escape_reads_after_2026_10_24_ruling(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """相对路径 ../ 穿越 → 锚定仅定位、打开面不拒（用户裁定 2026-10-24：
        能定位到即打开；写面守卫不变）。"""
        self._seed_workspace(tmp_path)
        (tmp_path / "secret.txt").write_text("机密", encoding="utf-8")

        result = _run(
            srv.get_file_content("p-direct", "../secret.txt", caller={"sub": "user-a"})
        )

        assert result["success"] is True
        assert result["content"] == "机密"


# ═══════════════════════════════════════════════════════════
# get_file_content：按需加载（offset/limit 字节窗口，用户裁定 2026-10-24）
# ═══════════════════════════════════════════════════════════


class TestFileContentPagination:
    """字节窗口分页：loaded/has_more 供前端「加载更多/加载全部」；无大小上限。"""

    def _seed_workspace_file(self, tmp_path: Path, payload: bytes, name: str = "big.bin") -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / name).write_bytes(payload)
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-page", "workspace": str(ws), "task.submitted_by": "user-a"}
        ])
        return ws

    def test_window_read_returns_loaded_and_has_more(self, srv, tmp_path) -> None:
        """offset+limit 窗口：返回窗口内容、loaded 末位与 has_more=True。"""
        self._seed_workspace_file(tmp_path, b"0123456789")

        result = _run(
            srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}, offset=2, limit=5)
        )

        assert result["success"] is True
        assert result["content"] == "23456"
        assert result["size"] == 10
        assert result["loaded"] == 7
        assert result["has_more"] is True

    def test_tail_window_marks_has_more_false(self, srv, tmp_path) -> None:
        """末窗：loaded == size → has_more=False（前端置全量标记）。"""
        self._seed_workspace_file(tmp_path, b"0123456789")

        result = _run(
            srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}, offset=5, limit=100)
        )

        assert result["success"] is True
        assert result["content"] == "56789"
        assert result["loaded"] == 10
        assert result["has_more"] is False

    def test_offset_without_limit_reads_to_eof(self, srv, tmp_path) -> None:
        """「加载全部」形态：offset 起不带 limit → 读到文件尾。"""
        self._seed_workspace_file(tmp_path, b"0123456789")

        result = _run(
            srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}, offset=4)
        )

        assert result["success"] is True
        assert result["content"] == "456789"
        assert result["has_more"] is False

    def test_default_full_read_compat(self, srv, tmp_path) -> None:
        """不带分页参数 → 全量语义（兼容既有调用面），返回体字段只增不删。"""
        self._seed_workspace_file(tmp_path, b"hello")

        result = _run(srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert result["content"] == "hello"
        assert result["size"] == 5
        assert result["loaded"] == 5
        assert result["has_more"] is False

    def test_negative_offset_clamped_to_zero(self, srv, tmp_path) -> None:
        """负 offset → 归 0 容错（不 500）。"""
        self._seed_workspace_file(tmp_path, b"abcdef")

        result = _run(
            srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}, offset=-3, limit=2)
        )

        assert result["success"] is True
        assert result["content"] == "ab"
        assert result["loaded"] == 2

    def test_offset_beyond_eof_returns_empty_window(self, srv, tmp_path) -> None:
        """offset 越过 EOF → 空窗口 + has_more=False（不报错）。"""
        self._seed_workspace_file(tmp_path, b"abc")

        result = _run(
            srv.get_file_content("p-page", "big.bin", caller={"sub": "user-a"}, offset=99, limit=10)
        )

        assert result["success"] is True
        assert result["content"] == ""
        assert result["loaded"] == 99
        assert result["has_more"] is False

    def test_file_over_10mb_opens_without_size_cap(self, srv, tmp_path) -> None:
        """10MB+ 文件按需窗口读取成功（打开面大小上限已废，用户裁定 2026-10-24）。"""
        self._seed_workspace_file(tmp_path, b"x" * (10 * 1024 * 1024 + 1))

        result = _run(
            srv.get_file_content(
                "p-page", "big.bin", caller={"sub": "user-a"}, offset=0, limit=64 * 1024
            )
        )

        assert result["success"] is True
        assert len(result["content"]) == 64 * 1024
        assert result["size"] == 10 * 1024 * 1024 + 1
        assert result["has_more"] is True


class TestFileContentHttpDispatchPagination:
    """http_handle file-content GET：offset/limit query 解析与非法值容错。"""

    @staticmethod
    def _http_payload(result: Any) -> dict[str, Any]:
        """解包 HttpHandleResponse（body base64 → JSON dict）。"""
        import base64
        import json

        data = result["data"]
        assert data["body_encoding"] == "base64"
        return json.loads(base64.b64decode(data["body"]).decode("utf-8"))

    def _seed(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "f.txt").write_bytes(b"0123456789")
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-http", "workspace": str(ws), "task.submitted_by": "user-a"}
        ])

    @staticmethod
    def _fake_caller(srv: Any) -> None:
        """绕过 kernel token 解析（单元级：身份闸不在本测覆盖面）。"""
        srv._resolve_caller = lambda headers: {"sub": "user-a"}

    def test_query_offset_limit_passed_through(self, srv, tmp_path) -> None:
        self._seed(tmp_path)
        self._fake_caller(srv)

        result = _run(srv.http_handle(
            path="/ext/workspace_service/workspaces/p-http/file-content",
            method="GET",
            query={"path": "f.txt", "offset": "3", "limit": "4"},
        ))

        payload = self._http_payload(result)
        assert payload["success"] is True
        assert payload["content"] == "3456"
        assert payload["has_more"] is True

    def test_invalid_offset_falls_back_to_zero(self, srv, tmp_path) -> None:
        self._seed(tmp_path)
        self._fake_caller(srv)

        result = _run(srv.http_handle(
            path="/ext/workspace_service/workspaces/p-http/file-content",
            method="GET",
            query={"path": "f.txt", "offset": "abc", "limit": "xyz"},
        ))

        payload = self._http_payload(result)
        # offset=abc → 0；limit=xyz → None（读到尾）→ 全量 10 字节
        assert payload["success"] is True
        assert payload["content"] == "0123456789"
        assert payload["has_more"] is False


# ═══════════════════════════════════════════════════════════
# open_workspace_in_ide：宿主原生直开（用户裁定 2026-10-08）
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


def _startfile_recorder(seen: list[str]) -> Any:
    """os.startfile 替身工厂：记录调用参数（显式 def，mypy func-returns-value 干净）。"""

    def _record(p: str) -> None:
        seen.append(p)

    return _record


def _webbrowser_recorder(seen: list[str]) -> Any:
    """webbrowser.open 替身工厂：记录 URI 并返回 True（显式 def，mypy 干净）。"""

    def _record(uri: str) -> bool:
        seen.append(uri)
        return True

    return _record


class TestOpenWorkspaceHostOpen:
    """宿主原生直开：两平台分支 + 失败面（路径不存在 / OS 调用异常）。

    os.startfile 仅 Windows 存在：monkeypatch 一律 raising=False（POSIX 上
    补属性再钉）。
    """

    def test_windows_branch_opens_via_startfile(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """Windows 分支：os.startfile 收到工作空间路径 → success=True。"""
        ws = _seed_state_workspace(srv, tmp_path)
        monkeypatch.setattr(srv.sys, "platform", "win32")
        opened: list[str] = []
        monkeypatch.setattr(srv.os, "startfile", _startfile_recorder(opened), raising=False)

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert opened == [str(ws)]
        assert result["path"] == str(ws)

    def test_posix_branch_falls_back_to_webbrowser_file_uri(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """非 Windows 分支：webbrowser.open 收到 file URI（路径不存在才轮到失败面）。"""
        ws = _seed_state_workspace(srv, tmp_path)
        monkeypatch.setattr(srv.sys, "platform", "linux")
        opened: list[str] = []
        monkeypatch.setattr(
            srv.webbrowser, "open", _webbrowser_recorder(opened)
        )

        result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is True
        assert opened == [Path(ws).as_uri()]

    def test_missing_path_reports_failure_without_os_call(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """坐标在目录不在 → success=False 且不发起 OS 调用（不假成功）。"""
        ghost = tmp_path / "ghost-ws"  # 不落盘
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-ghost", "workspace": str(ghost), "task.submitted_by": "user-a"}
        ])
        opened: list[str] = []
        monkeypatch.setattr(srv.os, "startfile", _startfile_recorder(opened), raising=False)

        result = _run(srv.open_workspace_in_ide("p-ghost", caller={"sub": "user-a"}))

        assert result["success"] is False
        assert "打开工作空间失败" in result["message"]
        assert result["path"] == str(ghost)
        assert opened == [], "路径缺失时不得发起 OS 打开调用"

    def test_os_call_failure_reports_failure_with_warning_trace(
        self, srv: Any, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """OS 调用异常 → success=False，且失败出口留带 container_task_id 的
        WARNING 痕迹（运维面）：用户提示（响应 message）与日志分离。"""
        import logging

        ws = _seed_state_workspace(srv, tmp_path)
        monkeypatch.setattr(srv.sys, "platform", "win32")

        def _boom(p: str) -> None:
            raise OSError("shell failure")

        monkeypatch.setattr(srv.os, "startfile", _boom, raising=False)

        with caplog.at_level(logging.WARNING):
            result = _run(srv.open_workspace_in_ide("p-open", caller={"sub": "user-a"}))

        assert result["success"] is False
        assert "打开工作空间失败" in result["message"]
        assert result["path"] == str(ws)
        assert any(
            "打开工作空间失败" in r.getMessage() and "p-open" in r.getMessage()
            for r in caplog.records
        )

    def test_real_host_open_rejects_missing_path(self, srv: Any, tmp_path: Path) -> None:
        """真实实现（非替身）：路径不存在 → False，不启动 OS 调用。"""
        assert srv._open_on_host(str(tmp_path / "ghost")) is False


class TestOpenWorkspaceNoCoordinateTrace:
    """无坐标失败退出必须留 WARNING 痕迹（同上，运维面可诊断）。"""

    def test_no_coordinate_failure_leaves_warning_trace(self, srv: Any, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            result = _run(
                srv.open_workspace_in_ide("task5dc60e00", caller={"sub": "user-a"})
            )

        assert result["success"] is False
        assert "工作空间路径" in result["message"]
        assert any(
            "无工作区坐标" in r.getMessage() and "task5dc60e00" in r.getMessage()
            for r in caplog.records
        )


class TestOpenFileHostOpen:
    """open_file_in_ide 宿主直开：成功调用参数 / 两平台分支 / 失败面。"""

    def test_success_calls_startfile_with_file_path(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        opened: list[str] = []
        # 钉 win32 分支（与下方 posix 分支测试同构）：POSIX 宿主无 os.startfile，
        # 不钉则落 webbrowser 回退分支，断言对象落空
        monkeypatch.setattr(srv.sys, "platform", "win32")
        monkeypatch.setattr(srv.os, "startfile", _startfile_recorder(opened), raising=False)

        result = _run(srv.open_file_in_ide({"file_path": str(f)}))

        assert result["success"] is True
        assert opened == [str(f.resolve())]
        assert result["file_path"] == str(f)

    def test_posix_branch_falls_back_to_webbrowser(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        f = tmp_path / "b.py"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(srv.sys, "platform", "linux")
        opened: list[str] = []
        monkeypatch.setattr(
            srv.webbrowser, "open", _webbrowser_recorder(opened)
        )

        result = _run(srv.open_file_in_ide({"file_path": str(f)}))

        assert result["success"] is True
        assert opened == [f.resolve().as_uri()]

    def test_missing_file_reports_failure_without_os_call(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(srv.os, "startfile", _startfile_recorder(opened), raising=False)

        result = _run(srv.open_file_in_ide({"file_path": str(tmp_path / "ghost.py")}))

        assert result["success"] is False
        assert "打开文件失败" in result["message"]
        assert opened == []

    def test_os_call_failure_reports_failure(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        f = tmp_path / "c.py"
        f.write_text("x", encoding="utf-8")

        def _boom(p: str) -> None:
            raise OSError("no association")

        # 钉 win32 分支：OS 调用失败面按 startfile 路径断言（POSIX 落 webbrowser）
        monkeypatch.setattr(srv.sys, "platform", "win32")
        monkeypatch.setattr(srv.os, "startfile", _boom, raising=False)

        result = _run(srv.open_file_in_ide({"file_path": str(f)}))

        assert result["success"] is False
        assert "打开文件失败" in result["message"]

    def test_empty_path_rejected(self, srv: Any) -> None:
        result = _run(srv.open_file_in_ide({"file_path": ""}))
        assert result["success"] is False
        assert "file_path 参数不能为空" in result["message"]


# ═══════════════════════════════════════════════════════════
# _resolve_project_workspace_path / _resolve_container_path：项目登记显式入口
# ═══════════════════════════════════════════════════════════


class TestProjectRegistryChannel:
    """项目登记显式入口：project.* 服务消费（T6a）与异常按非项目实体降级；
    路由层语义（服务故障落 state / 归属拒绝上抛 / 未登记键落 state）。"""

    @staticmethod
    def _inject_project_service(
        monkeypatch: Any, srv: Any, paths: Any, owner: str = "user-a"
    ) -> None:
        """project capability 桩：list_paths 按预设应答（Exception = 故障形态）；
        get 按登记键回固定归属行（归属闸消费面）。"""
        registry = paths if isinstance(paths, dict) else {}

        class _Handle:
            async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
                if method == "list_paths":
                    if isinstance(paths, Exception):
                        raise paths
                    return {"paths": paths}
                if method == "get":
                    pid = str(args.get("project_id") or "")
                    if pid in registry:
                        return {"found": True, "project": {"submitted_by": owner}}
                    return {"found": False}
                raise AssertionError(f"unexpected method: {method}")

        monkeypatch.setattr(srv.plugin, "get_capability", lambda name: _Handle())

    def test_registry_hit_returns_project_path(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """登记命中 → 归属校验通过后返回项目文件夹路径。"""
        project_dir = tmp_path / "registered-project"
        project_dir.mkdir()
        self._inject_project_service(monkeypatch, srv, {"proj-1": str(project_dir)})

        resolved = _run(
            srv._resolve_project_workspace_path("proj-1", caller={"sub": "user-a"})
        )
        assert resolved == str(project_dir)

    def test_registry_lookup_failure_degrades_to_state_channel(
        self, srv: Any, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """登记读取抛非归属异常 → warning + 按非项目实体落 state 通道。"""
        fallback_ws = tmp_path / "state-ws"
        fallback_ws.mkdir()
        _WS.set_state_reader(lambda: [
            {
                "pipeline_id": "p-degrade",
                "workspace": str(fallback_ws),
                "task.submitted_by": "user-a",
            }
        ])

        self._inject_project_service(
            monkeypatch, srv, RuntimeError("project.list_paths failed")
        )

        with caplog.at_level("WARNING", logger="workspace"):
            resolved = _run(
                srv._resolve_container_path("p-degrade", caller={"sub": "user-a"})
            )

        assert resolved == str(fallback_ws), "登记故障不阻断 state 通道"
        assert any("项目登记通道解析失败" in r.getMessage() for r in caplog.records)

    def test_registry_denied_is_not_swallowed(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """归属拒绝（WorkspaceAccessDenied）必须上抛，不被降级吞掉。"""
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-deny", "workspace": str(tmp_path), "task.submitted_by": "user-a"}
        ])
        self._inject_project_service(monkeypatch, srv, {"p-deny": str(tmp_path)})

        async def _deny(pid: str, sub: str) -> None:
            raise srv.WorkspaceAccessDenied(srv._DENIED_MESSAGE)

        monkeypatch.setattr(srv, "_assert_project_ownership", _deny)

        with pytest.raises(srv.WorkspaceAccessDenied):
            _run(srv._resolve_container_path("p-deny", caller={"sub": "user-b"}))

    def test_registry_absent_key_falls_through(
        self, srv: Any, tmp_path: Path, monkeypatch
    ) -> None:
        """登记表无该 id → 不进入归属校验，落 state 通道（对照组）。"""
        ws = tmp_path / "ws-fall"
        ws.mkdir()
        _WS.set_state_reader(lambda: [
            {"pipeline_id": "p-fall", "workspace": str(ws), "task.submitted_by": "user-a"}
        ])
        self._inject_project_service(monkeypatch, srv, {})

        called: list[str] = []

        async def _ownership(pid: str, sub: str) -> None:
            called.append(pid)

        monkeypatch.setattr(srv, "_assert_project_ownership", _ownership)
        resolved = _run(srv._resolve_container_path("p-fall", caller={"sub": "user-a"}))
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
        """控制 _resolve_container_path 的坐标解析结果（None = 无工作区坐标）。"""

        async def _fake(container_task_id: str, caller: Any = None) -> str | None:
            return path

        srv._resolve_container_path = _fake

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
        names = _collect_names(result["entries"])
        # 单层语义：根层只含直接子项，嵌套内容随子目录懒加载另列
        assert {"hello.txt", "sub"} <= names
        assert "nested.txt" not in names
        assert "workspace_status" not in result

    def test_explicit_base_path_bypasses_resolution(self, srv: Any, tmp_path: Path) -> None:
        """对照组：显式 base_path → 直接扫描该目录（不经解析通道）。"""
        base = tmp_path / "explicit"
        base.mkdir()
        (base / "listed.txt").write_text("x", encoding="utf-8")
        self._inject_service(srv)

        result = _run(srv.workspace_get_file_tree("any-task", base_path=str(base)))

        assert result["success"] is True
        assert "listed.txt" in _collect_names(result["entries"])

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


# ═══════════════════════════════════════════════════════════
# 直派任务出生行首查命中（2026-10-08 装机实锤 5dc60e00 回归钉）
# ═══════════════════════════════════════════════════════════


class TestBirthRowFirstQueryHit:
    """直派任务仅带出生 state（无任何管道运行记录）→ get_file_tree 首查即命中。

    tasks/root 直派经 task_submit 出生协议创建管道：出生登记（阶段一）把
    task.*/lineage.* 逐键落表（kernel chat_send_handler 创建即落 pipeline_state），
    显式 plain 拓扑随出生写 task.ws_meta（task_submit 兑现「任务 init 即写」）。
    本类钉消费侧契约：state 聚合行只要携带出生键集合，解析通道首查即出坐标——
    不依赖管道起跑后 workspace_lifecycle 的 init 镜像。
    """

    @staticmethod
    def _stub_task_mirror(monkeypatch: Any) -> None:
        """镜象通道钉死为未命中（本类只验证 state 出生通道，不依赖 YAML 镜像）。"""
        tasks_pkg = types.ModuleType("tasks")
        tasks_pkg.__path__ = []
        sa_stub = types.ModuleType("tasks.service_access")
        sa_stub.get_task_service = lambda: None
        tasks_pkg.service_access = sa_stub
        monkeypatch.setitem(sys.modules, "tasks", tasks_pkg)
        monkeypatch.setitem(sys.modules, "tasks.service_access", sa_stub)

    @staticmethod
    def _birth_row(ws_dir: Path, *, with_ws_meta: bool) -> dict[str, Any]:
        """task_submit 出生登记（阶段一）的行形态：仅出生键，无运行记录。"""
        row: dict[str, Any] = {
            "pipeline_id": "task5dc60e00",
            "task.goal": "直派任务",
            "task.status": "pending",
            "task.description": "",
            "task.acceptance_criteria": {},
            "task.dependencies": [],
            "task.submitted_by": "user-a",
            "lineage.root": True,
            "lineage.origin.kind": "plugin",
            "lineage.origin.source": "task_submit",
            "task.orchestration": "autonomous",
        }
        if with_ws_meta:
            row["task.ws_meta"] = {
                "mode": "plain",
                "path": str(ws_dir),
                "project_root": str(ws_dir),
            }
        return row

    def test_birth_row_with_task_ws_meta_hits_on_first_query(
        self, srv: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        ws = tmp_path / "01ed89f3952e"
        ws.mkdir()
        (ws / "hello.txt").write_text("hi", encoding="utf-8")
        self._stub_task_mirror(monkeypatch)
        _WS.set_state_reader(lambda: [self._birth_row(ws, with_ws_meta=True)])

        caller = {"sub": "user-a"}
        first = _run(srv.get_file_tree("task5dc60e00", caller=caller))
        retry = _run(srv.get_file_tree("task5dc60e00", caller=caller))

        assert "workspace_status" not in first
        assert "hello.txt" in _collect_names(first["entries"])
        # 幂等性质：首查与重试同形（命中不靠重试暖场）
        assert first == retry

    def test_birth_row_without_task_ws_meta_still_reports_no_workspace(
        self, srv: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """对照（符号量级相反）：出生行无 task.ws_meta（修复前形态）→ no_workspace。

        证明上一条的断言有区分度：命中来自出生行携带的 task.ws_meta，而非
        行存在本身。
        """
        ws = tmp_path / "plain-dir"
        ws.mkdir()
        self._stub_task_mirror(monkeypatch)
        _WS.set_state_reader(lambda: [self._birth_row(ws, with_ws_meta=False)])

        result = _run(srv.get_file_tree("task5dc60e00", caller={"sub": "user-a"}))

        assert result["workspace_status"] == "no_workspace"
        assert result["error"], "失败时应携带可读原因"
