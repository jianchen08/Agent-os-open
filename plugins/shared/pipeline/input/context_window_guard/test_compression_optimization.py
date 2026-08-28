# @feature: FP-0.2.〇 管道引擎（压缩优化） | @vision: V3 可嵌入 | @ci: python-coverage
"""压缩优化 TDD 测试（语义标记 + fork 消息队列压缩）。

对应 docs/tasks/task_compression_optimization.md 两项任务：

任务 1 —— 压缩消息加语义标记（_context_form 内部字段）：
1. test_context_form_vocabulary —— 五种语义形态常量（对齐 DSH ContextForm）
2. test_format_messages_renders_form_label —— _format_messages 给带标记的消息加 [form] 前缀
3. test_l1_l2_blocks_tagged_recall —— prompt_build 产出的 L1/L2 压缩块消息打 _context_form="recall"
4. test_state_snapshot_message_tagged_snapshot —— 状态快照消息打 _context_form="snapshot"
5. test_build_messages_strips_context_form —— llm_core 发给最终 LLM 前清理 _context_form
6. test_normalize_results_tags_recall —— memory_read 检索结果条目打 _context_form="recall"

任务 2 —— fork 消息队列压缩（对标 DSH summarizer，产物结构不变）：
7. test_compress_all_sends_message_list —— 压缩调用发消息列表，fork = [system] +
   compression_messages + 待压缩消息（原样）+ [user 压缩指令]
8. test_fork_renders_semantic_labels —— fork 时带语义标签（任务 1+2 叠加生效）
9. test_instruction_requires_five_part_json —— COMPACTION_INSTRUCTION 五段 JSON，
   旧占位符 {messages}/{state_snapshot}/{recent_process_blocks} 已删
10. test_chat_completion_accepts_message_list —— llm_client 支持任意消息列表
   （已退役：llm_client 全仓零生产消费者，本用例随其删除）
11. test_fallback_flattens_message_list —— capability 回退路径列表压平为字符串
12. test_compress_messages_threads_fork_context —— Service 把 fork 上下文传进压缩调用

测试不依赖真实 LLM/记忆后端——通过 AsyncMock / FakeBackend 注入。
[来源: docs/tasks/task_compression_optimization.md]
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

# 本插件目录（context_window_guard）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# plugins/shared/（pipeline 包）
_SHARED_DIR = str(_PLUGIN_DIR.parents[2])
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

# pipeline/input 目录（prompt_build 经 context_window_guard.plugin 复用 CompressionConfig，
# 跨插件用例加载 prompt_build 时导入方向为 guard → 可达性由本目录保证）
_INPUT_DIR = str(_PLUGIN_DIR.parents[0])
if _INPUT_DIR not in sys.path:
    sys.path.insert(0, _INPUT_DIR)

# llm_core 插件目录（import adapter / _message_normalizer 用）
_LLM_CORE_DIR = str(_PLUGIN_DIR.parents[1] / "core" / "llm_core")
if _LLM_CORE_DIR not in sys.path:
    sys.path.insert(0, _LLM_CORE_DIR)

# SDK 源码（llm_core 的 agentos 类型依赖）
_SDK_SRC = str(_PLUGIN_DIR.parents[3] / "sdk" / "src")
if Path(_SDK_SRC).exists() and _SDK_SRC not in sys.path:
    sys.path.insert(0, _SDK_SRC)


def _load_module(mod_name: str, rel_path: tuple[str, ...]) -> Any:
    """按相对 plugins/shared/pipeline/ 的路径动态加载插件模块（每次新建，避免状态污染）。"""
    module_path = _PLUGIN_DIR.parents[1].joinpath(*rel_path)
    assert module_path.exists(), f"module missing at {module_path}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, f"Cannot load {module_path}"
    assert spec.loader is not None, f"Cannot load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_cwg() -> Any:
    """加载 context_window_guard plugin 模块。"""
    return _load_module("cwg_compress_opt_test", ("input", "context_window_guard", "plugin.py"))


def _load_memory_read() -> Any:
    """加载 memory_read plugin 模块。"""
    return _load_module("mr_compress_opt_test", ("input", "memory_read", "plugin.py"))


def _load_llm_core() -> Any:
    """加载 llm_core plugin 模块（spec 加载避免裸名 'plugin' 跨文件冲突）。

    llm_core/plugin.py 平铺 import 本目录模块（adapter/_message_normalizer/
    uploads_path），全车道共跑时这些裸名可能被其他插件
    目录的同名模块（7 个 adapter.py 等）占据 sys.modules 或 sys.path 优先位。
    加载窗口内 pin 住：逐出裸名缓存 + llm_core 目录压 sys.path[0]，执行完
    还原现场，保证解析到本目录实现且不污染其他测试。
    """
    bare_names = ("adapter", "_message_normalizer", "uploads_path")
    saved = {n: sys.modules.get(n) for n in bare_names}
    for n in bare_names:
        sys.modules.pop(n, None)
    sys.path.insert(0, _LLM_CORE_DIR)
    try:
        return _load_module("lc_compress_opt_test", ("core", "llm_core", "plugin.py"))
    finally:
        sys.path.remove(_LLM_CORE_DIR)
        for n, old in saved.items():
            if old is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = old


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _valid_compress_json() -> str:
    """返回一份合法的 5 部分 JSON 压缩响应（供 mock LLM 返回）。"""
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
  "keywords": ["关键词1", "关键词2"],
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
    "user_profile_updates": "偏好 P",
    "project_knowledge_updates": "决策 D",
    "experience_updates": null
  }
}"""


