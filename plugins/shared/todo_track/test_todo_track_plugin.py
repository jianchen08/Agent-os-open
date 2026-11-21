# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage

"""todo_track 插件行为测试——fold 注入契约 / extract 提取契约 / latest 载荷。

断行为（输入→输出/副作用），不碰内部实现细节；todo_text/dynamic_vars 守卫
成组给区分度输入。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
for _d in [str(_DIR)]:
    if str(_d) not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin() -> Any:
    """按显式路径加载 plugin.py（裸名 plugin 全车道共跑会被劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "todo_track_plugin_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "plugin.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mod() -> Any:
    return _load_plugin()


def _plugin() -> Any:
    return _mod().TodoTrackPlugin()


def _assistant_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


async def test_instructions_merged_into_dynamic_vars() -> None:
    # 缓存契约（2026-10-23）：说明常量并入 dynamic_vars，系统提示词零触碰
    plugin = _plugin()
    updates = await plugin.fold(
        {
            "context.system_prompt": "你是一个助手。",
            "prompt.dynamic_vars": {"role": "user", "content": "动态变量基座"},
        }
    )
    assert "context.system_prompt" not in updates
    merged = updates["prompt.dynamic_vars"]
    assert merged["role"] == "user"
    assert merged["content"].startswith("动态变量基座")
    assert _mod().TODO_INSTRUCTIONS in merged["content"]


async def test_instructions_idempotent_no_duplicate() -> None:
    # fold 每轮运行：说明已在场不重复追加（幂等契约）
    plugin = _plugin()
    dvm = {"role": "user", "content": "基座\n\n" + _mod().TODO_INSTRUCTIONS}
    updates = await plugin.fold({"prompt.dynamic_vars": dvm})
    content = updates["prompt.dynamic_vars"]["content"]
    assert content.count(_mod().TODO_INSTRUCTIONS) == 1


async def test_instructions_then_todo_text_order() -> None:
    # 基座 → 说明常量 → 当前清单（未完成项），三段有序拼接
    plugin = _plugin()
    todo_text = _mod()._TODO_HEADER + "\n- [ ] a"
    dvm = {"role": "user", "content": "动态变量基座"}
    updates = await plugin.fold(
        {"context.todo_text": todo_text, "prompt.dynamic_vars": dvm}
    )
    content = updates["prompt.dynamic_vars"]["content"]
    base = content.find("动态变量基座")
    ins = content.find(_mod().TODO_INSTRUCTIONS)
    todo = content.find(todo_text)
    assert -1 < base < ins < todo


def test_injection_header_matches_incremental_protocol() -> None:
    # 块头指令契约（1.2.28 修）：「变更时整表重发」是整表替换时代遗留——
    # 贴数据、每轮出现的指令面赢，模型逐轮重发整表，措辞漂移绕过新增
    # 精确查重 → 近重复条目累积；块头必须与 2026-10-24 增量协议同口径。
    header = _mod()._TODO_HEADER
    assert "整表重发" not in header, "块头不得教整表重发（2026-10-24 增量协议裁定）"
    assert "无需重发" in header, "块头须明示清单已随每轮注入、无需重写"
    assert "- [ ]" not in header, (
        "块头含字面 checkbox 语法会破 _pending_lines「全完成清单块不注入」判定"
    )
    assert "- [x]" not in header, (
        "块头含字面 checkbox 语法会破 _pending_lines「全完成清单块不注入」判定"
    )


async def test_completed_items_filtered_from_injection() -> None:
    # 注入面只重申剩余工作（2026-10-24 用户裁定）：[x] 行剔除、[ ] 行保留、
    # header 维持——已完成阶段回放会把模型锚死在旧阶段（G2 循环事故）
    plugin = _plugin()
    todo_text = (
        _mod()._TODO_HEADER + "\n"
        "- [x] 1. 调研\n"
        "- [x] 2. 契约核对\n"
        "- [ ] 3. 写失败测试\n"
        "- [ ] 4. 实现接线\n"
    )
    updates = await plugin.fold(
        {"context.todo_text": todo_text, "prompt.dynamic_vars": {"role": "user", "content": "基座"}}
    )
    content = updates["prompt.dynamic_vars"]["content"]
    assert "- [ ] 3. 写失败测试" in content
    assert "- [ ] 4. 实现接线" in content
    # 断言域限定在清单块（TODO_INSTRUCTIONS 约定文本合法含 `- [x] ` 字样）
    todo_block = content[content.find("当前任务清单") :]
    assert "- [x]" not in todo_block, "已完成项不得出现在注入清单块"
    assert "已完成 2 项" in todo_block, "已完成以计数行注入（进度可感、零内容回放）"


