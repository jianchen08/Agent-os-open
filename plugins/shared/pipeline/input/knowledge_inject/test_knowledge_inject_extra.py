# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""knowledge_inject 插件补充测试（行覆盖 ≥90% 目标）。

覆盖既有 test_knowledge_inject.py 未触及的路径：
1. plugin.py：name/priority 属性、空 query、检索异常、空结果、
   _format_full/_format_compressed/_format_hint 三种格式化（含 token 上限截断）、
   _filter_by_relevance 空词集回退与部分命中排序
2. server.py：get_instance 单例缓存、on_load 有/无后端两分支、on_unload 清缓存、
   execute 工具对 dict / PluginResult(route_signal / skip_remaining) 的适配

[来源: 车道实测 knowledge_inject 58.6% → 补测]
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    """动态加载 plugin.py（唯一模块名，避免与裸名 plugin 及既有测试冲突）。"""
    mod_name = "knowledge_inject_plugin_extra_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_server_module() -> Any:
    """动态加载 server.py（先逐出裸名 plugin，防跨测试劫持）。"""
    mod_name = "knowledge_inject_server_extra_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules.pop("plugin", None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


class _FakeBackend:
    """可编程 search 的伪 IMemoryBackend。"""

    def __init__(self, results: list[dict[str, Any]] | None = None, exc: Exception | None = None) -> None:
        self._results = list(results or [])
        self._exc = exc
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str = "",
        user_id: str = "",
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"query": query, "user_id": user_id, "top_k": top_k, "memory_type": memory_type})
        if self._exc is not None:
            raise self._exc
        return list(self._results)


# ═══════════════════════════════════════════════════════════
# plugin.py：属性与 execute 边界
# ═══════════════════════════════════════════════════════════


class TestPluginMetadata:
    def test_name_and_priority(self) -> None:
        """插件标识与优先级（数据级 30，context_build 之后）。"""
        mod = _load_plugin_module()
        plugin = mod.KnowledgeInjectPlugin(config={})
        assert plugin.name == "knowledge_inject"
        assert plugin.priority == 30

    def test_config_defaults(self) -> None:
        """缺省配置：mode=disabled / top_k=5 / max_tokens=2000。"""
        mod = _load_plugin_module()
        plugin = mod.KnowledgeInjectPlugin()
        assert plugin._mode == "disabled"
        assert plugin._top_k == 5
        assert plugin._max_tokens == 2000


class TestExecuteEdgeCases:
    def test_empty_query_skips_search(self) -> None:
        """mode 非 disabled 但 current_query 为空 → 空 context，不调用后端。"""
        mod = _load_plugin_module()
        backend = _FakeBackend(results=[{"content": "x"}])
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_ctx({"user_id": "u-1"})  # 无 current_query

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""
        assert backend.search_calls == []

    def test_search_exception_returns_error(self) -> None:
        """后端 search 抛异常 → context 为空 + error 携带异常信息。"""
        mod = _load_plugin_module()
        backend = _FakeBackend(exc=RuntimeError("backend down"))
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_ctx({"current_query": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""
        assert result.error is not None and "backend down" in str(result.error)

    def test_empty_results_empty_context(self) -> None:
        """检索返回空列表 → context 为空。"""
        mod = _load_plugin_module()
        backend = _FakeBackend(results=[])
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_ctx({"current_query": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""

    def test_dict_and_object_results_joined(self) -> None:
        """混合 dict 与对象条目：content 均被提取并按空行拼接。"""
        mod = _load_plugin_module()
        backend = _FakeBackend(
            results=[
                {"content": "甲"},
                type("Obj", (), {"content": "乙"})(),
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_ctx({"current_query": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == "甲\n\n乙"


# ═══════════════════════════════════════════════════════════
# plugin.py：三种格式化
# ═══════════════════════════════════════════════════════════


class TestFormatFull:
    def test_full_joins_numbered_items(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 3, "max_tokens": 1000})
        out = plugin._format_full([{"content": "a"}, {"content": "b"}])
        assert out == "1. a\n2. b"

    def test_full_token_limit_breaks(self) -> None:
        """累计 token 超限即截断：首条即超 → 空；第二条超 → 只留首条。"""
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 5, "max_tokens": 10})
        # 首条 content 长 30 → 估算 15 > 10 → 立即 break
        assert plugin._format_full([{"content": "x" * 30}]) == ""
        # 首条 20 字符（估算 10）恰好不超，第二条 30 字符（估算 15）超 → 只留首条
        out = plugin._format_full([{"content": "y" * 20}, {"content": "z" * 30}])
        assert out == "1. " + "y" * 20

    def test_full_respects_top_k(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 1, "max_tokens": 1000})
        out = plugin._format_full([{"content": "a"}, {"content": "b"}])
        assert out == "1. a"


class TestFormatCompressed:
    def test_compressed_short_content_unchanged(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 2, "max_tokens": 1000})
        out = plugin._format_compressed([{"content": "短内容"}])
        assert out == "1. 短内容"

    def test_compressed_long_content_truncated(self) -> None:
        """超过 200 字符 → 截断加省略号。"""
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 2, "max_tokens": 1000})
        long_content = "长" * 300
        out = plugin._format_compressed([{"content": long_content}])
        assert out == "1. " + "长" * 200 + "..."

    def test_compressed_token_limit_breaks(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 5, "max_tokens": 5})
        # 首条 20 字符 → 摘要 20 字符 → 估算 10 > 5 → 空
        assert plugin._format_compressed([{"content": "y" * 20}]) == ""


