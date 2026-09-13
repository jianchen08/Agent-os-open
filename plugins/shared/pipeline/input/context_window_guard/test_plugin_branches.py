# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""context_window_guard 插件分支行为测试。

覆盖压缩器/压缩服务/插件主流程的分支契约（现状行为锚定）：
- 压缩输入净化：空响应 / think-only 响应 / 空 l1 均 fail-closed 跳过压缩；
- 预算截断：JSON 结构保持截断（安全逗号）与兜底空对象；
- fork 队列：字符串 system 前缀、无 system 时不带前缀；
- 服务层：llm_call_fn 延迟注入与逐调用覆盖、顶层异常返回 None、多轮压缩；
- 插件层：无窗口/空消息/无服务早退、on_chunk 进度通知、压缩失败前端
  事件（推送一次 + 失败不反噬）、块槽位不重复置 null、clean+降级合并 ops；
- 守卫辅助：trim/clean 的 backend 异常与边界、seq 标签解析、窗口值解析。

LLM / capability caller / memory backend 是外部依赖，以记录型伪对象注入；
不 mock 插件内部协作对象的行为（压缩主链路走真实 CompressionService）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + plugins/shared 加入 sys.path（与既有测试文件同一装配方式）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，隔离模块级注入状态）。"""
    mod_name = "cwg_branches_test"
    sys.modules.pop(mod_name, None)
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _valid_compress_json() -> str:
    """合法的 5 部分 JSON 压缩响应（l1 非空，供压缩主链路使用）。"""
    return """{
  "l1": {
    "session_title": "测试会话",
    "workflow": "完成了 X 任务",
    "errors_and_corrections": null,
    "decisions": null,
    "key_results": null
  },
  "l2": {
    "intent": "做 X",
    "process": "步骤 A 然后 B",
    "results": "产出 X | 待办 Y"
  },
  "keywords": ["关键词1"],
  "state_snapshot": {
    "current_state": "进行中",
    "task_specification": "测试任务",
    "pending": "收尾",
    "key_entities": "entity_a",
    "domain_knowledge": "约束 Z",
    "user_feedback": "",
    "attention_hints": "注意 Q"
  },
  "memory_items": {
    "user_profile_updates": null,
    "project_knowledge_updates": null,
    "experience_updates": null
  }
}"""


class _RecordingLlm:
    """伪压缩 LLM：记录每次调用的入参并返回预设响应（外部依赖桩）。"""

    def __init__(self, response: str) -> None:
        self.calls: list[Any] = []
        self._response = response

    async def __call__(self, payload: Any) -> str:
        self.calls.append(payload)
        return self._response


class _FakeBackend:
    """记录 add 调用、返回预设 search 结果的伪记忆后端（duck-typed）。"""

    def __init__(self, search_returns: list[Any] | None = None) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.search_returns: list[Any] = search_returns or []

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        return f"mem-{len(self.add_calls)}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.search_returns)


class _BoomBackend:
    """search 必抛异常的伪记忆后端（故障注入）。"""

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("backend down")


def _l1_chunk(tags: list[str], content: str = "L" * 40) -> dict[str, Any]:
    """构造 backend 检索结果里的 L1 块条目（sequence 信息在 metadata.tags）。"""
    return {
        "id": "c-l1",
        "content": content,
        "memory_type": "chunk",
        "metadata": {"tags": ["L1", *tags]},
    }


def _ops_by_seq(state_updates: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """从 state_updates 提取 messages._ops，按 seq 索引（非 _ops 形态返回空）。"""
    messages = state_updates.get("messages")
    if not isinstance(messages, dict) or "_ops" not in messages:
        return {}
    return {
        op["seq"]: op for op in messages["_ops"] if isinstance(op, dict) and "seq" in op
    }


# ═══════════════════════════════════════════════════════════
# 模块级注入 setter
# ═══════════════════════════════════════════════════════════


class TestFrontendEmitSetter:
    def test_set_and_clear(self) -> None:
        """set_frontend_emit 注入后模块句柄指向该函数，传 None 清空。"""
        mod = _load_plugin_module()

        async def _emit(event: str, payload: dict[str, Any], thread_id: str) -> None:
            return None

        mod.set_frontend_emit(_emit)
        assert mod._frontend_emit is _emit
        mod.set_frontend_emit(None)
        assert mod._frontend_emit is None


# ═══════════════════════════════════════════════════════════
# CompressionConfig.from_yaml_config 的 ConfigCenter 兼容路径
# ═══════════════════════════════════════════════════════════


class TestFromYamlConfigCompatPath:
    @staticmethod
    def _install_config_center(
        monkeypatch: pytest.MonkeyPatch, section: dict[str, Any]
    ) -> None:
        """注入伪 config.config_center 模块（外部配置存储桩）。"""
        fake_mod = types.ModuleType("config.config_center")
        fake_mod.get_config_center = lambda: types.SimpleNamespace(get=lambda _p: section)
        monkeypatch.setitem(sys.modules, "config", types.ModuleType("config"))
        monkeypatch.setitem(sys.modules, "config.config_center", fake_mod)

    def test_compat_path_reads_ratios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """injected 为空时经 ConfigCenter 兼容路径读取比例（非代码默认值）。"""
        mod = _load_plugin_module()
        self._install_config_center(
            monkeypatch,
            {
                "compress_trigger_ratio": 0.7,
                "budgets": {"l1": 0.2, "l2": 0.1, "recent": 0.3},
            },
        )
        cfg = mod.CompressionConfig.from_yaml_config(100_000, injected=None)
        assert cfg.compress_trigger_ratio == 0.7
        budgets = cfg.get_budgets()
        # 性质：每层预算 = int(context_window * 对应比例)
        assert budgets["recent"] == int(100_000 * 0.3)
        assert budgets["L1"] == int(100_000 * 0.2)
        assert budgets["L2"] == int(100_000 * 0.1)

    def test_compat_path_empty_section_uses_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigCenter 返回空节 → 代码默认比例（0.55/0.1/0.05/0.18）。"""
        mod = _load_plugin_module()
        self._install_config_center(monkeypatch, {})
        cfg = mod.CompressionConfig.from_yaml_config(128_000, injected=None)
        assert cfg.compress_trigger_ratio == 0.55
        assert cfg.get_trigger_threshold() == int(128_000 * 0.55)
        assert cfg.get_budgets()["recent"] == int(128_000 * 0.18)


