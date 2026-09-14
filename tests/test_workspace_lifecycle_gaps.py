# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""workspace_lifecycle 缺口分支补测（coverage.xml 缺口行靶单）。

按行为分组补齐此前未覆盖的分支（断输入→输出/副作用，不钉实现）：

1. 会话工作区键：session_id 优先 / pipeline_id 兜底 / 非目录安全字符清洗 /
   清洗后为空回落 default——同基目录下所有会话共用一层隔离目录，不外溢。
2. state 聚合行刷新：reader 未注入早退；reader 返回协程 / 返回 list（非 dict
   条目剔除）/ 非 list（保留旧快照）；reader 抛异常沿用旧快照且留痕。
3. _ExecutionContextTaskTree.get_task：聚合行命中 / 行缺失且非当前任务 → None /
   行缺失且是当前任务 → lineage 兜底 / 行命中 → 父链 + ws_meta 透传
   （原生 dict 与跨边界 JSON 字符串两形态）。
4. 服务实例化：manager 已缓存直接复用；实例化异常 → None（任务管道显式报错，
   主会话仅跳过 skills 同步）；base_path_hint 优先于 config.base_path。
5. execute 分发：current_phase=init/exit/其它（main 零产出）；exit 零操作。
6. bootstrap 幂等与重建：state.workspace 已就位 → 跳过创建 + 补写继承镜像；
   指向不存在目录 → 重建（禁止踩壳）；无 execution_context / 无 workspace
   声明 → 跳过创建；主会话无任务身份 → 会话目录；主会话显式源路径 → plain 解析。
7. 主会话工作区：isolation 路径解析失败 → 零产出（本会话文件工具无锚点）；
   服务可用 → skills 同步（失败仅留痕，工作区仍生效）。
8. 任务工作区：默认根解析失败 → 显式报错；服务不可用 → 显式报错（无降级）；
   创建抛异常 → 显式报错；返回无有效路径 → 显式报错；
   默认源按 mode 分流（worktree=项目根 / plain=工作区根/{task_id}）。
9. task_data 构造：is_root 由 lineage 推导、isolation level 透传、
   explicit 标记与继承坐标（optional_dict 容错非 dict 形态）。
10. ws_meta 镜像：writer 未绑定跳过；写失败留痕不阻断；补写路径无父链坐标
    → 不虚构坐标（维持门控 fail-closed 读取失败语义）。

不可达/环境依赖残留（逐条说明，勿硬凑）：
1. ``_bootstrap`` 里 ``ws_state_path`` 非空且目录存在时的 ``state["workspace"]``
   直接下标——与 ``ws_state_path`` 同一表达式（``str(state.get("workspace") or "")``
   非空即真），恒不抛 KeyError。
2. ``_get_manager`` 的 ``self._manager = None`` 赋值后返回 None 的路径已覆盖；
   ``ctx_await`` 用 ``asyncio.get_running_loop()``，仅在协程内被调用（无运行
   循环时抛 RuntimeError 属调用方违约，非本插件分支）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "shared" / "pipeline" / "input" / "workspace_lifecycle"
