# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""context_window_guard plugin TDD 测试（Step 4 重建 + 2026-11-16 裁剪修复）。

验证内容（与任务规格 10 个用例对齐，9 号随裁剪证据源修复重写）：
1. test_init_does_not_crash —— ContextWindowGuardPlugin() 构造不导入 memory 模块
2. test_resolve_trigger_ratio_falls_back —— _resolve_trigger_ratio(None) 不导入 memory.context_compressor
3. test_compression_config_from_yaml —— 本地 CompressionConfig.from_yaml_config 返回有效预算
4. test_compress_all_produces_json —— mock llm_call_fn 返回合法 JSON，compress_all 解析成 5 部分
5. test_compress_all_returns_none_on_empty —— 空消息 → 空结果字典
6. test_compress_all_returns_none_on_bad_json —— LLM 返回乱码 → None
7. test_format_messages_roles —— _format_messages 产出 【用户】【助手】【系统】【工具】 头
8. test_save_uses_memory_backend —— 压缩后 backend.add 对 L1 用 memory_type="chunk"，
   对 memory_items 用 memory_type="semantic"
9. trim 契约（2026-11-16 修复）：裁剪证据源 = 历史已应用压缩块
   （metadata.compression_ref.seq_range），不查记忆库——孤儿 chunk（落库后
   回写前被打断的覆盖声明）不再驱动删除；被删原文与 set(seq, null) 同批
   冻结（禁止裸删）；并行落库契约（refs 回填/单批失败隔离/批次不串行）
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

_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/
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


def _applied_block(
    seq_range: list[int],
    *,
    with_seq: int | None = None,
) -> dict[str, Any]:
    """构造一条已应用压缩块消息（_build_batch_blocks 生产形态，seq_range 可注入）。"""
    block: dict[str, Any] = {
        "role": "system",
        "name": "compressed",
        "content": f'<compressed seq="{seq_range[0]}-{seq_range[1]}" level="L1">\n## 过程摘要\nx\n</compressed>',
        "metadata": {
            "compression_ref": {
                "kind": "process",
                "seq_range": list(seq_range),
                "stored_at": "2026-11-16T00:00:00+00:00",
                "memory_ids": [],
            }
        },
    }
    if with_seq is not None:
        block["seq"] = with_seq
    return block


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
# 9. trim 契约（2026-11-16 修复）：证据源 = 历史已应用压缩块
# ═══════════════════════════════════════════════════════════


