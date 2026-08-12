"""context_window_guard plugin TDD 测试（Step 4 重建）。

验证内容（与任务规格 10 个用例对齐）：
1. test_init_does_not_crash —— ContextWindowGuardPlugin() 构造不导入 memory 模块
2. test_resolve_trigger_ratio_falls_back —— _resolve_trigger_ratio(None) 不导入 memory.context_compressor
3. test_compression_config_from_yaml —— 本地 CompressionConfig.from_yaml_config 返回有效预算
4. test_compress_all_produces_json —— mock llm_call_fn 返回合法 JSON，compress_all 解析成 5 部分
5. test_compress_all_returns_none_on_empty —— 空消息 → 空结果字典
6. test_compress_all_returns_none_on_bad_json —— LLM 返回乱码 → None
7. test_format_messages_roles —— _format_messages 产出 【用户】【助手】【系统】【工具】 头
8. test_save_uses_memory_backend —— 压缩后 backend.add 对 L1 用 memory_type="chunk"，
   对 memory_items 用 memory_type="semantic"
9. test_trim_covered_messages_uses_backend —— mock backend 返回带 sequence_end 的 chunk，
   断言 messages 被裁剪
10. test_get_memory_service_returns_none_without_deps —— 无 capability/backend → None，不崩

测试不依赖真实 LLM/能力后端——通过 AsyncMock 注入 llm_call_fn / IMemoryBackend。

[来源: docs/tasks Step 4 上下文窗口守卫插件重建]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + pipeline 包加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，避免模块级全局状态
    （_memory_backend / _capability_caller）在测试间互相污染。
    """
    mod_name = "context_window_guard_plugin_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    # 若旧缓存存在，先清掉，保证拿到全新模块实例
    if mod_name in sys.modules:
        del sys.modules[mod_name]
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
    """记录 add/search 调用的伪 IMemoryBackend（duck-typed，无需继承 ABC）。"""

    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.search_returns: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        return f"mem-{len(self.add_calls)}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.search_returns)


# ═══════════════════════════════════════════════════════════
# 1. 构造不导入 memory 模块
# ═══════════════════════════════════════════════════════════


class TestInit:
    def test_init_does_not_crash(self) -> None:
        """ContextWindowGuardPlugin() 构造成功，且不导入不存在的 memory 模块。"""
        mod = _load_plugin_module()
        # 构造前清掉任何可能的 memory 残留，确保构造不依赖它
        for k in list(sys.modules):
            if k == "memory" or k.startswith("memory."):
                if "context_compressor" in k or "memory_context_service" in k:
                    del sys.modules[k]
        plugin = mod.ContextWindowGuardPlugin()
        assert plugin.name == "context_window_guard"
        # 构造后不应存在 memory.memory_context_service 模块（0.2 中不存在）
        assert "memory.memory_context_service" not in sys.modules


# ═══════════════════════════════════════════════════════════
# 2. _resolve_trigger_ratio 回退
# ═══════════════════════════════════════════════════════════


class TestResolveTriggerRatio:
    def test_resolve_trigger_ratio_falls_back(self) -> None:
        """_resolve_trigger_ratio(None) 不导入 memory.context_compressor，返回 0.55。

        即使 yaml 读不到，代码默认也应为 0.55。
        """
        mod = _load_plugin_module()
        # 确保不存在的模块确实不存在
        sys.modules.pop("memory.context_compressor", None)
        ratio = mod.ContextWindowGuardPlugin._resolve_trigger_ratio(None)
        assert ratio == 0.55
        # 不应导入 memory.context_compressor
        assert "memory.context_compressor" not in sys.modules


# ═══════════════════════════════════════════════════════════
# 3. CompressionConfig.from_yaml_config
# ═══════════════════════════════════════════════════════════