class FakeBackend:
    """记录 search/add 调用的伪 IMemoryBackend（duck-typed）。"""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.search_returns: list[dict[str, Any]] = list(results or [])
        self.add_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.search_returns)

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        return f"mem-{len(self.add_calls)}"


def _chunk(mem_id: str, content: str, tags: list[str]) -> dict[str, Any]:
    """构造一条统一形态的后端 chunk 结果。"""
    return {
        "id": mem_id,
        "content": content,
        "score": 1.0,
        "memory_type": "chunk",
        "metadata": {"tags": tags},
    }


# ═══════════════════════════════════════════════════════════
# 任务 1：压缩消息加语义标记
# ═══════════════════════════════════════════════════════════


class TestContextFormVocabulary:
    def test_context_form_vocabulary(self) -> None:
        """五种语义形态常量存在，值与 DSH ContextForm 词汇对齐。"""
        mod = _load_cwg()
        assert mod.CONTEXT_FORM_INSTRUCTIONS == "instructions"
        assert mod.CONTEXT_FORM_NOTICE == "notice"
        assert mod.CONTEXT_FORM_RECALL == "recall"
        assert mod.CONTEXT_FORM_RELAY == "relay"
        assert mod.CONTEXT_FORM_SNAPSHOT == "snapshot"
        # form -> 可见标签映射（喂压缩 LLM 时渲染）
        labels = mod.CONTEXT_FORM_LABELS
        assert labels["instructions"] == "[instructions]"
        assert labels["notice"] == "[notice]"
        assert labels["recall"] == "[recall]"
        assert labels["relay"] == "[relay]"
        assert labels["snapshot"] == "[snapshot]"


class TestFormatMessagesSemanticLabels:
    def test_format_messages_renders_form_label(self) -> None:
        """_format_messages 给带 _context_form 的消息加 [form] 前缀；无标记消息不受影响。"""
        mod = _load_cwg()
        compressor = mod.ContextCompressor()
        messages = [
            {"role": "user", "content": "项目规则：必须用中文回复", "_context_form": "instructions"},
            {"role": "user", "content": "一条普通消息"},
            {"role": "system", "content": "状态快照内容", "_context_form": "snapshot"},
        ]
        text = compressor._format_messages(messages)

        # 带 form 的消息：标签出现在内容紧邻前方
        assert "[instructions]" in text
        assert "[snapshot]" in text
        i_tag, i_content = text.find("[instructions]"), text.find("项目规则")
        assert 0 <= i_tag < i_content, "instructions 标签应在内容之前"
        s_tag, s_content = text.find("[snapshot]"), text.find("状态快照内容")
        assert 0 <= s_tag < s_content, "snapshot 标签应在内容之前"

        # 无标记消息不带任何 [xxx] 语义标签
        plain_start = text.find("一条普通消息")
        assert plain_start > 0
        # 普通消息前方紧邻的一行不应有语义标签（只有 【用户 N】 角色头）
        preceding = text[:plain_start]
        for label in ("[instructions]", "[notice]", "[recall]", "[relay]", "[snapshot]"):
            assert label not in preceding.rsplit("\n", 2)[-2:], (
                f"无标记消息不应带 {label} 标签"
            )

    def test_format_messages_roles_unchanged_without_form(self) -> None:
        """回归：无 _context_form 时角色头行为与旧版一致。"""
        mod = _load_cwg()
        compressor = mod.ContextCompressor()
        messages = [
            {"role": "user", "content": "用户输入"},
            {"role": "assistant", "content": "助手回复"},
        ]
        text = compressor._format_messages(messages)
        assert "【用户" in text
        assert "【助手" in text
        assert "[recall]" not in text
        assert "[instructions]" not in text


