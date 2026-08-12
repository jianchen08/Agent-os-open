# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: python-plugins-test
"""memory.compress 工具 TDD 测试。

验证内容（与任务规格 6 个用例对齐）：
1. memory.compress 工具已注册到 plugin._tools
2. _llm 为 None 时降级返回 {summary: "", degraded: True}
3. _llm.chat_available 为 False 时降级
4. mock chat_completion 返回值后，工具返回 {summary: <text>, degraded: False}
5. chat_completion 抛异常时降级并带 error 信息
6. schema 校验：prompt 必填 string，max_tokens 可选 integer

测试不依赖真实 LLM——通过 monkeypatch 模块级 _llm 实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 源码加入 sys.path（与 hindsight test_server.py 同款 setup）
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，不依赖 importlib.reload
    （file-location spec 不可被 reload 重新查找，会抛 ModuleNotFoundError）。
    """
    mod_name = "memory_server_compress_test"
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
    """加载 server 模块，每个测试独立（重置 _llm/_store）。"""
    module = _load_module()
    module._llm = None
    module._store = None
    return module


# ═══════════════════════════════════════════════════════════
# 1. 工具注册
# ═══════════════════════════════════════════════════════════


class TestCompressRegistration:
    def test_compress_tool_registered(self, mod: Any) -> None:
        """memory.compress 工具必须注册到 plugin._tools。"""
        assert "memory.compress" in mod.plugin._tools, "memory.compress not registered"


# ═══════════════════════════════════════════════════════════
# 2 & 3. 降级（_llm 为 None / chat_available 为 False）
# ═══════════════════════════════════════════════════════════


class TestCompressDegrade:
    def test_compress_without_llm_degrades(self, mod: Any) -> None:
        """_llm 为 None 时降级返回 {summary: "", degraded: True}。"""
        mod._llm = None
        result = _call_tool(mod, "memory.compress", prompt="compress this")
        assert isinstance(result, dict)
        assert result["summary"] == ""
        assert result["degraded"] is True

    def test_compress_without_chat_available_degrades(self, mod: Any) -> None:
        """_llm.chat_available 为 False 时降级。"""
        llm = MagicMock()
        llm.chat_available = False
        mod._llm = llm
        result = _call_tool(mod, "memory.compress", prompt="compress this")
        assert isinstance(result, dict)
        assert result["summary"] == ""
        assert result["degraded"] is True
        # 不应调用 chat_completion
        llm.chat_completion.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 4. 正常路径：调用 chat_completion
# ═══════════════════════════════════════════════════════════


class TestCompressHappyPath:
    def test_compress_calls_chat_completion(self, mod: Any) -> None:
        """chat_available=True 时调用 chat_completion，返回 {summary: <text>, degraded: False}。"""
        llm = MagicMock()
        llm.chat_available = True
        llm.chat_completion.return_value = "compressed text"
        mod._llm = llm

        result = _call_tool(mod, "memory.compress", prompt="compress this")

        llm.chat_completion.assert_called_once()
        # 第一个位置参数应为完整 prompt
        call_args = llm.chat_completion.call_args
        assert call_args.args[0] == "compress this"
        # 返回形状
        assert result["summary"] == "compressed text"
        assert result["degraded"] is False


# ═══════════════════════════════════════════════════════════
# 5. 异常路径：chat_completion 抛错
# ═══════════════════════════════════════════════════════════


class TestCompressException:
    def test_compress_handles_llm_exception(self, mod: Any) -> None:
        """chat_completion 抛异常时降级返回 degraded=True 且 error 含异常信息。"""
        llm = MagicMock()
        llm.chat_available = True
        llm.chat_completion.side_effect = RuntimeError("boom-upstream")
        mod._llm = llm

        result = _call_tool(mod, "memory.compress", prompt="compress this")

        assert result["summary"] == ""
        assert result["degraded"] is True
        assert "boom-upstream" in str(result["error"])


# ═══════════════════════════════════════════════════════════
# 6. schema 校验
# ═══════════════════════════════════════════════════════════


class TestCompressSchema:
    def test_compress_schema_valid(self, mod: Any) -> None:
        """schema 含 prompt（必填 string）与 max_tokens（可选 integer）。"""
        td = mod.plugin._tools["memory.compress"]
        schema = td.schema
        assert schema["type"] == "object"
        props = schema["properties"]
        assert props["prompt"]["type"] == "string"
        assert props["max_tokens"]["type"] == "integer"
        assert "prompt" in schema["required"]
        # max_tokens 非必填
        assert "max_tokens" not in schema.get("required", [])
