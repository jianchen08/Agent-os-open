# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""KB 域原生摄取管线测试（ADR 2026-11-19-KB原生摄取管线改造：verbatim 主模式
+ chunks 降级位）。

机制（被测契约）：
- 上传 = 整文档单次 aretain（客户端零切块，`###` 条目边界交上游原生递归分隔符
  切块），标题双通道：内容头部前置 ``# {title}``（首 chunk 向量化含标题语义）
  + ``RetainContent.context``=标题（逐条归属）；
- bank 配置通道：kb bank（verbatim + entity_labels 受控词表）/ kb_chunks bank
  （chunks 模式）经 acreate_bank + banks.update_bank_config 双通道落配置；
- 降级上报：verbatim 落库失败（LLM 未配置 / bank 解析模式非 verbatim /
  aretain 调用失败）→ 降级落 kb_chunks bank，响应字段 + 日志如实标注实际
  落库模式，禁静默降级；
- 删除：kb_item:{id} 标签双 bank（主 + 降级）覆盖。

mock 仅限外部依赖（hindsight client 面，aresolvers 对齐真实语义：aretain 落
bank、bank config 读写、documents 面）；`_chunk_text` 客户端切块面机械闸
（生产调用与定义必须不存在）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_HEADERS = {"x-agentos-tenant": "default", "x-agentos-user": "admin"}
_LLM_ENV = "HINDSIGHT_API_LLM_PROVIDER"


def _load_module() -> Any:
    """动态加载 knowledge_base.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_kb_native_ingestion_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "knowledge_base.py")
    assert spec is not None, "Cannot load knowledge_base.py"
    assert spec.loader is not None, "Cannot load knowledge_base.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MultiBankDocs:
    """hindsight documents 面替身（外部依赖）：按 bank 分桶的内存文档表。"""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}

    def add(self, bank_id: str, doc: dict[str, Any]) -> None:
        self.docs.setdefault(bank_id, {})[doc["id"]] = doc

    def _filtered(self, bank_id: str, tags: list[str] | None, tags_match: str) -> list[dict[str, Any]]:
        docs = list(self.docs.get(bank_id, {}).values())
        if tags:
            if tags_match == "all":
                docs = [d for d in docs if set(tags) <= set(d.get("tags") or [])]
            else:  # any_strict
                docs = [d for d in docs if set(tags) & set(d.get("tags") or [])]
        return docs

    async def list_documents(self, bank_id: str = "", tags: list[str] | None = None,
                             tags_match: str = "any_strict", limit: int = 100,
                             offset: int = 0) -> Any:
        docs = self._filtered(bank_id, tags, tags_match)
        page = docs[offset : offset + limit]
        items = [SimpleNamespace(model_dump=lambda d=d: dict(d)) for d in page]
        return SimpleNamespace(items=items, total=len(docs))

    async def get_document(self, bank_id: str = "", document_id: str = "") -> Any:
        doc = self.docs.get(bank_id, {}).get(document_id)
        if doc is None:
            raise RuntimeError("not found")
        return SimpleNamespace(model_dump=lambda: dict(doc))

    async def delete_document(self, bank_id: str = "", document_id: str = "") -> None:
        self.docs.get(bank_id, {}).pop(document_id, None)


class _FakeBanksApi:
    """hindsight banks 面替身：list/update_config/get_config（config 值可注入
    强制形态，模拟 provider=none 时上游全局强制 chunks）。"""

    def __init__(self, created: dict[str, dict[str, Any]], documents: Any) -> None:
        self._created = created  # bank_id -> acreate_bank kwargs（最近一次）
        self._documents = documents  # documents 面引用（list_banks 真实语义含其 bank）
        self.label_patches: dict[str, Any] = {}
        self.forced_resolved: dict[str, str] = {}  # bank_id -> 强制解析模式
        self.config_api_broken = False

    async def list_banks(self) -> Any:
        ids = sorted({*self._created, *self._documents.docs.keys()})
        return SimpleNamespace(banks=[SimpleNamespace(bank_id=b) for b in ids])

    async def update_bank_config(self, bank_id: str = "", **updates: Any) -> Any:
        if self.config_api_broken:
            raise RuntimeError("bank config api disabled")
        self.label_patches[bank_id] = updates
        return {"bank_id": bank_id, "overrides": updates}

    async def get_bank_config(self, bank_id: str = "") -> Any:
        if self.config_api_broken:
            raise RuntimeError("bank config api disabled")
        mode = self.forced_resolved.get(bank_id) or self._created.get(bank_id, {}).get(
            "retain_extraction_mode", "concise"
        )
        return {"bank_id": bank_id, "config": {"retain_extraction_mode": mode}, "overrides": {}}


class _FakeClient:
    """hindsight client 替身：aretain 真实落 bank（响应形态对齐 RetainResponse）。"""

    def __init__(self, fail_banks: set[str] | None = None) -> None:
        self.documents = _MultiBankDocs()
        self.acreate_bank = AsyncMock(side_effect=self._create_bank)
        self.aretain = AsyncMock(side_effect=self._aretain)
        self.arecall = AsyncMock(return_value=SimpleNamespace(chunks=[], results=[]))
        self._created: dict[str, dict[str, Any]] = {}
        self.banks = _FakeBanksApi(self._created, self.documents)
        self._fail_banks = fail_banks or set()

    async def _create_bank(self, **kw: Any) -> None:
        bank_id = str(kw.get("bank_id", ""))
        if bank_id:
            self._created[bank_id] = dict(kw)

    async def _aretain(self, **kw: Any) -> Any:
        bank = str(kw.get("bank_id", ""))
        if bank in self._fail_banks:
            raise RuntimeError(f"bank {bank} unavailable")
        doc_id = f"{bank}:doc-{len(self.documents.docs.get(bank, {})) + 1}"
        self.documents.add(bank, {
            "id": doc_id,
            "bank_id": bank,
            "original_text": str(kw.get("content", "")),
            "created_at": "2026-11-19T00:00:00+00:00",
            "updated_at": "2026-11-19T00:00:00+00:00",
            "tags": list(kw.get("tags") or []),
            "document_metadata": dict(kw.get("metadata") or {}),
        })
        return SimpleNamespace(operation_id=f"op-{doc_id}", accepted=True)


def _make_world(fail_banks: set[str] | None = None) -> tuple[Any, _FakeClient]:
    module = _load_module()
    module.set_client(None)
    client = _FakeClient(fail_banks=fail_banks)
    module.set_client(client)
    return module, client


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 sidecar 已注入 LLM provider（verbatim 主模式可用的前置）。"""
    monkeypatch.setenv(_LLM_ENV, "openai")


