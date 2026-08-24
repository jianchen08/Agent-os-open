# @feature: FP-MIGR 工作空间拓扑解耦 | @ci: none-local
"""workspace_lifecycle 拓扑决策测试：workspace_mode 驱动（与隔离解耦）。

验证：
1. _start_root_task：workspace_mode=plain → 直接操作目录（mode=plain），不建 worktree
2. _start_subtask：workspace_mode=plain → 共享宿主目录（mode=shared）
3. init_container_workspace：容器空间恒复制（不依赖任何隔离字段）

[来源: 任务提交参数解耦设计（worktree 与隔离拆分）]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "plugins" / "shared" / "system"
_ISOLATION_DIR = _SYSTEM_DIR / "isolation"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    """模块级 sys.path 注入（teardown 恢复）。

    isolation 目录含 workspace.py（模块），system 目录含 workspace/（包）——
    对 `import workspace` 解析冲突。本测试懒加载 workspace_lifecycle 需要
    isolation 目录，用 fixture 管理并恢复，避免污染同进程其它测试
    （如 channel_api 的 routes_workspaces 依赖 system/workspace/ 包）。
    """
    added: list[str] = []
    for _p in (_SYSTEM_DIR, _ISOLATION_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)

@pytest.fixture(scope="module")
def manager_cls():
    """懒加载 WorkspaceLifecycleManager（isolation 目录经 fixture 注入 path）。"""
    from workspace_lifecycle import WorkspaceLifecycleManager

    return WorkspaceLifecycleManager


def make_manager(manager_cls, **overrides):
    """构造 WorkspaceLifecycleManager（轻量 stub：无 git 依赖路径）。"""
    manager = manager_cls(
        resource_merge=None,
        config=overrides.get("config", {"workspace": {"default_mode": "worktree"}}),
        task_tree=overrides.get("task_tree"),
        ws_meta_store={},
        base_path=str(overrides.get("base_path", Path.cwd())),
    )
    # 隔离 git 探测：_find_container_workspace 由测试 stub
    manager._find_container_workspace = lambda task_id: overrides.get("container_ws")
    return manager


def test_root_task_plain_mode_uses_directory_directly(manager_cls):
    """_start_root_task：workspace_mode=plain → 直接操作目录，无 worktree。"""
    manager = make_manager(manager_cls, container_ws=None)
    meta = manager._start_root_task(
        "task_plain",
        "/tmp/proj_x",
        {"workspace_mode": "plain"},
    )
    assert meta["mode"] == "plain"
    assert meta["path"] == "/tmp/proj_x"


def test_root_task_plain_mode_uses_container_ws_when_present(manager_cls):
    """plain 拓扑下优先使用容器工作空间（容器存在时）。"""
    manager = make_manager(manager_cls, container_ws="/tmp/container_abc")
    meta = manager._start_root_task(
        "task_plain2",
        "/tmp/proj_x",
        {"workspace_mode": "plain"},
    )
    assert meta["mode"] == "plain"
    assert meta["path"] == "/tmp/container_abc"


def test_root_task_default_mode_is_worktree(manager_cls):
    """缺省 workspace_mode → 走 worktree 分支（不进 plain 直接返回）。"""
    manager = make_manager(manager_cls, container_ws="/tmp/container_abc")
    # worktree 分支需要 git 仓库操作——用 stub 验证不会落入 plain 分支：
    # 若误入 plain 分支会直接返回 mode=plain，此处应抛错/走 worktree 流程。
    manager._find_container_workspace = lambda task_id: None
    manager._detect_scenario = lambda workspace, task_data: ("existing", "/tmp/proj_x")
    manager._git_init_and_initial_commit = lambda *a, **k: True
    manager._ensure_git_user = lambda *a, **k: None
    manager._run_git = lambda *a, **k: (0, "ok")
    manager._guard_root_branch = lambda *a, **k: False
    manager._calc_project_size = lambda *a, **k: 1
    manager._worktree_add_with_repair = lambda *a, **k: None
    manager._ensure_dir_and_git = lambda *a, **k: None
    meta = manager._start_root_task(
        "task_worktree",
        "/tmp/proj_x",
        {"_has_explicit_workspace": True},
    )
    assert meta["mode"] == "worktree", "默认拓扑应为 worktree"


def test_subtask_plain_mode_shares_host_dir(manager_cls):
    """_start_subtask：workspace_mode=plain → 共享宿主目录（mode=shared）。"""
    manager = make_manager(manager_cls, container_ws="/tmp/container_abc")
    meta = manager._start_subtask(
        "task_sub",
        "/tmp/whatever",
        {"workspace_mode": "plain"},
    )
    assert meta["mode"] == "shared"
    assert meta["path"] == "/tmp/container_abc"


def test_init_container_workspace_always_copies(manager_cls):
    """容器空间恒复制：无隔离字段也走复制路径（不依赖 isolation_mode 分支）。"""
    manager = make_manager(manager_cls, container_ws=None)
    manager._get_workspace_root = lambda: Path("/tmp/ws_root")
    manager._copy_project_to_container = lambda path, src: 0
    manager._git_init_and_initial_commit = lambda *a, **k: True
    manager._ensure_dir_and_git = lambda *a, **k: None

    meta = manager.init_container_workspace(
        "container_1",
        "/tmp/src_proj",
        {},  # 无任何隔离字段 → 不应影响恒复制语义
    )
    assert meta["mode"] == "project_root"
    assert Path(meta["path"]) == Path("/tmp/ws_root") / "container_container_1"


# ── 2026-08-24 裁定：模式未指定 → 默认 worktree；无显式 workspace 时源=项目根 ──

def _stub_worktree_manager(manager_cls, base_path: Path):
    """构造 manager 并 stub worktree 路径上全部 git 依赖（不真建 worktree）。

    显式 workspace 时 _run_git 只被 _ensure_git_user 调用（rc=0 无副作用）；
    无显式 workspace 时还会被 worktree 前置守卫（rev-parse 探测）调用——
    守卫要求 `--is-inside-work-tree` 返回 "true" 且 rev-parse HEAD rc=0。
    """
    manager = make_manager(manager_cls, container_ws=None, base_path=base_path)
    manager._detect_scenario = lambda workspace, task_data: ("existing", str(workspace))
    manager._ensure_git_user = lambda *a, **k: None
    manager._run_git = lambda *a, **k: (
        0,
        "true" if "--is-inside-work-tree" in a else "ok",
        "",
    )
    manager._guard_root_branch = lambda *a, **k: False
    manager._calc_project_size = lambda *a, **k: 1
    manager._git_init_and_initial_commit = lambda *a, **k: True
    manager._worktree_add_with_repair = lambda *a, **k: None
    manager._get_workspace_root = lambda: base_path / ".ai_workspaces"
    return manager


def test_root_task_no_explicit_ws_default_mode_builds_worktree(manager_cls, tmp_path):
    """2026-08-24 裁定：未指定 workspace_mode（无显式 workspace）→ 项目根上建 worktree。

    source_path 由 plugin._bootstrap 解析为项目根；_start_root_task 必须走
    worktree 建立分支（mode=worktree，path 落在 ws_base 下含 __wt_ 的目录），
    而不是旧行为 plain 空目录。
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    manager = _stub_worktree_manager(manager_cls, project_root)
    # 显式断言 worktree 建立被触发（不建真实 worktree，仅验证调用）
    worktree_called = []

    def _fake_worktree_add(repo_path, branch, ws_dir, task_id):
        worktree_called.append((str(repo_path), branch, str(ws_dir)))

    manager._worktree_add_with_repair = _fake_worktree_add
    meta = manager._start_root_task(
        "task_no_mode",
        str(project_root),
        {},  # 无 workspace_mode / 无 _has_explicit_workspace
    )
    assert meta["mode"] == "worktree", "未指定模式应默认 worktree"
    assert meta["project_root"] == str(project_root), "worktree 源应为项目根"
    assert "__wt_" in meta["path"], f"worktree 应建于工作区根下的 __wt_ 目录: {meta['path']}"
    assert worktree_called and worktree_called[0][0] == str(project_root)


