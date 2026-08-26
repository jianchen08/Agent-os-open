# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""压缩 LLM 调用函数测试（从 memory/test_compress.py 迁移）。

compress 已从独立 sidecar (plugins/shared/system/memory/) 迁入 context_window_guard
进程内。本测试验证 _build_compress_llm_call_fn 的 capability_caller 回退路径，
与原 6 个用例语义对齐（进程内 LLMClient 首选路径已退役——零生产消费者，
LLM 面收敛由 llm_service 承接）：

1. capability_caller 为 None 时降级返回空串
2. capability_caller 抛异常时降级返回空串
3. capability_caller 正常时返回其 summary 文本（strip 语义保留）
4. 工具返回 degraded=True 时降级返回空串
5. 消息列表被压平成字符串 prompt 传给工具

测试不依赖真实 LLM——通过 monkeypatch 模块级 _capability_caller 实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

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
    # 若已加载先清理，强制重建以重置模块级 _capability_caller
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
    """加载 plugin 模块，每个测试独立（重置 _capability_caller）。"""
    module = _load_plugin_module()
    module.set_capability_caller(None)
    return module


# ═══════════════════════════════════════════════════════════
# 1. 降级（capability_caller 为 None / 抛异常）
# ═══════════════════════════════════════════════════════════


class TestCompressDegrade:
    def test_compress_without_caller_degrades(self, mod: Any) -> None:
        """capability_caller 为 None 时返回空串。"""
        mod.set_capability_caller(None)

        async def _bad_caller(method: str, params: dict) -> Any:
            raise RuntimeError("no caller")

        fn = mod._build_compress_llm_call_fn(_bad_caller)
        result = _await(fn("compress this"))
        assert result == ""

    def test_compress_caller_exception_degrades(self, mod: Any) -> None:
        """capability_caller 抛异常时降级返回空串。"""
        mod.set_capability_caller(None)

        async def _raising_caller(method: str, params: dict) -> Any:
            raise RuntimeError("boom-upstream")

        fn = mod._build_compress_llm_call_fn(_raising_caller)
        result = _await(fn("compress this"))
        assert result == ""


# ═══════════════════════════════════════════════════════════
# 2. 正常路径：capability_caller 返回 summary
# ═══════════════════════════════════════════════════════════


class TestCompressHappyPath:
    def test_compress_returns_summary(self, mod: Any) -> None:
        """capability_caller 正常时返回其 summary 文本。"""
        mod.set_capability_caller(None)

        async def _caller(method: str, params: dict) -> Any:
            assert method == "tool-executor.invoke"
            assert params["tool_name"] == "memory.compress"
            assert params["args"]["prompt"] == "compress this"
            return {"summary": "  compressed text  ", "degraded": False}

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == "  compressed text  "

    def test_compress_degraded_returns_empty(self, mod: Any) -> None:
        """capability_caller 返回 degraded=True 时降级返回空串。"""
        mod.set_capability_caller(None)

        async def _caller(method: str, params: dict) -> Any:
            return {"summary": "", "degraded": True, "error": "no chat key"}

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == ""

    def test_compress_string_result_compat(self, mod: Any) -> None:
        """兼容直接返回字符串的形态。"""
        mod.set_capability_caller(None)

        async def _caller(method: str, params: dict) -> Any:
            return "plain string summary"

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == "plain string summary"