def _budget_todo_text(n: int) -> str:
    lines = [f"- [ ] 任务{i:02d}" for i in range(n)]
    return _mod()._TODO_HEADER + "\n" + "\n".join(lines)


async def _fold_todo_block(todo_text: str) -> str:
    plugin = _plugin()
    updates = await plugin.fold(
        {"context.todo_text": todo_text, "prompt.dynamic_vars": {"role": "user", "content": "基座"}}
    )
    content = updates["prompt.dynamic_vars"]["content"]
    return content[content.find("当前任务清单") :]


async def test_all_completed_no_todo_block() -> None:
    # 全部完成 → 清单块整体不注入（无剩余工作），说明常量仍在场
    plugin = _plugin()
    todo_text = _mod()._TODO_HEADER + "\n- [x] a\n- [x] b"
    updates = await plugin.fold(
        {"context.todo_text": todo_text, "prompt.dynamic_vars": {"role": "user", "content": "基座"}}
    )
    content = updates["prompt.dynamic_vars"]["content"]
    assert _mod().TODO_INSTRUCTIONS in content
    assert "当前任务清单" not in content, "全完成时清单块不得注入"


async def test_dynamic_guards_missing_or_malformed() -> None:
    plugin = _plugin()
    # dynamic_vars 缺失 → 零动作（说明常量也不写系统提示词）
    updates = await plugin.fold({"context.todo_text": "清单"})
    assert updates == {}
    # content 非 str（形态守卫）→ 零动作
    updates = await plugin.fold(
        {"context.todo_text": "清单", "prompt.dynamic_vars": {"content": 123}}
    )
    assert updates == {}


async def test_empty_state_zero_updates() -> None:
    assert await _plugin().fold({}) == {}


async def test_last_block_written_and_latest_populated() -> None:
    plugin = _plugin()
    text = "- [ ] 旧1\n- [ ] 旧2\n\n- [x] 新1\n- [ ] 新2"
    state = {"messages": [_assistant_msg(text)], "pipeline_id": "pipe-1"}
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert todo_text.startswith("当前任务清单")
    assert "- [x] 新1" in todo_text
    assert "- [ ] 新2" in todo_text
    assert "- [ ] 旧1" in todo_text
    payload = plugin.latest_payload("pipe-1")
    assert payload is not None
    assert [i["text"] for i in payload["items"]] == ["旧1", "旧2", "新1", "新2"]
    assert payload["updated_at"]


async def test_extract_never_rewrites_messages() -> None:
    # 消息 append-only 铁律（2026-10-24 用户裁定:渲染归声明,消息零改写）:
    # extract 只产 state 数据面更新,永不 emit messages ops
    plugin = _plugin()
    msg = dict(_assistant_msg("- [ ] a\n- [x] b"), seq=7, tool_calls=[])
    updates = await plugin.extract({"messages": [msg], "pipeline_id": "p"})
    assert "messages" not in updates
    assert updates["context.todo_text"].startswith("当前任务清单")


# ── thinking/reasoning 跳过（2026-11-10 用户提出：思考段清单草稿被捕获成
#    幽灵待办条目，正文按原文精确勾选永远匹配不上——提取面只扫正文最终
#    输出，跳过 reasoning/thinking 段。污染通道实测三条：content 内嵌
#    <think> 段（MiniMax-M3/DeepSeek-R1 形态）、流中断未闭合 <think> 尾段、
#    llm/adapter 非流式「思考当正文」兜底轮（content==reasoning_content）。）──


