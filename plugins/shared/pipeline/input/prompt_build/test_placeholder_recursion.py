"""占位符递归嵌套解析的单测。

验证 prompt_build.PromptBuildPlugin._resolve_placeholders 的递归行为：
    1. 单层：{{timestamp}} 等基础占位符正常解析
    2. 两层嵌套：{{path:partial.md}}（内容含 {{timestamp}}）→ 两层都解析
    3. 三层嵌套：a→b→c→值
    4. 死循环保护：{{a}} 解析成含 {{a}} 的内容 → 受 max_depth 限制
    5. 互相引用：{{a}}→{{b}}→{{a}} → 受 max_depth 限制
    6. 收敛检测：未识别占位符替换为空后，下一趟无变化则提前结束
    7. max_depth=0：退化为单趟扁平替换（向后兼容）
    8. 全局替换语义：同一占位符多次出现全部替换
    9. 向后兼容：max_depth=1 行为与旧版单趟一致

通过 monkey-patch _resolve_placeholder 精确控制每个占位符的解析结果，
不依赖整个 pipeline / 文件系统 / memory 服务。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 复制 server.py / test_timezone.py 的 sys.path 机制：
#   插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402
from plugin import PromptBuildPlugin  # noqa: E402


# ── 测试辅助 ──


def make_plugin(max_depth: int = 5) -> PromptBuildPlugin:
    """创建带指定 placeholder_max_depth 配置的插件实例。"""
    return PromptBuildPlugin(config={"placeholder_max_depth": max_depth})


def make_ctx() -> PluginContext:
    """创建最小化的插件上下文。"""
    return PluginContext(state={})


def patch_resolver(plugin: PromptBuildPlugin, mapping: dict[str, str]) -> None:
    """把 plugin._resolve_placeholder 替换为按 mapping 查表的同步版本。

    mapping 的 key 是占位符内部文本（不含 {{ }}），value 是解析结果。
    未知的 key 返回空字符串（与生产代码对未知占位符的行为一致）。
    """
    async def fake_resolve(_ctx: PluginContext, content: str) -> str:
        return mapping.get(content, "")

    plugin._resolve_placeholder = fake_resolve  # type: ignore[method-assign]


# ══════════════════════════════════════════════════
# 1. 单层：基础占位符正常解析
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_single_layer_basic() -> None:
    """单层占位符正常解析（向后兼容）。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(plugin, {"timestamp": "2026-08-11 10:00:00"})
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "现在是 {{timestamp}}")

    assert result == "现在是 2026-08-11 10:00:00"


@pytest.mark.asyncio
async def test_single_layer_multiple_distinct() -> None:
    """同一文本里多个不同占位符都解析。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(plugin, {"name": "小明", "age": "18"})
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "{{name}} 今年 {{age}} 岁")

    assert result == "小明 今年 18 岁"


# ══════════════════════════════════════════════════
# 2. 两层嵌套：解析结果里再含占位符
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_two_layer_nesting() -> None:
    """两层嵌套：外层 path 读出的内容含 {{timestamp}}，timestamp 也要被解析。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(
        plugin,
        {
            "path:partial.md": "报告生成时间：{{timestamp}}",
            "timestamp": "2026-08-11 10:00:00",
        },
    )
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "头部：{{path:partial.md}}")

    assert result == "头部：报告生成时间：2026-08-11 10:00:00"


# ══════════════════════════════════════════════════
# 3. 三层嵌套
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_three_layer_nesting() -> None:
    """三层嵌套：a→b→c→值。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(
        plugin,
        {
            "a": "[[{{b}}]]",
            "b": "<<{{c}}>>",
            "c": "VALUE",
        },
    )
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "X{{a}}X")

    assert result == "X[[<<VALUE>>]]X"


# ══════════════════════════════════════════════════
# 4. 死循环保护：自引用
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_self_reference_does_not_hang() -> None:
    """自引用 {{a}}→'{{a}}' 必须在 max_depth 内停止，不无限循环。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(plugin, {"a": "{{a}}"})  # 解析结果里又含自己
    ctx = make_ctx()

    # 必须在合理时间内返回（不死循环）
    result = await asyncio.wait_for(
        plugin._resolve_placeholders(ctx, "{{a}}"),
        timeout=5.0,
    )

    # 5 层后停止：每次都把 {{a}} 替换成 {{a}}，文本不变，第 1 趟后就因收敛检测退出
    # 实际结果仍是 {{a}}（无法收敛的自引用）
    assert result == "{{a}}"


