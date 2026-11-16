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
    todo_text = "当前任务清单（以此为准，变更时整表重发）：\n- [ ] a"
    dvm = {"role": "user", "content": "动态变量基座"}
    updates = await plugin.fold(
        {"context.todo_text": todo_text, "prompt.dynamic_vars": dvm}
    )
    content = updates["prompt.dynamic_vars"]["content"]
    base = content.find("动态变量基座")
    ins = content.find(_mod().TODO_INSTRUCTIONS)
    todo = content.find(todo_text)
    assert -1 < base < ins < todo


async def test_completed_items_filtered_from_injection() -> None:
    # 注入面只重申剩余工作（2026-10-24 用户裁定）：[x] 行剔除、[ ] 行保留、
    # header 维持——已完成阶段回放会把模型锚死在旧阶段（G2 循环事故）
    plugin = _plugin()
    todo_text = (
        "当前任务清单（以此为准，变更时整表重发）：\n"
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
    return "当前任务清单（以此为准，变更时整表重发）：\n" + "\n".join(lines)


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
    todo_text = "当前任务清单（以此为准，变更时整表重发）：\n- [x] a\n- [x] b"
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


async def test_no_block_zero_write_state_stalls() -> None:
    # 权威规则：无块 = 状态停更（零写入，不回退不清空）
    plugin = _plugin()
    plugin._latest["pipe-1"] = {"items": [{"text": "历史", "done": False}]}
    state = {
        "messages": [_assistant_msg("纯文本收尾，无清单")],
        "pipeline_id": "pipe-1",
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
    }
    assert await plugin.extract(state) == {}


def test_latest_payload_miss_returns_none() -> None:
    assert _plugin().latest_payload("ghost") is None


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"messages": "不是列表"},
        {"messages": [{"role": "assistant"}]},
        {"messages": [{"role": "assistant", "content": 123}]},
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
    return "当前任务清单（以此为准，变更时整表重发）：\n" + "\n".join(lines)


async def test_extract_merges_into_persisted_base_across_rounds() -> None:
    # 1.2.26 回归闸：合并基座 = 持久化的 context.todo_text（跨轮由内核冷
    # 恢复带回）。修复前该键未进 persistent_fields，每轮基座为空，清单只剩
    # 本轮增量——「前端面板只显示最新的」根因。
    plugin = _plugin()
    state = {
        "context.todo_text": _persisted_todo_text("- [ ] 调研", "- [x] 契约核对"),
        "context.todo_state_v": 1,
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
    assert updates["context.todo_state_v"] == 1
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
        "context.todo_state_v": 1,
        "messages": [_assistant_msg("纯文本收尾，无清单")],
        "pipeline_id": "pipe-ver",
    }
    assert await plugin.extract(state) == {}


async def test_no_effective_change_zero_write_even_with_ops() -> None:
    # 重复轮幂等：同态 ops 不推进状态（停更语义，不刷载荷时间戳）
    plugin = _plugin()
    state = {
        "context.todo_text": _persisted_todo_text("- [x] a"),
        "context.todo_state_v": 1,
        "messages": [_assistant_msg("- [x] a")],
        "pipeline_id": "pipe-idem",
    }
    assert await plugin.extract(state) == {}
