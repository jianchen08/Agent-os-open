# @feature: FP-0.2.二 memory tool 缺口分支补测 | @ci: python-coverage
"""MemoryTool/server/backend 缺口分支补测——守卫族、异常翻译族、降级族。

覆盖面（批五覆盖率冲刺，接 test_tool.py / test_backend.py 既有契约测试）：
- tool.py 各 action 的 backend 未注入守卫、缺参守卫、异常 → failure 翻译
- update 原生 update 路径（类上探测）vs add 降级路径的分流
- get_context 的 query 回落（session_id 兜底）与默认 top_k
- _extract_memory_id 的 memory:// 前缀剥离；_inject_agent_tags 的
  agent_config_id 注入与去重
- server.py 能力句柄缺失 → 后端不可用降级链（_make_capability_caller /
  _build_memory_backend）与 _get_memory_backend 懒构建缓存（失败不锁死）
- backend.py wire 装配分支：document_id/update_mode/memory_type 透传、
  recall 非 dict 非 list 载荷、条目跳过、delete 无信封返回

Mock 约定：capability_caller / memory backend 属跨进程外部依赖，按测试
纪律用 AsyncMock / 哑类；其余全真实。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import backend as backend_mod  # noqa: E402
from backend import HindsightBackend  # noqa: E402


def _load_module(rel: str, mod_name: str) -> Any:
    module_path = _PLUGIN_DIR / rel
    assert module_path.exists(), f"{rel} missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module("tool.py", "memory_tool_branches")


@pytest.fixture
def backend() -> AsyncMock:
    b = AsyncMock()
    b.add.return_value = "mem-1"
    b.search.return_value = [
        {"id": "m1", "content": "alpha", "score": 0.9, "metadata": {}}
    ]
    b.delete.return_value = True
    b.import_document.return_value = {"chunks_imported": 2, "name": "doc"}
    return b


@pytest.fixture
def tool_inst(mod: Any, backend: AsyncMock) -> Any:
    t = mod.MemoryTool(memory_backend=backend)
    t.set_trusted_user_id("tester")
    return t


# ═══════════════════════════════════════════════════════════
# 锚点提取 / 标签注入的边界
# ═══════════════════════════════════════════════════════════


class TestAnchorAndTags:
    async def test_delete_strips_memory_scheme_prefix(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """memory:// 前缀锚点在 delete 定向时剥离为裸 id。"""
        result = await tool_inst.execute(
            {"action": "delete", "memory_id": "memory://mem-9"}
        )
        assert result.output["deleted"] is True
        assert backend.delete.await_args.kwargs["memory_id"] == "mem-9"

    async def test_store_injects_agent_config_id_tag(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """agent_config_id 自动注入为内容归属标签。"""
        await tool_inst.execute(
            {
                "action": "store",
                "content": "x",
                "tags": ["t1"],
                "agent_config_id": "agent-A",
                "session_id": "sess-7",
            }
        )
        tags = backend.add.await_args.kwargs["tags"]
        assert "agent-A" in tags
        assert "session:sess-7" in tags
        assert "t1" in tags

    async def test_agent_config_id_not_duplicated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """agent_config_id 已在 tags 中时不重复注入。"""
        await tool_inst.execute(
            {
                "action": "store",
                "content": "x",
                "tags": ["agent-A"],
                "agent_config_id": "agent-A",
            }
        )
        tags = backend.add.await_args.kwargs["tags"]
        assert tags.count("agent-A") == 1


# ═══════════════════════════════════════════════════════════
# backend 未注入守卫（全 action 族）
# ═══════════════════════════════════════════════════════════


class TestBackendMissingGuards:
    @pytest.fixture
    def bare_tool(self, mod: Any) -> Any:
        t = mod.MemoryTool(memory_backend=None)
        t.set_trusted_user_id("tester")
        return t

    async def test_execute_fails_closed_without_backend(
        self, bare_tool: Any
    ) -> None:
        """公共入口集中守卫：backend 缺失时任何 action 都失败。"""
        result = await bare_tool.execute({"action": "store", "content": "x"})
        assert result.success is False
        assert "memory backend 未注入" in result.error

    # ── 纵深防御：各 action 方法自持同款守卫（绕过 execute 直测）──

    async def test_store_guard(self, mod: Any, bare_tool: Any) -> None:
        result = await bare_tool._store({"content": "x"})
        assert "memory backend 未注入" in result.error

    async def test_retrieve_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._retrieve({"query": "q"})
        assert "memory backend 未注入" in result.error

    async def test_import_text_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._import_text({"content": "x", "name": "n"})
        assert "memory backend 未注入" in result.error

    async def test_import_file_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._import_file({"file_path": "a.txt"})
        assert "memory backend 未注入" in result.error

    async def test_update_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._update({"content": "x", "memory_id": "m1"})
        assert "memory backend 未注入" in result.error

    async def test_delete_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._delete({"memory_id": "m1"})
        assert "memory backend 未注入" in result.error

    async def test_get_context_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._get_context({"query": "q"})
        assert "memory backend 未注入" in result.error

    async def test_list_guard(self, bare_tool: Any) -> None:
        result = await bare_tool._list({})
        assert "memory backend 未注入" in result.error


# ═══════════════════════════════════════════════════════════
# 缺参守卫 + 异常翻译（store/retrieve/import_text/import_file/update/delete/list）
# ═══════════════════════════════════════════════════════════


class TestParamGuards:
    async def test_store_without_content(self, tool_inst: Any) -> None:
        result = await tool_inst.execute({"action": "store"})
        assert result.success is False
        assert "缺少 content" in result.error

    async def test_store_empty_backend_id_is_unconfirmed_write(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """backend 返回空 id = 写入未确认，必须报失败而非假成功。"""
        backend.add.return_value = ""
        result = await tool_inst.execute({"action": "store", "content": "x"})
        assert result.success is False
        assert "写入未确认" in result.error

    async def test_store_backend_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.add.side_effect = RuntimeError("连接断开")
        result = await tool_inst.execute({"action": "store", "content": "x"})
        assert result.success is False
        assert "存储失败" in result.error
        assert "连接断开" in result.error

    async def test_retrieve_without_query(self, tool_inst: Any) -> None:
        result = await tool_inst.execute({"action": "retrieve"})
        assert result.success is False
        assert "query" in result.error

    async def test_retrieve_backend_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.search.side_effect = RuntimeError("召回超时")
        result = await tool_inst.execute({"action": "retrieve", "query": "q"})
        assert result.success is False
        assert "检索失败" in result.error
        assert "召回超时" in result.error

    @pytest.mark.parametrize(
        "inputs, fragment",
        [
            ({"action": "import_text", "name": "n"}, "缺少 content"),
            ({"action": "import_text", "content": "x"}, "缺少 name"),
            ({"action": "import_file"}, "缺少 file_path"),
            ({"action": "update", "memory_id": "m1"}, "content"),
            ({"action": "delete"}, "缺少 memory_id"),
        ],
    )
    async def test_missing_param_guards(
        self, tool_inst: Any, inputs: dict[str, Any], fragment: str
    ) -> None:
        result = await tool_inst.execute(inputs)
        assert result.success is False
        assert fragment in result.error

    async def test_import_text_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.import_document.side_effect = RuntimeError("抽取服务超载")
        result = await tool_inst.execute(
            {"action": "import_text", "content": "x", "name": "n"}
        )
        assert result.success is False
        assert "导入文本失败" in result.error
        assert "抽取服务超载" in result.error

    async def test_import_file_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.import_document.side_effect = RuntimeError("文件不可读")
        result = await tool_inst.execute(
            {"action": "import_file", "file_path": "知识.md"}
        )
        assert result.success is False
        assert "导入文件失败" in result.error
        assert "文件不可读" in result.error

    async def test_import_file_workspace_root_translates_to_empty(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """/workspace 根路径本身翻译为空串（相对 workspace 解析边界）。"""
        result = await tool_inst.execute(
            {"action": "import_file", "file_path": "/workspace", "name": "根"}
        )
        assert result.success is True
        kwargs = backend.import_document.await_args.kwargs
        assert kwargs["file_path"] == ""
        assert kwargs["name"] == "根"

    async def test_update_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.add.side_effect = RuntimeError("写入降级也失败")
        result = await tool_inst.execute(
            {"action": "update", "content": "x", "memory_id": "m1"}
        )
        assert result.success is False
        assert "更新失败" in result.error

    async def test_list_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.search.side_effect = RuntimeError("列举失败注入")
        result = await tool_inst.execute({"action": "list"})
        assert result.success is False
        assert "列举失败" in result.error


# ═══════════════════════════════════════════════════════════
# update 分流：原生 update（类上探测）vs add 降级
# ═══════════════════════════════════════════════════════════


class _NativeUpdateBackend:
    """类上带原生 update 的后端——探测在 type 上做，实例级属性探不到。"""

    def __init__(self) -> None:
        self.update_kwargs: dict[str, Any] = {}

    async def update(
        self, *, user_id: str, memory_id: str, content: str, tags: list[str]
    ) -> str:
        self.update_kwargs = {
            "user_id": user_id,
            "memory_id": memory_id,
            "content": content,
            "tags": tags,
        }
        return "mem-native"

    async def add(self, **_kwargs: Any) -> str:
        raise AssertionError("原生 update 存在时不得走 add 降级")


class _NativeUpdateFailingBackend(_NativeUpdateBackend):
    async def update(self, **_kwargs: Any) -> str:
        raise RuntimeError("原生更新失败")


class TestUpdateDispatch:
    async def test_native_update_path_used_when_class_provides_it(self, mod: Any) -> None:
        """类上有 callable update → 走原生路径，返回 updated=True 且无 degraded。"""
        b = _NativeUpdateBackend()
        t = mod.MemoryTool(memory_backend=b)
        t.set_trusted_user_id("tester")
        result = await t.execute(
            {
                "action": "update",
                "content": "新内容",
                "memory_id": "mem-8",
                "tags": ["t"],
            }
        )
        assert result.success is True
        assert result.output["memory_id"] == "mem-native"
        assert result.output["updated"] is True
        assert "degraded" not in result.output
        assert b.update_kwargs["memory_id"] == "mem-8"

    async def test_native_update_exception_translated(self, mod: Any) -> None:
        b = _NativeUpdateFailingBackend()
        t = mod.MemoryTool(memory_backend=b)
        t.set_trusted_user_id("tester")
        result = await t.execute(
            {"action": "update", "content": "x", "memory_id": "m1"}
        )
        assert result.success is False
        assert "更新失败" in result.error
        assert "原生更新失败" in result.error


# ═══════════════════════════════════════════════════════════
# get_context：query 回落 / 默认 top_k / 异常翻译
# ═══════════════════════════════════════════════════════════


class TestGetContext:
    async def test_query_falls_back_to_session_id(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """无 query 时以 session_id 作为检索词（会话上下文语义）。"""
        result = await tool_inst.execute(
            {"action": "get_context", "session_id": "sess-3"}
        )
        assert result.success is True
        kwargs = backend.search.await_args.kwargs
        assert kwargs["query"] == "sess-3"
        assert kwargs["top_k"] == 10

    async def test_explicit_query_and_top_k_win(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        result = await tool_inst.execute(
            {"action": "get_context", "query": "部署", "top_k": 3}
        )
        kwargs = backend.search.await_args.kwargs
        assert kwargs["query"] == "部署"
        assert kwargs["top_k"] == 3

    async def test_empty_search_returns_empty_results(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.search.return_value = None
        result = await tool_inst.execute({"action": "get_context", "query": "q"})
        assert result.output["results"] == []

    async def test_exception_translated(
        self, backend: AsyncMock, tool_inst: Any
    ) -> None:
        backend.search.side_effect = RuntimeError("上下文检索崩了")
        result = await tool_inst.execute({"action": "get_context", "query": "q"})
        assert result.success is False
        assert "获取上下文失败" in result.error


# ═══════════════════════════════════════════════════════════
# server.py：能力缺失降级链 + 懒构建缓存
# ═══════════════════════════════════════════════════════════


class TestServerBackendWiring:
    @pytest.fixture
    def server_mod(self, monkeypatch) -> Any:
        m = _load_module("server.py", "memory_server_branches")
        monkeypatch.setattr(m, "_memory_backend", None, raising=False)
        return m

    async def test_capability_handle_missing_degrades_to_none(
        self, server_mod: Any, monkeypatch
    ) -> None:
        """tool-executor 能力未注入（KeyError）→ caller None → 后端 None。"""

        def _raise(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(server_mod.plugin, "get_capability", _raise)
        assert server_mod._make_capability_caller() is None
        assert server_mod._build_memory_backend() is None

    async def test_capability_handle_binds_caller(
        self, server_mod: Any, monkeypatch
    ) -> None:
        """句柄存在 → bind_capability_caller 以 (handle, plugin_id) 装配。"""
        sentinel_handle = object()
        monkeypatch.setattr(
            server_mod.plugin, "get_capability", lambda name: sentinel_handle
        )
        bound: dict[str, Any] = {}

        def _fake_bind(handle: Any, plugin_id: str) -> str:
            bound["handle"] = handle
            bound["plugin_id"] = plugin_id
            return "caller"

        monkeypatch.setattr(server_mod, "bind_capability_caller", _fake_bind)
        assert server_mod._make_capability_caller() == "caller"
        assert bound["handle"] is sentinel_handle
        assert bound["plugin_id"] == "tool-executor"

    async def test_build_backend_success_uses_factory(
        self, server_mod: Any, monkeypatch
    ) -> None:
        monkeypatch.setattr(server_mod, "_make_capability_caller", lambda: "caller")
        monkeypatch.setattr(
            backend_mod, "get_memory_backend", lambda config, capability_caller: "built"
        )
        assert server_mod._build_memory_backend() == "built"

    async def test_build_backend_factory_failure_returns_none(
        self, server_mod: Any, monkeypatch
    ) -> None:
        """工厂抛异常 → 后端 None（工具降级），sidecar 不崩。"""
        monkeypatch.setattr(server_mod, "_make_capability_caller", lambda: "caller")

        def _boom(config: Any, capability_caller: Any) -> Any:
            raise RuntimeError("工厂爆炸")

        monkeypatch.setattr(backend_mod, "get_memory_backend", _boom)
        assert server_mod._build_memory_backend() is None

    async def test_lazy_build_caches_success(
        self, server_mod: Any, monkeypatch
    ) -> None:
        calls = {"n": 0}

        def _build() -> Any:
            calls["n"] += 1
            return "cached-backend"

        monkeypatch.setattr(server_mod, "_build_memory_backend", _build)
        assert server_mod._get_memory_backend() == "cached-backend"
        assert server_mod._get_memory_backend() == "cached-backend"
        assert calls["n"] == 1

    async def test_lazy_build_failure_does_not_lock_in(
        self, server_mod: Any, monkeypatch
    ) -> None:
        """构建失败不缓存 None——下次调用重试（竞态不死锁后端）。"""
        calls = {"n": 0}

        def _build() -> Any:
            calls["n"] += 1
            return None

        monkeypatch.setattr(server_mod, "_build_memory_backend", _build)
        assert server_mod._get_memory_backend() is None
        assert server_mod._get_memory_backend() is None
        assert calls["n"] == 2


# ═══════════════════════════════════════════════════════════
# backend.py：wire 装配分支与 recall 载荷容错
# ═══════════════════════════════════════════════════════════


class TestBackendWireBranches:
    def _backend(self) -> tuple[HindsightBackend, AsyncMock]:
        caller = AsyncMock()
        return HindsightBackend(capability_caller=caller), caller

    async def test_add_passes_document_id_and_update_mode(self) -> None:
        """document_id / update_mode 原样上 wire（upsert 锚点通路）。"""
        b, caller = self._backend()
        caller.return_value = {"success": True, "data": {"id": "mem-1"}}
        await b.add(
            user_id="u1",
            content="c",
            memory_type="semantic",
            tags=[],
            source="s",
            document_id="doc-77",
            update_mode="replace",
        )
        assert caller.await_args is not None
        params = caller.await_args.args[1]
        args = params["args"]
        assert args["document_id"] == "doc-77"
        assert args["update_mode"] == "replace"

    async def test_search_passes_memory_type(self) -> None:
        b, caller = self._backend()
        caller.return_value = {"success": True, "data": {"results": []}}
        await b.search(query="q", user_id="u1", top_k=5, memory_type="episodic")
        assert caller.await_args is not None
        args = caller.await_args.args[1]["args"]
        assert args["memory_type"] == "episodic"

    @pytest.mark.parametrize(
        "raw",
        [None, "plain-string", 42],
        ids=["none", "string", "int"],
    )
    async def test_search_non_dict_non_list_payload_yields_empty(
        self, raw: Any
    ) -> None:
        """recall 返回非 dict 非 list 载荷 → 空结果（不崩）。"""
        b, caller = self._backend()
        caller.return_value = raw
        results = await b.search(query="q", user_id="u1", top_k=5)
        assert results == []

    async def test_search_skips_non_dict_items(self) -> None:
        """结果列表中的非 dict 条目跳过，dict 条目正常映射。"""
        b, caller = self._backend()
        caller.return_value = {
            "success": True,
            "data": {
                "results": [
                    "junk-string",
                    {"id": "m1", "content": "ok", "score": 0.5, "metadata": {}},
                ]
            },
        }
        results = await b.search(query="q", user_id="u1", top_k=5)
        assert [r["id"] for r in results] == ["m1"]

    async def test_delete_without_dict_mapping_returns_true(self) -> None:
        """delete 响应解包后非 dict（无信封）→ 视为已删除。"""
        b, caller = self._backend()
        caller.return_value = None
        assert await b.delete(user_id="u1", memory_id="m1") is True
