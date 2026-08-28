# @feature: FP-0.2.二 | @vision: V2 安全 | @ci: python-coverage
"""param_inject 的 task_id 注入测试（0.2 键名统一回归）。

背景：0.2 统一后（task = pipeline）任务身份在 state 的权威键是 `task.id`
（点号键，内核 chat_send_handler 创建管道时注入，值 = pipeline_id）。
param_inject 曾只读 0.1 下划线键 `task_id`，任务管道里 task_evaluate/
task_submit/task_manage 恒拿不到任务身份（「系统错误：task_id 未注入」）。

钉死语义：
1. state 有 `task.id` → 注入 task_id
2. 无任务身份（主会话）→ 不注入，args 不新增 task_id
3. LLM 夹带的伪造 task_id 被服务端权威值覆盖（先剥后注入同款安全边界）
"""

from __future__ import annotations

import asyncio

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit

_plugin_cls = None


def _get_plugin_cls():
    """延迟导入插件裸名模块（conftest 的裸名串扰治理在 setup 期生效）。"""
    global _plugin_cls
    if _plugin_cls is None:
        from plugin import ParamInjectPlugin

        _plugin_cls = ParamInjectPlugin
    return _plugin_cls


def _run(tool_calls: list[dict], state_extra: dict | None = None) -> list[dict]:
    state: dict = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
    }
    state.update(state_extra or {})
    ctx = PluginContext(state=state)
    result = asyncio.run(_get_plugin_cls()().execute(ctx))
    return result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])


def _args_of(calls: list[dict]) -> dict:
    assert calls, "RAW_TOOL_CALLS 未回写"
    return calls[0].get("args", calls[0].get("arguments", {}))


def test_task_id_injected_from_task_dot_id_key() -> None:
    """权威键 task.id 存在时注入 task_id（task_evaluate 等任务工具依赖）。"""
    calls = _run(
        [{"name": "task_evaluate", "args": {"action": "auto_complete"}}],
        {"task.id": "abc123", "pipeline_id": "abc123"},
    )
    args = _args_of(calls)
    assert args["task_id"] == "abc123"


def test_no_task_keys_leaves_args_untouched() -> None:
    """主会话（无任务身份）不注入，args 不新增 task_id。"""
    calls = _run([{"name": "task_evaluate", "args": {"action": "auto_complete"}}])
    args = _args_of(calls)
    assert "task_id" not in args


def test_explicit_task_id_preserved_for_task_manage() -> None:
    """LLM 显式传的 task_id 保留（task_manage 的目标任务是功能入参，非注入身份）。

    task_id 与 workspace/project_root 不同：task_manage 查询/操作任意任务是
    合法需求，无条件覆盖会废掉该功能——与 task_submit 的 workspace 例外同款。
    注入只在参数缺失时发生（session_id/user_id 同款语义）。
    """
    calls = _run(
        [{"name": "task_manage", "args": {"action": "get", "task_id": "target789"}}],
        {"task.id": "abc123"},
    )
    args = _args_of(calls)
    assert args["task_id"] == "target789"


def test_project_root_not_injected_when_state_lacks_it() -> None:
    """state 无 project_root → 不注入（仓库根不得隐式成为读写锚点）。

    主会话锚点由 workspace_lifecycle 写入 state（工作空间根）；缺锚点时
    文件工具 fail-closed 报错——比静默继承项目源码树安全。
    """
    calls = _run(
        [{"name": "file_read", "args": {"path": "skills/x/SKILL.md"}}],
        {"task.id": "", "pipeline_id": "p1"},
    )
    args = _args_of(calls)
    assert "project_root" not in args


def test_project_root_state_value_injected() -> None:
    """state 权威值（任务管道 = 工作空间 / 主会话 = 工作空间根）照常注入。"""
    calls = _run(
        [{"name": "file_read", "args": {"path": "doc.md"}}],
        {"project_root": "D:/ws/task1", "workspace": "D:/ws/task1"},
    )
    args = _args_of(calls)
    assert args.get("project_root") == "D:/ws/task1"

def test_project_root_state_value_wins_over_backfill() -> None:
    """state 权威值优先：任务管道 project_root=工作空间，不被仓库根覆盖。"""
    calls = _run(
        [{"name": "file_read", "args": {"path": "doc.md"}}],
        {"project_root": "D:/ws/task1", "workspace": "D:/ws/task1"},
    )
    args = _args_of(calls)
    assert args.get("project_root") == "D:/ws/task1"
