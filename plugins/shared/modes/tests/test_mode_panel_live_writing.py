# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
# -*- coding: utf-8 -*-
"""写作模式活面板行为测试：作品树投影 / 设定集白名单读 / 章节提取 / 章动作派发。

活面板数据面契约（ADR 2026-09-17-mode-panel-mature-interfaces）：fake provider
注入（module._set_provider）捕获 task_submit args 断言形状，不真派发任务；
设定集读数用 tmp_path 造真实文件（文件面不 mock）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_EP = "/ext/mode_writing/data"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_writing server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_writing", "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_writing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(
    module: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _envelope_status(result: dict[str, Any]) -> int:
    """边界状态断言走 envelope data.status（HttpHandleResponse 契约）。"""
    return int(result["data"]["status"])


def _profile_chain_target() -> str:
    """派发目标期望值从 profile.yaml 真值推导（不硬编码 agent 键）。"""
    with open(os.path.join(MODES_DIR, "mode_writing", "profile.yaml"), encoding="utf-8") as fh:
        profile = yaml.safe_load(fh)
    pool = profile["chain"]["executor_pool"]
    assert isinstance(pool, list) and pool
    return str(pool[0])


def _fake_rows() -> list[dict[str, Any]]:
    return [
        {  # 本模式：ws_meta 为 dict 形态、有 task.id（可继承工作空间）
            "pipeline_id": "pipe-www1", "thread_id": "th-www1", "mode": "writing",
            "task.goal": "长篇《雾都孤灯》第 3 章", "task.status": "running",
            "task.id": "a1b2c3d4e5f6", "task.parent_project_id": "f0e9d8c7b6a5",
            "task.ws_meta": {"mode": "isolated", "path": "/tmp/ws-www1"},
            "message_count": 6, "run_status": "running", "input": "写第三章",
        },
        {  # 本模式：ws_meta 为 JSON 字符串形态、无 task.id（不继承）、无 thread_id
            "pipeline_id": "pipe-www2", "thread_id": "", "mode": "writing",
            "task.goal": "短篇《信号》", "task.status": "completed",
            "task.id": "", "task.parent_project_id": "",
            "task.ws_meta": json.dumps({"mode": "plain", "path": "/tmp/ws-www2"}),
            "message_count": 2, "run_status": "completed", "input": "写短篇",
        },
        {  # 他模式行：必须被过滤
            "pipeline_id": "pipe-ccc", "mode": "coding", "task.goal": "修 bug",
        },
    ]


def _set_state_provider(module: Any) -> None:
    module._set_provider(
        "pipeline-state", lambda: asyncio.sleep(0, result=_fake_rows())
    )


# ── 作品树投影 ──────────────────────────────────────────────────────────────────


def test_works_projection_filters_mode_and_tolerates_ws_meta_forms() -> None:
    module = _load_server()
    _set_state_provider(module)
    works = _body_json(_call(module, f"{_EP}/works"))["works"]
    # 他模式行被过滤；新任务在前（pipeline_id 倒序）
    assert [w["pipeline_id"] for w in works] == ["pipe-www2", "pipe-www1"]
    by_id = {w["pipeline_id"]: w for w in works}
    row1 = by_id["pipe-www1"]
    assert row1["goal"] == "长篇《雾都孤灯》第 3 章"
    assert row1["thread_id"] == "th-www1"  # 行级动作（章动作）会话锚点
    assert row1["task_status"] == "running"
    assert row1["task_id"] == "a1b2c3d4e5f6"
    assert row1["parent_project_id"] == "f0e9d8c7b6a5"
    assert row1["ws_meta"] == {"mode": "isolated", "path": "/tmp/ws-www1"}
    assert row1["message_count"] == 6
    # ws_meta JSON 字符串形态解析为 dict（跨边界两形态容忍）
    assert by_id["pipe-www2"]["ws_meta"] == {"mode": "plain", "path": "/tmp/ws-www2"}
    assert by_id["pipe-www2"]["task_id"] == ""


def test_reads_degrade_to_empty_without_kernel() -> None:
    """provider 未注入（内核握手前）→ 200 空载荷（前端契约不破坏）。"""
    module = _load_server()
    assert _body_json(_call(module, f"{_EP}/sessions")) == {"sessions": []}
    assert _body_json(_call(module, f"{_EP}/works")) == {"works": []}


def test_bootstrap_reports_mode_metadata() -> None:
    module = _load_server()
    body = _body_json(_call(module, f"{_EP}/bootstrap"))
    assert body["mode"] == "writing"
    assert body["name"] == "写作模式"
    assert body["panel_page_id"] == "writing_workshop"
    assert body["chain"]["entry"] == "main"


# ── 设定集（白名单三件读；tmp_path 真实文件面）──────────────────────────────────


def _seed_bible(root: Path) -> None:
    root.joinpath("BIBLE.md").write_text("世界观：雾都，蒸汽与孤灯。", encoding="utf-8")
    root.joinpath("OUTLINE.md").write_text("大纲：第一章 夜航。", encoding="utf-8")
    root.joinpath("LEDGER.md").write_text("台账：主角左利亚，佩灯人。", encoding="utf-8")


def test_bible_reads_whitelisted_trio(tmp_path: Path) -> None:
    module = _load_server()
    _seed_bible(tmp_path)
    tmp_path.joinpath("SECRET.md").write_text("不应被读取", encoding="utf-8")
    tmp_path.parent.joinpath("OUTSIDE.md").write_text("根外文件不可达", encoding="utf-8")
    rows = [
        {
            "pipeline_id": "pipe-www1", "mode": "writing",
            "task.ws_meta": {"mode": "plain", "path": str(tmp_path)},
        }
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    body = _body_json(_call(module, f"{_EP}/bible", query={"pipeline_id": "pipe-www1"}))
    bible = body["bible"]
    assert bible["absent"] == []
    # 白名单硬编码：只读三件，白名单外文件（同目录/根外）都不出现
    assert [s["name"] for s in bible["sections"]] == ["BIBLE.md", "OUTLINE.md", "LEDGER.md"]
    assert bible["sections"][0]["content"] == "世界观：雾都，蒸汽与孤灯。"
    assert bible["sections"][2]["content"] == "台账：主角左利亚，佩灯人。"
    assert bible["root"] == os.path.realpath(str(tmp_path))
    assert "不应被读取" not in json.dumps(body, ensure_ascii=False)
    assert "根外文件不可达" not in json.dumps(body, ensure_ascii=False)


def test_bible_absent_files_reported_honestly(tmp_path: Path) -> None:
    """缺失文件如实进 absent，不拿占位内容冒充。"""
    module = _load_server()
    rows = [
        {
            "pipeline_id": "pipe-www1", "mode": "writing",
            "task.ws_meta": {"mode": "plain", "path": str(tmp_path)},  # 空工作空间
        }
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    body = _body_json(_call(module, f"{_EP}/bible", query={"pipeline_id": "pipe-www1"}))
    bible = body["bible"]
    assert bible["sections"] == []
    assert bible["absent"] == ["BIBLE.md", "OUTLINE.md", "LEDGER.md"]


def test_bible_root_traversal_normalized_within_workspace(tmp_path: Path) -> None:
    """ws_meta.path 带穿越段时归一回真实工作空间根——可读的仍只有根内白名单三件。"""
    module = _load_server()
    _seed_bible(tmp_path)
    tmp_path.joinpath("deep").mkdir()
    traversed = os.path.join(str(tmp_path), "deep", os.pardir, os.pardir, tmp_path.name)
    assert os.path.realpath(traversed) == os.path.realpath(str(tmp_path))

    rows = [
        {
            "pipeline_id": "pipe-www1", "mode": "writing",
            "task.ws_meta": {"mode": "plain", "path": traversed},
        }
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    body = _body_json(_call(module, f"{_EP}/bible", query={"pipeline_id": "pipe-www1"}))
    bible = body["bible"]
    assert bible["absent"] == []
    assert [s["name"] for s in bible["sections"]] == ["BIBLE.md", "OUTLINE.md", "LEDGER.md"]
    assert bible["sections"][1]["content"] == "大纲：第一章 夜航。"


def test_bible_without_ws_meta_degrades_to_empty() -> None:
    """行内无工作空间坐标（缺键/坏 JSON 串）→ 诚实空设定集（三件全 absent）。"""
    module = _load_server()
    rows = [
        {"pipeline_id": "pipe-www1", "mode": "writing"},
        {"pipeline_id": "pipe-www2", "mode": "writing", "task.ws_meta": "{not-json"},
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    for pipeline_id in ("pipe-www1", "pipe-www2"):
        body = _body_json(
            _call(module, f"{_EP}/bible", query={"pipeline_id": pipeline_id})
        )
        assert body["bible"] == {
            "root": "", "sections": [], "absent": ["BIBLE.md", "OUTLINE.md", "LEDGER.md"],
        }
    # 未知 pipeline_id 同口径：诚实空，不报错（读面降级）
    body = _body_json(_call(module, f"{_EP}/bible", query={"pipeline_id": "pipe-none"}))
    assert body["bible"]["sections"] == []


# ── 章节正文提取（≥2 组区分度输入）───────────────────────────────────────────────


def _long_prose(module: Any) -> str:
    return "雨打在铁皮屋檐上，" * (module._CHAPTER_MIN_CHARS * 2 // 9 + 1)


def test_chapter_extracts_last_long_assistant_message() -> None:
    module = _load_server()
    long_prose = _long_prose(module)
    assert len(long_prose) >= module._CHAPTER_MIN_CHARS
    messages = [
        {"role": "user", "content_preview": "写第三章", "status": "success", "created_at": "t1"},
        {"role": "assistant", "content_preview": long_prose, "status": "success", "created_at": "t2"},
        {  # 长 user 消息不得被当作章节正文
            "role": "user", "content_preview": long_prose, "status": "success", "created_at": "t3",
        },
        {"role": "assistant", "content_preview": "（未完待续）", "status": "success", "created_at": "t4"},
    ]

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        assert pipeline_id == "pipe-www1"
        return messages

    module._set_provider("messages", _provider)
    body = _body_json(_call(module, f"{_EP}/chapter", query={"pipeline_id": "pipe-www1"}))
    chapter = body["chapter"]
    assert chapter["pipeline_id"] == "pipe-www1"
    assert chapter["content"] == long_prose  # 最后一条达标的 assistant，短尾不顶替
    assert chapter["char_count"] == len(long_prose)


def test_chapter_empty_paths() -> None:
    """无消息 / 只有短回复 → 空正文如实返回（字数 0），不拿短消息凑数。"""
    module = _load_server()
    cases: list[list[dict[str, Any]]] = [
        [],
        [
            {"role": "user", "content_preview": "在吗", "status": "success", "created_at": "t1"},
            {"role": "assistant", "content_preview": "（未完待续）", "status": "success", "created_at": "t2"},
        ],
    ]
    for messages in cases:  # ≥2 组区分度输入同契约
        async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict[str, Any]]:
            return messages

        module._set_provider("messages", _provider)
        body = _body_json(_call(module, f"{_EP}/chapter", query={"pipeline_id": "pipe-www1"}))
        assert body["chapter"] == {"pipeline_id": "pipe-www1", "content": "", "char_count": 0}


# ── 查询缺参 / 未路由 ────────────────────────────────────────────────────────────


def test_query_endpoints_require_pipeline_id() -> None:
    module = _load_server()
    for suffix in ("/messages", "/chapter", "/bible"):
        assert _envelope_status(_call(module, f"{_EP}{suffix}")) == 400
    # 数据端点错误 method → 404
    assert _envelope_status(_call(module, f"{_EP}/works", method="POST")) == 404


def test_unrouted_path_returns_404() -> None:
    module = _load_server()
    result = _call(module, "/ext/mode_writing/data/does-not-exist")
    assert _envelope_status(result) == 404
    assert "error" in _body_json(result)


# ── 章动作写动作（args 捕获断形状，不真派发）─────────────────────────────────────


def test_chain_target_fallbacks() -> None:
    """派发目标解析：执行者池缺席退链尾，chain 缺失退 main（纯函数逐档验证）。"""
    module = _load_server()
    assert module._chain_target({"chain": {"executor_pool": ["a/x", "b/y"]}}) == "a/x"
    assert module._chain_target({"chain": {"expected_path": ["main", "orchestrator/x"]}}) == (
        "orchestrator/x"
    )
    assert module._chain_target({}) == "main"


def test_chapter_act_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict[str, Any] = {}

    async def _fake_invoke(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"data": {"task_id": "task-b64"}}

    module._set_provider("tool-executor", _fake_invoke)
    raw_b64 = base64.b64encode(
        json.dumps({"act": "expand", "instruction": "内核 base64 形态的扩写指令"}).encode("utf-8")
    ).decode("ascii")
    body = _body_json(
        _call(module, f"{_EP}/actions/chapter_act", method="POST", raw_body=raw_b64)
    )
    assert body == {"task_id": "task-b64"}
    args = captured["args"]
    assert args["task_kind"] == "writing_expand"
    assert "内核 base64 形态的扩写指令" in args["goal_description"]

    # 裸 JSON 形态（第二组区分度输入）
    captured.clear()
    _body_json(
        _call(
            module, f"{_EP}/actions/chapter_act", method="POST",
            raw_body=json.dumps({"act": "continue", "instruction": "裸 JSON 形态的续写指令"}),
        )
    )
    args = captured["args"]
    assert args["task_kind"] == "writing_continue"
    assert "裸 JSON 形态的续写指令" in args["goal_description"]


def test_chapter_act_dispatches_task_submit() -> None:
    module = _load_server()
    captured: dict[str, Any] = {}

    async def _fake_invoke(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"data": {"task_id": "task-xyz"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/chapter_act", method="POST",
            raw_body=json.dumps({"act": "continue", "instruction": "加强雾的描写"}),
        )
    )
    assert body == {"task_id": "task-xyz"}
    assert captured["tool_name"] == "task_submit"
    assert captured["plugin_id"] == "task_submit_tool"
    args = captured["args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == _profile_chain_target()  # profile.chain 执行者池首选
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "writing"
    assert args["task_kind"] == "writing_continue"
    assert "加强雾的描写" in args["goal_description"]
    assert "续写" in args["goal_title"]
    assert len(args["goal_description"]) <= 2000
    # 未带 pipeline_id：无继承键（不虚构继承源）
    assert "inherit_mode" not in args
    assert "inherit_from" not in args


def test_chapter_act_inherits_workspace_only_with_task_id() -> None:
    """带 pipeline_id：行内有 task.id 才带继承扁平键；无 task.id 仍可派发。"""
    module = _load_server()
    _set_state_provider(module)
    captured: dict[str, Any] = {}

    async def _fake_invoke(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"data": {"task_id": "task-out"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/chapter_act", method="POST",
            raw_body=json.dumps({"act": "outline", "pipeline_id": "pipe-www1"}),
        )
    )
    assert body == {"task_id": "task-out"}
    args = captured["args"]
    # 工作空间继承（扁平键，task_submit _parse_inherit_modes 口径）：续写任务看到作品文件
    assert args["inherit_mode"] == "workspace"
    assert args["inherit_from"] == "a1b2c3d4e5f6"
    assert args["task_kind"] == "writing_outline"
    assert args["metadata"] == {"source_pipeline_id": "pipe-www1"}

    body2 = _body_json(
        _call(
            module, f"{_EP}/actions/chapter_act", method="POST",
            raw_body=json.dumps({"act": "brainstorm", "pipeline_id": "pipe-www2"}),
        )
    )
    assert body2 == {"task_id": "task-out"}
    args2 = captured["args"]
    assert args2["task_kind"] == "writing_brainstorm"
    assert "inherit_mode" not in args2  # 行内无 task.id：不虚构继承键
    assert args2["metadata"] == {"source_pipeline_id": "pipe-www2"}


def test_chapter_act_error_paths() -> None:
    module = _load_server()
    _set_state_provider(module)
    # 缺 act → 400
    assert _envelope_status(_call(module, f"{_EP}/actions/chapter_act", method="POST")) == 400
    # 未知 act → 400
    result = _call(
        module, f"{_EP}/actions/chapter_act", method="POST",
        raw_body=json.dumps({"act": "polish"}),
    )
    assert _envelope_status(result) == 400
    assert "未知 act" in _body_json(result)["error"]
    # 未知 pipeline_id → 400（写面必须校验，不派孤儿任务）
    result = _call(
        module, f"{_EP}/actions/chapter_act", method="POST",
        raw_body=json.dumps({"act": "continue", "pipeline_id": "pipe-none"}),
    )
    assert _envelope_status(result) == 400
    assert "不存在" in _body_json(result)["error"]
    # GET 打写动作端点 → 404
    assert _envelope_status(_call(module, f"{_EP}/actions/chapter_act")) == 404


def test_chapter_act_channel_missing_reports_error() -> None:
    """tool-executor 未注入 → 显式 error（不静默假成功）。"""
    module = _load_server()
    body = _body_json(
        _call(
            module, f"{_EP}/actions/chapter_act", method="POST",
            raw_body=json.dumps({"act": "continue"}),
        )
    )
    assert "error" in body
    assert "tool-executor" in body["error"]


# ── 宿主融合（v1.2.0）：章动作 session_id 送入对话框线程 + 面板主题 token 化 ────────

# 下行桥协议 theme.sync 白名单 token（宿主 → 面板 postMessage params.tokens）
HOST_THEME_TOKENS = (
    "--ag-bg", "--ag-fg", "--ag-muted", "--ag-border", "--ag-card", "--ag-accent",
    "--ag-accent-soft", "--ag-chip", "--ag-ok", "--ag-warn", "--ag-err", "--ag-radius",
)


def _panel_html() -> str:
    path = os.path.join(MODES_DIR, "mode_writing", "webview", "writing_panel.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_panel_html_declares_host_bridge_receivers() -> None:
    """面板页带下行桥接收器：theme.sync → token 注入；ctx.sync → __agentosCtx。"""
    html = _panel_html()
    lowered = html.lower()
    # 两个接收器方法 + 落点（documentElement token 注入 / __agentosCtx 会话上下文）
    assert "theme.sync" in lowered and "ctx.sync" in lowered
    assert "documentelement.style.setproperty" in lowered
    assert "__agentosctx" in lowered
    # 协议白名单 token 全量出现，且以 var(--ag-*, 现值 fallback) 形态消费
    for token in HOST_THEME_TOKENS:
        assert token in html, f"面板缺宿主主题 token {token}"
    assert "var(--ag-bg," in html and "var(--ag-accent," in html
    # 章动作成功口径改为「送入对话框」；章动作=行级操作只发 pipeline_id，
    # 会话归属由后端按行 thread_id 解析（body 不再带 session_id）
    assert "已送入对话框生成" in html
    assert "payload = { act: act, instruction: instruction, pipeline_id: state.activeWork }" in html
    assert "payload.session_id" not in html


def test_chapter_act_anchors_source_pipeline_thread() -> None:
    """行级动作锚定来源管道（用户裁定 T1）：带 pipeline_id 时 session_id = 该行
    thread_id（非 body 透传的当前活跃会话）；行缺 thread_id 回退 body 显式值；
    无 pipeline_id（新建动作）保持 body 值透传。"""
    module = _load_server()
    _set_state_provider(module)
    captured: dict[str, Any] = {}

    async def _fake_invoke(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"data": {"task_id": "task-sess"}}

    module._set_provider("tool-executor", _fake_invoke)

    def _act(raw: dict[str, Any]) -> dict[str, Any]:
        return _body_json(
            _call(module, f"{_EP}/actions/chapter_act", method="POST", raw_body=json.dumps(raw))
        )

    # 行锚定：操作 pipe-www1 → th-www1；body 携带的当前活跃会话值被忽略
    assert _act({"act": "rewrite", "pipeline_id": "pipe-www1", "session_id": "sess-active"}) == {
        "task_id": "task-sess"
    }
    args = captured["args"]
    assert args["session_id"] == "th-www1"
    assert args["session_id"] != "sess-active"
    # 与工作空间继承键共存（互不挤占）
    assert args["inherit_mode"] == "workspace"
    assert args["inherit_from"] == "a1b2c3d4e5f6"

    # 行缺 thread_id：回退 body 显式 session_id；皆缺省省略
    captured.clear()
    assert _act({"act": "brainstorm", "pipeline_id": "pipe-www2", "session_id": "sess-fb"}) == {
        "task_id": "task-sess"
    }
    assert captured["args"]["session_id"] == "sess-fb"

    captured.clear()
    assert _act({"act": "outline", "pipeline_id": "pipe-www2"}) == {"task_id": "task-sess"}
    assert "session_id" not in captured["args"]

    # 新建动作（无 pipeline_id）：body 值透传（面板当前活跃会话）
    captured.clear()
    assert _act({"act": "continue", "session_id": "sess-new"}) == {"task_id": "task-sess"}
    args = captured["args"]
    assert args["session_id"] == "sess-new"
    assert args["task_kind"] == "writing_continue"


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    html = _panel_html()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
