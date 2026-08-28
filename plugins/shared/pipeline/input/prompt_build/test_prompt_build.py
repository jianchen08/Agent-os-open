# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""prompt_build plugin 行为测试。

覆盖面：
1. 读路径零记忆库操作 —— 压缩块是 message 序列里的普通消息（guard 原位写入），
   本插件不再按 pipeline 回源 recall/search（ADR 2026-08-28
   compression-block-pointer-indirection；hindsight.recall/search 自取退役）；
2. routed 条件注入（按 state 键值路由）
3. 未识别占位符可观测（warning 留痕）
4. 知识注入服务缺失可观测（warning 留痕）

测试不依赖真实记忆后端——通过 FakeBackend 注入模块级 _memory_backend
（vector/hybrid 变量模式仍消费该 backend）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + pipeline 包加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级 _memory_backend 跨测试污染）。"""
    mod_name = "prompt_build_plugin_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    """构造最小 PluginContext。"""
    return PluginContext(state=dict(state or {}))


class FakeBackend:
    """记录 search 调用的伪 IMemoryBackend（duck-typed，无需继承 ABC）。"""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.search_returns: list[dict[str, Any]] = list(results or [])

    async def search(
        self,
        query: str = "",
        user_id: str = "",
        top_k: int = 5,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "query": query,
                "user_id": user_id,
                "top_k": top_k,
                "memory_type": memory_type,
                "tags": tags,
                "tags_match": tags_match,
            }
        )
        return list(self.search_returns)


# ═══════════════════════════════════════════════════════════
# 1. 读路径零记忆库操作（search 自取退役）
# ═══════════════════════════════════════════════════════════


class TestNoCompressionSelfFetch:
    """压缩块读路径零操作：无论有无 pipeline_id / backend，构建消息时
    对 memory backend 零调用（ADR 2026-08-28 compression-block-pointer-
    indirection：块消息随序列持久化，llm_core 从 history 直接拼装）。"""

    @pytest.mark.parametrize(
        "state",
        [
            {"pipeline_id": "pipe-1", "user_id": "u-1"},
            {"pipeline_id": "", "user_id": ""},
        ],
        ids=["with-pipeline", "without-pipeline"],
    )
    def test_execute_never_calls_backend(self, state: dict[str, Any]) -> None:
        """execute 产出 system_message，不产出 compression_messages，
        且全程未发生任何后端 search 调用（空块也不查）。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        mod.set_memory_backend(backend)
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(state)

        updates = _run(plugin._do_work(ctx))

        assert "compression_messages" not in updates, "压缩块不再经本插件装配"
        assert "system_message" in updates
        assert backend.search_calls == [], "读路径必须零后端调用"

    def test_no_backend_at_all_still_builds(self) -> None:
        """未注入 backend → 构建照常完成（无压缩装配路径可走）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"pipeline_id": "pipe-1", "user_id": "u-1"})

        updates = _run(plugin._do_work(ctx))

        assert "system_message" in updates
        assert "compression_messages" not in updates


# ═══════════════════════════════════════════════════════════
# 2. routed 条件注入（按 state 键值路由，2026-08-15 增强）
# ═══════════════════════════════════════════════════════════


class TestRoutedVar:
    """routed 变量：精确/通配/布尔规范化/_default 兜底。"""

    def test_exact_match(self) -> None:
        """精确匹配：state 值命中的 routes 键内容直接返回。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "llm_call"})
        var_def = {
            "route_key": "core_type",
            "routes": {
                "llm_call": "LLM 轮次提示",
                "tool_execute": "工具轮次提示",
                "_default": "兜底提示",
            },
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "LLM 轮次提示"

    def test_default_fallback(self) -> None:
        """未命中 → _default 兜底。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "other"})
        var_def = {
            "route_key": "core_type",
            "routes": {"llm_call": "A", "_default": "兜底"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "兜底"

    def test_wildcard_match(self) -> None:
        """fnmatch 通配键：deepseek-* 命中 state 值；_default 不参与通配。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"model_tier": "large"})
        var_def = {
            "route_key": "model_tier",
            "routes": {"large*": "大档系", "small": "小档", "_default": "兜底"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "大档系"

    def test_boolean_normalization(self) -> None:
        """布尔规范化：yaml 键 true 匹配 state 布尔 True（含 routes 键本身是布尔）。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"ended": True})
        var_def = {
            "route_key": "ended",
            "routes": {True: "已结束", False: "进行中"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "已结束"

    def test_nested_content_dict(self) -> None:
        """routes 值为 dict 时按 content 字段返回（嵌套定义）。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "llm_call"})
        var_def = {
            "route_key": "core_type",
            "routes": {"llm_call": {"type": "content", "content": "嵌套内容"}},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "嵌套内容"


# ═══════════════════════════════════════════════════════════
# 未知占位符可观测（兜底反模式审查 P2，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestUnknownPlaceholderWarns:
    """P2：未识别占位符替换为空串必须 warning 留痕（含原文）。"""

    def test_unknown_placeholder_warns_with_original_text(self, caplog) -> None:
        """拼错的占位符 → 空串 + warning 含占位符原文。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, "timestmp"))
        assert out == ""
        assert "未识别" in caplog.text and "timestmp" in caplog.text

    def test_empty_placeholder_warns(self, caplog) -> None:
        """空占位符（{{}}）同样未识别 → 留痕。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, ""))
        assert out == ""
        assert "未识别" in caplog.text

    def test_known_placeholder_no_warning(self, caplog) -> None:
        """合法占位符（session）正常解析且不触发未识别告警。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"context.session_id": "s-1"})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, "session"))
        assert out == "s-1"
        assert "未识别" not in caplog.text


# ═══════════════════════════════════════════════════════════
# 知识注入静默落空可观测（兜底反模式审查 P16，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestKnowledgeObservability:
    def test_memory_service_missing_warns(self, caplog) -> None:
        """P16：static_vars 声明知识注入但 memory_service 未注册 → warning。"""
        import logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin(config={})
        ctx = _make_ctx({})  # 无 services → get_service 抛 KeyError
        var_def = {
            "type": "retrieval",
            "name": "知识",
            "tags": ["a", "b"],
            "top_k": 3,
            "inject_type": "full",
        }
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(ctx, var_def, "sess-1"))
        assert out == "", "降级语义保持（空知识）"
        assert any("memory_service 未注册" in r.getMessage() for r in caplog.records)