def test_root_task_explicit_plain_no_ws_no_git_ops(manager_cls, tmp_path):
    """2026-08-24 裁定：显式 plain（非 worktree）→ 不建 worktree，只建空目录。

    plugin._bootstrap 对 plain 模式已把 workspace 解析为「工作区根/{task_id}」
    占位目录（`_root/.ai_workspaces/task_id`）；_start_root_task 直接使用该
    目录，不做任何 git 操作。
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    ws_dir = project_root / ".ai_workspaces" / "task_plain_no_ws"
    manager = make_manager(manager_cls, container_ws=None, base_path=project_root)
    manager._get_workspace_root = lambda: project_root / ".ai_workspaces"
    git_ops: list[tuple] = []
    manager._run_git = lambda *a, **k: git_ops.append(a) or (0, "ok")

    meta = manager._start_root_task(
        "task_plain_no_ws",
        str(ws_dir),  # plugin 对 plain 已解析为占位目录
        {"workspace_mode": "plain"},
    )
    assert meta["mode"] in ("plain", "shared"), "显式 plain 不得标 worktree"
    assert meta["path"] == str(ws_dir), "直接操作目标目录，不改写位置"
    assert not git_ops, f"显式 plain 不应触发任何 git 操作: {git_ops}"


def test_root_task_no_explicit_ws_non_git_root_degrades_to_plain(manager_cls, tmp_path):
    """2026-08-24 降级：项目根非 git 仓库 → warn 降级 plain 空目录（不污染项目根）。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    manager = _stub_worktree_manager(manager_cls, project_root)
    # 前置守卫探测：is-inside-work-tree / rev-parse HEAD 均失败 → 降级
    manager._run_git = lambda *_a, **_k: (128, "", "not a git repository")
    meta = manager._start_root_task(
        "task_non_git_root",
        str(project_root),
        {"workspace_mode": "worktree"},
    )
    assert meta["mode"] == "plain"
    assert meta["path"] == str(project_root / ".ai_workspaces" / "task_non_git_root")
    assert not (project_root / "task_non_git_root").exists(), "不得在项目根下建目录"


