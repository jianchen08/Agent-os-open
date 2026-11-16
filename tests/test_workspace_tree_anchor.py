# @feature: FP-0.2.多端同步 2c 统一路径树 | @ci: python-coverage
"""会话/任务工作空间统一路径树锚定测试（D9' 编址，台账 R19 铺 T5）。

断行为：
- 锚定纯函数（真实路径 → /本设备id/相对路径，D9' 全局设备前缀编址）；
- 与解析层（tree_core.resolve_path）的往返一致性：锚定出的统一路径经路由表
  解析回同一真实路径（一次翻译契约）；
- workspace_lifecycle 接线：会话域 ws_meta / 任务域 task.ws_meta / 坐标自愈 /
  继承镜像四处坐标落笔同刀携带 tree_path（锚定失败省略，绝不阻断创建）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "workspace_lifecycle"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
_ISOLATION_DIR = _SHARED_ROOT / "isolation"
_DISTRIBUTED_DIR = _SHARED_ROOT / "distributed_router"
for _p in (str(_PLUGIN_DIR), str(_SHARED_ROOT), str(_ISOLATION_DIR), str(_DISTRIBUTED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tree_anchor import workspace_tree_path  # noqa: E402
from tree_core import resolve_path  # noqa: E402

_MOD_NAME = "workspace_lifecycle_tree_anchor_under_test"


def _load_plugin_module() -> Any:
    """显式按路径加载 workspace_lifecycle plugin.py（裸名 plugin 防抢注）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return mod


@pytest.fixture
def plugin_mod() -> Any:
    return _load_plugin_module()


class _ScriptedManager:
    """工作空间服务替身（契约回放，同 test_workspace_lifecycle_gaps 形态）。"""

    def __init__(self, ws_meta: Any = None) -> None:
        self.ws_meta = ws_meta
        self.task_starts: list[tuple[Any, ...]] = []

    def on_task_start(self, task_id: str, source_path: str, task_data: dict[str, Any]) -> Any:
        self.task_starts.append((task_id, source_path, task_data))
        return self.ws_meta

    def on_session_start(self, ws_root: str) -> None:
        pass


