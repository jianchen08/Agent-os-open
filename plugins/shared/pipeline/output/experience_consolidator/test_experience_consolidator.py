# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""experience_consolidator 输出插件 TDD 测试（Step 5c 重建）。

验证内容（与任务规格 7 个用例对齐，另加 add 失败降级 1 个）：
1. test_execute_without_backend_noop —— 未注入 memory backend → experience_consolidated=False，不崩
2. test_execute_task_not_complete_skips —— 任务未完成 → 空 OutputResult，不调 backend
3. test_execute_consolidates_chunks —— mock backend.search 返回 chunk，按 pipeline 标签过滤，
   逐条 add → knowledge_ids 2 条、experience_consolidated=True
4. test_execute_extracts_knowledge_content —— chunk content + keywords 都进入知识内容
5. test_execute_no_chunks_noop —— backend.search 返回 [] → experience_consolidated=False
6. test_execute_backend_error_degrades —— backend.search 抛异常 → 返回 False，不崩
7. test_set_memory_backend —— 注入 setter 生效，后续 execute 使用注入的后端
8. test_execute_backend_add_error_degrades —— backend.add 抛异常 → 返回 False，不崩

测试不依赖真实能力后端——通过 FakeBackend（duck-typed IMemoryBackend）注入。
每条用例动态重载 plugin.py 模块，避免模块级 _memory_backend 状态跨测试污染。

[来源: docs/tasks Step 5c 经验沉淀插件重建]
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
# 必须在 `from pipeline.types import ...` 之前执行，保证 pipeline 包可解析
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.types import StateKeys  # noqa: E402


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级 _memory_backend 状态跨测试污染）。"""
    mod_name = "experience_consolidator_plugin_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
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


def _completed_state(pipeline_id: str = "pipe-1") -> dict[str, Any]:
    """构造任务已完成的最小 state（触发经验沉淀）。"""
    return {
        StateKeys.EXECUTION_STATUS: "completed",
        StateKeys.PIPELINE_ID: pipeline_id,
        "user_id": "user-1",
    }


def _chunk_item(
    chunk_id: str,
    content: str,
    *,
    keywords: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """构造 backend.search 统一形态 chunk 条目（{id, content, metadata}）。"""
    return {
        "id": chunk_id,
        "content": content,
        "score": 1.0,
        "memory_type": "chunk",
        "metadata": {"tags": tags or []},
        **({"keywords": keywords} if keywords else {}),
    }


class FakeBackend:
    """记录 add/search 调用的伪 IMemoryBackend（duck-typed，无需继承 ABC）。

    - search_returns: search 返回的条目列表
    - add_returns: add 依次返回的 memory id 列表；耗尽后自动生成 mem-N
    - search_raises / add_raises: 置为异常时对应方法抛错（模拟能力失败）
    """

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []
        self.search_returns: list[dict[str, Any]] = []
        self.add_returns: list[str] = []
        self.search_raises: Exception | None = None
        self.add_raises: Exception | None = None

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        if self.search_raises is not None:
            raise self.search_raises
        return list(self.search_returns)

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        if self.add_raises is not None:
            raise self.add_raises
        if self.add_returns:
            return self.add_returns.pop(0)
        return f"mem-{len(self.add_calls)}"


# ═══════════════════════════════════════════════════════════
# 1. 未注入 backend → noop
# ═══════════════════════════════════════════════════════════


class TestWithoutBackend:
    def test_execute_without_backend_noop(self) -> None:
        """_memory_backend=None → state_updates 含 experience_consolidated=False，不崩。"""
        mod = _load_plugin_module()
        assert mod._memory_backend is None  # 模块初始未注入
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates.get("experience_consolidated") is False
        assert "knowledge_ids" not in result.state_updates


# ═══════════════════════════════════════════════════════════
# 2. 任务未完成 → 跳过
# ═══════════════════════════════════════════════════════════


class TestTaskNotComplete:
    def test_execute_task_not_complete_skips(self) -> None:
        """execution_status != completed → 空 OutputResult，不调 backend。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        state = {
            StateKeys.EXECUTION_STATUS: "running",
            StateKeys.PIPELINE_ID: "pipe-1",
        }
        ctx = mod.PluginContext(state=state)
        result = _run(plugin.execute(ctx))
        assert result.state_updates == {}
        assert backend.search_calls == [], "任务未完成不应检索 backend"
        assert backend.add_calls == [], "任务未完成不应写入 backend"


# ═══════════════════════════════════════════════════════════
# 3. 正常沉淀：检索 → 按 pipeline 标签过滤 → 逐条 add
# ═══════════════════════════════════════════════════════════