class TestLLMCoreStripsContextForm:
    """llm_core _build_messages 发给最终 LLM 前清理 _context_form（不影响 cache）。"""

    def _build(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        mod = _load_llm_core()
        core = mod.LLMCore(config={"model_name": "test"}, adapter=MagicMock())
        return core._build_messages(state)

    def test_build_messages_strips_context_form(self) -> None:
        """compression_messages 与历史消息的 _context_form 都不发给最终 LLM。"""
        state = {
            "system_message": {"role": "system", "content": "系统提示"},
            "compression_messages": [
                {
                    "role": "system",
                    "name": "compressed",
                    "content": "<compressed>摘要</compressed>",
                    "_context_form": "recall",
                },
                {
                    "role": "system",
                    "name": "state_snapshot",
                    "content": "<current_state>状态</current_state>",
                    "_context_form": "snapshot",
                },
            ],
            "messages": [
                {"role": "user", "content": "历史消息", "seq": 7, "_context_form": "notice"},
            ],
        }
        built = self._build(state)

        # 所有发出消息都不含 _context_form / seq
        for m in built:
            assert "_context_form" not in m, f"内部字段 _context_form 泄漏: {m}"
            assert "seq" not in m, f"内部字段 seq 泄漏: {m}"
        # 内容本体保留（清理字段不丢内容）
        contents = [m.get("content", "") for m in built]
        assert any("<compressed>摘要</compressed>" in c for c in contents)
        assert any("历史消息" in c for c in contents)


class TestMemoryReadTagsRecall:
    """memory_read 检索结果条目打 _context_form="recall"。"""

    def test_normalize_results_tags_recall(self) -> None:
        mod = _load_memory_read()
        sample = {
            "id": "m1",
            "content": "记忆内容1",
            "score": 0.95,
            "memory_type": "semantic",
            "metadata": {"tags": []},
        }
        normalized = mod.MemoryReadPlugin._normalize_results([sample])

        assert normalized, "应归一化出结果"
        assert normalized[0]["_context_form"] == "recall"
        # 原有字段原样保留
        assert normalized[0]["content"] == "记忆内容1"
        assert normalized[0]["id"] == "m1"


# ═══════════════════════════════════════════════════════════
# 任务 2：fork 消息队列压缩（对标 DSH summarizer，产物结构不变）
# ═══════════════════════════════════════════════════════════


class TestForkCompressionCall:
    """compress_all 改为 fork 消息队列 + 末尾追加压缩指令。"""

    def test_compress_all_sends_message_list(self) -> None:
        """压缩调用收到消息列表（非字符串）；fork = [system] + 待压缩消息
        （含既有压缩块消息——它们排在消息序列里，原样 role/content）+
        [user 压缩指令]。"""
        mod = _load_cwg()
        captured: list[Any] = []

        async def fake_llm(payload: Any) -> str:
            captured.append(payload)
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {
                "role": "system",
                "name": "compressed",
                "content": "<compressed>L2 三元组</compressed>",
                "_context_form": "recall",
                "seq": 1,
            },
            {"role": "user", "content": "请帮我做 X", "seq": 2},
            {"role": "assistant", "content": "好的，开始", "seq": 3},
        ]
        result = _run(
            compressor.compress_all(
                messages,
                system_message={"role": "system", "content": "执行时系统提示"},
            )
        )

        assert result is not None
        assert len(captured) == 1, "应恰好调用一次 LLM"
        fork = captured[0]
        assert isinstance(fork, list), "压缩调用应发消息列表，不是单字符串"

        # fork[0]：执行时 system（原样）
        assert fork[0]["role"] == "system"
        assert fork[0]["content"] == "执行时系统提示"
        # fork[1]：既有压缩块消息在消息流原位（渲染 [recall] 语义标签前缀）
        assert "<compressed>L2 三元组</compressed>" in fork[1]["content"]
        assert fork[1]["content"].startswith("[recall] ")
        # fork[2:4]：待压缩消息原样 role/content（不压成 【用户 N】），seq 已剥离
        assert fork[2] == {"role": "user", "content": "请帮我做 X"}
        assert fork[3] == {"role": "assistant", "content": "好的，开始"}
        # fork[-1]：末尾追加 user 角色压缩指令
        assert fork[-1]["role"] == "user"
        assert "l1" in fork[-1]["content"]
        assert "memory_items" in fork[-1]["content"]

    def test_fork_renders_semantic_labels(self) -> None:
        """带 _context_form 的消息在 fork 中渲染 [form] 内容前缀（任务 1+2 叠加生效）。"""
        mod = _load_cwg()
        captured: list[Any] = []

        async def fake_llm(payload: Any) -> str:
            captured.append(payload)
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {"role": "user", "content": "项目规则：必须用中文", "_context_form": "instructions"},
            {"role": "user", "content": "任务已完成的通知", "_context_form": "notice"},
            {"role": "user", "content": "普通消息"},
        ]
        _run(compressor.compress_all(messages))

        fork = captured[0]
        contents = [m.get("content", "") for m in fork]
        assert any(c.startswith("[instructions] 项目规则") for c in contents)
        assert any(c.startswith("[notice] 任务已完成") for c in contents)
        # 无标记消息不加前缀
        assert "普通消息" in contents
        # 渲染后 _context_form 字段本身不进载荷
        assert all("_context_form" not in m for m in fork)

    def test_fork_without_context_messages(self) -> None:
        """无 system_message/compression_messages 时 fork 直接以待压缩消息开头。"""
        mod = _load_cwg()
        captured: list[Any] = []

        async def fake_llm(payload: Any) -> str:
            captured.append(payload)
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [{"role": "user", "content": "hi"}]
        _run(compressor.compress_all(messages))

        fork = captured[0]
        assert fork[0] == {"role": "user", "content": "hi"}
        assert fork[-1]["role"] == "user"

    def test_no_format_flattening(self) -> None:
        """fork 中不出现 【用户 N】 压扁格式（_format_messages 不再用于压缩输入）。"""
        mod = _load_cwg()
        captured: list[Any] = []

        async def fake_llm(payload: Any) -> str:
            captured.append(payload)
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        _run(
            compressor.compress_all(
                [{"role": "user", "content": "内容甲"}, {"role": "assistant", "content": "内容乙"}]
            )
        )
        assert isinstance(captured[0], list)
        for m in captured[0]:
            assert "【用户" not in str(m.get("content", ""))
            assert "【助手" not in str(m.get("content", ""))

    def test_output_structure_unchanged(self) -> None:
        """产物仍是五段结构 {l1, l2, keywords, state_snapshot, memory_items}（结构不变）。"""
        mod = _load_cwg()

        async def fake_llm(payload: Any) -> str:
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        result = _run(
            compressor.compress_all(
                [{"role": "user", "content": "做 X"}],
                system_message={"role": "system", "content": "S"},
            )
        )
        assert result is not None
        assert set(result.keys()) == {"l1", "l2", "keywords", "state_snapshot", "memory_items"}
        assert result["keywords"] == ["关键词1", "关键词2"]


