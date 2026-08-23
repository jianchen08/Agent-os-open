# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""memory_read plugin TDD 测试（Step 6 重建）。

验证内容（与任务规格 5 个用例对齐）：
1. test_execute_without_backend_empty —— 无后端 → state["memory.retrieved"]=[]
2. test_execute_retrieval_calls_search —— mock 后端 + inject_type=retrieval + user_message 已设 →
   backend.search 收到 query
3. test_execute_full_injects_all —— inject_type=full → 全部结果写入 state
4. test_cache_reuse —— 第二次 execute 复用缓存 state（不重复 search）
5. test_backend_error_degrades —— search 抛异常 → 空结果，不崩溃

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
    """动态加载 plugin.py 模块（每次新建，避免模块级 _memory_backend 跨测试污染）。

    用 module_from_spec + exec_module 直接重建，与 context_window_guard
    测试的加载方式一致。
    """
    mod_name = "memory_read_plugin_test"
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


class ErrorBackend(FakeBackend):
    """search 必抛异常的后端，验证降级路径。"""

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:  # type: ignore[override]
        self.search_calls.append(kwargs)
        raise RuntimeError("backend down")


_SAMPLE = {
    "id": "m1",
    "content": "记忆内容1",
    "score": 0.95,
    "memory_type": "semantic",
    "metadata": {"tags": []},
}


# ═══════════════════════════════════════════════════════════
# 1. 无后端 → 空结果
# ═══════════════════════════════════════════════════════════


class TestExecuteWithoutBackend:
    def test_execute_without_backend_empty(self) -> None:
        """未注入 _memory_backend → state["memory.retrieved"]=[]，不崩溃。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.MemoryReadPlugin()
        ctx = _make_ctx({"user_message": "你好", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["memory.retrieved"] == []


# ═══════════════════════════════════════════════════════════
# 2. RETRIEVAL 注入：按 query 检索
# ═══════════════════════════════════════════════════════════


class TestExecuteRetrieval:
    def test_execute_retrieval_calls_search(self) -> None:
        """inject_type=retrieval + user_message 已设 → backend.search 收到 query。"""
        mod = _load_plugin_module()
        backend = FakeBackend(results=[_SAMPLE])
        mod.set_memory_backend(backend)
        plugin = mod.MemoryReadPlugin(config={"inject_type": "retrieval", "top_k": 5, "memory_type": "semantic"})
        ctx = _make_ctx({"user_message": "怎么配置记忆", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert backend.search_calls, "应调用 backend.search"
        call = backend.search_calls[0]
        assert call["query"] == "怎么配置记忆"
        assert call["user_id"] == "u-1"
        assert call["top_k"] == 5
        assert call["memory_type"] == "semantic"
        # 后端结果写入 state（统一形态 id/content/score/memory_type/metadata），
        # 每条追加 _context_form="recall" 语义标记（压缩优化任务 1：内部字段，
        # 声明"从记忆库检索的内容"，压缩链路差异化摘要用）
        retrieved = result.state_updates["memory.retrieved"]
        assert retrieved == [
            {**_SAMPLE, "_context_form": "recall"},
        ]


# ═══════════════════════════════════════════════════════════
# 3. FULL 注入：全量取回
# ═══════════════════════════════════════════════════════════


class TestExecuteFull:
    def test_execute_full_injects_all(self) -> None:
        """inject_type=full → 全部结果写入 state，search 用空 query。"""
        mod = _load_plugin_module()
        backend = FakeBackend(results=[_SAMPLE, dict(_SAMPLE, id="m2", content="记忆内容2")])
        mod.set_memory_backend(backend)
        plugin = mod.MemoryReadPlugin(config={"inject_type": "full", "top_k": 10})
        ctx = _make_ctx({"user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert len(result.state_updates["memory.retrieved"]) == 2
        assert backend.search_calls, "应调用 backend.search"
        assert backend.search_calls[0]["query"] == "", "FULL 注入用空 query 全量取回"
        assert backend.search_calls[0]["top_k"] == 10


# ═══════════════════════════════════════════════════════════
# 4. 缓存复用：后续轮次不重复检索
# ═══════════════════════════════════════════════════════════


class TestCacheReuse:
    def test_cache_reuse(self) -> None:
        """首轮检索结果写入 state 后，第二轮直接复用（不再 search）。"""
        mod = _load_plugin_module()
        backend = FakeBackend(results=[_SAMPLE])
        mod.set_memory_backend(backend)
        plugin = mod.MemoryReadPlugin()
        ctx = _make_ctx({"user_message": "q", "user_id": "u-1"})

        result1 = _run(plugin.execute(ctx))
        # 模拟管道把 state_updates 合并进 state
        ctx.state.update(result1.state_updates)
        result2 = _run(plugin.execute(ctx))

        assert len(backend.search_calls) == 1, "第二轮不应再调用 backend.search"
        assert result2.state_updates["memory.retrieved"] == result1.state_updates["memory.retrieved"]


# ═══════════════════════════════════════════════════════════
# 5. 后端异常 → 降级为空，不崩溃
# ═══════════════════════════════════════════════════════════


class TestBackendError:
    def test_backend_error_degrades(self) -> None:
        """search 抛异常 → state["memory.retrieved"]=[] 且 error 记录，不崩溃。"""
        mod = _load_plugin_module()
        backend = ErrorBackend()
        mod.set_memory_backend(backend)
        plugin = mod.MemoryReadPlugin()
        ctx = _make_ctx({"user_message": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert result.state_updates["memory.retrieved"] == []
        assert result.error is not None


# ═══════════════════════════════════════════════════════════
# 6. SUMMARY 注入：caller → hindsight.summarize；无 caller / 出错降级拼接
# ═══════════════════════════════════════════════════════════


class FakeCaller:
    """记录调用的伪 capability_caller（async fn `(method, params) -> Any`）。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        return self._result


