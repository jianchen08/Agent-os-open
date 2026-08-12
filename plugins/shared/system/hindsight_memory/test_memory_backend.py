# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: python-plugins-test
"""IMemoryBackend 端口与 Hindsight/Kernel 后端 + 工厂的 TDD 测试。

验证内容（与任务规格 10 个用例对齐）：
1. IMemoryBackend 不可直接实例化（抽象端口）
2. HindsightBackend.add 调用 capability_caller，方法为 tool-executor.invoke，
   args 中含 tool_name="hindsight.retain"
3. HindsightBackend.search 把 hindsight recall 原始格式映射为统一 dict
   {id, content, score, memory_type, metadata}
4. HindsightBackend.add 在 capability_caller 抛错时优雅降级，返回空串不崩溃
5. KernelMemoryBackend.add 调用方法 memory.create，参数为 MemoryRecord 形态
   （含 id/content/memory_type/tags/created_at）
6. KernelMemoryBackend.search 调用方法 memory.search
7. KernelMemoryBackend.import_document 把长文本切分为多块，多次调用 memory.create
8. get_memory_backend 默认返回 HindsightBackend
9. get_memory_backend(config backend="kernel") 返回 KernelMemoryBackend
10. get_memory_backend(capability_caller=None) 抛 ValueError

唯一外部依赖是注入的 capability_caller（AsyncMock），不引入 hindsight 包或重依赖。

[来源: docs/tasks Step 3 IMemoryBackend 端口 + 工厂]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 test_server.py 同款 setup）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 memory_backend.py 模块。

    用 module_from_spec + exec_module 直接加载，避免依赖包安装。
    """
    mod_name = "memory_backend_test"
    module_path = _PLUGIN_DIR / "memory_backend.py"
    assert module_path.exists(), f"memory_backend.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load memory_backend.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """加载 memory_backend 模块。"""
    return _load_module()


@pytest.fixture
def caller() -> AsyncMock:
    """注入一个 AsyncMock 作为 capability_caller。"""
    return AsyncMock()


# ═══════════════════════════════════════════════════════════
# 1. 端口抽象性
# ═══════════════════════════════════════════════════════════


class TestPortAbstract:
    def test_port_is_abstract(self, mod: Any) -> None:
        """IMemoryBackend 是 ABC，不能直接实例化。"""
        with pytest.raises(TypeError):
            mod.IMemoryBackend()


# ═══════════════════════════════════════════════════════════
# 2-4. HindsightBackend
# ═══════════════════════════════════════════════════════════


class TestHindsightBackend:
    async def test_hindsight_add_calls_retain(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """HindsightBackend.add 调用 tool-executor.invoke，
        args 中 tool_name="hindsight.retain"。"""
        caller.return_value = {"id": "mem-1", "stored": True}
        backend = mod.HindsightBackend(caller)

        mem_id = await backend.add(
            user_id="user-1", content="hello", memory_type="semantic"
        )

        assert mem_id == "mem-1"
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "hindsight.retain"
        assert params["args"]["content"] == "hello"
        assert params["args"]["bank_id"] == "user-1"

    async def test_hindsight_search_maps_results(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """search 把 hindsight recall 原始结果映射为统一 dict 形态。"""
        # hindsight recall 返回的原始条目形态（id/content/score/metadata.memory_type）
        caller.return_value = {
            "results": [
                {
                    "id": "m1",
                    "content": "alpha",
                    "score": 0.9,
                    "metadata": {"memory_type": "semantic", "tag": "x"},
                }
            ],
            "total": 1,
        }
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="user-1")

        assert len(results) == 1
        item = results[0]
        # 统一形态必须包含这些 key
        for key in ("id", "content", "score", "memory_type", "metadata"):
            assert key in item, f"missing key {key}"
        assert item["id"] == "m1"
        assert item["content"] == "alpha"
        assert item["score"] == 0.9
        assert item["memory_type"] == "semantic"

    async def test_hindsight_add_degrades_on_error(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """capability_caller 抛错时 add() 优雅降级，返回空串而非崩溃。"""
        caller.side_effect = RuntimeError("boom")
        backend = mod.HindsightBackend(caller)

        result = await backend.add(user_id="user-1", content="hello")

        assert result == ""


# ═══════════════════════════════════════════════════════════
# 5-7. KernelMemoryBackend
# ═══════════════════════════════════════════════════════════


class TestKernelMemoryBackend:
    async def test_kernel_add_calls_memory_create(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """KernelMemoryBackend.add 调用 memory.create，
        参数为 MemoryRecord 形态（含 id/content/memory_type/tags/created_at）。"""
        caller.return_value = {"ok": True}
        backend = mod.KernelMemoryBackend(caller)

        await backend.add(
            user_id="user-1", content="hello", memory_type="semantic", tags=["t1"]
        )

        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "memory.create"
        # MemoryRecord 必备字段
        for key in ("id", "content", "memory_type", "tags", "created_at"):
            assert key in params, f"missing MemoryRecord field {key}"
        assert params["content"] == "hello"
        assert params["memory_type"] == "semantic"
        assert params["tags"] == ["t1"]

    async def test_kernel_search_calls_memory_search(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """KernelMemoryBackend.search 调用方法 memory.search。"""
        caller.return_value = [
            {
                "id": "k1",
                "content": "foo",
                "memory_type": "semantic",
                "tags": [],
                "score": 0.5,
                "created_at": "2026-01-01",
            }
        ]
        backend = mod.KernelMemoryBackend(caller)

        results = await backend.search(query="foo", user_id="user-1", top_k=3)

        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "memory.search"
        assert params["query"] == "foo"
        assert params["top_k"] == 3
        # 仍映射为统一形态
        assert len(results) == 1
        for key in ("id", "content", "score", "memory_type", "metadata"):
            assert key in results[0]

    async def test_kernel_import_document_chunks(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """import_document 把长文本切分为多块，触发多次 memory.create。"""
        caller.return_value = {"ok": True}
        backend = mod.KernelMemoryBackend(caller)

        # 5000 字符文本，chunk_size 默认 2000 → 应切 3 块
        text = "x" * 5000
        result = await backend.import_document(user_id="user-1", text=text, name="doc")

        # 应有 3 次 memory.create
        assert caller.await_count == 3
        for call in caller.call_args_list:
            method, params = call.args
            assert method == "memory.create"
            assert "content" in params
        assert result["chunks_imported"] == 3


# ═══════════════════════════════════════════════════════════
# 8-10. 工厂
# ═══════════════════════════════════════════════════════════


class TestFactory:
    async def test_factory_returns_hindsight_by_default(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """get_memory_backend(config={}) 默认返回 HindsightBackend。"""
        backend = mod.get_memory_backend(config={}, capability_caller=caller)
        assert isinstance(backend, mod.HindsightBackend)

    async def test_factory_returns_kernel_when_configured(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """config backend="kernel" 返回 KernelMemoryBackend。"""
        backend = mod.get_memory_backend(
            config={"backend": "kernel"}, capability_caller=caller
        )
        assert isinstance(backend, mod.KernelMemoryBackend)

    def test_factory_requires_caller(self, mod: Any) -> None:
        """capability_caller=None 时抛 ValueError。"""
        with pytest.raises(ValueError):
            mod.get_memory_backend(capability_caller=None)