class TestTrimUsesAppliedBlocks:
    """裁剪证据源 = 历史已应用压缩块（metadata.compression_ref.seq_range）。

    裁剪与记忆库 chunk 完全解耦（孤儿 chunk 无害化）；被删原文与
    set(seq, null) ops 同批冻结（禁止裸删，2026-11-16 装机事故回归）。
    """

    def test_trim_trims_to_applied_blocks_with_freeze(self) -> None:
        """历史含已应用块（seq_range=[1,50]）+ 消息 1..60 → 裁掉 1..50。

        execute 级断言 ops 批次：冻结 op 在批次中且 members 为被删原文全文
        （逐条与输入深度相等）、null ops 覆盖被删 seq、块消息保留不被改写；
        全程 backend=None（新证据源不需要记忆库）。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})
        block = _applied_block([1, 50])
        # seq 1..50 已被块覆盖（裁），seq 51..60 是 recent（保留，>10% 防护不触发）
        messages: list[dict[str, Any]] = [block] + [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        # 块消息无 seq 槽位（覆盖区间头部槽位语义），不与被裁槽位碰撞

        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)

        ctx = mod._make_minimal_ctx(
            state={"context_window": 128000, "messages": messages},
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        ops_list = result.state_updates["messages"]["_ops"]

        # 冻结 op 在 ops 批次中，形态按契约
        freezes = [op for op in ops_list if op.get("op") == "freeze_segment"]
        assert len(freezes) == 1, "被删原文必须同批冻结（禁止裸删）"
        freeze = freezes[0]
        assert freeze["id"].startswith("seg_")
        assert freeze["visible_to"] == "user"
        assert freeze["base_seq"] == 1
        assert freeze["base_len"] == 50
        # members = 被删原文全文，逐条与输入深度相等（升序 = 输入顺序）
        assert freeze["members"] == messages[1:51]

        # null ops 覆盖被删 seq 1..50
        null_ops = [
            op for op in ops_list if op.get("op") == "set" and op.get("msg") is None
        ]
        assert {op["seq"] for op in null_ops} == set(range(1, 51))

        # 块消息保留：无任何 set(seq, 块) 改写 op；批次总 ops = 1 冻结 + 50 null
        assert not [
            op for op in ops_list if op.get("op") == "set" and isinstance(op.get("msg"), dict)
        ]
        assert len(ops_list) == 51

    def test_trim_preserves_applied_block_in_output(self) -> None:
        """列表级：块消息（system）保留在裁剪输出中，仅非 system 被裁。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.ContextWindowGuardPlugin()
        block = _applied_block([1, 50], with_seq=1)
        messages: list[dict[str, Any]] = [block] + [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        result = _run(plugin._trim_covered_messages(messages))
        assert block in result
        kept_non_sys_seqs = [
            m["seq"] for m in result if m.get("role") != "system"
        ]
        assert kept_non_sys_seqs == list(range(51, 61))

    def test_trim_ignores_memory_chunks_even_when_present(self) -> None:
        """记忆库 chunk 与裁剪决策解耦：chunk 只声明覆盖到 20，块证据到 50
        → 裁到 50（不是 20）。孤儿/滞后 chunk 抬高或压低 max_end 均无效。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        backend.search_returns = [
            {
                "id": "c1",
                "content": "滞后 chunk",
                "memory_type": "chunk",
                "metadata": {"tags": ["L1", "pipeline:pipe-1", "seq:1-20"]},
            }
        ]
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        history: list[dict[str, Any]] = [_applied_block([1, 50])] + [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        result = _run(plugin._trim_covered_messages(history))
        kept_non_sys_seqs = [
            m["seq"] for m in result if m.get("role") != "system"
        ]
        assert kept_non_sys_seqs == list(range(51, 61)), (
            "裁剪必须按历史块证据裁到 50，而不是按记忆库 chunk 裁到 20"
        )


class TestTrimEvidenceBoundaries:
    """证据解析边界（fail-closed）：坏 seq_range 不作证据，宁可不裁。"""

    def _trim_result(self, mod: Any, blocks: list[dict[str, Any]]) -> Any:
        plugin = mod.ContextWindowGuardPlugin()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        return _run(plugin._trim_covered_messages(blocks + messages))

    def test_malformed_seq_range_is_not_evidence(self) -> None:
        """seq_range 缺失/非 int/start>end 的块不作为证据 → 零裁剪。"""
        mod = _load_plugin_module()
        cases = [
            # 缺 seq_range
            {
                "role": "system",
                "name": "compressed",
                "content": "x",
                "metadata": {"compression_ref": {"kind": "process", "memory_ids": []}},
            },
            # seq_range 非 int
            _applied_block([1, 50]) | {
                "metadata": {
                    "compression_ref": {
                        "kind": "process",
                        "seq_range": ["1", 50],
                        "memory_ids": [],
                    }
                }
            },
            # start > end
            _applied_block([50, 1]),
            # metadata 无 compression_ref 键 / compression_ref 非 dict
            _applied_block([1, 50]) | {"metadata": {}},
            _applied_block([1, 50]) | {"metadata": {"compression_ref": "not-a-dict"}},
        ]
        for bad_block in cases:
            plugin = mod.ContextWindowGuardPlugin()
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
            ]
            history = [bad_block] + messages
            result = _run(plugin._trim_covered_messages(history))
            assert result == history, (
                f"坏证据必须放弃裁剪: seq_range={bad_block.get('metadata', {}).get('compression_ref', {}).get('seq_range')}"
            )

    def test_multiple_blocks_take_max_end(self) -> None:
        """多块取 seq_range[1] 最大值：块 [1,20] + 块 [1,50] → 裁掉 1..50。"""
        mod = _load_plugin_module()
        result = self._trim_result(mod, [_applied_block([1, 20]), _applied_block([1, 50])])
        kept = [m["seq"] for m in result if m.get("role") != "system"]
        assert kept == list(range(51, 61))

    def test_block_evidence_misaligned_protection_keeps_original(self) -> None:
        """块 seq_range 与实际消息错位（声明覆盖 1..1000，消息只到 60）→
        <10% 防护兜底放弃裁剪，返回原消息。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        history = [_applied_block([1, 1000])] + messages
        result = _run(plugin._trim_covered_messages(history))
        assert result is history, "证据与序列错位必须由 <10% 防护兜底，放弃裁剪"

    def test_trim_freeze_op_none_when_no_deleted_members(self) -> None:
        """被删集合为空（异常形态）→ 不发空 members 冻结 op（防御分支）。"""
        mod = _load_plugin_module()
        msg = {"role": "user", "content": "x", "seq": 1}
        assert mod.ContextWindowGuardPlugin._trim_freeze_op([msg], [dict(msg)]) is None


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

    def test_compress_emits_set_null_ops_only(self) -> None:
        """压缩场景：被删消息 set(seq, null)；幸存消息原样保留（不改写不重复 op）。

        mock service.compress_messages 返回压缩子集（删 seq 2,3），
        其中幸存的 seq4 assistant 带非标准 tool_calls——写回标准化已上移
        llm_service 标准化唯一关卡（2026-09-06 T7），guard 不再复制判定语义，
        幸存消息原样落 state，无额外 op。
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
        # 幸存 seq4 原样保留（含非标准 tool_calls——标准化由 llm_service 唯一
        # 关卡负责），guard 不改写 → 无 op
        assert 4 not in ops
        # 未动的 seq1（system）无 op
        assert 1 not in ops
        # 不应有多余 op
        assert set(ops.keys()) == {2, 3}

    def test_trim_refuses_orphan_chunks_without_applied_block(self) -> None:
        """本事故回归（2026-11-16 装机事故）：仅记忆库裸 chunk、历史无已应用
        块 → 零删除。

        事故形态：save_compression_result 落库后、压缩批次回写前被打断，
        记忆库留下从未真正应用的覆盖声明（chunk tags seq:1-50）；旧裁剪盲信
        该声明裸删 297 条头部消息且不冻结 → blob GC 物理清除不可恢复。
        修复后裁剪证据源 = 历史已应用块，孤儿 chunk 不驱动任何删除 op。
        """
        mod = _load_plugin_module()
        mod._capability_caller = None

        class _RecordingSearchBackend(FakeBackend):
            """记录 search 调用的 FakeBackend（零检索断言用）。"""

            def __init__(self) -> None:
                super().__init__()
                self.search_calls = 0

            async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
                self.search_calls += 1
                return list(self.search_returns)

        backend = _RecordingSearchBackend()
        backend.search_returns = [
            {
                "id": "c1",
                "content": "孤儿覆盖声明",
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

        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)

        ctx = mod._make_minimal_ctx(
            state={"context_window": 128000, "messages": messages},
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))

        # 零删除：state_updates 不含 messages key（孤儿 chunk 不产生任何 op）
        assert "messages" not in result.state_updates

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

    def test_memory_backend_search_failure_warns_in_estimate(self, caplog, monkeypatch) -> None:
        """P13：记忆后端检索异常按无历史处理但必须 warning（trim 已与记忆库
        解耦，回归点迁移到仍检索后端的 _estimate_assembled_tokens）。"""
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
            estimated = asyncio.run(plugin._estimate_assembled_tokens(ctx, []))
        assert estimated == -1, "检索失败按无 L1 预算处理（行为保持）"
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


def _saved_seqs_from_backend(backend: _FlakyBackend | _BatchScopedFlakyBackend) -> set[int]:
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


def _process_blocks(compressed: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """从压缩结果中拣出过程块消息（按块所在 seq 索引）。"""
    return {
        m["seq"]: m
        for m in compressed
        if m.get("metadata", {}).get("compression_ref", {}).get("kind") == "process"
    }


class _BatchScopedFlakyBackend:
    """按批次 seq 标签定点失败的伪记忆后端（并行落库下仍确定性的故障注入）。

    并行落库后各批 save 的 add 调用交错，按调用序号定点失败不再确定；
    改按 chunk 落库 tags 里的批次标记（每批唯一）定点失败：L1/L2 携带
    ``seq:<start>-<end>``、快照携带 ``seq_end:<end>``，一批全量失败需同时
    覆盖两类标记。
    """

    def __init__(self, doomed_tags: set[str]) -> None:
        self.doomed_tags = doomed_tags
        self.successful_adds: list[dict[str, Any]] = []
        self.failed_adds: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> str:
        tags = kwargs.get("tags") or []
        if self.doomed_tags & set(tags):
            self.failed_adds.append(kwargs)
            raise RuntimeError("backend write boom")
        self.successful_adds.append(kwargs)
        return f"mem-{len(self.successful_adds)}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class TestSaveFailureFailOpen:
    """写库失败逐工件降级（ADR 2026-08-28 压缩块消息化 fail-open 语义）：

    块消息以内联摘要照常进入序列（摘要知识随块消息持久化，不再依赖
    落库背书），失败工件的引用留空并 warning，流程不阻塞。
    """

    def _service(self, mod: Any, backend: Any) -> Any:
        async def fake_llm(payload: list) -> str:
            return _valid_compress_json()

        svc = mod.CompressionService(backend=backend, llm_call_fn=fake_llm)
        svc.setup(pipeline_id="pipe-sf", session_id="sess-sf")
        return svc

    def _run_round(self, mod: Any, backend: Any) -> Any:
        svc = self._service(mod, backend)
        return mod.CompressionService._do_compress_round(
            svc,
            _round_messages(),
            800,
            {"recent": 300},
        )

    def test_all_saves_fail_blocks_still_inline(self, caplog: Any) -> None:
        """全部批次落库失败 → 块消息照常产出（仅内联摘要、引用留空），
        被压区间照常替换（fail-open 不阻塞）。"""
        mod = _load_plugin_module()
        result = _run(self._run_round(mod, _FlakyBackend(set(range(1, 100)))))
        assert result is not None, "写库失败不得阻塞压缩流程"
        compressed, deleted = result
        by_seq = {m["seq"]: m for m in compressed if isinstance(m.get("seq"), int)}
        process_blocks = [
            m
            for m in compressed
            if m.get("metadata", {}).get("compression_ref", {}).get("kind") == "process"
        ]
        assert process_blocks, "写库失败仍应产出块消息"
        for block in process_blocks:
            assert block["content"], "内联摘要不得为空"
            assert block["metadata"]["compression_ref"]["memory_ids"] == []
        # 被压区间照常替换（b0 单条批次占 seq 1；b1 占 seq 2,3——两批槽位全被块占用，无留 gap）
        assert deleted == []
        assert "落库失败" in caplog.text

    def test_partial_save_failure_keeps_successful_refs(self, caplog: Any) -> None:
        """b0 全部 chunk 工件写库失败（按批次 seq 标签定点）、b1 全部成功：

        - b0 块消息引用留空、b1 块消息引用指向成功落库工件；
        - 两批的被压区间照常替换（成功批次的覆盖范围可从落库 tags 还原）。

        并行落库后各批 add 调用交错，按调用序号定点失败不再确定，
        改按每批唯一的 ``seq:<start>-<end>`` 标签定点（b0=seq:1-1）。
        """
        backend = _BatchScopedFlakyBackend({"seq:1-1", "seq_end:1"})
        mod = _load_plugin_module()
        result = _run(self._run_round(mod, backend))
        assert result is not None, "任一批次成功即应产出压缩结果"
        compressed, _deleted = result

        saved = _saved_seqs_from_backend(backend)
        assert saved == {2, 3}, f"成功批次的覆盖范围应可从落库 tags 还原: {saved}"

        process_blocks = _process_blocks(compressed)
        assert len(process_blocks) == 2
        refs_by_seq = {
            seq: m["metadata"]["compression_ref"]["memory_ids"]
            for seq, m in process_blocks.items()
        }
        assert refs_by_seq[1] == [], "落库失败批次的块引用应留空"
        assert refs_by_seq[2], "落库成功批次的块应携带引用"
        assert "落库失败" in caplog.text

    def test_all_saves_succeed_baseline(self) -> None:
        """对照基线：全部落库成功 → 所有块携带引用。"""
        backend = _FlakyBackend(set())
        mod = _load_plugin_module()
        result = _run(self._run_round(mod, backend))
        assert result is not None
        compressed, _deleted = result
        process_blocks = [
            m
            for m in compressed
            if m.get("metadata", {}).get("compression_ref", {}).get("kind") == "process"
        ]
        assert len(process_blocks) == 2
        for block in process_blocks:
            assert block["metadata"]["compression_ref"]["memory_ids"], (
                "全部落库成功时块必须携带引用"
            )


class TestParallelBatchSaves:
    """并行落库契约（2026-11-16 修复）：

    每批压缩 LLM 产出后立即后台落库（不阻塞下一批 LLM 调用）；全部批次
    LLM 循环结束后统一 gather 收割再建块；task 内自兜异常（fail-open），
    execute 返回前不留悬挂 task。正确性不依赖落库成败（裁剪证据源 =
    历史已应用块，孤儿 chunk 无害化），并行化是纯性能优化。
    """

    @staticmethod
    def _two_batch_service(mod: Any, backend: Any) -> Any:
        async def fake_llm(payload: list) -> str:
            return _valid_compress_json()

        svc = mod.CompressionService(backend=backend, llm_call_fn=fake_llm)
        svc.setup(pipeline_id="pipe-par", session_id="sess-par")
        return svc

    @staticmethod
    def _chunk_ref_id_by_tag(backend: FakeBackend) -> dict[str, str]:
        """从 add 调用记录还原「层级@seq标签」→ 返回的 memory id 映射。

        同批 L1/L2 共享同一 seq 标签，须按层级标签（L1/L2）二次区分；
        快照块无 seq 区间标签（仅 seq_end），不在映射内。
        """
        out: dict[str, str] = {}
        for i, call in enumerate(backend.add_calls):
            if call.get("memory_type") != "chunk":
                continue
            tags = call.get("tags", [])
            level = "L1" if "L1" in tags else ("L2" if "L2" in tags else None)
            seq_tag = next(
                (
                    t
                    for t in tags
                    if isinstance(t, str) and t.startswith("seq:")
                ),
                None,
            )
            if level and seq_tag:
                out[f"{level}@{seq_tag}"] = f"mem-{i + 1}"
        return out

    def test_refs_backfilled_to_own_batch(self) -> None:
        """(a) refs 正常回填到对应批次块：每批过程块的 L1 引用 id 等于该批
        自己那次 L1 落库调用返回的 id（按 tags 定位，不依赖批次间到达序）。"""
        mod = _load_plugin_module()
        backend = FakeBackend()
        svc = self._two_batch_service(mod, backend)
        result = _run(
            mod.CompressionService._do_compress_round(
                svc, _round_messages(), 800, {"recent": 300}
            )
        )
        assert result is not None
        compressed, _deleted = result
        blocks = _process_blocks(compressed)
        assert set(blocks.keys()) == {1, 2}, "两批各产出一块（b0=[m1], b1=[m2,m3]）"

        mapping = self._chunk_ref_id_by_tag(backend)
        assert set(mapping.keys()) == {"L1@seq:1-1", "L2@seq:1-1", "L1@seq:2-3", "L2@seq:2-3"}
        assert len(set(mapping.values())) == 4, "各次落库调用各有其 id（有区分度）"
        ref_0 = blocks[1]["metadata"]["compression_ref"]["memory_ids"]
        ref_1 = blocks[2]["metadata"]["compression_ref"]["memory_ids"]
        assert next(e for e in ref_0 if e["level"] == "L1")["id"] == mapping["L1@seq:1-1"]
        assert next(e for e in ref_1 if e["level"] == "L1")["id"] == mapping["L1@seq:2-3"]
        assert next(e for e in ref_0 if e["level"] == "L2")["id"] == mapping["L2@seq:1-1"]
        assert next(e for e in ref_1 if e["level"] == "L2")["id"] == mapping["L2@seq:2-3"]

    def test_batches_do_not_serialize_on_save(self) -> None:
        """(c) 批次间不串行：第一批落库挂起期间第二批 LLM 调用已发出
        （调用顺序记录断言，不测墙钟）；gather 收割后 refs 照常回填、
        无悬挂 task（全部 add 完成）。"""
        mod = _load_plugin_module()
        events: list[str] = []

        class _SuspendingBackend:
            """add 进入后让出控制权三次再完成（挂起面，纯调度不测墙钟）。"""

            async def add(self, **kwargs: Any) -> str:
                events.append("add-enter")
                for _ in range(3):
                    await asyncio.sleep(0)
                events.append("add-done")
                return f"mem-{events.count('add-done')}"

            async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
                return []

        llm_count = {"n": 0}

        async def fake_llm(payload: list) -> str:
            await asyncio.sleep(0)  # 让出控制权：给后台落库 task 运行机会
            llm_count["n"] += 1
            events.append(f"llm-{llm_count['n']}")
            return _valid_compress_json()

        svc = mod.CompressionService(backend=_SuspendingBackend(), llm_call_fn=fake_llm)
        svc.setup(pipeline_id="pipe-par2", session_id="sess-par2")
        result = _run(
            mod.CompressionService._do_compress_round(
                svc, _round_messages(), 800, {"recent": 300}
            )
        )
        assert result is not None

        # 批次间不串行的可观察事实：第二批 LLM 调用发生在第一批落库完成之前
        assert events.index("add-enter") < events.index("llm-2") < events.index("add-done")
        # execute 返回前所有 save task 已收割：每批 5 次 add（L1/L2/快照/2 memory_items）
        assert events.count("add-done") == 10
        # refs 照常回填（两块引用非空）
        blocks = _process_blocks(result[0])
        assert all(
            m["metadata"]["compression_ref"]["memory_ids"] for m in blocks.values()
        )

    def test_save_batch_refs_swallows_unexpected_error(self, caplog: Any) -> None:
        """save_compression_result 之外的非预期异常（task 内兜底层）→ 引用
        归空 + warning，不向批循环泄漏。"""
        import logging

        mod = _load_plugin_module()
        backend = FakeBackend()
        svc = mod.CompressionService(backend=backend)

        comp_result = {
            "l1": "x",
            "l2": "",
            "keywords": [],
            # state_snapshot 含不可序列化对象 → save_compression_result 内
            # json.dumps（工件 try 面之外）抛 TypeError
            "state_snapshot": {"boom": object()},
            "memory_items": {},
        }
        with caplog.at_level(logging.WARNING):
            refs = _run(
                svc._save_batch_refs(
                    [{"role": "user", "content": "c", "seq": 1}], comp_result, 1000
                )
            )
        assert refs == [], "兜底层必须把非预期异常归为空引用（fail-open）"
        assert any("批次落库异常" in r.getMessage() for r in caplog.records)

    def test_save_outcome_refs_normalizes_gather_outcomes(self, caplog: Any) -> None:
        """gather(return_exceptions=True) 面上的残余异常形态归一为空引用
        （防御层）；正常清单原样透传。"""
        import logging

        mod = _load_plugin_module()
        refs = [{"level": "L1", "id": "mem-1"}]
        assert mod.CompressionService._save_outcome_refs(refs) == refs
        with caplog.at_level(logging.WARNING):
            assert mod.CompressionService._save_outcome_refs(
                RuntimeError("leaked")
            ) == []
        assert any("task 异常" in r.getMessage() for r in caplog.records)


class TestCompressionDegradeNotBlocking:
    """压缩无产出时降级继续管线（类契约：压缩失败不阻塞管线）。

    2026-09-02 真机事故回归：触发线（0.06×窗口）低于 recent 预算
    （0.18×窗口）时，压缩服务判"全部消息都在 recent 预算内"返回 None，
    守卫曾把 None 当压缩失败置 ENDED 终止管线——用户消息零回复。
    """

    @staticmethod
    def _mock_service(return_value: Any = None, side_effect: Any = None) -> MagicMock:
        service = MagicMock()
        service.setup = MagicMock()
        service.compress_messages = AsyncMock(
            return_value=return_value, side_effect=side_effect
        )
        service._last_deleted_seqs = []
        service._last_block_msgs = []
        return service

    @staticmethod
    def _trigger_messages() -> list[dict[str, Any]]:
        """15 条消息，字符估算约 6 万 token，配合 llm_usage 越过 0.5×128k 触发线。"""
        return [
            {"role": "user", "content": f"msg {i} " + "x" * 4000, "seq": i}
            for i in range(1, 16)
        ]

    def test_band_below_recent_budget_continues_pipeline(self) -> None:
        """触发线已越过但全部消息在 recent 预算内（无料可压）→ 管线继续。

        真实 CompressionService 复现事故区间：trigger=0.06×100k=6000 <
        总量 8000（守卫触发），recent=0.18×100k=18000 > 总量 8000（无料可压）。
        断言：不 END、不 skip_remaining、不浪费压缩 LLM 调用。
        """
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.06})
        injected = {"budgets": {"recent": 0.18, "l1": 0.1, "l2": 0.05}}
        llm_fn = AsyncMock(return_value="{}")
        service = mod.CompressionService(
            backend=None,
            llm_call_fn=llm_fn,
            config=mod.CompressionConfig.from_yaml_config(100_000, injected=injected),
            injected=injected,
        )

        messages = [{"role": "system", "content": "s", "seq": 0}] + [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "x" * 800,
                "seq": i + 1,
            }
            for i in range(20)
        ]
        ctx = mod._make_minimal_ctx(
            state={"context_window": 100_000, "messages": messages},
            pipeline_id="pipe-band",
        )
        ctx._services["context_service"] = service

        result = _run(plugin.execute(ctx))

        assert result.state_updates.get(mod.StateKeys.ENDED) is not True
        assert not result.skip_remaining
        llm_fn.assert_not_called()

    def test_service_exception_continues_pipeline(self) -> None:
        """压缩服务抛异常 → 降级继续管线（不 END、不 skip）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        service = self._mock_service(side_effect=RuntimeError("compress llm down"))
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": self._trigger_messages(),
                "llm_usage": {"input_tokens": 80_000},
            },
            pipeline_id="pipe-exc",
        )
        ctx._services["context_service"] = service

        result = _run(plugin.execute(ctx))

        assert result.state_updates.get(mod.StateKeys.ENDED) is not True
        assert not result.skip_remaining
        assert "messages" not in result.state_updates

    def test_service_returns_none_continues_pipeline(self) -> None:
        """压缩服务返回 None（如全部批次失败）→ 降级继续管线，无消息写入。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        service = self._mock_service(return_value=None)
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": self._trigger_messages(),
                "llm_usage": {"input_tokens": 80_000},
            },
            pipeline_id="pipe-none",
        )
        ctx._services["context_service"] = service

        result = _run(plugin.execute(ctx))

        assert result.state_updates.get(mod.StateKeys.ENDED) is not True
        assert not result.skip_remaining
        assert "messages" not in result.state_updates

    def test_no_shrink_result_continues_without_ops(self) -> None:
        """压缩产出未收缩 token（边界：压缩前后等量）→ 继续管线且不应用 ops。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        messages = self._trigger_messages()
        service = self._mock_service(return_value=[dict(m) for m in messages])
        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128_000,
                "messages": messages,
                "llm_usage": {"input_tokens": 80_000},
            },
            pipeline_id="pipe-noshrink",
        )
        ctx._services["context_service"] = service

        result = _run(plugin.execute(ctx))

        assert result.state_updates.get(mod.StateKeys.ENDED) is not True
        assert not result.skip_remaining
        assert "messages" not in result.state_updates
