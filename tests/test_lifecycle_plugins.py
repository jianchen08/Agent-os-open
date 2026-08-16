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
async def test_workspace_exit_finalizes(plugins):
    """exit：无任务/非 worktree 时 no-op（收尾仅对任务 worktree 生效）。"""
    result = await plugins["ws"].execute(
        plugins["ctx_factory"]({"current_phase": "exit", "workspace": "D:/proj/x"})
    )
    # 无 task_id + ws_meta.mode != worktree → 无产出（主会话/plain 空间不合并）
    assert result.state_updates == {}


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
