# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""knowledge-base 域身份归属过滤（X10-F1 kb bank 全体共享修复，复用 X4-F1/X6-F1 模式）。

行为锚点（租户分 bank，fail-closed）：
- 身份 = 内核 dispatcher 权威注入的 x-agentos-tenant / x-agentos-user /
  x-agentos-role 头（匿名请求剥离——handler 可见即可信）；
- bank 按租户划分：default 种子租户沿用存量 bank ``kb``（旧 kb bank 即
  default 租户的 bank，存量文档零迁移），其余租户 ``kb_<tenant>`` 从空开始；
  租户内共享（读/删），跨租户互不可见；
- 读面（list/stats/tags/get/search）：缺身份头 → 空集/404，绝不返回全量；
- 写面（upload/delete）：缺身份头 → 403；DELETE 跨租户 → 403（真实状态到
  前端，不走 404 掩护），未知 id → 404；
- admin 角色（x-agentos-role）跨租户可见可操作（运维面，对照 X6-F1 同款）；
- 上传落属主（kb_owner/kb_tenant 进 chunk metadata，追溯面）。

替身边界：hindsight sidecar 是外部依赖——FakeKBClient 按 bank 隔离 documents
面 + banks.list_banks 发现 + arecall 按 bank 返回（真链路见 dispatch 端到端）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"
_TENANT_DEFAULT = "default"  # admin 种子租户（存量 kb bank 归属）

_BANK_A = "kb_tenant-a"
_BANK_B = "kb_tenant-b"
_BANK_LEGACY = "kb"  # 存量 bank（= default 租户 bank，零迁移）

_KB_PREFIX = "/ext/hindsight_memory_service/knowledge-base"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _headers(tenant: str, user: str, role: str = "") -> dict[str, str]:
    """构造内核注入形态的身份头（role 仅 admin 场景携带）。"""
    h = {"x-agentos-tenant": tenant, "x-agentos-user": user}
    if role:
        h["x-agentos-role"] = role
    return h


def _load_kb(unique: str) -> Any:
    """动态加载 knowledge_base.py（每次新建，避免模块级 _client 跨测试污染）。"""
    spec = importlib.util.spec_from_file_location(unique, _PLUGIN_DIR / "knowledge_base.py")
    assert spec is not None, "Cannot load knowledge_base.py"
    assert spec.loader is not None, "Cannot load knowledge_base.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


# ── 多 bank hindsight 客户端替身（外部依赖）───────────────────────────────


class _MultiBankDocs:
    """hindsight documents 面替身：bank→doc 二级表（list 分页+tags 过滤 /
    get 404 / delete 移除），bank_id 参与定位（与单 bank 旧替身的本质差异）。"""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}

    def add(self, bank_id: str, doc: dict[str, Any]) -> None:
        self.docs.setdefault(bank_id, {})[doc["id"]] = doc

    @staticmethod
    def _filtered(docs: list[dict[str, Any]], tags: list[str] | None, tags_match: str) -> list[dict[str, Any]]:
        if not tags:
            return docs
        if tags_match == "all":
            return [d for d in docs if set(tags) <= set(d.get("tags") or [])]
        return [d for d in docs if set(tags) & set(d.get("tags") or [])]

    async def list_documents(self, bank_id: str = "", tags: list[str] | None = None,
                             tags_match: str = "any_strict", limit: int = 100,
                             offset: int = 0) -> Any:
        docs = self._filtered(list(self.docs.get(bank_id, {}).values()), tags, tags_match)
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


