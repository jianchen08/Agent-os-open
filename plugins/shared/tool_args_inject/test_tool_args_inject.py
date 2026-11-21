# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""tool_args_inject 注入行为测试（真实依赖：插件模块 + 真实 state dict）。

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
_SHARED_ROOT = _PLUGIN_DIR.parents[0]  # plugins/shared（pipeline 包）
for _p in (str(_SHARED_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD_NAME = "tool_args_inject_plugin_test"


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
ToolArgsInjectPlugin = mod.ToolArgsInjectPlugin


def _make_plugin(config: dict | None = None) -> Any:
    return ToolArgsInjectPlugin(config=config)


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
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=state)


def _calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["raw_tool_calls"]


async def _run(state: dict[str, Any], config: dict | None = None) -> dict[str, Any]:
    return await _make_plugin(config)._do_work(_ctx(state))


async def test_non_tool_execute_and_empty_calls_not_injected() -> None:
    """无工具调用（纯文本轮）→ 不注入；core_type 已退役，不再是门
    （有调用即注入，与 core_type 取值无关）。"""
    assert await _run(_state(raw_tool_calls=[])) == {"tool.params_injected": False}
    result = await _run(_state(core_type="llm_call"))
    assert result.get("tool.params_injected") is True, "core_type 不再拦截注入"


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
        raw_tool_calls=[{"name": "bash_execute", "args": {"command": "ls", "_owner": "evil", "_container_id": "x"}}]
    )
    result = await _run(state)
    args = _calls(result)[0]["args"]
    assert "_owner" not in args
    assert "_container_id" not in args
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
        raw_tool_calls=[{"name": "task_submit", "args": {"workspace": "/llm/forged", "parent_task_id": "p1"}}],
        ws_meta=ws_meta,
    )
    args = _calls(await _run(state))[0]["args"]
    assert args["workspace"] == "/llm/forged"  # agent 显式选择项，不覆盖
    assert "isolation_level" not in args
    assert "project_root" not in args
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


async def test_authorized_zones_injected_when_present() -> None:
    """管道级授权写区（zone_grant 卡批准写 state）注入 authorized_zones。

    state 键 = security_check 落盘键（zone_policy.STATE_KEY =
    task.authorized_write_zones，pipeline-state 仅认 task.* 前缀，同名同前缀）。
    """
    import json

    zones = json.dumps([r"d:\proj", r"e:\work"])
    result = await _run(_state(**{"task.authorized_write_zones": zones}))
    (tc,) = _calls(result)
    assert tc["args"]["authorized_zones"] == zones


async def test_authorized_zones_absent_not_injected() -> None:
    """state 无授权键 → 不注入（写工具收 None，走名单判定链）。"""
    result = await _run(_state())
    (tc,) = _calls(result)
    assert "authorized_zones" not in tc["args"]


async def test_authorized_read_zones_injected_when_present() -> None:
    """管道级授权读取（读授权卡批准写 state，用户裁定 2026-09-28）注入。

    state 键与 security_check 落盘键同名同前缀（zone_policy.STATE_KEY_READ =
    task.authorized_read_zones），与写键同取 zone_policy 常量单源。
    """
    import json

    grants = json.dumps([r"d:\proj\config"])
    result = await _run(_state(**{"task.authorized_read_zones": grants}))
    (tc,) = _calls(result)
    assert tc["args"]["authorized_read_zones"] == grants


async def test_authorized_read_zones_absent_not_injected() -> None:
    """state 无读授权键 → 不注入（读工具走黑名单判定链）。"""
    result = await _run(_state())
    (tc,) = _calls(result)
    assert "authorized_read_zones" not in tc["args"]


# ── A3 task_id 注入告警收紧（装机 11-05）：任务管道缺 task.id 才告警，
#    主会话（无血缘键）降 debug 防噪音 ──────────────────────────────


def _taskless_state(tool: str) -> dict[str, Any]:
    """无 task.id 的 state（task.id 置空）+ 指定工具调用。"""
    return _state(**{
        "task.id": "",
        "pipeline_id": "pipe-main-1",
        "raw_tool_calls": [{"name": tool, "args": {}}],
    })


