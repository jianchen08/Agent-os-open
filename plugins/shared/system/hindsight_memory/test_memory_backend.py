# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""IMemoryBackend 端口与 Hindsight 后端 + 工厂的 TDD 测试。

验证内容（KernelMemoryBackend 随内核记忆表退役删除，2026-08-19）：
1. IMemoryBackend 不可直接实例化（抽象端口）
2. HindsightBackend.add 调用 capability_caller，方法为 tool-executor.invoke，
   args 中含 tool_name="hindsight.retain"
3. HindsightBackend.search 把 hindsight recall 原始格式映射为统一 dict
   {id, content, score, memory_type, metadata}
4. HindsightBackend.add 在 capability_caller 抛错时优雅降级，返回空串不崩溃
5. get_memory_backend 默认返回 HindsightBackend
6. get_memory_backend(config backend="kernel") 抛 ValueError（退役后端 fail loudly）
7. get_memory_backend(capability_caller=None) 抛 ValueError

唯一外部依赖是注入的 capability_caller（AsyncMock），不引入 hindsight 包或重依赖。

[来源: docs/tasks Step 3 IMemoryBackend 端口 + 工厂]
"""

from __future__ import annotations

import importlib.util
import json
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

    async def test_hindsight_add_unwraps_invoke_envelope(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """tool-executor.invoke 经内核 invoker 归一把业务 dict 包成
        {success:true, data:<业务>} 信封（invoker.rs 决策树 ③）——add 必须
        解 data 再取 id，否则恒取空（2026-08-19 e2e 实测"未返回 memory id"）。"""
        caller.return_value = {
            "success": True,
            "data": {"id": "mem-env-1", "stored": True, "metadata": {}},
        }
        backend = mod.HindsightBackend(caller)

        mem_id = await backend.add(user_id="user-1", content="hello")

        assert mem_id == "mem-env-1"

    async def test_hindsight_search_unwraps_invoke_envelope(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """search 同受信封包裹，需解 data 后映射，否则结果恒空。"""
        caller.return_value = {
            "success": True,
            "data": {
                "results": [
                    {"id": "m-e", "content": "beta", "score": 0.7,
                     "metadata": {"memory_type": "semantic"}}
                ],
                "total": 1,
            },
        }
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="user-1")

        assert len(results) == 1
        assert results[0]["id"] == "m-e"

    async def test_hindsight_add_raises_on_caller_error(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """capability_caller 抛错时 add() 上抛——吞错降级曾让 memory 工具
        把失败包装成 success:true 的假成功（2026-08-19 e2e 实测），已改诚实上抛。"""
        caller.side_effect = RuntimeError("boom")
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="hindsight.retain"):
            await backend.add(user_id="user-1", content="hello")

    async def test_hindsight_add_raises_on_degrade_response(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """hindsight sidecar 降级响应（{error, initialized: False}，无 id 键）
        必须视为失败上抛，不得静默返回空 id。"""
        caller.return_value = {
            "error": "hindsight not initialized",
            "initialized": False,
            "operation": "retain",
        }
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="降级"):
            await backend.add(user_id="user-1", content="hello")

    async def test_hindsight_add_raises_on_empty_id(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """响应无 error 但也无 id（写入未确认）必须失败——空 id 不是成功。"""
        caller.return_value = {"stored": True}
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="memory id"):
            await backend.add(user_id="user-1", content="hello")


# ═══════════════════════════════════════════════════════════
# 8. add metadata 键值全 str（缺陷①：tags list 进 wire metadata 必炸）
# ── hindsight-client 0.9.x aretain metadata 是 dict[str, str] pydantic 校验，
#    tags 以 list 塞进 metadata → ValidationError → 报告从未真正持久化
#    （2026-08-19 批 C 取证，真实 API A/B 对照复现）。
# ═══════════════════════════════════════════════════════════


class TestAddMetadataStrSafety:
    async def test_add_serializes_tags_to_str(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """红①：add 装配的 wire metadata 键值必须全 str——tags 序列化为 JSON 串。"""
        caller.return_value = {"id": "mem-t1", "stored": True}
        backend = mod.HindsightBackend(caller)

        await backend.add(
            user_id="review",
            content="hello",
            memory_type="review",
            tags=["review_id:r1", "review_report"],
            source="review_agent",
        )

        meta = caller.call_args.args[1]["args"]["metadata"]
        assert isinstance(meta["tags"], str), (
            f"metadata.tags 必须是 str，实际 {type(meta['tags']).__name__}"
        )
        assert json.loads(meta["tags"]) == ["review_id:r1", "review_report"]
        assert meta["source"] == "review_agent"
        # pydantic dict[str,str] 校验面：所有值都是 str
        assert all(isinstance(v, str) for v in meta.values()), meta

    async def test_add_without_tags_keeps_no_tags_key(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """tags 缺省时不产生 tags 键（既有语义保留）。"""
        caller.return_value = {"id": "mem-t2", "stored": True}
        backend = mod.HindsightBackend(caller)

        await backend.add(user_id="u", content="c", source="s")

        meta = caller.call_args.args[1]["args"]["metadata"]
        assert "tags" not in meta

    async def test_add_merges_extra_metadata_str_coerced(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """红②：add 可选 metadata 参数合并进 wire metadata（review_id 定向键），
        非 str 值一律强转 str（校验面安全）。"""
        caller.return_value = {"id": "mem-t3", "stored": True}
        backend = mod.HindsightBackend(caller)

        await backend.add(
            user_id="review",
            content="c",
            memory_type="review",
            tags=["t"],
            source="review_agent",
            metadata={"review_id": "r1", "count": 5},
        )

        meta = caller.call_args.args[1]["args"]["metadata"]
        assert meta["review_id"] == "r1"
        assert meta["count"] == "5"
        assert all(isinstance(v, str) for v in meta.values()), meta

    async def test_add_extra_metadata_does_not_override_tags(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """调用方 metadata 里的同名键不得覆盖 add 自身装配的 tags/source 语义键。"""
        caller.return_value = {"id": "mem-t4", "stored": True}
        backend = mod.HindsightBackend(caller)

        await backend.add(
            user_id="u", content="c", tags=["real-tag"],
            source="review_agent", metadata={"tags": "caller-injected"},
        )

        meta = caller.call_args.args[1]["args"]["metadata"]
        assert json.loads(meta["tags"]) == ["real-tag"]


# ═══════════════════════════════════════════════════════════
# 9. get_documents（缺陷②冷读面）：经 tool-executor 调
#    hindsight.get_documents 按 bank/tags 精确取回文档原文。
# ═══════════════════════════════════════════════════════════


class TestGetDocuments:
    async def test_get_documents_invokes_tool(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """红③：get_documents 经 tool-executor.invoke 调 hindsight.get_documents，
        透传 bank/tags/tags_match。"""
        caller.return_value = {
            "documents": [
                {"id": "d1", "original_text": "{}", "document_metadata": {}}
            ],
            "total": 1,
        }
        backend = mod.HindsightBackend(caller)

        docs = await backend.get_documents(
            user_id="review", tags=["review_id:r1"]
        )

        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "hindsight.get_documents"
        assert params["plugin_id"] == "hindsight_memory_service"
        assert params["args"]["bank_id"] == "review"
        assert params["args"]["tags"] == ["review_id:r1"]
        assert params["args"]["tags_match"] == "any_strict"
        assert len(docs) == 1
        assert docs[0]["id"] == "d1"

    async def test_get_documents_by_exact_id(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """document_id 精确取回（单文档直查模式）。"""
        caller.return_value = {
            "documents": [{"id": "doc-9", "original_text": "raw"}],
            "total": 1,
        }
        backend = mod.HindsightBackend(caller)

        docs = await backend.get_documents(user_id="review", document_id="doc-9")

        args = caller.call_args.args[1]["args"]
        assert args["document_id"] == "doc-9"
        assert docs[0]["original_text"] == "raw"

    async def test_get_documents_unwraps_invoke_envelope(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """tool-executor 信封 {success, data} 解包后取 documents。"""
        caller.return_value = {
            "success": True,
            "data": {"documents": [{"id": "d-e"}], "total": 1},
        }
        backend = mod.HindsightBackend(caller)

        docs = await backend.get_documents(user_id="review")
        assert docs[0]["id"] == "d-e"

    async def test_get_documents_empty_and_malformed_degrade_to_empty(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """空结果/非 dict 响应降级为 []，不抛。"""
        backend = mod.HindsightBackend(caller)
        caller.return_value = {"documents": [], "total": 0}
        assert await backend.get_documents(user_id="review") == []
        caller.return_value = ["not", "a", "dict"]
        assert await backend.get_documents(user_id="review") == []

    async def test_get_documents_raises_on_caller_error(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """能力调用失败诚实上抛（与 search 同风格，调用方决定降级）。"""
        caller.side_effect = RuntimeError("boom")
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="hindsight.get_documents"):
            await backend.get_documents(user_id="review")

    async def test_get_documents_raises_on_degrade_signature(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """sidecar 降级响应（error/initialized:false）必须视为失败上抛。"""
        caller.return_value = {
            "error": "hindsight not initialized",
            "initialized": False,
            "operation": "get_documents",
        }
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="降级"):
            await backend.get_documents(user_id="review")


# ═══════════════════════════════════════════════════════════
# 5-7. KernelMemoryBackend
# ═══════════════════════════════════════════════════════════



class TestFactory:
    async def test_factory_returns_hindsight_by_default(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """get_memory_backend(config={}) 默认返回 HindsightBackend。"""
        backend = mod.get_memory_backend(config={}, capability_caller=caller)
        assert isinstance(backend, mod.HindsightBackend)

    async def test_factory_kernel_backend_retired(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """config backend="kernel" 抛 ValueError——内核记忆表后端已退役（2026-08-19）。"""
        with pytest.raises(ValueError, match="已退役"):
            mod.get_memory_backend(
                config={"backend": "kernel"}, capability_caller=caller
            )

    def test_factory_requires_caller(self, mod: Any) -> None:
        """capability_caller=None 时抛 ValueError。"""
        with pytest.raises(ValueError):
            mod.get_memory_backend(capability_caller=None)


# ═══════════════════════════════════════════════════════════
# 8-22 真机测试修复回归：search 过滤接线 + delete/add document_id
# ═══════════════════════════════════════════════════════════


class TestSearchFilters:
    async def test_search_passes_tags_and_session(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """search 的 tags/tags_match/session_id 必须投给 recall（修复前 filter
        除 memory_type 外全被丢弃）；session_id 转为 session:<id> 标签过滤，
        隔离键始终是 user_id 对应 bank。"""
        caller.return_value = {"results": [], "total": 0}
        backend = mod.HindsightBackend(caller)

        await backend.search(
            query="q", user_id="user-1", tags=["食谱"],
            tags_match="any_strict", session_id="sess-9",
        )

        _, params = caller.call_args.args
        args = params["args"]
        assert args["bank_id"] == "user-1"
        assert args["tags"] == ["食谱", "session:sess-9"]
        assert args["tags_match"] == "any_strict"

    async def test_search_filters_knowledge_name_client_side(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """knowledge_name 客户端过滤：只保留 metadata.knowledge_name 匹配项。"""
        caller.return_value = {
            "results": [
                {"id": "a", "content": "x", "score": 1.0,
                 "metadata": {"memory_type": "semantic", "knowledge_name": "kb1"}},
                {"id": "b", "content": "y", "score": 1.0,
                 "metadata": {"memory_type": "semantic"}},
            ],
            "total": 2,
        }
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="u", knowledge_name="kb1")

        assert [r["id"] for r in results] == ["a"]

    async def test_add_passes_document_id(
        self, mod: Any, caller: AsyncMock
    ) -> None:
        """add 的 document_id 透传到 hindsight.retain args（定向删除通路）。"""
        caller.return_value = {"id": "mem-doc-1", "stored": True}
        backend = mod.HindsightBackend(caller)

        mem_id = await backend.add(user_id="u", content="c", document_id="mem-doc-1")

        _, params = caller.call_args.args
        assert params["args"]["document_id"] == "mem-doc-1"
        assert mem_id == "mem-doc-1"