# ═══════════════════════════════════════════════════════════
# compress_all 的 fail-closed 输入净化
# ═══════════════════════════════════════════════════════════


class TestCompressAllEmptyResponse:
    @pytest.mark.parametrize("response", ["", "   \n  "], ids=["empty", "whitespace"])
    def test_blank_response_returns_none(self, response: str) -> None:
        """LLM 空白响应 → 压缩跳过（None），不产出半截摘要。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=_RecordingLlm(response))
        result = _run(compressor.compress_all([{"role": "user", "content": "做 X"}]))
        assert result is None

    @pytest.mark.parametrize(
        "response",
        ["<think>推理过程 { 干扰 }</think>", "<think></think>   "],
        ids=["think-with-noise", "think-empty"],
    )
    def test_think_only_response_returns_none(self, response: str) -> None:
        """剥掉 <think> 块后无有效内容 → JSON 提取为空，压缩跳过（None）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=_RecordingLlm(response))
        result = _run(compressor.compress_all([{"role": "user", "content": "做 X"}]))
        assert result is None


# ═══════════════════════════════════════════════════════════
# _truncate_to_budget 的 JSON 截断路径
# ═══════════════════════════════════════════════════════════


class TestTruncateToBudgetJsonPaths:
    def test_flat_json_truncated_at_safe_comma(self) -> None:
        """扁平 JSON 超预算 → 在最后一个完整键值对的逗号处截断并补 }，
        结果仍是合法 JSON 对象且保留前缀键值。"""
        import json

        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        blob = json.dumps({f"k{i}": "y" * 40 for i in range(30)}, indent=2)
        out = compressor._truncate_to_budget(blob, 50)
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert 0 < len(parsed) < 30, "截断应保留部分键值而非全部/清空"
        assert out.endswith("}")

    def test_nested_json_unsafe_comma_falls_back_empty(self) -> None:
        """最后一个逗号位于嵌套对象内（补一个 } 无法闭合）→ 兜底空对象。

        截断点落在 outer 的键值串列中：最后一个 ",\\n" 是内部键值对的分隔逗号，
        只补一个 } 少一层闭合括号，safe 非法 JSON → except 落到空对象。
        """
        import json

        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        blob = json.dumps(
            {"outer": {f"k{i}": "z" * 60 for i in range(10)}, "tail": 1}, indent=2
        )
        out = compressor._truncate_to_budget(blob, 100)
        assert json.loads(out) == {}


# ═══════════════════════════════════════════════════════════
# _extract_json / _format_messages / fork 队列 / _estimate_tokens
# ═══════════════════════════════════════════════════════════


class TestCompressorHelpers:
    def test_extract_json_empty_input(self) -> None:
        """空文本原样返回（调用方上游已拦截空响应，此处为纯函数边界）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        assert compressor._extract_json("") == ""

    @pytest.mark.parametrize("content", ["", None], ids=["empty-str", "none"])
    def test_format_messages_skips_blank_content(self, content: Any) -> None:
        """content 为空的消息不进格式化输出，其余消息保留。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        text = compressor._format_messages(
            [
                {"role": "user", "content": content},
                {"role": "user", "content": "有效内容"},
            ]
        )
        assert "【用户 2】" in text
        assert "【用户 1】" not in text

    @pytest.mark.parametrize(
        ("role", "header"),
        [("developer", "【DEVELOPER"), ("function", "【FUNCTION")],
    )
    def test_format_messages_unknown_role(self, role: str, header: str) -> None:
        """未知角色按大写角色头输出（不丢消息）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        text = compressor._format_messages([{"role": role, "content": "x"}])
        assert text.startswith(header)

    def test_fork_messages_string_system_prefix(self) -> None:
        """字符串 system 前缀 → fork 首条为 {"role": "system"}，末条为压缩指令。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=_RecordingLlm(_valid_compress_json()))
        _run(
            compressor.compress_all(
                [{"role": "user", "content": "做 X"}],
                system_message="执行时系统提示",
            )
        )
        fork = compressor._llm_call_fn.calls[0]
        assert fork[0] == {"role": "system", "content": "执行时系统提示"}
        assert fork[-1]["role"] == "user"
        assert fork[-1]["content"] == mod.ContextCompressor.COMPACTION_INSTRUCTION

    def test_fork_messages_without_system(self) -> None:
        """system_message=None → fork 不带 system 前缀，直接以待压消息开头。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=_RecordingLlm(_valid_compress_json()))
        _run(compressor.compress_all([{"role": "user", "content": "做 X"}]))
        fork = compressor._llm_call_fn.calls[0]
        assert fork[0]["role"] == "user"
        assert fork[0]["content"] == "做 X"
        assert all(m["role"] != "system" for m in fork)

    def test_compress_all_without_llm_raises(self) -> None:
        """未注入 LLM 调用函数 → compress_all 上抛 RuntimeError 并指明原因。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        with pytest.raises(RuntimeError, match="压缩失败"):
            _run(compressor.compress_all([{"role": "user", "content": "做 X"}]))
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            _run(compressor._call_llm([{"role": "user", "content": "x"}]))

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ([{"role": "user", "content": "abcd"}, {"content": "ab"}], 3),
            (["raw", {"content": "abcd"}], 3),
        ],
        ids=["dict-msgs", "mixed-non-dict"],
    )
    def test_estimate_tokens_list_payload(
        self, payload: list[Any], expected: int
    ) -> None:
        """列表载荷按条目 content 逐条估算（非 dict 条目按其字符串形态）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        assert compressor._estimate_tokens(payload) == expected

    @pytest.mark.parametrize("text", ["", None], ids=["empty", "none"])
    def test_estimate_tokens_blank_is_zero(self, text: Any) -> None:
        """空文本估算为 0（不产生幻影 token）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        assert compressor._estimate_tokens(text) == 0