class TestCompressionConfig:
    def test_compression_config_from_yaml(self) -> None:
        """本地 CompressionConfig.from_yaml_config(128000) 返回有效预算。"""
        mod = _load_plugin_module()
        cfg = mod.CompressionConfig.from_yaml_config(128000)
        budgets = cfg.get_budgets()
        # recent/L1/L2 预算都应是正数
        assert budgets["recent"] > 0, "recent budget should be positive"
        assert budgets["L1"] > 0, "L1 budget should be positive"
        assert budgets["L2"] > 0, "L2 budget should be positive"
        # 预算与 context_window 成比例：recent = 128000 * 0.18 = 23040
        assert budgets["recent"] == int(128000 * 0.18)
        assert budgets["L1"] == int(128000 * 0.1)
        assert budgets["L2"] == int(128000 * 0.05)
        # trigger threshold
        assert cfg.get_trigger_threshold() == int(128000 * 0.55)


# ═══════════════════════════════════════════════════════════
# 4. compress_all 正常路径
# ═══════════════════════════════════════════════════════════


class TestCompressAllHappy:
    def test_compress_all_produces_json(self) -> None:
        """mock llm_call_fn 返回合法 5 部分 JSON，compress_all 解析成对应字段。"""
        mod = _load_plugin_module()

        async def fake_llm(prompt: str) -> str:
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {"role": "user", "content": "请帮我做 X"},
            {"role": "assistant", "content": "好的，开始"},
        ]
        result = _run(
            compressor.compress_all(messages, state_snapshot="", recent_process_blocks="")
        )
        assert result is not None
        assert "l1" in result and result["l1"]
        assert "l2" in result and result["l2"]
        assert isinstance(result["keywords"], list)
        assert len(result["keywords"]) == 2
        assert isinstance(result["state_snapshot"], dict)
        assert isinstance(result["memory_items"], dict)
        assert result["memory_items"]["user_profile_updates"] == "偏好 P"


# ═══════════════════════════════════════════════════════════
# 5. compress_all 空消息
# ═══════════════════════════════════════════════════════════


