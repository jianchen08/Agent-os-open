# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace_lifecycle init 工作区决策贯通测试（task_data 计算 → 服务分支）。

锁 2026-09-03 用户裁定「子任务继承直接上级的工作空间」（收窄 2026-08-30
「挂靠即显式锚」至显式挂靠）：

- 继承挂靠（explicit=False）+ 出生契约父 worktree 坐标 → mode=shared 落父
  worktree。此前 ``task.parent_project_id`` 被并进显式坐标，子任务命中
  「显式 worktree：源目录建隔离副本」分支，在项目根上另开副本、父链坐标
  被带了却不用——父任务 worktree 内的成果对子任务不可见；
- 显式挂靠（explicit=True）→ 仍在项目文件夹上分叉自己的 worktree
  （2026-08-30 场景保真：主会话派的第一层挂项目任务不从会话目录）；
- 继承型子任务坐标缺失 → 显式报错不降级（拒绝静默回退 source_path 假工作
  空间裸奔）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[0]  # plugins/shared（state_fields 等平铺裸模块解析根）
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

_MOD_NAME = "workspace_lifecycle_plugin_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


class _FakeTree:
    """最小 task_tree：聚合行缺席（继承分支只消费出生契约坐标）。"""

    def get_task(self, task_id: str) -> None:
        return None

    def save_task(self, task: Any) -> Any:
        return task


def _make_plugin(mod: Any, tmp_path: Path, parent_wt: Path, proj_dir: Path) -> Any:
    """真服务（真 WorkspaceLifecycleManager）+ 出生契约父坐标注入 store。"""
    import isolation_path

    isolation_path.ensure_isolation_path()  # isolation/ 裸模块解析，与 plugin.py 懒加载同源
    service_mod = importlib.import_module("isolation.workspace_lifecycle")
    manager = service_mod.WorkspaceLifecycleManager(
        resource_merge=None,
        config={"workspace": {"root": str(tmp_path / "wsroot"), "default_mode": "worktree"}},
        task_tree=_FakeTree(),
        ws_meta_store={
            "parent000001": {
                "mode": "worktree",
                "path": str(parent_wt),
                "branch": "task/parent000001",
                "project_root": str(proj_dir),
            }
        },
        base_path=str(tmp_path),
    )
    plugin = mod.WorkspaceLifecyclePlugin(config={})
    plugin._get_manager = lambda *_args, **_kwargs: manager  # type: ignore[method-assign]
    return plugin


def _subtask_state(
    proj_dir: Path,
    *,
    explicit: bool,
    with_parent_ws_meta: bool = True,
    parent_wt: Path | None = None,
) -> dict[str, Any]:
    """继承挂靠子任务的出生 state（task_submit 装配形态：source_path=项目
    文件夹、explicit=调用方声明、lineage.parent_ws_meta=父 worktree 坐标）。"""
    state: dict[str, Any] = {
        "task.id": "sub0000000001",
        "pipeline_id": "sub0000000001",
        "lineage.parent_pipeline_id": "parent000001",
        "task.parent_project_id": "proj00000001",
        "execution_context": {
            "isolation": {"level": "isolated"},
            "workspace": {
                "source_path": str(proj_dir),
                "mode": "worktree",
                "explicit": explicit,
            },
        },
    }
    if with_parent_ws_meta:
        assert parent_wt is not None
        state["lineage.parent_ws_meta"] = {
            "mode": "worktree",
            "path": str(parent_wt),
            "branch": "task/parent000001",
            "project_root": str(proj_dir),
        }
    return state


def _git_init(repo: Path) -> None:
    """最小项目仓（CI runner 无全局 git 身份，repo 级补齐）。"""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Agent OS"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "agent@agent-os.local"], cwd=repo, check=True, capture_output=True, text=True
    )
    (repo / "README.md").write_text("proj", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)