async def test_think_block_checklist_not_extracted() -> None:
    # 通道①：content 内嵌 <think>...</think> 段——段内清单草稿不产生待办
    plugin = _plugin()
    text = (
        "<think>\n先列一下计划：\n- [ ] 审批设计稿 v2 出稿后转呈你评审\n"
        "- [ ] 思考里的变体措辞草稿\n</think>\n这是正文回复，没有清单。"
    )
    state = {
        "messages": [_assistant_msg(text)],
        "pipeline_id": "pipe-t1",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


async def test_think_block_body_checklist_still_extracted() -> None:
    # 剥离只作用于思考段：段外正文清单行正常提取，思考措辞不入状态面
    plugin = _plugin()
    text = (
        "<think>\n草稿 - [ ] 思考里的变体A\n- [ ] 思考里的变体B\n</think>\n"
        "正文开始\n\n- [ ] 真实任务甲\n- [ ] 真实任务乙"
    )
    state = {"messages": [_assistant_msg(text)], "pipeline_id": "pipe-t2"}
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "- [ ] 真实任务甲" in todo_text
    assert "- [ ] 真实任务乙" in todo_text
    assert "变体" not in todo_text


async def test_unclosed_think_tail_not_extracted() -> None:
    # 通道②：流中断未闭合 <think> 尾段（含清单行）不产生待办
    plugin = _plugin()
    text = "正文回答。\n\n<think>\n- [ ] 半截思考里的草稿\n- [ ] 半截思考第二条"
    state = {
        "messages": [_assistant_msg(text)],
        "pipeline_id": "pipe-t3",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


async def test_pure_thinking_fallback_round_not_extracted() -> None:
    # 通道③：llm/adapter 非流式兜底「思考当正文」轮——content 与
    # reasoning_content 同文 = 无正文，跳过提取
    plugin = _plugin()
    thinking = "推理中列计划：\n- [ ] 思考草稿任务一\n- [ ] 思考草稿任务二"
    msg = {"role": "assistant", "content": thinking, "reasoning_content": thinking}
    state = {
        "messages": [msg],
        "pipeline_id": "pipe-t4",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


async def test_pure_think_block_no_body_not_extracted() -> None:
    # content 为纯 <think> 块（剥后正文为空）= 无正文可扫，跳过提取
    plugin = _plugin()
    text = "<think>\n- [ ] 思考草稿一\n- [ ] 思考草稿二\n</think>"
    state = {
        "messages": [_assistant_msg(text)],
        "pipeline_id": "pipe-t7",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


async def test_reasoning_content_field_ignored_body_authoritative() -> None:
    # reasoning_content 独立字段（隔离通道）永不参与提取；正文是唯一权威面
    plugin = _plugin()
    msg = {
        "role": "assistant",
        "content": "正文回复\n\n- [ ] 正文任务",
        "reasoning_content": "- [ ] 思考字段里的草稿",
    }
    state = {"messages": [msg], "pipeline_id": "pipe-t5"}
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "- [ ] 正文任务" in todo_text
    assert "草稿" not in todo_text


async def test_body_checklist_and_checkoff_unaffected_by_strip() -> None:
    # 回归：正文提取/勾选不受剥离影响（跨轮增量协议照常）
    plugin = _plugin()
    msg1 = {
        "role": "assistant",
        "content": "- [ ] 任务甲\n- [ ] 任务乙",
        "reasoning_content": "思考：先列计划",
    }
    updates = await plugin.extract(
        {"messages": [msg1], "pipeline_id": "pipe-t6"}
    )
    todo_text = updates["context.todo_text"]
    assert "- [ ] 任务甲" in todo_text
    assert "- [ ] 任务乙" in todo_text
    # 第二轮：正文勾选任务甲（合并基座 = 第一轮状态，同生产 pipeline_state 流转）
    msg2 = {
        "role": "assistant",
        "content": "- [x] 任务甲",
        "reasoning_content": "思考：甲完成",
    }
    updates2 = await plugin.extract(
        {
            "messages": [msg1, msg2],
            "pipeline_id": "pipe-t6",
            "context.todo_text": todo_text,
            "context.todo_state_v": _mod()._TODO_STATE_VERSION,
        }
    )
    merged_text = updates2["context.todo_text"]
    assert "- [x] 任务甲" in merged_text
    assert "- [ ] 任务乙" in merged_text


async def test_state_version_migration_rebuilds_body_only() -> None:
    # 幽灵条目自动清理：状态版本迁移 = 旧状态面作废，从近轮 assistant 正文
    # 重放重建（修复后重放只扫正文）——旧污染不延续，真实清单保留
    plugin = _plugin()
    ghost_text = _mod()._TODO_HEADER + "\n- [ ] 幽灵条目思考变形体"
    msg = {
        "role": "assistant",
        "content": "<think>\n- [ ] 幽灵来源草稿\n</think>\n\n- [ ] 正文真实任务",
    }
    state = {
        "messages": [msg],
        "pipeline_id": "pipe-m1",
        "context.todo_text": ghost_text,
        "context.todo_state_v": _mod()._TODO_STATE_VERSION - 1,
    }
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "- [ ] 正文真实任务" in todo_text
    assert "幽灵" not in todo_text
    assert updates["context.todo_state_v"] == _mod()._TODO_STATE_VERSION


async def test_state_version_migration_empty_replay_clears_ghost() -> None:
    # 版本迁移 + 近轮正文无清单 = 空基座：注入源显式清空（旧幽灵不固化、
    # 下轮 fold 不再注入污染清单）
    plugin = _plugin()
    ghost_text = _mod()._TODO_HEADER + "\n- [ ] 幽灵条目A\n- [ ] 幽灵条目B"
    msg = {"role": "assistant", "content": "本轮正文无清单。"}
    state = {
        "messages": [msg],
        "pipeline_id": "pipe-m2",
        "context.todo_text": ghost_text,
        "context.todo_state_v": _mod()._TODO_STATE_VERSION - 1,
    }
    updates = await plugin.extract(state)
    assert updates["context.todo_text"] == ""
    assert updates["context.todo_state_v"] == _mod()._TODO_STATE_VERSION


async def test_fallback_round_raw_equal_with_think_block_not_extracted() -> None:
    # 审查#1 方向A（缺口a）：content == reasoning_content 且含 <think> 段——
    # 同文判定必须 raw 对 raw（剥离前）；「剥离后正文对 raw reasoning」的
    # 归一化不对称不得复活漏判（剥离后尾部草稿被捕获 = 缺陷回归）
    plugin = _plugin()
    same = "<think>\n- [ ] 思考草稿任务\n</think>\n- [ ] 尾部思考草稿"
    msg = {"role": "assistant", "content": same, "reasoning_content": same}
    state = {
        "messages": [msg],
        "pipeline_id": "pipe-t8",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


async def test_body_extracted_even_when_matches_stripped_reasoning() -> None:
    # 审查#1 方向B（缺口b）：content 含 think 段 + reasoning_content 恰等于
    # 剥离后正文——raw 对 raw 不等（content 带标签）= 非兜底轮，剥离后正文
    # 是唯一权威面，不得被误判丢弃
    plugin = _plugin()
    body = "- [ ] 真实任务甲"
    msg = {
        "role": "assistant",
        "content": "<think>内部推理</think>\n" + body,
        "reasoning_content": body,
    }
    state = {"messages": [msg], "pipeline_id": "pipe-t9"}
    updates = await plugin.extract(state)
    assert "- [ ] 真实任务甲" in updates.get("context.todo_text", "")


async def test_think_variant_tags_draft_not_extracted() -> None:
    # 审查#2：think 变体标签族三形态——带属性闭合 / 无 > 开标签闭合 /
    # 带属性未闭合。字面 <think> 正则不匹配 → 草稿整段漏进提取面 = 缺陷
    # 回归（canonical 口径与 llm/adapter_minimax 同族实现对齐）
    plugin = _plugin()
    cases = [
        '<think type="x">\n- [ ] 变体草稿一\n- [ ] 变体草稿二\n</think...>',
        "<think\n- [ ] 无gt草稿\n</think/>",
        '<think type="x">\n- [ ] 半截变体草稿',
    ]
    for i, text in enumerate(cases):
        state = {
            "messages": [_assistant_msg(text)],
            "pipeline_id": f"pipe-v{i}",
            "context.todo_state_v": _mod()._TODO_STATE_VERSION,
        }
        assert await plugin.extract(state) == {}, f"变体形态{i}草稿泄漏: {text!r}"


async def test_think_variant_body_checklist_still_extracted() -> None:
    # 审查#2 正向面：变体标签剥离不伤正文——变体段后正文清单行正常提取、
    # 段内草稿不入状态面
    plugin = _plugin()
    cases = [
        '<think type="x">\n- [ ] 变体草稿\n</think...>\n\n- [ ] 真实任务乙',
        "<think\n引导\n</think/>\n- [ ] 真实任务丙",
    ]
    for i, text in enumerate(cases):
        state = {"messages": [_assistant_msg(text)], "pipeline_id": f"pipe-vb{i}"}
        updates = await plugin.extract(state)
        todo_text = updates.get("context.todo_text", "")
        assert "真实任务" in todo_text, f"变体形态{i}正文被误剥: {text!r}"
        assert "草稿" not in todo_text, f"变体形态{i}思考措辞泄漏: {text!r}"
        assert "引导" not in todo_text, f"变体形态{i}段内文本泄漏: {text!r}"


async def test_state_version_migration_empty_replay_clears_latest_panel() -> None:
    # 审查#3：版本迁移空基座分支——面板载荷同步清空（状态面=面板不变量：
    # 前端计划卡取数源 self._latest，状态已清而面板残留旧清单即两面对用户
    # 不一致，残留项还可能经 latest_any 串桶）
    plugin = _plugin()
    await plugin.extract(
        {
            "messages": [_assistant_msg("- [ ] 旧任务一\n- [ ] 旧任务二")],
            "pipeline_id": "pipe-m3",
        }
    )
    assert plugin.latest_payload("pipe-m3") is not None
    ghost_text = _mod()._TODO_HEADER + "\n- [ ] 幽灵条目"
    msg = {"role": "assistant", "content": "本轮正文无清单。"}
    state = {
        "messages": [msg],
        "pipeline_id": "pipe-m3",
        "context.todo_text": ghost_text,
        "context.todo_state_v": _mod()._TODO_STATE_VERSION - 1,
    }
    await plugin.extract(state)
    payload = plugin.latest_payload("pipe-m3")
    assert payload is not None
    assert payload["items"] == []


async def test_manifest_declares_todo_content_card() -> None:
    # manifest 声明面：content_cards 命中 fence=todo（片段级，form=webview）
    import json

    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    cards = manifest["contributes"]["content_cards"]
    card = next(c for c in cards if c["id"] == "todo_list")
    assert card["form"] == "template"
    assert "task-list" in card["match"]["blocks"]
    assert "<details" in card["template"]
    assert "chatMessages" not in manifest["contributes"]


async def test_badge_html_fetches_by_explicit_pipeline() -> None:
    # 角标取桶契约（2026-10-27 用户裁定：显示哪个管道就拉取哪个，不按
    # "最近更新"猜桶——后台多管道并行时最近更新者可能是其他任务）
    html = (_DIR / "web" / "cards" / "todo_badge.html").read_text(encoding="utf-8")
    assert "ctx.sync" in html, "宿主 ctx.sync 下行桥接收管道 ID"
    assert "pipeline_id=" in html, "取数带显式管道参数"


async def test_badge_html_declares_color_scheme_and_late_resize() -> None:
    # 2026-11-10 用户实测：深色主题下圆钮/展开面板四角出现实色方块底（圆角
    # 外侧本应透出宿主）。根因＝宿主深色而 iframe 文档未声明 color-scheme 时
    # Chromium 把文档画布涂成近白实色；旧宿主不注入 color-scheme，页面须在
    # theme.sync 时按生效主题兜底声明。尺寸面同批修：量测须避开入场动画
    # （pop scale .95→1）窗口，否则面板宽按 247/255 报出、宿主裁掉右缘。
    html = (_DIR / "web" / "cards" / "todo_badge.html").read_text(encoding="utf-8")
    assert "colorScheme" in html, "theme.sync 时落文档色彩方案（旧宿主兜底）"
    assert "schemeOf" in html, "色彩方案按生效主题背景亮度判定"
    assert "--ag-bg" in html, "色彩方案按生效主题背景亮度判定"
    assert "animationend" in html, "面板入场动画结束补报尺寸"
    assert "document.fonts" in html, "字体就绪补报尺寸（度量晚到改 scrollHeight）"


async def test_no_block_zero_write_state_stalls() -> None:
    # 权威规则：无块 = 状态停更（零写入，不回退不清空）
    plugin = _plugin()
    plugin._latest["pipe-1"] = {"items": [{"text": "历史", "done": False}]}
    state = {
        "messages": [_assistant_msg("纯文本收尾，无清单")],
        "pipeline_id": "pipe-1",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}
    assert plugin.latest_payload("pipe-1")["items"][0]["text"] == "历史"


async def test_multimodal_content_text_parts_joined() -> None:
    plugin = _plugin()
    state = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "结论："},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "- [x] 一\n- [ ] 二"},
                ],
            }
        ],
        "pipeline_id": "pipe-2",
    }
    updates = await plugin.extract(state)
    assert "- [x] 一" in updates["context.todo_text"]


