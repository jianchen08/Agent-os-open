# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage

"""todo_extract 纯函数测试——断行为（输入→输出），区分度输入成组。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

for _p in (str(Path(__file__).resolve().parent),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from todo_extract import (  # noqa: E402
    latest_items,
    parse_html_card_ops,
    parse_todo_blocks,
)


def test_multi_blocks_preserves_order() -> None:
    text = "- [ ] a\n- [x] b\n\n前文\n- [ ] c\n- [ ] d"
    blocks = parse_todo_blocks(text)
    assert len(blocks) == 2
    assert [i["text"] for i in blocks[0]] == ["a", "b"]
    assert blocks[0][1]["done"] is True
    assert [i["text"] for i in blocks[1]] == ["c", "d"]


def test_single_checkbox_line_is_not_a_block() -> None:
    # 单条不成块（防散文误报的阈值规则）
    assert parse_todo_blocks("- [ ] only") == []
    assert latest_items("- [ ] only") is None


def test_no_checkboxes_returns_empty() -> None:
    assert parse_todo_blocks("普通散文\n- 普通短横线列表\n1. 有序列表") == []
    assert latest_items("") is None
    assert latest_items("完全没内容") is None


def test_code_fence_excluded_and_splits_blocks() -> None:
    text = (
        "- [ ] a\n"
        "- [x] b\n"
        "```python\n"
        "- [ ] 代码里的假清单\n"
        "- [ ] 也是假的\n"
        "```\n"
        "- [ ] c\n"
        "- [ ] d"
    )
    blocks = parse_todo_blocks(text)
    assert len(blocks) == 2
    assert [i["text"] for i in blocks[0]] == ["a", "b"]
    assert [i["text"] for i in blocks[1]] == ["c", "d"]
    assert all("假清单" not in i["text"] for b in blocks for i in b)


def test_tilde_fence_excluded() -> None:
    text = "~~~\n- [ ] 假\n- [ ] 假\n~~~\n- [ ] 真1\n- [ ] 真2"
    blocks = parse_todo_blocks(text)
    assert len(blocks) == 1
    assert [i["text"] for i in blocks[0]] == ["真1", "真2"]


def test_noncheckbox_line_closes_block() -> None:
    # 勾选行之间夹普通文字 → 拆块（各 ≥2 条才成块）
    text = "- [ ] a\n说明文字\n- [x] b\n- [ ] c"
    blocks = parse_todo_blocks(text)
    assert len(blocks) == 1
    assert [i["text"] for i in blocks[0]] == ["b", "c"]


def test_nested_items_flattened_in_order() -> None:
    # 已知简化：嵌套子项扁平提取，保序不建层级
    text = "- [ ] 父\n  - [ ] 子1\n  - [x] 子2\n- [ ] 另一父"
    blocks = parse_todo_blocks(text)
    assert len(blocks) == 1
    assert [i["text"] for i in blocks[0]] == ["父", "子1", "子2", "另一父"]
    assert blocks[0][2]["done"] is True


def test_cjk_and_punctuation_text_preserved() -> None:
    text = "- [ ] 完成 中文条目：带标点！\n- [x] 第二条 (English mixed)"
    blocks = parse_todo_blocks(text)
    assert blocks[0][0]["text"] == "完成 中文条目：带标点！"
    assert blocks[0][1]["text"] == "第二条 (English mixed)"


def test_last_block_wins() -> None:
    text = "- [ ] 旧1\n- [ ] 旧2\n中间的话\n- [x] 新1\n- [ ] 新2"
    items = latest_items(text)
    assert items is not None
    assert [i["text"] for i in items] == ["新1", "新2"]


def test_done_flag_x_case_tolerant() -> None:
    # 完成态 x/X 宽容（可枚举输入展开）
    for mark, expected in (("x", True), ("X", True), (" ", False)):
        items = latest_items(f"- [{mark}] 一\n- [{mark}] 二")
        assert items is not None
        assert all(i["done"] is expected for i in items), f"mark={mark!r}"


def test_none_when_no_block() -> None:
    assert latest_items("无清单") is None
    assert latest_items("- [ ] 单条不成块") is None


# ── HTML 任务卡解析（2026-10-25 用户实测：模型仿历史折叠卡把清单包进
#    ```html 围栏——GFM 零 ops、勾选丢失、界面显示源码）──


def _card_html(items: str) -> str:
    return (
        '<details style="margin:6px 0;font:13px/1.6">'
        '<summary style="cursor:pointer;">📋 任务清单 3/7</summary>'
        f'<ul style="padding-left:20px;">{items}</ul></details>'
    )


def test_parse_html_card_ops_user_reported_shape() -> None:
    # 用户上报的真实形态：带内联样式 li + span 标记 + 已完成划线 + 🔄 进行中
    text = (
        "三线全部重新拉起成功 ✅。当前任务清单：\n```html\n"
        + _card_html(
            '<li style="color:#8b949e;text-decoration:line-through;">'
            '<span style="margin-right:6px;color:#0969da;">☑</span>三线恢复执行：步骤 1b 补台账</li>'
            '<li style="color:#8b949e;text-decoration:line-through;">'
            '<span style="margin-right:6px;color:#0969da;">☑</span>应用重启撞车处置</li>'
            '<li style="margin:2px 0;"><span style="color:#d29922;">🔄</span>步骤 1b 收尾执行与收账</li>'
            '<li style="margin:2px 0;"><span style="color:#d29922;">🔄</span>UI 修复续作 2 契约同步</li>'
            '<li style="margin:2px 0;"><span style="color:#8b949e;">☐</span>三线回卷后继续派发剩余项</li>'
        )
        + "\n```\n"
    )
    ops = parse_html_card_ops(text)
    assert [o["text"] for o in ops] == [
        "三线恢复执行：步骤 1b 补台账",
        "应用重启撞车处置",
        "步骤 1b 收尾执行与收账",
        "UI 修复续作 2 契约同步",
        "三线回卷后继续派发剩余项",
    ]
    assert [o["done"] for o in ops] == [True, True, False, False, False]


def test_parse_html_card_ops_two_char_glyph_and_entities() -> None:
    # ☑️ 两字符形 + HTML 实体文本
    text = _card_html(
        '<li><span>☑️</span>确认目的地 &amp; 出发</li>'
        '<li><span>☐</span>查询航班 &lt;周五&gt;</li>'
    )
    ops = parse_html_card_ops(text)
    assert ops[0] == {"text": "确认目的地 & 出发", "done": True}
    assert ops[1] == {"text": "查询航班 <周五>", "done": False}


def test_parse_html_card_ops_guard_rejects_non_card() -> None:
    # 非 <details 任务卡形态（普通 html 文档/单条 li）返回 [] —— 防误解析
    assert parse_html_card_ops("<ul><li>☑ 只有单条</li></ul>") == []
    assert parse_html_card_ops("<p>普通段落，无任务清单卡</p>") == []
    assert parse_html_card_ops("<details><summary>折叠</summary><li>☑ 一条</li><li>☐ 两条</li></ul>") == []


def test_extract_falls_back_to_html_card_ops() -> None:
    # extract 增量合并走通：HTML 卡 ops 勾选现有项（GFM 零 ops 时兜底）
    import asyncio
    import importlib.util

    # 裸名 `plugin` 全车道共跑时会被先收集目录的同名模块劫持（邻居测试模块
    # 对 sys.path 的 insert(0) 不回收，运行期解析落在别人头上）——按本目录
    # 显式路径加载（与 task_reminder/security_check 系测试同范式）。
    _dir = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "todo_track_plugin_extract_test", str(_dir / "plugin.py")
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["todo_track_plugin_extract_test"] = _mod
    _spec.loader.exec_module(_mod)
    TodoTrackPlugin = _mod.TodoTrackPlugin

    plugin = TodoTrackPlugin()
    state = {
        "pipeline_id": "p1",
        "messages": [
            {"role": "user", "content": "继续"},
            {
                "role": "assistant",
                "content": "进度如下：\n```html\n"
                + _card_html(
                    '<li><span>☑</span>查询航班</li><li><span>☐</span>输出摘要</li>'
                )
                + "\n```",
            },
        ],
    }
    out = asyncio.run(plugin.extract(state))
    todo_text = out["context.todo_text"]
    assert "- [x] 查询航班" in todo_text
    assert "- [ ] 输出摘要" in todo_text