# ── 2026-08-24 收口：_get_workspace_root 统一走配置驱动解析 ──

def test_get_workspace_root_delegates_to_unified_resolver(manager_cls, monkeypatch, tmp_path):
    """未注入 root 时 _get_workspace_root 委托统一解析函数（配置驱动，不硬编码）。"""
    import tests._isolation_path  # noqa: F401  （system/isolation 目录入 sys.path）
    import isolation.workspace as ws_mod

    # 无注入配置 → 走统一 get_workspace_base_dir；monkeypatch 其返回值断言被消费
    manager = make_manager(manager_cls, config={}, base_path=str(tmp_path))
    frozen = tmp_path / "frozen_base"
    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: str(frozen))
    assert manager._get_workspace_root() == frozen.resolve()


def test_get_workspace_root_injected_config_wins(manager_cls, tmp_path):
    """显式注入 self._config.workspace.root 优先（内核 plugin.get_config 注入链）。"""
    base = tmp_path / "project"
    manager = make_manager(
        manager_cls,
        config={"workspace": {"root": "my_ws"}},
        base_path=str(base),
    )
    assert manager._get_workspace_root() == (base / "my_ws").resolve()

    manager_abs = make_manager(
        manager_cls,
        config={"workspace": {"root": "D:/injected/abs"}},
        base_path=str(base),
    )
    assert manager_abs._get_workspace_root() == Path("D:/injected/abs").resolve()