async def test_x_normalized_to_lowercase_in_render() -> None:
    plugin = _plugin()
    state = {
        "messages": [_assistant_msg("- [X] 大写完成\n- [ ] 待办")],
        "pipeline_id": "pipe-3",
    }
    updates = await plugin.extract(state)
    assert "- [x] 大写完成" in updates["context.todo_text"]


async def test_no_pipeline_id_updates_without_latest() -> None:
    plugin = _plugin()
    state = {"messages": [_assistant_msg("- [ ] a\n- [ ] b")]}
    updates = await plugin.extract(state)
    assert "context.todo_text" in updates
    assert plugin.latest_payload("") is None


async def test_recent_assistant_message_is_source() -> None:
    # 提取源 = 最近一条 assistant（倒序找第一条 role=assistant），user 消息不算
    plugin = _plugin()
    state = {
        "messages": [
            _assistant_msg("- [ ] a\n- [ ] b"),
            {"role": "user", "content": "- [ ] 工具结果不算清单源\n- [ ] 再假"},
        ]
    }
    updates = await plugin.extract(state)
    assert "- [ ] a" in updates["context.todo_text"]
    assert "工具结果" not in updates["context.todo_text"]


async def test_no_assistant_message_zero_write() -> None:
    # 提取源只认 assistant：全 user 消息（无 assistant）= 无操作源，零写入；
    # 自愈重放同样只扫 assistant（用户正文里的 checkbox 不进状态面）
    plugin = _plugin()
    state = {
        "messages": [{"role": "user", "content": "- [ ] 用户行不参与"}],
        "pipeline_id": "pipe-user",
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
    }
    assert await plugin.extract(state) == {}


