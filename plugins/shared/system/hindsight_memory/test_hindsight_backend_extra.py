# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""HindsightBackend 补充覆盖测试（主用例见 test_memory_backend.py）。

覆盖未达分支：
1. add 的 update_mode 透传 / 非 dict 返回上抛（非预期类型）；
2. search 的 memory_type 透传 / 调用失败上抛 / 降级响应上抛 /
   _map_hindsight_results 的 list 输入、非 dict 输入、非 dict 条目跳过；
3. get_documents 的 q 参数透传；
4. delete 全分支：带/不带 memory_id、dict 真假、非 dict、调用失败降级 False；
5. import_document 全分支：text/file_path/name 装配、信封解包、失败降级、非 dict 降级。

同一行为 ≥2 组区分度输入；mock 仅限 capability_caller（外部能力边界）。

[来源: plugins/shared/system/hindsight_memory/memory_backend.py]
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

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 memory_backend.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_memory_backend_extra_test"
    path = _PLUGIN_DIR / "memory_backend.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load memory_backend.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


@pytest.fixture
def caller() -> AsyncMock:
    return AsyncMock()


# ═══════════════════════════════════════════════════════════
# add 分支补全
# ═══════════════════════════════════════════════════════════


class TestAddBranches:
    async def test_add_passes_update_mode(self, mod: Any, caller: AsyncMock) -> None:
        """document_id + update_mode 一并透传（服务端文档级替换语义）。"""
        caller.return_value = {"id": "mem-d1", "stored": True}
        backend = mod.HindsightBackend(caller)

        mem_id = await backend.add(
            user_id="u", content="c", document_id="mem-d1", update_mode="replace"
        )

        assert mem_id == "mem-d1"
        args = caller.call_args.args[1]["args"]
        assert args["document_id"] == "mem-d1"
        assert args["update_mode"] == "replace"

    async def test_add_without_document_id_omits_keys(self, mod: Any, caller: AsyncMock) -> None:
        """无 document_id/update_mode 时不产生对应键（参数面保持最小）。"""
        caller.return_value = {"id": "m", "stored": True}
        backend = mod.HindsightBackend(caller)

        await backend.add(user_id="u", content="c")

        args = caller.call_args.args[1]["args"]
        assert "document_id" not in args
        assert "update_mode" not in args

    async def test_add_raises_on_non_dict_result(self, mod: Any, caller: AsyncMock) -> None:
        """返回非 dict（如意外字符串）→ 诚实上抛（不伪造成成功）。"""
        caller.return_value = "ok"
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="非预期类型"):
            await backend.add(user_id="u", content="c")


# ═══════════════════════════════════════════════════════════
# search 分支补全
# ═══════════════════════════════════════════════════════════


