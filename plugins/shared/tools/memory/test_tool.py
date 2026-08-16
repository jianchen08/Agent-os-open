# @feature: FP-0.2.六 记忆检索/注入补全 | @vision: V1 可进化 | @ci: none-local
"""MemoryTool 0.2 重写版 TDD 测试——IMemoryBackend 注入替代已删除的 0.1 MemoryService。

验证内容（与任务规格 10 个用例对齐）：
1. test_execute_store_calls_backend_add —— action=store 调用 backend.add（content/tags/source）
2. test_execute_retrieve_calls_backend_search —— action=retrieve 调用 backend.search 并返回结果
3. test_execute_import_text —— action=import_text 调用 backend.import_document(text=...)
4. test_execute_import_file —— action=import_file 调用 backend.import_document(file_path=...)
5. test_execute_delete —— action=delete 调用 backend.delete
6. test_execute_without_backend_error —— 未注入 backend 时返回错误，不崩溃
7. test_execute_unknown_action —— 未知 action 返回错误
8. test_execute_update_degrades —— action=update 降级（backend.add 或错误），不崩溃
9. test_tool_definition_schema —— get_tool_definition() 仍返回合法 Tool，action 枚举齐全
10. test_set_memory_backend —— setter 注入生效

Mock 后端（AsyncMock），不依赖真实记忆能力；模块经 importlib 直接加载，
不依赖 0.1 已删除的 core/tools/memory 包（与 test_memory_backend.py 同款 setup）。

[来源: docs/tasks Step 5d MemoryTool 重写为 IMemoryBackend]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 test_memory_backend.py 同款 setup）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 tool.py 模块。

    用 module_from_spec + exec_module 直接加载，避免依赖 0.1 包安装。
    """
    mod_name = "memory_tool_test"
    module_path = _PLUGIN_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load tool.py"
    assert spec.loader is not None, "Cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """加载 tool 模块。"""
    return _load_module()


@pytest.fixture
def backend() -> AsyncMock:
    """注入一个 AsyncMock 作为 IMemoryBackend（duck-type）。"""
    b = AsyncMock()
    b.add.return_value = "mem-1"
    b.search.return_value = [
        {
            "id": "m1",
            "content": "alpha",
            "score": 0.9,
            "memory_type": "semantic",
            "metadata": {"tags": ["t1"]},
        }
    ]
    b.delete.return_value = True
    b.import_document.return_value = {"chunks_imported": 3, "name": "doc"}
    return b


@pytest.fixture
def tool_inst(mod: Any, backend: AsyncMock) -> Any:
    """构造注入了 mock backend 的 MemoryTool 实例。

    敏感 action（store/import/update/delete）自 B6 起需要可信身份，
    这里以服务端同款 set_trusted_user_id 注入。
    """
    t = mod.MemoryTool(memory_backend=backend)
    t.set_trusted_user_id("tester")
    return t


# ═══════════════════════════════════════════════════════════
# 1. store → backend.add
# ═══════════════════════════════════════════════════════════


class TestExecuteStore:
    async def test_execute_store_calls_backend_add(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=store 调用 backend.add，content/tags/source 正确传递。"""
        result = await tool_inst.execute(
            {
                "action": "store",
                "content": "hello",
                "tags": ["t1"],
                "memory_type": "semantic",
            }
        )

        backend.add.assert_awaited_once()
        kwargs = backend.add.await_args.kwargs
        assert kwargs["content"] == "hello"
        assert kwargs["memory_type"] == "semantic"
        assert "t1" in kwargs["tags"]
        assert kwargs["source"] == "memory_tool"

        assert result.success is True
        assert result.output["memory_id"] == "mem-1"


# ═══════════════════════════════════════════════════════════
# 2. retrieve → backend.search
# ═══════════════════════════════════════════════════════════


