# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""研究模式活面板行为测试：报告解析（交付文件直读优先/消息回落）/ URL 抽取 /
KB 两态 / 写动作 args 捕获。

ADR 2026-09-17-mode-panel-mature-interfaces：research 面板对标 Perplexity/Elicit
（研究任务 + 报告阅读器[引用角标] + 信源库）。provider 全 fake，不真派发任务；
装载模块名前缀 mode_live_mode_research，与契约文件（test_mode_panel_pages）互不覆写。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EP = "/ext/mode_research/data"

pytestmark = pytest.mark.unit


def _load_server():
    """按唯一模块名装载 mode_research/server.py（每次调用全新 provider 态）。"""
    path = os.path.join(MODES_DIR, "mode_research", "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_research", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
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


def _envelope_status(result: dict) -> int:
    """HTTP 语义落信封 data.status（同 test_mode_panel_pages 契约）。"""
    return int(result["data"]["status"])


# ── fake 内核数据 ────────────────────────────────────────────────────────────────

def _state_rows() -> list[dict]:
    return [
        {  # 本模式行
            "pipeline_id": "pipe-aaa", "thread_id": "th-1", "agent_id": "main",
            "run_status": "completed", "mode": "research",
            "task.goal": "边缘部署下大模型量化方案取舍", "task.status": "completed",
            "message_count": 2,
        },
        {  # 他模式行：必须被过滤
            "pipeline_id": "pipe-bbb", "thread_id": "th-2", "agent_id": "main",
            "run_status": "completed", "mode": "coding", "task.goal": "修 bug",
            "message_count": 9,
        },
    ]


_BODY = "调研结论：多轮检索交叉核验后收敛如下。" + "论据充分展开，覆盖正反两面证据与适用边界。" * 12


def _messages_with(content: str) -> list[dict]:
    return [
        {"role": "user", "content_preview": "帮我调研量化方案", "status": "success", "created_at": "t1"},
        {"role": "assistant", "content_preview": content, "status": "success", "created_at": "t2"},
    ]


def _set_messages(module, content: str, captured: dict | None = None) -> None:
    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        if captured is not None:
            captured["pipeline_id"] = pipeline_id
        return _messages_with(content)

    module._set_provider("messages", _provider)


# ── 报告解析（/data/report）──────────────────────────────────────────────────────

def test_report_parses_citations_and_urls() -> None:
    """带角标带 URL 报告：角标有序去重、URL 去尾标点去重、字数=正文长度。"""
    module = _load_server()
    content = (
        _BODY
        + "结论先后被[3]与[1]佐证，[2]亦被引用，[3]重复出现。"
        + "信源：https://example.com/a，https://example.com/b。https://example.com/a "
    )
    _set_messages(module, content)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body["report"] == content
    assert body["word_count"] == len(content)
    assert body["citations_found"] == [3, 1, 2]  # 首次出现序 + 去重
    assert body["sources"] == [
        "https://example.com/a",
        "https://example.com/b",
    ]  # 中文标点去尾 + 去重保序


def test_report_without_citations_or_urls() -> None:
    """无角标无 URL 报告：列表为空但正文完整（≥2 组有区分度输入之反例组）。"""
    module = _load_server()
    _set_messages(module, _BODY)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body["report"] == _BODY
    assert body["word_count"] == len(_BODY)
    assert body["citations_found"] == []
    assert body["sources"] == []


def test_report_picks_last_long_assistant_message() -> None:
    """报告 = 最后一条达阈值的 assistant 消息（短消息与更早长消息都不入选）。"""
    module = _load_server()

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        return [
            {"role": "assistant", "content_preview": _BODY + "第一版长输出。", "status": "success", "created_at": "t1"},
            {"role": "assistant", "content_preview": "太短，不是报告", "status": "success", "created_at": "t2"},
            {"role": "assistant", "content_preview": _BODY + "最终版[9]。", "status": "success", "created_at": "t3"},
        ]

    module._set_provider("messages", _provider)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body["report"].endswith("最终版[9]。")
    assert body["citations_found"] == [9]


def test_report_honest_empty_without_long_assistant() -> None:
    """只有短消息 → 空报告载荷（不拿短消息凑数）；source=message 如实标注。"""
    module = _load_server()

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        return [{"role": "assistant", "content_preview": "还在检索中", "status": "success", "created_at": "t1"}]

    module._set_provider("messages", _provider)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body == {
        "report": "", "word_count": 0, "citations_found": [], "sources": [],
        "source": "message",
    }


def test_report_reads_deliverable_file_first(tmp_path) -> None:
    """交付文件直读优先：task_manage.get → research_report_path → 工作空间文件。

    source=file + report_path 如实标注；角标/信源解析对文件正文同样生效。
    """
    module = _load_server()
    file_body = _BODY + "文件版信源 https://example.com/report。"
    (tmp_path / "report.md").write_text(file_body, encoding="utf-8")
    rows = [
        {
            "pipeline_id": "pipe-aaa", "mode": "research",
            "task.id": "task-r1", "task.ws_meta": {"path": str(tmp_path)},
        }
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {
            "data": {
                "task": {
                    "result_data": json.dumps({"research_report_path": "report.md"}),
                }
            }
        }

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert captured["tool_name"] == "task_manage" and captured["plugin_id"] == "task_manage_tool"
    assert captured["args"] == {"action": "get", "task_id": "task-r1"}
    assert body["source"] == "file" and body["report_path"] == "report.md"
    assert body["report"] == file_body
    assert body["sources"] == ["https://example.com/report"]


def test_report_falls_back_to_message_without_deliverable() -> None:
    """交付文件链任一环缺失（result_data 无路径/文件不可读）→ 回落消息启发式。"""
    module = _load_server()
    rows = [
        {
            "pipeline_id": "pipe-aaa", "mode": "research",
            "task.id": "task-r1", "task.ws_meta": {"path": "Z:/no/such/dir"},
        }
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))

    async def _fake_invoke(payload: dict) -> dict:
        # result_data 无 research_report_path → 直读链断 → 回落
        return {"data": {"task": {"result_data": "{}"}}}

    module._set_provider("tool-executor", _fake_invoke)
    _set_messages(module, _BODY)
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body["source"] == "message"
    assert body["report"] == _BODY


def test_report_and_messages_missing_params() -> None:
    module = _load_server()
    assert _envelope_status(_call(module, f"{EP}/report")) == 400
    assert _envelope_status(_call(module, f"{EP}/messages")) == 400
    assert _envelope_status(_call(module, f"{EP}/messages", method="POST")) == 404


def test_bootstrap_contract() -> None:
    module = _load_server()
    body = _body_json(_call(module, f"{EP}/bootstrap"))
    assert body["mode"] == "research" and body["panel_page_id"] == "research_desk"


# ── 会话归一 + 无 provider 降级 ─────────────────────────────────────────────────

def test_sessions_filter_and_normalize() -> None:
    module = _load_server()
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=_state_rows()))
    sessions = _body_json(_call(module, f"{EP}/sessions"))["sessions"]
    assert len(sessions) == 1
    row = sessions[0]
    assert row["pipeline_id"] == "pipe-aaa"
    assert row["goal"] == "边缘部署下大模型量化方案取舍"
    assert row["task_status"] == "completed" and row["message_count"] == 2


