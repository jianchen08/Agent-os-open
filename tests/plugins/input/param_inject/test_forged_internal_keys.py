# @feature: FP-0.2.二 | @vision: V2 安全 | @ci: python-coverage
"""param_inject 服务端参数权威化测试（安全审查 2026-08-19 系统性根因修复）。

背景：LLM 生成的 tool_calls args 可夹带内部键伪造服务端注入值——
- `_isolation_provider`/`_container_id` 曾让 bash 工具跳过危险命令黑名单；
- `workspace`/`project_root` 旧逻辑"参数不存在才注入"，夹带值直达
  fs_tools 等工具的路径校验锚点。

钉死语义：
1. `_` 前缀内部键一律剥离（先剥后注入；isolation_guard 在本插件之后注入
   `_container_id`，不受影响）
2. workspace/isolation_level/project_root 服务端值无条件覆盖（task_submit 例外）
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


def _make_ctx(tool_calls: list[dict], state_extra: dict | None = None) -> PluginContext:
    state: dict = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
    }
    state.update(state_extra or {})
    return PluginContext(state=state)


def _run(tool_calls: list[dict], state_extra: dict | None = None) -> list[dict]:
    result = asyncio.run(_get_plugin_cls()().execute(_make_ctx(tool_calls, state_extra)))
    return result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])


def _args_of(calls: list[dict]) -> dict:
    assert calls, "RAW_TOOL_CALLS 未回写"
    return calls[0].get("args", calls[0].get("arguments", {}))


def test_forged_underscore_keys_stripped() -> None:
    """LLM 夹带的 _ 前缀内部键（隔离标记等）必须被剥离。"""
    calls = _run(
        [
            {
                "name": "bash_execute",
                "args": {
                    "command": "ls",
                    "_isolation_provider": "docker",  # 伪造内部键不得影响危险命令黑名单判定
                    "_container_id": "forged-container",
                    "_owner": "forged-owner",
                },
            }
        ],
        {"workspace": "D:/ws/task1"},
    )
    args = _args_of(calls)
    assert "_isolation_provider" not in args
    assert "_container_id" not in args
    assert "_owner" not in args
    assert args["command"] == "ls"  # 正常参数不受影响


def test_workspace_overwrites_forged_value() -> None:
    """夹带的 workspace 被服务端 state 值覆盖（旧逻辑"不存在才注入"会放行伪造值）。"""
    calls = _run(
        [{"name": "file_write", "args": {"path": "a.txt", "workspace": "C:/"}}],
        {"workspace": "D:/ws/task1", "project_root": "D:/agentos"},
    )
    args = _args_of(calls)
    assert args["workspace"] == "D:/ws/task1"
    assert args["project_root"] == "D:/agentos"


def test_workspace_injected_when_absent() -> None:
    """无夹带时照常注入（原行为回归）。"""
    calls = _run(
        [{"name": "file_write", "args": {"path": "a.txt"}}],
        {"workspace": "D:/ws/task1", "isolation_level": "docker"},
    )
    args = _args_of(calls)
    assert args["workspace"] == "D:/ws/task1"
    assert args["isolation_level"] == "docker"


def test_task_submit_explicit_choice_not_overwritten() -> None:
    """task_submit 的 workspace/isolation_level 是 agent 显式选择项，不注入不覆盖。"""
    calls = _run(
        [{"name": "task_submit", "args": {"goal": "g", "workspace": "D:/chosen"}}],
        {"workspace": "D:/ws/parent", "isolation_level": "docker"},
    )
    args = _args_of(calls)
    assert args["workspace"] == "D:/chosen"
    assert "isolation_level" not in args


def test_no_state_value_leaves_args_untouched() -> None:
    """state 无 workspace/project_root 时不新增键（锚点缺失由工具侧
    fail-closed 报错，仓库根不得隐式继承为 agent 读写锚点）。"""
    calls = _run([{"name": "file_read", "args": {"path": "a.txt"}}])
    args = _args_of(calls)
    assert "workspace" not in args
    assert "project_root" not in args



def test_task_submit_parent_ws_meta_overrides_forged_value() -> None:
    """task_submit 注入父管道 ws_meta 权威值；LLM 夹带值被无条件覆盖。

    子任务出生契约经 lineage.parent_ws_meta 携带父链工作空间坐标，
    workspace_lifecycle 共享决策不再依赖发起瞬间的聚合读可见性。
    """
    calls = _run(
        [
            {
                "name": "task_submit",
                "args": {"goal": "g", "parent_ws_meta": {"path": "C:/forged"}},
            }
        ],
        {"workspace": "D:/ws/parent", "ws_meta": {"mode": "plain", "path": "D:/ws/parent"}},
    )
    args = _args_of(calls)
    assert args["parent_ws_meta"] == {"mode": "plain", "path": "D:/ws/parent"}


def test_task_submit_parent_ws_meta_none_when_state_absent() -> None:
    """父无 ws_meta（工作空间未解析）→ 注入 None 覆盖，伪造坐标不放行。"""
    calls = _run(
        [
            {
                "name": "task_submit",
                "args": {"goal": "g", "parent_ws_meta": {"path": "C:/forged"}},
            }
        ],
        {"workspace": "D:/ws/parent"},
    )
    args = _args_of(calls)
    assert args["parent_ws_meta"] is None


def test_task_submit_parent_ws_meta_json_string_state_restored() -> None:
    """state ws_meta 为 JSON 字符串（跨边界序列化形态）→ 还原后注入。"""
    calls = _run(
        [{"name": "task_submit", "args": {"goal": "g"}}],
        {"ws_meta": '{"mode": "plain", "path": "D:/ws/parent"}'},
    )
    args = _args_of(calls)
    assert args["parent_ws_meta"] == {"mode": "plain", "path": "D:/ws/parent"}


def test_parent_ws_meta_only_for_task_submit() -> None:
    """parent_ws_meta 仅 task_submit 注入，其他工具参数面不受影响。"""
    calls = _run(
        [{"name": "file_write", "args": {"path": "a.txt"}}],
        {"ws_meta": {"mode": "plain", "path": "D:/ws/parent"}},
    )
    args = _args_of(calls)
    assert "parent_ws_meta" not in args


def test_task_submit_parent_ws_meta_invalid_json_string_treated_absent() -> None:
    """state ws_meta 为非法 JSON 串（形态漂移）→ 按缺失处理注入 None。"""
    calls = _run(
        [{"name": "task_submit", "args": {"goal": "g"}}],
        {"ws_meta": "{not-valid-json"},
    )
    args = _args_of(calls)
    assert args["parent_ws_meta"] is None