async def test_task_pipeline_missing_task_id_warns(caplog: Any) -> None:
    """任务管道（父形式血缘键在场）缺 task.id → WARNING（真异常）。"""
    import logging

    state = _taskless_state("task_submit")
    state["lineage.parent_pipeline_id"] = "pipe-parent"
    with caplog.at_level(logging.WARNING, logger=_MOD_NAME):
        await _run(state)
    assert any(
        rec.levelno == logging.WARNING and "task_id 注入失败" in rec.message
        for rec in caplog.records
    ), "任务管道缺 task.id 必须告警"


async def test_task_pipeline_root_form_missing_task_id_warns(caplog: Any) -> None:
    """任务管道（根形式血缘键 lineage.root 在场）缺 task.id → 同样告警。"""
    import logging

    state = _taskless_state("task_evaluate")
    state["lineage.root"] = True
    with caplog.at_level(logging.WARNING, logger=_MOD_NAME):
        await _run(state)
    assert any(
        rec.levelno == logging.WARNING and "task_id 注入失败" in rec.message
        for rec in caplog.records
    ), "根形式出生的任务管道缺 task.id 同样必须告警"


async def test_main_session_missing_task_id_downgrades_to_debug(caplog: Any) -> None:
    """主会话（无任何 lineage.* 键）调 task_submit 缺 task.id → 降 DEBUG。"""
    import logging

    with caplog.at_level(logging.DEBUG, logger=_MOD_NAME):
        await _run(_taskless_state("task_submit"))
    warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert not any("task_id 注入失败" in rec.message for rec in warnings), (
        "主会话场景不得产生 WARNING（设计常态，17 条噪音根因）"
    )
    assert any(
        rec.levelno == logging.DEBUG and "task_id 注入失败" in rec.message
        for rec in caplog.records
    ), "主会话场景应降 DEBUG 保留诊断线索"


async def test_other_tools_missing_task_id_stay_silent(caplog: Any) -> None:
    """非 task 域工具（如 memory）缺 task_id → 不产生任何注入失败日志。"""
    import logging

    with caplog.at_level(logging.DEBUG, logger=_MOD_NAME):
        await _run(_taskless_state("memory"))
    assert not any("task_id 注入失败" in rec.message for rec in caplog.records)


# ── 动态工具面：call_tool 快照注入（工具调用路径的可靠定义来源）──────────────
# 为什么走入参：`pipeline-state.list` 读的是内存 registry，而 registry 只在 run
# 收尾时由 `stage_exec` 刷新（`reg.update_state(persisted)`）——run 进行中读不到
# 本轮写入的 `dynamic_tools`。本插件与 `tool_schema` 同轮执行、按 `state.reads`
# 直接看到活值，故同轮把快照放进入参。以下锁定注入契约。


async def test_dynamic_tools_injected_for_call_tool() -> None:
    """`call_tool` 调用收本轮快照（两组有区分度输入：单条 / 多条定义）。"""
    defs_one = [{"name": "file_read", "input_schema": {"type": "object"}}]
    defs_two = defs_one + [{"name": "file_write", "input_schema": {"type": "object"}}]
    for defs in (defs_one, defs_two):
        result = await _run(
            _state(
                raw_tool_calls=[{"name": "call_tool", "args": {"tool": "file_read", "arguments": {}}}],
                dynamic_tools=defs,
            )
        )
        args = _calls(result)[0]["args"]
        assert args["_dynamic_tools"] == defs


async def test_dynamic_tools_not_injected_for_other_tools() -> None:
    """非 `call_tool` 工具不收快照（同一 state 的对照组：call_tool 收、memory 不收）。"""
    defs = [{"name": "file_read", "input_schema": {"type": "object"}}]
    state = _state(dynamic_tools=defs)
    state["raw_tool_calls"] = [
        {"name": "call_tool", "args": {"tool": "file_read", "arguments": {}}},
        {"name": "memory", "args": {}},
    ]
    calls = _calls(await _run(state))
    by_name = {c["name"]: c["args"] for c in calls}
    assert "_dynamic_tools" in by_name["call_tool"], "动态面唯一消费者必须收快照"
    assert "_dynamic_tools" not in by_name["memory"], "其余工具不得无谓增重"