_SHARED_ROOT = _PLUGIN_DIR.parents[2]  # plugins/shared
_ISOLATION_DIR = _SHARED_ROOT / "system" / "isolation"
for _p in (str(_SHARED_ROOT), str(_ISOLATION_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD_NAME = "workspace_lifecycle_plugin_gaps_under_test"


def _load_module() -> Any:
    """显式按路径加载本插件 plugin.py（裸名 plugin 会被同进程其它插件抢注）。"""
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
    return _load_module()


async def _session_start(manager: Any, workspace: Path) -> None:
    """经线程池调 on_session_start（同步服务方法，与插件 ctx_await 同式）。"""
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: manager.on_session_start(str(workspace))
    )


class _ScriptedManager:
    """工作空间服务替身：脚本化 on_task_start / on_session_start。

    真服务是外部协作对象（git/文件系统拓扑），此处只按契约回放其返回
    形态与异常，并记录实参（用于断言 task_data 装配）。
    """

    def __init__(self, ws_meta: Any = None, error: Exception | None = None,
                 session_error: Exception | None = None) -> None:
        self.ws_meta = ws_meta
        self.error = error
        self.session_error = session_error
        self.task_starts: list[tuple[Any, ...]] = []
        self.session_starts: list[str] = []

    def on_task_start(self, task_id: str, source_path: str, task_data: dict[str, Any]) -> Any:
        self.task_starts.append((task_id, source_path, task_data))
        if self.error is not None:
            raise self.error
        return self.ws_meta

    def on_session_start(self, ws_root: str) -> None:
        self.session_starts.append(ws_root)
        if self.session_error is not None:
            raise self.session_error


def _plugin(mod: Any, manager: Any = None, *, config: dict[str, Any] | None = None) -> Any:
    p = mod.WorkspaceLifecyclePlugin(config=config or {})
    if manager is not None:
        p._manager = manager
    return p


def _ctx(mod: Any, state: dict[str, Any]) -> Any:
    return mod.PluginContext(state=state, config={})




class _ScriptedManager:
    """工作空间服务替身：脚本化 on_task_start / on_session_start。

    真服务是外部协作对象（git/文件系统拓扑），此处只按契约回放其返回
    形态与异常，并记录实参（用于断言 task_data 装配）。
    """

    def __init__(self, ws_meta: Any = None, error: Exception | None = None,
                 session_error: Exception | None = None) -> None:
        self.ws_meta = ws_meta
        self.error = error
        self.session_error = session_error
        self.task_starts: list[tuple[Any, ...]] = []
        self.session_starts: list[str] = []

    def on_task_start(self, task_id: str, source_path: str, task_data: dict[str, Any]) -> Any:
        self.task_starts.append((task_id, source_path, task_data))
        if self.error is not None:
            raise self.error
        return self.ws_meta

    def on_session_start(self, ws_root: str) -> None:
        self.session_starts.append(ws_root)
        if self.session_error is not None:
            raise self.session_error


def _plugin(mod: Any, manager: Any = None, *, config: dict[str, Any] | None = None) -> Any:
    p = mod.WorkspaceLifecyclePlugin(config=config or {})
    if manager is not None:
        p._manager = manager
    return p


def _ctx(mod: Any, state: dict[str, Any]) -> Any:
    return mod.PluginContext(state=state, config={})



def _skills_source(manager: Any) -> Path:
    """服务 skills 复制源（= base_path / "skills"）：暴露 base_path 决策的可观察面。

    真服务把 skills 快照从该目录复制进工作区（on_session_start →
    _copy_skills_to_workspace），是 base_path 唯一对外可观察的派生结果。
    """
    calls: list[Any] = []
    original = manager._task_tree  # noqa: SLF001 — 记录协作对象，不读私有状态
    return manager._base_path / "skills"


# ═══════════════ 会话工作区键 ═══════════════


class TestSessionWorkspaceKey:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ({"session_id": "sess-1", "pipeline_id": "pipe-9"}, "sess-1"),
            ({"pipeline_id": "pipe-9"}, "pipe-9"),
            ({}, "default"),
            ({"session_id": None, "pipeline_id": ""}, "default"),
            ({"session_id": "a/b c?d"}, "abcd"),          # 只留字母数字 -_
            ({"session_id": "a-b_c-9"}, "a-b_c-9"),
            ({"session_id": "../../etc"}, "etc"),          # 路径分隔符被清洗，不外溢
        ],
    )
    def test_key_derivation(self, plugin_mod: Any, state: dict[str, Any], expected: str) -> None:
        assert plugin_mod._session_workspace_key(state) == expected


# ═══════════════ state 聚合行刷新 ═══════════════


