# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""server.py 工具分支 + 环境装配 + 生命周期（on_load/on_unload）补充覆盖测试。

主用例见 test_hindsight_server.py / test_hindsight_server_http.py；本文件补：
1. 工具分支：retain（tags list 提升/update_mode/async 幂等 id/success 回落/异常降级）、
   recall（tags 过滤/结果形态归一/异常降级）、reflect（dict/str/异常）、
   summarize（dict recall 计数/空摘要/异常）、delete（无 documents API/无 deleter/
   dict 与非 dict 响应）、import_document（文件读取失败/无文本/异常）、
   get_documents（model_dump 条目/无 id 条目/单条取失败降级）；
2. _chunk_text 空文本；模块 sys.path 自插；
3. _load_env_file_keys / _apply_llm_env（环境变量装配）；
4. _ensure_memory_backend 懒注入（成功缓存/失败降级）；
5. on_load：既有服务复用、子进程 spawn（health 轮询）、venv 缺失降级；
6. on_unload：aclose/close 清理、terminate/kill 兜底。

mock 仅限外部依赖（hindsight client/子进程/网络/环境）；存储逻辑走真实实现。

[来源: plugins/shared/system/hindsight_memory/server.py]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_server_lifecycle_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    """调用插件工具并 await 协程结果。"""
    td = module.plugin._tools[tool_name]
    result = td.handler(**kwargs)
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(result)
        finally:
            loop.close()
    return result


@pytest.fixture
def srv() -> Any:
    """每个测试独立 server 模块实例（_client 初始 None）。"""
    module = _load_module()
    module._client = None
    module._api_process = None
    module._bank_default_warned = False
    return module


@pytest.fixture
def mock_client(srv: Any) -> MagicMock:
    """注入 mock hindsight client（与既有测试同款）。"""
    client = MagicMock()
    client.aretain = AsyncMock(return_value=MagicMock(operation_id="mem_mock", accepted=True))
    client.arecall = AsyncMock(return_value=MagicMock(results=[], chunks=[], source_facts=[]))
    client.areflect = AsyncMock(return_value=MagicMock(model_dump=lambda: {"facts": []}))
    client.adelete_bank = AsyncMock(return_value=None)
    client.acreate_bank = AsyncMock(return_value=None)
    srv._client = client
    return client


# ═══════════════════════════════════════════════════════════
# 基础 helpers
# ═══════════════════════════════════════════════════════════


class TestBasics:
    def test_chunk_text_empty(self, srv: Any) -> None:
        """空文本 → []（不产出空块）。"""
        assert srv._chunk_text("") == []

    def test_module_self_registers_on_sys_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """插件目录不在 sys.path 时 server.py 导入自插（本地模块可达性）。"""
        plugin_dir = str(_PLUGIN_DIR)
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != plugin_dir])

        assert plugin_dir not in sys.path
        _load_module()
        assert plugin_dir in sys.path


# ═══════════════════════════════════════════════════════════
# retain 工具分支补全
# ═══════════════════════════════════════════════════════════


