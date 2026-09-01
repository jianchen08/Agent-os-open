# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""压缩 LLM 调用函数测试（从 memory/test_compress.py 迁移）。

compress 已从独立 sidecar (plugins/shared/system/memory/) 迁入 context_window_guard
进程内。本测试验证 _build_compress_llm_call_fn 的 capability_caller 路径，
与原 6 个用例语义对齐（进程内 LLMClient 首选路径已退役——零生产消费者，
LLM 面收敛由 llm_service 承接）：

1. capability_caller 抛异常时上抛 RuntimeError 并携带原因（不伪装空响应）
2. tool-executor 信封 success=false（服务未注册/执行失败）→ 上抛
3. 流中断（partial 非 None）→ 上抛（半截内容不可作压缩摘要）
4. 正常时返回 data.text（llm.complete_stream 聚合响应）
5. 消息列表原样透传（不再压平成字符串 prompt）
6. model_id 参数透传（空串兜底 llm_service 默认 chat）
7. 压缩服务级失败路径：调用异常沿链路上抛，由 compress_messages 顶层捕获
   （error 日志 + 返回 None，本轮压缩显式跳过）

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
# 1. 调用失败传播（错误要么处理要么传播，禁止伪装成空响应）
# ═══════════════════════════════════════════════════════════


class TestCompressCallerFailure:
    @pytest.mark.parametrize(
        ("src_exc", "case_id"),
        [
            (RuntimeError("boom-upstream"), "runtime"),
            (ValueError("bad-args"), "value"),
            (TimeoutError("llm-timed-out"), "timeout"),
        ],
        ids=["runtime", "value", "timeout"],
    )
    def test_compress_caller_exception_raises(self, mod: Any, src_exc: Exception, case_id: str) -> None:
        """capability_caller 抛异常 → RuntimeError 上抛并携带原因（不伪装空响应）。"""

        async def _raising_caller(method: str, params: dict, timeout: float | None = None) -> Any:
            raise src_exc

        fn = mod._build_compress_llm_call_fn(_raising_caller)
        with pytest.raises(RuntimeError) as exc_info:
            _await(fn("compress this"))
        assert "llm.complete_stream 调用失败" in str(exc_info.value), "须标明失败点"
        assert str(src_exc) in str(exc_info.value), "须携带根因"

    def test_envelope_success_false_raises(self, mod: Any) -> None:
        """tool-executor 信封 success=false（服务未注册/执行失败）→ 上抛，不伪装空响应。"""

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            return {"success": False, "error": "llm_service not registered"}

        fn = mod._build_compress_llm_call_fn(_caller)
        with pytest.raises(RuntimeError) as exc_info:
            _await(fn("compress this"))
        assert "工具执行失败" in str(exc_info.value)
        assert "llm_service not registered" in str(exc_info.value)

    def test_partial_interrupted_raises(self, mod: Any) -> None:
        """流中断（partial 非 None）→ 上抛（半截内容不可作压缩摘要）。"""

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            return {
                "success": True,
                "data": {"status": "interrupted", "partial": {"text": "half"}},
            }

        fn = mod._build_compress_llm_call_fn(_caller)
        with pytest.raises(RuntimeError) as exc_info:
            _await(fn("compress this"))
        assert "流中断" in str(exc_info.value)

    def test_service_compress_failure_returns_none_with_cause_logged(self, mod: Any, caplog: Any) -> None:
        """压缩服务级失败路径：调用异常沿链路上抛并携带根因，由
        _build_compression_content 显式捕获留痕、跳过该批；全部批次失败时
        compress_messages 返回 None（本轮压缩显式跳过）。"""
        import logging

        async def _failing_fn(_payload: Any) -> str:
            raise RuntimeError("llm.complete_stream down")

        svc = mod.CompressionService(llm_call_fn=_failing_fn, backend=None)
        # 总量远超 context_window*trigger_ratio，确保进入压缩轮
        messages = [
            {"role": "user", "content": "a" * 120},
            {"role": "assistant", "content": "b" * 120},
            {"role": "user", "content": "c" * 3600},
        ]
        with caplog.at_level(logging.WARNING):
            out = _await(svc.compress_messages(messages, context_window=100))
        assert out is None, "调用失败应显式跳过本轮压缩，而非以空摘要继续"
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("压缩失败" in m for m in msgs), "失败必须显式留痕"
        assert any("llm.complete_stream down" in m for m in msgs), "根因必须随链路抵达处置层，不得翻译成空响应"


# ═══════════════════════════════════════════════════════════
# 2. 正常路径：capability_caller 返回 text
# ═══════════════════════════════════════════════════════════