class TestExecuteRetrieve:
    async def test_execute_retrieve_calls_backend_search(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=retrieve 调用 backend.search，结果原样返回。"""
        result = await tool_inst.execute(
            {"action": "retrieve", "query": "q", "top_k": 3}
        )

        backend.search.assert_awaited_once()
        kwargs = backend.search.await_args.kwargs
        assert kwargs["query"] == "q"
        assert kwargs["top_k"] == 3

        assert result.success is True
        assert result.output["results"][0]["content"] == "alpha"


# ═══════════════════════════════════════════════════════════
# 3/4. import_text / import_file → backend.import_document
# ═══════════════════════════════════════════════════════════


class TestExecuteImport:
    async def test_execute_import_text(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=import_text 调用 backend.import_document(text=...)。"""
        result = await tool_inst.execute(
            {"action": "import_text", "content": "doc text", "name": "my_doc"}
        )

        backend.import_document.assert_awaited_once()
        kwargs = backend.import_document.await_args.kwargs
        assert kwargs["text"] == "doc text"
        assert kwargs["name"] == "my_doc"
        assert "file_path" not in kwargs

        assert result.success is True
        assert result.output["chunks_imported"] == 3

    async def test_execute_import_file(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=import_file 调用 backend.import_document(file_path=...)。"""
        result = await tool_inst.execute(
            {"action": "import_file", "file_path": "/tmp/a.md", "name": "a"}
        )

        backend.import_document.assert_awaited_once()
        kwargs = backend.import_document.await_args.kwargs
        assert kwargs["file_path"] == "/tmp/a.md"
        assert kwargs["name"] == "a"
        assert "text" not in kwargs

        assert result.success is True
        assert result.output["chunks_imported"] == 3


# ═══════════════════════════════════════════════════════════
# 5. delete → backend.delete
# ═══════════════════════════════════════════════════════════


class TestExecuteDelete:
    async def test_execute_delete(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=delete 调用 backend.delete(user_id, memory_id)。"""
        result = await tool_inst.execute(
            {"action": "delete", "memory_id": "mem-9"}
        )

        backend.delete.assert_awaited_once()
        kwargs = backend.delete.await_args.kwargs
        assert kwargs["memory_id"] == "mem-9"

        assert result.success is True
        assert result.output["deleted"] is True


# ═══════════════════════════════════════════════════════════
# 6/7. 无 backend / 未知 action → 错误，不崩溃
# ═══════════════════════════════════════════════════════════


class TestExecuteErrors:
    async def test_execute_without_backend_error(self, mod: Any) -> None:
        """未注入 backend 时返回错误 dict，不崩溃。"""
        t = mod.MemoryTool()  # 不注入 backend

        result = await t.execute({"action": "store", "content": "x"})

        assert result.success is False
        assert result.error is not None
        assert "memory backend 未注入" in result.error

    async def test_execute_unknown_action(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """未知 action 返回错误，不崩溃。"""
        result = await tool_inst.execute({"action": "unknown"})

        assert result.success is False
        assert result.error is not None
        assert "未知操作" in result.error


# ═══════════════════════════════════════════════════════════
# 8. update → 降级，不崩溃
# ═══════════════════════════════════════════════════════════


class TestExecuteUpdate:
    async def test_execute_update_degrades(
        self, mod: Any, backend: AsyncMock, tool_inst: Any
    ) -> None:
        """action=update：后端无 update 能力时降级为 backend.add，不崩溃。"""
        result = await tool_inst.execute(
            {"action": "update", "content": "new content", "tags": ["t2"]}
        )

        # 不崩溃；降级路径必须真正调用过 add（或原生 update）
        assert result is not None
        assert backend.add.await_count >= 1
        assert result.success is True
        assert result.output["degraded"] is True


# ═══════════════════════════════════════════════════════════
# 9. 工具定义 schema
# ═══════════════════════════════════════════════════════════


class TestToolDefinition:
    def test_tool_definition_schema(self, mod: Any) -> None:
        """get_tool_definition() 仍返回合法 Tool，action 枚举齐全。"""
        tool_def = mod.MemoryTool.get_tool_definition()

        assert tool_def.name == "memory"
        assert "description" in tool_def.__dict__
        assert tool_def.description

        properties = tool_def.input_schema["properties"]
        assert "action" in properties
        enum = properties["action"]["enum"]
        for action in (
            "store",
            "retrieve",
            "import_text",
            "import_file",
            "update",
            "delete",
            "get_context",
        ):
            assert action in enum, f"action enum missing {action}"
        assert "action" in tool_def.input_schema["required"]


# ═══════════════════════════════════════════════════════════
# 10. set_memory_backend 注入
# ═══════════════════════════════════════════════════════════


class TestSetMemoryBackend:
    async def test_set_memory_backend(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """setter 注入后立即生效。"""
        t = mod.MemoryTool()
        assert t._memory_backend is None

        t.set_memory_backend(backend)
        assert t._memory_backend is backend
        t.set_trusted_user_id("tester")

        result = await t.execute({"action": "store", "content": "x"})
        backend.add.assert_awaited_once()
        assert result.success is True


# ═══════════════════════════════════════════════════════════
# 11+. IDOR 防护（B6）：敏感 action 需可信身份；注入后按身份隔离
# ═══════════════════════════════════════════════════════════


class TestIdorProtection:
    async def test_store_without_trusted_identity_rejected(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """无服务端可信身份注入时 write（store）被明确拒绝，后端未被触碰。"""
        t = mod.MemoryTool(memory_backend=backend)  # 不注入 set_trusted_user_id

        result = await t.execute(
            {"action": "store", "content": "x", "user_id": "attacker"}
        )

        assert result.success is False
        assert result.error is not None
        assert "可信调用方身份" in result.error
        assert "IDOR" in result.error
        backend.add.assert_not_awaited()

    async def test_delete_without_trusted_identity_rejected(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """delete 同为敏感 action，无身份注入被拒。"""
        t = mod.MemoryTool(memory_backend=backend)

        result = await t.execute({"action": "delete", "memory_id": "m1"})

        assert result.success is False
        assert "可信调用方身份" in (result.error or "")
        backend.delete.assert_not_awaited()

    async def test_read_action_without_identity_still_works(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """只读 action（retrieve）保留向后兼容：无身份注入仍可执行。"""
        t = mod.MemoryTool(memory_backend=backend)

        result = await t.execute({"action": "retrieve", "query": "q"})

        assert result.success is True
        backend.search.assert_awaited_once()

    async def test_trusted_identity_overrides_client_user_id(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """注入可信身份后隔离 key 恒用可信值，忽略客户端 inputs.user_id。"""
        t = mod.MemoryTool(memory_backend=backend)
        t.set_trusted_user_id("alice")

        await t.execute(
            {"action": "store", "content": "x", "user_id": "bob"}
        )

        kwargs = backend.add.await_args.kwargs
        assert kwargs["user_id"] == "alice"

    async def test_user_id_isolation_between_callers(
        self, mod: Any, backend: AsyncMock
    ) -> None:
        """不同可信身份各自隔离：alice/bob 的写入分别落在各自 user_id 下。"""
        alice = mod.MemoryTool(memory_backend=backend)
        alice.set_trusted_user_id("alice")
        bob = mod.MemoryTool(memory_backend=backend)
        bob.set_trusted_user_id("bob")

        await alice.execute({"action": "store", "content": "a"})
        await bob.execute({"action": "store", "content": "b"})

        used = [c.kwargs["user_id"] for c in backend.add.await_args_list]
        assert used == ["alice", "bob"]


# ═══════════════════════════════════════════════════════════
# 12. server.py 入口接线（B6）：memory() 从注入参数取可信身份
# ═══════════════════════════════════════════════════════════


class TestServerIdorWiring:
    """server.memory() 按 bash _owner_from_inputs 模式注入可信身份。"""

    @pytest.fixture
    def server_mod(self, monkeypatch, backend: AsyncMock) -> Any:
        """加载 server.py（唯一模块名），并替换后端工厂为 mock。"""
        import importlib.util as _ilu

        mod_name = "memory_server_test"
        module_path = _PLUGIN_DIR / "server.py"
        spec = _ilu.spec_from_file_location(mod_name, module_path)
        assert spec is not None and spec.loader is not None
        module = _ilu.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_get_memory_backend", lambda: backend)
        return module

    async def test_owner_extracted_and_injected(self, server_mod: Any, backend: AsyncMock) -> None:
        """_owner 注入 → set_trusted_user_id 生效，且忽略客户端 user_id。"""
        out = await server_mod.memory(
            _owner="sess-1", action="store", content="x", user_id="attacker"
        )
        assert out == {"success": True, "memory_id": "mem-1"}
        kwargs = backend.add.await_args.kwargs
        assert kwargs["user_id"] == "sess-1"

    async def test_session_id_fallback(self, server_mod: Any, backend: AsyncMock) -> None:
        """无 _owner 时回落 session_id（param_inject 常规注入路径）。"""
        await server_mod.memory(session_id="sess-2", action="store", content="x")
        kwargs = backend.add.await_args.kwargs
        assert kwargs["user_id"] == "sess-2"

    async def test_no_identity_sensitive_action_rejected(
        self, server_mod: Any, backend: AsyncMock
    ) -> None:
        """无任何注入时 write（store）被明确拒绝（不静默用 inputs.user_id）。"""
        out = await server_mod.memory(action="store", content="x", user_id="attacker")
        assert "error" in out
        assert "可信调用方身份" in out["error"]
        backend.add.assert_not_awaited()

    async def test_no_identity_read_action_compat(
        self, server_mod: Any, backend: AsyncMock
    ) -> None:
        """无注入时只读 action 保留兼容（user_id 回退仅读路径可达）。"""
        out = await server_mod.memory(action="retrieve", query="q", user_id="legacy")
        assert out["success"] is True
        kwargs = backend.search.await_args.kwargs
        assert kwargs["user_id"] == "legacy"