def test_no_provider_degrades_to_empty_payloads() -> None:
    """内核 provider 未注入 → 会话空列表 / 报告空载荷（前端诚实空态，不崩）。"""
    module = _load_server()
    assert _body_json(_call(module, f"{EP}/sessions")) == {"sessions": []}
    body = _body_json(_call(module, f"{EP}/report", query={"pipeline_id": "pipe-aaa"}))
    assert body == {
        "report": "", "word_count": 0, "citations_found": [], "sources": [],
        "source": "message",
    }


# ── 信源库（/data/sources：URL 抽取 + KB 两态）──────────────────────────────────

def test_sources_url_extraction_by_pipeline_id() -> None:
    """带 pipeline_id 无 q → 只做报告 URL 抽取，无 KB 键。"""
    module = _load_server()
    content = _BODY + "来源 https://example.com/report。"
    captured: dict = {}
    _set_messages(module, content, captured)
    body = _body_json(_call(module, f"{EP}/sources", query={"pipeline_id": "pipe-aaa"}))
    assert captured["pipeline_id"] == "pipe-aaa"
    assert body == {"sources": ["https://example.com/report"]}


def test_sources_kb_search_with_provider() -> None:
    """q + tool-executor provider → hindsight.recall 真调用形状 + 结果归一。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"results": [{"id": "m1", "content": "量化知识条目", "score": 0.87}]}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(_call(module, f"{EP}/sources", query={"q": "量化方案"}))
    assert body["kb_available"] is True
    assert body["kb_results"] == [{"id": "m1", "content": "量化知识条目", "score": 0.87}]
    assert captured["tool_name"] == "hindsight.recall"
    assert captured["plugin_id"] == "hindsight_memory_service"
    assert captured["args"]["query"] == "量化方案"
    assert captured["args"]["bank_id"] == "kb"


def test_sources_kb_unavailable_without_provider() -> None:
    """无 provider → kb_available=False + 诚实降级注记；URL 抽取不受影响。"""
    module = _load_server()
    content = _BODY + "来源 https://example.com/report。"
    _set_messages(module, content)
    body = _body_json(
        _call(module, f"{EP}/sources", query={"q": "量化", "pipeline_id": "pipe-aaa"})
    )
    assert body["kb_available"] is False
    assert body["kb_results"] == []
    assert body["note"]
    assert body["sources"] == ["https://example.com/report"]


def test_sources_kb_error_envelope_degrades() -> None:
    """KB 后端报错（降级信封）→ 如实 kb_available=False，不造假结果。"""
    module = _load_server()

    async def _fake_invoke(payload: dict) -> dict:
        return {"data": {"error": "hindsight 后端未初始化"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(_call(module, f"{EP}/sources", query={"q": "量化"}))
    assert body["kb_available"] is False
    assert body["kb_results"] == []
    assert body["sources"] == []


def test_sources_missing_params() -> None:
    module = _load_server()
    assert _envelope_status(_call(module, f"{EP}/sources")) == 400


# ── 写动作（task_submit 真派发形状；fake 捕获不落任务）──────────────────────────

def test_start_action_dispatches_task_submit() -> None:
    """发起研究：mode/task_kind/goal 随深度档变化的 args 形状。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-r1"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(module, f"{EP}/actions/start", method="POST",
              raw_body=json.dumps({"question": "量化方案怎么选", "depth": "deep"}))
    )
    assert body == {"task_id": "task-r1"}
    assert captured["tool_name"] == "task_submit" and captured["plugin_id"] == "task_submit_tool"
    args = captured["args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "orchestrator/research_orchestrator_agent"
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "research" and args["task_kind"] == "research_deep"
    assert "量化方案怎么选" in args["goal_description"]
    assert "多轮检索" in args["goal_description"]  # deep 档要求
    assert args["goal_title"].startswith("研究·")
    assert len(args["goal_description"]) <= 2000
    assert args["metadata"] == {"depth": "deep"}

    # 换档位：task_kind 与档位要求随之变化（≥2 组有区分度输入）
    captured.clear()
    _body_json(
        _call(module, f"{EP}/actions/start", method="POST",
              raw_body=json.dumps({"question": "简明版问题", "depth": "quick"}))
    )
    args = captured["args"]
    assert args["task_kind"] == "research_quick"
    assert "单轮聚焦检索" in args["goal_description"]
    assert args["metadata"] == {"depth": "quick"}


def test_start_action_missing_params() -> None:
    module = _load_server()
    # 空 body → 400
    assert _envelope_status(_call(module, f"{EP}/actions/start", method="POST")) == 400
    # 缺 depth / 非法 depth → 400
    assert _envelope_status(
        _call(module, f"{EP}/actions/start", method="POST",
              raw_body=json.dumps({"question": "只有问题"}))
    ) == 400
    result = _call(
        module, f"{EP}/actions/start", method="POST",
        raw_body=json.dumps({"question": "q", "depth": "ultra"}),
    )
    assert _envelope_status(result) == 400
    # GET 打写动作端点 → 404
    assert _envelope_status(_call(module, f"{EP}/actions/start")) == 404


def test_start_action_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-b64"}}

    module._set_provider("tool-executor", _fake_invoke)
    payload = {"question": "内核 base64 形态怎么解析", "depth": "deep"}
    raw_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    body = _body_json(
        _call(module, f"{EP}/actions/start", method="POST", raw_body=raw_b64)
    )
    assert body == {"task_id": "task-b64"}
    args = captured["args"]
    assert args["target_id"] == "orchestrator/research_orchestrator_agent"
    assert "内核 base64 形态怎么解析" in args["goal_description"]
    assert args["metadata"] == {"depth": "deep"}

    # 裸 JSON 形态（第二组区分度输入）：quick 档解析结果不同
    captured.clear()
    _body_json(
        _call(module, f"{EP}/actions/start", method="POST",
              raw_body=json.dumps({"question": "裸 JSON 形态", "depth": "quick"}))
    )
    args = captured["args"]
    assert args["task_kind"] == "research_quick"
    assert "裸 JSON 形态" in args["goal_description"]


def test_dispatch_target_key_resolves_through_task_submit_chain() -> None:
    """F2 目标键合法性（真解析链，离线）：task_submit 磁盘回退查找命中配置，
    level L2/L3（闸门拒 L1 与缺失）、is_active——锁住 target 键可派发性。"""
    tool_module = _load_task_submit_tool()
    config, corrupt = tool_module.TaskSubmitTool._load_agent_yaml_dict(
        "orchestrator/research_orchestrator_agent"
    )
    assert corrupt == ""
    assert isinstance(config, dict)
    assert config.get("level") in ("L2", "L3")
    assert config.get("is_active", True) is True


def _load_task_submit_tool():
    """装载 task_submit 工具模块（唯一实例），键合法性走真解析链。"""
    if "task_submit_tool_live_module" in sys.modules:
        return sys.modules["task_submit_tool_live_module"]
    shared_root = os.path.dirname(MODES_DIR)  # plugins/shared
    tool_dir = os.path.join(shared_root, "tools", "task_submit")
    for p in (shared_root, tool_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    path = os.path.join(tool_dir, "tool.py")
    spec = importlib.util.spec_from_file_location("task_submit_tool_live_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_followup_action_composes_from_history() -> None:
    """追问：goal 携原报告节选（最后一条 assistant 前 600 字）+ 追问内容。"""
    module = _load_server()
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=_state_rows()))
    long_answer = _BODY + "补充展开。" * 70  # 271 + 350 = 621 字 > 600 截取窗
    captured_msgs: dict = {}

    async def _messages_provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        captured_msgs["pipeline_id"] = pipeline_id
        return _messages_with(long_answer)

    module._set_provider("messages", _messages_provider)
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-r2"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(module, f"{EP}/actions/followup", method="POST",
              raw_body=json.dumps({"pipeline_id": "pipe-aaa", "question": "INT8 的反例呢？"}))
    )
    assert body == {"task_id": "task-r2"}
    assert captured_msgs["pipeline_id"] == "pipe-aaa"
    args = captured["args"]
    assert args["mode"] == "research" and args["task_kind"] == "research_followup"
    assert args["target_id"] == "orchestrator/research_orchestrator_agent"  # 与 start 同键
    assert args["parent_agent_level"] == 1
    assert long_answer[:600] in args["goal_description"]  # 原报告节选
    assert long_answer[:601] not in args["goal_description"]  # 恰 600 字截断
    assert "INT8 的反例呢？" in args["goal_description"]
    assert args["metadata"] == {"source_pipeline_id": "pipe-aaa"}