def test_latest_payload_miss_returns_none() -> None:
    assert _plugin().latest_payload("ghost") is None


@pytest.mark.parametrize(
    "state",
    [
        {"context.todo_state_v": _mod()._TODO_STATE_VERSION},
        {"context.todo_state_v": _mod()._TODO_STATE_VERSION, "messages": "不是列表"},
        {"context.todo_state_v": _mod()._TODO_STATE_VERSION, "messages": [{"role": "assistant"}]},
        {"context.todo_state_v": _mod()._TODO_STATE_VERSION, "messages": [{"role": "assistant", "content": 123}]},
    ],
)
async def test_extract_malformed_state_zero_write(state: dict) -> None:
    # 形态守卫：畸形 state 零写入不抛（server 层降级契约兜底，插件层先自守）
    assert await _plugin().extract(state) == {}


async def test_pending_over_budget_keeps_head_elides_middle_keeps_tail() -> None:
    # 注入面预算裁选（2026-10-27 用户裁定：超长裁选只作用于注入面）：35 项
    # 超预算 30 → 保首 10 + 尾 10，中段省略行替代；队首=下一步动作不丢
    block = await _fold_todo_block(_budget_todo_text(35))
    assert "- [ ] 任务00" in block, "队首保留"
    assert "- [ ] 任务09" in block, "队首保留（首 10 项完整）"
    assert "- [ ] 任务25" in block, "队尾保留"
    assert "- [ ] 任务34" in block, "队尾保留（末 10 项完整）"
    assert "- [ ] 任务10" not in block, "中段省略（首个被略项）"
    assert "- [ ] 任务24" not in block, "中段省略（末个被略项）"
    assert "已省略 15 项较早未完成项" in block
    assert "- [ ] 已省略" not in block, "省略行不得用 checkbox 语法（防整表重发被当新增合并）"


