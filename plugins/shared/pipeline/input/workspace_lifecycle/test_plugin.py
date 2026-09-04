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
_SHARED_ROOT = _PLUGIN_DIR.parents[2]  # plugins/shared（state_fields / pipeline 包）
_ISOLATION_DIR = _SHARED_ROOT / "system" / "isolation"
for _p in (str(_SHARED_ROOT), str(_ISOLATION_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
    service_mod = importlib.import_module("workspace_lifecycle")
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
        ws_meta = result.state_updates["ws_meta"]
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
        ws_meta = result.state_updates["ws_meta"]
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
                "ws_meta": boot.state_updates["ws_meta"],
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
            {"workspace": str(ws_path), "ws_meta": boot.state_updates["ws_meta"]}
        )
        rebuilt = await plugin._bootstrap(
            mod.PluginContext(state=ghost_state, config={})
        )
        assert rebuilt.error is None, rebuilt.error
        new_ws = Path(rebuilt.state_updates["workspace"])
        assert new_ws.is_dir()
