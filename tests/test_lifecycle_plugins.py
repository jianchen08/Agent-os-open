# @feature: FP-0.2.一 插件协议（生命周期插件） | @ci: python-coverage
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
from typing import Any

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
    assert updates["ws_meta"]["mode"] == "plain"
    assert updates["ws_meta"]["path"] == "D:/proj/x"
    # project_root 语义 = 实际项目目录，工作区路径不再伪装写它（工作区由
    # workspace/ws_meta.path 独立承载，param_inject 工具锚点只认 workspace）
    assert "project_root" not in updates


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
async def test_workspace_init_main_session_gets_workspace_root(plugins, monkeypatch, tmp_path):
    """init：主会话（无 task.id、无显式工作区）工作区 = 配置的工作空间根。

    仓库根不得作为会话工作区（agent 读写面不得触及项目源码树）；skills
    快照由 manager.on_session_start 同步到工作区根，skills/... 相对路径
    在工作区内解析（与任务管道同一复制例程）。
    """
    import tests._isolation_path  # noqa: F401  （system/isolation 目录入 sys.path）
    import isolation.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: tmp_path)

    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "session_id": "thread-abc123"})
    )
    updates = result.state_updates
    expected = str(tmp_path / "sessions" / "thread-abc123")
    assert updates["workspace"] == expected
    assert updates["ws_meta"]["mode"] == "plain"
    assert updates["ws_meta"]["session_id"] == "thread-abc123"
    # 主会话不写 project_root（语义 = 实际项目目录，防会话目录伪装成项目目录）
    assert "project_root" not in updates


@pytest.mark.asyncio
async def test_workspace_init_main_session_key_fallback_and_sanitized(plugins, monkeypatch, tmp_path):
    """会话键：session_id 缺省回退 pipeline_id；不安全字符清洗；全空回退 default。"""
    import tests._isolation_path  # noqa: F401
    import isolation.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: tmp_path)

    r1 = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "pipeline_id": "pipe-xyz"})
    )
    assert r1.state_updates["workspace"] == str(tmp_path / "sessions" / "pipe-xyz")

    r2 = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "session_id": "../escape"})
    )
    assert r2.state_updates["workspace"] == str(tmp_path / "sessions" / "escape")

    r3 = await plugins["ws"].execute(plugins["ctx_factory"]({"current_phase": "init"}))
    assert r3.state_updates["workspace"] == str(tmp_path / "sessions" / "default")


async def test_workspace_init_main_session_syncs_skills_via_manager(
    plugins, monkeypatch, tmp_path,
) -> None:
    """init：manager 可用时 skills 同步到会话工作区根（复制源 = 项目根 skills/）。"""
    import shutil

    import tests._isolation_path  # noqa: F401
    import isolation.workspace as ws_mod
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager

    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: tmp_path)
    repo_skills = tmp_path / "_repo" / "skills"
    (repo_skills / "skill-demo").mkdir(parents=True)
    (repo_skills / "skill-demo" / "SKILL.md").write_text("demo", encoding="utf-8")
    manager = WorkspaceLifecycleManager(
        resource_merge=None,
        config={},
        task_tree=None,
        ws_meta_store={},
        base_path=str(tmp_path / "_repo"),
    )
    monkeypatch.setattr(
        type(plugins["ws"]), "_get_manager", lambda self, base_path_hint=None: manager
    )

    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "session_id": "thread-abc"})
    )
    ws_dir = tmp_path / "sessions" / "thread-abc"
    assert result.state_updates["workspace"] == str(ws_dir)
    # 删值实验对照：manager 同步把源技能带进会话工作区
    synced = ws_dir / "skills" / "skill-demo" / "SKILL.md"
    assert synced.read_text(encoding="utf-8") == "demo"
    # 增量幂等：改源后重跑不同步已存在技能（已有技能保持原样）
    (repo_skills / "skill-demo" / "SKILL.md").write_text("changed", encoding="utf-8")
    await plugins["ws"].execute(
        plugins["ctx_factory"](
            {"current_phase": "init", "session_id": "thread-abc", "workspace": str(ws_dir)}
        )
    )
    assert synced.read_text(encoding="utf-8") == "demo"
    assert shutil