# ═══════════════════════════════════════════════════════════
# 场景①：整文档单次 aretain（客户端零切块，### 条目边界交上游）
# ═══════════════════════════════════════════════════════════


class TestSingleDocumentRetain:
    def test_upload_whole_document_single_aretain_keeps_entry_bodies_intact(
        self, llm_env: None
    ) -> None:
        """含 ### 条目标题的 md：单次 aretain、全文原样投递（客户端切块循环已
        移除——### 条目边界由上游原生递归分隔符切块承载，条目不再被截半）。"""
        kb, client = _make_world()
        body = "\n\n".join(
            f"### 条目{i}\n\n这是条目{i}的完整正文，包含证据等级与来源标注。" for i in range(60)
        )
        md = f"# 指南\n\n{body}".encode("utf-8")
        assert len(md) > 2000, "前置：内容超旧客户端切块阈值，旧实现必多块"

        result = _run(kb.upload_document("guide.md", md, "text/markdown", headers=_HEADERS))

        assert result["chunks_imported"] == 1
        assert client.aretain.call_count == 1, "整文档必须单次 aretain（客户端零切块）"
        sent = client.aretain.call_args.kwargs["content"]
        assert "### 条目0" in sent and "### 条目59" in sent
        assert sent.endswith("这是条目59的完整正文，包含证据等级与来源标注。"), "首尾条目正文完整（不在条目中间截断）"

    def test_upload_tags_and_metadata_contract(self, llm_env: None) -> None:
        """tags=既有自动生成集（type:knowledge/kb_item/filetype），metadata 聚合面
        保留（kb_id/kb_name/kb_size/kb_mime/kb_owner/kb_tenant）。"""
        kb, client = _make_world()
        result = _run(kb.upload_document("guide.md", "正文".encode(), "text/markdown", headers=_HEADERS))

        call = client.aretain.call_args
        assert call.kwargs["bank_id"] == "kb"
        assert call.kwargs["tags"] == ["type:knowledge", f"kb_item:{result['item_id']}", "filetype:md"]
        meta = call.kwargs["metadata"]
        assert meta["kb_id"] == result["item_id"]
        assert meta["kb_name"] == "guide.md"
        assert meta["kb_owner"] == "admin"
        assert meta["kb_tenant"] == "default"
        assert meta["source"] == "knowledge_base"