class TestSearchBranches:
    async def test_search_passes_memory_type(self, mod: Any, caller: AsyncMock) -> None:
        """memory_type 透传给 recall args（服务端 type 过滤）。"""
        caller.return_value = {"results": [], "total": 0}
        backend = mod.HindsightBackend(caller)

        await backend.search(query="q", user_id="u", memory_type="episode")

        assert caller.call_args.args[1]["args"]["memory_type"] == "episode"

    async def test_search_raises_on_caller_error(self, mod: Any, caller: AsyncMock) -> None:
        """capability_caller 抛错 → 诚实上抛（降级决策归调用方）。"""
        caller.side_effect = RuntimeError("recall down")
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="hindsight.recall"):
            await backend.search(query="q", user_id="u")

    async def test_search_raises_on_degrade_response(self, mod: Any, caller: AsyncMock) -> None:
        """sidecar 降级签名（error/initialized:false）→ 上抛。"""
        caller.return_value = {"error": "hindsight not initialized", "initialized": False}
        backend = mod.HindsightBackend(caller)

        with pytest.raises(RuntimeError, match="降级"):
            await backend.search(query="q", user_id="u")

    async def test_search_maps_raw_list_result(self, mod: Any, caller: AsyncMock) -> None:
        """结果直接是 list（无 results 信封）→ 同样映射。"""
        caller.return_value = [
            {"id": "a", "content": "x", "score": "0.8", "memory_type": "episode"},
        ]
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="u")

        assert len(results) == 1
        item = results[0]
        assert item["id"] == "a"
        assert item["content"] == "x"
        assert item["score"] == 0.8  # 字符串分数转 float
        assert item["memory_type"] == "episode"

    async def test_search_empty_on_unknown_shape(self, mod: Any, caller: AsyncMock) -> None:
        """既非 dict 带 results 也非 list → 空列表（降级不崩溃）。"""
        caller.return_value = "unexpected"
        backend = mod.HindsightBackend(caller)

        assert await backend.search(query="q", user_id="u") == []

    async def test_search_skips_non_dict_items(self, mod: Any, caller: AsyncMock) -> None:
        """results 中非 dict 条目跳过（不炸映射）。"""
        caller.return_value = {"results": ["junk", {"id": "ok", "content": "c"}]}
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="u")

        assert [r["id"] for r in results] == ["ok"]

    async def test_search_memory_type_falls_back_to_item_level(self, mod: Any, caller: AsyncMock) -> None:
        """metadata 无 memory_type 时回落条目级 memory_type，再回落 'semantic'。"""
        caller.return_value = {
            "results": [
                {"id": "a", "content": "x", "metadata": {}},
                {"id": "b", "content": "y", "metadata": {}, "memory_type": "episode"},
            ]
        }
        backend = mod.HindsightBackend(caller)

        results = await backend.search(query="q", user_id="u")

        assert [r["memory_type"] for r in results] == ["semantic", "episode"]


# ═══════════════════════════════════════════════════════════
# get_documents 分支补全
# ═══════════════════════════════════════════════════════════


class TestGetDocumentsBranches:
    async def test_get_documents_passes_q(self, mod: Any, caller: AsyncMock) -> None:
        """q 子串过滤透传（document_id/tags 同时给定时并存）。"""
        caller.return_value = {"documents": [], "total": 0}
        backend = mod.HindsightBackend(caller)

        await backend.get_documents(
            user_id="review", document_id="d1", tags=["t1"], tags_match="exact", q="review"
        )

        args = caller.call_args.args[1]["args"]
        assert args["document_id"] == "d1"
        assert args["tags"] == ["t1"]
        assert args["tags_match"] == "exact"
        assert args["q"] == "review"

    async def test_get_documents_minimal_args(self, mod: Any, caller: AsyncMock) -> None:
        """仅 bank_id/limit/tags_match 缺省装配（可选键不产生）。"""
        caller.return_value = {"documents": [], "total": 0}
        backend = mod.HindsightBackend(caller)

        await backend.get_documents(user_id="u")

        args = caller.call_args.args[1]["args"]
        assert args == {"bank_id": "u", "limit": 20, "tags_match": "any_strict"}

    async def test_get_documents_filters_non_dict_entries(self, mod: Any, caller: AsyncMock) -> None:
        """documents 列表中非 dict 条目剔除。"""
        caller.return_value = {"documents": ["raw", {"id": "d", "original_text": "t"}]}
        backend = mod.HindsightBackend(caller)

        docs = await backend.get_documents(user_id="u")

        assert [d["id"] for d in docs] == ["d"]


# ═══════════════════════════════════════════════════════════
# delete 全分支
# ═══════════════════════════════════════════════════════════