async def test_workspace_init_main_session_degrades_without_manager(plugins, monkeypatch, tmp_path):
    """init：manager 不可用时降级为纯解析（工作区三件套仍写入，无 skills 同步）。"""
    import tests._isolation_path  # noqa: F401
    import isolation.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: tmp_path)
    monkeypatch.setattr(
        type(plugins["ws"]), "_get_manager", lambda self, base_path_hint=None: None
    )

    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "session_id": "thread-abc"})
    )
    updates = result.state_updates
    ws_dir = tmp_path / "sessions" / "thread-abc"
    assert updates["workspace"] == str(ws_dir)
    assert not (ws_dir / "skills").exists()


async def test_workspace_init_main_session_project_root_idempotent(plugins):
    """init：主会话 state 已有 workspace 时不重复解析（顶部幂等短路）。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "init", "workspace": "D:/already/set"})
    )
    assert result.state_updates == {}


async def test_workspace_init_task_without_ws_declaration_unchanged(plugins):
    """init：有 task.id 但无 workspace 声明 → 走既有默认工作区创建
    （workspace 三件套），不得回落成主会话工作区根。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task.id": "t1",
                "execution_context": {"workspace": {"explicit": False, "mode": "", "source_path": ""}},
            }
        )
    )
    updates = result.state_updates
    assert "workspace" in updates, "任务管道必须创建任务工作区"
    assert updates.get("workspace") != "" 


@pytest.mark.asyncio
async def test_workspace_init_task_mirrors_task_ws_meta(plugins, monkeypatch):
    """init：任务管道创建成功后经 task.* 写面镜像 task.ws_meta（运行中即时可见）。

    task_evaluate 合并门控等运行中读面依赖该镜像——state_updates 的 ws_meta 键
    随引擎回写快照有延迟，update 写直入注册表即时可见。
    """
    ws_mod = plugins["ws_mod"]
    ws = plugins["ws"]

    class _OkManager:
        def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
            return {"mode": "plain", "path": workspace, "task_id": task_id}

    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: _OkManager())
    writes: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(ws_mod, "_task_state_writer", lambda pid, fields: writes.append((pid, fields)))
    try:
        result = await ws.execute(
            plugins["ctx_factory"](
                {
                    "current_phase": "init",
                    "task.id": "task_mirror_1",
                    "execution_context": {
                        "workspace": {"source_path": "D:/proj/x", "mode": "plain"}
                    },
                }
            )
        )
    finally:
        ws_mod._task_state_writer = None
    assert result.error is None
    assert writes == [("task_mirror_1", {"task.ws_meta": {"mode": "plain", "path": "D:/proj/x", "task_id": "task_mirror_1"}})]
    assert result.state_updates["ws_meta"]["path"] == "D:/proj/x"


@pytest.mark.asyncio
async def test_workspace_init_task_mirror_failure_not_blocking(plugins, monkeypatch, caplog):
    """init：task.ws_meta 镜像写失败 → ERROR 留痕但不阻断 init（主创建已成功）。"""
    ws_mod = plugins["ws_mod"]
    ws = plugins["ws"]

    class _OkManager:
        def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
            return {"mode": "plain", "path": workspace, "task_id": task_id}

    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: _OkManager())

    def _boom(pid: str, fields: dict) -> None:
        raise RuntimeError("写面故障")

    monkeypatch.setattr(ws_mod, "_task_state_writer", _boom)
    try:
        with caplog.at_level("ERROR"):
            result = await ws.execute(
                plugins["ctx_factory"](
                    {
                        "current_phase": "init",
                        "task.id": "task_mirror_2",
                        "execution_context": {
                            "workspace": {"source_path": "D:/proj/x", "mode": "plain"}
                        },
                    }
                )
            )
    finally:
        ws_mod._task_state_writer = None
    assert result.error is None, "镜像失败不阻断已成功的主创建"
    assert result.state_updates["ws_meta"]["path"] == "D:/proj/x"
    assert "task.ws_meta 镜像写失败" in caplog.text