def test_followup_action_error_paths() -> None:
    module = _load_server()
    # 缺 pipeline_id / 缺 question → 400
    assert _envelope_status(_call(module, f"{EP}/actions/followup", method="POST")) == 400
    assert _envelope_status(
        _call(module, f"{EP}/actions/followup", method="POST",
              raw_body=json.dumps({"pipeline_id": "pipe-aaa"}))
    ) == 400
    # 会话不在本模式 → 400
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=_state_rows()))
    result = _call(
        module, f"{EP}/actions/followup", method="POST",
        raw_body=json.dumps({"pipeline_id": "pipe-bbb", "question": "q"}),
    )
    assert _envelope_status(result) == 400
    assert "不存在" in _body_json(result)["error"]
    # 会话无 assistant 输出 → 409（无可追问物）
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=_state_rows()))
    module._set_provider("messages", lambda pipeline_id, limit=None: asyncio.sleep(0, result=[]))
    result = _call(
        module, f"{EP}/actions/followup", method="POST",
        raw_body=json.dumps({"pipeline_id": "pipe-aaa", "question": "q"}),
    )
    assert _envelope_status(result) == 409


# ── 宿主融合：写动作 session_id 入对话框线程 + 面板主题 token 化 ─────────────────

def test_start_action_passes_session_id_through() -> None:
    """body 带 session_id（宿主 ctx.sync 对话框线程）→ task_submit args 原样透传。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-r9"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(module, f"{EP}/actions/start", method="POST",
              raw_body=json.dumps({"question": "量化方案怎么选", "depth": "quick",
                                   "session_id": "sess-host-7"}))
    )
    assert body == {"task_id": "task-r9"}
    assert captured["args"]["session_id"] == "sess-host-7"


def test_start_action_omits_session_id_when_absent() -> None:
    """无 session_id / 空串（面板未收到 ctx.sync）→ args 不带该键（无则省略）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-r8"}}

    module._set_provider("tool-executor", _fake_invoke)
    for raw in (
        json.dumps({"question": "q", "depth": "quick"}),  # 键缺席
        json.dumps({"question": "q", "depth": "quick", "session_id": ""}),  # 空串同缺席
    ):
        _body_json(
            _call(module, f"{EP}/actions/start", method="POST", raw_body=raw)
        )
        assert "session_id" not in captured["args"]