async def test_dynamic_tools_forged_snapshot_not_effective() -> None:
    """模型夹带 `_dynamic_tools` 伪造定义 → 先剥后注入，服务端值权威。"""
    forged = {"name": "evil_tool", "input_schema": {"type": "object"}}
    defs = [{"name": "file_read", "input_schema": {"type": "object"}}]
    result = await _run(
        _state(
            raw_tool_calls=[
                {"name": "call_tool", "args": {"tool": "evil_tool", "arguments": {}, "_dynamic_tools": [forged]}}
            ],
            dynamic_tools=defs,
        )
    )
    args = _calls(result)[0]["args"]
    assert args["_dynamic_tools"] == defs, "伪造快照必须被服务端快照覆盖"


async def test_no_dynamic_tools_state_no_injection() -> None:
    """state 无 `dynamic_tools`（本轮未引入）→ 不注入该键，不塞空对象。"""
    result = await _run(_state(raw_tool_calls=[{"name": "call_tool", "args": {"tool": "x", "arguments": {}}}]))
    args = _calls(result)[0]["args"]
    assert "_dynamic_tools" not in args


# ── R20：视觉输入上限注入（file_read 专属，服务端权威覆盖）──────────────


async def test_vision_max_pixels_injected_into_file_read_only() -> None:
    """state 有规格 → 仅 file_read 收到注入，其余工具不带（避免无谓参数扩散）。"""
    state = _state(
        raw_tool_calls=[
            {"name": "file_read", "args": {"path": "/ws/a.png"}},
            {"name": "bash_execute", "args": {"command": "ls"}},
        ],
        llm_vision_max_pixels=1048576,
    )
    result = await _run(state)
    calls = _calls(result)
    assert calls[0]["args"]["llm_vision_max_pixels"] == 1048576
    assert "llm_vision_max_pixels" not in calls[1]["args"]


async def test_vision_max_pixels_overrides_forged_value() -> None:
    """LLM 夹带的伪造规格被服务端权威值覆盖（防绕过切分/伪造超小规格）。"""
    state = _state(
        llm_vision_max_pixels=1048576,
    )
    state["raw_tool_calls"] = [
        {"name": "file_read", "args": {"path": "/ws/a.png", "llm_vision_max_pixels": 99999}},
    ]
    result = await _run(state)
    (tc,) = _calls(result)
    assert tc["args"]["llm_vision_max_pixels"] == 1048576


async def test_vision_max_pixels_absent_or_invalid_not_injected() -> None:
    """state 无键（非视觉模型常态）/脏值 → 不注入，file_read 行为与既往一致。"""
    result = await _run(_state(raw_tool_calls=[{"name": "file_read", "args": {}}]))
    assert "llm_vision_max_pixels" not in _calls(result)[0]["args"]
    for dirty in (0, -3, "1568", True, 1.5):
        state = _state(llm_vision_max_pixels=dirty)
        state["raw_tool_calls"] = [{"name": "file_read", "args": {}}]
        result = await _run(state)
        assert "llm_vision_max_pixels" not in _calls(result)[0]["args"], f"dirty={dirty!r}"


# ── 动态目标（call_tool）嵌套参数注入：与原生同一份注入代码 ──────────────

_DYNAMIC_FS = {
    "name": "FileSystem",
    "description": "fs",
    "input_schema": {"type": "object", "properties": {"mode": {"type": "string"}}},
    "plugin_id": "mcp_windows_mcp",
    "source": "code",
}