class TestRetainBranches:
    def test_retain_promotes_list_tags(self, srv: Any, mock_client: MagicMock) -> None:
        """metadata.tags 为 list（直调工具方）→ 并入 aretain tags + 回写 JSON 串。"""
        result = _call_tool(
            srv, "hindsight.retain", bank_id="b", content="c",
            metadata={"tags": ["review_id:r1", "x", ""]},
        )
        assert result["stored"] is True
        kwargs = mock_client.aretain.call_args.kwargs
        assert "type:semantic" in kwargs["tags"]
        assert "review_id:r1" in kwargs["tags"]
        assert kwargs["metadata"]["tags"] == json.dumps(["review_id:r1", "x", ""], ensure_ascii=False)

    def test_retain_async_sends_operation_id(self, srv: Any, mock_client: MagicMock) -> None:
        """retain_async=True：operation_id 显式传入时原样透传（幂等 id）。"""
        _call_tool(srv, "hindsight.retain", bank_id="b", content="c", retain_async=True, operation_id="op-9")
        kwargs = mock_client.aretain.call_args.kwargs
        assert kwargs["retain_async"] is True
        assert kwargs["operation_id"] == "op-9"

    def test_retain_async_generates_operation_id(self, srv: Any, mock_client: MagicMock) -> None:
        """retain_async=True 且未传 operation_id → 服务端生成 uuid4（长度性质断言）。"""
        mock_client.aretain.return_value = MagicMock(operation_id="op-gen", accepted=True)
        result = _call_tool(srv, "hindsight.retain", bank_id="b", content="c", retain_async=True)
        op_id = mock_client.aretain.call_args.kwargs["operation_id"]
        assert len(op_id) == 36  # uuid4 标准长度
        assert op_id.count("-") == 4
        assert result["id"] == "op-gen"

    def test_retain_async_with_document_id_prefers_document_id(self, srv: Any, mock_client: MagicMock) -> None:
        """document_id 给定 → 返回 document_id（服务端原样落库锚点）。"""
        mock_client.aretain.return_value = MagicMock(operation_id="op-x", accepted=True)
        result = _call_tool(
            srv, "hindsight.retain", bank_id="b", content="c",
            document_id="doc-1", retain_async=True, update_mode="append",
        )
        kwargs = mock_client.aretain.call_args.kwargs
        assert kwargs["document_id"] == "doc-1"
        assert kwargs["update_mode"] == "append"
        assert result["id"] == "doc-1"

    def test_retain_sync_no_document_id_returns_real_id_when_present(self, srv: Any, mock_client: MagicMock) -> None:
        """同步无 document_id：aretain 回传真实落库 id → 原样透传。"""
        mock_client.aretain.return_value = SimpleNamespace(success=True, id="doc-real")
        result = _call_tool(srv, "hindsight.retain", bank_id="b", content="c")
        assert result["id"] == "doc-real"

    def test_retain_sync_without_document_id_never_returns_fake_id(self, srv: Any, mock_client: MagicMock) -> None:
        """同步无 document_id 且客户端未给真实 id → 返回空串，不伪造。

        operation_id 是调用方幂等 id，不是落库锚点——旧实现拿它顶 id 是
        "原样回传假 id"（delete/update 定向通路会打到不存在的锚点）。
        """
        mock_client.aretain.return_value = SimpleNamespace(success=True)
        result = _call_tool(srv, "hindsight.retain", bank_id="b", content="c", operation_id="caller-op")
        assert result["id"] == ""
        assert result["stored"] is True

    def test_retain_error_degrades(self, srv: Any, mock_client: MagicMock) -> None:
        """aretain 抛错 → {stored: False, error}（诚实失败）。"""
        mock_client.aretain.side_effect = RuntimeError("server down")
        result = _call_tool(srv, "hindsight.retain", bank_id="b", content="c")
        assert result["stored"] is False
        assert "server down" in result["error"]


# ═══════════════════════════════════════════════════════════
# recall 工具分支补全
# ═══════════════════════════════════════════════════════════


class TestRecallBranches:
    def test_recall_passes_explicit_tags(self, srv: Any, mock_client: MagicMock) -> None:
        """调用方 tags + tags_match 显式过滤（不转 type 过滤）。"""
        _call_tool(srv, "hindsight.recall", bank_id="b", query="q", tags=["t1"], tags_match="all_strict")
        kwargs = mock_client.arecall.call_args.kwargs
        assert kwargs["tags"] == ["t1"]
        assert kwargs["tags_match"] == "all_strict"

    def test_recall_maps_dict_and_scalar_items(self, srv: Any, mock_client: MagicMock) -> None:
        """results 条目归一：dict 原样、其余包 {content}；text 键转 content。"""
        mock_client.arecall.return_value = MagicMock(
            results=[{"id": "d1", "text": "alpha"}, "plain"], chunks=[], source_facts=[]
        )
        result = _call_tool(srv, "hindsight.recall", bank_id="b", query="q")
        assert result["total"] == 2
        assert result["results"][0] == {"id": "d1", "content": "alpha"}
        assert result["results"][1] == {"content": "plain"}

    def test_recall_error_degrades(self, srv: Any, mock_client: MagicMock) -> None:
        """arecall 抛错 → 空结果 + error（诚实失败，不崩溃）。"""
        mock_client.arecall.side_effect = RuntimeError("recall down")
        result = _call_tool(srv, "hindsight.recall", bank_id="b", query="q")
        assert result["results"] == []
        assert "recall down" in result["error"]


# ═══════════════════════════════════════════════════════════
# reflect / summarize 工具
# ═══════════════════════════════════════════════════════════