@pytest.mark.asyncio
async def test_workspace_init_task_service_down_errors_without_fallback(plugins, monkeypatch, tmp_path):
    """init（任务管道 + 服务不可用）→ 显式报错，无降级、不落占位目录。

    2026-08-28 去降级：原"服务不可用 → worktree 回退占位目录/plain 源路径"
    分支删除——假工作空间裸奔会让任务产出与合并门控全部失真，失败必须
    显式可见（PluginResult.error，引擎记入 _plugin_errors 可见面）。
    """
    import tests._isolation_path  # noqa: F401  （system/isolation 目录入 sys.path）
    import isolation.workspace as ws_mod

    ws = plugins["ws"]
    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: None)
    # 确定性覆盖基目录解析（worktree 默认源 = 项目根；plain 默认源 = 基目录/task_id）
    frozen_base = tmp_path / "ws_base"
    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", lambda: frozen_base)
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task.id": "task_degrade_1",
                "execution_context": {
                    "workspace": {"source_path": "", "mode": "worktree"}
                },
            }
        )
    )
    assert result.error is not None, "服务不可用必须显式报错，不得静默降级"
    assert "工作空间服务不可用" in str(result.error)
    assert result.state_updates == {}, "服务不可用不得落任何工作空间 state（无占位目录）"


@pytest.mark.asyncio
async def test_workspace_init_task_creation_exception_errors(plugins, monkeypatch):
    """init（任务管道 + on_task_start 异常）→ 显式报错，不再降级为源路径。"""
    ws = plugins["ws"]

    class _BoomManager:
        def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
            raise RuntimeError("git 崩了")

    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: _BoomManager())
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task.id": "task_boom_1",
                "execution_context": {
                    "workspace": {"source_path": "D:/proj/x", "mode": "plain"}
                },
            }
        )
    )
    assert result.error is not None, "创建异常必须显式报错"
    assert "工作空间创建失败" in str(result.error) and "git 崩了" in str(result.error)
    assert result.state_updates == {}


@pytest.mark.asyncio
async def test_workspace_init_task_invalid_ws_meta_errors(plugins, monkeypatch):
    """init（任务管道 + 服务返回无 path 的 ws_meta）→ 显式报错。"""
    ws = plugins["ws"]

    class _BadManager:
        def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
            return {"mode": "plain"}  # 缺 path = 无效工作空间

    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: _BadManager())
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task.id": "task_badmeta_1",
                "execution_context": {
                    "workspace": {"source_path": "D:/proj/x", "mode": "plain"}
                },
            }
        )
    )
    assert result.error is not None, "无效 ws_meta 必须显式报错"
    assert "未返回有效路径" in str(result.error)
    assert result.state_updates == {}


@pytest.mark.asyncio
async def test_workspace_init_task_default_root_resolution_failure_errors(plugins, monkeypatch):
    """init（任务管道 + 默认工作空间根解析失败）→ 显式报错，不再静默跳过创建。"""
    import tests._isolation_path  # noqa: F401
    import isolation.workspace as ws_mod

    ws = plugins["ws"]
    monkeypatch.setattr(ws, "_get_manager", lambda base_path_hint=None: None)

    def _boom() -> str:
        raise RuntimeError("配置读不到")

    monkeypatch.setattr(ws_mod, "get_workspace_base_dir", _boom)
    result = await ws.execute(
        plugins["ctx_factory"](
            {
                "current_phase": "init",
                "task.id": "task_rootfail_1",
                "execution_context": {
                    "workspace": {"source_path": "", "mode": "plain"}
                },
            }
        )
    )
    assert result.error is not None, "默认根解析失败必须显式报错"
    assert "默认工作空间根解析失败" in str(result.error)
    assert result.state_updates == {}


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