class TestFormatHint:
    def test_hint_counts_and_lists_topics(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={})
        out = plugin._format_hint([{"content": "甲"}, {"content": "乙"}])
        assert out == "知识库中找到 2 条相关内容：\n- 甲\n- 乙"

    def test_hint_truncates_long_topic_and_caps_at_five(self) -> None:
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={})
        items = [{"content": "x" * 80}] + [{"content": f"t{i}"} for i in range(6)]
        out = plugin._format_hint(items)
        # 7 条内容：count 全量，topics 只取前 5
        assert out.startswith("知识库中找到 7 条相关内容：")
        assert "x" * 50 + "..." in out
        assert out.count("\n- ") == 5


# ═══════════════════════════════════════════════════════════
# plugin.py：相关性过滤边界
# ═══════════════════════════════════════════════════════════


class TestRelevanceFilterEdge:
    def test_single_char_query_words_falls_back_to_top_k(self) -> None:
        """query 全为单字符词 → 词集为空 → 原样返回前 top_k 条。"""
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 2})
        items = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
        out = plugin._filter_by_relevance(items, "a b")
        assert out == items[:2]

    def test_partial_hits_sorted_by_hit_count(self) -> None:
        """部分命中：零命中条目被过滤，命中数降序，同分保持原始顺序。"""
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 10})
        items = [
            {"content": "苹果 香蕉", "tags": []},
            {"content": "无关内容", "tags": []},
            {"content": "苹果", "tags": []},
        ]
        out = plugin._filter_by_relevance(items, "苹果 香蕉")
        assert [i["content"] for i in out] == ["苹果 香蕉", "苹果"]

    def test_tags_contribute_to_hits(self) -> None:
        """命中统计同时计入 tags 字段。"""
        plugin = _load_plugin_module().KnowledgeInjectPlugin(config={"top_k": 10})
        items = [{"content": "正文无关", "tags": ["量子"]}]
        out = plugin._filter_by_relevance(items, "量子")
        assert [i["content"] for i in out] == ["正文无关"]


# ═══════════════════════════════════════════════════════════
# server.py：单例与生命周期
# ═══════════════════════════════════════════════════════════


class TestServerLifecycle:
    def test_get_instance_cached_singleton(self) -> None:
        mod = _load_server_module()
        first = mod.get_instance()
        second = mod.get_instance()
        assert first is second
        assert isinstance(first, mod.KnowledgeInjectPlugin)

    def test_on_load_injects_backend(self, monkeypatch) -> None:
        mod = _load_server_module()
        fake_backend = object()
        monkeypatch.setattr(mod, "build_memory_backend", lambda plugin: fake_backend)
        injected: list[Any] = []
        monkeypatch.setattr(mod, "set_memory_backend", lambda b: injected.append(b))

        _run(mod._on_load({}))

        assert injected == [fake_backend]

    def test_on_load_without_backend_warns(self, monkeypatch, caplog) -> None:
        mod = _load_server_module()
        monkeypatch.setattr(mod, "build_memory_backend", lambda plugin: None)
        monkeypatch.setattr(mod, "set_memory_backend", lambda b: pytest.fail("不应注入 None 后端"))

        with caplog.at_level(logging.WARNING):
            _run(mod._on_load({}))

        assert any("记忆后端未注入" in r.getMessage() for r in caplog.records)

    def test_on_unload_clears_cache(self) -> None:
        mod = _load_server_module()
        first = mod.get_instance()
        _run(mod._on_unload({}))
        second = mod.get_instance()
        assert first is not second


# ═══════════════════════════════════════════════════════════
# server.py：execute 工具适配
# ═══════════════════════════════════════════════════════════


class _FakePlugin:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[Any] = []

    async def execute(self, ctx: Any) -> Any:
        self.calls.append(ctx)
        return self._result


class TestServerExecute:
    def _server_with_result(self, monkeypatch, result: Any) -> tuple[Any, _FakePlugin]:
        mod = _load_server_module()
        fake = _FakePlugin(result)
        monkeypatch.setattr(mod, "get_instance", lambda: fake)
        return mod, fake

    def test_execute_passthrough_dict(self, monkeypatch) -> None:
        """核心插件返回 dict → 原样透传。"""
        mod, fake = self._server_with_result(monkeypatch, {"state_updates": {"knowledge.context": "x"}})
        out = _run(mod.execute({"current_query": "q"}, None))
        assert out == {"state_updates": {"knowledge.context": "x"}}
        assert fake.calls[0].config == {}

    def test_execute_plugin_result_with_route_signal(self, monkeypatch) -> None:
        """PluginResult 带 route_signal → 展开为 route_signal 字典。"""
        from agentos_plugin_sdk.pipeline_types import PluginResult, RouteSignal

        mod, _ = self._server_with_result(
            monkeypatch,
            PluginResult(
                state_updates={"knowledge.context": "k"},
                route_signal=RouteSignal(route_type="next_llm", target="main", reason="有知识"),
            ),
        )
        out = _run(mod.execute({"current_query": "q"}))
        assert out["state_updates"] == {"knowledge.context": "k"}
        assert out["route_signal"] == {
            "route_type": "next_llm",
            "target": "main",
            "reason": "有知识",
        }
        assert "skip_remaining" not in out

    def test_execute_plugin_result_skip_remaining(self, monkeypatch) -> None:
        from agentos_plugin_sdk.pipeline_types import PluginResult

        mod, _ = self._server_with_result(
            monkeypatch,
            PluginResult(state_updates={}, skip_remaining=True),
        )
        out = _run(mod.execute({"current_query": "q"}))
        assert out == {"state_updates": {}, "skip_remaining": True}

    def test_execute_plugin_result_plain(self, monkeypatch) -> None:
        from agentos_plugin_sdk.pipeline_types import PluginResult

        mod, _ = self._server_with_result(monkeypatch, PluginResult(state_updates={"a": 1}))
        out = _run(mod.execute({"current_query": "q"}))
        assert out == {"state_updates": {"a": 1}}