class FakeKBClient:
    """多 bank hindsight 客户端：aretain 真实落 bank（upload→list 链路闭环）、
    banks.list_banks 发现 + bank config 读写、arecall 按 bank 返回可区分结果。"""

    def __init__(self) -> None:
        self.documents = _MultiBankDocs()
        self._created_banks: list[str] = []
        self._retain_seq = 0
        self.recall_hits: dict[str, list[dict[str, Any]]] = {}
        self.recall_banks: list[str] = []
        self.acreate_bank = AsyncMock(side_effect=self._create_bank)
        self.aretain = AsyncMock(side_effect=self._aretain)
        self.arecall = AsyncMock(side_effect=self._arecall)
        self.banks = SimpleNamespace(
            list_banks=self._list_banks,
            update_bank_config=AsyncMock(return_value={"overrides": {}}),
            get_bank_config=AsyncMock(return_value={"config": {}}),
        )

    async def _create_bank(self, bank_id: str = "", **_kw: Any) -> None:
        if bank_id and bank_id not in self._created_banks:
            self._created_banks.append(bank_id)

    async def _list_banks(self) -> Any:
        ids = sorted({*self._created_banks, *self.documents.docs.keys()})
        return SimpleNamespace(banks=[SimpleNamespace(bank_id=b) for b in ids])

    async def _aretain(self, **kw: Any) -> Any:
        self._retain_seq += 1
        bank = str(kw.get("bank_id", ""))
        doc_id = f"{bank}:chunk-{self._retain_seq}"
        self.documents.add(bank, {
            "id": doc_id,
            "bank_id": bank,
            "original_text": str(kw.get("content", "")),
            "created_at": "2026-11-04T00:00:00+00:00",
            "updated_at": "2026-11-04T00:00:00+00:00",
            "tags": list(kw.get("tags") or []),
            "document_metadata": dict(kw.get("metadata") or {}),
        })
        return SimpleNamespace(operation_id=doc_id, accepted=True)

    async def _arecall(self, **kw: Any) -> Any:
        bank = str(kw.get("bank_id", ""))
        self.recall_banks.append(bank)
        hits = [dict(h) for h in self.recall_hits.get(bank, [])]
        chunks = [SimpleNamespace(model_dump=lambda h=h: dict(h)) for h in hits]
        return SimpleNamespace(chunks=chunks, results=[])


def _bank_doc(doc_id: str, kb_id: str, bank: str, kb_name: str = "doc.md",
              created_at: str = "2026-11-01T00:00:00+00:00", score: float | None = None,
              owner: str = "", tenant: str = "") -> dict[str, Any]:
    """bank 文档形态（DocumentResponse 关键字段；owner/tenant 模拟落章存量差异）。"""
    meta: dict[str, Any] = {
        "kb_id": kb_id,
        "kb_name": kb_name,
        "kb_size": "120",
        "kb_mime": "text/markdown",
        "kb_chunk_total": "1",
        "source": "knowledge_base",
        "created_at": created_at,
        "memory_type": "semantic",
    }
    if owner:
        meta["kb_owner"] = owner
        meta["kb_tenant"] = tenant
    return {
        "id": doc_id,
        "bank_id": bank,
        "original_text": f"{kb_name}-正文-{doc_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "tags": ["type:knowledge", f"kb_item:{kb_id}", "filetype:md"],
        "document_metadata": meta,
    }


@pytest.fixture
def kb() -> Any:
    module = _load_kb("hindsight_kb_identity_test")
    module.set_client(None)
    yield module
    module.set_client(None)