class TestCompactionInstruction:
    def test_instruction_requires_five_part_json(self) -> None:
        """COMPACTION_INSTRUCTION 要求五段 JSON；占位符 {messages}/{state_snapshot}/
        {recent_process_blocks} 已删（信息改由 fork 消息流承载）。"""
        mod = _load_cwg()
        instr = mod.ContextCompressor.COMPACTION_INSTRUCTION
        for key in ("l1", "l2", "keywords", "state_snapshot", "memory_items"):
            assert f'"{key}"' in instr, f"指令应要求产出 {key}"
        # 旧占位符已删（去冗余：内容已在 fork 消息流里）
        for ph in ("{messages}", "{state_snapshot}", "{recent_process_blocks}"):
            assert ph not in instr, f"占位符 {ph} 应已删除"
        # 语义标签 legend（任务 1 联动：压缩 LLM 据此分配摘要权重）
        assert "[instructions]" in instr
        assert "[notice]" in instr

    def test_instruction_mentions_prior_compressed_blocks(self) -> None:
        """指令说明 <compressed>/<current_state> 是此前压缩产物，state_snapshot 合并更新。"""
        mod = _load_cwg()
        instr = mod.ContextCompressor.COMPACTION_INSTRUCTION
        assert "<compressed>" in instr
        assert "<current_state>" in instr