class TestConsolidatesChunks:
    def test_execute_consolidates_chunks(self) -> None:
        """backend.search 返回 2 条本管道 chunk（+1 条他管道 chunk 应被过滤），
        backend.add 返回 id → knowledge_ids 2 条、experience_consolidated=True。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_returns = [
            _chunk_item("c1", "块一内容", keywords=["kw1"], tags=["L1", "pipeline:pipe-1"]),
            _chunk_item("c2", "块二内容", keywords=["kw2"], tags=["L2", "pipeline:pipe-1"]),
            # 他管道 chunk：metadata.tags 不包含 pipeline:pipe-1，必须被过滤掉
            _chunk_item("c9", "别管道的块", tags=["L1", "pipeline:pipe-999"]),
        ]
        backend.add_returns = ["mem-1", "mem-2"]
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))

        assert result.state_updates["experience_consolidated"] is True
        assert result.state_updates["knowledge_ids"] == ["mem-1", "mem-2"]
        assert result.state_updates["knowledge_id"] == "mem-2"  # 最后一个

        # 检索参数：chunk 类型 + user_id + top_k=50
        assert backend.search_calls, "应调用 backend.search"
        search_kwargs = backend.search_calls[0]
        assert search_kwargs["memory_type"] == "chunk"
        assert search_kwargs["user_id"] == "user-1"
        assert search_kwargs["top_k"] == 50

        # 只沉淀 2 条本管道 chunk（他管道 c9 被过滤）
        assert len(backend.add_calls) == 2
        assert backend.add_calls[0]["memory_type"] == "experience"
        assert backend.add_calls[0]["source"] == "consolidation"
        assert "pipeline:pipe-1" in backend.add_calls[0]["tags"]
        assert "source_chunk:c1" in backend.add_calls[0]["tags"]
        assert backend.add_calls[1]["tags"][0] == "pipeline:pipe-1" or any(
            t == "pipeline:pipe-1" for t in backend.add_calls[1]["tags"]
        )


# ═══════════════════════════════════════════════════════════
# 4. 知识内容提炼
# ═══════════════════════════════════════════════════════════


class TestExtractsKnowledgeContent:
    def test_execute_extracts_knowledge_content(self) -> None:
        """chunk content + keywords 都进入 backend.add 的知识内容。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_returns = [
            _chunk_item(
                "c1",
                "实现了文件扫描模块",
                keywords=["扫描", "文件"],
                tags=["L1", "pipeline:pipe-1"],
            ),
        ]
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates["experience_consolidated"] is True

        assert len(backend.add_calls) == 1
        content = backend.add_calls[0]["content"]
        assert "实现了文件扫描模块" in content, "知识内容应包含 chunk content"
        assert "扫描" in content, "知识内容应包含 keywords"
        assert "文件" in content
        assert "层级: L1" in content, "知识内容应包含层级标注"


# ═══════════════════════════════════════════════════════════
# 5. 无 chunk → noop
# ═══════════════════════════════════════════════════════════


class TestNoChunks:
    def test_execute_no_chunks_noop(self) -> None:
        """backend.search 返回 [] → experience_consolidated=False，不调 add。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates["experience_consolidated"] is False
        assert backend.add_calls == []


# ═══════════════════════════════════════════════════════════
# 6. backend.search 失败 → 降级
# ═══════════════════════════════════════════════════════════


class TestBackendError:
    def test_execute_backend_error_degrades(self) -> None:
        """backend.search 抛异常 → 返回 experience_consolidated=False，不崩。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_raises = RuntimeError("search 能力不可用")
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates["experience_consolidated"] is False
        assert backend.add_calls == []


# ═══════════════════════════════════════════════════════════
# 7. set_memory_backend 注入
# ═══════════════════════════════════════════════════════════


class TestSetMemoryBackend:
    def test_set_memory_backend(self) -> None:
        """setter 注入生效：execute 使用注入的后端；传 None 恢复 noop。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_returns = [
            _chunk_item("c1", "内容", tags=["L1", "pipeline:pipe-1"]),
        ]
        mod.set_memory_backend(backend)
        assert mod._memory_backend is backend
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates["experience_consolidated"] is True
        assert backend.search_calls, "注入的后端应被 execute 使用"

        # 传 None 清空 → 回到 noop
        mod.set_memory_backend(None)
        assert mod._memory_backend is None
        result2 = _run(plugin.execute(ctx))
        assert result2.state_updates["experience_consolidated"] is False


# ═══════════════════════════════════════════════════════════
# 8. backend.add 失败 → 降级（与 0.1 逐条容错语义一致）
# ═══════════════════════════════════════════════════════════


class TestAddError:
    def test_execute_backend_add_error_degrades(self) -> None:
        """backend.add 抛异常 → 该条知识失败但不崩整体，返回 experience_consolidated=False。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_returns = [
            _chunk_item("c1", "内容一", tags=["L1", "pipeline:pipe-1"]),
        ]
        backend.add_raises = RuntimeError("写入能力不可用")
        mod.set_memory_backend(backend)
        plugin = mod.ExperienceConsolidatorPlugin()
        ctx = mod.PluginContext(state=_completed_state())
        result = _run(plugin.execute(ctx))
        assert result.state_updates["experience_consolidated"] is False
        assert "knowledge_ids" not in result.state_updates