class TestReflectTool:
    def test_reflect_degrades_without_client(self, srv: Any) -> None:
        srv._client = None
        result = _call_tool(srv, "hindsight.reflect", bank_id="b")
        assert result["initialized"] is False

    def test_reflect_returns_model_dump(self, srv: Any, mock_client: MagicMock) -> None:
        mock_client.areflect.return_value = MagicMock(model_dump=lambda: {"reflection": "x"})
        result = _call_tool(srv, "hindsight.reflect", bank_id="b", query="q")
        assert result == {"reflection": "x"}
        assert mock_client.areflect.call_args.kwargs["query"] == "q"

    def test_reflect_returns_dict_and_scalar(self, srv: Any, mock_client: MagicMock) -> None:
        """dict 响应原样返回；其余按 str 包 {result}。"""
        mock_client.areflect.return_value = {"summary": "s"}
        assert _call_tool(srv, "hindsight.reflect", bank_id="b") == {"summary": "s"}
        mock_client.areflect.return_value = "plain-text"
        assert _call_tool(srv, "hindsight.reflect", bank_id="b") == {"result": "plain-text"}

    def test_reflect_error_degrades(self, srv: Any, mock_client: MagicMock) -> None:
        mock_client.areflect.side_effect = RuntimeError("reflect down")
        result = _call_tool(srv, "hindsight.reflect", bank_id="b")
        assert result == {"error": "reflect down"}

    def test_reflect_default_query(self, srv: Any, mock_client: MagicMock) -> None:
        """query 缺省 → 通用反思查询。"""
        _call_tool(srv, "hindsight.reflect", bank_id="b")
        assert mock_client.areflect.call_args.kwargs["query"]


class TestExtractSummaryText:
    def test_str_result_returned(self, srv: Any) -> None:
        assert srv._extract_summary_text("direct") == "direct"

    def test_dict_known_key_preferred(self, srv: Any) -> None:
        """dict：按已知键序取首个非空字符串值。"""
        assert srv._extract_summary_text({"summary": "sum", "text": "t"}) == "sum"
        assert srv._extract_summary_text({"text": "  ", "content": "c"}) == "c"

    def test_dict_scalar_values_joined(self, srv: Any) -> None:
        """无已知键：标量值（str/int/float）join 兜底。"""
        out = srv._extract_summary_text({"a": "x", "b": 3, "c": "y"})
        assert out == "x\n3\ny"

    def test_none_returns_empty(self, srv: Any) -> None:
        assert srv._extract_summary_text(None) == ""

    def test_object_falls_back_to_str(self, srv: Any) -> None:
        class Obj:
            def __str__(self) -> str:
                return "obj-repr"

        assert srv._extract_summary_text(Obj()) == "obj-repr"