class TestCapabilityFallbackFlattens:
    def test_fallback_passes_message_list(self) -> None:
        """capability 路径（llm.complete_stream）把消息列表原样透传。"""
        mod = _load_cwg()
        mod.set_capability_caller(None)

        captured: dict[str, Any] = {}

        async def _caller(method: str, params: dict, timeout: float | None = None) -> Any:
            captured.update(params)
            return {"success": True, "data": {"text": "回退摘要", "finish_reason": "stop"}}

        fn = mod._build_compress_llm_call_fn(_caller)
        payload = [
            {"role": "system", "content": "系统提示", "_context_form": None},
            {"role": "user", "content": "用户内容"},
        ]
        result = _run(fn(payload))

        assert result == "回退摘要"
        assert captured["tool_name"] == "llm.complete_stream"
        assert captured["plugin_id"] == "llm_service"
        assert captured["args"]["messages"] == payload


class TestCompressionServiceForkThreading:
    """CompressionService 把 system_message/compression_messages 传进 fork。"""

    def test_compress_messages_threads_fork_context(self) -> None:
        mod = _load_cwg()
        captured: list[Any] = []

        async def fake_llm(payload: Any) -> str:
            captured.append(payload)
            return _valid_compress_json()

        # recent 预算极小 → 首条消息即进待压缩区
        config = mod.CompressionConfig(
            context_window=1000,
            compress_trigger_ratio=0.99,
            recent_ratio=0.01,
            l1_ratio=0.1,
            l2_ratio=0.05,
        )
        backend = FakeBackend()
        service = mod.CompressionService(backend=backend, llm_call_fn=fake_llm, config=config)
        service.setup(pipeline_id="pipe-1", session_id="s-1", user_id="u-1")

        # 既有快照块消息排在消息序列头部（压缩块消息化：随 messages 进入 fork 前缀）
        messages = [
            {
                "role": "system",
                "name": "state_snapshot",
                "content": "<current_state>已有状态</current_state>",
                "_context_form": "snapshot",
                "seq": 1,
                "metadata": {
                    "compression_ref": {"kind": "state_snapshot", "memory_ids": []}
                },
            },
            {"role": "user", "content": "A" * 400, "seq": 2},
            {"role": "assistant", "content": "B" * 400, "seq": 3},
        ]
        result = _run(
            service.compress_messages(
                messages,
                context_window=1000,
                trigger_ratio=0.99,
                system_message={"role": "system", "content": "执行时系统提示"},
            )
        )

        assert result is not None, "应完成压缩"
        assert captured, "应产生压缩 LLM 调用"
        fork = captured[0]
        assert isinstance(fork, list)
        # system + 既有压缩块消息前缀进入 fork
        assert fork[0]["role"] == "system"
        assert fork[0]["content"] == "执行时系统提示"
        assert "<current_state>已有状态</current_state>" in fork[1]["content"]
        # 语义标签渲染（snapshot）
        assert fork[1]["content"].startswith("[snapshot] ")
        # 待压缩消息原样进入（seq 剥离）
        assert {"role": "user", "content": "A" * 400} in fork
        # 末尾指令
        assert fork[-1]["role"] == "user"
        # 落库结构不变（chunk/semantic）
        memory_types = [c["memory_type"] for c in backend.add_calls]
        assert "chunk" in memory_types
        assert "semantic" in memory_types