async def test_call_tool_target_gets_native_context_injected() -> None:
    """call_tool 的嵌套目标参数收到与原生相同的环境键。"""
    state = _state(
        raw_tool_calls=[
            {"name": "call_tool", "args": {"tool": "FileSystem", "arguments": {"mode": "list"}}}
        ],
        dynamic_tools=[_DYNAMIC_FS],
    )
    result = await _run(state)
    nested = _calls(result)[0]["args"]["arguments"]
    assert nested["mode"] == "list"
    assert nested["session_id"] == "s-1"
    assert nested["workspace"] == "/ws/root"
    assert nested["task_id"] == "t-1"
    assert "timestamp" in nested


async def test_call_tool_target_external_mcp_skips_injection() -> None:
    """source=mcp 目标：不注入（严格 MCP server 不接受未声明参数）。"""
    state = _state(
        raw_tool_calls=[
            {"name": "call_tool", "args": {"tool": "mcp_fetch", "arguments": {"url": "u"}}}
        ],
        dynamic_tools=[{**_DYNAMIC_FS, "name": "mcp_fetch", "source": "mcp"}],
    )
    result = await _run(state)
    nested = _calls(result)[0]["args"]["arguments"]
    assert nested == {"url": "u"}


async def test_call_tool_target_default_params_and_model_overwrite_semantics() -> None:
    """目标默认参数补缺；workspace 服务端覆盖、session_id 模型值保留（原生语义）。"""
    state = _state(
        raw_tool_calls=[
            {
                "name": "call_tool",
                "args": {
                    "tool": "FileSystem",
                    "arguments": {"session_id": "model-s", "workspace": "/model/ws"},
                },
            }
        ],
        dynamic_tools=[_DYNAMIC_FS],
    )
    result = await _run(state, config={"default_params": {"FileSystem": {"mode": "list"}}})
    nested = _calls(result)[0]["args"]["arguments"]
    assert nested["mode"] == "list", "目标默认参数按原生语义补缺"
    assert nested["session_id"] == "model-s", "身份键仅补缺，不覆盖模型值"
    assert nested["workspace"] == "/ws/root", "工作区按原生语义服务端权威覆盖"


async def test_dynamic_target_args_parity_with_native_call() -> None:
    """同一工具/同一 state：动态嵌套参数与原生调用参数逐键一致（除时间戳）。"""
    state = _state(
        raw_tool_calls=[
            {"name": "FileSystem", "args": {"mode": "list"}},
            {"name": "call_tool", "args": {"tool": "FileSystem", "arguments": {"mode": "list"}}},
        ],
        dynamic_tools=[_DYNAMIC_FS],
    )
    result = await _run(state)
    native_args = dict(_calls(result)[0]["args"])
    dynamic_args = dict(_calls(result)[1]["args"]["arguments"])
    native_args.pop("timestamp", None)
    dynamic_args.pop("timestamp", None)
    assert dynamic_args == native_args


async def test_call_tool_target_underscore_forgery_stripped() -> None:
    """模型在嵌套目标参数里伪造下划线键 → 先剥（与原生同一边界）。"""
    state = _state(
        raw_tool_calls=[
            {
                "name": "call_tool",
                "args": {
                    "tool": "FileSystem",
                    "arguments": {"mode": "list", "_container_id": "forged"},
                },
            }
        ],
        dynamic_tools=[_DYNAMIC_FS],
    )
    result = await _run(state)
    nested = _calls(result)[0]["args"]["arguments"]
    assert "_container_id" not in nested


async def test_call_tool_target_json_string_arguments_parsed_before_injection() -> None:
    """嵌套 arguments 是 JSON 字符串 → 注入方先解析为对象再注入（壳只透传）。"""
    state = _state(
        raw_tool_calls=[
            {
                "name": "call_tool",
                "args": {
                    "tool": "FileSystem",
                    "arguments": '{"mode": "list"}',
                },
            }
        ],
        dynamic_tools=[_DYNAMIC_FS],
    )
    result = await _run(state)
    nested = _calls(result)[0]["args"]["arguments"]
    assert isinstance(nested, dict)
    assert nested["mode"] == "list"
    assert nested["workspace"] == "/ws/root"