class TestSummarizeBranches:
    def test_summarize_degrades_without_client(self, srv: Any) -> None:
        srv._client = None
        result = _call_tool(srv, "hindsight.summarize", bank_id="b")
        assert result["initialized"] is False

    def test_summarize_counts_dict_recall_results(self, srv: Any, mock_client: MagicMock) -> None:
        """recall 响应是 dict（results list）→ recalled 计数。

        [疑似 bug] dict 分支（recall 响应为 dict）计数不应用 top_k 钳制——
        现按现状断言（len(results)=3 全量计数），与 list 分支（min(len, top_k)）
        行为不一致；修复归属产品侧。
        """
        mock_client.arecall.return_value = {"results": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        mock_client.areflect.return_value = MagicMock(model_dump=lambda: {"summary": "s"})
        result = _call_tool(srv, "hindsight.summarize", bank_id="b", query="q", top_k=2)
        assert result["recalled"] == 3
        assert result["summary"] == "s"

    def test_summarize_dict_recall_no_results_list(self, srv: Any, mock_client: MagicMock) -> None:
        """dict 响应但 results 非 list → recalled 0。"""
        mock_client.arecall.return_value = {"results": "unexpected"}
        mock_client.areflect.return_value = MagicMock(model_dump=lambda: {"summary": "s"})
        result = _call_tool(srv, "hindsight.summarize", bank_id="b", query="q")
        assert result["recalled"] == 0

    def test_summarize_fallback_empty_summary(self, srv: Any, mock_client: MagicMock) -> None:
        """reflect 无摘要文本 → 占位文案（不报错）。"""
        mock_client.arecall.return_value = MagicMock(results=[], chunks=[])
        mock_client.areflect.return_value = MagicMock(model_dump=lambda: {})
        result = _call_tool(srv, "hindsight.summarize", bank_id="b", query="q")
        assert result["summary"] == "（无摘要内容）"
        assert result["recalled"] == 0

    def test_summarize_error_degrades(self, srv: Any, mock_client: MagicMock) -> None:
        """调用失败 → 降级 dict（含 error），不抛。"""
        mock_client.arecall.side_effect = RuntimeError("sum down")
        result = _call_tool(srv, "hindsight.summarize", bank_id="b")
        assert result["error"] == "sum down"
        assert result["operation"] == "summarize"


# ═══════════════════════════════════════════════════════════
# delete 工具分支补全
# ═══════════════════════════════════════════════════════════


class TestDeleteToolBranches:
    def test_delete_degrades_without_client(self, srv: Any) -> None:
        srv._client = None
        result = _call_tool(srv, "hindsight.delete", bank_id="b")
        assert result["initialized"] is False

    def test_delete_no_documents_api_errors(self, srv: Any, mock_client: MagicMock) -> None:
        """client 无 documents API → 明确失败（不静默删整库）。"""
        del mock_client.documents  # 移除 auto-child，模拟无 documents 面
        result = _call_tool(srv, "hindsight.delete", bank_id="b", memory_id="m1")
        assert result["deleted"] is False
        assert "delete_document" in result["error"]

    def test_delete_document_dict_response(self, srv: Any, mock_client: MagicMock) -> None:
        """delete_document 返回 dict → 读 success 键。"""
        mock_client.documents = MagicMock()
        mock_client.documents.delete_document = AsyncMock(return_value={"success": False})
        result = _call_tool(srv, "hindsight.delete", bank_id="b", memory_id="m1")
        assert result["deleted"] is False
        mock_client.documents.delete_document.assert_awaited_once_with(bank_id="b", document_id="m1")

    def test_delete_document_non_dict_response_success(self, srv: Any, mock_client: MagicMock) -> None:
        """delete_document 返回非 dict（服务端已删除）→ 视为成功。"""
        mock_client.documents = MagicMock()
        mock_client.documents.delete_document = AsyncMock(return_value=None)
        result = _call_tool(srv, "hindsight.delete", bank_id="b", memory_id="m1")
        assert result["deleted"] is True

    def test_delete_no_deleter_errors(self, srv: Any, mock_client: MagicMock) -> None:
        """无 memory_id 且 client 无任何删除方法 → 明确失败。"""
        del mock_client.adelete_bank
        del mock_client.adelete
        result = _call_tool(srv, "hindsight.delete", bank_id="b")
        assert result["deleted"] is False
        assert "no delete method" in result["error"]


# ═══════════════════════════════════════════════════════════
# import_document 工具分支补全
# ═══════════════════════════════════════════════════════════


class TestImportDocumentBranches:
    def test_import_degrades_without_client(self, srv: Any) -> None:
        srv._client = None
        result = _call_tool(srv, "hindsight.import_document", bank_id="b", text="x")
        assert result["initialized"] is False

    def test_import_file_read_failure(self, srv: Any, mock_client: MagicMock) -> None:
        """file_path 读取失败 → 错误 dict（chunks_imported 0）。"""
        result = _call_tool(
            srv, "hindsight.import_document", bank_id="b",
            file_path=str(_PLUGIN_DIR / "no_such_file_xyz.md"),
        )
        assert result["chunks_imported"] == 0
        assert "failed to read file" in result["error"]
        mock_client.aretain.assert_not_called()

    def test_import_no_text_provided(self, srv: Any, mock_client: MagicMock) -> None:
        """text 与 file_path 均无 → 错误 dict。"""
        result = _call_tool(srv, "hindsight.import_document", bank_id="b")
        assert result["chunks_imported"] == 0
        assert "no text provided" in result["error"]
        mock_client.aretain.assert_not_called()

    def test_import_empty_text_rejected(self, srv: Any, mock_client: MagicMock) -> None:
        """text 空串 → 同 no text provided。"""
        result = _call_tool(srv, "hindsight.import_document", bank_id="b", text="")
        assert result["chunks_imported"] == 0
        mock_client.aretain.assert_not_called()

    def test_import_error_degrades(self, srv: Any, mock_client: MagicMock) -> None:
        """aretain 中途抛错 → chunks_imported 0 + error。"""
        mock_client.aretain.side_effect = RuntimeError("ingest down")
        result = _call_tool(srv, "hindsight.import_document", bank_id="b", text="abc")
        assert result["chunks_imported"] == 0
        assert "ingest down" in result["error"]

    def test_import_reads_file(self, srv: Any, mock_client: MagicMock, tmp_path: Path) -> None:
        """file_path 真实文件 → 读取切块入库（真实文件系统）。"""
        doc = tmp_path / "doc.md"
        doc.write_text("abc", encoding="utf-8")
        result = _call_tool(
            srv, "hindsight.import_document", bank_id="b",
            file_path=str(doc), knowledge_name="kb-f",
        )
        assert result["chunks_imported"] == 1
        assert result["knowledge_name"] == "kb-f"
        assert mock_client.aretain.call_args.kwargs["content"] == "abc"


# ═══════════════════════════════════════════════════════════
# get_documents 工具分支补全
# ═══════════════════════════════════════════════════════════


class TestGetDocumentsToolBranches:
    def test_exact_id_model_dump_doc(self, srv: Any, mock_client: MagicMock) -> None:
        """document_id 直查：doc 为 model_dump 对象 → 转 dict。"""
        mock_client.documents = MagicMock()
        mock_client.documents.get_document = AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"id": "d1", "original_text": "raw"})
        )
        result = _call_tool(srv, "hindsight.get_documents", bank_id="b", document_id="d1")
        assert result["documents"] == [{"id": "d1", "original_text": "raw"}]

    def test_list_item_without_id_appended(self, srv: Any, mock_client: MagicMock) -> None:
        """list 条目无 id → 直接追加（不尝试取原文）。"""
        mock_client.documents = MagicMock()
        mock_client.documents.list_documents = AsyncMock(
            return_value=SimpleNamespace(items=[{"name": "no-id"}], total=1)
        )
        result = _call_tool(srv, "hindsight.get_documents", bank_id="b")
        assert result["total"] == 1
        assert result["documents"][0]["name"] == "no-id"
        mock_client.documents.get_document.assert_not_called()

    def test_list_per_item_fetch_failure_degrades_entry(self, srv: Any, mock_client: MagicMock) -> None:
        """单条原文取失败 → 返回 list 条目本身（不炸整个列举）。"""
        mock_client.documents = MagicMock()
        mock_client.documents.list_documents = AsyncMock(
            return_value=SimpleNamespace(items=[{"id": "d1", "tags": []}], total=1)
        )
        mock_client.documents.get_document = AsyncMock(side_effect=RuntimeError("db down"))
        result = _call_tool(srv, "hindsight.get_documents", bank_id="b")
        assert result["total"] == 1
        assert result["documents"][0]["id"] == "d1"
        assert "error" not in result

    def test_list_merged_fields(self, srv: Any, mock_client: MagicMock) -> None:
        """list 条目字段与 full 并集（full 未提供的键不丢）。"""
        mock_client.documents = MagicMock()
        mock_client.documents.list_documents = AsyncMock(
            return_value=SimpleNamespace(items=[{"id": "d1", "tags": ["t1"]}], total=1)
        )
        mock_client.documents.get_document = AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"id": "d1", "original_text": "raw"})
        )
        result = _call_tool(srv, "hindsight.get_documents", bank_id="b")
        assert result["documents"][0] == {"id": "d1", "original_text": "raw", "tags": ["t1"]}

    def test_list_q_and_limit_params(self, srv: Any, mock_client: MagicMock) -> None:
        """list 模式：q/limit/tags_match 参数透传。"""
        mock_client.documents = MagicMock()
        mock_client.documents.list_documents = AsyncMock(
            return_value=SimpleNamespace(items=[], total=0)
        )
        _call_tool(
            srv, "hindsight.get_documents", bank_id="b",
            tags=["t1"], tags_match="exact", q="sub", limit=50,
        )
        kwargs = mock_client.documents.list_documents.call_args.kwargs
        assert kwargs == {
            "bank_id": "b", "limit": 50, "tags": ["t1"], "tags_match": "exact", "q": "sub",
        }


