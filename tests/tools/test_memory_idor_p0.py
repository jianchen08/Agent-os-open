"""P0-1 memory IDOR 修复测试（TDD）。

回归安全缺口：``MemoryTool._resolve_user_id`` 直接信任客户端 ``inputs["user_id"]``，
无服务端校验——任一调用者传他人 id 即可读写他人记忆（store/retrieve/import/
delete 全以 user_id 为 key 隔离）。

契约：当服务端注入了可信 caller 身份（``_trusted_user_id``）后，
- 篡改 ``inputs["user_id"]`` 无法改变实际隔离 key；
- store / retrieve / import_text / delete 全部以可信身份访问后端。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# ── 路径注入：memory 工具自包含（Tool/ToolExecutionResult 就地定义）──
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MEM_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "memory"
_s = str(_MEM_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)
# 裸模块 tool 可能被其它插件缓存污染，逐出后按本目录解析
sys.modules.pop("tool", None)

import tool as memory_tool  # noqa: E402

MemoryTool = memory_tool.MemoryTool


def _make_backend() -> AsyncMock:
    """构造 duck-type IMemoryBackend mock（add/search/delete/import_document 全 async）。"""
    backend = AsyncMock()
    backend.add.return_value = "mem-1"
    backend.search.return_value = []
    backend.delete.return_value = True
    backend.import_document.return_value = {"document_id": "doc-1"}
    return backend


# ═══════════════════════════════════════════════════════════
# RED：注入可信身份前，篡改 inputs.user_id 即可越权（证明漏洞存在 / 锁定契约）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_store_uses_trusted_user_id_ignoring_tampered_input() -> None:
    """store：注入 trusted=alice 后，inputs.user_id=attacker 必须被忽略。

    RED：当前 _resolve_user_id 直接返回 inputs.user_id（"attacker"），
         backend.add 收到 user_id="attacker" → 越权写入他人空间。
    GREEN：_resolve_user_id 优先 _trusted_user_id → backend.add 收到 "alice"。
    """
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)
    mt.set_trusted_user_id("alice")

    await mt.execute(
        {
            "action": "store",
            "content": "secret of alice",
            "tags": [],
            # 客户端篡改：冒充 attacker
            "user_id": "attacker",
        }
    )

    backend.add.assert_awaited_once()
    assert backend.add.call_args.kwargs["user_id"] == "alice", (
        "IDOR 未修复：篡改 inputs.user_id 渗透进了后端隔离 key"
    )


@pytest.mark.asyncio
async def test_retrieve_uses_trusted_user_id_ignoring_tampered_input() -> None:
    """retrieve：注入 trusted=alice 后，inputs.user_id=attacker 必须被忽略。"""
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)
    mt.set_trusted_user_id("alice")

    await mt.execute(
        {
            "action": "retrieve",
            "query": "anything",
            "user_id": "attacker",  # 篡改
        }
    )

    backend.search.assert_awaited_once()
    assert backend.search.call_args.kwargs["user_id"] == "alice"


@pytest.mark.asyncio
async def test_delete_uses_trusted_user_id_ignoring_tampered_input() -> None:
    """delete：注入 trusted=alice 后，篡改 user_id 无法删他人记忆。"""
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)
    mt.set_trusted_user_id("alice")

    await mt.execute(
        {
            "action": "delete",
            "memory_id": "mem-1",
            "user_id": "attacker",  # 篡改
        }
    )

    backend.delete.assert_awaited_once()
    assert backend.delete.call_args.kwargs["user_id"] == "alice"


@pytest.mark.asyncio
async def test_import_text_uses_trusted_user_id() -> None:
    """import_text：注入 trusted 身份后，篡改 user_id 无效。"""
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)
    mt.set_trusted_user_id("alice")

    await mt.execute(
        {
            "action": "import_text",
            "content": "knowledge",
            "name": "k1",
            "user_id": "attacker",  # 篡改
        }
    )

    backend.import_document.assert_awaited_once()
    assert backend.import_document.call_args.kwargs["user_id"] == "alice"


# ═══════════════════════════════════════════════════════════
# 兼容契约：未注入可信身份时，回退 inputs.user_id（不破坏既有合法行为）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
def test_fallback_to_inputs_user_id_when_no_trusted_injection() -> None:
    """未注入可信身份时，_resolve_user_id 沿用 inputs.user_id（只读路径回退）。"""
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)
    # 不调用 set_trusted_user_id

    assert mt._resolve_user_id({"user_id": "bob"}) == "bob"


def test_fallback_to_system_when_no_user_id_anywhere() -> None:
    """既无可信注入也无 inputs.user_id → 回退 SYSTEM_USER_ID。"""
    backend = _make_backend()
    mt = MemoryTool(memory_backend=backend)

    assert mt._resolve_user_id({}) == MemoryTool.SYSTEM_USER_ID