class TestCompressHappyPath:
    def test_compress_returns_summary(self, mod: Any) -> None:
        """capability_caller 正常时返回其 text 文本。"""
        mod.set_capability_caller(None)

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            assert method == "tool-executor.invoke"
            assert params["tool_name"] == "llm.complete_stream"
            assert params["plugin_id"] == "llm_service"
            assert params["args"]["messages"] == [{"role": "user", "content": "compress this"}]
            return {"success": True, "data": {"text": "  compressed text  ", "finish_reason": "stop"}}

        fn = mod._build_compress_llm_call_fn(_caller)
        result = _await(fn("compress this"))
        assert result == "  compressed text  "

    def test_message_list_passthrough(self, mod: Any) -> None:
        """消息列表原样透传给 llm.complete_stream（不再压平成字符串）。"""
        mod.set_capability_caller(None)

        captured: dict[str, Any] = {}

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            captured.update(params)
            return {"success": True, "data": {"text": "摘要", "finish_reason": "stop"}}

        fn = mod._build_compress_llm_call_fn(_caller)
        payload = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "用户内容"},
        ]
        result = _await(fn(payload))

        assert result == "摘要"
        assert captured["args"]["messages"] == payload

    def test_model_id_passthrough(self, mod: Any) -> None:
        """model_id 参数透传给 llm.complete_stream（空串兜底默认 chat）。"""
        mod.set_capability_caller(None)

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            assert params["args"]["model"] == "deepseek-v4"
            return {"success": True, "data": {"text": "ok", "finish_reason": "stop"}}

        fn = mod._build_compress_llm_call_fn(_caller, model_id="deepseek-v4")
        result = _await(fn("compress this"))
        assert result == "ok"


class TestTruncateToBudget:
    """_truncate_to_budget 的三条路径（JSON 结构保持 / 直接字符截断 / 兜底空对象）。"""

    def _compressor(self) -> Any:
        m = _load_plugin_module()
        return m.ContextCompressor.__new__(m.ContextCompressor)

    def test_short_text_passthrough(self) -> None:
        c = self._compressor()
        # 估算 token 小于预算 → 原样返回
        text = "短文本"
        assert c._truncate_to_budget(text, 10_000) == text

    def test_non_json_truncated_by_chars(self) -> None:
        c = self._compressor()
        text = "x" * 10_000  # 不合法 JSON → 字符截断 1.5x
        out = c._truncate_to_budget(text, 100)
        assert out == "x" * 150

    def test_json_truncated_at_last_comma(self) -> None:
        c = self._compressor()
        items = ",".join(f'"k{i}": "{ "y" * 20 }"' for i in range(50))
        blob = "{" + items + "}"
        out = c._truncate_to_budget(blob, 60)
        # 截断后的仍是可解析 JSON（安全逗号收尾 + } 补齐）
        import json

        parsed = json.loads(out)
        assert isinstance(parsed, dict)

    def test_json_no_safe_comma_falls_back_empty(self) -> None:
        c = self._compressor()
        # 单 key 超长 value：截断处无 ",\n" → 兜底 {}
        blob = '{"k": "' + "z" * 5_000 + '"}'
        out = c._truncate_to_budget(blob, 10)
        assert out == "{}"


class TestExtractJsonAndRender:
    """_extract_json 三分支（think 块剥离/代码块/裸对象）+ _render_fork_message 标签链。"""

    def _compressor(self) -> Any:
        m = _load_plugin_module()
        return m.ContextCompressor.__new__(m.ContextCompressor)

    def test_extract_json_strips_think_block(self) -> None:
        c = self._compressor()
        text = "<think>分析 {干扰} ```json ``` </think>```json\n{\"a\": 1}\n``` 后缀"
        out = c._extract_json(text)
        import json

        assert json.loads(out) == {"a": 1}

    def test_extract_json_bare_object(self) -> None:
        c = self._compressor()
        out = c._extract_json('前置说明 {"b": [1, 2]} 尾注')
        import json

        assert json.loads(out) == {"b": [1, 2]}

    def test_extract_json_no_json_returns_stripped(self) -> None:
        c = self._compressor()
        out = c._extract_json("完全不包含 JSON 的回答  ")
        assert out == "完全不包含 JSON 的回答"

    def test_render_fork_message_strips_internal_fields(self) -> None:
        m = _load_plugin_module()
        cc = m.ContextCompressor
        # 内部字段（seq/_context_form/tool_result）不进 LLM 载荷
        r = cc._render_fork_message(
            {"role": "user", "content": "hi", "seq": 3, "_context_form": "notice", "tool_result": {}}
        )
        assert "seq" not in r and "_context_form" not in r and "tool_result" not in r
        assert r["content"] == "[notice] hi"

    def test_render_fork_message_context_form_label(self) -> None:
        import sys as _sys

        m = _load_plugin_module()
        cc = m.ContextCompressor
        # _context_form 带合法标签（CONTEXT_FORM_LABELS 命中）
        form = next(iter(m.CONTEXT_FORM_LABELS), None) if hasattr(m, "CONTEXT_FORM_LABELS") else None
        if form:
            msg = {"role": "user", "content": "c", "_context_form": form}
            r = cc._render_fork_message(msg)
            assert str(m.CONTEXT_FORM_LABELS.get(form, "")) in r["content"]

    def test_render_fork_message_empty_content_no_label(self) -> None:
        m = _load_plugin_module()
        cc = m.ContextCompressor
        # content 为空时不加标签前缀（label 短路）
        r = cc._render_fork_message({"role": "user", "content": "", "_context_form": next(iter(m.CONTEXT_FORM_LABELS), "")})
        assert r["content"] == ""