class TestWorkspaceTreePath:
    def test_under_root_yields_device_prefix_path(self, tmp_path: Path) -> None:
        ws = tmp_path / "sessions" / "abc123"
        tree = workspace_tree_path(str(ws), device_id="desk-1", subtree_root=str(tmp_path))
        assert tree == "/desk-1/sessions/abc123"

    def test_root_itself_maps_to_bare_prefix(self, tmp_path: Path) -> None:
        assert workspace_tree_path(str(tmp_path), device_id="d", subtree_root=str(tmp_path)) == "/d"

    def test_outside_root_omitted(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "elsewhere"
        assert (
            workspace_tree_path(str(outside), device_id="d", subtree_root=str(tmp_path)) is None
        )

    @pytest.mark.parametrize(
        ("path", "device", "root"),
        [("", "d", "C:/r"), ("C:/r/x", "", "C:/r"), ("C:/r/x", "d", ""), ("rel/x", "d", "C:/r")],
    )
    def test_degenerate_inputs_omitted(self, path: str, device: str, root: str) -> None:
        assert workspace_tree_path(path, device_id=device, subtree_root=root) is None

    def test_roundtrip_with_resolution_layer(self, tmp_path: Path) -> None:
        """锚定 ↔ 解析往返同一（D9' 一次翻译契约）：real_path 还原为原工作区路径。"""
        ws = tmp_path / "workspace" / "t1" / "src"
        view = {
            "devices": [
                {
                    "device_id": "desk-1",
                    "online": True,
                    "capabilities": {"exec_channel": {"allowed_roots": [str(tmp_path)]}},
                }
            ]
        }
        tree = workspace_tree_path(str(ws), device_id="desk-1", subtree_root=str(tmp_path))
        assert tree is not None
        resolution = resolve_path(tree, view, self_device_id="other-device")
        assert resolution.owner_device == "desk-1"
        assert Path(resolution.real_path) == ws

    def test_self_prefix_resolves_local(self, tmp_path: Path) -> None:
        tree = workspace_tree_path(str(tmp_path / "s"), device_id="desk-1", subtree_root=str(tmp_path))
        view = {
            "devices": [
                {"device_id": "desk-1", "online": False, "capabilities": {"exec_channel": {"allowed_roots": [str(tmp_path)]}}}
            ]
        }
        assert tree is not None
        resolution = resolve_path(tree, view, self_device_id="desk-1")
        assert resolution.local is True
        assert resolution.reachable is True, "本端前缀可达性不依赖心跳（本地执行零网络开销）"


class TestSessionWorkspaceAnchoring:
    async def test_session_ws_meta_carries_tree_path(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base = tmp_path / "wsbase"
        base.mkdir()
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        monkeypatch.setattr(iw, "find_project_root", lambda: tmp_path)  # 同盘防跨盘退化
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = _ScriptedManager()

        result = await p._bootstrap(plugin_mod.PluginContext(state={"session_id": "sess-7"}, config={}))

        ws_meta = result.state_updates["ws_meta"]
        assert ws_meta["path"] == str(base / "sessions" / "sess7")
        assert ws_meta["tree_path"] == "/desk-9/sessions/sess7"

    async def test_anchor_failure_omits_tree_path_but_keeps_workspace(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """锚定基根解析失败 → tree_path 省略（锚定是增强不是依赖），工作空间照常创建。

        会话分支的基根在创建段已解析（fail-closed 同既有契约）；本景钉任务
        分支的锚定隔离——workspace_lifecycle 主创建已成功，树地址缺失只留痕。
        （resolve_workspace_base 只在锚定路径消费：默认源解析走
        get_workspace_base_dir，不受本注入影响。）
        """
        base = tmp_path / "wsbase"
        base.mkdir()
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)

        def _boom(base_path: Path, root_path: Path) -> Path:
            raise RuntimeError("no anchor base")

        monkeypatch.setattr(iw, "resolve_workspace_base", _boom)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        written: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            written.update(fields)

        plugin_mod.set_task_state_writer(_writer)
        default_ws = base / "task00000003"
        manager = _ScriptedManager(ws_meta={"mode": "plain", "path": str(default_ws)})
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = manager

        result = await p._bootstrap(plugin_mod.PluginContext(
            state={
                "task.id": "task00000003",
                "execution_context": {"isolation": {"level": "isolated"}, "workspace": {"mode": "plain"}},
            },
            config={},
        ))

        assert result.error is None
        ws_meta = result.state_updates["task.ws_meta"]
        assert ws_meta["path"] == str(default_ws)
        assert "tree_path" not in ws_meta
        assert written["task.ws_meta"] == ws_meta


class TestTaskWorkspaceAnchoring:
    async def test_task_ws_meta_carries_tree_path(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base = tmp_path / "wsbase"
        base.mkdir()
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        written: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            written.update(fields)

        plugin_mod.set_task_state_writer(_writer)
        default_ws = base / "task00000001"
        manager = _ScriptedManager(ws_meta={"mode": "plain", "path": str(default_ws)})
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = manager

        result = await p._bootstrap(plugin_mod.PluginContext(
            state={
                "task.id": "task00000001",
                "execution_context": {"isolation": {"level": "isolated"}, "workspace": {"mode": "plain"}},
            },
            config={},
        ))

        ws_meta = result.state_updates["task.ws_meta"]
        assert ws_meta["tree_path"] == "/desk-9/task00000001"
        assert written["task.ws_meta"]["tree_path"] == "/desk-9/task00000001"

    async def test_explicit_source_outside_base_omits_tree_path(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """显式挂靠项目（工作区在基根外）→ tree_path 省略（领地声明是部署面，不虚构）。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        base = tmp_path / "wsbase"
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        manager = _ScriptedManager(ws_meta={"mode": "shared", "path": str(proj)})
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = manager

        result = await p._bootstrap(plugin_mod.PluginContext(
            state={
                "task.id": "task00000002",
                "execution_context": {"workspace": {"source_path": str(proj), "mode": "plain", "explicit": True}},
            },
            config={},
        ))

        assert "tree_path" not in result.state_updates["task.ws_meta"]


class TestHealAndMirrorAnchoring:
    async def test_session_selfheal_carries_tree_path(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base = tmp_path / "wsbase"
        ws = base / "sessions" / "old123"
        ws.mkdir(parents=True)
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = _ScriptedManager()

        result = await p._bootstrap(plugin_mod.PluginContext(
            state={"session_id": "old123", "workspace": str(ws)}, config={}
        ))

        ws_meta = result.state_updates["ws_meta"]
        assert ws_meta["path"] == str(ws)
        assert ws_meta["tree_path"] == "/desk-9/sessions/old123"

    async def test_inherited_mirror_carries_tree_path(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base = tmp_path / "wsbase"
        parent_ws = base / "workspace" / "parenttask00"
        parent_ws.mkdir(parents=True)
        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", lambda: None, raising=False)
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "desk-9")
        written: dict[str, Any] = {}

        async def _writer(pipeline_id: str, fields: dict[str, Any]) -> None:
            written.update(fields)

        plugin_mod.set_task_state_writer(_writer)
        p = plugin_mod.WorkspaceLifecyclePlugin(config={})
        p._manager = _ScriptedManager()

        await p._bootstrap(plugin_mod.PluginContext(
            state={
                "task.id": "task00000009",
                "workspace": str(parent_ws),
                "lineage.parent_ws_meta": {"mode": "plain", "path": str(parent_ws)},
            },
            config={},
        ))

        assert written["task.ws_meta"]["tree_path"] == "/desk-9/workspace/parenttask00"
