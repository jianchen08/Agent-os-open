# @feature: FP-0.2.一 插件协议（生命周期插件） | @ci: none-local（不在任何 CI 车道：python-coverage 的 BASE_TEST_PATHS 未收集本文件）
"""workspace_lifecycle / environment_lifecycle 插件测试。

验证（多循环体 init/exit 分发）：
1. workspace_lifecycle init：消费 execution_context.workspace → 写 state.workspace/
   project_root/ws_meta；幂等（已有 workspace 跳过）；无声明零产出
2. workspace_lifecycle exit：收尾占位（workspace_finalized）
3. environment_lifecycle init：消费 execution_context.isolation → 写 environment_basis
4. environment_lifecycle exit：释放占位（environment_released）
5. main 循环体：两插件均零产出

[来源: 任务提交参数解耦设计（生命周期插件）]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SHARED_DIR = Path(__file__).resolve().parent.parent / "plugins" / "shared"
_PIPELINE_DIR = _SHARED_DIR / "pipeline"
_WORKSPACE_DIR = _PIPELINE_DIR / "input" / "workspace_lifecycle"
_ENV_DIR = _PIPELINE_DIR / "input" / "environment_lifecycle"

if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))


def _load_plugin(dirpath: Path, modname: str):
    spec = importlib.util.spec_from_file_location(modname, dirpath / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plugins():
    from pipeline.plugin import PluginContext  # noqa: PLC0415

    ws_mod = _load_plugin(_WORKSPACE_DIR, "ws_lc_test_mod")
    env_mod = _load_plugin(_ENV_DIR, "env_lc_test_mod")
    return {
        "ws": ws_mod.WorkspaceLifecyclePlugin(),
        "ws_mod": ws_mod,
        "env": env_mod.EnvironmentLifecyclePlugin(),
        "ctx_factory": lambda state: PluginContext(state=state),
    }


@pytest.mark.asyncio
async def test_workspace_init_resolves_from_execution_context(plugins):
    """init：从 execution_context.workspace 解析工作空间写入 state。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "execution_context": {
                    "workspace": {"source_path": "D:/proj/x", "mode": "plain"}
                },
            }
        )
    )
    updates = result.state_updates
    assert updates["workspace"] == "D:/proj/x"
    assert updates["project_root"] == "D:/proj/x"
    assert updates["ws_meta"]["mode"] == "plain"
    assert updates["ws_meta"]["path"] == "D:/proj/x"


@pytest.mark.asyncio
async def test_workspace_init_idempotent(plugins):
    """init：state 已有 workspace 时跳过（恢复/复用幂等）。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "workspace": "already_set",
                "execution_context": {
                    "workspace": {"source_path": "D:/proj/x", "mode": "worktree"}
                },
            }
        )
    )
    assert result.state_updates == {}, "已有 workspace 不应重复解析"


@pytest.mark.asyncio
async def test_workspace_init_no_context_noop(plugins):
    """init：无 execution_context.workspace 声明时零产出。"""
    result = await plugins["ws"].execute(plugins["ctx_factory"]({"current_phase": "init"}))
    assert result.state_updates == {}


@pytest.mark.asyncio
async def test_workspace_init_no_explicit_ws_degrades_to_plain(plugins, monkeypatch, tmp_path):
    """init 降级（服务不可用）：无显式 workspace 时 ws_meta 不得标 worktree。

    对齐服务层矫正（_start_root_task：无显式 workspace → 强制 plain 目录）——
    服务不可用时没有 worktree 被创建，声明 worktree 会造成"没有 workspace
    却 worktree 模式"的虚假标记（exit 会据此尝试 merge）。

    2026-08-24 修正：source_path 已被解析为项目根（worktree 的源）——降级时
    workspace 必须回退「工作区根/{task_id}」（配置驱动基目录下），不得把
    项目根直接当 workspace（任务会在项目根上直接读写）。

    2026-08-24 收口：基目录解析统一走 get_workspace_base_dir()（配置驱动）——
    真身配置可把基目录配到项目外（本仓现配 D:/myproject），断言只依赖解析
    函数，测试内确定性覆盖其返回值。
    """
    import tests._isolation_path  # noqa: F401  （system/isolation 目录入 sys.path）
    import isolation.workspace as ws_mod

    ws = plugins["ws"]
    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: None)
    # 确定性覆盖基目录解析（真身配置可能把基目录配到项目外），断言只依赖解析函数
    frozen_base = tmp_path / "ws_base"
    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: frozen_base)
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task_id": "task_degrade_1",
                "execution_context": {
                    "workspace": {"source_path": "", "mode": "worktree"}
                },
            }
        )
    )
    updates = result.state_updates
    assert updates["ws_meta"]["mode"] == "plain", "无显式 workspace 不得标 worktree"
    ws_path = Path(updates["ws_meta"]["path"])
    assert ws_path == frozen_base / "task_degrade_1", (
        f"降级 workspace 应为配置基目录下占位目录，实际: {ws_path}"
    )
    assert not str(ws_path).endswith(str(Path(__file__).resolve().parent.parent)), (
        "不得把项目根直接当 workspace"
    )


@pytest.mark.asyncio
async def test_workspace_init_explicit_ws_keeps_mode_on_degrade(plugins, monkeypatch):
    """init 降级（服务不可用）：有显式 workspace 时保留声明 mode。"""
    ws = plugins["ws"]
    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: None)
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "execution_context": {
                    "workspace": {
                        "source_path": "D:/proj/x",
                        "mode": "worktree",
                        "explicit": True,
                    }
                },
            }
        )
    )
    updates = result.state_updates
    assert updates["ws_meta"]["mode"] == "worktree", "显式 worktree 应保留声明"


@pytest.mark.asyncio
async def test_workspace_exit_finalizes(plugins):
    """exit：无任务/非 worktree 时 no-op（收尾仅对任务 worktree 生效）。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "exit", "workspace": "D:/proj/x"})
    )
    # 无 task_id + ws_meta.mode != worktree → 无产出（主会话/plain 空间不合并）
    assert result.state_updates == {}


