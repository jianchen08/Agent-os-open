# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
# -*- coding: utf-8 -*-
"""六模式包写动作成功路径补测（对 test_mode_servers_gaps 的补位）。

gaps 文件覆盖写动作的错误面（通道抛错/缺任务 id/非法载荷）；本文件驱动
成功面：假 tool-executor 回放含 id 的结果，断言 200 载荷形状与派发 args
契约（目标键/mode/task_kind/parent_agent_level/session 锚点/继承键）。
行级动作（followup/regenerate/chapter_act 带 pipeline_id）以 pipeline-state
+ messages 假 provider 供行数据，断言行级锚定与工作空间继承口径。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit

_loaded: dict[str, Any] = {}


def _fresh(mode: str):
    """按唯一模块名装载模式包 server.py 并复位 provider（防双实例串扰）。"""
    if mode not in _loaded:
        path = os.path.join(MODES_DIR, mode, "server.py")
        spec = importlib.util.spec_from_file_location(f"mode_wa_{mode}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _loaded[mode] = module
    module = _loaded[mode]
    module.reset_providers()
    return module


def _call(module, path: str, method: str = "GET", raw_body: str = "", query: dict | None = None) -> dict:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_json(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status(result: dict) -> int:
    return int(result["data"]["status"])


class _RecordingExecutor:
    """假 tool-executor：记录调用载荷并回放预置结果。"""

    def __init__(self, result: Any = None):
        self.result = result if result is not None else {"data": {"task_id": "task-ok"}}
        self.last: dict | None = None

    async def __call__(self, payload: dict) -> Any:
        self.last = payload
        return self.result


def _install_state(module, rows: list[dict], messages: list[dict]) -> None:
    """装 pipeline-state / messages 假 provider（行级动作的数据源，异步取数同形）。"""

    async def _state():
        return rows

    async def _messages(pipeline_id, limit=None):
        return messages

    module._set_provider("pipeline-state", _state)
    module._set_provider("messages", _messages)


# ── planning：create_project / plan ──────────────────────────────────────────────


def test_planning_create_project_success_full_shape():
    """创建项目成功 → 200 全字段形状；可选参 path/project_type 非空才透传。"""
    module = _fresh("mode_planning")
    executor = _RecordingExecutor(
        {
            "data": {
                "project_id": "proj-1",
                "title": "新项目",
                "path": "D:/w/proj-1",
                "workflow_state": "created",
                "created": True,
            }
        }
    )
    module._set_provider("tool-executor", executor)

    body = {"goal": "做一款塔防", "path": "D:/w/proj-1", "project_type": "game"}
    out = _body_json(
        _call(module, "/ext/mode_planning/data/actions/create_project", "POST", json.dumps(body))
    )
    assert out["project_id"] == "proj-1"
    assert out["title"] == "新项目" and out["created"] is True
    assert executor.last["tool_name"] == "project_create"
    assert executor.last["args"]["goal"] == "做一款塔防"
    assert executor.last["args"]["path"] == "D:/w/proj-1"

    # 可选参缺省 → args 不携带空键
    _call(module, "/ext/mode_planning/data/actions/create_project", "POST", json.dumps({"goal": "g"}))
    assert "path" not in executor.last["args"]
    assert "project_type" not in executor.last["args"]


def test_planning_plan_success_with_session_passthrough():
    """发起规划成功 → 200 task_id；session_id 非空透传，派发契约键齐全。"""
    module = _fresh("mode_planning")
    executor = _RecordingExecutor({"data": {"task_id": "task-9"}})
    module._set_provider("tool-executor", executor)

    body = {"goal": "规划一个博客系统", "session_id": "sess-1"}
    out = _body_json(
        _call(module, "/ext/mode_planning/data/actions/plan", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-9"}
    args = executor.last["args"]
    assert executor.last["tool_name"] == "task_submit"
    assert args["target_id"] == "executor/generation/research_agent"
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "planning" and args["task_kind"] == "planning_plan"
    assert args["session_id"] == "sess-1"
    assert args["goal_title"].startswith("方案规划·")


# ── godot：dispatch / open_editor ────────────────────────────────────────────────


def test_godot_dispatch_success_contract():
    """派发修改成功 → 200 task_id；编排键+场景修改标注+会话锚点入 args。"""
    module = _fresh("mode_godot")
    executor = _RecordingExecutor({"data": {"task_id": "t-god-1"}})
    module._set_provider("tool-executor", executor)

    body = {"instruction": "把主菜单按钮放大", "session_id": "sess-g"}
    out = _body_json(
        _call(module, "/ext/mode_godot/data/actions/dispatch", "POST", json.dumps(body))
    )
    assert out == {"task_id": "t-god-1"}
    args = executor.last["args"]
    assert args["target_id"] == "mode_godot/godot_orchestrator_agent"
    assert args["task_kind"] == "godot_scene_edit"
    assert args["session_id"] == "sess-g"
    assert 'source="godot"' in args["goal_description"]


def _fake_godot_open(module, result: Any):
    executor = _RecordingExecutor(result)
    module._set_provider("tool-executor", executor)
    return executor


def test_godot_open_editor_success():
    """打开编辑器成功 → 200 result 透传；调 godot_run 探活命令形状。"""
    module = _fresh("mode_godot")
    executor = _fake_godot_open(module, {"data": {"stdout": "editor alive"}})
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/open_editor", "POST", "{}"))
    assert out == {"result": {"stdout": "editor alive"}}
    assert executor.last["tool_name"] == "godot_run"
    assert executor.last["plugin_id"] == "godot_mcp"
    assert executor.last["args"] == {"method": "engine.commands", "params": {}}


def test_godot_open_editor_channel_down_and_invalid_result():
    """通道未注入/返回非 dict → error 如实回 200（写面无假成功）。"""
    module = _fresh("mode_godot")
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/open_editor", "POST", "{}"))
    assert "通道不可用" in out["error"]

    _fake_godot_open(module, "not-a-dict")
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/open_editor", "POST", "{}"))
    assert "未返回有效结果" in out["error"]

    module = _fresh("mode_godot")  # 复位：派发通道未注入
    out = _body_json(
        _call(module, "/ext/mode_godot/data/actions/dispatch", "POST", json.dumps({"instruction": "x"}))
    )
    assert "通道不可用" in out["error"]


# ── research：start（双档位）/ followup ──────────────────────────────────────────


@pytest.mark.parametrize("depth,kind", [("quick", "research_quick"), ("deep", "research_deep")])
def test_research_start_success_both_depths(depth: str, kind: str):
    """发起调研成功（quick/deep 双档）→ 200 task_id；档位要求入派发描述。"""
    module = _fresh("mode_research")
    executor = _RecordingExecutor({"data": {"task_id": f"task-{depth}"}})
    module._set_provider("tool-executor", executor)

    body = {"question": "Rust 信号机制现状", "depth": depth, "session_id": "sess-r"}
    out = _body_json(
        _call(module, "/ext/mode_research/data/actions/start", "POST", json.dumps(body))
    )
    assert out == {"task_id": f"task-{depth}"}
    args = executor.last["args"]
    assert args["target_id"] == "orchestrator/research_orchestrator_agent"
    assert args["task_kind"] == kind
    assert args["metadata"] == {"depth": depth}
    assert args["session_id"] == "sess-r"
    assert "档位要求" in args["goal_description"]


def test_research_followup_success_row_anchor():
    """追问成功 → 行级锚定（thread_id 优先）+ 原报告节选入派发描述。"""
    module = _fresh("mode_research")
    executor = _RecordingExecutor({"data": {"task_id": "task-fu"}})
    module._set_provider("tool-executor", executor)
    _install_state(
        module,
        rows=[{"mode": "research", "pipeline_id": "p-r1", "thread_id": "th-r1", "goal": "原调研"}],
        messages=[{"role": "assistant", "content_preview": "结论：信号机制已统一。"}],
    )
    body = {"pipeline_id": "p-r1", "question": "补充跨平台对比"}
    out = _body_json(
        _call(module, "/ext/mode_research/data/actions/followup", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-fu"}
    args = executor.last["args"]
    assert args["task_kind"] == "research_followup"
    assert args["metadata"] == {"source_pipeline_id": "p-r1"}
    assert args["session_id"] == "th-r1"
    assert "结论：信号机制已统一。" in args["goal_description"]
    assert "补充跨平台对比" in args["goal_description"]


def test_research_followup_errors_unknown_session_and_no_report():
    """追问错误分诊：未知会话 400；无 assistant 输出 409。"""
    module = _fresh("mode_research")
    _install_state(module, rows=[], messages=[])

    result = _call(
        module,
        "/ext/mode_research/data/actions/followup",
        "POST",
        json.dumps({"pipeline_id": "p-x", "question": "q"}),
    )
    assert _status(result) == 400 and "会话不存在" in _body_json(result)["error"]

    _install_state(
        module,
        rows=[{"mode": "research", "pipeline_id": "p-r1", "thread_id": "th-r1"}],
        messages=[{"role": "user", "content_preview": "只有提问"}],
    )
    result = _call(
        module,
        "/ext/mode_research/data/actions/followup",
        "POST",
        json.dumps({"pipeline_id": "p-r1", "question": "q"}),
    )
    assert _status(result) == 409 and "还没有可追问" in _body_json(result)["error"]


# ── roleplay：play / regenerate ──────────────────────────────────────────────────


def test_roleplay_play_success_with_real_seed_card():
    """以此角色开演成功（出厂卡 card_luna）→ 目标=卡 agent 键+人设入描述。

    BUG-73 契约：开演锚**卡专属扮演会话**（thread-rp-<card_id>，同卡复演复用），
    body 的宿主活跃 session_id 一律忽略（R92 同族「落活跃既有 thread」根除）；
    所选开场白全文随派发注入（greeting_index=2 → 备选第 2 条原文入上下文）。
    """
    module = _fresh("mode_roleplay")
    executor = _RecordingExecutor({"data": {"task_id": "task-play"}})
    module._set_provider("tool-executor", executor)

    body = {
        "card_id": "card_luna",
        "user_persona": "旅行者",
        "greeting_index": 2,
        "session_id": "sess-active-host",
    }
    out = _body_json(
        _call(module, "/ext/mode_roleplay/data/actions/play", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-play"}
    args = executor.last["args"]
    assert args["target_id"] == "mode_roleplay/card_luna"
    assert args["task_kind"] == "roleplay_opening"
    assert args["metadata"] == {"card_id": "card_luna", "greeting_index": 2}
    # 专属扮演会话锚：卡维度派生，与宿主活跃会话无关（body session_id 被忽略）
    assert args["session_id"] == "thread-rp-card_luna"
    assert args["session_id"] != "sess-active-host"
    assert "塞拉菲娜·月语" in args["goal_description"]
    assert "# 用户设定（对话者扮演）" in args["goal_description"]
    # 出厂卡自带性格与场景字段 → 两段都入描述
    assert "# 性格" in args["goal_description"]
    assert "# 场景" in args["goal_description"]
    # 所选开场白全文注入（greeting_index=2 = alternate_greetings[1]）
    assert "# 本场开场白" in args["goal_description"]
    assert "一曲还缺一个听众" in args["goal_description"]
    # 开场表演必须作为回复正文输出（不得只写进 task_evaluate summary——
    # 首次开演 0bf057da402f 的可观测病灶：可见消息只剩待办清单）
    assert "回复正文" in args["goal_description"]


def test_roleplay_play_greeting_fallback_out_of_range():
    """greeting_index 越界/缺省 → 回落 first_mes（不虚构不报错），锚仍专属。"""
    module = _fresh("mode_roleplay")
    executor = _RecordingExecutor({"data": {"task_id": "task-play-fb"}})
    module._set_provider("tool-executor", executor)

    # 越界（card_luna 仅 2 条备选：index 9 不存在）
    body = {"card_id": "card_luna", "greeting_index": 9}
    _body_json(
        _call(module, "/ext/mode_roleplay/data/actions/play", "POST", json.dumps(body))
    )
    args = executor.last["args"]
    assert "以月神之名" in args["goal_description"]  # first_mes 原文
    assert args["metadata"]["greeting_index"] == 0
    assert args["session_id"] == "thread-rp-card_luna"

    # 缺省 greeting_index → 同 first_mes 口径；body 带宿主 session_id 仍被忽略
    executor.last = None
    body2 = {"card_id": "card_luna", "session_id": "thread-858ab1bd"}
    _body_json(
        _call(module, "/ext/mode_roleplay/data/actions/play", "POST", json.dumps(body2))
    )
    args2 = executor.last["args"]
    assert "以月神之名" in args2["goal_description"]
    assert args2["metadata"]["greeting_index"] == 0
    assert args2["session_id"] == "thread-rp-card_luna"


def test_roleplay_regenerate_success_row_anchor():
    """重新生成成功 → 目标=行 agent_id，上一条角色回复节选入派发描述。"""
    module = _fresh("mode_roleplay")
    executor = _RecordingExecutor({"data": {"task_id": "task-regen"}})
    module._set_provider("tool-executor", executor)
    _install_state(
        module,
        rows=[
            {
                "mode": "roleplay",
                "pipeline_id": "p-rp1",
                "thread_id": "th-rp1",
                "agent_id": "mode_roleplay/card_luna",
                "task.goal": "开演",
            }
        ],
        messages=[{"role": "assistant", "content_preview": "（微微一笑）欢迎，旅人。"}],
    )
    body = {"pipeline_id": "p-rp1", "session_id": "sess-body"}
    out = _body_json(
        _call(module, "/ext/mode_roleplay/data/actions/regenerate", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-regen"}
    args = executor.last["args"]
    assert args["target_id"] == "mode_roleplay/card_luna"
    assert args["task_kind"] == "roleplay_regenerate"
    assert args["metadata"] == {"source_pipeline_id": "p-rp1"}
    assert args["session_id"] == "th-rp1"  # 行 thread_id 优先于 body 兜底
    assert "（微微一笑）欢迎，旅人。" in args["goal_description"]


def test_roleplay_regenerate_errors_unknown_session_and_no_reply():
    """重新生成错误分诊：未知会话 400；无 assistant 回复 409。"""
    module = _fresh("mode_roleplay")
    _install_state(module, rows=[], messages=[])
    result = _call(
        module,
        "/ext/mode_roleplay/data/actions/regenerate",
        "POST",
        json.dumps({"pipeline_id": "p-x"}),
    )
    assert _status(result) == 400 and "会话不存在" in _body_json(result)["error"]

    _install_state(
        module,
        rows=[{"mode": "roleplay", "pipeline_id": "p-rp1", "thread_id": "th-rp1"}],
        messages=[{"role": "user", "content_preview": "只有用户输入"}],
    )
    result = _call(
        module,
        "/ext/mode_roleplay/data/actions/regenerate",
        "POST",
        json.dumps({"pipeline_id": "p-rp1"}),
    )
    assert _status(result) == 409 and "还没有可重新生成" in _body_json(result)["error"]


# ── writing：chapter_act ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "act,kind",
    [
        ("continue", "writing_continue"),
        ("expand", "writing_expand"),
        ("rewrite", "writing_rewrite"),
        ("outline", "writing_outline"),
        ("brainstorm", "writing_brainstorm"),
    ],
)
def test_writing_chapter_act_success_new_session_passthrough(act: str, kind: str):
    """新建章动作（无 pipeline_id）五档全过 → 会话锚=body，目标=执行者池首选。"""
    module = _fresh("mode_writing")
    executor = _RecordingExecutor({"data": {"task_id": f"task-{act}"}})
    module._set_provider("tool-executor", executor)

    body = {"act": act, "instruction": "第三卷开篇", "session_id": "sess-w"}
    out = _body_json(
        _call(module, "/ext/mode_writing/data/actions/chapter_act", "POST", json.dumps(body))
    )
    assert out == {"task_id": f"task-{act}"}
    args = executor.last["args"]
    assert args["task_kind"] == kind
    assert args["parent_agent_level"] == 1
    assert args["session_id"] == "sess-w"
    assert "inherit_mode" not in args and "inherit_from" not in args


def test_writing_chapter_act_workspace_inherit_from_row():
    """带 pipeline_id 且行有 task.id → 工作空间继承键 + 行 thread 锚优先。"""
    module = _fresh("mode_writing")
    executor = _RecordingExecutor({"data": {"task_id": "task-ws"}})
    module._set_provider("tool-executor", executor)
    _install_state(
        module,
        rows=[
            {
                "mode": "writing",
                "pipeline_id": "p-w1",
                "thread_id": "th-w1",
                "task.id": "task-parent",
                "task.goal": "卷一",
            }
        ],
        messages=[],
    )
    body = {"act": "continue", "instruction": "", "pipeline_id": "p-w1", "session_id": "sess-body"}
    out = _body_json(
        _call(module, "/ext/mode_writing/data/actions/chapter_act", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-ws"}
    args = executor.last["args"]
    assert args["inherit_mode"] == "workspace"
    assert args["inherit_from"] == "task-parent"
    assert args["metadata"] == {"source_pipeline_id": "p-w1"}
    assert args["session_id"] == "th-w1"  # 行锚优先于 body 兜底


def test_writing_chapter_act_row_without_task_id_no_inherit():
    """带 pipeline_id 但行无 task.id → 不带继承键，仍记来源管道。"""
    module = _fresh("mode_writing")
    executor = _RecordingExecutor({"data": {"task_id": "task-ws2"}})
    module._set_provider("tool-executor", executor)
    _install_state(
        module,
        rows=[{"mode": "writing", "pipeline_id": "p-w2", "thread_id": "", "task.goal": "卷二"}],
        messages=[],
    )
    body = {"act": "outline", "pipeline_id": "p-w2"}
    out = _body_json(
        _call(module, "/ext/mode_writing/data/actions/chapter_act", "POST", json.dumps(body))
    )
    assert out == {"task_id": "task-ws2"}
    args = executor.last["args"]
    assert "inherit_mode" not in args and "inherit_from" not in args
    assert "session_id" not in args
    assert args["metadata"] == {"source_pipeline_id": "p-w2"}


def test_writing_chapter_act_unknown_pipeline():
    """带未知 pipeline_id → 400 会话不存在（不静默按新建派发）。"""
    module = _fresh("mode_writing")
    module._set_provider("tool-executor", _RecordingExecutor())
    _install_state(module, rows=[], messages=[])
    result = _call(
        module,
        "/ext/mode_writing/data/actions/chapter_act",
        "POST",
        json.dumps({"act": "continue", "pipeline_id": "p-x"}),
    )
    assert _status(result) == 400 and "会话不存在" in _body_json(result)["error"]