def test_followup_action_anchors_source_pipeline_thread() -> None:
    """行级动作锚定来源管道（用户裁定 T1）：session_id = 所操作行的 thread_id
    （GUI 操作哪条管道，交互落哪条管道的线程），非 body 透传的当前活跃会话。"""
    module = _load_server()
    rows = [
        {
            "pipeline_id": "pipe-aaa", "thread_id": "th-1", "agent_id": "main",
            "run_status": "completed", "mode": "research",
            "task.goal": "边缘部署下大模型量化方案取舍", "task.status": "completed",
            "message_count": 2,
        },
        {   # 另一行（不同 thread_id）：证明锚的是所操作行，不是任意行
            "pipeline_id": "pipe-r2", "thread_id": "th-9", "agent_id": "main",
            "run_status": "completed", "mode": "research",
            "task.goal": "另一份调研", "task.status": "completed",
            "message_count": 3,
        },
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    long_answer = _BODY + "补充展开。" * 70

    async def _messages_provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        return _messages_with(long_answer)

    module._set_provider("messages", _messages_provider)
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-r7"}}

    module._set_provider("tool-executor", _fake_invoke)

    def _followup(raw: dict) -> dict:
        return _body_json(
            _call(module, f"{EP}/actions/followup", method="POST", raw_body=json.dumps(raw))
        )

    # 操作 pipe-aaa：锚该行 thread_id；body 携带的当前活跃会话值被忽略
    assert _followup({"pipeline_id": "pipe-aaa", "question": "INT8 的反例呢？",
                      "session_id": "sess-active"}) == {"task_id": "task-r7"}
    args = captured["args"]
    assert args["session_id"] == "th-1"
    assert args["session_id"] != "th-9" and args["session_id"] != "sess-active"
    assert args["task_kind"] == "research_followup"
    assert args["metadata"] == {"source_pipeline_id": "pipe-aaa"}

    # 行缺 thread_id：回退 body 显式 session_id；皆缺省省略
    rows[0]["thread_id"] = ""
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    assert _followup({"pipeline_id": "pipe-aaa", "question": "q",
                      "session_id": "sess-fb"}) == {"task_id": "task-r7"}
    assert captured["args"]["session_id"] == "sess-fb"

    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    assert _followup({"pipeline_id": "pipe-aaa", "question": "q"}) == {"task_id": "task-r7"}
    assert "session_id" not in captured["args"]


def test_panel_html_host_fusion_contract() -> None:
    """面板 HTML：宿主下行桥接收器（theme.sync/ctx.sync）+ 主题 token 化（--ag-*）
    + 追问语义「送入对话框追问」（按钮文案落 HTML）。"""
    path = os.path.join(MODES_DIR, "mode_research", "webview", "research_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "theme.sync" in html and "ctx.sync" in html
    assert "--ag-" in html and "__agentosCtx" in html
    assert "送入对话框追问" in html


# ── 路由边界 ─────────────────────────────────────────────────────────────────────

def test_unrouted_paths_return_404() -> None:
    module = _load_server()
    result = _call(module, "/ext/mode_research/page/does-not-exist")
    assert _envelope_status(result) == 404
    result = _call(module, f"{EP}/no_such_data")
    assert _envelope_status(result) == 404
    assert "error" in _body_json(result)


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    path = os.path.join(MODES_DIR, "mode_research", "webview", "research_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
