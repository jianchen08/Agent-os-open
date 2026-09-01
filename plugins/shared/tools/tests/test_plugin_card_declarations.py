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
import os
from collections.abc import Iterator
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
    "form",  # 交互表单块（widget 化 T2 引入，widget_demo 使用）
}

# 遍历剪枝集：.venv（各插件独立 venv）/ node_modules / 缓存目录；
# 另剪一切 junction——dsh_adapter 装载区以 peer junction 指向 DSH 参考仓
# （设计机制，见 docs/dsh_decision_records.md），rglob 旧写法会跟进 junction
# 遍历整个参考仓（junction 环 + .pnpm 巨树，2026-08-21 实测收集期挂死 45min+）。
_SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", ".pytest_cache"}


def _is_junction(path: str) -> bool:
    """junction/符号链接判定（跨版本）。

    ``os.path.isjunction`` 仅 py3.12+；CI runner 为 py3.11，
    降级判 islink（junction 在 POSIX 侧即 symlink，覆盖 CI 场景）。
    """
    if hasattr(os.path, "isjunction"):
        return bool(os.path.isjunction(path))
    return os.path.islink(path)


def _iter_plugin_jsons(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIRS and not _is_junction(os.path.join(dirpath, d))
        ]
        if "plugin.json" in filenames:
            yield Path(dirpath) / "plugin.json"


# 全部工具插件（含工具声明的 plugin.json）
TOOL_PLUGIN_JSONS = sorted(_iter_plugin_jsons(_TOOLS_ROOT.parent))


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


# source 求值根（与 frontend/src/utils/chatCardInterpreter.ts 的 evalSinglePath 对齐）：
# args./error./duration_ms./partial_output. 根的取值不在工具 output 里，
# output_schema 一致性检查只对 output./result. 根生效（见 _referenced_output_fields）。
def _chat_card_source_texts(chat_card: dict) -> list[str]:
    """收集 chat_card 全部 source 表达式（blocks/fields/filePathSource/diffStat）。"""
    texts: list[str] = []
    for block in chat_card.get("blocks", []):
        for key in ("source", "when", "unless", "diffOldSource", "diffNewSource"):
            v = block.get(key)
            if isinstance(v, str):
                texts.append(v)
        for field in block.get("fields") or []:
            if isinstance(field.get("source"), str):
                texts.append(field["source"])
    if isinstance(chat_card.get("filePathSource"), str):
        texts.append(chat_card["filePathSource"])
    diff_stat = chat_card.get("diffStat") or {}
    for key in ("addedSource", "removedSource"):
        if isinstance(diff_stat.get(key), str):
            texts.append(diff_stat[key])
    return texts


def _referenced_output_fields(texts: list[str]) -> set[str]:
    """从 source 表达式提取引用的 output 一级字段名。

    表达式语法：`output.X || result.Y | filter:arg`（|| 回退 + 过滤器管道），
    仅取 output./result. 根的首段字段；args./error./duration_ms. 等非输出根跳过。
    """
    fields: set[str] = set()
    for text in texts:
        for alt in text.split("||"):
            path = alt.split("|")[0].strip()
            root, _, rest = path.partition(".")
            if root in ("output", "result") and rest:
                fields.add(rest.split(".")[0])
    return fields


@pytest.mark.parametrize("path", TOOL_PLUGIN_JSONS, ids=lambda p: str(p.relative_to(_TOOLS_ROOT.parent)))
def test_chat_card_sources_in_output_schema(path: Path) -> None:
    """chat_card 引用的 output/result 字段必须存在于该工具的 output_schema。

    防声明与实现断链（前端解释器对缺失字段静默判空，坏声明=卡片块悄悄不渲染，
    无任何报错）：diff 块/统计/内容块的 source 引用了 output_schema 未声明的
    字段即视为契约漂移。
    """
    for tool in _tool_entries(path):
        chat_card = (tool.get("ui") or {}).get("chat_card")
        if not chat_card:
            continue
        output_schema = tool.get("output_schema") or {}
        props = (output_schema.get("properties") or {})
        referenced = _referenced_output_fields(_chat_card_source_texts(chat_card))
        if not props:
            # 未声明 output_schema 的工具无从对表（render 卡另有词汇校验兜底）
            continue
        missing = sorted(f for f in referenced if f not in props)
        assert not missing, (
            f"{path.name}/{tool['name']}: chat_card 引用了 output_schema 未声明的字段 {missing}——"
            f"要么输出侧补字段，要么声明侧改引用（现状=前端对应块静默不渲染）"
        )


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