class TestCompressAllEmpty:
    def test_compress_all_returns_none_on_empty(self) -> None:
        """空消息列表 → 返回空结果字典（不调用 LLM）。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor(llm_call_fn=AsyncMock(return_value="x"))
        result = _run(compressor.compress_all([], state_snapshot="", recent_process_blocks=""))
        # 空消息返回的是空结果字典（含 l1/l2/keywords 等键，值为空），不是 None
        assert result is not None
        assert result.get("l1") == ""
        assert result.get("l2") == ""


# ═══════════════════════════════════════════════════════════
# 6. compress_all JSON 解析失败
# ═══════════════════════════════════════════════════════════


class TestCompressAllBadJson:
    def test_compress_all_returns_none_on_bad_json(self) -> None:
        """LLM 返回乱码（无 JSON）→ compress_all 返回 None。"""
        mod = _load_plugin_module()

        async def bad_llm(prompt: str) -> str:
            return "这不是 JSON，完全无法解析的乱码文本 (((( "

        compressor = mod.ContextCompressor(llm_call_fn=bad_llm)
        messages = [{"role": "user", "content": "做一些事"}]
        result = _run(compressor.compress_all(messages, state_snapshot="", recent_process_blocks=""))
        assert result is None


# ═══════════════════════════════════════════════════════════
# 7. _format_messages 角色头
# ═══════════════════════════════════════════════════════════


class TestFormatMessages:
    def test_format_messages_roles(self) -> None:
        """_format_messages 产出 【用户】【助手】【系统】【工具】 头。"""
        mod = _load_plugin_module()
        compressor = mod.ContextCompressor()
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "用户输入"},
            {"role": "assistant", "content": "助手回复"},
            {"role": "tool", "content": "工具结果", "name": "search"},
        ]
        text = compressor._format_messages(messages)
        assert "【用户" in text
        assert "【助手" in text
        assert "【系统" in text
        assert "【工具" in text
        assert "search" in text  # 工具名出现在头里


# ═══════════════════════════════════════════════════════════
# 8. 压缩结果落库到 memory backend
# ═══════════════════════════════════════════════════════════


class TestSaveUsesBackend:
    def test_save_uses_memory_backend(self) -> None:
        """压缩成功后：L1/L2/STATE_SNAPSHOT 以 memory_type='chunk' 入库，
        memory_items 以 memory_type='semantic' 入库。"""
        mod = _load_plugin_module()
        backend = FakeBackend()

        async def fake_llm(prompt: str) -> str:
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {"role": "user", "content": "做 X", "_record_sequence": 1},
            {"role": "assistant", "content": "好的", "_record_sequence": 2},
        ]
        result = _run(
            compressor.compress_all(messages, state_snapshot="", recent_process_blocks="")
        )
        assert result is not None

        # 调用插件级落库函数
        saver = mod.CompressionService(backend=backend)
        _run(
            saver.save_compression_result(
                old_msgs=messages,
                comp_result=result,
                pipeline_id="pipe-1",
                session_id="sess-1",
                context_window=128000,
            )
        )

        memory_types = [c["memory_type"] for c in backend.add_calls]
        # 至少有 chunk（L1）和 semantic（memory_items）两类
        assert "chunk" in memory_types, "L1 块应以 memory_type='chunk' 入库"
        assert "semantic" in memory_types, "memory_items 应以 memory_type='semantic' 入库"


# ═══════════════════════════════════════════════════════════
# 9. _trim_covered_messages 走 backend
# ═══════════════════════════════════════════════════════════


class TestTrimUsesBackend:
    def test_trim_covered_messages_uses_backend(self) -> None:
        """mock backend 返回带 sequence_end 的 L1 chunk，断言 messages 被裁剪。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        # backend 返回一条 L1 chunk，sequence_end=5。
        # 注意：sequence 信息放在 metadata.tags（"seq:1-5"）里——这正是
        # save_compression_result 通过 backend.add(tags=[...]) 写入、
        # 以及 HindsightBackend 原样存入 metadata.tags 的统一形态，
        # 与 _parse_seq_from_tags 的解析契约一致。
        backend.search_returns = [
            {
                "id": "c1",
                "content": "已有摘要",
                "score": 1.0,
                "memory_type": "chunk",
                "metadata": {
                    "tags": ["L1", "pipeline:pipe-1", "seq:1-5"],
                },
            }
        ]
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx(pipeline_id="pipe-1")
        # seq 1-5 已被压缩（应裁），seq 6-8 是 recent（应保留，>10% 防护不触发）
        messages: list[dict[str, Any]] = []
        for i in range(1, 6):
            messages.append({"role": "user", "content": f"msg{i}", "_record_sequence": i})
        for i in range(6, 9):
            messages.append({"role": "assistant", "content": f"recent{i}", "_record_sequence": i})

        result = _run(plugin._trim_covered_messages(ctx, messages))
        kept_seqs = [
            m["_record_sequence"]
            for m in result
            if m.get("role") != "system" and isinstance(m.get("_record_sequence"), int)
        ]
        # recent 段 6,7,8 保留，1-5 被裁
        assert 6 in kept_seqs and 8 in kept_seqs
        assert 1 not in kept_seqs and 5 not in kept_seqs


# ═══════════════════════════════════════════════════════════
# 10. _get_memory_service 无依赖返回 None
# ═══════════════════════════════════════════════════════════


class TestGetMemoryServiceNoDeps:
    def test_get_memory_service_returns_none_without_deps(self) -> None:
        """无 capability/backend 设置 → _get_memory_service 返回 None，不崩溃。"""
        mod = _load_plugin_module()
        # 确保模块级 backend/capability 都是 None
        mod._memory_backend = None
        mod._capability_caller = None
        plugin = mod.ContextWindowGuardPlugin()
        ctx = mod._make_minimal_ctx()
        # ctx.get_service 会抛 KeyError（无服务注册）
        service = plugin._get_memory_service(ctx)
        assert service is None