class TestSummaryInjection:
    def test_summary_calls_hindsight_summarize(self) -> None:
        """inject_type=summary + caller 注入 → 经 tool-executor 调 hindsight.summarize，注入整合摘要。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(FakeBackend(results=[_SAMPLE]))
        caller = FakeCaller({"summary": "整合后的记忆摘要：用户偏好 X", "recalled": 1})
        mod.set_capability_caller(caller)
        plugin = mod.MemoryReadPlugin(config={"inject_type": "summary", "top_k": 5, "memory_type": "semantic"})
        ctx = _make_ctx({"user_message": "总结一下我的偏好", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        assert caller.calls, "应调用 capability_caller"
        method, params = caller.calls[0]
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "hindsight.summarize"
        assert params["args"]["bank_id"] == "u-1"
        assert params["args"]["query"] == "总结一下我的偏好"
        assert params["args"]["memory_type"] == "semantic"
        assert result.state_updates["memory.retrieved"] == "整合后的记忆摘要：用户偏好 X"

    def test_summary_degrades_to_concat_when_caller_errors(self) -> None:
        """caller 返回 error → 降级为检索拼接（不抛异常）。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(FakeBackend(results=[_SAMPLE]))
        mod.set_capability_caller(FakeCaller({"error": "hindsight not initialized", "operation": "summarize"}))
        plugin = mod.MemoryReadPlugin(config={"inject_type": "summary", "top_k": 5})
        ctx = _make_ctx({"user_message": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        injected = result.state_updates["memory.retrieved"]
        assert isinstance(injected, str)
        assert "记忆内容1" in injected  # 拼接路径包含检索内容

    def test_summary_degrades_to_concat_without_caller(self) -> None:
        """无 caller → 直接降级拼接（保持原行为）。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(FakeBackend(results=[_SAMPLE]))
        mod.set_capability_caller(None)
        plugin = mod.MemoryReadPlugin(config={"inject_type": "summary", "top_k": 5})
        ctx = _make_ctx({"user_message": "q", "user_id": "u-1"})

        result = _run(plugin.execute(ctx))

        injected = result.state_updates["memory.retrieved"]
        assert isinstance(injected, str)
        assert "记忆内容1" in injected
