# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""knowledge_inject plugin TDD 测试（Step 6 重建）。

验证内容（与任务规格 3 个用例对齐）：
1. test_disabled_mode_skips —— mode=disabled → knowledge.context 为空，不调用后端
2. test_without_backend_empty —— 无后端 → knowledge.context=""
3. test_retrieval_writes_context —— mock 后端 search 返回结果 → knowledge.context 含内容

测试不依赖真实记忆后端——通过 FakeBackend 注入模块级 _memory_backend，
与 context_window_guard 的 set_memory_backend 模式保持一致。

[来源: docs/tasks Step 6 记忆插件接入 IMemoryBackend]
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
    mod_name = "knowledge_inject_plugin_test"
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
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"query": query, "user_id": user_id, "top_k": top_k, "memory_type": memory_type})
        return list(self.search_returns)


# ═══════════════════════════════════════════════════════════
# 1. disabled 模式跳过
# ═══════════════════════════════════════════════════════════


class TestDisabledMode:
    def test_disabled_mode_skips(self) -> None:
        """mode=disabled → knowledge.context 为空，且不调用后端。"""
        mod = _load_plugin_module()
        backend = FakeBackend(results=[])
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "disabled"})
        ctx = _make_ctx({"current_query": "x", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""
        assert backend.search_calls == [], "disabled 模式不应调用后端"


# ═══════════════════════════════════════════════════════════
# 2. 无后端 → 空
# ═══════════════════════════════════════════════════════════


class TestWithoutBackend:
    def test_without_backend_empty(self) -> None:
        """未注入 _memory_backend → knowledge.context=""，不崩溃。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_ctx({"current_query": "x", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""


# ═══════════════════════════════════════════════════════════
# 3. 检索结果写入 context
# ═══════════════════════════════════════════════════════════


class TestRetrievalWritesContext:
    def test_retrieval_writes_context(self) -> None:
        """mock 后端 search 返回结果 → knowledge.context 包含各条 content。"""
        mod = _load_plugin_module()
        backend = FakeBackend(
            results=[
                {
                    "id": "k1",
                    "content": "第一条知识",
                    "score": 0.9,
                    "memory_type": "semantic",
                    "metadata": {},
                },
                {
                    "id": "k2",
                    "content": "第二条知识",
                    "score": 0.8,
                    "memory_type": "semantic",
                    "metadata": {},
                },
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.KnowledgeInjectPlugin(config={"mode": "full", "top_k": 5})
        ctx = _make_ctx({"current_query": "记忆机制", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert "第一条知识" in result.state_updates["knowledge.context"]
        assert "第二条知识" in result.state_updates["knowledge.context"]
        assert backend.search_calls, "应调用 backend.search"
        call = backend.search_calls[0]
        assert call["query"] == "记忆机制"
        assert call["user_id"] == "u-1"
        assert call["top_k"] == 5
        assert call["memory_type"] == "semantic"