class TestInheritedAttachSubtask:
    async def test_inherited_attach_shares_parent_worktree(self, tmp_path: Path) -> None:
        """回归：继承挂靠 + 出生契约父 worktree → 共享父 worktree（非项目根新副本）。"""
        mod = _load_module()
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        ctx = mod.PluginContext(state=_subtask_state(proj_dir, explicit=False, parent_wt=parent_wt), config={})
        result = await plugin._bootstrap(ctx)
        assert result.error is None, result.error
        ws_meta = result.state_updates["task.ws_meta"]
        assert ws_meta["mode"] == "shared"
        assert ws_meta["path"] == str(parent_wt)
        assert result.state_updates["workspace"] == str(parent_wt)
        # 子任务不拥有合并（mode=shared），父任务 exit 负责整个 worktree
        assert "branch" not in ws_meta

    async def test_explicit_attach_forks_own_worktree(self, tmp_path: Path) -> None:
        """保真：显式挂靠仍按声明拓扑在项目文件夹上分叉自己的 worktree。"""
        mod = _load_module()
        proj_dir = tmp_path / "proj"
        _git_init(proj_dir)
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        ctx = mod.PluginContext(state=_subtask_state(proj_dir, explicit=True, parent_wt=parent_wt), config={})
        result = await plugin._bootstrap(ctx)
        assert result.error is None, result.error
        ws_meta = result.state_updates["task.ws_meta"]
        assert ws_meta["mode"] == "worktree"
        assert ws_meta["project_root"] == str(proj_dir)
        assert ws_meta["branch"] == "task/sub0000000001"
        # 自己的隔离副本（配置工作区根下），既非父 worktree 也非项目根本身
        assert ws_meta["path"] != str(parent_wt)
        assert ws_meta["path"] != str(proj_dir)
        assert Path(ws_meta["path"]).is_dir()

    async def test_missing_coordinates_raises_without_fallback(self, tmp_path: Path) -> None:
        """无降级：继承型子任务坐标缺失 → 显式报错，不静默落 source_path 目录。"""
        mod = _load_module()
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        ctx = mod.PluginContext(
            state=_subtask_state(proj_dir, explicit=False, with_parent_ws_meta=False),
            config={},
        )
        result = await plugin._bootstrap(ctx)
        assert result.error is not None
        assert "父链工作空间解析失败" in str(result.error)
        assert not result.state_updates


def _make_root_plugin(mod: Any, tmp_path: Path) -> Any:
    """真服务（真 WorkspaceLifecycleManager）+ 空 store（根任务，无父链）。"""
    import isolation_path

    isolation_path.ensure_isolation_path()  # isolation/ 裸模块解析，与 plugin.py 懒加载同源
    service_mod = importlib.import_module("isolation.workspace_lifecycle")
    manager = service_mod.WorkspaceLifecycleManager(
        resource_merge=None,
        config={"workspace": {"root": str(tmp_path / "wsroot"), "default_mode": "plain"}},
        task_tree=_FakeTree(),
        ws_meta_store={},
        base_path=str(tmp_path),
    )
    plugin = mod.WorkspaceLifecyclePlugin(config={})
    plugin._get_manager = lambda *_args, **_kwargs: manager  # type: ignore[method-assign]
    return plugin