async def test_pending_within_budget_full_injection() -> None:
    # 恰在预算内（30 项）= 全量注入，无省略行
    block = await _fold_todo_block(_budget_todo_text(30))
    for i in range(30):
        assert f"- [ ] 任务{i:02d}" in block
    assert "已省略" not in block


def _persisted_todo_text(*lines: str) -> str:
    return _mod()._TODO_HEADER + "\n" + "\n".join(lines)


async def test_extract_merges_into_persisted_base_across_rounds() -> None:
    # 1.2.26 回归闸：合并基座 = 持久化的 context.todo_text（跨轮由内核冷
    # 恢复带回）。修复前该键未进 persistent_fields，每轮基座为空，清单只剩
    # 本轮增量——「前端面板只显示最新的」根因。
    plugin = _plugin()
    state = {
        "context.todo_text": _persisted_todo_text("- [ ] 调研", "- [x] 契约核对"),
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
        "messages": [_assistant_msg("- [x] 调研")],
        "pipeline_id": "pipe-base",
    }
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "- [x] 调研" in todo_text, "本轮勾选叠加在持久基座上"
    assert "- [x] 契约核对" in todo_text, "历史条目不得被本轮增量挤掉"


async def test_extract_repairs_lost_state_from_recent_messages() -> None:
    # 一次性自愈：状态版本标记缺失（1.2.25 及以前每轮丢状态）→ 从近轮
    # assistant 消息重放增量协议重建累积清单，并同批写版本标记（只跑一次）
    plugin = _plugin()
    state = {
        "messages": [
            _assistant_msg("- [ ] 甲\n- [ ] 乙\n- [ ] 丙"),
            _assistant_msg("- [x] 甲"),
            _assistant_msg("本轮无清单"),
        ],
        "pipeline_id": "pipe-heal",
    }
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "- [x] 甲" in todo_text
    assert "- [ ] 乙" in todo_text
    assert "- [ ] 丙" in todo_text
    assert updates["context.todo_state_v"] == _mod()._TODO_STATE_VERSION
    assert [i["text"] for i in plugin.latest_payload("pipe-heal")["items"]] == [
        "甲",
        "乙",
        "丙",
    ]