# ═══════════════════════════════════════════════════════════
# 场景②：标题双通道（内容头部前置 + context 字段）
# ═══════════════════════════════════════════════════════════


class TestTitleDualChannel:
    def test_content_head_and_context_both_carry_title(self, llm_env: None) -> None:
        """内容头部前置 ``# {title}``（首 chunk 向量化含标题语义）+ context=标题
        （逐条 fact 归属 + verbatim 抽取获得文档上下文）。"""
        kb, client = _make_world()
        _run(kb.upload_document("健身指南.md", "条目正文".encode(), "text/markdown", headers=_HEADERS))

        call = client.aretain.call_args
        assert call.kwargs["content"].startswith("# 健身指南\n\n")
        assert call.kwargs["context"] == "健身指南"


# ═══════════════════════════════════════════════════════════
# 场景③：verbatim 落库失败 → 降级 kb_chunks（如实上报，禁静默）
# ═══════════════════════════════════════════════════════════


class TestVerbatimDegrade:
    def test_primary_retain_failure_degrades_to_chunks_bank(
        self, llm_env: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """主 bank aretain 失败 → 降级落 kb_chunks bank（chunks 模式），响应字段
        （ingestion_mode/bank_id/degraded/degradation_reason）+ 告警日志如实标注。"""
        kb, client = _make_world(fail_banks={"kb"})
        with caplog.at_level("WARNING"):
            result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert result["ingestion_mode"] == "chunks"
        assert result["bank_id"] == "kb_chunks"
        assert result["degraded"] is True
        assert result["degradation_reason"]
        assert any("降级" in r.getMessage() for r in caplog.records), "降级必须日志留痕（禁静默）"
        assert client.documents.docs.get("kb_chunks"), "降级文档落 kb_chunks bank"

    def test_llm_provider_unconfigured_lands_chunks_bank_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM provider 未配置（env 缺失）→ verbatim 主模式天然不可用，直落
        kb_chunks bank 并如实上报（不触主 bank，不静默）。"""
        monkeypatch.delenv(_LLM_ENV, raising=False)
        kb, client = _make_world()
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert result["ingestion_mode"] == "chunks"
        assert result["bank_id"] == "kb_chunks"
        assert result["degraded"] is True
        primary_calls = [
            c for c in client.aretain.call_args_list if c.kwargs["bank_id"] == "kb"
        ]
        assert primary_calls == [], "LLM 未配置不得尝试主 bank"

    def test_resolved_mode_forced_chunks_degrades(self, llm_env: None) -> None:
        """bank 解析模式被上游强制为 chunks（provider=none 全局强制）→ 主 bank
        不可 verbatim，降级落 kb_chunks（配置面权威信号，不做假 verbatim）。"""
        kb, client = _make_world()
        client.banks.forced_resolved["kb"] = "chunks"
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert result["ingestion_mode"] == "chunks"
        assert result["degraded"] is True
        assert client.documents.docs.get("kb_chunks")

    def test_happy_path_reports_verbatim(self, llm_env: None) -> None:
        """主链路：落 kb bank，如实上报 verbatim（非降级）。"""
        kb, client = _make_world()
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert result["ingestion_mode"] == "verbatim"
        assert result["bank_id"] == "kb"
        assert result["degraded"] is False
        assert result["degradation_reason"] == ""

    def test_both_banks_fail_raises_honestly(self, llm_env: None) -> None:
        """主 + 降级 bank 双失败 → 上传如实 500（唯一存储面，无兜底）。"""
        kb, client = _make_world(fail_banks={"kb", "kb_chunks"})
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        assert ei.value.status_code == 500
        assert ei.value.error_code == "KNB_ING_8001"


# ═══════════════════════════════════════════════════════════
# bank 配置通道（kb=verbatim+entity_labels / kb_chunks=chunks）
# ═══════════════════════════════════════════════════════════


class TestBankConfigChannel:
    def test_primary_bank_configured_verbatim_with_entity_labels(self, llm_env: None) -> None:
        """kb bank：acreate_bank 带 retain_extraction_mode=verbatim +
        banks.update_bank_config 落 entity_labels 受控词表。"""
        kb, client = _make_world()
        _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert client._created["kb"]["retain_extraction_mode"] == "verbatim"
        patch = client.banks.label_patches["kb"]
        assert patch["entity_labels"], "受控词表必须经 bank config 通道落库"
        keys = {v["value"] for grp in patch["entity_labels"] for v in grp.get("values", [])}
        assert {"健康", "金钱", "法律", "安全", "学习"} <= keys, "ADR 受控词表五域必须在位"

    def test_fallback_bank_configured_chunks_without_labels(self, llm_env: None) -> None:
        """kb_chunks bank：chunks 模式，零 LLM，不设 entity_labels。"""
        kb, client = _make_world()
        _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        kb2, client2 = _make_world(fail_banks={"kb"})
        _run(kb2.upload_document("a.md", "正文".encode(), headers=_HEADERS))

        assert client2._created["kb_chunks"]["retain_extraction_mode"] == "chunks"
        assert "kb_chunks" not in client2.banks.label_patches, "降级 bank 不设受控词表"
        assert client._created.get("kb_chunks", {}).get("retain_extraction_mode") in ("", None, "chunks")

    def test_entity_labels_patch_failure_warns_and_upload_continues(
        self, llm_env: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """bank config API 被服务端禁用 → 词表缺席 warn 留痕，上传主链路照常
        （acreate_bank 已承载模式；缺失仅降级词表增强面）。"""
        kb, client = _make_world()
        client.banks.config_api_broken = True
        with caplog.at_level("WARNING"):
            result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        assert result["ingestion_mode"] == "verbatim"
        assert any("entity_labels" in r.getMessage() or "词表" in r.getMessage() for r in caplog.records)

    def test_resolved_mode_probe_without_capability_proceeds_optimistic(
        self, llm_env: None
    ) -> None:
        """client 无 banks.get_bank_config 能力 → 解析模式未知（空串），乐观走
        verbatim 主链路（降级信号面只收窄不放大）。"""
        kb, client = _make_world()
        banks_any: Any = SimpleNamespace(
            list_banks=AsyncMock(return_value=SimpleNamespace(banks=[])),
            update_bank_config=AsyncMock(return_value={"overrides": {}}),
        )  # 无 get_bank_config
        client.banks = banks_any
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        assert result["ingestion_mode"] == "verbatim"
        assert result["degraded"] is False

    def test_resolved_mode_model_dump_object_form_forced_chunks_degrades(
        self, llm_env: None
    ) -> None:
        """get_bank_config 回 pydantic 形态（model_dump 对象）→ 解析模式读取
        照常生效（强制 chunks → 降级）。"""
        kb, client = _make_world()
        banks_any: Any = client.banks
        banks_any.get_bank_config = AsyncMock(return_value=SimpleNamespace(
            model_dump=lambda: {"config": {"retain_extraction_mode": "chunks"}},
        ))
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        assert result["ingestion_mode"] == "chunks"
        assert result["degraded"] is True

    def test_resolved_mode_non_dict_response_proceeds_optimistic(self, llm_env: None) -> None:
        """get_bank_config 回非 dict 形态 → 按未知处理，乐观走 verbatim 主链路。"""
        kb, client = _make_world()
        banks_any: Any = client.banks
        banks_any.get_bank_config = AsyncMock(return_value="junk")
        result = _run(kb.upload_document("a.md", "正文".encode(), headers=_HEADERS))
        assert result["ingestion_mode"] == "verbatim"
        assert result["degraded"] is False

    def test_ensure_bank_without_client_raises_503(self) -> None:
        """_ensure_bank 防御线：client 缺席 → KBError(503)（唯一存储不可达）。"""
        kb, _client = _make_world()
        kb.set_client(None)
        with pytest.raises(kb.KBError) as ei:
            _run(kb._ensure_bank("kb"))
        assert ei.value.status_code == 503
        assert "KNB_INIT" in ei.value.error_code


# ═══════════════════════════════════════════════════════════
# 场景④：kb_item 标签删除双 bank 覆盖
# ═══════════════════════════════════════════════════════════


def _seed_doc(client: _FakeClient, bank_id: str, kb_id: str) -> None:
    client.documents.add(bank_id, {
        "id": f"{bank_id}:d-{kb_id}",
        "bank_id": bank_id,
        "original_text": "正文",
        "created_at": "2026-11-19T00:00:00+00:00",
        "updated_at": "2026-11-19T00:00:00+00:00",
        "tags": ["type:knowledge", f"kb_item:{kb_id}", "filetype:md"],
        "document_metadata": {"kb_id": kb_id, "kb_name": "a.md", "kb_size": "2",
                              "kb_mime": "text/markdown", "source": "knowledge_base",
                              "created_at": "2026-11-19T00:00:00+00:00"},
    })


class TestDeleteDualBank:
    def test_delete_covers_primary_and_fallback_banks(self, llm_env: None) -> None:
        """同一 kb_item 落双 bank（降级残留形态）→ 删除按标签双 bank 覆盖。"""
        kb, client = _make_world()
        _seed_doc(client, "kb", "item-x")
        _seed_doc(client, "kb_chunks", "item-x")

        result = _run(kb.delete_item("item-x", headers=_HEADERS))

        assert result == {"message": "条目已删除", "id": "item-x"}
        assert client.documents.docs.get("kb", {}) == {}
        assert client.documents.docs.get("kb_chunks", {}) == {}

    def test_cross_tenant_chunks_bank_foreign_403(self, llm_env: None) -> None:
        """他租户降级 bank 持有同 id 条目 → 非 admin 403（授权面跨主/降级 bank）。"""
        kb, client = _make_world()
        _seed_doc(client, "kb_chunks_tenant-b", "item-x")

        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("item-x", headers={"x-agentos-tenant": "tenant-a",
                                                    "x-agentos-user": "u-a"}))
        assert ei.value.status_code == 403

    def test_search_and_list_cover_fallback_bank(self, llm_env: None) -> None:
        """降级 bank 条目在列表/检索面可见（_caller_banks 双 bank 扇出）。"""
        kb, client = _make_world()
        _seed_doc(client, "kb_chunks", "item-fb")

        items = _run(kb.list_items(headers=_HEADERS))
        assert [it["id"] for it in items] == ["item-fb"]

        recall_hits = {"kb_chunks": [{"id": "kb_chunks:d-1", "text": "降级正文", "score": 0.9,
                                      "metadata": {"kb_id": "item-fb", "kb_name": "a.md"},
                                      "tags": ["filetype:md"]}]}
        client.arecall = AsyncMock(side_effect=lambda **kw: SimpleNamespace(
            chunks=[],
            results=[SimpleNamespace(model_dump=lambda h=h: dict(h)) for h in recall_hits.get(kw["bank_id"], [])],
        ))
        result = _run(kb.search("降级", headers=_HEADERS))
        recalled_banks = [c.kwargs["bank_id"] for c in client.arecall.call_args_list]
        assert set(recalled_banks) == {"kb", "kb_chunks"}, "检索必须跨主/降级 bank 扇出"
        assert result["results"][0]["item_id"] == "item-fb"


# ═══════════════════════════════════════════════════════════
# 机械闸：客户端切块面不复活
# ═══════════════════════════════════════════════════════════


class TestNoClientChunking:
    def test_chunking_surface_removed(self) -> None:
        """_chunk_text 生产调用与定义不存在（knowledge_base.py / server.py 双文件
        grep 级机械闸；兼容性一刀切不留兼容层）。"""
        module = _load_module()
        assert not hasattr(module, "_chunk_text"), "客户端切块函数不得复活"
        for filename in ("knowledge_base.py", "server.py"):
            source = (_PLUGIN_DIR / filename).read_text(encoding="utf-8")
            assert "_chunk_text" not in source, f"{filename} 仍有 _chunk_text 引用"
            assert "_KB_CHUNK_SIZE" not in source and "_CHUNK_SIZE" not in source, (
                f"{filename} 仍有客户端切块常量"
            )