class TestRefreshStateRows:
    def test_no_reader_keeps_cache_untouched(self, plugin_mod: Any) -> None:
        """reader 未注入 → 直接返回，缓存保持原值（主会话/单体进程形态）。"""
        plugin_mod.set_state_reader(None)
        plugin_mod._state_rows_cache = [{"pipeline_id": "keep"}]

        asyncio.run(plugin_mod.refresh_state_rows())

        assert plugin_mod._state_rows_cache == [{"pipeline_id": "keep"}]
        plugin_mod._state_rows_cache = []

    def test_sync_reader_list_filters_non_dict(self, plugin_mod: Any) -> None:
        """同步 reader 返回 list → 只保留 dict 条目（下游按键读取零防御）。"""
        plugin_mod.set_state_reader(lambda: [{"pipeline_id": "a"}, "junk", None, 7, {"pipeline_id": "b"}])

        asyncio.run(plugin_mod.refresh_state_rows())

        assert plugin_mod._state_rows_cache == [{"pipeline_id": "a"}, {"pipeline_id": "b"}]
        plugin_mod.set_state_reader(None)
        plugin_mod._state_rows_cache = []

    def test_async_reader_awaited(self, plugin_mod: Any) -> None:
        """异步 reader（coroutine）→ await 后取结果（sidecar capability 形态）。"""
        async def _reader() -> list[Any]:
            return [{"pipeline_id": "async-1"}]

        plugin_mod.set_state_reader(_reader)

        asyncio.run(plugin_mod.refresh_state_rows())

        assert plugin_mod._state_rows_cache == [{"pipeline_id": "async-1"}]
        plugin_mod.set_state_reader(None)
        plugin_mod._state_rows_cache = []

    def test_non_list_result_keeps_previous_snapshot(self, plugin_mod: Any) -> None:
        """reader 返回非 list（None/dict）→ 保留旧快照（不清空可用数据）。"""
        plugin_mod._state_rows_cache = [{"pipeline_id": "old"}]

        plugin_mod.set_state_reader(lambda: None)
        asyncio.run(plugin_mod.refresh_state_rows())
        assert plugin_mod._state_rows_cache == [{"pipeline_id": "old"}]

        plugin_mod.set_state_reader(lambda: {"pipeline_id": "not-a-list"})
        asyncio.run(plugin_mod.refresh_state_rows())
        assert plugin_mod._state_rows_cache == [{"pipeline_id": "old"}]
        plugin_mod.set_state_reader(None)
        plugin_mod._state_rows_cache = []

    def test_reader_exception_keeps_previous_snapshot(
        self, plugin_mod: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """reader 抛异常 → 沿用旧快照 + warning 留痕（失败可排障）。"""
        plugin_mod._state_rows_cache = [{"pipeline_id": "survivor"}]

        def _boom() -> Any:
            raise RuntimeError("registry down")

        plugin_mod.set_state_reader(_boom)
        with caplog.at_level("WARNING"):
            asyncio.run(plugin_mod.refresh_state_rows())

        assert plugin_mod._state_rows_cache == [{"pipeline_id": "survivor"}]
        assert any("state 聚合行刷新失败" in r.message for r in caplog.records)
        plugin_mod.set_state_reader(None)
        plugin_mod._state_rows_cache = []


# ═══════════════ _ExecutionContextTaskTree ═══════════════


class TestExecutionContextTaskTree:
    @pytest.fixture(autouse=True)
    def _reset_cache(self, plugin_mod: Any) -> Any:
        plugin_mod._state_rows_cache = []
        yield
        plugin_mod._state_rows_cache = []

    def _tree(self, plugin_mod: Any, state: dict[str, Any]) -> Any:
        p = _plugin(plugin_mod)
        p._last_state = state
        return plugin_mod._ExecutionContextTaskTree(p)

    def test_row_missing_and_not_current_returns_none(self, plugin_mod: Any) -> None:
        """聚合行缺失且目标非当前任务 → None（父链查找据此判无祖先）。"""
        tree = self._tree(plugin_mod, {"task.id": "cur"})

        assert tree.get_task("other") is None

    def test_row_missing_current_task_uses_lineage_fallback(self, plugin_mod: Any) -> None:
        """当前任务行缺失 → 用本 state 的 lineage 扁平键兜底（不返回 None）。"""
        tree = self._tree(plugin_mod, {
            "task.id": "cur", "lineage.parent_pipeline_id": "parent-1",
        })

        task = tree.get_task("cur")

        assert task.id == "cur"
        assert task.parent_task_id == "parent-1"
        assert task.metadata == {}

    def test_row_missing_current_task_no_parent_yields_none_parent(self, plugin_mod: Any) -> None:
        """当前任务无父管道 → parent_task_id 为 None（根任务语义）。"""
        tree = self._tree(plugin_mod, {"task.id": "cur"})

        task = tree.get_task("cur")

        assert task.parent_task_id is None

    def test_row_hit_carries_parent_and_ws_meta(self, plugin_mod: Any) -> None:
        """聚合行命中 → 父链由行的 lineage 键解析，ws_meta 并入 metadata。"""
        plugin_mod._state_rows_cache = [{
            "pipeline_id": "t-child",
            "lineage.parent_pipeline_id": "t-parent",
            "ws_meta": {"mode": "shared", "path": "/ws/parent"},
        }]
        tree = self._tree(plugin_mod, {"task.id": "t-child"})

        task = tree.get_task("t-child")

        assert task.parent_task_id == "t-parent"
        assert task.metadata["ws_meta"] == {"mode": "shared", "path": "/ws/parent"}

    def test_row_ws_meta_json_string_restored(self, plugin_mod: Any) -> None:
        """跨边界序列化形态（JSON 字符串）→ 还原为 dict（state_fields 契约）。"""
        plugin_mod._state_rows_cache = [{
            "pipeline_id": "t-2",
            "ws_meta": '{"mode": "plain", "path": "/ws/p"}',
        }]
        tree = self._tree(plugin_mod, {"task.id": "t-2"})

        assert tree.get_task("t-2").metadata["ws_meta"] == {"mode": "plain", "path": "/ws/p"}

    def test_save_task_is_persistence_noop(self, plugin_mod: Any) -> None:
        """save_task 是持久化 no-op：原样返回入参（ws_meta 由 state 承载）。"""
        tree = self._tree(plugin_mod, {})
        sentinel = {"id": "x"}

        assert tree.save_task(sentinel) is sentinel


# ═══════════════ 服务实例化 ═══════════════


class TestGetManager:
    def test_cached_manager_reused(self, plugin_mod: Any) -> None:
        cached = _ScriptedManager()
        p = _plugin(plugin_mod, cached)

        assert p._get_manager() is cached

    def test_instantiation_failure_returns_none(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """隔离服务不可用 → None（任务管道显式报错；主会话跳过 skills 同步）。"""
        p = _plugin(plugin_mod)

        def _boom() -> None:
            raise RuntimeError("isolation tree unavailable")

        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", _boom)

        assert p._get_manager() is None

    def test_base_path_hint_wins_and_manager_cached(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """hint 优先于 config.base_path：会话启动时 skills 快照从 hint/skills
        复制进工作区；构造成功即缓存（二次取用同实例）。"""
        cfg_base = tmp_path / "cfg_base"
        (cfg_base / "skills" / "cfg_skill").mkdir(parents=True)
        hint = tmp_path / "hint_base"
        (hint / "skills" / "hint_skill").mkdir(parents=True)
        p = _plugin(plugin_mod, config={"base_path": str(cfg_base)})
        manager = p._get_manager(base_path_hint=str(hint))

        assert manager is not None
        session_ws = tmp_path / "session_ws"
        asyncio.run(_session_start(manager, session_ws))

        assert (session_ws / "skills" / "hint_skill").is_dir(), "hint 基目录生效"
        assert not (session_ws / "skills" / "cfg_skill").exists(), (
            "config.base_path 必须被 hint 覆盖"
        )
        assert p._get_manager(base_path_hint=str(tmp_path / "other")) is manager

    def test_config_base_path_used_without_hint(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """无 hint → config.base_path 生效（skills 快照源落在配置基目录）。"""
        cfg_base = tmp_path / "cfg_base"
        (cfg_base / "skills" / "cfg_skill").mkdir(parents=True)
        p = _plugin(plugin_mod, config={"base_path": str(cfg_base)})

        manager = p._get_manager()

        assert manager is not None
        session_ws = tmp_path / "session_ws2"
        asyncio.run(_session_start(manager, session_ws))

        assert (session_ws / "skills" / "cfg_skill").is_dir()

    def test_empty_hint_falls_back_to_project_root(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """空 hint 且无 config.base_path → find_project_root()（绝不回退 cwd）。"""
        from isolation.workspace import find_project_root

        p = _plugin(plugin_mod)

        manager = p._get_manager(base_path_hint=None)

        assert manager is not None
        # 可观察证据：仓库技能根（项目根/skills）存在时，快照落在会话工作区
        repo_skills = find_project_root() / ".zcode" / "skills"
        session_ws = tmp_path / "session_ws3"
        asyncio.run(_session_start(manager, session_ws))
        assert session_ws.is_dir(), "会话工作区必须被服务创建"
        if repo_skills.is_dir():
            assert (session_ws / "skills").is_dir(), "skills 源按项目根解析"


# ═══════════════ execute 分发 ═══════════════


class TestExecuteDispatch:
    @pytest.mark.parametrize("phase", ["main", "", "unknown"])
    async def test_non_lifecycle_phase_yields_no_output(
        self, plugin_mod: Any, phase: str,
    ) -> None:
        """main/未知循环体：生命周期插件不参与，零产出且不写 state。"""
        p = _plugin(plugin_mod, _ScriptedManager())

        result = await p.execute(_ctx(plugin_mod, {"current_phase": phase}))

        assert result.error is None
        assert not result.state_updates

    async def test_exit_phase_is_zero_op(self, plugin_mod: Any) -> None:
        """exit：零操作（合并归评估通过门控），worktree 路径原样不动。"""
        p = _plugin(plugin_mod, _ScriptedManager())
        manager = p._manager

        result = await p.execute(_ctx(plugin_mod, {
            "current_phase": "exit", "task.id": "t-1", "workspace": "/ws/t-1",
        }))

        assert not result.state_updates
        assert manager.task_starts == [], "exit 不得触发任何服务动作"

    async def test_execute_records_last_state_for_sync_readers(self, plugin_mod: Any) -> None:
        """execute 把本 state 记为 _last_state（_ExecutionContextTaskTree 的读源）。"""
        p = _plugin(plugin_mod, _ScriptedManager())
        state = {"current_phase": "main", "task.id": "rec"}

        await p.execute(_ctx(plugin_mod, state))

        assert p._last_state is state


# ═══════════════ bootstrap 幂等与重建 ═══════════════


class TestBootstrapIdempotence:
    async def test_existing_workspace_dir_skips_creation(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """state.workspace 指向真实目录 → 跳过创建（恢复/复用语义）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        manager = _ScriptedManager()
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {
            "task.id": "t-1", "workspace": str(ws),
            "execution_context": {"workspace": {"source_path": str(tmp_path), "mode": "plain"}},
        }))

        assert result.error is None
        assert not result.state_updates
        assert manager.task_starts == []

    async def test_ghost_workspace_rebuilt(
        self, plugin_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """state.workspace 指向已删目录 → 重建（禁止踩壳：续跑轮不落空壳）。"""
        manager = _ScriptedManager(ws_meta={"mode": "plain", "path": str(tmp_path / "fresh")})
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {
            "task.id": "t-1",
            "workspace": str(tmp_path / "ghost"),
            "execution_context": {
                "workspace": {"source_path": str(tmp_path), "mode": "plain"},
            },
        }))

        assert result.error is None
        assert result.state_updates["workspace"] == str(tmp_path / "fresh")
        assert len(manager.task_starts) == 1, "幽灵路径必须走真实创建段"

    async def test_no_execution_context_skips_creation(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """有任务身份但无 execution_context → 跳过创建（零产出，不报错）。"""
        manager = _ScriptedManager()
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {"task.id": "t-1"}))

        assert result.error is None
        assert not result.state_updates
        assert manager.task_starts == []

    async def test_execution_context_without_workspace_declaration_skips(
        self, plugin_mod: Any,
    ) -> None:
        """execution_context 无 workspace 声明（非 dict）→ 跳过创建。"""
        manager = _ScriptedManager()
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {
            "task.id": "t-1", "execution_context": {"workspace": "plain-string"},
        }))

        assert result.error is None
        assert not result.state_updates
        assert manager.task_starts == []

    async def test_main_session_explicit_source_resolves_plain(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """主会话（无 task.id）显式源路径 → plain 语义解析，不写 project_root。"""
        src = tmp_path / "proj"
        src.mkdir()
        p = _plugin(plugin_mod, _ScriptedManager())

        result = await p._bootstrap(_ctx(plugin_mod, {
            "execution_context": {"workspace": {"source_path": str(src), "mode": "worktree"}},
        }))

        assert result.state_updates["workspace"] == str(src)
        assert result.state_updates["ws_meta"] == {"mode": "worktree", "path": str(src)}
        assert "project_root" not in result.state_updates, (
            "project_root 语义 = 实际项目目录，主会话不得写会话/解析路径"
        )

    async def test_main_session_default_mode_is_plain(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """主会话显式源路径未声明 mode → 默认 plain（worktree 是显式选择）。"""
        src = tmp_path / "proj2"
        src.mkdir()
        p = _plugin(plugin_mod, _ScriptedManager())

        result = await p._bootstrap(_ctx(plugin_mod, {
            "execution_context": {"workspace": {"source_path": str(src)}},
        }))

        assert result.state_updates["ws_meta"]["mode"] == "plain"


# ═══════════════ 主会话工作区 ═══════════════


class TestSessionWorkspace:
    async def test_no_task_and_no_source_uses_session_dir(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """无任务身份、无显式工作区 → 工作区 = 工作空间根/sessions/{session_id}。"""
        base = tmp_path / "wsbase"
        base.mkdir()
        monkeypatch.setattr(
            plugin_mod, "_ensure_isolation_path", lambda: None, raising=False
        )
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        manager = _ScriptedManager()
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {"session_id": "sess-7"}))

        expected = str(base / "sessions" / "sess-7")
        assert result.state_updates["workspace"] == expected
        assert result.state_updates["ws_meta"] == {
            "mode": "plain", "path": expected, "session_id": "sess-7",
        }
        assert manager.session_starts == [expected], "skills 快照同步到会话工作区"

    async def test_skills_sync_failure_keeps_workspace(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """skills 同步失败 → 仅留痕，工作区解析结果仍生效（不因同步失败丢锚点）。"""
        base = tmp_path / "wsbase"
        base.mkdir()
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        manager = _ScriptedManager(session_error=RuntimeError("copy failed"))
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, {"session_id": "sess-8"}))

        assert result.error is None
        assert result.state_updates["workspace"] == str(base / "sessions" / "sess-8")

    async def test_workspace_root_resolution_failure_yields_no_output(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """工作空间根解析失败 → 零产出（本会话文件工具无锚点，fail-closed）。"""
        import isolation.workspace as iw

        def _boom() -> None:
            raise RuntimeError("no workspace base")

        monkeypatch.setattr(iw, "get_workspace_base_dir", _boom)
        p = _plugin(plugin_mod, _ScriptedManager())

        result = await p._bootstrap(_ctx(plugin_mod, {"session_id": "sess-9"}))

        assert result.error is None
        assert not result.state_updates

    async def test_manager_unavailable_still_resolves_workspace(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """服务不可用（manager None）→ 工作区仍解析（目录由根解析侧建）。"""
        base = tmp_path / "wsbase"
        base.mkdir()
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        p = _plugin(plugin_mod)  # 无 manager

        def _none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(p, "_get_manager", _none)
        result = await p._bootstrap(_ctx(plugin_mod, {"session_id": "sess-10"}))

        assert result.state_updates["workspace"] == str(base / "sessions" / "sess-10")


# ═══════════════ 任务工作区 ═══════════════


def _task_state(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "task.id": "task00000001",
        "execution_context": {
            "isolation": {"level": "isolated"},
            "workspace": {"source_path": str(tmp_path), "mode": "plain"},
        },
    }
    state.update(overrides)
    return state


class TestTaskWorkspaceFailClosed:
    async def test_default_source_resolution_failure_errors(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无显式源 + 默认根解析失败 → 显式报错（不落假工作空间）。"""
        p = _plugin(plugin_mod, _ScriptedManager())

        def _fail(task_id: str, mode: str) -> tuple[None, str]:
            return None, "no base"

        monkeypatch.setattr(p, "_resolve_default_source", _fail)
        state = _task_state(Path.cwd())
        state["execution_context"]["workspace"] = {"mode": "plain"}

        result = await p._bootstrap(_ctx(plugin_mod, state))

        assert result.error is not None
        assert "默认工作空间根解析失败" in str(result.error)

    async def test_manager_unavailable_errors_without_fallback(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """服务不可用 → 显式报错（无降级路径），不写 state.workspace。"""
        p = _plugin(plugin_mod)

        def _none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(p, "_get_manager", _none)
        result = await p._bootstrap(_ctx(plugin_mod, _task_state(tmp_path)))

        assert result.error is not None
        assert "无降级路径" in str(result.error)
        assert not result.state_updates

    async def test_creation_exception_errors_with_reason(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """服务创建抛异常 → 显式报错并带原因（插件错误面可见）。"""
        manager = _ScriptedManager(error=RuntimeError("git worktree failed"))
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, _task_state(tmp_path)))

        assert result.error is not None
        assert "git worktree failed" in str(result.error)
        assert not result.state_updates

    @pytest.mark.parametrize(
        "ws_meta",
        [
            None,
            "plain-string",
            {},
            {"mode": "plain"},                       # 有 mode 无 path
            {"path": ""},
        ],
    )
    async def test_invalid_ws_meta_errors(
        self, plugin_mod: Any, tmp_path: Path, ws_meta: Any,
    ) -> None:
        """服务返回非 dict / 无有效 path → 显式报错（不落空工作空间）。"""
        manager = _ScriptedManager(ws_meta=ws_meta)
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, _task_state(tmp_path)))

        assert result.error is not None
        assert "未返回有效路径" in str(result.error)

    async def test_valid_ws_meta_written_to_state(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """服务返回有效路径 → state.workspace / ws_meta 落盘（真契约形态）。"""
        meta = {"mode": "plain", "path": str(tmp_path / "ws"), "project_root": str(tmp_path)}
        manager = _ScriptedManager(ws_meta=meta)
        p = _plugin(plugin_mod, manager)

        result = await p._bootstrap(_ctx(plugin_mod, _task_state(tmp_path)))

        assert result.error is None
        assert result.state_updates["workspace"] == meta["path"]
        assert result.state_updates["ws_meta"] == meta


class TestResolveDefaultSource:
    @pytest.mark.parametrize(("mode", "expect_root"), [("worktree", "find"), ("plain", "base")])
    def test_source_depends_on_mode(
        self, plugin_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mode: str, expect_root: str,
    ) -> None:
        """worktree 拓扑源 = 项目根；plain 拓扑源 = 工作区根/{task_id}（默认隔离）。"""
        import isolation.workspace as iw

        proj = tmp_path / "proj"
        proj.mkdir()
        base = tmp_path / "wsbase"
        base.mkdir()
        monkeypatch.setattr(iw, "find_project_root", lambda: proj)
        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        p = _plugin(plugin_mod, _ScriptedManager())

        source, err = p._resolve_default_source("task-abcd", mode)

        assert err is None
        assert source == (str(proj) if expect_root == "find" else str(base / "task-abcd"))

    def test_resolution_failure_reports_reason(
        self, plugin_mod: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """隔离路径解析失败 → (None, 原因)（调用方据此显式报错）。"""
        payload: dict[str, Any] = {}

        def _boom() -> None:
            raise RuntimeError("probe gone")

        monkeypatch.setattr(plugin_mod, "_ensure_isolation_path", _boom, raising=False)
        p = _plugin(plugin_mod, _ScriptedManager())

        source, err = p._resolve_default_source("task-x", "plain")

        assert source is None
        assert err is not None and "probe gone" in err


# ═══════════════ task_data 装配 ═══════════════


class TestBuildTaskData:
    def _build(self, plugin_mod: Any, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        p = _plugin(plugin_mod, _ScriptedManager())
        return p._build_task_data(
            state,
            kwargs.pop("ec", state.get("execution_context") or {}),
            kwargs.pop("ws_spec", {"mode": "plain"}),
            kwargs.pop("mode", "plain"),
        )

    def test_root_task_flag_from_lineage(self, plugin_mod: Any) -> None:
        """is_root：无父管道 → True；有父管道 → False（服务按此分发 root/subtask）。"""
        assert self._build(plugin_mod, {})["is_root"] is True
        assert self._build(plugin_mod, {"lineage.parent_pipeline_id": "p1"})["is_root"] is False

    def test_isolation_level_and_mode_passthrough(self, plugin_mod: Any) -> None:
        """isolation level 与 workspace_mode 原样透传给服务（拓扑决策输入）。"""
        data = self._build(
            plugin_mod,
            {"execution_context": {"isolation": {"level": "isolated"}}},
            mode="worktree",
        )

        assert data["workspace_mode"] == "worktree"
        assert data["isolation_mode"] == "isolated"

    def test_missing_isolation_block_yields_empty_level(self, plugin_mod: Any) -> None:
        """execution_context 无 isolation → 空串（服务按默认隔离处理）。"""
        assert self._build(plugin_mod, {})["isolation_mode"] == ""

    @pytest.mark.parametrize(
        ("ws_spec", "expected"),
        [
            ({"explicit": True}, True),
            ({"explicit": False}, False),
            ({}, False),
            ({"explicit": "yes"}, True),   # 非空真值按显式处理
        ],
    )
    def test_explicit_flag_projected(self, plugin_mod: Any, ws_spec: dict[str, Any],
                                     expected: bool) -> None:
        """_has_explicit_workspace 忠实投影调用方显式声明（继承型不置位）。"""
        assert self._build(plugin_mod, {}, ws_spec=ws_spec)["_has_explicit_workspace"] is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"path": "/p/parent"}, {"path": "/p/parent"}),
            ('{"path": "/p/json"}', {"path": "/p/json"}),
            (None, {}),
            ("not-json", {}),
        ],
    )
    def test_inherited_ws_meta_uses_optional_dict(
        self, plugin_mod: Any, raw: Any, expected: dict[str, Any],
    ) -> None:
        """继承坐标经 optional_dict：原生 dict/JSON 字符串还原，缺形态降级空 dict。"""
        data = self._build(plugin_mod, {"lineage.parent_ws_meta": raw})

        assert data["_inherited_parent_ws_meta"] == expected
        assert data["_inherit_workspace_resolved"] is False


# ═══════════════ ws_meta 镜像 ═══════════════


class _ScriptedWriter:
    """task.* 写面替身：记录 (task_id, updates)，可脚本化异常。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, task_id: str, updates: dict[str, Any]) -> None:
        self.calls.append((task_id, updates))
        if self.error is not None:
            raise self.error


class TestMirrorTaskWsMeta:
    @pytest.fixture(autouse=True)
    def _reset_writer(self, plugin_mod: Any) -> Any:
        plugin_mod.set_task_state_writer(None)
        yield
        plugin_mod.set_task_state_writer(None)

    async def test_writer_unbound_skips_silently(self, plugin_mod: Any) -> None:
        """writer 未绑定 → 跳过镜像（镜像失败不阻断 init 主创建）。"""
        p = _plugin(plugin_mod, _ScriptedManager())
        await p._mirror_task_ws_meta("t-1", {"path": "/x"})  # 不应抛

    async def test_mirror_written_to_task_key_space(self, plugin_mod: Any) -> None:
        """镜像键 = task.ws_meta（运行中 task_evaluate 门控即时可见）。"""
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        p = _plugin(plugin_mod, _ScriptedManager())
        meta = {"mode": "plain", "path": "/x"}

        await p._mirror_task_ws_meta("t-1", meta)

        assert writer.calls == [("t-1", {"task.ws_meta": meta})]

    async def test_mirror_failure_logged_not_raised(
        self, plugin_mod: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """镜像写失败 → ERROR 留痕但不阻断（主创建已成功，门控将显式报错）。"""
        plugin_mod.set_task_state_writer(_ScriptedWriter(error=RuntimeError("state locked")))
        p = _plugin(plugin_mod, _ScriptedManager())

        with caplog.at_level("ERROR"):
            await p._mirror_task_ws_meta("t-1", {"path": "/x"})

        assert any("task.ws_meta 镜像写失败" in r.message for r in caplog.records)


class TestMirrorInheritedWsMeta:
    @pytest.fixture(autouse=True)
    def _reset_writer(self, plugin_mod: Any) -> Any:
        plugin_mod.set_task_state_writer(None)
        yield
        plugin_mod.set_task_state_writer(None)

    async def test_no_task_id_skips(self, plugin_mod: Any) -> None:
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        p = _plugin(plugin_mod, _ScriptedManager())

        await p._mirror_inherited_ws_meta({"workspace": "/ws"})

        assert writer.calls == []

    @pytest.mark.parametrize(
        "state",
        [
            {"task.id": "t-1", "task.ws_meta": {"path": "/already"}},
            {"task.id": "t-1", "ws_meta": {"path": "/already"}},
        ],
    )
    async def test_existing_ws_meta_skips(self, plugin_mod: Any, state: dict[str, Any]) -> None:
        """task.ws_meta / ws_meta 已有值 → 不重复补写（幂等）。"""
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        p = _plugin(plugin_mod, _ScriptedManager())
        state["lineage.parent_ws_meta"] = {"path": "/parent"}

        await p._mirror_inherited_ws_meta(state)

        assert writer.calls == []

    async def test_missing_parent_coordinates_no_fabrication(
        self, plugin_mod: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """出生契约无父链坐标 → 不虚构 plain（虚构会让真 worktree 任务跳过合并）。"""
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        p = _plugin(plugin_mod, _ScriptedManager())

        with caplog.at_level("WARNING"):
            await p._mirror_inherited_ws_meta({"task.id": "t-1", "workspace": "/ws/t-1"})

        assert writer.calls == []
        assert any("出生契约无父链坐标" in r.message for r in caplog.records)

    async def test_inherited_mirror_shape(self, plugin_mod: Any) -> None:
        """补写镜像与 _start_subtask 继承分支同形（mode=shared，不拥有合并）。"""
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        p = _plugin(plugin_mod, _ScriptedManager())

        await p._mirror_inherited_ws_meta({
            "task.id": "t-child",
            "workspace": "/ws/child",
            "lineage.parent_ws_meta": {
                "path": "/ws/parent", "project_root": "/proj",
            },
        })

        assert writer.calls == [("t-child", {"task.ws_meta": {
            "mode": "shared",
            "path": "/ws/parent",
            "parent_workspace": "/ws/child",
            "project_root": "/proj",
        }})]

    async def test_writer_unbound_warns(self, plugin_mod: Any,
                                       caplog: pytest.LogCaptureFixture) -> None:
        """有坐标但 writer 未绑定 → warning 留痕（门控读取失败可排障）。"""
        p = _plugin(plugin_mod, _ScriptedManager())

        with caplog.at_level("WARNING"):
            await p._mirror_inherited_ws_meta({
                "task.id": "t-1", "workspace": "/ws",
                "lineage.parent_ws_meta": {"path": "/parent"},
            })

        assert any("task_state_writer 未绑定" in r.message for r in caplog.records)

    async def test_mirror_write_failure_logged(
        self, plugin_mod: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """补写失败 → ERROR 留痕，不向上抛（不阻断 init 短路路径）。"""
        plugin_mod.set_task_state_writer(_ScriptedWriter(error=RuntimeError("locked")))
        p = _plugin(plugin_mod, _ScriptedManager())

        with caplog.at_level("ERROR"):
            await p._mirror_inherited_ws_meta({
                "task.id": "t-1", "workspace": "/ws",
                "lineage.parent_ws_meta": {"path": "/parent"},
            })

        assert any("task.ws_meta 镜像补写失败" in r.message for r in caplog.records)


# ═══════════════ 属性与契约 ═══════════════


class TestPluginProperties:
    def test_name_and_priority(self, plugin_mod: Any) -> None:
        assert _plugin(plugin_mod).name == "workspace_lifecycle"
        assert _plugin(plugin_mod).priority == 5
        assert _plugin(plugin_mod, config={"priority": 1}).priority == 1

    async def test_bootstrap_mirrors_inherited_meta_when_workspace_absent(
        self, plugin_mod: Any, tmp_path: Path,
    ) -> None:
        """无 state.workspace 时先走继承镜像补写，再进创建段。"""
        writer = _ScriptedWriter()
        plugin_mod.set_task_state_writer(writer)
        try:
            parent = tmp_path / "parent_ws"
            parent.mkdir()
            manager = _ScriptedManager(ws_meta={"mode": "plain", "path": str(tmp_path / "new")})
            p = _plugin(plugin_mod, manager)

            result = await p._bootstrap(_ctx(plugin_mod, {
                "task.id": "t-child",
                "execution_context": {
                    "workspace": {"source_path": str(tmp_path), "mode": "plain"},
                },
                "lineage.parent_ws_meta": {"path": str(parent)},
            }))
        finally:
            plugin_mod.set_task_state_writer(None)

        assert result.error is None
        # 继承镜像在创建段前补写（无 task.ws_meta 时），创建段再镜像新建坐标
        assert writer.calls[0] == ("t-child", {"task.ws_meta": {
            "mode": "shared",
            "path": str(parent),
            "parent_workspace": "",
            "project_root": "",
        }})
        assert writer.calls[-1][1]["task.ws_meta"]["path"] == str(tmp_path / "new")
