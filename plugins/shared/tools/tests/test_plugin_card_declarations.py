# @feature: FP-0.2.可观测性 工具卡片渲染声明 | @ci: python-coverage
"""工具插件 plugin.json 卡片渲染声明结构校验。

渲染双路由（声明路由 + 数据路由）的声明侧契约：
- 每个工具插件在自家 plugin.json 的 capabilities.tools[] 条目声明渲染形式：
  - ``render``：渲染卡片（card ∈ terminal/diff/read/web/search/generic/
    image/file/table/form，bindings 指向 ``args.x`` / ``result.y`` 路径）；
  - ``ui.chat_card``：块级表单声明（title 模板 / blocks 类型合法）。
- 本测试静态校验全部工具插件的声明结构合法（防手写 JSON 漂移）——
  与前端 dshRenderIntent / chatCardInterpreter 的宽松解析（非法即弃）互补：
  前端静默丢弃坏声明，这里在 CI 层拦截。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_TOOLS_ROOT = Path(__file__).resolve().parent.parent

# 渲染卡片词汇表（与 frontend/src/utils/dshRenderIntent.ts 保持一致）
RENDER_CARDS = {
    "terminal",
    "diff",
    "read",
    "web",
    "search",
    "generic",
    "image",
    "file",
    "table",
    "form",
}

# chat_card 块类型词汇表（与 frontend/src/utils/chatCardInterpreter.ts 保持一致）
CHAT_CARD_BLOCK_TYPES = {
    "text",
    "code",
    "json",
    "markdown",
    "diff",
    "kv",
    "file",
    "image",
    "link",
    "log",
}

# 全部工具插件（含工具声明的 plugin.json）
TOOL_PLUGIN_JSONS = sorted(
    p for p in (_TOOLS_ROOT / "..").rglob("plugin.json") if "node_modules" not in str(p)
)


def _tool_entries(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("capabilities") or {}).get("tools") or []


@pytest.mark.parametrize("path", TOOL_PLUGIN_JSONS, ids=lambda p: str(p.relative_to(_TOOLS_ROOT.parent)))
def test_render_declaration_valid(path: Path) -> None:
    """render 声明：card 在词汇表内；bindings 指向 args./result. 路径。"""
    for tool in _tool_entries(path):
        render = tool.get("render")
        if not render:
            continue
        assert render.get("card") in RENDER_CARDS, (
            f"{path.name}/{tool['name']}: render.card={render.get('card')!r} 不在词汇表 {sorted(RENDER_CARDS)}"
        )
        bindings = render.get("bindings")
        if bindings:
            assert isinstance(bindings, dict), f"{tool['name']}: bindings 必须是对象"
            for field, source in bindings.items():
                assert isinstance(source, str) and source.startswith(("args.", "result.")), (
                    f"{path.name}/{tool['name']}: bindings[{field}]={source!r} 必须以 args./result. 开头"
                )


@pytest.mark.parametrize("path", TOOL_PLUGIN_JSONS, ids=lambda p: str(p.relative_to(_TOOLS_ROOT.parent)))
def test_chat_card_declaration_valid(path: Path) -> None:
    """ui.chat_card 声明：blocks 类型合法；diffStat 源为 args./result./output. 路径。"""
    for tool in _tool_entries(path):
        ui = tool.get("ui") or {}
        chat_card = ui.get("chat_card")
        if not chat_card:
            continue
        for block in chat_card.get("blocks", []):
            assert block.get("type") in CHAT_CARD_BLOCK_TYPES, (
                f"{path.name}/{tool['name']}: block.type={block.get('type')!r} 不在词汇表"
            )
            for key in ("source", "when", "unless"):
                source = block.get(key)
                if source:
                    assert isinstance(source, str), f"{tool['name']}: blocks[].{key} 必须为字符串"
        diff_stat = chat_card.get("diffStat")
        if diff_stat:
            for key in ("addedSource", "removedSource"):
                source = diff_stat.get(key)
                assert source and isinstance(source, str) and source.startswith(
                    ("args.", "result.", "output.")
                ), f"{tool['name']}: diffStat.{key}={source!r} 必须以 args./result./output. 开头"


def test_all_tool_plugins_have_card_declaration() -> None:
    """全部 LLM 面工具插件都应声明渲染形式（render 或 ui.chat_card）。

    内置工具（含服务能力 interaction.* 等非 LLM 工具）豁免——只校验
    plugins/shared/tools/ 下声明的、有前端渲染诉求的 LLM 工具。
    """
    tool_plugins = sorted((_TOOLS_ROOT / "..").glob("plugins/shared/tools/*/plugin.json"))
    undeclared: list[str] = []
    for path in tool_plugins:
        for tool in _tool_entries(path):
            name = tool["name"]
            # 服务能力（点号命名，如 interaction.create_choice）不渲染卡片，豁免
            if "." in name:
                continue
            if "render" not in tool and "ui" not in tool:
                undeclared.append(f"{path.parent.name}/{name}")
    assert not undeclared, f"以下 LLM 工具未声明渲染形式: {undeclared}"
