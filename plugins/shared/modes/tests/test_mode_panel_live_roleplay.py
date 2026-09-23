# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
# -*- coding: utf-8 -*-
"""角色扮演模式活面板行为测试（宿主融合形态，2026-09-17 三裁定 + BUG-73 修复契约）：

- 对话回归对话框：开演产出经「开演记录」详情回显（/data/messages 消费面恢复，
  只读渲染 assistant 扮演轮次）——任务管道不再锚宿主活跃会话，聊天区看不到的
  演出在面板可见。
- BUG-73（开演断链三件套）契约：
  ① 开演锚**卡专属扮演会话**（thread-rp-<card_id>，服务端派生，body 的宿主
    活跃 session_id 一律忽略）——R92「任务落活跃既有 thread」根除；
  ② 所选开场白全文随派发注入 agent 上下文；
  ③ 面板可见反馈：派发成功 toast 带任务号 + 自动切「开演记录」页签 + 立即与
    延迟各刷新一次（新记录行等管道写入 mode 键后才可见）。
- 卡 theme/avatar：/data/cards 透出，theme 为 ThemeConfig 档（必备键断言对照
  frontend/src/types/theme.ts 的 ThemeConfig/ThemeColors/BackgroundsConfig 类型定义
  手写，与 theme.apply 桥 validateThemeConfig 的 fail-closed 校验同口径）。
- 面板主题跟宿主：HTML 内联 theme.sync 接收器 + CSS var(--ag-*) token 桥；
  ctx.sync 宿主上下文消费面移除（开演不再读宿主活跃会话，防回退断言在案）。

fake provider 注入（module._set_provider）捕获 task_submit args 断言形状，不真派发。
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

_EP = "/ext/mode_roleplay/data"
_PANEL_HTML = os.path.join(MODES_DIR, "mode_roleplay", "webview", "roleplay_panel.html")

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_roleplay", path)
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


def _panel_html() -> str:
    with open(_PANEL_HTML, encoding="utf-8") as fh:
        return fh.read()


# ── 卡 theme/avatar 字段（ThemeConfig 必备键，对照 frontend/src/types/theme.ts）────


def test_cards_expose_theme_and_avatar() -> None:
    module = _load_server()
    cards = _body_json(_call(module, f"{_EP}/cards"))["cards"]
    assert len(cards) >= 3
    for card in cards:
        assert card["avatar"], f"{card['id']}: 缺 avatar"
        theme = card["theme"]
        assert isinstance(theme, dict), f"{card['id']}: 缺 theme 段"
        # ThemeConfig 四支柱 + id/name（validateThemeConfig fail-closed 同口径）
        assert isinstance(theme["id"], str) and theme["id"]
        assert isinstance(theme["name"], str) and theme["name"]
        for pillar in ("colors", "components", "effects", "backgrounds"):
            assert isinstance(theme[pillar], dict), f"{card['id']}: 缺支柱 {pillar}"
        colors = theme["colors"]
        # ThemeColors：基础三色 + 背景/文字/边框/状态/气泡五域
        for key in ("primary", "secondary", "accent"):
            assert isinstance(colors[key], str) and colors[key], f"{card['id']}: colors.{key}"
        assert set(colors["background"]) == {"main", "card", "sidebar", "input", "elevated"}
        assert set(colors["text"]) == {"primary", "secondary", "muted", "disabled"}
        assert set(colors["border"]) == {"default", "hover", "active"}
        assert set(colors["status"]) >= {"success", "warning", "error", "info", "running", "pending"}
        # BubbleColors（聊天区气泡消费，缺席会让宿主 apply 崩——必备）
        assert {"user_bg", "user_text", "ai_bg", "ai_text"} <= set(colors["bubble"])
        # BackgroundsConfig：main 必备 + chat 聊天背景语义字段
        assert {"main", "chat"} <= set(theme["backgrounds"])
        chat_bg = theme["backgrounds"]["chat"]
        assert chat_bg["type"] in ("solid", "gradient", "image") and chat_bg["value"]
        # effects 四键（EffectsConfig）
        assert {"glassmorphism", "animations", "transitionDuration", "transitionEasing"} == set(theme["effects"])
    # 三卡 theme 档互异（有区分度：冷紫夜色 / 霓虹暗青 / 暖橙昼色）
    theme_ids = [c["theme"]["id"] for c in cards]
    assert len(set(theme_ids)) == len(theme_ids)
    categories = {c["theme"].get("category") for c in cards}
    assert "dark" in categories and "light" in categories


def test_card_system_prompt_has_no_raw_placeholders() -> None:
    """BUG-73 断点b 伴生缺陷：卡 system_prompt 的 {description}/{personality}/
    {scenario} 占位符无任何替换机制（直进 LLM=字面量），出厂卡必须内联实际值。
    比对按空白折叠（YAML 多行折叠标量会在行间插空格，语义等价）。"""
    import re as _re

    import yaml as _yaml

    def _norm(text: str) -> str:
        return _re.sub(r"\s+", "", text)

    agents_dir = os.path.join(MODES_DIR, "mode_roleplay", "agents")
    for fname in sorted(os.listdir(agents_dir)):
        if not (fname.startswith("card_") and fname.endswith(".yaml")):
            continue
        with open(os.path.join(agents_dir, fname), encoding="utf-8") as fh:
            data = _yaml.safe_load(fh) or {}
        prompt = _norm(str(data.get("system_prompt") or ""))
        assert "{description}" not in prompt, fname
        assert "{personality}" not in prompt, fname
        assert "{scenario}" not in prompt, fname
        # 实际设定值必须真实内联（不是空占位）
        assert _norm(str(data.get("description") or "")) in prompt, fname
        assert _norm(str(data.get("personality") or "")) in prompt, fname
        assert _norm(str(data.get("scenario") or "")) in prompt, fname


def _validate_theme_config_like_host(theme: Any) -> list[str]:
    """手写 validateThemeConfig 同构校验（frontend/src/services/themeService.ts
    的 fail-closed 必填键矩阵）：id/name + colors 六字段 + components/effects/
    backgrounds 三支柱。另加 colors.bubble 四键——compileThemeVariables 对其
    无条件取键（pushCoreColorVars），缺席会让宿主 apply 链崩，比 TS 校验更严
    的宿主同口径（对齐 types/theme.ts ThemeColors.bubble 必备键）。"""
    errors: list[str] = []
    if not isinstance(theme, dict):
        return ["配置不是对象"]
    if not theme.get("id") or not isinstance(theme.get("id"), str):
        errors.append("缺少或无效的 id 字段")
    if not theme.get("name") or not isinstance(theme.get("name"), str):
        errors.append("缺少或无效的 name 字段")
    colors = theme.get("colors")
    if not isinstance(colors, dict):
        errors.append("缺少或无效的 colors 字段")
    else:
        for field in ("primary", "secondary", "accent", "background", "text", "border"):
            if field not in colors:
                errors.append(f"缺少必需的颜色字段: {field}")
        bubble = colors.get("bubble")
        if not isinstance(bubble, dict):
            errors.append("缺少或无效的 colors.bubble 字段")
        else:
            for field in ("user_bg", "user_text", "ai_bg", "ai_text"):
                if not bubble.get(field):
                    errors.append(f"缺少必需的气泡字段: bubble.{field}")
    for pillar in ("components", "effects", "backgrounds"):
        if not isinstance(theme.get(pillar), dict):
            errors.append(f"缺少或无效的 {pillar} 字段")
    return errors


def test_cards_theme_passes_host_validate_theme_config_isomorphically() -> None:
    """三卡 theme 段过宿主 validateThemeConfig 同构校验（关键必填键手写，对齐
    themeService.ts / types/theme.ts）——缺键 = 宿主 fail-closed 静默丢弃
    （「选卡切主题零生效」的成因候选），此闸保证出厂卡载荷永远合法可入栈。"""
    module = _load_server()
    cards = _body_json(_call(module, f"{_EP}/cards"))["cards"]
    assert len(cards) >= 3
    for card in cards:
        assert _validate_theme_config_like_host(card["theme"]) == [], card["id"]
    # 校验器有牙（防恒真刷绿）：残缺载荷必须被逐项检出
    assert _validate_theme_config_like_host({"id": "x", "name": "缺四支柱档"}) != []
    assert _validate_theme_config_like_host("not-an-object") == ["配置不是对象"]
    assert _validate_theme_config_like_host(
        {"id": "y", "name": "缺气泡档", "colors": {}, "components": {},
         "effects": {}, "backgrounds": {}}
    ) != []


# ── 面板 HTML 宿主融合契约 ────────────────────────────────────────────────────────


def test_panel_html_theme_sync_receiver_and_host_tokens() -> None:
    """宿主主题 token 桥：theme.sync 接收器 + --ag-* 变量引用。
    ctx.sync 宿主会话上下文消费面移除（BUG-73 断点a 根除：开演不再读宿主活跃
    会话——防回退断言 __agentosctx 不得回归）。"""
    html = _panel_html().lower()
    assert "theme.sync" in html
    assert "__agentosctx" not in html
    assert "ctx.sync" not in html
    assert "--ag-" in html  # var(--ag-*, fallback) 桥接形式
    assert "setproperty" in html  # token 逐键落根元素
    # 面板主题跟宿主：prefers-color-scheme 仅作 fallback 基座
    assert "prefers-color-scheme" in html


def test_panel_html_dedicated_session_play_flow() -> None:
    """BUG-73 断点a/c 面板契约：开演不透传宿主会话锚（专属会话由服务端派生），
    派发成功可见反馈=toast 带任务号 + 自动切「开演记录」页签 + 立即与延迟刷新。"""
    html = _panel_html()
    lowered = html.lower()
    # 三页签布局保持：角色卡 / 世界书 / 开演记录
    assert "角色卡" in html and "世界书" in html and "开演记录" in html
    # 宿主会话锚不再出现在任何上行 body（开演/重新生成均不透传）
    assert "session_id" not in lowered
    assert "currentsessionid" not in lowered
    # 派发成功反馈三件套：toast 带任务号/去向、切开演记录页签、延迟补刷新
    assert "已开演" in html and "开演记录" in html
    assert "switchtab('sessions')" in lowered
    assert "settimeout" in lowered  # 新记录行需等管道写 mode 键，延迟补刷新
    # 派发中状态反馈保留
    assert "派发中" in html


def test_panel_html_record_detail_renders_performance() -> None:
    """BUG-73 断点b 可见面：开演记录详情消费 /data/messages 只读回显 assistant
    扮演轮次（此前产出落在无 UI 渲染的任务管道里=「无扮演产出」的观测成因）。"""
    html = _panel_html()
    lowered = html.lower()
    # 消息端点消费恢复（按 pipeline_id 查询）
    assert "/data/messages?pipeline_id=" in lowered
    # assistant 轮次渲染 + 空态诚实（演出未落时提示稍后刷新）
    assert "开场表演" in html
    assert "演出尚未落" in html


# ── 开演/重新生成的会话锚（args 捕获断形状）─────────────────────────────────────


def _fake_invoke_capturing(captured: dict[str, Any], task_id: str) -> Any:
    async def _fake_invoke(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"data": {"task_id": task_id}}

    return _fake_invoke


def test_play_action_anchors_dedicated_card_thread() -> None:
    """BUG-73 断点a：开演锚卡专属扮演会话（thread-rp-<card_id>），body 携带的
    宿主活跃 session_id 一律忽略（缺席/空/活跃会话 id 三形态同裁决）。"""
    module = _load_server()
    captured: dict[str, Any] = {}
    module._set_provider("tool-executor", _fake_invoke_capturing(captured, "task-s1"))

    def _play(body_extra: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"card_id": "card_luna", "user_persona": "北地佣兵"}
        body.update(body_extra)
        return _body_json(
            _call(module, f"{_EP}/actions/play", method="POST", raw_body=json.dumps(body))
        )

    # body 带「宿主当前活跃会话」：忽略，锚卡专属会话（R92 同族污染根除）
    assert _play({"session_id": "thread-858ab1bd-bf13-4d59-a7c7-e39b589f158b"}) == {
        "task_id": "task-s1"
    }
    args = captured["args"]
    assert args["session_id"] == "thread-rp-card_luna"
    assert args["target_id"] == "mode_roleplay/card_luna" and args["mode"] == "roleplay"
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1

    # 无 session_id / 空 session_id：同一专属锚（不虚构宿主归属）
    assert _play({}) == {"task_id": "task-s1"}
    assert captured["args"]["session_id"] == "thread-rp-card_luna"
    assert _play({"session_id": ""}) == {"task_id": "task-s1"}
    assert captured["args"]["session_id"] == "thread-rp-card_luna"

    # 不同卡 → 各自专属锚（同卡复演=复用同一扮演会话）
    captured.clear()
    assert _play({"card_id": "card_kael"}) == {"task_id": "task-s1"}
    assert captured["args"]["session_id"] == "thread-rp-card_kael"


def test_play_action_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict[str, Any] = {}
    module._set_provider("tool-executor", _fake_invoke_capturing(captured, "task-b64"))

    # 内核形态：base64(json)
    raw_b64 = base64.b64encode(
        json.dumps({"card_id": "card_kael", "user_persona": "银币商人"}).encode("utf-8")
    ).decode("ascii")
    assert _body_json(_call(module, f"{_EP}/actions/play", method="POST", raw_body=raw_b64)) == {
        "task_id": "task-b64"
    }
    args = captured["args"]
    assert args["target_id"] == "mode_roleplay/card_kael"
    assert "银币商人" in args["goal_description"]

    # 裸 JSON 形态（第二组区分度输入）：另一张卡解析结果不同
    captured.clear()
    assert _body_json(
        _call(
            module, f"{_EP}/actions/play", method="POST",
            raw_body=json.dumps({"card_id": "card_mira", "user_persona": "星见高中转校生"}),
        )
    ) == {"task_id": "task-b64"}
    args = captured["args"]
    assert args["target_id"] == "mode_roleplay/card_mira"
    assert "星见高中转校生" in args["goal_description"]


def test_regenerate_action_anchors_source_pipeline_thread() -> None:
    """行级动作锚定来源管道：session_id = 所操作行的 thread_id（用户裁定 T1——
    GUI 操作哪条管道，交互落哪条管道的线程），非 body 透传的当前活跃会话。"""
    module = _load_server()
    rows = [
        {
            "pipeline_id": "pipe-r1", "thread_id": "th-1", "agent_id": "mode_roleplay/card_kael",
            "run_status": "completed", "mode": "roleplay", "task.goal": "与凯尔谈委托",
            "task.status": "completed", "message_count": 2,
        },
        {   # 另一行（不同 thread_id）：证明锚的是所操作行，不是任意行
            "pipeline_id": "pipe-r2", "thread_id": "th-2", "agent_id": "mode_roleplay/card_luna",
            "run_status": "completed", "mode": "roleplay", "task.goal": "月语神殿夜谈",
            "task.status": "completed", "message_count": 4,
        },
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))

    async def _messages_provider(pipeline_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content_preview": "这单有风险", "status": "success", "created_at": "t1"},
            {"role": "assistant", "content_preview": "「说数。」", "status": "success", "created_at": "t2"},
        ]

    module._set_provider("messages", _messages_provider)
    captured: dict[str, Any] = {}
    module._set_provider("tool-executor", _fake_invoke_capturing(captured, "task-r1"))

    def _regen(body_extra: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"pipeline_id": "pipe-r1", **body_extra}
        return _body_json(
            _call(module, f"{_EP}/actions/regenerate", method="POST", raw_body=json.dumps(body))
        )

    # 操作 pipe-r1：锚该行 thread_id；body 携带的当前活跃会话值被忽略
    assert _regen({"session_id": "sess-current-active"}) == {"task_id": "task-r1"}
    args = captured["args"]
    assert args["session_id"] == "th-1"
    assert args["session_id"] != "th-2" and args["session_id"] != "sess-current-active"
    assert args["task_kind"] == "roleplay_regenerate"
    assert args["metadata"] == {"source_pipeline_id": "pipe-r1"}
    # parent_agent_level=1 依据同 play（面板 = 主 agent 身份代用户派发）
    assert args["parent_agent_level"] == 1

    # 行缺 thread_id：回退 body 显式 session_id；皆缺省省略
    rows[0]["thread_id"] = ""
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    assert _regen({"session_id": "sess-fallback"}) == {"task_id": "task-r1"}
    assert captured["args"]["session_id"] == "sess-fallback"

    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    assert _regen({}) == {"task_id": "task-r1"}
    assert "session_id" not in captured["args"]


# ── /data/messages 端点（BUG-73 后恢复消费：开演记录详情回显扮演轮次）──────────────


def test_messages_endpoint_still_served() -> None:
    module = _load_server()
    # 缺 pipeline_id → 400；错误 method → 404（端点契约保持）
    assert _envelope_status(_call(module, f"{_EP}/messages")) == 400
    assert _envelope_status(_call(module, f"{_EP}/messages", method="POST")) == 404
    # 正常查询仍返回归一消息
    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return [{"role": "assistant", "content_preview": "「说数。」", "status": "success", "created_at": "t2"}]

    module._set_provider("messages", _provider)
    body = _body_json(_call(module, f"{_EP}/messages", query={"pipeline_id": "pipe-r1"}))
    assert body["messages"][0]["content"] == "「说数。」"


def test_sessions_rows_carry_created_at() -> None:
    """开演记录摘要卡时间源：task.created_at 出生键透传；缺席如实空。"""
    module = _load_server()
    rows = [
        {
            "pipeline_id": "pipe-t1", "mode": "roleplay", "task.goal": "夜谈",
            "task.created_at": "2026-09-17T10:00:00+00:00",
        },
        {"pipeline_id": "pipe-t2", "mode": "roleplay", "task.goal": "无时间戳"},
    ]
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))
    sessions = _body_json(_call(module, f"{_EP}/sessions"))["sessions"]
    by_id = {s["pipeline_id"]: s for s in sessions}
    assert by_id["pipe-t1"]["created_at"] == "2026-09-17T10:00:00+00:00"
    assert by_id["pipe-t2"]["created_at"] == ""


def test_panel_html_theme_apply_feedback_toasts() -> None:
    """theme.apply result/error 下行各 toast：宿主 fail-closed 丢弃（无活跃会话/
    载荷不合法）不再静默——丢弃原因上屏是「选卡切主题零生效」排障的第一手证据。"""
    html = _panel_html()
    lowered = html.lower()
    # 选卡即上行（openCard → applyCardTheme，非开演按钮才调）且带 id 配对等待回执
    assert "function applycardtheme(" in lowered
    assert "agentosfetch('theme.apply'" in lowered
    # result/error 各 toast 反馈
    assert "对话框主题已随" in html
    assert "随卡主题切换失败" in html
    assert "unwraperrormessage" in lowered


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    html = _panel_html()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