# ═══════════════════════════════════════════════════════════
# CompressionService：llm_call_fn 注入时机 / 顶层异常 / 多轮
# ═══════════════════════════════════════════════════════════


def _round_like_messages() -> list[dict[str, Any]]:
    """4 条各约 200 token 的消息（seq 1-4，total 800t > recent 144t）。"""
    roles = ["user", "assistant", "user", "assistant"]
    return [
        {"role": roles[i], "content": "A" * 400, "seq": i + 1} for i in range(4)
    ]


class TestServiceLlmInjection:
    def test_set_llm_call_fn_after_construct(self) -> None:
        """构造时未给 LLM 函数、事后 set_llm_call_fn → 压缩照常执行。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(backend=None)
        svc.set_llm_call_fn(_RecordingLlm(_valid_compress_json()))
        result = _run(svc.compress_messages(_round_like_messages(), context_window=800))
        assert result is not None
        blocks = [
            m
            for m in result
            if isinstance(m.get("metadata"), dict)
            and "compression_ref" in m["metadata"]
        ]
        assert blocks, "压缩成功必须产出块消息"

    def test_compress_messages_llm_fn_overrides(self) -> None:
        """compress_messages 的 llm_call_fn 参数覆盖构造时的坏函数。"""
        mod = _load_plugin_module()

        async def _broken(_payload: Any) -> str:
            raise RuntimeError("broken channel")

        good = _RecordingLlm(_valid_compress_json())
        svc = mod.CompressionService(backend=None, llm_call_fn=_broken)
        result = _run(
            svc.compress_messages(
                _round_like_messages(), context_window=800, llm_call_fn=good
            )
        )
        assert result is not None, "覆盖后的可用函数应驱动压缩成功"
        assert len(good.calls) >= 1, "压缩应调用覆盖后的 LLM 通道"


class TestServiceTopLevelException:
    @pytest.mark.parametrize("bad_tool_calls", [5, None], ids=["int", "none"])
    def test_malformed_message_returns_none(
        self, caplog: pytest.LogCaptureFixture, bad_tool_calls: Any
    ) -> None:
        """畸形消息（tool_calls 不可迭代）→ 顶层捕获返回 None 并留痕。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        messages = [
            {"role": "user", "content": "x" * 800, "seq": 1, "tool_calls": bad_tool_calls},
            {"role": "assistant", "content": "y" * 800, "seq": 2},
        ]
        with caplog.at_level(logging.ERROR):
            result = _run(svc.compress_messages(messages, context_window=800))
        assert result is None
        assert any("compress_messages 顶层异常" in r.getMessage() for r in caplog.records)

    def test_no_llm_fn_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """无 LLM 函数 → 显式跳过压缩（None + warning），不抛异常。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(backend=None)
        with caplog.at_level(logging.WARNING):
            result = _run(svc.compress_messages(_round_like_messages(), context_window=800))
        assert result is None
        assert any("未提供 LLM 调用函数" in r.getMessage() for r in caplog.records)


class TestMultiRoundCompression:
    def test_second_round_runs_when_still_above_trigger(self) -> None:
        """首轮压缩后仍超触发线 → 进入第二轮（第二轮无料可压则保住首轮产物）。

        5 条各 200t：recent=375t 只留 seq6；old=seq1-5 分两批压成块；
        块+recent 总量仍超触发线（500t）→ 第二轮扫描后无料可压，返回首轮结果。
        """
        mod = _load_plugin_module()
        injected = {
            "compress_trigger_ratio": 0.5,
            "budgets": {"recent": 0.375, "l1": 0.1, "l2": 0.05},
        }
        llm = _RecordingLlm(_valid_compress_json())
        svc = mod.CompressionService(
            backend=None,
            llm_call_fn=llm,
            config=mod.CompressionConfig.from_yaml_config(1000, injected=injected),
            injected=injected,
        )
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "A" * 400, "seq": i + 1}
            for i in range(5)
        ] + [{"role": "user", "content": "B" * 400, "seq": 6}]

        result = _run(svc.compress_messages(messages, context_window=1000, trigger_ratio=0.5))
        assert result is not None
        # 首轮产物：seq1-5 原位替换为块消息，recent（seq6）保留
        block_seqs = [
            m["seq"]
            for m in result
            if isinstance(m.get("metadata"), dict) and "compression_ref" in m["metadata"]
        ]
        assert sorted(block_seqs) == [1, 2, 3, 4]
        kept_recent = [m["seq"] for m in result if "metadata" not in m]
        assert kept_recent == [6]
        # LLM 只被首轮的两个批次调用（第二轮无料可压，不浪费调用）
        assert len(llm.calls) == 2


class TestLegacyBlockHandling:
    @pytest.mark.parametrize(
        "legacy_content",
        ["## 历史对话压缩摘要（旧窗口产物）", None],  # None → 运行时替换为 _COMPRESSION_NOTICE
        ids=["legacy-header", "legacy-notice"],
    )
    def test_legacy_summary_block_deleted(self, legacy_content: Any) -> None:
        """遗留格式压缩摘要（system）不参与压缩，直接按其 seq 删除。"""
        mod = _load_plugin_module()
        if legacy_content is None:
            legacy_content = mod._COMPRESSION_NOTICE
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        messages = [
            {"role": "system", "content": legacy_content, "seq": 1},
            {"role": "user", "content": "A" * 400, "seq": 2},
            {"role": "assistant", "content": "B" * 400, "seq": 3},
        ]
        result = _run(
            mod.CompressionService._do_compress_round(
                svc, messages, 800, {"recent": 300}
            )
        )
        assert result is not None
        compressed, deleted = result
        assert deleted == [1], "遗留摘要的槽位应被删除"
        assert all(m["content"] != legacy_content for m in compressed)

    def test_all_system_messages_returns_none(self) -> None:
        """序列里没有任何非 system 消息 → 无料可压返回 None。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        messages = [
            {"role": "system", "content": "普通系统提示", "seq": 1},
            {"role": "system", "content": "另一条系统提示", "seq": 2},
        ]
        assert _run(mod.CompressionService._do_compress_round(svc, messages, 800, {"recent": 100})) is None

    def test_full_tool_pair_migration_returns_none(self) -> None:
        """recent 的 tool 结果把 old 里唯一的配对 assistant 迁走 → old 为空返回 None。

        recent 预算 200t：尾部 3 条小消息留守，配对 assistant（201t）落 old；
        配对迁移把它挪回 recent 后 old 清空，本轮无料可压。
        """
        mod = _load_plugin_module()
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(_valid_compress_json())
        )
        messages = [
            {
                "role": "assistant",
                "content": "A" * 400,
                "seq": 1,
                "tool_calls": [{"id": "t1", "function": {"name": "s", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "R" * 100, "seq": 2, "tool_call_id": "t1"},
            {"role": "user", "content": "U" * 100, "seq": 3},
            {"role": "user", "content": "V" * 100, "seq": 4},
        ]
        assert _run(mod.CompressionService._do_compress_round(svc, messages, 800, {"recent": 200})) is None


# ═══════════════════════════════════════════════════════════
# 压缩内容构建与落库边界
# ═══════════════════════════════════════════════════════════


class TestBuildCompressionContent:
    def test_no_seq_batch_blocks_empty(self) -> None:
        """批次消息不带 int seq → 不产块消息（空列表）。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(backend=None)
        assert svc._build_batch_blocks([{"role": "user", "content": "x"}], {"l1": "y"}, []) == []

    def test_no_llm_fn_returns_none(self) -> None:
        """_build_compression_content 无 LLM 函数 → None。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(backend=None)
        assert _run(svc._build_compression_content([{"role": "user", "content": "x"}])) is None

    def test_compress_all_none_propagates(self) -> None:
        """LLM 输出 think-only（compress_all 得 None）→ 本批跳过。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm("<think>只想想</think>")
        )
        assert _run(svc._build_compression_content([{"role": "user", "content": "x"}])) is None

    def test_empty_l1_rejected(self) -> None:
        """压缩产物 l1 为空 → 视为无效结果跳过（不接受空摘要）。"""
        import json

        mod = _load_plugin_module()
        parsed = json.loads(_valid_compress_json())
        parsed["l1"] = ""
        svc = mod.CompressionService(
            backend=None, llm_call_fn=_RecordingLlm(json.dumps(parsed, ensure_ascii=False))
        )
        assert _run(svc._build_compression_content([{"role": "user", "content": "x"}])) is None

    def test_save_without_backend_returns_empty_refs(self) -> None:
        """backend 为 None → 不落库，引用清单为空（块退化为纯内联摘要）。"""
        mod = _load_plugin_module()
        svc = mod.CompressionService(backend=None)
        import json

        comp_result = json.loads(_valid_compress_json())
        refs = _run(
            svc.save_compression_result(
                old_msgs=[{"role": "user", "content": "x", "seq": 1}],
                comp_result=comp_result,
                pipeline_id="p",
                session_id="s",
                context_window=1000,
            )
        )
        assert refs == []


# ═══════════════════════════════════════════════════════════
# token 估算（tool_calls 增量）与 tool 配对切分
# ═══════════════════════════════════════════════════════════


class TestEstimateMsgTokensToolCalls:
    @staticmethod
    def _msg(args: Any) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "c" * 20,
            "tool_calls": [{"function": {"arguments": args}}],
        }

    @pytest.mark.parametrize(
        "estimator_owner",
        ["service", "plugin"],
    )
    @pytest.mark.parametrize(
        ("args", "extra"),
        [('{"a": 1}', 4), ("", 0)],
        ids=["with-args", "empty-args"],
    )
    def test_tool_call_arguments_counted(
        self, estimator_owner: str, args: str, extra: int
    ) -> None:
        """标准 tool_calls（function.arguments）按参数长度追加估算；空参数不追加。"""
        mod = _load_plugin_module()
        if estimator_owner == "service":
            estimate = mod.CompressionService._estimate_msg_tokens
        else:
            estimate = mod.ContextWindowGuardPlugin._estimate_msg_tokens
        msg = self._msg(args)
        base = max(1, 20 // 2)
        assert estimate(msg) == base + extra
        assert estimate(msg) >= estimate({"role": "assistant", "content": "c" * 20})


class TestSplitPreservingToolPairs:
    def test_partial_migration_keeps_head(self) -> None:
        """recent 的 tool 结果 → old 尾部的配对 assistant 迁入 recent，其余不动。"""
        mod = _load_plugin_module()
        messages = [
            {"role": "user", "content": "u1", "seq": 1},
            {
                "role": "assistant",
                "content": "a",
                "seq": 2,
                "tool_calls": [{"id": "t1", "function": {"arguments": "{}"}}],
            },
            {"role": "tool", "content": "r1", "seq": 3, "tool_call_id": "t1"},
            {"role": "user", "content": "u2", "seq": 4},
        ]
        old, recent = mod.CompressionService._split_preserving_tool_pairs(messages, 2)
        assert [m["seq"] for m in old] == [1]
        assert [m["seq"] for m in recent] == [2, 3, 4]

    def test_unmatched_tool_id_keeps_split(self) -> None:
        """recent 的 tool id 在 old 中无配对 → 切分点保持不变。"""
        mod = _load_plugin_module()
        messages = [
            {"role": "user", "content": "u1", "seq": 1},
            {"role": "tool", "content": "r", "seq": 2, "tool_call_id": "ghost"},
        ]
        old, recent = mod.CompressionService._split_preserving_tool_pairs(messages, 1)
        assert [m["seq"] for m in old] == [1]
        assert [m["seq"] for m in recent] == [2]

    def test_tool_without_call_id_not_tracked(self) -> None:
        """无 tool_call_id 的 tool 消息不参与配对迁移。"""
        mod = _load_plugin_module()
        messages = [
            {
                "role": "assistant",
                "content": "a",
                "seq": 1,
                "tool_calls": [{"id": "t1", "function": {"arguments": "{}"}}],
            },
            {"role": "tool", "content": "r", "seq": 2},
        ]
        old, recent = mod.CompressionService._split_preserving_tool_pairs(messages, 1)
        assert [m["seq"] for m in old] == [1]
        assert [m["seq"] for m in recent] == [2]


# ═══════════════════════════════════════════════════════════
# 插件：优先级 / token 估算 / 无依赖早退
# ═══════════════════════════════════════════════════════════


class TestPluginBasics:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [({}, 5), ({"priority": 9}, 9)],
        ids=["default", "explicit"],
    )
    def test_priority(self, config: dict[str, Any], expected: int) -> None:
        """priority 缺省 5，显式配置覆盖。"""
        mod = _load_plugin_module()
        assert mod.ContextWindowGuardPlugin(config).priority == expected

    def test_estimate_effective_tokens_track_fallback(self) -> None:
        """llm_usage 为空时从 track.llm_usage 回退，并叠加新增消息增量。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        plugin._tracked_msg_count = 2
        ctx = mod._make_minimal_ctx(
            state={"track.llm_usage": {"input_tokens": 1000}},
        )
        messages = [
            {"role": "user", "content": "m1", "seq": 1},
            {"role": "assistant", "content": "m2", "seq": 2},
            {"role": "user", "content": "x" * 30, "seq": 3},
        ]
        estimate = _run(plugin._estimate_effective_tokens(messages, ctx))
        assert estimate == 1000 + mod.ContextWindowGuardPlugin._estimate_msg_tokens(messages[2])
        assert estimate >= 1000

    def test_estimate_effective_tokens_no_delta(self) -> None:
        """消息数未超追踪计数 → 直接沿用上一轮真实 input_tokens。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        plugin._tracked_msg_count = 5
        ctx = mod._make_minimal_ctx(state={"llm_usage": {"input_tokens": 800}})
        messages = [{"role": "user", "content": "x" * 1000, "seq": i} for i in range(3)]
        assert _run(plugin._estimate_effective_tokens(messages, ctx)) == 800


class TestExecuteEarlyExits:
    def test_missing_context_window_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """context_window 缺失 → 守卫停摆（空结果），告警只发一次。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(state={"messages": [{"role": "user", "content": "x"}]})
        with caplog.at_level(logging.ERROR):
            first = _run(plugin.execute(ctx))
            second = _run(plugin.execute(ctx))
        assert first.state_updates == {}
        assert second.state_updates == {}
        assert not first.skip_remaining
        errors = [r for r in caplog.records if "context_window 未设置" in r.getMessage()]
        assert len(errors) == 1, "同一实例的重复告警应被去重"

    def test_empty_messages_returns_empty_result(self) -> None:
        """窗口已配置但消息为空 → 空结果早退。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(state={"context_window": 128_000, "messages": []})
        result = _run(plugin.execute(ctx))
        assert result.state_updates == {}

    def test_no_deps_returns_empty_result(self) -> None:
        """无 backend/capability/已注册服务 → 无法构建压缩服务，空结果早退。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": [{"role": "user", "content": "x", "seq": 1}],
            }
        )
        result = _run(plugin.execute(ctx))
        assert result.state_updates == {}


# ═══════════════════════════════════════════════════════════
# 插件主流程：进度通知 / ops 构造分支 / 降级
# ═══════════════════════════════════════════════════════════


def _mock_service(return_value: Any = None) -> MagicMock:
    service = MagicMock()
    service.setup = MagicMock()
    service.compress_messages = AsyncMock(return_value=return_value)
    return service


def _trigger_state(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """越过触发线的 state（llm_usage 80k > 0.5×128k）。"""
    state = {
        "context_window": 128_000,
        "messages": [
            {"role": "user", "content": "msg " + "x" * 4000, "seq": i}
            for i in range(1, 16)
        ],
        "llm_usage": {"input_tokens": 80_000},
    }
    state.update(extra or {})
    return state


class TestExecuteProgressNotification:
    def test_compression_start_emitted(self) -> None:
        """触发压缩时 on_chunk 收到 compression_start 事件（含 pipeline_id）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        events: list[dict[str, Any]] = []

        def _on_chunk(event: dict[str, Any]) -> None:
            events.append(event)

        service = _mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(
            state=_trigger_state({"on_chunk": _on_chunk, "pipeline_id": "pipe-ckpt"}),
            pipeline_id="pipe-ckpt",
        )
        ctx._services["context_service"] = service
        _run(mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5}).execute(ctx))
        assert {"type": "compression_start", "pipeline_id": "pipe-ckpt"} in events

    def test_raising_on_chunk_does_not_break_pipeline(self) -> None:
        """on_chunk 回调抛异常被抑制，压缩流程照常完成。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        def _boom(_event: dict[str, Any]) -> None:
            raise RuntimeError("sink broken")

        service = _mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(
            state=_trigger_state({"on_chunk": _boom}), pipeline_id="pipe-boom"
        )
        ctx._services["context_service"] = service
        result = _run(mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5}).execute(ctx))
        assert not result.skip_remaining


