# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""by-id 操作面跨 bank 化 TDD（BUG-76 修复「登记不实施」项：记忆页查看/删除单条）。

行为锚点：
- get/delete 单条记忆按 id 逐会话 bank（thread-* + 默认 bank，与列表面同集合
  同序）探测定位，会话 bank 落库的记忆（memory 工具全部写入）在页面可见即可
  查看/删除——列表面跨 bank 而 by-id 只查默认 bank 的半残面在此收口。
- 跨 bank 命中（bank != 默认 bank）记日志（管理面放行可审计）。
- 响应键面冻结：get 详情六键、delete 消息体不变；未命中任何 bank 结构化 404
  （MemoryAPIError MEM_NOTF_5001）。

测试分两层：路由层替身（逐 bank 行为精确可控）+ 真实链路（sidecar 真实
retain/delete handler + 内存 hindsight 假件，test_routes_listing_chain 同款）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _load_routes() -> Any:
    """动态加载 routes_memory.py（独立模块名，避免跨文件模块级状态污染）。"""
    mod_name = "hindsight_routes_byid_cross_bank"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "routes_memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _doc(id_: str, text: str, mtype: str = "semantic", created_at: str = "2026-09-20T00:00:00Z") -> dict[str, Any]:
    """构造 sidecar documents 面条目（retain 注入 type:* 服务端标签）。"""
    return {
        "id": id_,
        "original_text": text,
        "tags": [f"type:{mtype}"],
        "document_metadata": {"memory_type": mtype},
        "created_at": created_at,
    }


class StubBankBackend:
    """逐 bank 行为可控的 IMemoryBackend 替身（documents 通路 + delete）。"""

    def __init__(
        self,
        banks: list[str],
        docs_by_bank: dict[str, dict[str, dict[str, Any]]] | None = None,
        deletable: set[tuple[str, str]] | None = None,
    ) -> None:
        self.banks = banks
        self.docs_by_bank = docs_by_bank or {}
        self.deletable = deletable or set()

    async def list_banks(self) -> list[str]:
        return list(self.banks)

    async def get_documents(self, user_id: str, document_id: str = "", limit: int = 20, **kw: Any) -> list[dict[str, Any]]:
        doc = self.docs_by_bank.get(user_id, {}).get(document_id) if document_id else None
        return [dict(doc)] if doc else []

    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        return (user_id, str(memory_id)) in self.deletable


@pytest.fixture
def routes() -> Any:
    module = _load_routes()
    module._memory_backend = None
    return module


class TestGetMemoryCrossBank:
    @pytest.mark.parametrize("host_bank", ["thread-abc", "thread-xyz", "default"])
    def test_get_locates_memory_wherever_it_landed(self, routes: Any, host_bank: str) -> None:
        """记忆落在任一会话/默认 bank 都可按 id 查看（列表面可见即可查看）。"""
        routes.set_memory_backend(StubBankBackend(
            banks=["thread-abc", "thread-xyz"],
            docs_by_bank={host_bank: {"mem-1": _doc("mem-1", "蓝鲸关键词记忆")}},
        ))

        result = _run(routes.get_memory("mem-1"))

        assert result["id"] == "mem-1"
        assert result["content"] == "蓝鲸关键词记忆"

    def test_get_response_keys_frozen(self, routes: Any) -> None:
        """详情响应键面冻结：id/content/memory_type/tags/score/created_at。"""
        routes.set_memory_backend(StubBankBackend(
            banks=["thread-abc"],
            docs_by_bank={"thread-abc": {"mem-1": _doc("mem-1", "正文", "episode", "2026-09-21T08:00:00Z")}},
        ))

        result = _run(routes.get_memory("mem-1"))

        assert set(result) == {"id", "content", "memory_type", "tags", "score", "created_at"}
        assert result["memory_type"] == "episode"
        assert result["created_at"] == "2026-09-21T08:00:00Z"
        assert result["score"] == 0.0

    def test_get_cross_bank_hit_logged(self, routes: Any, caplog: pytest.LogCaptureFixture) -> None:
        """跨 bank 命中（bank != 默认）必须留日志（管理面放行可审计）。"""
        routes.set_memory_backend(StubBankBackend(
            banks=["thread-abc"],
            docs_by_bank={"thread-abc": {"mem-1": _doc("mem-1", "正文")}},
        ))

        with caplog.at_level("INFO"):
            _run(routes.get_memory("mem-1"))

        assert any("mem-1" in r.getMessage() and "thread-abc" in r.getMessage() for r in caplog.records)

    def test_get_missing_id_structured_404(self, routes: Any) -> None:
        """全部 bank 未命中 → 结构化 404（MEM_NOTF_5001）。"""
        routes.set_memory_backend(StubBankBackend(banks=["thread-abc"], docs_by_bank={}))

        with pytest.raises(routes.MemoryAPIError) as ei:
            _run(routes.get_memory("mem-gone"))
        assert ei.value.status_code == 404
        assert ei.value.error_code == "MEM_NOTF_5001"

    def test_get_without_backend_degrades_404(self, routes: Any) -> None:
        """后端未注入（降级态）→ 404，不崩溃。"""
        with pytest.raises(routes.MemoryAPIError) as ei:
            _run(routes.get_memory("mem-1"))
        assert ei.value.status_code == 404