async def test_replay_window_excludes_old_messages() -> None:
    # 自愈重放有界：窗口外的旧阶段条目属于已翻篇内容，不得捞回
    plugin = _plugin()
    old = _assistant_msg("- [ ] 旧阶段条目")
    fresh = [_assistant_msg(f"- [ ] 近轮条目{i}") for i in range(10)]
    state = {"messages": [old, *fresh], "pipeline_id": "pipe-window"}
    updates = await plugin.extract(state)
    todo_text = updates["context.todo_text"]
    assert "旧阶段条目" not in todo_text
    assert "近轮条目9" in todo_text


async def test_versioned_state_with_no_ops_zero_write() -> None:
    # 版本标记已在场 = 自愈已跑过；无 ops 轮零写入（注释不刷 updated_at）
    plugin = _plugin()
    state = {
        "context.todo_text": _persisted_todo_text("- [ ] a"),
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
        "messages": [_assistant_msg("纯文本收尾，无清单")],
        "pipeline_id": "pipe-ver",
    }
    assert await plugin.extract(state) == {}


async def test_no_effective_change_zero_write_even_with_ops() -> None:
    # 重复轮幂等：同态 ops 不推进状态（停更语义，不刷载荷时间戳）
    plugin = _plugin()
    state = {
        "context.todo_text": _persisted_todo_text("- [x] a"),
        "context.todo_state_v": _mod()._TODO_STATE_VERSION,
        "messages": [_assistant_msg("- [x] a")],
        "pipeline_id": "pipe-idem",
    }
    assert await plugin.extract(state) == {}



async def test_instructions_contract_new_keywords_and_no_legacy() -> None:
    # 文案契约（2026-11-18 用户口径四轮收敛的精简定稿）：新关键词在场、
    # 旧长版表述移除。断言的是注入说明常量的内容契约——fold/extract 行为
    # 不变，变的只是模型指令面文本。
    ins = _mod().TODO_INSTRUCTIONS
    new_keywords = (
        "每条待办只写两次",  # 生命周期契约：两次=建卡+勾销（用户裁定：非「两行」）
        "用户需求必须建待办",  # 提出即建
        "勾销门槛",  # 分两类
        "原始需求",
        "中间步骤",
        "决策分流",
        "human_interaction",  # 要问/要拍→第一优先级交互工具
        "失效条目",
        "禁止把清单输出成文件",  # GFM 任务行约束扩面（用户裁定）
        "清单只能写 GFM 任务行",  # 保留条
    )
    for kw in new_keywords:
        assert kw in ins, f"新口径关键词缺失：{kw}"
    legacy_phrases = (
        "全生命周期只输出两行",  # 旧生命周期表述（改「只写两次」）
        "可折叠清单卡",
        "用户没说通过，不得勾选",  # 被勾销门槛分两类取代
        "全域适用",  # 用户裁定删除
        "其余时刻",  # 「报进度/后台状态用普通文字」句删除
        "禁止正文问句",  # 改为交互工具第一优先级
    )
    for legacy in legacy_phrases:
        assert legacy not in ins, f"旧表述残留：{legacy}"
