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
        config={"workspace": {"default_mode": "worktree"}},
        task_tree=overrides.get("task_tree"),
        ws_meta_store={},
        base_path=str(Path.cwd()),
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
