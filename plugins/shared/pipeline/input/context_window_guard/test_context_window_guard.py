# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
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

        async def fake_llm(payload: list) -> str:
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {"role": "user", "content": "请帮我做 X"},
            {"role": "assistant", "content": "好的，开始"},
        ]
        result = _run(
            compressor.compress_all(messages)
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
        result = _run(compressor.compress_all([]))
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

        async def bad_llm(payload: list) -> str:
            return "这不是 JSON，完全无法解析的乱码文本 (((( "

        compressor = mod.ContextCompressor(llm_call_fn=bad_llm)
        messages = [{"role": "user", "content": "做一些事"}]
        result = _run(compressor.compress_all(messages))
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

        async def fake_llm(payload: list) -> str:
            return _valid_compress_json()

        compressor = mod.ContextCompressor(llm_call_fn=fake_llm)
        messages = [
            {"role": "user", "content": "做 X", "seq": 1},
            {"role": "assistant", "content": "好的", "seq": 2},
        ]
        result = _run(
            compressor.compress_all(messages)
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
            messages.append({"role": "user", "content": f"msg{i}", "seq": i})
        for i in range(6, 9):
            messages.append({"role": "assistant", "content": f"recent{i}", "seq": i})

        result = _run(plugin._trim_covered_messages(ctx, messages))
        kept_seqs = [
            m["seq"]
            for m in result
            if m.get("role") != "system" and isinstance(m.get("seq"), int)
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


# ═══════════════════════════════════════════════════════════
# 11-14. op 模式迁移：state_updates["messages"] 形如 {"_ops": [...]}
#     （任务 2：context_window_guard 迁移 op 模式）
# ═══════════════════════════════════════════════════════════


def _ops_by_seq(state_updates: SimpleNamespace | Any) -> dict[int, dict[str, Any]]:
    """从 PluginResult.state_updates 提取 messages._ops，按 seq 索引成字典。

    若 messages 不是 _ops 形态（旧全量数组），返回空字典便于断言区分。
    """
    messages = state_updates.get("messages")
    if not isinstance(messages, dict) or "_ops" not in messages:
        return {}
    return {op["seq"]: op for op in messages["_ops"] if isinstance(op, dict) and "seq" in op}


class TestOpModeEmission:
    """op 模式迁移测试：插件 emit {"_ops":[...]} 而非全量数组。

    覆盖四个场景：压缩 / 裁剪 / 窗口清理 / 未触发。
    """

    def test_compress_emits_set_null_and_set_modify_ops(self) -> None:
        """压缩场景：被删消息 set(seq, null)；被 standardize 改写的幸存消息
        set(seq, 新内容)；未动消息无 op。

        mock service.compress_messages 返回压缩子集（删 seq 2,3），
        其中幸存的 seq4 assistant 带非标准 tool_calls（会被 normalizer 改写）。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})

        messages = [
            {"role": "system", "content": "S" * 300, "seq": 1},
            {"role": "user", "content": "U" * 300, "seq": 2},
            {"role": "user", "content": "V" * 300, "seq": 3},
            {
                "role": "assistant",
                "content": "",
                "seq": 4,
                "tool_calls": [{"id": "bad_id", "name": "search", "args": "{}"}],
            },
        ]
        # service 返回的压缩结果：保留 seq1(system) 与 seq4(assistant)，删 2,3
        survivor = {
            "role": "assistant",
            "content": "",
            "seq": 4,
            "tool_calls": [{"id": "bad_id", "name": "search", "args": "{}"}],
        }
        compressed = [
            {"role": "system", "content": "S" * 300, "seq": 1},
            survivor,
        ]
        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=compressed)
        mock_service._last_deleted_seqs = [2, 3]

        ctx = mod._make_minimal_ctx(
            state={"context_window": 200, "messages": messages},
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        ops = _ops_by_seq(result.state_updates)

        # messages 必须是 _ops 形态
        assert isinstance(result.state_updates.get("messages"), dict)
        assert "_ops" in result.state_updates["messages"]

        # 被删 seq 2,3 → set(seq, null)
        assert ops[2] == {"op": "set", "seq": 2, "msg": None}
        assert ops[3] == {"op": "set", "seq": 3, "msg": None}
        # 幸存但被 standardize 改写的 seq4 → set(seq, 新内容)，新内容含标准 tool_calls
        assert 4 in ops
        assert ops[4]["op"] == "set"
        new_msg = ops[4]["msg"]
        assert new_msg is not None
        assert new_msg["role"] == "assistant"
        tc = new_msg["tool_calls"][0]
        assert tc["type"] == "function"
        assert isinstance(tc["function"], dict)
        assert tc["function"]["name"] == "search"
        assert tc["id"].startswith("call_")
        # 未动的 seq1（system）无 op
        assert 1 not in ops
        # 不应有多余 op
        assert set(ops.keys()) == {2, 3, 4}

    def test_trim_emits_set_null_for_dropped_seqs(self) -> None:
        """裁剪场景（_trim_covered_messages）：dropped 的 seq 都是 set(seq, null)。

        backend 返回 L1 chunk（sequence_end=50），60 条消息中 seq 1-50 被覆盖裁掉，
        seq 51-60 保留（>10% 防护不触发）。阈值之下不压缩。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        backend = FakeBackend()
        backend.search_returns = [
            {
                "id": "c1",
                "content": "已有摘要",
                "score": 1.0,
                "memory_type": "chunk",
                "metadata": {"tags": ["L1", "pipeline:pipe-1", "seq:1-50"]},
            }
        ]
        mod.set_memory_backend(backend)

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})

        messages: list[dict[str, Any]] = []
        for i in range(1, 61):
            messages.append({"role": "user", "content": f"m{i}", "seq": i})

        # 注入一个 service 占位（让 _get_memory_service 不早退），但不会触发压缩
        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)

        ctx = mod._make_minimal_ctx(
            state={"context_window": 128000, "messages": messages},
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        ops = _ops_by_seq(result.state_updates)

        # messages 是 _ops 形态
        assert "_ops" in result.state_updates["messages"]
        # dropped seq 1-50 都是 set(seq, null)
        for s in (1, 25, 50):
            assert ops[s] == {"op": "set", "seq": s, "msg": None}
        # 保留的 seq 51-60 没有 op
        for s in (51, 55, 60):
            assert s not in ops
        # op 总数 = 50
        assert len(result.state_updates["messages"]["_ops"]) == 50

    def test_clean_emits_set_null_for_old_summary(self) -> None:
        """窗口清理场景（clean_if_window_changed）：被删旧摘要消息 set(seq, null)。

        backend 返回的 L1 块 ctx 标签=64000，当前 context_window=128000（变更），
        clean 移除旧压缩摘要 system 消息（seq1）。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        backend = FakeBackend()
        backend.search_returns = [
            {
                "id": "c1",
                "content": "## 历史对话压缩摘要 ...",
                "score": 1.0,
                "memory_type": "chunk",
                "metadata": {
                    "tags": ["L1", "pipeline:pipe-1", "seq:1-5", "ctx:64000"]
                },
            }
        ]
        mod.set_memory_backend(backend)

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})

        messages = [
            {
                "role": "system",
                "content": "## 历史对话压缩摘要 旧窗口产物",
                "seq": 1,
            },
            {"role": "system", "content": "正常系统提示", "seq": 2},
            {"role": "user", "content": "hello", "seq": 3},
        ]

        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)

        ctx = mod._make_minimal_ctx(
            state={"context_window": 128000, "messages": messages},
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        ops = _ops_by_seq(result.state_updates)

        # messages 是 _ops 形态
        assert "_ops" in result.state_updates["messages"]
        # 旧摘要 seq1 被删 → set(1, null)
        assert ops[1] == {"op": "set", "seq": 1, "msg": None}
        # 正常 system seq2 与 user seq3 保留，无 op
        assert 2 not in ops
        assert 3 not in ops
        assert len(result.state_updates["messages"]["_ops"]) == 1

    def test_not_triggered_has_no_messages_key(self) -> None:
        """未触发场景：既无 trim 也无 clean 也无压缩时，state_updates 不含 messages key。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})

        messages = [{"role": "user", "content": "hi", "seq": 1}]

        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)

        ctx = mod._make_minimal_ctx(
            state={"context_window": 128000, "messages": messages},
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        # 没有 clean/trim/压缩 → 不 emit messages key（仅 _tracked_msg_count）
        assert "messages" not in result.state_updates


# ═══════════════════════════════════════════════════════════
# 预算配置回退/记忆后端检索可观测（兜底反模式审查 P12/P13，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestFallbackObservability:
    def test_from_yaml_config_failure_warns(self, caplog, monkeypatch) -> None:
        """P12：预算配置读取失败回退代码默认必须 warning 留痕。"""
        import logging

        mod = _load_plugin_module()
        monkeypatch.setitem(sys.modules, "config.config_center", None)
        with caplog.at_level(logging.WARNING):
            cfg = mod.CompressionConfig.from_yaml_config(128000)
        assert cfg.context_window == 128000, "回退代码默认（行为保持）"
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("压缩预算配置读取失败" in m for m in msgs)
        assert any("context_window_config.yaml" in m for m in msgs)

    def _make_plugin(self, mod) -> Any:
        return mod.ContextWindowGuardPlugin(config={})

    def test_memory_backend_search_failure_warns(self, caplog, monkeypatch) -> None:
        """P13：记忆后端检索异常按无历史处理但必须 warning（三处之一回归）。"""
        import logging

        mod = _load_plugin_module()

        class BoomBackend:
            async def search(self, **kwargs):
                raise RuntimeError("backend down")

        monkeypatch.setattr(mod, "_memory_backend", BoomBackend())
        plugin = self._make_plugin(mod)
        ctx = mod.PluginContext(
            state={mod.StateKeys.PIPELINE_ID: "pipe-p13", "user_id": "u1", "messages": []},
            config={},
        )
        with caplog.at_level(logging.WARNING):
            messages = asyncio.run(plugin._trim_covered_messages(ctx, []))
        assert messages == [], "检索失败按无历史处理（行为保持）"
        assert any("记忆后端检索失败" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# 11. 保存失败 fail-closed：该批原消息不计入删除
# ═══════════════════════════════════════════════════════════


class _FlakyBackend:
    """指定序号的 add 调用抛错、其余照常记录的伪 IMemoryBackend。"""

    def __init__(self, fail_calls: set[int]) -> None:
        self.add_count = 0
        self.fail_calls = fail_calls
        self.successful_adds: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> str:
        self.add_count += 1
        if self.add_count in self.fail_calls:
            raise RuntimeError("backend write boom")
        self.successful_adds.append(kwargs)
        return f"mem-{self.add_count}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _saved_seqs_from_backend(backend: _FlakyBackend) -> set[int]:
    """从成功落库的 chunk tags（"seq:a-b"）还原已获摘要覆盖的消息 seq 集合。"""
    seqs: set[int] = set()
    for call in backend.successful_adds:
        if call.get("memory_type") != "chunk":
            continue
        for tag in call.get("tags", []):
            if isinstance(tag, str) and tag.startswith("seq:") and "-" in tag[4:]:
                a, b = tag[4:].split("-", 1)
                seqs.update(range(int(a), int(b) + 1))
    return seqs


def _round_messages() -> list[dict[str, Any]]:
    """4 条各约 200 token 的消息（seq 1-4）。

    recent 预算 300 → 尾部仅 m4 留守，old = [m1,m2,m3]；
    context_window=800 → 批预算 400 → 两批：b0=[m1]，b1=[m2,m3]。
    """
    roles = ["user", "assistant", "user", "assistant"]
    return [
        {"role": roles[i], "content": "A" * 400, "seq": i + 1} for i in range(4)
    ]


class TestSaveFailureKeepsMessages:
    def _service(self, mod: Any, backend: _FlakyBackend) -> Any:
        async def fake_llm(payload: list) -> str:
            return _valid_compress_json()

        svc = mod.CompressionService(backend=backend, llm_call_fn=fake_llm)
        svc.setup(pipeline_id="pipe-sf", session_id="sess-sf")
        return svc

    def _run_round(self, mod: Any, backend: _FlakyBackend) -> Any:
        svc = self._service(mod, backend)
        return mod.CompressionService._do_compress_round(
            svc,
            _round_messages(),
            800,
            {"recent": 300},
        )

    def test_all_saves_fail_deletes_nothing(self) -> None:
        """全部批次落库失败 → 整轮返回 None，不产生任何删除。"""
        result = _run(self._run_round(_load_plugin_module(), _FlakyBackend(set(range(1, 100)))))
        assert result is None, "摘要未落库时不得返回删除列表"

    def test_partial_save_failure_keeps_failed_batch(self) -> None:
        """首个保存调用失败（b0 整批失败）、b1 成功：

        - 删除列表只含 b1 覆盖的 seq；
        - 失败批次的原消息按原文保留在返回消息里。
        """
        backend = _FlakyBackend({1})
        mod = _load_plugin_module()
        result = _run(self._run_round(mod, backend))
        assert result is not None, "任一批次成功即应产出压缩结果"
        _, deleted = result

        # 上游用 deleted_seqs emit set(seq,null)；未删的 seq 在引擎存储中
        # 原样留存——这就是失败批次"原消息保留"的可观察出口。
        saved = _saved_seqs_from_backend(backend)
        assert saved == {2, 3}, f"成功批次的覆盖范围应可从落库 tags 还原: {saved}"
        assert sorted(deleted) == [2, 3], (
            f"删除列表必须只含落库成功的批次，实际: {deleted}"
        )
        # fail-closed 性质：删除集合 ⊆ 成功摘要覆盖集合（无摘要背书的 seq 不得被删）
        assert set(deleted) <= saved
        assert 1 not in deleted, "落库失败的批次（seq=1）不得进入删除列表"

    def test_all_saves_succeed_baseline(self) -> None:
        """对照基线：全部落库成功 → 全部 old seq 进入删除列表。"""
        backend = _FlakyBackend(set())
        result = _run(self._run_round(_load_plugin_module(), backend))
        assert result is not None
        _, deleted = result
        assert sorted(deleted) == [1, 2, 3]