class TestDeleteBranches:
    async def test_delete_with_memory_id(self, mod: Any, caller: AsyncMock) -> None:
        """memory_id 透传；dict 结果 deleted:true → True。"""
        caller.return_value = {"deleted": True, "memory_id": "m1"}
        backend = mod.HindsightBackend(caller)

        result = await backend.delete(user_id="u", memory_id="m1")

        assert result is True
        args = caller.call_args.args[1]["args"]
        assert args["bank_id"] == "u"
        assert args["memory_id"] == "m1"

    async def test_delete_without_memory_id_omits_key(self, mod: Any, caller: AsyncMock) -> None:
        """memory_id=None → 不产生 memory_id 键（删整个 bank 语义）。"""
        caller.return_value = {"deleted": False}
        backend = mod.HindsightBackend(caller)

        result = await backend.delete(user_id="u", memory_id=None)

        assert result is False
        args = caller.call_args.args[1]["args"]
        assert args == {"bank_id": "u"}

    async def test_delete_dict_deleted_false(self, mod: Any, caller: AsyncMock) -> None:
        """服务端显式 deleted:false → False（诚实透传）。"""
        caller.return_value = {"deleted": False, "error": "doc gone"}
        backend = mod.HindsightBackend(caller)

        assert await backend.delete(user_id="u", memory_id="m") is False

    async def test_delete_non_dict_result_true(self, mod: Any, caller: AsyncMock) -> None:
        """非 dict 响应（服务端语义成功，如 None）→ True（既有语义）。"""
        caller.return_value = None
        backend = mod.HindsightBackend(caller)

        assert await backend.delete(user_id="u") is True

    async def test_delete_caller_error_degrades_false(self, mod: Any, caller: AsyncMock) -> None:
        """调用失败 → 告警 + False（delete 走降级路径，不抛）。"""
        caller.side_effect = RuntimeError("delete down")
        backend = mod.HindsightBackend(caller)

        assert await backend.delete(user_id="u", memory_id="m") is False


# ═══════════════════════════════════════════════════════════
# import_document 全分支
# ═══════════════════════════════════════════════════════════


class TestImportDocumentBranches:
    async def test_import_assembles_all_args(self, mod: Any, caller: AsyncMock) -> None:
        """text/file_path/knowledge_name 全部装配透传。"""
        caller.return_value = {"chunks_imported": 3, "knowledge_name": "kb1"}
        backend = mod.HindsightBackend(caller)

        result = await backend.import_document(
            user_id="u", text="abc", file_path="x.md", name="kb1"
        )

        args = caller.call_args.args[1]["args"]
        assert args == {
            "bank_id": "u", "text": "abc", "file_path": "x.md", "knowledge_name": "kb1",
        }
        assert result == {"chunks_imported": 3, "knowledge_name": "kb1", "name": "kb1"}

    async def test_import_minimal_args(self, mod: Any, caller: AsyncMock) -> None:
        """仅 text 时可选键不产生。"""
        caller.return_value = {"chunks_imported": 1, "knowledge_name": "document"}
        backend = mod.HindsightBackend(caller)

        await backend.import_document(user_id="u", text="abc")

        args = caller.call_args.args[1]["args"]
        assert args == {"bank_id": "u", "text": "abc"}

    async def test_import_unwraps_envelope(self, mod: Any, caller: AsyncMock) -> None:
        """tool-executor 信封 {success, data} 解包后取业务字段。"""
        caller.return_value = {
            "success": True,
            "data": {"chunks_imported": 2, "knowledge_name": "kb2"},
        }
        backend = mod.HindsightBackend(caller)

        result = await backend.import_document(user_id="u", text="ab")

        assert result["chunks_imported"] == 2
        assert result["name"] == ""  # name 缺省空串

    async def test_import_caller_error_degrades(self, mod: Any, caller: AsyncMock) -> None:
        """调用失败 → 告警 + 降级 dict（不抛）。"""
        caller.side_effect = RuntimeError("import down")
        backend = mod.HindsightBackend(caller)

        result = await backend.import_document(user_id="u", text="abc", name="kb")

        assert result["chunks_imported"] == 0
        assert result["name"] == "kb"
        assert "error" in result

    async def test_import_non_dict_result_degrades(self, mod: Any, caller: AsyncMock) -> None:
        """非 dict 响应 → 降级 {chunks_imported: 0, name}（不抛）。"""
        caller.return_value = "unexpected"
        backend = mod.HindsightBackend(caller)

        result = await backend.import_document(user_id="u", text="abc", name="kb")

        assert result == {"chunks_imported": 0, "name": "kb"}

    async def test_import_result_dict_keeps_name_default(self, mod: Any, caller: AsyncMock) -> None:
        """业务 dict 缺 name 时补默认（调用方传入 name 优先）。"""
        caller.return_value = {"chunks_imported": 1}
        backend = mod.HindsightBackend(caller)

        result = await backend.import_document(user_id="u", text="abc", name="mine")

        assert result["name"] == "mine"
        assert result["chunks_imported"] == 1
