# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""param_inject 注入行为测试（真实依赖：插件模块 + 真实 state dict）。

锁定注入契约（安全边界行为逐项钉死）：
- 非 tool_execute / 无工具调用 → 不注入（params_injected=False）；
- 上下文参数仅在缺位时注入（session_id/user_id/timestamp/pipeline_id/
  parent_agent_level/agent_config_id）；task_id 空值占位（None/""）不算已存在；
- LLM 夹带 `_` 前缀伪造键先剥后注入（服务端值权威）；
- workspace/isolation_level/project_root 服务端权威覆盖式注入，
  task_submit 例外跳过（隔离为 agent 显式选择项）；
- task_submit 专属 parent_ws_meta 覆盖式注入（str JSON 还原 / 残缺 → None）；
- arguments JSON 串解析；解析失败且修复不可用 → args 置空 dict（不阻塞）；
- default_params 按工具名补缺；{{project_root}} 模板串替换为实际项目根。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[2]  # plugins/shared（pipeline 包）
for _p in (str(_SHARED_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD_NAME = "param_inject_plugin_test"


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


mod = _load_module()
ParamInjectPlugin = mod.ParamInjectPlugin


def _make_plugin(config: dict | None = None) -> Any:
    return ParamInjectPlugin(config=config)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "memory", "args": {}}],
        "session_id": "s-1",
        "user_id": "u-1",
        "task.id": "t-1",
        "pipeline_id": "t-1",
        "agent_level": "L2",
        "agent_config_id": "cfg-9",
        "workspace": "/ws/root",
        "isolation_level": "isolated",
        "project_root": "/pr/root",
    }
    state.update(overrides)
    return state


def _ctx(state: dict[str, Any]) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=state)


def _calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["raw_tool_calls"]


async def _run(state: dict[str, Any], config: dict | None = None) -> dict[str, Any]:
    return await _make_plugin(config)._do_work(_ctx(state))


async def test_non_tool_execute_and_empty_calls_not_injected() -> None:
    """core_type=llm_call / 无工具调用 → 不注入。"""
    assert await _run(_state(core_type="llm_call")) == {"tool.params_injected": False}
    assert await _run(_state(raw_tool_calls=[])) == {"tool.params_injected": False}


async def test_context_params_injected_when_absent() -> None:
    """缺位的上下文参数被注入（session/user/timestamp/task/pipeline/level/config）。"""
    result = await _run(_state())
    (tc,) = _calls(result)
    args = tc["args"]
    assert args["session_id"] == "s-1"
    assert args["user_id"] == "u-1"
    assert "timestamp" in args
    assert args["task_id"] == "t-1"
    assert args["pipeline_id"] == "t-1"
    assert args["parent_agent_level"] == 2
    assert args["agent_config_id"] == "cfg-9"
    assert args["workspace"] == "/ws/root"
    assert args["isolation_level"] == "isolated"
    assert args["project_root"] == "/pr/root"
    assert result["tool.params_injected"] is True


async def test_existing_values_not_overwritten_but_empty_task_id_is() -> None:
    """已有值不覆盖；task_id 空值占位不算已存在（空串被权威值替换）。"""
    state = _state(
        raw_tool_calls=[
            {
                "name": "memory",
                "args": {
                    "session_id": "llm-chosen",
                    "user_id": "fake-user",
                    "task_id": "",
                },
            }
        ]
    )
    result = await _run(state)
    args = _calls(result)[0]["args"]
    assert args["session_id"] == "llm-chosen"  # 缺位才注入，已有值保留
    assert args["user_id"] == "fake-user"
    assert args["task_id"] == "t-1"  # 空值占位被权威值覆盖


async def test_forged_underscore_keys_stripped() -> None:
    """LLM 夹带 `_` 前缀伪造键先剥（防绕过危险命令黑名单）。"""
    state = _state(
        raw_tool_calls=[
            {"name": "bash_execute", "args": {"command": "ls", "_owner": "evil", "_container_id": "x"}}
        ]
    )
    result = await _run(state)
    args = _calls(result)[0]["args"]
    assert "_owner" not in args and "_container_id" not in args
    assert args["command"] == "ls"


async def test_string_arguments_parsed_and_invalid_repair_unavailable_falls_back_empty() -> None:
    """arguments JSON 串解析；非法 JSON 且修复不可用 → args 置空 dict 后续注入。"""
    state = _state(raw_tool_calls=[{"name": "memory", "arguments": '{"query": "x"}'}])
    args = _calls(await _run(state))[0]["args"]
    assert args["query"] == "x"  # JSON 串被解析为 dict 并照常注入上下文参数
    assert args["session_id"] == "s-1"
    assert "_args_truncated" not in args

    state_bad = _state(raw_tool_calls=[{"name": "memory", "arguments": "not-json{"}])
    args_bad = _calls(await _run(state_bad))[0]["args"]
    assert args_bad.get("session_id") == "s-1"  # 仍继续注入上下文参数
    assert "query" not in args_bad


async def test_task_submit_skips_workspace_ctx_and_gets_parent_ws_meta() -> None:
    """task_submit：workspace/isolation/project_root 不注入；parent_ws_meta 覆盖注入。"""
    ws_meta = '{"path": "/ws/parent", "mode": "shared"}'
    state = _state(
        raw_tool_calls=[
            {"name": "task_submit", "args": {"workspace": "/llm/forged", "parent_task_id": "p1"}}
        ],
        ws_meta=ws_meta,
    )
    args = _calls(await _run(state))[0]["args"]
    assert args["workspace"] == "/llm/forged"  # agent 显式选择项，不覆盖
    assert "isolation_level" not in args and "project_root" not in args
    assert args["parent_ws_meta"] == {"path": "/ws/parent", "mode": "shared"}


async def test_parent_ws_meta_invalid_shapes_become_none() -> None:
    """ws_meta 残缺（非 dict / 无 path）→ None 覆盖（父无工作空间语义）。"""
    state = _state(raw_tool_calls=[{"name": "task_submit", "args": {}}], ws_meta='{"mode": "shared"}')
    args = _calls(await _run(state))[0]["args"]
    assert args["parent_ws_meta"] is None

    state2 = _state(raw_tool_calls=[{"name": "task_submit", "args": {}}], ws_meta={"no_path": 1})
    args2 = _calls(await _run(state2))[0]["args"]
    assert args2["parent_ws_meta"] is None


async def test_default_params_fill_missing_only() -> None:
    """default_params 按工具名补缺，不覆盖已有值。"""
    config = {"default_params": {"memory": {"scope": "task", "session_id": "should-not-override"}}}
    state = _state(raw_tool_calls=[{"name": "memory", "args": {"scope": "global"}}])
    args = _calls(await _run(state, config))[0]["args"]
    assert args["scope"] == "global"
    assert args["session_id"] == "s-1"


async def test_project_root_template_replaced() -> None:
    """{{project_root}} 模板串替换为实际项目根（真实依赖本仓布局可解析）。"""
    state = _state(raw_tool_calls=[{"name": "memory", "args": {"path": "{{project_root}}/config"}}])
    args = _calls(await _run(state))[0]["args"]
    resolved = mod._resolve_project_root()
    if resolved is None:
        pytest.skip("本环境无法解析项目根（AGENTOS_CONFIG_ROOT 缺失且非仓库布局）")
    assert args["path"] == f"{resolved}/config"