# ═══════════════════════════════════════════════════════════
# 环境装配：_load_env_file_keys / _apply_llm_env
# ═══════════════════════════════════════════════════════════


def _patch_this_dir(srv: Any, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """把 _THIS_DIR 指到 root/a/b/c/hindsight_memory（.env 置于 root）。

    server._load_env_file_keys 自 _THIS_DIR 上溯 4 级定位项目根——
    真实布局 plugins/shared/system/hindsight_memory（4 层）↔ 测试用 3 层中间目录。
    """
    target = root / "a" / "b" / "c" / "hindsight_memory"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "_THIS_DIR", str(target))


class TestLoadEnvFileKeys:
    def test_extracts_target_keys(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """.env 读取：只取 ZHIPU/SILICONFLOW key，引号剥离，注释/无关行跳过。"""
        (tmp_path / ".env").write_text(
            "# comment\n"
            "OTHER_KEY=ignored\n"
            "ZHIPU_API_KEY=zk-123\n"
            "SILICONFLOW_API_KEY='sf-456'\n"
            "NO_EQUALS_LINE\n",
            encoding="utf-8",
        )
        _patch_this_dir(srv, monkeypatch, tmp_path)

        assert srv._load_env_file_keys() == {"ZHIPU_API_KEY": "zk-123", "SILICONFLOW_API_KEY": "sf-456"}

    def test_missing_env_file_returns_empty(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """无 .env → 空 dict（不炸；调用方继续降级）。"""
        _patch_this_dir(srv, monkeypatch, tmp_path)
        assert srv._load_env_file_keys() == {}


class TestApplyLlmEnv:
    def _clear_hindsight_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_BASE_URL",
            "HINDSIGHT_API_LLM_MODEL", "HINDSIGHT_API_LLM_API_KEY",
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY",
            "HINDSIGHT_API_RERANKER_PROVIDER",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_sets_defaults_and_wires_keys(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境无配置时：LLM/embedding 默认值写入 + 已有 key 接线。"""
        self._clear_hindsight_env(monkeypatch)
        monkeypatch.setenv("ZHIPU_API_KEY", "zk")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sf")

        srv._apply_llm_env()

        assert os.environ["HINDSIGHT_API_LLM_PROVIDER"] == "openai"
        assert os.environ["HINDSIGHT_API_LLM_MODEL"] == "glm-5.2"
        assert os.environ["HINDSIGHT_API_LLM_API_KEY"] == "zk"
        assert os.environ["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openai"
        assert os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"] == "BAAI/bge-m3"
        assert os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] == "sf"
        assert os.environ["HINDSIGHT_API_RERANKER_PROVIDER"] == "rrf"

    def test_preserves_existing_config(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """已有显式配置不被覆盖；无 key 时 API key 键不产生。"""
        self._clear_hindsight_env(monkeypatch)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        # 指向无 .env 的空目录：避免读到仓库真实 .env 的 key
        _patch_this_dir(srv, monkeypatch, tmp_path / "noenv")
        monkeypatch.setenv("HINDSIGHT_API_LLM_MODEL", "custom-model")
        monkeypatch.setenv("HINDSIGHT_API_RERANKER_PROVIDER", "bge-reranker")

        srv._apply_llm_env()

        assert os.environ["HINDSIGHT_API_LLM_MODEL"] == "custom-model"
        assert os.environ["HINDSIGHT_API_RERANKER_PROVIDER"] == "bge-reranker"
        assert "HINDSIGHT_API_LLM_API_KEY" not in os.environ
        assert "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY" not in os.environ

    def test_env_file_keys_fallback(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """进程 env 缺 key 时从 .env 补读（sidecar 自足）。"""
        self._clear_hindsight_env(monkeypatch)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "ZHIPU_API_KEY=zk-file\nSILICONFLOW_API_KEY=sf-file\n", encoding="utf-8"
        )
        _patch_this_dir(srv, monkeypatch, tmp_path)

        srv._apply_llm_env()

        assert os.environ["ZHIPU_API_KEY"] == "zk-file"
        assert os.environ["SILICONFLOW_API_KEY"] == "sf-file"
        assert os.environ["HINDSIGHT_API_LLM_API_KEY"] == "zk-file"
        assert os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] == "sf-file"


# ═══════════════════════════════════════════════════════════
# _ensure_memory_backend 懒注入
# ═══════════════════════════════════════════════════════════


class TestEnsureMemoryBackend:
    def test_builds_and_caches_backend(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """构建成功 → 返回实例且幂等（attempted 短路，不重建）。"""
        sentinel = object()
        stub = types.ModuleType("wiring")
        stub.build_memory_backend = MagicMock(return_value=sentinel)
        monkeypatch.setitem(sys.modules, "wiring", stub)

        first = srv._ensure_memory_backend()
        second = srv._ensure_memory_backend()

        assert first is sentinel
        assert second is sentinel
        stub.build_memory_backend.assert_called_once()

    def test_build_failure_returns_none(self, srv: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """构建抛错 → None + 告警（能力缺失降级不崩溃）。"""
        stub = types.ModuleType("wiring")
        stub.build_memory_backend = MagicMock(side_effect=RuntimeError("no capability"))
        monkeypatch.setitem(sys.modules, "wiring", stub)

        with caplog.at_level(logging.WARNING):
            assert srv._ensure_memory_backend() is None
        assert any("记忆后端构建失败" in r.getMessage() for r in caplog.records)

    def test_returns_none_without_caller(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """wiring 返回 None（能力未注入）→ 保持 None。"""
        stub = types.ModuleType("wiring")
        stub.build_memory_backend = MagicMock(return_value=None)
        monkeypatch.setitem(sys.modules, "wiring", stub)

        assert srv._ensure_memory_backend() is None


# ═══════════════════════════════════════════════════════════
# on_load / on_unload 生命周期
# ═══════════════════════════════════════════════════════════


class _FakeResponse:
    """urlopen 响应替身：支持 `with` 协议（server 用 `with urlopen() as resp`）。"""

    status = 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class TestOnLoad:
    def _fake_hindsight_client(self, monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
        client_mod = types.ModuleType("hindsight_client")
        client_mod.Hindsight = MagicMock(return_value=client)
        monkeypatch.setitem(sys.modules, "hindsight_client", client_mod)

    def test_reuses_existing_server(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """health 已 200 → 复用既有服务，不 spawn 子进程。"""
        client = MagicMock()
        client.acreate_bank = AsyncMock(return_value=None)
        self._fake_hindsight_client(monkeypatch, client)
        monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))

        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(return_value=_FakeResponse()))
        monkeypatch.setattr(subprocess, "Popen", MagicMock())
        srv.plugin._injected_config = {"default_bank_id": "tenant-1"}

        _run(srv._on_load({}))

        assert srv._client is client
        assert srv._api_process is None
        subprocess.Popen.assert_not_called()
        client.acreate_bank.assert_awaited_once_with(bank_id="tenant-1")

    def test_spawns_server_process(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """health 不通 → 用 .venv-hindsight python spawn 子进程，轮询就绪后建 client。"""
        client = MagicMock()
        client.acreate_bank = AsyncMock(return_value=None)
        self._fake_hindsight_client(monkeypatch, client)
        monkeypatch.setenv("HINDSIGHT_PORT", "18420")
        monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))

        # venv 探测平台中立化：生产先探 Windows 布局再回退 Unix 布局（isfile）。
        # CI Linux 上两布局都真实缺失会提前 raise；这里让 Windows 布局恒命中，
        # 测试聚焦 spawn 命令形状本身（其余路径走真实 isfile）。
        real_isfile = os.path.isfile

        def _fake_isfile(p: str | os.PathLike[str]) -> bool:
            s = str(p)
            if ".venv-hindsight" in s and s.replace("\\", "/").endswith("Scripts/python.exe"):
                return True
            return real_isfile(p)

        monkeypatch.setattr(os.path, "isfile", _fake_isfile)

        # 首次 health 探测失败（未起）→ 进入 spawn 路径；轮询首轮即 200
        probe_count = {"n": 0}

        def _flaky_urlopen(*args: Any, **kwargs: Any) -> Any:
            probe_count["n"] += 1
            if probe_count["n"] == 1:
                raise ConnectionError("down")
            return _FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=_flaky_urlopen))
        proc = MagicMock()
        proc.pid = 4242
        monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=proc))
        # 轮询 sleep 1s/次 → 测试中置零（外部时序依赖）
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        _run(srv._on_load({}))

        popen_args = subprocess.Popen.call_args.args[0]
        venv_python = str(_PLUGIN_DIR / ".venv-hindsight" / "Scripts" / "python.exe")
        assert popen_args == [venv_python, "-m", "hindsight_api.main", "--port", "18420", "--host", "127.0.0.1"]
        assert subprocess.Popen.call_args.kwargs["stdout"] == subprocess.DEVNULL
        assert srv._api_process is proc
        assert srv._client is client
        client.acreate_bank.assert_awaited_once()
        # stderr 落盘文件可诊断（append 模式）
        stderr_file = subprocess.Popen.call_args.kwargs["stderr"]
        assert stderr_file.mode == "ab"

    def test_wait_ready_closes_stderr_handle_on_success(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """就绪即返回 → 父进程侧 stderr 句柄关闭（子进程自持副本不受影响）。"""
        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(return_value=_FakeResponse()))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        log = tmp_path / "s.log"
        fh = open(log, "ab")
        _run(srv._wait_api_ready("http://127.0.0.1:18420", SimpleNamespace(poll=lambda: None), fh, str(log)))
        assert fh.closed

    def test_wait_ready_closes_stderr_handle_on_timeout(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """60s 未就绪超时抛错 → 句柄同样关闭（泄漏面收口，S15）。"""
        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=ConnectionError("down")))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        log = tmp_path / "s.log"
        fh = open(log, "ab")
        with pytest.raises(RuntimeError, match="60s 内未就绪"):
            _run(srv._wait_api_ready(
                "http://127.0.0.1:18420",
                SimpleNamespace(poll=lambda: None, returncode=None),
                fh, str(log),
            ))
        assert fh.closed

    def test_wait_ready_closes_stderr_handle_on_crash_and_tails_log(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """子进程提前退出 → 抛错带 stderr tail，句柄关闭。"""
        log = tmp_path / "s.log"
        log.write_bytes(b"panic-trace-" * 100)
        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=ConnectionError("down")))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        proc = SimpleNamespace(poll=lambda: 1, returncode=1)
        fh = open(log, "ab")
        with pytest.raises(RuntimeError) as ei:
            _run(srv._wait_api_ready("http://127.0.0.1:18420", proc, fh, str(log)))
        assert "code=1" in str(ei.value)
        assert "stderr_tail" in str(ei.value)
        assert "panic-trace" in str(ei.value)
        assert fh.closed

    def test_missing_venv_degrades(self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """venv python 缺失 → 初始化失败降级（_client=None + 告警），不崩。"""
        monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(
            urllib.request, "urlopen", MagicMock(side_effect=ConnectionError("down"))
        )
        monkeypatch.setattr(os.path, "isfile", MagicMock(return_value=False))

        with caplog.at_level(logging.WARNING):
            _run(srv._on_load({}))

        assert srv._client is None
        assert any("初始化失败" in r.getMessage() for r in caplog.records)

class TestOnUnload:
    def test_closes_client_and_terminates_process(self, srv: Any) -> None:
        """正常清理：aclose await + 子进程 terminate/wait。"""
        client = MagicMock()
        client.aclose = AsyncMock(return_value=None)
        srv._client = client
        proc = MagicMock()
        srv._api_process = proc

        _run(srv._on_unload({}))

        client.aclose.assert_awaited_once()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=10)
        assert srv._client is None
        assert srv._api_process is None

    def test_sync_close_fallback(self, srv: Any) -> None:
        """client 无 aclose → 回落同步 close。"""
        client = MagicMock()
        del client.aclose  # 移除 auto-child，模拟仅同步 close 的 client
        srv._client = client

        _run(srv._on_unload({}))

        client.close.assert_called_once()
        assert srv._client is None

    def test_cleanup_errors_still_clear_state(self, srv: Any, caplog: pytest.LogCaptureFixture) -> None:
        """aclose 抛错 + terminate 抛错 → 告警但状态仍清空（kill 兜底）。"""
        client = MagicMock()
        client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
        srv._client = client
        proc = MagicMock()
        proc.terminate.side_effect = RuntimeError("term failed")
        srv._api_process = proc

        with caplog.at_level(logging.WARNING):
            _run(srv._on_unload({}))

        proc.kill.assert_called_once()
        assert srv._client is None
        assert srv._api_process is None
        assert any("on_unload" in r.getMessage() for r in caplog.records)

    def test_unload_with_nothing_attached(self, srv: Any) -> None:
        """无 client 无子进程 → 幂等空操作。"""
        srv._client = None
        srv._api_process = None
        _run(srv._on_unload({}))
        assert srv._client is None
        assert srv._api_process is None


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