# ══════════════════════════════════════════════════
# 5. 互相引用：a→b→a→b...
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mutual_reference_does_not_hang() -> None:
    """互相引用 {{a}}→'{{b}}'、{{b}}→'{{a}}' 必须受 max_depth 限制。"""
    plugin = make_plugin(max_depth=4)
    patch_resolver(
        plugin,
        {"a": "{{b}}", "b": "{{a}}"},
    )
    ctx = make_ctx()

    result = await asyncio.wait_for(
        plugin._resolve_placeholders(ctx, "start {{a}} end"),
        timeout=5.0,
    )

    # 第 1 趟：{{a}} → {{b}}（文本变化，继续）
    # 第 2 趟：{{b}} → {{a}}（文本变化，继续）
    # 第 3 趟：{{a}} → {{b}}（文本变化，继续）
    # 第 4 趟：{{b}} → {{a}}（达到 max_depth，停止）
    # 最终剩 {{a}}
    assert "{{" in result  # 仍有未解析的占位符
    assert result == "start {{a}} end"


# ══════════════════════════════════════════════════
# 6. 收敛检测：未识别占位符提前结束
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unknown_placeholder_terminates_early() -> None:
    """未知占位符（替换为空）后，下一趟无变化则提前结束，不跑满 max_depth。"""
    plugin = make_plugin(max_depth=10)  # 设大，看是否真的提前停
    patch_resolver(plugin, {})  # 所有占位符都未知 → 返回空
    ctx = make_ctx()

    # 不应该 hang，也不应该跑 10 趟
    result = await asyncio.wait_for(
        plugin._resolve_placeholders(ctx, "a{{unknown}}b"),
        timeout=5.0,
    )

    # {{unknown}} → ""，文本变成 "ab"，下一趟 findall 无占位符 → 收敛退出
    assert result == "ab"


# ══════════════════════════════════════════════════
# 7. max_depth=0：退化为单趟扁平替换（向后兼容）
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_max_depth_zero_is_single_pass() -> None:
    """max_depth=0 等同于单趟扁平替换，嵌套占位符不再二次解析。"""
    plugin = make_plugin(max_depth=0)
    patch_resolver(
        plugin,
        {
            "a": "({{b}})",  # 内含 {{b}}
            "b": "VALUE",
        },
    )
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "{{a}}")

    # 单趟：只解析 {{a}} → "({{b}})"，{{b}} 不会再被解析
    assert result == "({{b}})"


# ══════════════════════════════════════════════════
# 8. 全局替换语义：同一占位符多次出现全部替换
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_global_replacement_multiple_occurrences() -> None:
    """同一个占位符出现多次，全部替换（保留旧版 text.replace 全局语义）。"""
    plugin = make_plugin(max_depth=5)
    patch_resolver(plugin, {"x": "OK"})
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "{{x}} {{x}} {{x}}")

    assert result == "OK OK OK"


# ══════════════════════════════════════════════════
# 9. 向后兼容：max_depth=1 行为与旧版单趟一致
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_max_depth_one_matches_old_behavior() -> None:
    """max_depth=1 只跑一趟，嵌套占位符不被二次解析（与旧版完全一致）。"""
    plugin = make_plugin(max_depth=1)
    patch_resolver(
        plugin,
        {
            "path:f.md": "内容 {{timestamp}} 结束",
            "timestamp": "TS",
        },
    )
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "{{path:f.md}}")

    # 只跑一趟：{{path:f.md}} → "内容 {{timestamp}} 结束"，timestamp 不再解析
    assert result == "内容 {{timestamp}} 结束"


# ══════════════════════════════════════════════════
# 10. 无占位符：直接返回原文
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_placeholder_returns_original() -> None:
    """无占位符的文本直接返回，不调用 resolver。"""
    plugin = make_plugin(max_depth=5)
    called: list[str] = []

    async def spy_resolve(_ctx: PluginContext, content: str) -> str:
        called.append(content)
        return ""

    plugin._resolve_placeholder = spy_resolve  # type: ignore[method-assign]
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "纯文本无占位符")

    assert result == "纯文本无占位符"
    assert called == []  # resolver 未被调用


# ══════════════════════════════════════════════════
# 11. 深度限制：嵌套超过 max_depth 不再解析
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_depth_limit_truncates_deep_nesting() -> None:
    """嵌套 5 层但 max_depth=2，只解析前 2 层。"""
    plugin = make_plugin(max_depth=2)
    patch_resolver(
        plugin,
        {
            "a": "[{{b}}]",
            "b": "<{{c}}>",
            "c": "({{d}})",
            "d": "VALUE",
        },
    )
    ctx = make_ctx()

    result = await plugin._resolve_placeholders(ctx, "{{a}}")

    # 第 1 趟：{{a}} → [{{b}}]
    # 第 2 趟：{{b}} → <{{c}}>
    # 达 max_depth=2，停止。{{c}} 未解析
    assert result == "[<{{c}}>]"
    assert "{{c}}" in result
    assert "{{d}}" not in result  # 更深层根本没碰到