class TestDeleteMemoryCrossBank:
    @pytest.mark.parametrize("host_bank", ["thread-abc", "thread-xyz", "default"])
    def test_delete_locates_memory_wherever_it_landed(self, routes: Any, host_bank: str) -> None:
        """记忆落在任一 bank 都可按 id 删除。"""
        routes.set_memory_backend(StubBankBackend(
            banks=["thread-abc", "thread-xyz"],
            deletable={(host_bank, "mem-1")},
        ))

        result = _run(routes.delete_memory("mem-1"))

        assert result == {"message": "记忆已删除"}

    def test_delete_cross_bank_hit_logged(self, routes: Any, caplog: pytest.LogCaptureFixture) -> None:
        """跨 bank 删除命中必须留日志。"""
        routes.set_memory_backend(StubBankBackend(
            banks=["thread-abc"],
            deletable={("thread-abc", "mem-1")},
        ))

        with caplog.at_level("INFO"):
            _run(routes.delete_memory("mem-1"))

        assert any("mem-1" in r.getMessage() and "thread-abc" in r.getMessage() for r in caplog.records)

    def test_delete_missing_id_structured_404(self, routes: Any) -> None:
        """全部 bank 删除未命中 → 结构化 404。"""
        routes.set_memory_backend(StubBankBackend(banks=["thread-abc"]))

        with pytest.raises(routes.MemoryAPIError) as ei:
            _run(routes.delete_memory("mem-gone"))
        assert ei.value.status_code == 404
        assert ei.value.error_code == "MEM_NOTF_5001"

    def test_delete_without_backend_degrades_404(self, routes: Any) -> None:
        """后端未注入（降级态）→ 404，不崩溃。"""
        with pytest.raises(routes.MemoryAPIError) as ei:
            _run(routes.delete_memory("mem-1"))
        assert ei.value.status_code == 404


# ── 真实链路：sidecar 真实 retain/delete handler + 内存 hindsight 假件 ─────────


class FakeDocumentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class FakeDocumentsAPI:
    """内存 documents API：get_document 定向取回 + delete_document 级联删除。"""

    def __init__(self, banks: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._banks = banks

    async def get_document(self, *, bank_id: str, document_id: str) -> dict[str, Any]:
        doc = self._banks.get(bank_id, {}).get(document_id)
        if doc is None:
            raise FakeDocumentError(404, f"document {document_id} not found")
        return dict(doc)

    async def delete_document(self, *, bank_id: str, document_id: str) -> dict[str, Any]:
        bank = self._banks.get(bank_id, {})
        if document_id not in bank:
            raise FakeDocumentError(404, f"document {document_id} not found")
        del bank[document_id]
        return {"success": True}


class FakeBanksAPI:
    def __init__(self, banks: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._banks = banks

    async def list_banks(self) -> Any:
        items = [SimpleNamespace(bank_id=bid) for bid in self._banks]
        return SimpleNamespace(banks=items)


class FakeHindsightClient:
    """内存 hindsight 客户端：aretain 落库 + documents/banks 面读写。"""

    def __init__(self) -> None:
        self.bank_store: dict[str, dict[str, dict[str, Any]]] = {}
        self.documents = FakeDocumentsAPI(self.bank_store)
        self.banks = FakeBanksAPI(self.bank_store)

    async def aretain(self, **kwargs: Any) -> Any:
        bank = kwargs.get("bank_id", "")
        doc_id = kwargs.get("document_id") or f"auto-{len(self.bank_store.get(bank, {})) + 1}"
        self.bank_store.setdefault(bank, {})[doc_id] = {
            "id": doc_id,
            "original_text": kwargs.get("content", ""),
            "tags": [str(t) for t in (kwargs.get("tags") or [])],
            "document_metadata": dict(kwargs.get("metadata") or {}),
            "created_at": "2026-09-20T00:00:00Z",
        }
        return SimpleNamespace(success=True, operation_id=None)


def _load_server() -> Any:
    mod_name = "hindsight_byid_chain_sidecar"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chain() -> dict[str, Any]:
    """真实 sidecar + 真实 routes，经 HindsightBackend 直连（tool-executor 替身）。"""
    hmod = _load_server()
    fake_client = FakeHindsightClient()
    hmod._client = fake_client

    async def caller(method: str, params: dict[str, Any]) -> Any:
        assert method == "tool-executor.invoke", method
        result = hmod.plugin._tools[params["tool_name"]].handler(**params["args"])
        if asyncio.iscoroutine(result):
            result = await result
        return result

    from memory_backend import HindsightBackend

    rmod = _load_routes()
    rmod.set_memory_backend(HindsightBackend(caller))
    return {"hindsight": hmod, "routes": rmod, "client": fake_client}


def _retain(chain: dict[str, Any], bank_id: str, doc_id: str, text: str) -> None:
    """经真实 retain handler 落一条记忆（生产同款写入路径）。"""
    handler = chain["hindsight"].plugin._tools["hindsight.retain"].handler
    _run(handler(bank_id=bank_id, content=text, memory_type="semantic", document_id=doc_id))


class TestByIdRealChain:
    def test_session_bank_memory_get_delete_roundtrip(self, chain: dict[str, Any]) -> None:
        """会话 bank 记忆：get 详情命中 → delete 成功 → get 转 404（真删）。"""
        _retain(chain, "thread-ebae9800", "mem-bluewhale", "用户测试关键词是蓝鲸")

        detail = _run(chain["routes"].get_memory("mem-bluewhale"))
        assert detail["content"] == "用户测试关键词是蓝鲸"

        result = _run(chain["routes"].delete_memory("mem-bluewhale"))
        assert result == {"message": "记忆已删除"}

        with pytest.raises(chain["routes"].MemoryAPIError) as ei:
            _run(chain["routes"].get_memory("mem-bluewhale"))
        assert ei.value.status_code == 404

    def test_default_bank_memory_still_manageable(self, chain: dict[str, Any]) -> None:
        """默认 bank 记忆 by-id 语义回归：查看/删除照常。"""
        _retain(chain, "default", "mem-legacy", "默认 bank 旧记忆")

        detail = _run(chain["routes"].get_memory("mem-legacy"))
        assert detail["content"] == "默认 bank 旧记忆"
        result = _run(chain["routes"].delete_memory("mem-legacy"))
        assert result == {"message": "记忆已删除"}

    def test_missing_id_real_chain_structured_404(self, chain: dict[str, Any]) -> None:
        """真实链路不存在 id：get/delete 都结构化 404。"""
        _retain(chain, "thread-abc", "mem-real", "真实记忆")

        with pytest.raises(chain["routes"].MemoryAPIError) as ei:
            _run(chain["routes"].get_memory("mem-gone"))
        assert ei.value.status_code == 404
        with pytest.raises(chain["routes"].MemoryAPIError) as ei:
            _run(chain["routes"].delete_memory("mem-gone"))
        assert ei.value.status_code == 404