class TestExecuteOpsConstruction:
    def test_block_slot_not_double_nulled(self) -> None:
        """块消息占用的槽位不再追加 set(seq, null)（同一槽位一个 op）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        block = {"role": "system", "name": "compressed", "seq": 3, "content": "<compressed/>"}
        service = _mock_service()
        service.compress_messages = AsyncMock(
            return_value=[
                {"role": "user", "content": "kept", "seq": 1},
                block,
            ]
        )
        service._last_deleted_seqs = [2, 3]
        service._last_block_msgs = [block]
        ctx = mod._make_minimal_ctx(state=_trigger_state(), pipeline_id="pipe-slot")
        ctx._services["context_service"] = service
        result = _run(mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5}).execute(ctx))
        ops = _ops_by_seq(result.state_updates)
        assert ops[2] == {"op": "set", "seq": 2, "msg": None}
        assert ops[3] == {"op": "set", "seq": 3, "msg": block}
        # 槽位 3 只出现一次（块写入），没有同槽位的 null op
        raw_ops = result.state_updates["messages"]["_ops"]
        assert [op["seq"] for op in raw_ops].count(3) == 1

    def test_missing_service_attrs_fall_back_to_seq_diff(self) -> None:
        """service 未携带 _last_deleted_seqs/_last_block_msgs → 按 seq 差集回退。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        messages = _trigger_state()["messages"]
        shrunk = [
            {"role": m["role"], "content": "s", "seq": m["seq"]}
            for m in messages
            if m["seq"] in (1, 4)
        ]
        service = _mock_service(return_value=shrunk)  # 不设置 _last_* 属性
        ctx = mod._make_minimal_ctx(state=_trigger_state(), pipeline_id="pipe-diff")
        ctx._services["context_service"] = service
        result = _run(mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5}).execute(ctx))
        ops = _ops_by_seq(result.state_updates)
        deleted = {seq for seq, op in ops.items() if op["msg"] is None}
        assert deleted == {2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
        assert all(ops[seq]["msg"] is None for seq in deleted)


class TestExecuteDegradePath:
    def test_degrade_reports_clean_ops(self) -> None:
        """压缩无产出时：clean 删除 ops 照常上报（不因压缩失败而丢弃）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        backend = _FakeBackend(
            [
                _l1_chunk(["pipeline:pipe-dg", "seq:1-5", "ctx:64000"]),
            ]
        )
        mod.set_memory_backend(backend)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "## 历史对话压缩摘要 旧窗口", "seq": 1}
        ]
        messages.extend({"role": "user", "content": "x" * 4000, "seq": i} for i in range(2, 16))
        state = {
            "context_window": 128_000,
            "messages": messages,
            "llm_usage": {"input_tokens": 80_000},
        }
        service = _mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(state=state, pipeline_id="pipe-dg")
        ctx._services["context_service"] = service
        result = _run(mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5}).execute(ctx))
        ops = _ops_by_seq(result.state_updates)
        assert ops[1] == {"op": "set", "seq": 1, "msg": None}
        assert not result.skip_remaining


class TestCompressFailureNotification:
    def test_failure_event_emitted_once(self) -> None:
        """压缩失败向前端推送一次 compression_failed，连续失败不刷屏。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        events: list[tuple[str, dict[str, Any], str]] = []

        async def _emit(event: str, payload: dict[str, Any], thread_id: str) -> None:
            events.append((event, payload, thread_id))

        mod.set_frontend_emit(_emit)
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        service = _mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(
            state=_trigger_state({"session_id": "sess-9"}), pipeline_id="pipe-emit"
        )
        ctx._services["context_service"] = service
        _run(plugin.execute(ctx))
        _run(plugin.execute(ctx))
        assert len(events) == 1, "同一故障周期只推一次"
        event, payload, thread_id = events[0]
        assert event == "compression_failed"
        assert thread_id == "sess-9"
        assert payload["pipeline_id"] == "pipe-emit"
        assert "压缩失败" in payload["message"]

    def test_emit_failure_swallowed(self) -> None:
        """前端通道抛异常 → 只留日志，管线降级路径不受影响。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        async def _boom(_event: str, _payload: dict[str, Any], _tid: str) -> None:
            raise RuntimeError("frontend down")

        mod.set_frontend_emit(_boom)
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        service = _mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(state=_trigger_state(), pipeline_id="pipe-emitfail")
        ctx._services["context_service"] = service
        result = _run(plugin.execute(ctx))
        assert not result.skip_remaining
        assert result.state_updates.get("messages") is None


# ═══════════════════════════════════════════════════════════
# trim / clean / 拼接估算的 backend 边界
# ═══════════════════════════════════════════════════════════


class TestTrimCoveredMessagesGuards:
    def test_missing_pipeline_or_backend_returns_original(self) -> None:
        """无 pipeline_id 或无 backend → 原消息原样返回。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.ContextWindowGuardPlugin()
        messages = [{"role": "user", "content": "x", "seq": 1}]
        ctx = mod._make_minimal_ctx()  # 无 pipeline_id
        assert _run(plugin._trim_covered_messages(ctx, messages)) is messages
        ctx2 = mod._make_minimal_ctx(pipeline_id="pipe-x")
        assert _run(plugin._trim_covered_messages(ctx2, messages)) is messages

    def test_backend_error_returns_original(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """检索异常 → 原消息原样返回并 warning。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_BoomBackend())
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-boom")
        messages = [{"role": "user", "content": "x", "seq": 1}]
        with caplog.at_level(logging.WARNING):
            result = _run(plugin._trim_covered_messages(ctx, messages))
        assert result is messages
        assert any("记忆后端检索失败" in r.getMessage() for r in caplog.records)

    def test_no_l1_chunks_returns_original(self) -> None:
        """检索无 L1 块 → 原消息原样返回。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_FakeBackend([]))
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-empty")
        messages = [{"role": "user", "content": "x", "seq": 1}]
        assert _run(plugin._trim_covered_messages(ctx, messages)) is messages

    def test_l1_without_seq_tags_returns_original(self) -> None:
        """L1 块无 seq 标签（max_end=0）→ 不裁剪。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_FakeBackend([_l1_chunk(["pipeline:p"])]))
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-noseq")
        messages = [{"role": "user", "content": "x", "seq": 1}]
        assert _run(plugin._trim_covered_messages(ctx, messages)) is messages

    def test_trim_keeps_system_messages(self) -> None:
        """裁剪保留全部 system 消息，仅移除被覆盖的非 system 消息。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(
            _FakeBackend([_l1_chunk(["pipeline:p", "seq:1-4"])])
        )
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        messages = [
            {"role": "system", "content": "系统提示", "seq": 0},
            *[{"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 5)],
            *[{"role": "user", "content": f"r{i}", "seq": i} for i in range(5, 9)],
        ]
        result = _run(plugin._trim_covered_messages(ctx, messages))
        kept_seqs = [m["seq"] for m in result if m["role"] != "system"]
        assert kept_seqs == [5, 6, 7, 8]
        assert any(m["role"] == "system" for m in result)

    def test_protection_keeps_original_when_trim_too_aggressive(self) -> None:
        """裁剪后非 system 消息不足原 10% → 放弃裁剪保留原消息。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(
            _FakeBackend([_l1_chunk(["pipeline:p", "seq:1-10"])])
        )
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        messages = [{"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 12)]
        assert _run(plugin._trim_covered_messages(ctx, messages)) is messages

    def test_messages_without_seq_survive(self) -> None:
        """非 system 消息缺 int seq → 视为不可裁剪对象，全量保留。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(
            _FakeBackend([_l1_chunk(["pipeline:p", "seq:1-5"])])
        )
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        messages = [
            {"role": "user", "content": "no-seq-a"},
            {"role": "assistant", "content": "no-seq-b"},
        ]
        assert _run(plugin._trim_covered_messages(ctx, messages)) is messages


class TestCleanIfWindowChangedGuards:
    def test_backend_error_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """检索异常 → 跳过窗口变更检测（None）并 warning。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_BoomBackend())
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-clean-boom")
        with caplog.at_level(logging.WARNING):
            result = _run(
                plugin.clean_if_window_changed(
                    [{"role": "user", "content": "x"}], 128_000, ctx
                )
            )
        assert result is None
        assert any("记忆后端检索失败" in r.getMessage() for r in caplog.records)

    def test_l1_without_seq_returns_none(self) -> None:
        """L1 块无 seq 标签 → 无可比较的最新块，返回 None。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_FakeBackend([_l1_chunk(["pipeline:p"])]))
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        result = _run(
            plugin.clean_if_window_changed([{"role": "user", "content": "x"}], 128_000, ctx)
        )
        assert result is None

    def test_window_changed_but_nothing_to_clean(self) -> None:
        """窗口变更但序列里没有旧格式摘要 → 无可清理，返回 None。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(
            _FakeBackend([_l1_chunk(["pipeline:p", "seq:1-5", "ctx:64000"])])
        )
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        messages = [
            {"role": "system", "content": "正常系统提示", "seq": 1},
            {"role": "user", "content": "x", "seq": 2},
        ]
        result = _run(plugin.clean_if_window_changed(messages, 128_000, ctx))
        assert result is None


class TestEstimateAssembledTokens:
    def test_backend_error_returns_minus_one(self) -> None:
        """检索异常 → 无法估算返回 -1（由上层走全量字符兜底）。"""
        mod = _load_plugin_module()
        mod.set_memory_backend(_BoomBackend())
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-asm")
        assert _run(plugin._estimate_assembled_tokens(ctx, [])) == -1

    def test_snapshot_and_recent_estimated(self) -> None:
        """L1 块 + STATE_SNAPSHOT 块 + system/recent 消息逐项计入估算。

        检索结果混入非 dict 条目与无 tags 条目（应被跳过）。
        """
        mod = _load_plugin_module()
        backend = _FakeBackend(
            [
                "junk-non-dict",  # 非 dict → 跳过
                {"id": "no-tags", "content": "x" * 100},  # 无 tags → 跳过
                _l1_chunk(["pipeline:p", "seq:1-5"], content="L" * 40),
                {
                    "id": "c-ss",
                    "content": "S" * 100,
                    "memory_type": "chunk",
                    "metadata": {"tags": ["STATE_SNAPSHOT", "pipeline:p", "seq_end:5"]},
                },
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="p")
        messages = [
            {"role": "system", "content": "s" * 10, "seq": 0},
            {"role": "user", "content": "r" * 20, "seq": 6},
            {"role": "user", "content": "old" * 20, "seq": 2},  # seq <= max_end 不计 recent
        ]
        total = _run(plugin._estimate_assembled_tokens(ctx, messages))
        # L1=40//2=20，snapshot=100//2=50，system=10//2=5，recent=20//2=10
        assert total == 20 + 50 + 5 + 10
        assert total > 0


class TestParseSeqFromTags:
    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            (["seq:1-5"], (1, 5)),
            ([123, "seq:1-5"], (1, 5)),  # 非 str 标签跳过
            (["seq:abc-def"], (0, 0)),  # 区间解析失败回零
            (["seq:7"], (7, 7)),  # 无区间单值
            (["seq:xyz"], (0, 0)),
            (["seq_end:9"], (0, 9)),
            (["seq_end:oops"], (0, 0)),
            (["seq:2-6", "seq_end:99"], (2, 99)),  # 后写标签覆盖 seq_end
        ],
    )
    def test_tag_forms(
        self, tags: list[Any], expected: tuple[int, int]
    ) -> None:
        """seq:start-end / seq:N / seq_end:N 标签解析契约。"""
        mod = _load_plugin_module()
        assert mod.ContextWindowGuardPlugin._parse_seq_from_tags(tags) == expected


class TestFindChunkWindow:
    @pytest.mark.parametrize(
        ("results", "target", "expected"),
        [
            (["not-a-dict", _l1_chunk(["seq:1-5", "ctx:128000"])], 5, 128_000),
            ([{"metadata": {}}], 5, 0),  # 无 tags → 跳过
            ([_l1_chunk(["seq:1-2", "ctx:64000"])], 5, 0),  # seq_end 不匹配
            ([_l1_chunk(["seq:1-5", "ctx:abc"])], 5, 0),  # ctx 标签非整数
        ],
        ids=["skip-non-dict", "skip-no-tags", "seq-mismatch", "bad-ctx-int"],
    )
    def test_window_resolution(
        self, results: list[Any], target: int, expected: int
    ) -> None:
        """窗口值解析：跳过异形条目，只认 seq_end 匹配且 ctx 标签合法的块。"""
        mod = _load_plugin_module()
        assert mod.ContextWindowGuardPlugin._find_chunk_window(results, target) == expected


# ═══════════════════════════════════════════════════════════
# 服务构建（capability 路径）与 setup 容错
# ═══════════════════════════════════════════════════════════


class TestGetMemoryServiceWithCapability:
    def test_capability_path_builds_and_compresses(self) -> None:
        """注入 capability caller + compression.model → 端到端压缩产出块 ops。

        capability caller 是内核能力边界（外部依赖）：记录调用参数并返回
        llm.complete_stream 信封；压缩主链路走真实 CompressionService。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        captured: dict[str, Any] = {}

        async def _caller(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            captured.update(params)
            return {"success": True, "data": {"text": _valid_compress_json()}}

        mod.set_capability_caller(_caller)
        window_cfg = {
            "compress_trigger_ratio": 0.1,
            "compression": {"model": "m-1", "temperature": 0.3},
        }
        plugin = mod.ContextWindowGuardPlugin({"context_window": window_cfg})
        messages = [
            {"role": "user", "content": "A" * 400, "seq": 1},
            {"role": "assistant", "content": "B" * 400, "seq": 2},
        ]
        ctx = mod._make_minimal_ctx(
            state={"context_window": 1000, "messages": messages},
            config={"context_window": window_cfg},
        )
        result = _run(plugin.execute(ctx))
        # 压缩模型与调用方显式参数经 capability 边界透传
        assert captured["args"]["model"] == "m-1"
        assert captured["args"]["temperature"] == 0.3
        assert captured["tool_name"] == "llm.complete_stream"
        # 两条消息同批压缩：seq1 过程块 + seq2 快照块原位替换；
        # 无 backend → 落库跳过，块引用留空（fail-open 内联摘要）
        ops = _ops_by_seq(result.state_updates)
        assert set(ops) == {1, 2}
        assert ops[1]["msg"]["metadata"]["compression_ref"]["kind"] == "process"
        assert ops[2]["msg"]["metadata"]["compression_ref"]["kind"] == "state_snapshot"
        assert ops[1]["msg"]["metadata"]["compression_ref"]["memory_ids"] == []

    def test_backend_only_builds_service_without_llm(self) -> None:
        """仅有 backend 无 capability → 构建服务（llm 函数为 None，压缩空转）。"""
        mod = _load_plugin_module()
        mod._capability_caller = None
        mod.set_memory_backend(_FakeBackend())
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": [{"role": "user", "content": "x", "seq": 1}],
            }
        )
        service = plugin._get_memory_service(ctx, {})
        assert service is not None
        assert service._llm_call_fn is None

    def test_setup_exception_swallowed(self) -> None:
        """service.setup 抛异常 → 留日志不反噬，execute 正常返回。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        service = _mock_service(return_value=None)
        service.setup = MagicMock(side_effect=RuntimeError("setup boom"))
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": [{"role": "user", "content": "x", "seq": 1}],
            }
        )
        ctx._services["context_service"] = service
        result = _run(plugin.execute(ctx))
        assert "_tracked_msg_count" in result.state_updates


# ═══════════════════════════════════════════════════════════
# 压缩 LLM 调用函数的信封校验
# ═══════════════════════════════════════════════════════════


class TestCompressCallFnEnvelopeShape:
    @pytest.mark.parametrize(
        ("result", "needle"),
        [
            ("not-a-dict", "信封形状异常"),
            ({"success": True, "data": "nope"}, "返回形状异常"),
        ],
        ids=["non-dict-envelope", "non-dict-data"],
    )
    def test_shape_violation_raises(self, result: Any, needle: str) -> None:
        """信封/data 形状异常 → fail-closed 上抛（不伪装空响应）。"""
        mod = _load_plugin_module()

        async def _caller(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            return result

        fn = mod._build_compress_llm_call_fn(_caller, model_id="m-1")
        with pytest.raises(RuntimeError, match=needle):
            _run(fn([{"role": "user", "content": "x"}]))