def test_workspace_degrade_path_no_parents_derivation(plugins):
    """降级路径源码不含 parents[N] 硬编码推导（2026-08-24 收口）。

    项目根必须由配置驱动的 find_project_root/get_workspace_base_dir 提供——
    parents[5] 式推导在插件目录深度变化（部署布局迁移）时静默错位。
    只允许 _ensure_isolation_path 的 parents[2]（定位 isolation 插件目录，
    非项目根推导）。
    """
    src = Path(plugins["ws_mod"].__file__).read_text(encoding="utf-8")
    # parents[5]（旧项目根推导）与 parents[4]+ 的 Path(...).resolve().parents 用法禁止
    assert "parents[5]" not in src, "降级路径不得用 parents[5] 推导项目根"
    assert "parents[4]" not in src
    assert "parents[3]" not in src
    # 项目根只允许经 find_project_root（祖先查找）获得
    assert "find_project_root" in src


@pytest.mark.asyncio
async def test_environment_init_resolves_basis(plugins):
    """init：从 execution_context.isolation 解析环境基线（含服务可达性探测）。"""
    result = await plugins["env"].execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "execution_context": {"isolation": {"level": "isolated"}},
            }
        )
    )
    basis = result.state_updates["environment_basis"]
    assert basis["level"] == "isolated"
    assert basis["resolved"] is True
    assert "service_ready" in basis


@pytest.mark.asyncio
async def test_environment_init_invalid_level_noop(plugins):
    """init：isolation level 非法/缺失时零产出。"""
    result = await plugins["env"].execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "execution_context": {"isolation": {"level": "bogus"}},
            }
        )
    )
    assert result.state_updates == {}


@pytest.mark.asyncio
async def test_environment_exit_releases(plugins):
    """exit：释放占位，写 environment_released。"""
    result = await plugins["env"].execute(
        plugins["ctx_factory"](
            {"current_phase": "exit", "environment_basis": {"level": "isolated"}}
        )
    )
    assert result.state_updates == {"environment_released": True}


@pytest.mark.asyncio
async def test_main_phase_noop(plugins):
    """main 循环体：两个生命周期插件均零产出（不参与 agent 循环）。"""
    ws_result = await plugins["ws"].execute(plugins["ctx_factory"]({"current_phase": "main"}))
    env_result = await plugins["env"].execute(plugins["ctx_factory"]({"current_phase": "main"}))
    assert ws_result.state_updates == {}
    assert env_result.state_updates == {}


# ── state 聚合读取器缓存（2026-08-20 F7：sync 消费端只读缓存，不在 sync 上下文调 async reader）──


@pytest.mark.asyncio
async def test_refresh_state_rows_populates_cache_for_sync_reader(plugins):
    """sync reader：refresh 直接调用并落缓存，_read_rows 读到。"""
    ws_mod = plugins["ws_mod"]
    ws_mod.set_state_reader(lambda: [{"pipeline_id": "p1", "task.scope": "container"}])
    try:
        await ws_mod.refresh_state_rows()
        tree = ws_mod._ExecutionContextTaskTree(plugins["ws"], None)
        rows = tree._read_rows()
        assert rows == [{"pipeline_id": "p1", "task.scope": "container"}]
    finally:
        ws_mod.set_state_reader(None)


@pytest.mark.asyncio
async def test_refresh_state_rows_awaits_async_reader_no_warning(plugins):
    """async reader：refresh await 后落缓存——sync 读路径不再产生永不 await 的协程。"""
    import asyncio as _asyncio  # noqa: PLC0415
    import warnings  # noqa: PLC0415

    ws_mod = plugins["ws_mod"]

    async def _reader() -> list[dict]:
        await _asyncio.sleep(0)
        return [{"pipeline_id": "p2", "lineage.parent_pipeline_id": "p0"}]

    ws_mod.set_state_reader(_reader)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # RuntimeWarning（coroutine never awaited）即失败
            await ws_mod.refresh_state_rows()
        tree = ws_mod._ExecutionContextTaskTree(plugins["ws"], None)
        assert tree._read_rows() == [{"pipeline_id": "p2", "lineage.parent_pipeline_id": "p0"}]
    finally:
        ws_mod.set_state_reader(None)


@pytest.mark.asyncio
async def test_read_rows_without_refresh_returns_empty(plugins):
    """未刷新（缓存空）→ sync 读路径安全返回空，且绝不调用 reader（零协程）。"""
    import warnings  # noqa: PLC0415

    ws_mod = plugins["ws_mod"]

    async def _never_called() -> list[dict]:
        raise AssertionError("sync 读路径不得调用 async reader")

    ws_mod._state_rows_cache = []  # 重置模块级缓存（隔离前序用例污染）
    ws_mod.set_state_reader(_never_called)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # 若产生协程必报 RuntimeWarning
            tree = ws_mod._ExecutionContextTaskTree(plugins["ws"], None)
            assert tree._read_rows() == []
    finally:
        ws_mod.set_state_reader(None)