@pytest.fixture
def world(kb: Any, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """kb 模块 + 注入多 bank 假客户端的世界（client 引用回填给断言用）。

    LLM provider env 注入（sidecar 运行时由 _apply_llm_env 写入）——verbatim
    主模式可用的前置，上传走主 bank 落点。
    """
    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")
    client = FakeKBClient()
    kb.set_client(client)
    return SimpleNamespace(kb=kb, client=client)


def _seed(world: SimpleNamespace, bank: str, doc_id: str, kb_id: str, **kw: Any) -> None:
    world.client.documents.add(bank, _bank_doc(doc_id, kb_id, bank, **kw))


# ═══════════════════════════════════════════════════════════
# 读面租户过滤（list/stats/tags/get）
# ═══════════════════════════════════════════════════════════


class TestTenantScopedReads:
    def test_list_scoped_per_tenant(self, world: SimpleNamespace) -> None:
        """租户 A 上传的条目只进 A 视野：B 不可见、admin 全见、匿名空集（X10-F1 主诉求）。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")
        _seed(world, _BANK_B, "c-b1", "item-b", kb_name="b.md")

        items_a = _run(world.kb.list_items(headers=_headers(_TENANT_A, "u-a")))
        assert [it["id"] for it in items_a] == ["item-a"]

        items_b = _run(world.kb.list_items(headers=_headers(_TENANT_B, "u-b")))
        assert [it["id"] for it in items_b] == ["item-b"]

        items_admin = _run(world.kb.list_items(headers=_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert {it["id"] for it in items_admin} == {"item-a", "item-b"}

        assert _run(world.kb.list_items(headers=None)) == []
        assert _run(world.kb.list_items(headers={})) == []

    def test_legacy_bank_is_default_tenant_only(self, world: SimpleNamespace) -> None:
        """存量迁移语义：旧 kb bank 对 default 租户可见（零搬家），他租户从空开始。"""
        _seed(world, _BANK_LEGACY, "c-legacy", "item-legacy", kb_name="seed.md")

        items_default = _run(world.kb.list_items(headers=_headers(_TENANT_DEFAULT, "admin")))
        assert [it["id"] for it in items_default] == ["item-legacy"]

        assert _run(world.kb.list_items(headers=_headers(_TENANT_A, "u-a"))) == []
        assert _run(world.kb.list_items(headers=_headers(_TENANT_B, "u-b"))) == []

    def test_stats_and_tags_scoped(self, world: SimpleNamespace) -> None:
        """stats/tags 只聚合调用方租户 bank；匿名 fail-closed 空集。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")
        _seed(world, _BANK_B, "c-b1", "item-b", kb_name="b.md")

        stats_a = _run(world.kb.get_stats(headers=_headers(_TENANT_A, "u-a")))
        assert stats_a["total"] == 1
        assert stats_a["total_size"] == 120

        tags_b = _run(world.kb.list_tags(headers=_headers(_TENANT_B, "u-b")))
        assert "filetype:md" in tags_b

        stats_anon = _run(world.kb.get_stats(headers=None))
        assert stats_anon == {"total": 0, "categories_count": 0, "tags_count": 0,
                              "total_chunks": 0, "total_size": 0}
        assert _run(world.kb.list_tags(headers=None)) == []

    def test_get_item_scoped_404_cross_tenant(self, world: SimpleNamespace) -> None:
        """by-id 读：本人租户命中；他租户 404（不泄露存在性，X6-F1 同款）。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")

        item = _run(world.kb.get_item("item-a", headers=_headers(_TENANT_A, "u-a")))
        assert item["id"] == "item-a"

        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.get_item("item-a", headers=_headers(_TENANT_B, "u-b")))
        assert ei.value.status_code == 404

        item_admin = _run(world.kb.get_item("item-a", headers=_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert item_admin["id"] == "item-a"


# ═══════════════════════════════════════════════════════════
# 检索租户过滤（arecall 按 bank）
# ═══════════════════════════════════════════════════════════


class TestSearchScoped:
    def test_search_scoped_to_caller_tenant(self, world: SimpleNamespace) -> None:
        """search 只召回调用方租户双 bank（主+降级）；recall 调用面不触碰他租户 bank。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")
        _seed(world, _BANK_B, "c-b1", "item-b", kb_name="b.md")
        world.client.recall_hits = {
            _BANK_A: [{"id": "c-a1", "text": "a正文", "score": 0.9}],
            _BANK_B: [{"id": "c-b1", "text": "b正文", "score": 0.95}],
        }

        result_a = _run(world.kb.search("正文", headers=_headers(_TENANT_A, "u-a")))
        assert result_a["total"] == 1
        assert result_a["results"][0]["item_id"] == "item-a"
        assert world.client.recall_banks == ["kb_chunks_tenant-a", _BANK_A]

        world.client.recall_banks.clear()
        result_b = _run(world.kb.search("正文", headers=_headers(_TENANT_B, "u-b")))
        assert result_b["results"][0]["item_id"] == "item-b"
        assert world.client.recall_banks == ["kb_chunks_tenant-b", _BANK_B]

    def test_search_missing_identity_fail_closed(self, world: SimpleNamespace) -> None:
        """缺身份头：检索空集 + error 降噪（不触任何 bank）。"""
        _seed(world, _BANK_A, "c-a1", "item-a")
        world.client.recall_hits = {_BANK_A: [{"id": "c-a1", "text": "a", "score": 0.9}]}

        result = _run(world.kb.search("正文", headers=None))
        assert result["results"] == []
        assert result["total"] == 0
        assert result["error"]
        assert world.client.recall_banks == []

    def test_search_admin_merges_cross_tenant_by_score(self, world: SimpleNamespace) -> None:
        """admin 运维面：跨租户召回合并按 score 降序截断（X4-F1 归一语义）。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")
        _seed(world, _BANK_B, "c-b1", "item-b", kb_name="b.md")
        world.client.recall_hits = {
            _BANK_A: [{"id": "c-a1", "text": "a正文", "score": 0.6}],
            _BANK_B: [{"id": "c-b1", "text": "b正文", "score": 0.95}],
        }

        result = _run(world.kb.search("正文", headers=_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert [r["item_id"] for r in result["results"]] == ["item-b", "item-a"]

        result_top1 = _run(world.kb.search("正文", top_k=1,
                                           headers=_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert [r["item_id"] for r in result_top1["results"]] == ["item-b"]


# ═══════════════════════════════════════════════════════════
# 上传：租户 bank 落点 + 属主落章
# ═══════════════════════════════════════════════════════════


class TestUploadScoped:
    def test_upload_writes_caller_tenant_bank_with_owner(self, world: SimpleNamespace) -> None:
        """upload 落调用方租户 bank 并落章属主（kb_owner/kb_tenant）；default 沿用
        存量 bank ``kb``（零迁移）。"""
        r_a = _run(world.kb.upload_document("a.md", "租户A文档".encode(), "text/markdown",
                                            headers=_headers(_TENANT_A, "u-a")))
        first_a = world.client.aretain.call_args_list[0].kwargs
        assert first_a["bank_id"] == _BANK_A
        assert first_a["metadata"]["kb_owner"] == "u-a"
        assert first_a["metadata"]["kb_tenant"] == _TENANT_A
        assert f"kb_item:{r_a['item_id']}" in first_a["tags"]
        # 上传后本租户可见、他租户不可见（链路闭环）
        assert [it["id"] for it in _run(world.kb.list_items(headers=_headers(_TENANT_A, "u-a")))] == [r_a["item_id"]]
        assert _run(world.kb.list_items(headers=_headers(_TENANT_B, "u-b"))) == []

        world.client.aretain.reset_mock()
        _run(world.kb.upload_document("seed.md", "存量租户文档".encode(), "text/markdown",
                                      headers=_headers(_TENANT_DEFAULT, "admin")))
        assert world.client.aretain.call_args_list[0].kwargs["bank_id"] == _BANK_LEGACY

    def test_upload_missing_identity_403_no_write(self, world: SimpleNamespace) -> None:
        """缺身份头：写面显式 403，不触 bank（fail-closed 不落写）。"""
        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.upload_document("a.md", "内容".encode(), "text/markdown", headers=None))
        assert ei.value.status_code == 403
        world.client.aretain.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 删除授权（同租户放行 / 跨租户 403 / admin 全通）
# ═══════════════════════════════════════════════════════════


class TestDeleteAuthorization:
    def test_same_tenant_delete_allowed(self, world: SimpleNamespace) -> None:
        """同租户（上传租户）删除放行：条目文档全清。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")

        result = _run(world.kb.delete_item("item-a", headers=_headers(_TENANT_A, "u-b")))
        assert result == {"message": "条目已删除", "id": "item-a"}
        assert world.client.documents.docs[_BANK_A] == {}

    def test_cross_tenant_delete_403_intact(self, world: SimpleNamespace) -> None:
        """跨租户删除：403（真实状态到前端），他租户文档原样保留。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")

        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.delete_item("item-a", headers=_headers(_TENANT_B, "u-b")))
        assert ei.value.status_code == 403
        assert "c-a1" in world.client.documents.docs[_BANK_A]

    def test_foreign_tenant_cannot_delete_legacy(self, world: SimpleNamespace) -> None:
        """存量 bank 归 default 租户：他租户删存量条目 → 403（迁移语义的写面）。"""
        _seed(world, _BANK_LEGACY, "c-legacy", "item-legacy", kb_name="seed.md")

        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.delete_item("item-legacy", headers=_headers(_TENANT_A, "u-a")))
        assert ei.value.status_code == 403
        assert "c-legacy" in world.client.documents.docs[_BANK_LEGACY]

    def test_admin_deletes_cross_tenant(self, world: SimpleNamespace) -> None:
        """admin 运维面：跨租户删除放行。"""
        _seed(world, _BANK_A, "c-a1", "item-a", kb_name="a.md")
        result = _run(world.kb.delete_item("item-a", headers=_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert result == {"message": "条目已删除", "id": "item-a"}
        assert world.client.documents.docs.get(_BANK_A, {}) == {}

    def test_delete_missing_identity_403(self, world: SimpleNamespace) -> None:
        """缺身份头：写面 fail-closed 403。"""
        _seed(world, _BANK_A, "c-a1", "item-a")
        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.delete_item("item-a", headers=None))
        assert ei.value.status_code == 403
        assert "c-a1" in world.client.documents.docs[_BANK_A]

    def test_delete_unknown_id_404(self, world: SimpleNamespace) -> None:
        """全域无此条目 → 404（定位 0 文档语义保持）。"""
        _seed(world, _BANK_A, "c-a1", "item-a")
        with pytest.raises(world.kb.KBError) as ei:
            _run(world.kb.delete_item("ghost", headers=_headers(_TENANT_A, "u-a")))
        assert ei.value.status_code == 404


# ═══════════════════════════════════════════════════════════
# check：身份感知可用性报告
# ═══════════════════════════════════════════════════════════


class TestCheckScoped:
    def test_check_reports_caller_bank(self, world: SimpleNamespace) -> None:
        """check 报告调用方租户 bank（A → kb_tenant-a；default → 存量 kb）。"""
        check_a = _run(world.kb.check_available(headers=_headers(_TENANT_A, "u-a")))
        assert check_a["available"] is True
        assert check_a["bank"] == _BANK_A

        check_default = _run(world.kb.check_available(headers=_headers(_TENANT_DEFAULT, "admin")))
        assert check_default["bank"] == _BANK_LEGACY

    def test_check_missing_identity_unavailable(self, world: SimpleNamespace) -> None:
        """缺身份头：check 如实报告不可用（fail-closed，不预置可用面）。"""
        check = _run(world.kb.check_available(headers=None))
        assert check["available"] is False


# ═══════════════════════════════════════════════════════════
# bank 发现与跨 bank 面（发现失败/缺能力只收窄不放大）
# ═══════════════════════════════════════════════════════════


class TestBankDiscovery:
    def test_discover_parses_object_and_dict_and_model_dump_forms(
        self, kb: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """banks.list_banks 三种回包形态（pydantic model_dump / dict / 对象）全解析。"""
        world_client = FakeKBClient()
        kb.set_client(world_client)

        async def obj_form() -> Any:
            return SimpleNamespace(banks=[SimpleNamespace(bank_id=_BANK_A), "kb_noise"])

        async def dict_form() -> Any:
            return {"banks": [{"bank_id": _BANK_B}, {"id": "无 bank_id 键忽略"}]}

        async def model_dump_form() -> Any:
            return SimpleNamespace(model_dump=lambda: {"banks": [{"bank_id": _BANK_LEGACY}]})

        monkeypatch.setattr(world_client.banks, "list_banks", obj_form)
        assert _run(kb._discover_kb_banks()) == [_BANK_A]

        monkeypatch.setattr(world_client.banks, "list_banks", dict_form)
        assert _run(kb._discover_kb_banks()) == [_BANK_B]

        monkeypatch.setattr(world_client.banks, "list_banks", model_dump_form)
        assert _run(kb._discover_kb_banks()) == [_BANK_LEGACY]

    def test_discover_without_capability_or_client_returns_empty(self, kb: Any) -> None:
        """client 无 banks.list_banks 能力 / client 缺席 → 空集（跨 bank 面 fail-closed）。"""
        kb.set_client(SimpleNamespace(documents=_MultiBankDocs()))  # 无 banks 属性
        assert _run(kb._discover_kb_banks()) == []
        kb.set_client(SimpleNamespace(banks=SimpleNamespace(list_banks="not-callable")))
        assert _run(kb._discover_kb_banks()) == []
        kb.set_client(None)
        assert _run(kb._discover_kb_banks()) == []

    def test_admin_degrades_to_own_bank_when_discovery_broken(self, kb: Any) -> None:
        """admin：bank 发现故障 → 只见自身双 bank（只收窄不放大）。"""
        kb.set_client(SimpleNamespace(documents=_MultiBankDocs()))  # 无 bank 发现能力
        banks = _run(kb._caller_banks(_headers(_TENANT_DEFAULT, "admin", "admin")))
        assert banks == [_BANK_LEGACY, "kb_chunks"]

    def test_foreign_probe_failure_deletes_degrade_to_own_bank(
        self, kb: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨租户归属探测故障：按「不在他 bank」处理（403 精判退化为本 bank 404）。"""
        client = FakeKBClient()
        kb.set_client(client)
        _seed(SimpleNamespace(client=client), _BANK_A, "c-a1", "item-a")

        real_list = client.documents.list_documents

        async def foreign_probe_down(bank_id: str = "", **kw: Any) -> Any:
            if bank_id == _BANK_A:  # 外租 bank（B 的他者面）探测故障
                raise RuntimeError("probe down")
            return await real_list(bank_id=bank_id, **kw)

        monkeypatch.setattr(client.documents, "list_documents", foreign_probe_down)
        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("item-a", headers=_headers(_TENANT_B, "u-b")))
        assert ei.value.status_code == 404
        assert "c-a1" in client.documents.docs[_BANK_A]  # 原样保留

    def test_bank_documents_truncation_warns(
        self, kb: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """单 bank 超页界截断：聚合继续（诚实计数）+ warn 留痕。"""
        client = FakeKBClient()
        kb.set_client(client)
        for i in range(5):
            client.documents.add(_BANK_LEGACY, _bank_doc(f"c{i}", f"item-{i}", _BANK_LEGACY))
        monkeypatch.setattr(kb, "_KB_LIST_PAGE_SIZE", 2)
        monkeypatch.setattr(kb, "_KB_LIST_MAX_PAGES", 1)

        with caplog.at_level("WARNING"):
            items = _run(kb.list_items(headers=_headers(_TENANT_DEFAULT, "admin")))

        assert len(items) == 2  # 截断面：只聚合首页
        assert any("分页达上限" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# dispatch 端到端（FP-0.2.六）：http_handle 全链路身份过滤
# ═══════════════════════════════════════════════════════════


def _decode_http(out: dict[str, Any]) -> tuple[int, Any]:
    """http.handle 出参 → (HTTP 状态, 解码后 body)——kb 列表端点 body 可为数组。"""
    data = out["data"]
    body = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return int(data["status"]), body


def _multipart_body(filename: str, content: str) -> tuple[str, str]:
    boundary = "kbIdentityBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return base64.b64encode(body).decode("ascii"), f"multipart/form-data; boundary={boundary}"


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """端到端测试台：canonical knowledge_base（server 分发解析名）+ fresh server。"""
    import knowledge_base as kb  # 裸名导入 = server 分发解析到的同一模块

    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")

    kb.set_client(None)
    spec = importlib.util.spec_from_file_location(
        "hindsight_server_kb_identity_dispatch", _PLUGIN_DIR / "server.py")
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    srv = importlib.util.module_from_spec(spec)
    sys.modules["hindsight_server_kb_identity_dispatch"] = srv
    spec.loader.exec_module(srv)

    client = FakeKBClient()
    srv._client = client
    _seed(SimpleNamespace(client=client), _BANK_A, "c-a1", "item-a", kb_name="a.md")
    _seed(SimpleNamespace(client=client), _BANK_B, "c-b1", "item-b", kb_name="b.md")
    yield SimpleNamespace(srv=srv, kb=kb, client=client)
    srv._client = None
    kb.set_client(None)


class TestKbIdentityDispatch:
    def test_dispatch_list_scoped_and_anonymous_empty(self, dispatch: SimpleNamespace) -> None:
        """GET 列表：A 只见 A 条目、B 只见 B、无身份头 → 空数组（200）。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=_KB_PREFIX, method="GET", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_A, "u-a"),
        )))
        assert status == 200
        assert [it["id"] for it in body] == ["item-a"]

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=_KB_PREFIX, method="GET", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_B, "u-b"),
        )))
        assert status == 200
        assert [it["id"] for it in body] == ["item-b"]

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=_KB_PREFIX, method="GET", plugin_id="hindsight_memory_service", headers=None,
        )))
        assert status == 200
        assert body == []

    def test_dispatch_search_scoped(self, dispatch: SimpleNamespace) -> None:
        """GET /search：query 命中双 bank 时只回归属内条目。"""
        dispatch.client.recall_hits = {
            _BANK_A: [{"id": "c-a1", "text": "正文", "score": 0.9}],
            _BANK_B: [{"id": "c-b1", "text": "正文", "score": 0.95}],
        }
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/search", method="GET", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_A, "u-a"), query={"query": "正文"},
        )))
        assert status == 200
        assert [r["item_id"] for r in body["results"]] == ["item-a"]

    def test_dispatch_cross_tenant_delete_403_admin_ok(self, dispatch: SimpleNamespace) -> None:
        """DELETE：B 删 A 条目 → 403 且文档保留；admin 跨租户删除放行。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/item-a", method="DELETE", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_B, "u-b"),
        )))
        assert status == 403
        assert "c-a1" in dispatch.client.documents.docs[_BANK_A]

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/item-a", method="DELETE", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_DEFAULT, "admin", "admin"),
        )))
        assert status == 200
        assert body == {"message": "条目已删除", "id": "item-a"}

    def test_dispatch_upload_scoped_bank(self, dispatch: SimpleNamespace) -> None:
        """POST /upload：A 上传落 kb_tenant-a；无身份头上传 → 403。"""
        raw_body, content_type = _multipart_body("a.md", "租户A上传内容 " * 50)
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/upload", method="POST", plugin_id="hindsight_memory_service",
            raw_body=raw_body, headers={**_headers(_TENANT_A, "u-a"), "Content-Type": content_type},
        )))
        assert status == 200
        assert body["message"] == "文件上传成功"
        first = dispatch.client.aretain.call_args_list[0].kwargs
        assert first["bank_id"] == _BANK_A
        assert first["metadata"]["kb_tenant"] == _TENANT_A

        raw_body, content_type = _multipart_body("anon.md", "匿名内容")
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/upload", method="POST", plugin_id="hindsight_memory_service",
            raw_body=raw_body, headers={"Content-Type": content_type},
        )))
        assert status == 403

    def test_dispatch_get_item_cross_tenant_404(self, dispatch: SimpleNamespace) -> None:
        """GET /{item_id}：他租户条目结构化 404（不泄露存在性）。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{_KB_PREFIX}/item-b", method="GET", plugin_id="hindsight_memory_service",
            headers=_headers(_TENANT_A, "u-a"),
        )))
        assert status == 404
        assert body["detail"] == "未找到知识库条目"