class TestBirthContractFallback:
    """出生契约回退（2026-10-15 装机哨兵任务实锤）。

    现场：task_submit root 派发的任务出生只写 task.ws_meta（plain 显式拓扑
    提交期写全），execution_context 未入管道 state——bootstrap 走「无
    execution_context 跳过」分支，state.workspace 永不落键，fs 工具无锚
    （「workspace/project_root 未注入，无法锚定路径」），任务每轮 failed。
    """

    async def test_missing_ec_falls_back_to_birth_ws_meta(self, tmp_path: Path) -> None:
        """无 execution_context + 出生坐标 → 照常创建工作空间，落 workspace/project_root。"""
        mod = _load_module()
        ws_dir = tmp_path / "sentinel_ws"
        plugin = _make_root_plugin(mod, tmp_path)
        state: dict[str, Any] = {
            "current_phase": "init",
            "task.id": "task000000001",
            "pipeline_id": "task000000001",
            "task.ws_meta": {
                "mode": "plain",
                "path": str(ws_dir),
                "project_root": str(ws_dir),
            },
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None, result.error
        updates = result.state_updates or {}
        assert updates.get("workspace") == str(ws_dir)
        # fs 工具锚单键：plain 显式拓扑 path=project_root=source_path
        assert updates.get("project_root") == str(ws_dir)
        assert updates.get("task.ws_meta", {}).get("path") == str(ws_dir)
        assert ws_dir.is_dir()

    async def test_missing_ec_without_birth_ws_meta_still_skips(self, tmp_path: Path) -> None:
        """无 execution_context 且无出生坐标 → 维持跳过（不虚构默认工作区）。"""
        mod = _load_module()
        plugin = _make_root_plugin(mod, tmp_path)
        state: dict[str, Any] = {
            "current_phase": "init",
            "task.id": "task000000001",
            "pipeline_id": "task000000001",
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        assert not result.state_updates

    async def test_missing_ec_pathless_birth_ws_meta_still_skips(self, tmp_path: Path) -> None:
        """出生坐标残缺（无 path）→ 视同无坐标，维持跳过。"""
        mod = _load_module()
        plugin = _make_root_plugin(mod, tmp_path)
        state: dict[str, Any] = {
            "current_phase": "init",
            "task.id": "task000000001",
            "pipeline_id": "task000000001",
            "task.ws_meta": {"mode": "plain"},
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        assert not result.state_updates

    async def test_with_execution_context_behavior_unchanged(self, tmp_path: Path) -> None:
        """向后兼容：有 execution_context 时走原路径，不写 project_root 单键。"""
        mod = _load_module()
        ws_dir = tmp_path / "explicit_ws"
        plugin = _make_root_plugin(mod, tmp_path)
        state: dict[str, Any] = {
            "current_phase": "init",
            "task.id": "task000000002",
            "pipeline_id": "task000000002",
            "execution_context": {
                "workspace": {"source_path": str(ws_dir), "mode": "plain", "explicit": True},
            },
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None, result.error
        updates = result.state_updates or {}
        assert updates.get("workspace") == str(ws_dir)
        assert "project_root" not in updates
        assert ws_dir.is_dir()


class TestSessionWsMetaSurvival:
    """主会话域坐标键跨 run 存活（2026-10-13 装机 P1 回归锚）。

    现场：主会话首轮 task_submit 子任务正常（出生契约带父链坐标），同会话
    第二轮起子任务工作空间创建 fail-closed（「父链工作空间解析失败」）。
    根因：ws_meta 只在 export_fields、未声明 persistent_fields——投影落表
    白名单缺键，run 终态注销热缓存后冷恢复（pipeline_state 表行）读不到，
    提交者 state 无坐标可注入。
    """

    @pytest.fixture(autouse=True)
    def _reset_rows_cache(self) -> Any:
        """聚合行快照是模块级缓存，用后复位防测试间污染。"""
        yield
        mod = _load_module()
        mod._state_rows_cache = []

    def test_manifest_declares_ws_meta_persistent(self) -> None:
        """manifest 契约：ws_meta 与 workspace 同列 persistent_fields（逐键投影落表）。"""
        import json

        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert "ws_meta" in manifest["persistent_fields"]
        assert "workspace" in manifest["persistent_fields"]
        assert "task.ws_meta" in manifest["persistent_fields"]

    async def test_skip_branch_selfheals_missing_session_ws_meta(self, tmp_path: Path) -> None:
        """幂等跳过分支自愈：workspace 在而 ws_meta 缺（冷恢复后的第二轮现场）→ 补写。"""
        mod = _load_module()
        ws_dir = tmp_path / "sessions" / "thread-x"
        ws_dir.mkdir(parents=True)
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        state: dict[str, Any] = {
            "current_phase": "init",
            "pipeline_id": "pipe1",
            "session_id": "thread-x",
            "workspace": str(ws_dir),
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        ws_meta = (result.state_updates or {}).get("ws_meta")
        assert ws_meta is not None, "缺坐标必须补写，否则本轮 task_submit 出生契约断链"
        assert ws_meta["path"] == str(ws_dir)
        assert ws_meta["mode"] == "plain"
        # 补写键 = 现行派生规则的短 id（thread-x 剥前缀连字符 → "x"）
        assert ws_meta["session_id"] == "x"

    async def test_skip_branch_no_op_when_ws_meta_already_present(self, tmp_path: Path) -> None:
        """坐标已在（有 path）→ 零产出：自愈只补缺，不覆盖运行时权威值。"""
        mod = _load_module()
        ws_dir = tmp_path / "sessions" / "thread-x"
        ws_dir.mkdir(parents=True)
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        state: dict[str, Any] = {
            "current_phase": "init",
            "pipeline_id": "pipe1",
            "session_id": "thread-x",
            "workspace": str(ws_dir),
            "ws_meta": {"mode": "plain", "path": str(ws_dir), "session_id": "thread-x"},
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        assert not (result.state_updates or {}).get("ws_meta")

    async def test_skip_branch_task_pipeline_keeps_domain_mutex(self, tmp_path: Path) -> None:
        """任务管道（有 task.id）跳过分支不写会话域键：两域互斥（task.ws_meta 归镜像补全管）。"""
        mod = _load_module()
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        state: dict[str, Any] = {
            "current_phase": "init",
            "task.id": "task000000001",
            "pipeline_id": "task000000001",
            "workspace": str(ws_dir),
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        assert not (result.state_updates or {}).get("ws_meta")

    async def test_skip_branch_heals_pathless_ws_meta(self, tmp_path: Path) -> None:
        """坐标残缺（无 path = 注入侧判无效的同一形态）→ 视同缺失补写。"""
        mod = _load_module()
        ws_dir = tmp_path / "sessions" / "thread-x"
        ws_dir.mkdir(parents=True)
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        state: dict[str, Any] = {
            "current_phase": "init",
            "pipeline_id": "pipe1",
            "session_id": "thread-x",
            "workspace": str(ws_dir),
            "ws_meta": {"mode": "plain"},
        }
        result = await plugin._bootstrap(mod.PluginContext(state=state, config={}))
        assert result.error is None
        ws_meta = (result.state_updates or {}).get("ws_meta")
        assert ws_meta is not None
        assert ws_meta["path"] == str(ws_dir)

    def test_aggregate_row_falls_back_to_session_ws_meta(self, tmp_path: Path) -> None:
        """父链聚合查找认主会话域键：父行只有 ws_meta（会话域）→ metadata.ws_meta 命中。"""
        mod = _load_module()
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        mod._state_rows_cache = [
            {
                "pipeline_id": "pipe1",
                "ws_meta": {"mode": "plain", "path": str(tmp_path / "session_ws")},
            }
        ]
        tree = mod._ExecutionContextTaskTree(plugin)
        task = tree.get_task("pipe1")
        assert task is not None
        assert task.metadata["ws_meta"]["path"] == str(tmp_path / "session_ws")

    def test_aggregate_row_task_domain_wins_when_both_present(self, tmp_path: Path) -> None:
        """两键并存（防御形态）→ 任务域 task.ws_meta 优先，不越域。"""
        mod = _load_module()
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        mod._state_rows_cache = [
            {
                "pipeline_id": "task000000001",
                "task.ws_meta": {"mode": "worktree", "path": str(tmp_path / "wt")},
                "ws_meta": {"mode": "plain", "path": str(tmp_path / "session_ws")},
            }
        ]
        tree = mod._ExecutionContextTaskTree(plugin)
        task = tree.get_task("task000000001")
        assert task is not None
        assert task.metadata["ws_meta"]["path"] == str(tmp_path / "wt")

    async def test_start_subtask_resolves_session_parent_via_aggregate(
        self, tmp_path: Path
    ) -> None:
        """装机回归复现（0413ad64c94b）：出生契约缺坐标 + 父链=主会话行（仅会话域
        ws_meta）→ 聚合查找命中共享会话工作空间，不再 fail-closed。"""
        mod = _load_module()
        session_ws = tmp_path / "sessions" / "thread-x"
        session_ws.mkdir(parents=True)
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        token = mod._current_pipeline_state.set(
            {
                "task.id": "sub0000000001",
                "pipeline_id": "sub0000000001",
                "lineage.parent_pipeline_id": "pipe1",
            }
        )
        mod._state_rows_cache = [
            {
                "pipeline_id": "pipe1",
                "ws_meta": {"mode": "plain", "path": str(session_ws)},
            }
        ]
        import isolation_path

        isolation_path.ensure_isolation_path()  # isolation/ 裸模块解析，与 plugin.py 懒加载同源
        service_mod = importlib.import_module("isolation.workspace_lifecycle")
        manager = service_mod.WorkspaceLifecycleManager(
            resource_merge=None,
            config={"workspace": {"root": str(tmp_path / "wsroot")}},
            task_tree=mod._ExecutionContextTaskTree(plugin),
            ws_meta_store={},
            base_path=str(tmp_path),
        )
        try:
            meta = manager.on_task_start(
                "sub0000000001",
                str(tmp_path / "default_root"),
                {"is_root": False, "workspace_mode": "plain"},
            )
        finally:
            mod._current_pipeline_state.reset(token)
        assert meta["mode"] == "shared"
        assert meta["path"] == str(session_ws)


class TestSessionWorkspaceCrossDrive:
    """主会话工作区与任务工作空间同盘（ADR 草稿
    2026-10-17-workspace-crossdrive-fallback 配套第 4 项）。

    会话目录 `{base}/sessions/{短id}` 与任务 worktree 同属工作空间坐标面，
    基目录必须经同一收口：否则跨盘布局下会话目录落用户盘、任务 worktree 落
    项目盘，两套坐标分盘。判定按路径形态（POSIX 无盘符形态恒同盘），故
    以下用例在任意平台以 Windows 形态路径断行为，不依赖本机盘符布局。
    """

    async def _resolve(self, mod: Any, monkeypatch: pytest.MonkeyPatch, base: Path, proj: Path) -> str:
        import isolation.workspace as iw

        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: base)
        plugin = mod.WorkspaceLifecyclePlugin(config={"base_path": str(proj)})
        # 服务置空：会话工作区路径解析不依赖服务（skills 同步可跳过），
        # 顺带避免在盘符根建目录——本组用例只断解析出的坐标。
        plugin._get_manager = lambda *_a, **_k: None  # type: ignore[method-assign]
        result = await plugin._bootstrap_session_workspace(
            mod.PluginContext(state={"session_id": "thread-x"}, config={}),
            {"session_id": "thread-x"},
        )
        assert result.error is None
        assert (result.state_updates or {}).get("ws_meta", {}).get("path") is not None
        return str((result.state_updates or {})["ws_meta"]["path"])

    async def test_cross_drive_base_falls_back_to_project_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """基目录在 C:、项目在 D: → 会话目录退化为「D: 盘符根/根名/sessions/短id」。"""
        mod = _load_module()
        path = await self._resolve(mod, monkeypatch, Path("C:/Users/t/ws"), Path("D:/proj"))
        assert path.replace("\\", "/").lower() == "d:/.agentos-workspaces/sessions/x"

    async def test_same_drive_keeps_configured_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同盘（含 POSIX 无盘符形态）→ 会话目录仍落配置基目录，不退化。"""
        mod = _load_module()
        path = await self._resolve(mod, monkeypatch, Path("/srv/ws"), Path("/srv/proj"))
        assert path.replace("\\", "/") == "/srv/ws/sessions/x"


class TestExitNoOpAndGhostRebuild:
    """锁 2026-09-04 用户裁定：合并唯一触发点=评估通过；run 退出零操作。"""

    async def test_exit_leaves_worktree_intact(self, tmp_path: Path) -> None:
        """回归（598b4ad4 实锤）：exit 阶段零操作——worktree/产物/分支保留。"""
        import shutil

        mod = _load_module()
        proj_dir = tmp_path / "proj"
        _git_init(proj_dir)
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        ctx = mod.PluginContext(
            state=_subtask_state(proj_dir, explicit=True, parent_wt=parent_wt), config={}
        )
        boot = await plugin._bootstrap(ctx)
        assert boot.error is None, boot.error
        ws_path = Path(boot.state_updates["workspace"])
        artifact = ws_path / "artifact.txt"
        artifact.write_text("work", encoding="utf-8")
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj_dir, capture_output=True, text=True
        ).stdout

        exit_ctx = mod.PluginContext(
            state={
                "current_phase": "exit",
                "task.id": "sub0000000001",
                "workspace": str(ws_path),
                "ws_meta": boot.state_updates["task.ws_meta"],
            },
            config={},
        )
        fin = await plugin._finalize(exit_ctx)
        assert fin.error is None
        assert fin.state_updates in (None, {}) or not fin.state_updates
        # worktree 完好：目录在、产物在、git 分支在、根仓库零新提交
        assert ws_path.is_dir()
        assert artifact.read_text(encoding="utf-8") == "work"
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj_dir, capture_output=True, text=True
        ).stdout
        assert head_after == head_before
        branches = subprocess.run(
            ["git", "branch", "--list", "task/sub0000000001"],
            cwd=proj_dir,
            capture_output=True,
            text=True,
        )
        assert "task/sub0000000001" in branches.stdout

    async def test_bootstrap_rebuilds_removed_worktree(self, tmp_path: Path) -> None:
        """踩壳防护：state.workspace 指向已删目录 → 重建，禁止静默复用死路径。"""
        import shutil

        mod = _load_module()
        proj_dir = tmp_path / "proj"
        _git_init(proj_dir)
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        ctx = mod.PluginContext(
            state=_subtask_state(proj_dir, explicit=True, parent_wt=parent_wt), config={}
        )
        boot = await plugin._bootstrap(ctx)
        assert boot.error is None
        ws_path = Path(boot.state_updates["workspace"])
        shutil.rmtree(ws_path)

        ghost_state = dict(ctx.state)
        ghost_state.update(
            {"workspace": str(ws_path), "ws_meta": boot.state_updates["task.ws_meta"]}
        )
        rebuilt = await plugin._bootstrap(
            mod.PluginContext(state=ghost_state, config={})
        )
        assert rebuilt.error is None, rebuilt.error
        new_ws = Path(rebuilt.state_updates["workspace"])
        assert new_ws.is_dir()


class TestBirthTimeWorkspacePrepare:
    """出生期同步准备（ADR-2026-10-17 四段式出生第三段）。

    提交期调用面 `prepare_task_workspace` 与管道 init 走同一段 `_bootstrap`：
    四形态（worktree / plain+显式 / plain 缺省 / 非 git 降级）决策零重复，
    坐标形态与 init 段完全同形。这里钉的是该入口的**外部可观察行为**：
    就绪则交出真实存在的坐标，未就绪则带 error 且无坐标。
    """

    async def test_explicit_worktree_prepared_at_birth_time(self, tmp_path: Path) -> None:
        """显式 worktree 声明：出生期即在真实 git 仓上分叉出隔离副本。"""
        mod = _load_module()
        proj_dir = tmp_path / "proj"
        _git_init(proj_dir)
        plugin = _make_root_plugin(mod, tmp_path)
        result = await plugin.prepare_task_workspace(
            pipeline_id="task000000002",
            state={"task.id": "task000000002"},
            execution_context={
                "workspace": {
                    "source_path": str(proj_dir),
                    "mode": "worktree",
                    "explicit": True,
                },
                "isolation": {"level": "isolated"},
            },
        )
        assert result.error is None, result.error
        updates = result.state_updates or {}
        ws_path = Path(updates["workspace"])
        assert ws_path.is_dir(), "出生期必须交出真实存在的目录（幻影坐标不得裸奔）"
        assert updates["task.ws_meta"]["mode"] == "worktree"
        assert updates["task.ws_meta"]["branch"] == "task/task000000002"

    async def test_plain_explicit_prepared_at_birth_time(self, tmp_path: Path) -> None:
        """plain 显式拓扑：出生期建目录并直落坐标（不建 worktree）。"""
        mod = _load_module()
        ws_dir = tmp_path / "plain_ws"
        plugin = _make_root_plugin(mod, tmp_path)
        result = await plugin.prepare_task_workspace(
            pipeline_id="task000000003",
            state={"task.id": "task000000003"},
            execution_context={
                "workspace": {
                    "source_path": str(ws_dir),
                    "mode": "plain",
                    "explicit": True,
                }
            },
        )
        assert result.error is None, result.error
        assert result.state_updates["workspace"] == str(ws_dir)
        assert ws_dir.is_dir()

    async def test_unresolvable_parent_chain_reports_error(self, tmp_path: Path) -> None:
        """未就绪面：继承型子任务坐标缺失 → 带 error 且零坐标（与上面两组构成
        有区分度的失败输入；调用方据此回传失败并保留任务记录）。"""
        mod = _load_module()
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        parent_wt = tmp_path / "parent_wt"
        parent_wt.mkdir()
        plugin = _make_plugin(mod, tmp_path, parent_wt, proj_dir)
        state = _subtask_state(proj_dir, explicit=False, with_parent_ws_meta=False)
        result = await plugin.prepare_task_workspace(
            pipeline_id="sub0000000001",
            state=state,
            execution_context=state["execution_context"],
        )
        assert result.error is not None
        assert "父链工作空间解析失败" in str(result.error)
        assert not result.state_updates

    async def test_repeat_prepare_is_idempotent(self, tmp_path: Path) -> None:
        """性质：同一任务重复准备交出同一坐标（幂等键 = task_id，管道 init 段
        复核时不重建——恢复/续跑面靠这条不变量）。"""
        mod = _load_module()
        ws_dir = tmp_path / "idem_ws"
        plugin = _make_root_plugin(mod, tmp_path)
        ec = {
            "workspace": {
                "source_path": str(ws_dir),
                "mode": "plain",
                "explicit": True,
            }
        }
        first = await plugin.prepare_task_workspace(
            pipeline_id="task000000004", state={"task.id": "task000000004"}, execution_context=ec
        )
        second = await plugin.prepare_task_workspace(
            pipeline_id="task000000004", state={"task.id": "task000000004"}, execution_context=ec
        )
        assert first.error is None
        assert second.error is None
        assert first.state_updates["workspace"] == second.state_updates["workspace"]


class TestModeScopedSkillSources:
    """技能按模式复制（2026-11-11）：只复制当前 mode 包的 skills/ + 仓根/用户根。

    钉的是「调用面传递」：mode 标签解析（execution_context.mode 优先、agent
    键回退）→ 模式包双根解析 → 作为 on_task_start 的 mode_packages 传参。
    """

    def _user_mode_pkg(self, tmp_path: Path, mode: str, skill: str) -> Path:
        pkg = tmp_path / "userroot" / "plugins" / "modes" / f"mode_{mode}"
        (pkg / "skills" / skill).mkdir(parents=True)
        (pkg / "skills" / skill / "SKILL.md").write_text(skill, encoding="utf-8")
        (pkg / "plugin.json").write_text("{}", encoding="utf-8")
        return pkg

    def test_active_mode_prefers_execution_context(self) -> None:
        mod = _load_module()
        state = {
            "execution_context": {"mode": "coding"},
            "agent.id": "mode_evolution/evolution",
        }
        assert mod._active_mode(state) == "coding"

    def test_active_mode_reads_tag_set_primary(self) -> None:
        """前端发送形态 = 单元素标签集；主标签取末元素（最新激活优先）。"""
        mod = _load_module()
        assert mod._active_mode({"execution_context": {"mode": ["coding"]}}) == "coding"
        assert mod._active_mode(
            {"execution_context": {"mode": ["evolution", "coding"]}}
        ) == "coding"

    def test_active_mode_falls_back_to_agent_key(self) -> None:
        mod = _load_module()
        assert mod._active_mode({"agent.id": "mode_research/research_agent"}) == "research"
        assert mod._active_mode({"agent.id": "code_writer"}) == ""

    def test_mode_package_dirs_resolves_user_mode(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        mod = _load_module()
        pkg = self._user_mode_pkg(tmp_path, "coding", "s_mode")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
        dirs = mod._mode_package_dirs({"execution_context": {"mode": "coding"}})
        assert dirs == [pkg]

    def test_mode_package_dirs_reads_tag_set(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        mod = _load_module()
        pkg = self._user_mode_pkg(tmp_path, "coding", "s_mode")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
        dirs = mod._mode_package_dirs({"execution_context": {"mode": ["coding"]}})
        assert dirs == [pkg]

    def test_mode_package_dirs_rejects_bad_mode_shape(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """形态闸：`../` 形态不得进入包目录拼装（无模式 → 全量回退）。"""
        mod = _load_module()
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
        assert mod._mode_package_dirs({"execution_context": {"mode": "../evil"}}) is None

    async def test_task_workspace_copies_only_active_mode_skills(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        mod = _load_module()
        (tmp_path / "skills" / "s_root").mkdir(parents=True)
        (tmp_path / "skills" / "s_root" / "SKILL.md").write_text("r", encoding="utf-8")
        self._user_mode_pkg(tmp_path, "coding", "s_coding")
        self._user_mode_pkg(tmp_path, "research", "s_research")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
        plugin = _make_root_plugin(mod, tmp_path)
        ws_dir = tmp_path / "plain_ws"
        result = await plugin.prepare_task_workspace(
            pipeline_id="task000000005",
            state={"task.id": "task000000005"},
            execution_context={
                "mode": "coding",
                "workspace": {
                    "source_path": str(ws_dir),
                    "mode": "plain",
                    "explicit": True,
                },
            },
        )
        assert result.error is None, result.error
        assert (ws_dir / "skills" / "s_root" / "SKILL.md").is_file()
        assert (ws_dir / "skills" / "s_coding" / "SKILL.md").is_file()
        assert not (ws_dir / "skills" / "s_research").exists()

    async def test_session_workspace_passes_mode_packages(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """主会话同一收窄面：execution_context.mode 解析后传给 on_session_start。"""
        import isolation.workspace as iw

        mod = _load_module()
        pkg = self._user_mode_pkg(tmp_path, "coding", "s_coding")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: tmp_path / "wsbase")
        calls: dict[str, Any] = {}

        class _Manager:
            def on_session_start(self, *args: Any, **kwargs: Any) -> None:
                calls["args"] = args
                calls["kwargs"] = kwargs

        plugin = mod.WorkspaceLifecyclePlugin(config={"base_path": str(tmp_path)})
        plugin._get_manager = lambda *_a, **_k: _Manager()  # type: ignore[method-assign]
        state = {"session_id": "thread-x", "execution_context": {"mode": "coding"}}
        result = await plugin._bootstrap_session_workspace(
            mod.PluginContext(state=state, config={}), state
        )
        assert result.error is None
        assert calls["kwargs"]["mode_packages"] == [pkg]
