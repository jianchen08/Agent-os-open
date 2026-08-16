# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""压缩 LLM 调用函数测试（从 memory/test_compress.py 迁移）。

compress 已从独立 sidecar (plugins/shared/system/memory/) 迁入 context_window_guard
进程内。本测试验证 _build_compress_llm_call_fn 的 LLMClient 首选路径与
capability_caller 回退路径，与原 6 个用例语义对齐：

1. set_llm_client 注入点存在
2. _llm_client 为 None 时降级返回空串（走 capability_caller 回退，此处 caller 也为 None）
3. _llm_client.chat_available 为 False 时降级
4. chat_available=True 时调用 chat_completion 返回其文本
5. chat_completion 抛异常时降级返回空串
6. _llm_client 为 None 但 capability_caller 可用时走回退路径

测试不依赖真实 LLM——通过 monkeypatch 模块级 _llm_client / set_llm_client 实现。
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

# 插件目录加入 sys.path
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 源码加入 sys.path
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))

# pipeline 包（plugins/shared）加入 sys.path
_SHARED_DIR = Path(__file__).resolve().parents[3]
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "cwg_compress_test"
    # 若已加载先清理，强制重建以重置模块级 _llm_client
    sys.modules.pop(mod_name, None)
    plugin_path = _PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _await(coro: Any) -> Any:
    """同步等待协程结果（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def mod() -> Any:
    """加载 plugin 模块，每个测试独立（重置 _llm_client）。"""
    module = _load_plugin_module()
    module.set_llm_client(None)
    return module


# ═══════════════════════════════════════════════════════════
# 1. 注入点存在
# ═══════════════════════════════════════════════════════════


class TestCompressInjection:
    def test_set_llm_client_exists(self, mod: Any) -> None:
        """set_llm_client 注入函数必须存在。"""
        assert hasattr(mod, "set_llm_client"), "set_llm_client missing"
        # 调用不应抛错
        mod.set_llm_client(None)


class TestLLMClientRoleResolution:
    """验证 LLMClient 按 default_role 从 defaults 选模型。"""

    def test_compression_role_selects_minimax(self) -> None:
        """default_role='compression' 时读 defaults.compression → minimax-m3。"""
        from llm_client import LLMClient

        config = {
            "models": {
                "defaults": {"chat": "deepseek-v4-flash", "compression": "minimax-m3"},
                "models": {
                    "deepseek-v4-flash": {
                        "api_base": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-v4-flash",
                        "provider": "deepseek",
                    },
                    "minimax-m3": {
                        "api_base": "https://api.minimaxi.com/v1",
                        "model_name": "MiniMax-M3",
                        "provider": "minimax",
                    },
                },
                "providers": {
                    "deepseek": {"keys": [{"api_key": "dk-xxx"}]},
                    "minimax": {"keys": [{"api_key": "mm-xxx"}]},
                },
            }
        }
        client = LLMClient(config, default_role="compression")
        assert client.chat_model == "MiniMax-M3"
        assert client.chat_api_base == "https://api.minimaxi.com/v1"
        assert client.chat_api_key == "mm-xxx"
        assert client.chat_available is True

    def test_chat_role_selects_deepseek(self) -> None:
        """default_role='chat'(默认)时读 defaults.chat → deepseek-v4-flash。"""
        from llm_client import LLMClient

        config = {
            "models": {
                "defaults": {"chat": "deepseek-v4-flash", "compression": "minimax-m3"},
                "models": {
                    "deepseek-v4-flash": {
                        "api_base": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-v4-flash",
                        "provider": "deepseek",
                    },
                    "minimax-m3": {
                        "api_base": "https://api.minimaxi.com/v1",
                        "model_name": "MiniMax-M3",
                        "provider": "minimax",
                    },
                },
                "providers": {
                    "deepseek": {"keys": [{"api_key": "dk-xxx"}]},
                    "minimax": {"keys": [{"api_key": "mm-xxx"}]},
                },
            }
        }
        client = LLMClient(config)  # 默认 chat
        assert client.chat_model == "deepseek-v4-flash"
        assert client.chat_api_key == "dk-xxx"


# ═══════════════════════════════════════════════════════════
# 2 & 3. 降级（_llm_client 为 None / chat_available 为 False）
# ═══════════════════════════════════════════════════════════


class TestCompressDegrade:
    def test_compress_without_llm_client_degrades(self, mod: Any) -> None:
        """_llm_client 为 None 且无 capability_caller 回退时返回空串。

        _build_compress_llm_call_fn 需要 caller 参数（回退路径），这里给一个永远
        抛错的 caller 模拟回退也不可用；首选路径因 _llm_client=None 跳过。
        """
        mod.set_llm_client(None)

        async def _bad_caller(method: str, params: dict) -> Any:
            raise RuntimeError("no caller")

        fn = mod._build_compress_llm_call_fn(_bad_caller)
        result = _await(fn("compress this"))
        assert result == ""

    def test_compress_without_chat_available_degrades(self, mod: Any) -> None:
        """_llm_client.chat_available 为 False 时降级返回空串。"""
        llm = MagicMock()
        llm.chat_available = False
        mod.set_llm_client(llm)

        async def _noop_caller(method: str, params: dict) -> Any:
            return {}

        fn = mod._build_compress_llm_call_fn(_noop_caller)
        result = _await(fn("compress this"))
        assert result == ""
        # 不应调用 chat_completion
        llm.chat_completion.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 4. 正常路径：调用 chat_completion
# ═══════════════════════════════════════════════════════════


class TestCompressHappyPath:
    def test_compress_calls_chat_completion(self, mod: Any) -> None:
        """chat_available=True 时调用 chat_completion，返回其文本（strip 后）。"""
        llm = MagicMock()
        llm.chat_available = True
        llm.chat_completion.return_value = "  compressed text  "
        mod.set_llm_client(llm)

        async def _unused_caller(method: str, params: dict) -> Any:  # 不应被调用
            raise AssertionError("capability_caller 不应在 LLMClient 可用时被调用")

        fn = mod._build_compress_llm_call_fn(_unused_caller)
        result = _await(fn("compress this"))

        llm.chat_completion.assert_called_once()
        # 第一个位置参数应为完整 prompt，第二参数 max_tokens=8000
        call_args = llm.chat_completion.call_args
        assert call_args.args[0] == "compress this"
        assert call_args.args[1] == 8000
        # 返回 strip 后的文本
        assert result == "compressed text"


# ═══════════════════════════════════════════════════════════
# 5. 异常路径：chat_completion 抛错
# ═══════════════════════════════════════════════════════════


class TestCompressException:
    def test_compress_handles_llm_exception(self, mod: Any) -> None:
        """chat_completion 抛异常时降级返回空串（不抛给上层）。"""
        llm = MagicMock()
        llm.chat_available = True
        llm.chat_completion.side_effect = RuntimeError("boom-upstream")
        mod.set_llm_client(llm)

        async def _noop_caller(method: str, params: dict) -> Any:
            return {}

        fn = mod._build_compress_llm_call_fn(_noop_caller)
        result = _await(fn("compress this"))
        assert result == ""


# ═══════════════════════════════════════════════════════════
# 6. 回退路径：_llm_client 为 None 时走 capability_caller
# ═══════════════════════════════════════════════════════════


class TestCompressFallback:
    def test_compress_falls_back_to_capability_caller(self, mod: Any) -> None:
        """_llm_client 为 None 时经 capability_caller 调 memory.compress 工具。"""
        mod.set_llm_client(None)

        async def _caller(method: str, params: dict) -> Any:
            assert method == "tool-executor.invoke"
            assert params["tool_name"] == "memory.compress"
            assert params["args"]["prompt"] == "compress this"
            return {"summary": "fallback summary", "degraded": False}

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == "fallback summary"

    def test_compress_fallback_degraded(self, mod: Any) -> None:
        """回退路径工具降级时返回空串。"""
        mod.set_llm_client(None)

        async def _caller(method: str, params: dict) -> Any:
            return {"summary": "", "degraded": True, "error": "no chat key"}

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == ""
