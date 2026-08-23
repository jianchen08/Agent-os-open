# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""Hindsight 记忆 sidecar 插件 TDD 测试。

验证内容（与任务规格 9 个用例对齐）：
1. plugin.json manifest 必备字段
2. 5 个工具全部注册到 plugin 对象
3. _client 为 None 时 retain/recall 优雅降级（不崩溃）
4. mock client.retain 后调用工具，断言调用参数与返回形状
5. recall 按 memory_type 客户端过滤
6. import_document 将长文本切分为 ~3 块并 retain 3 次
7. import_document 拒绝非 txt/md 文件
8. bank_id 缺省时回落到默认值（多租户隔离 key）

测试不依赖真实 hindsight 包——通过 monkeypatch 模块级 _client 实现。

[来源: docs/tasks Step 2 Hindsight memory sidecar]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 源码加入 sys.path（与 plugins/test_system_plugins.py 同款 setup）
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，不依赖 importlib.reload
    （file-location spec 不可被 reload 重新查找，会抛 ModuleNotFoundError）。
    """
    mod_name = "hindsight_memory_server_test"
    plugin_path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    """调用插件工具并 await 协程结果（新建事件循环，避免 pytest-asyncio 冲突）。"""
    td = module.plugin._tools[tool_name]
    result = td.handler(**kwargs)
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()
    return result


@pytest.fixture
def mod() -> Any:
    """加载 server 模块，每个测试独立（重置 _client）。"""
    module = _load_module()
    module._client = None
    return module


@pytest.fixture
def mock_client(mod: Any) -> MagicMock:
    """注入 mock hindsight client 到模块。

    真实 hindsight_client.Hindsight 的方法都是异步的(aretain/arecall/areflect/...),
    故 mock 用 AsyncMock。返回值由各测试在 mock_client.aretain.return_value 上设置。
    """
    client = MagicMock()
    # 异步方法默认返回 None/空(各测试按需覆盖 return_value)
    client.aretain = AsyncMock(return_value=MagicMock(operation_id="mem_mock", accepted=True))
    client.arecall = AsyncMock(return_value=MagicMock(results=[], chunks=[], source_facts=[]))
    client.areflect = AsyncMock(return_value=MagicMock(model_dump=lambda: {"facts": []}))
    client.adelete_bank = AsyncMock(return_value=None)
    client.acreate_bank = AsyncMock(return_value=None)
    mod._client = client
    return client


# ═══════════════════════════════════════════════════════════
# 1. plugin.json manifest 校验
# ═══════════════════════════════════════════════════════════


class TestPluginManifest:
    def test_plugin_manifest_valid(self) -> None:
        """plugin.json 必须包含 id/plugin_type/host_type/entry 等必备字段。"""
        manifest_path = _PLUGIN_DIR / "plugin.json"
        assert manifest_path.exists(), "plugin.json must exist"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        # 必备字段
        assert data["id"] == "hindsight_memory_service"
        assert data["plugin_type"] == "system"
        assert data["host_type"] == "sidecar"
        # invoke 入口（entry 字段，python server.py 形式）
        entry = data.get("entry") or data.get("invoke", {}).get("entry")
        assert entry and "server.py" in entry, "entry must reference server.py"

        # capabilities.services 至少声明 5 个服务方法（D.6 槽位拆分）
        tool_names = [t["name"] for t in data.get("capabilities", {}).get("services", [])]
        for name in (
            "hindsight.retain",
            "hindsight.recall",
            "hindsight.reflect",
            "hindsight.delete",
            "hindsight.import_document",
            "hindsight.get_documents",
        ):
            assert name in tool_names, f"{name} missing in capabilities.services"


# ═══════════════════════════════════════════════════════════
# 2. 工具注册
# ═══════════════════════════════════════════════════════════


class TestToolRegistration:
    def test_tools_registered(self, mod: Any) -> None:
        """5 个 hindsight 工具全部注册到 plugin._tools。"""
        for name in (
            "hindsight.retain",
            "hindsight.recall",
            "hindsight.reflect",
            "hindsight.delete",
            "hindsight.import_document",
            "hindsight.get_documents",
        ):
            assert name in mod.plugin._tools, f"tool {name} not registered"


# ═══════════════════════════════════════════════════════════
# 3. 降级（_client is None）
# ═══════════════════════════════════════════════════════════


class TestDegradeWithoutClient:
    def test_retain_without_client_degrades(self, mod: Any) -> None:
        """_client 为 None 时 retain 返回降级字典而非崩溃。"""
        mod._client = None
        result = _call_tool(
            mod, "hindsight.retain", bank_id="b1", content="hello"
        )
        assert isinstance(result, dict)
        assert result.get("initialized") is False
        assert "error" in result

    def test_recall_without_client_degrades(self, mod: Any) -> None:
        """_client 为 None 时 recall 返回降级字典而非崩溃。"""
        mod._client = None
        result = _call_tool(
            mod, "hindsight.recall", bank_id="b1", query="hello"
        )
        assert isinstance(result, dict)
        assert result.get("initialized") is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════
# 4. retain + mock client
# ═══════════════════════════════════════════════════════════


class TestRetainWithMock:
    def test_retain_with_mock_client(self, mod: Any, mock_client: MagicMock) -> None:
        """mock client.aretain 被正确调用，返回 {id, stored:true}。"""
        mock_client.aretain.return_value = MagicMock(operation_id="mem_123", accepted=True)

        result = _call_tool(
            mod,
            "hindsight.retain",
            bank_id="bank_A",
            content="hello world",
            memory_type="semantic",
            metadata={"source": "test"},
        )

        # 调用断言(aretain 异步)
        mock_client.aretain.assert_called_once()
        call_kwargs = mock_client.aretain.call_args.kwargs
        assert call_kwargs["bank_id"] == "bank_A"
        assert call_kwargs["content"] == "hello world"

        # 返回形状
        assert result["stored"] is True


# ═══════════════════════════════════════════════════════════
# 5. recall + memory_type 过滤
# ═══════════════════════════════════════════════════════════


class TestRecallFilter:
    def test_recall_filters_by_memory_type(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """recall 用 tags 服务端过滤,返回结果直接是过滤后的。"""
        # arecall 返回带 results 的对象(0.9.0 主字段, 已由服务端按 tags 过滤)
        mock_client.arecall.return_value = MagicMock(
            results=[
                MagicMock(model_dump=lambda: {"id": "1", "text": "a"}),
                MagicMock(model_dump=lambda: {"id": "3", "text": "c"}),
            ],
            chunks=[],
            source_facts=[],
        )

        result = _call_tool(
            mod,
            "hindsight.recall",
            bank_id="bank_A",
            query="abc",
            memory_type="semantic",
        )

        mock_client.arecall.assert_called_once()
        call_kwargs = mock_client.arecall.call_args.kwargs
        # memory_type 转成 tags 服务端过滤
        assert call_kwargs.get("tags") == ["type:semantic"]
        assert result["total"] == 2
        contents = [r["content"] for r in result["results"]]
        assert contents == ["a", "c"]


# ═══════════════════════════════════════════════════════════
# 6. import_document 切分
# ═══════════════════════════════════════════════════════════


class TestImportDocument:
    def test_import_document_chunks_text(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """5000 字符文本被切分为 ~3 块,aretain 调用 3 次。"""
        long_text = "x" * 5000
        mock_client.aretain.return_value = MagicMock(operation_id="mem_x", accepted=True)

        result = _call_tool(
            mod,
            "hindsight.import_document",
            bank_id="bank_A",
            text=long_text,
            knowledge_name="kb1",
        )

        # 2000 字符/块 → 5000/2000 ≈ 3 块
        assert result["chunks_imported"] == 3
        assert result["knowledge_name"] == "kb1"
        assert mock_client.aretain.call_count == 3

    def test_import_document_rejects_non_txt(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """file_path 为 .pdf 时返回错误字典,不读文件不 aretain。"""
        result = _call_tool(
            mod,
            "hindsight.import_document",
            bank_id="bank_A",
            file_path="secret.pdf",
        )

        assert isinstance(result, dict)
        assert "error" in result
        # 关键：未触发 aretain
        mock_client.aretain.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 7. bank_id 缺省
# ═══════════════════════════════════════════════════════════


class TestBankIdDefault:
    def test_bank_id_defaults_to_tenant(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """bank_id 未给定时回落到默认值（多租户隔离 key）。"""
        mock_client.aretain.return_value = MagicMock(operation_id="mem_d", accepted=True)

        result = _call_tool(
            mod,
            "hindsight.retain",
            content="some memory",  # 注意：未传 bank_id
        )

        call_kwargs = mock_client.aretain.call_args.kwargs
        # bank_id 必须被填上默认值（非空字符串）
        assert call_kwargs["bank_id"]
        assert call_kwargs["bank_id"] != ""
        assert result["stored"] is True
# ═══════════════════════════════════════════════════════════
# HTTP 展示面（前端接成熟 Hindsight 数据）
# ═══════════════════════════════════════════════════════════

def _http_call(path, method="GET", query=None):
    import asyncio

    from server import http_handle
    return asyncio.run(http_handle(path=path, method=method, plugin_id="hindsight_memory", query=query or {}))


def _decode(data):
    import base64
    assert data.get("status") == 200, data
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def test_http_recall_degrade_when_client_not_ready():
    out = _http_call("/ext/hindsight_memory_service/recall", query={"query": "test", "limit": "5"})
    assert out["success"] is True
    payload = _decode(out["data"])
    # client 未初始化 → recall 降级（不崩溃），HTTP 仍 200
    assert "initialized" in payload or "results" in payload


def test_http_stats_reports_hindsight_backend():
    out = _http_call("/ext/hindsight_memory_service/stats")
    payload = _decode(out["data"])
    assert payload["backend"] == "hindsight"
    assert "bank_id" in payload


def test_http_unknown_route_404():
    out = _http_call("/ext/hindsight_memory_service/nope")
    assert out["data"]["status"] == 404


# ═══════════════════════════════════════════════════════════
# 缺陷①配套：retain 把 metadata["tags"]（JSON 串）提升为 hindsight 真实 tags
# ── 服务端 list_documents/recall tag 过滤（冷读定向）的前提。
# ═══════════════════════════════════════════════════════════


class TestRetainTagsPromotion:
    def test_retain_promotes_metadata_tags(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """红：metadata["tags"]（HindsightBackend.add 序列化的 JSON 串）解析后
        并入 aretain tags（与 type:<memory_type> 并存）。"""
        _call_tool(
            mod,
            "hindsight.retain",
            bank_id="review",
            content="hello",
            memory_type="review",
            metadata={
                "review_id": "r1",
                "tags": json.dumps(["review_id:r1", "review_report"]),
                "source": "review_agent",
            },
        )

        call_kwargs = mock_client.aretain.call_args.kwargs
        tags = call_kwargs["tags"]
        assert "type:review" in tags
        assert "review_id:r1" in tags
        assert "review_report" in tags
        # metadata 原样透传（值全 str，pydantic dict[str,str] 校验面安全）
        assert call_kwargs["metadata"]["review_id"] == "r1"

    def test_retain_tolerates_invalid_tags_json(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """metadata["tags"] 非 JSON 数组时不炸——仅保留 type tag（防御）。"""
        result = _call_tool(
            mod,
            "hindsight.retain",
            bank_id="b",
            content="c",
            metadata={"tags": "not-a-json-list"},
        )
        assert result["stored"] is True
        assert mock_client.aretain.call_args.kwargs["tags"] == ["type:semantic"]

    def test_retain_without_metadata_tags_unchanged(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """无 tags 元数据时行为与既有完全一致（type tag only）。"""
        _call_tool(mod, "hindsight.retain", bank_id="b", content="c")
        assert mock_client.aretain.call_args.kwargs["tags"] == ["type:semantic"]


# ═══════════════════════════════════════════════════════════
# 缺陷②：hindsight.get_documents 只读工具（按 bank/tags/document_id 取文档原文）
# ═══════════════════════════════════════════════════════════


def _doc_api_mock(
    items: list[dict[str, Any]] | None = None,
    total: int | None = None,
    doc: Any = None,
    get_error: Exception | None = None,
) -> MagicMock:
    """构造 _client.documents 替身（list_documents/get_document 均 AsyncMock）。"""
    docs_api = MagicMock()
    docs_api.list_documents = AsyncMock(
        return_value=SimpleNamespace(items=items or [], total=total if total is not None else len(items or []))
    )
    if get_error is not None:
        docs_api.get_document = AsyncMock(side_effect=get_error)
    else:
        docs_api.get_document = AsyncMock(return_value=doc)
    return docs_api


class TestGetDocumentsTool:
    def test_degrades_without_client(self, mod: Any) -> None:
        """_client 为 None 时降级字典，不崩溃。"""
        mod._client = None
        result = _call_tool(mod, "hindsight.get_documents", bank_id="b")
        assert result.get("initialized") is False
        assert "error" in result

    def test_list_by_tags_fills_original_text(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """tags 过滤列举 → 每条再取原文（list 响应不含 original_text，
        get_document 才有——2026-08-19 真实 API 实测）。"""
        mock_client.documents = _doc_api_mock(
            items=[{"id": "d1", "tags": ["type:review", "review_id:r1"]}],
            doc=SimpleNamespace(
                id="d1",
                original_text='{"review_id": "r1"}',
                document_metadata={"review_id": "r1"},
                tags=["type:review", "review_id:r1"],
                created_at="t0",
                updated_at="t1",
                memory_unit_count=2,
            ),
        )
        result = _call_tool(
            mod,
            "hindsight.get_documents",
            bank_id="review",
            tags=["review_id:r1"],
        )

        list_kwargs = mock_client.documents.list_documents.call_args.kwargs
        assert list_kwargs["bank_id"] == "review"
        assert list_kwargs["tags"] == ["review_id:r1"]
        assert list_kwargs["tags_match"] == "any_strict"
        assert result["total"] == 1
        doc = result["documents"][0]
        assert doc["id"] == "d1"
        assert doc["original_text"] == '{"review_id": "r1"}'
        assert doc["document_metadata"] == {"review_id": "r1"}
        # 精确取原文被调用
        mock_client.documents.get_document.assert_awaited_once_with(
            bank_id="review", document_id="d1"
        )

    def test_exact_document_id_short_circuits(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """document_id 直查（不调 list），命中返回单条原文。"""
        mock_client.documents = _doc_api_mock(
            doc=SimpleNamespace(
                id="doc-9",
                original_text="raw",
                document_metadata={},
                tags=[],
                created_at="t0",
                updated_at="t1",
                memory_unit_count=1,
            ),
        )
        result = _call_tool(
            mod, "hindsight.get_documents", bank_id="review", document_id="doc-9"
        )

        mock_client.documents.list_documents.assert_not_called()
        mock_client.documents.get_document.assert_awaited_once_with(
            bank_id="review", document_id="doc-9"
        )
        assert result["documents"][0]["original_text"] == "raw"

    def test_document_not_found_returns_empty(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """404（NotFound，status=404）→ 空 documents（not found 语义，非错误）。"""
        err = Exception("not found")
        err.status = 404  # hindsight ApiException 带 status 属性
        mock_client.documents = _doc_api_mock(get_error=err)

        result = _call_tool(
            mod, "hindsight.get_documents", bank_id="review", document_id="gone"
        )

        assert result["documents"] == []
        assert result["total"] == 0
        assert "error" not in result

    def test_documents_api_error_returns_error_dict(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """非 404 错误 → error dict（诚实失败，不伪造成空）。"""
        mock_client.documents = _doc_api_mock(get_error=RuntimeError("db down"))

        result = _call_tool(
            mod, "hindsight.get_documents", bank_id="review", document_id="d"
        )

        assert result.get("documents") in (None, [])
        assert "error" in result


# ═══════════════════════════════════════════════════════════
# 预存 bug①：hindsight.summarize arecall 传 top_k/memory_type（非形参，
# TypeError 被 except 降级吞掉——2026-08-19 批 C 取证）
# ═══════════════════════════════════════════════════════════


class TestSummarizeFix:
    def test_summarize_arecall_kwargs_valid(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """红：arecall 只收真实形参——memory_type 转 tags 服务端过滤，
        top_k 不透传（arecall 无此参数，token 预算驱动）。"""
        mock_client.arecall.return_value = MagicMock(
            results=[MagicMock(id="f1")], chunks=[], source_facts=[]
        )
        mock_client.areflect.return_value = MagicMock(
            model_dump=lambda: {"summary": "摘要文本"}
        )

        result = _call_tool(
            mod,
            "hindsight.summarize",
            bank_id="bank_A",
            query="q",
            top_k=20,
            memory_type="semantic",
        )

        call_kwargs = mock_client.arecall.call_args.kwargs
        assert "top_k" not in call_kwargs, (
            f"arecall 无 top_k 形参，实际收到: {sorted(call_kwargs)}"
        )
        assert "memory_type" not in call_kwargs
        assert call_kwargs.get("tags") == ["type:semantic"]
        assert call_kwargs.get("tags_match") == "any"
        # 成功路径（无 error），recalled 从 RecallResponse.results 计数
        assert "error" not in result
        assert result["recalled"] == 1
        assert result["summary"] == "摘要文本"

    def test_summarize_without_memory_type_plain_recall(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """无 memory_type 时 arecall 不带 tags（全库检索语义不变）。"""
        mock_client.arecall.return_value = MagicMock(results=[], chunks=[], source_facts=[])
        mock_client.areflect.return_value = MagicMock(
            model_dump=lambda: {"summary": "s"}
        )

        _call_tool(mod, "hindsight.summarize", bank_id="bank_A", query="q")

        call_kwargs = mock_client.arecall.call_args.kwargs
        assert "tags" not in call_kwargs
        assert call_kwargs["bank_id"] == "bank_A"
        assert call_kwargs["query"] == "q"


# ═══════════════════════════════════════════════════════════
# 预存 bug②：hindsight.delete callable(coro) 恒 False → adelete_bank
# 从不 await、删库假成功（协程无 __call__）
# ═══════════════════════════════════════════════════════════


class TestDeleteFix:
    def test_delete_awaits_coroutine(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """红：adelete_bank 返回协程必须被 await（真实等待删除完成）。"""
        result = _call_tool(mod, "hindsight.delete", bank_id="bank_del")

        mock_client.adelete_bank.assert_awaited_once_with(bank_id="bank_del")
        assert result["deleted"] is True

    def test_delete_failure_returns_false(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """adelete_bank 抛错 → deleted:false（诚实失败）。"""
        mock_client.adelete_bank.side_effect = RuntimeError("bank locked")

        result = _call_tool(mod, "hindsight.delete", bank_id="bank_del")

        assert result["deleted"] is False
        assert "error" in result

    def test_delete_awaits_adelete_fallback(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """adelete_bank 缺席时回落 adelete——协程同样要 await。"""
        del mock_client.adelete_bank
        mock_client.adelete = AsyncMock(return_value=None)

        result = _call_tool(mod, "hindsight.delete", bank_id="b2")

        mock_client.adelete.assert_awaited_once_with(bank_id="b2")
        assert result["deleted"] is True


# ═══════════════════════════════════════════════════════════
# bank_id 缺省回落可观测（兜底反模式审查 P10，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestBankIdDefaultWarnsOnce:
    """P10：回落字面 'default' 必须一次性 warning（租户隔离未生效可见）。"""

    def test_default_fallback_warns_once(self, mod: Any, monkeypatch, caplog) -> None:
        """未配置缺省库回落 'default' → 首次 warning，后续静默。"""
        import logging

        monkeypatch.setattr(mod, "_bank_default_warned", False)
        monkeypatch.setattr(mod, "_DEFAULT_BANK_ID", "default")
        with caplog.at_level(logging.WARNING):
            assert mod._resolve_bank_id(None) == "default"
            first = [r for r in caplog.records if "隔离未生效" in r.getMessage()]
            assert first, "首次回落 default 必须告警"
            n = len(caplog.records)
            assert mod._resolve_bank_id("") == "default"
            assert len(caplog.records) == n, "一次性告警：回落不重复刷屏"

    def test_configured_bank_no_warning(self, mod: Any, monkeypatch, caplog) -> None:
        """配置了缺省库（非 'default'）→ 回落该值不告警。"""
        import logging

        monkeypatch.setattr(mod, "_bank_default_warned", False)
        monkeypatch.setattr(mod, "_DEFAULT_BANK_ID", "tenant_42")
        with caplog.at_level(logging.WARNING):
            assert mod._resolve_bank_id(None) == "tenant_42"
            assert not [r for r in caplog.records if "隔离未生效" in r.getMessage()]

    def test_explicit_bank_id_short_circuits(self, mod: Any) -> None:
        """显式 bank_id 优先（回归）。"""
        assert mod._resolve_bank_id("bank_X") == "bank_X"


# ═══════════════════════════════════════════════════════════
# memory 域 / knowledge-base 域 http.handle 分发（channel_api 退役自持迁移）
# ═══════════════════════════════════════════════════════════

def _http_ok(out):
    """http_handle 返回 {success, data(HttpHandleResponse)} → 解出 body dict。"""
    import base64
    assert out["success"] is True, out
    data = out["data"]
    assert data.get("status") == 200, data
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


# ═══════════════════════════════════════════════════════════
# 8-22 真机记忆测试 5 症状回归（TDD 红灯）：
#   S1 存取后即时检索空（async retain 后台抽取未完成）
#   S2 delete 忽略 memory_id 删整库（无逐条删除通路）
#   S3 import_document metadata 数字进 pydantic dict[str,str] 422
#   S4 import_text 落库的 chunk_index/chunk_total 字符串化
#   S5 recall 空 query 422（list 工具必经之路）
# ═══════════════════════════════════════════════════════════


class TestRetainSyncImmediatelyRecallable:
    def test_retain_uses_sync_mode_by_default(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """S1 根因：store 必须同步确认（sync retain），否则刚存即搜是空。

        retain_async=True 时服务端后台抽取需数秒完成，期间 recall 检索不到
        （8-22 真机实证：sync 立即可召回，async 需 ~8s）——memory 工具层
        store→retrieve 连续调用在 LLM 编排下间隔远小于抽取耗时。
        """
        mock_client.aretain.return_value = MagicMock(operation_id="mem_sync", accepted=True)

        _call_tool(
            mod, "hindsight.retain", bank_id="bank_A",
            content="张三在北京召开会议", metadata={"tags": '["t1"]'},
        )

        call_kwargs = mock_client.aretain.call_args.kwargs
        assert call_kwargs.get("retain_async") is False
        assert "operation_id" not in call_kwargs

    def test_retain_returns_document_id_when_supplied(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """S2 前提：调用方给定 document_id 时原样回传（真删除通路）。"""
        mock_client.aretain.return_value = MagicMock(operation_id="op_doc", accepted=True)

        result = _call_tool(
            mod, "hindsight.retain", bank_id="bank_A",
            content="hello", metadata={"tags": '["t1"]'}, document_id="mem-abc123",
        )

        call_kwargs = mock_client.aretain.call_args.kwargs
        assert call_kwargs.get("document_id") == "mem-abc123"
        assert result["id"] == "mem-abc123"


class TestDeleteTargeted:
    def test_delete_with_memory_id_uses_documents_api(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """S2 根因：带 memory_id 必须走 documents.delete_document 逐条删除，
        不再忽略 memory_id 删整个 bank。"""
        mock_client.documents = MagicMock()
        mock_client.documents.delete_document = AsyncMock(
            return_value=MagicMock(
                model_dump=lambda: {"success": True, "document_id": "mem-abc123"},
            )
        )

        result = _call_tool(
            mod, "hindsight.delete", bank_id="bank_del", memory_id="mem-abc123",
        )

        mock_client.documents.delete_document.assert_awaited_once_with(
            bank_id="bank_del", document_id="mem-abc123",
        )
        assert result["deleted"] is True
        assert mock_client.adelete_bank.await_count == 0

    def test_delete_without_memory_id_deletes_bank(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """无 memory_id 才允许删整个 bank（既有语义保留）。"""
        result = _call_tool(mod, "hindsight.delete", bank_id="bank_del")

        mock_client.adelete_bank.assert_awaited_once_with(bank_id="bank_del")
        assert result["deleted"] is True

    def test_delete_documents_error_returns_false(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """delete_document 抛错 → deleted:false（诚实失败）。"""
        mock_client.documents = MagicMock()
        mock_client.documents.delete_document = AsyncMock(
            side_effect=RuntimeError("doc gone"),
        )

        result = _call_tool(
            mod, "hindsight.delete", bank_id="bank_del", memory_id="mem-x",
        )

        assert result["deleted"] is False
        assert "error" in result


class TestImportDocMetaStr:
    def test_import_document_metadata_values_are_str(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """S3/S4 根因：hindsight MemoryItem.metadata 是 dict[str,str] pydantic
        校验面，chunk_index/chunk_total 以 int 传入必 422（8-22 实测）。"""
        mock_client.aretain.return_value = MagicMock(operation_id="mem_x", accepted=True)

        _call_tool(
            mod, "hindsight.import_document",
            bank_id="bank_A", text="abc", knowledge_name="kb1",
        )

        for call in mock_client.aretain.call_args_list:
            meta = call.kwargs["metadata"]
            assert all(isinstance(v, str) for v in meta.values())
            assert meta["chunk_index"] == "0"
            assert meta["chunk_total"] == "1"
            assert meta["knowledge_name"] == "kb1"


class TestRecallEmptyQuery:
    def test_recall_rejects_empty_query(self, mod: Any, mock_client: MagicMock) -> None:
        """S5 根因：hindsight 服务端拒绝空 query（'query must contain at least
        one word character'，8-22 实测 422）——list 工具先于查询判空，不把空
        查询打到后端。"""
        result = _call_tool(mod, "hindsight.recall", bank_id="bank_A", query="")

        assert result["error"]
        mock_client.arecall.assert_not_called()


def test_http_memory_episodes_dispatch():
    """memory 域：http.handle 分发到 routes_memory，注入的 mock 后端结果回传。

    注入路径经 server 模块级_ensure_memory_backend（分发时懒构建）——
    测试通过 server._memory_backend + _memory_backend_attempted 短路注入。
    """
    from unittest.mock import AsyncMock, MagicMock

    import server as srv
    from routes_memory import set_memory_backend

    backend = MagicMock()
    backend.search = AsyncMock(return_value=[
        {"id": "e1", "content": "intent", "score": 1.0, "memory_type": "episode",
         "metadata": {"tags": ["t"], "created_at": "t0"}},
    ])
    srv._memory_backend = backend
    srv._memory_backend_attempted = True
    try:
        out = _http_call("/ext/hindsight_memory_service/memory/episodes",
                         query={"page": "1", "page_size": "10"})
        payload = _http_ok(out)
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == "e1"
        assert payload["items"][0]["intent_text"] == "intent"
        backend.search.assert_awaited_once()
    finally:
        srv._memory_backend = None
        srv._memory_backend_attempted = False
        set_memory_backend(None)


def test_http_memory_search_get_dispatch():
    from unittest.mock import AsyncMock, MagicMock

    import server as srv
    from routes_memory import set_memory_backend

    backend = MagicMock()
    backend.search = AsyncMock(return_value=[
        {"id": "m1", "content": "hit", "score": 0.9, "memory_type": "semantic", "metadata": {}},
    ])
    srv._memory_backend = backend
    srv._memory_backend_attempted = True
    try:
        out = _http_call("/ext/hindsight_memory_service/memory/search",
                         query={"query": "q", "top_k": "5"})
        payload = _http_ok(out)
        assert payload["total"] == 1
        assert payload["items"][0]["content"] == "hit"
        assert backend.search.call_args.kwargs["query"] == "q"
    finally:
        srv._memory_backend = None
        srv._memory_backend_attempted = False
        set_memory_backend(None)


def test_http_memory_delete_missing_404():
    """memory 域 DELETE 未命中 → HTTP 404（body {"detail": ...}，与旧版一致）。"""
    from unittest.mock import AsyncMock, MagicMock

    import server as srv
    from routes_memory import set_memory_backend

    backend = MagicMock()
    backend.search = AsyncMock(return_value=[])
    backend.delete = AsyncMock(return_value=False)
    srv._memory_backend = backend
    srv._memory_backend_attempted = True
    try:
        out = _http_call("/ext/hindsight_memory_service/memory/nope", method="DELETE")
        assert out["success"] is True
        assert out["data"]["status"] == 404
        payload = json.loads(__import__("base64").b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["detail"] == "未找到相关记忆"
    finally:
        srv._memory_backend = None
        srv._memory_backend_attempted = False
        set_memory_backend(None)


def test_http_kb_list_and_stats_dispatch():
    """knowledge-base 域：列表/统计走本地元数据仓（不依赖 client）。"""
    import knowledge_base as kb

    kb.set_data_dir(__import__("tempfile").mkdtemp(prefix="kb_test_"))

    out = _http_call("/ext/hindsight_memory_service/knowledge-base")
    assert _http_ok(out) == []

    out = _http_call("/ext/hindsight_memory_service/knowledge-base/stats")
    stats = _http_ok(out)
    assert stats["total"] == 0
    assert "categories_count" in stats


def test_http_kb_check_degrades_without_client():
    """knowledge-base check：client 未初始化如实报告 unavailable（HTTP 200）。"""
    import server as srv

    srv._client = None  # 显式复位（防其它测试泄漏注入的 client）
    out = _http_call("/ext/hindsight_memory_service/knowledge-base/check")
    payload = _http_ok(out)
    assert payload["available"] is False
    assert payload["bank"] == "kb"


def test_http_kb_upload_dispatch_multipart():
    """knowledge-base 上传：multipart 解析 → mock client 入库 → 注册条目。

    分发时 kb.set_client(server._client)——注入路径经 server 模块级 _client。
    """
    from unittest.mock import AsyncMock, MagicMock

    import knowledge_base as kb
    import server as srv

    kb.set_data_dir(__import__("tempfile").mkdtemp(prefix="kb_test_"))
    client = MagicMock()
    client.aretain = AsyncMock(side_effect=lambda **kw: MagicMock(
        operation_id=f"c{kw['metadata']['kb_chunk_index']}", accepted=True))
    client.acreate_bank = AsyncMock(return_value=None)
    srv._client = client
    try:
        import base64 as b64
        boundary = "----kbTestBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="doc.md"\r\n'
            f"Content-Type: text/markdown\r\n\r\n"
            f"{'hello 知识库 ' * 500}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        # 手工构造完整 http_handle 调用（含 headers）
        import asyncio

        from server import http_handle

        result = asyncio.run(http_handle(
            path="/ext/hindsight_memory_service/knowledge-base/upload",
            method="POST",
            plugin_id="hindsight_memory_service",
            raw_body=b64.b64encode(body).decode("ascii"),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            query={},
        ))
        assert result["success"] is True, result
        assert result["data"]["status"] == 200, result
        payload = json.loads(b64.b64decode(result["data"]["body"]).decode("utf-8"))
        assert payload["message"] == "文件上传成功"
        assert payload["chunks_imported"] >= 1
        assert client.aretain.call_count == payload["chunks_imported"]
    finally:
        srv._client = None
        kb.set_client(None)
