# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""压缩块消息化三态测试（ADR 2026-08-28-compression-block-pointer-indirection）。

压缩 = 对 message 序列的一次原地编辑：压缩块是排在序列头部的普通
system 消息（role/name/content 完整），自带 metadata.compression_ref
引用元数据（指向记忆库落库锚点，供回溯/展开/审计；LLM 输入只消费块
消息自身的摘要内容）。块消息经引擎 messages ops 与普通消息同机制持久化
（message_slots/blobs 账本），读路径零额外操作。

三态（行为断言，输入 → 序列装配结果/落库调用记录）：
1. 压缩触发 → 序列头部被替换为块消息且块带引用（L1/L2/快照落库 id）；
2. 未触发 → 序列原样（无删除、零落库写）；
3. 写记忆库失败 → 块内容降级为仅内联摘要、引用留空并 warning，流程
   不阻塞（fail-open；存储放置是策略不是定式，默认存记忆库）。

另含跨模块行为断言：llm_core._build_messages 对块消息零特判——块消息
作为 history 普通成员流动，仅经既有通用内部字段清理（seq/_context_form）。

memory backend 属外部依赖，用 FakeBackend/_FlakyBackend 替身；LLM 用
fake llm_call_fn；断言不 mock 内部函数。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_SDK_SRC = str(_PLUGIN_DIR.parents[3] / "sdk" / "src")
if _SDK_SRC not in sys.path:
    sys.path.insert(0, _SDK_SRC)


def _load_guard_module() -> Any:
    """动态加载 guard plugin.py（唯一模块名，避免裸名串扰）。"""
    mod_name = "cwg_block_messages_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "plugin.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_llm_core_module() -> Any:
    """动态加载 llm_core plugin.py（拼消息零特判断言的被测方）。

    plugin.py 内部平铺 import 裸名 'adapter'。pytest 单进程收集多插件，
    sys.modules['adapter'] 可能已指向其他插件的 adapter.py（channel_*/llm/
    multimodal 同名，生产 sidecar 每插件独立进程无此冲突）——逐出指向
    llm_core 目录之外的缓存并把自身目录置顶，保证平铺 import 命中
    llm_core 自己的 adapter.py（与 bash 测试 conftest 同款防御）。
    """
    llm_core_dir = _PLUGIN_DIR.parents[1] / "core" / "llm_core"
    if str(llm_core_dir) not in sys.path:
        sys.path.insert(0, str(llm_core_dir))
    cached = sys.modules.get("adapter")
    cached_file = getattr(cached, "__file__", None)
    if cached is not None and not (
        cached_file and Path(cached_file).resolve().is_relative_to(llm_core_dir.resolve())
    ):
        sys.modules.pop("adapter", None)
    mod_name = "llm_core_block_messages_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, str(llm_core_dir / "plugin.py"))
    assert spec is not None and spec.loader is not None
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
    """合法五段 JSON 压缩响应（l1/l2/keywords/state_snapshot/memory_items）。"""
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
    """记录 add 调用并按序返回 mem-{n} id 的伪 IMemoryBackend。"""

    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        return f"mem-{len(self.add_calls)}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class FlakyBackend(FakeBackend):
    """指定序号的 add 调用抛错的伪 IMemoryBackend（写库故障替身）。"""

    def __init__(self, fail_calls: set[int]) -> None:
        super().__init__()
        self._fail_calls = fail_calls

    async def add(self, **kwargs: Any) -> str:
        n = len(self.add_calls) + 1
        if n in self._fail_calls:
            self.add_calls.append(kwargs)
            raise RuntimeError("backend write boom")
        return await super().add(**kwargs)


def _round_msgs(n: int) -> list[dict[str, Any]]:
    """n 条各约 200 token 的消息（seq 1..n，user/assistant 交替）。"""
    roles = ["user", "assistant"]
    return [
        {"role": roles[i % 2], "content": "A" * 400, "seq": i + 1} for i in range(n)
    ]


def _make_service(mod: Any, backend: Any) -> Any:
    async def fake_llm(payload: list) -> str:
        return _valid_compress_json()

    svc = mod.CompressionService(backend=backend, llm_call_fn=fake_llm)
    svc.setup(pipeline_id="pipe-bm", session_id="sess-bm")
    return svc


def _blocks_of(compressed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in compressed if m.get("metadata", {}).get("compression_ref")]


# ═══════════════════════════════════════════════════════════
# 状态 1：压缩触发 → 序列头部被块消息替换且块带引用
# ═══════════════════════════════════════════════════════════


class TestTriggeredHeadReplacedByBlocks:
    @pytest.mark.parametrize(
        ("recent_budget", "old_max_seq", "freed_expected"),
        [
            (300, 3, [3]),  # old=[m1,m2,m3] 单批：块占 1,2，槽 3 留 gap
            (500, 2, []),  # old=[m1,m2] 单批：块占满 1,2，无留 gap
        ],
        ids=["three-old", "two-old"],
    )
    def test_head_replaced_by_blocks_with_refs(
        self,
        recent_budget: int,
        old_max_seq: int,
        freed_expected: list[int],
    ) -> None:
        """压缩触发：头部两槽原位变为过程块/快照块，块带记忆库引用元数据。

        recent 预算决定 old 段大小（≥2 保证快照块有落槽）；两组输入区分
        「块占满被压区间」与「块占头部、中段留 gap」两种几何。
        """
        mod = _load_guard_module()
        backend = FakeBackend()
        svc = _make_service(mod, backend)
        msgs = _round_msgs(4)

        result = _run(
            mod.CompressionService._do_compress_round(
                svc, msgs, 10000, {"recent": recent_budget}
            )
        )
        assert result is not None
        compressed, deleted = result

        # 序列头两槽 = 过程块 + 快照块（原位编辑：seq 不顺延）
        by_seq = {m["seq"]: m for m in compressed}
        process = by_seq[1]
        snapshot = by_seq[2]
        assert process["role"] == "system" and process["name"] == "compressed"
        assert snapshot["role"] == "system" and snapshot["name"] == "state_snapshot"
        # LLM 输入只消费块自身摘要内容：过程块内联 L1，快照块内联 <current_state>
        assert "完成了 X 任务" in process["content"]
        assert "<current_state>" in snapshot["content"]
        # 语义标记（既有通用内部字段词汇表）
        assert process["_context_form"] == "recall"
        assert snapshot["_context_form"] == "snapshot"

        # 引用元数据：指向记忆库落库锚点（FakeBackend 按 add 顺序发 mem-1..）
        ref = process["metadata"]["compression_ref"]
        assert ref["kind"] == "process"
        assert ref["seq_range"] == [1, old_max_seq]
        assert {e["level"]: e["id"] for e in ref["memory_ids"]} == {
            "L1": "mem-1",
            "L2": "mem-2",
        }
        assert ref["stored_at"]  # 落块时间
        sref = snapshot["metadata"]["compression_ref"]
        assert sref["kind"] == "state_snapshot"
        assert {e["level"]: e["id"] for e in sref["memory_ids"]} == {
            "state_snapshot": "mem-3"
        }

        # 未被块占用的被压槽位进入删除列表（留 gap）；recent 段原样保留
        assert sorted(deleted) == freed_expected
        recent_seqs = [m["seq"] for m in compressed if m not in (process, snapshot)]
        assert recent_seqs == sorted(recent_seqs)
        for s in range(old_max_seq + 1, 5):
            assert by_seq[s]["content"] == "A" * 400

        # 块消息登记给插件 emit set(seq, block) ops
        assert [m["seq"] for m in svc._last_block_msgs] == [1, 2]

    def test_memory_tags_contract_unchanged(self) -> None:
        """既有 tags/pipeline 落库契约保留：chunk 类型 + L1/L2/STATE_SNAPSHOT/
        pipeline:{id}/seq 标签原样（外部引用可按 tag 定向检索）。"""
        mod = _load_guard_module()
        backend = FakeBackend()
        svc = _make_service(mod, backend)
        msgs = _round_msgs(4)

        _run(
            mod.CompressionService._do_compress_round(
                svc, msgs, 10000, {"recent": 300}
            )
        )
        chunk_calls = [c for c in backend.add_calls if c.get("memory_type") == "chunk"]
        assert len(chunk_calls) == 3  # L1 + L2 + STATE_SNAPSHOT
        assert "L1" in chunk_calls[0]["tags"]
        assert "pipeline:pipe-bm" in chunk_calls[0]["tags"]
        assert "seq:1-3" in chunk_calls[0]["tags"]
        assert "L2" in chunk_calls[1]["tags"]
        assert "STATE_SNAPSHOT" in chunk_calls[2]["tags"]


# ═══════════════════════════════════════════════════════════
# 状态 2：未触发 → 序列原样
# ═══════════════════════════════════════════════════════════


class TestNotTriggeredSequenceUnchanged:
    def test_within_budget_no_blocks_no_writes(self) -> None:
        """消息全在 recent 预算内 → 整轮 None：零删除、零块、零落库写。"""
        mod = _load_guard_module()
        backend = FakeBackend()
        svc = _make_service(mod, backend)
        msgs = _round_msgs(4)

        result = _run(
            mod.CompressionService._do_compress_round(
                svc, msgs, 10000, {"recent": 10**9}
            )
        )
        assert result is None
        assert backend.add_calls == []
        assert svc._last_block_msgs == []
        assert svc._last_deleted_seqs == []


# ═══════════════════════════════════════════════════════════
# 状态 3：写记忆库失败 → 内联摘要 + 引用留空 + warning，不阻塞
# ═══════════════════════════════════════════════════════════


class TestMemoryWriteFailureFailOpen:
    def test_all_writes_fail_blocks_still_inline(self, caplog: Any) -> None:
        """全部落库失败 → 块消息照常产出（仅内联摘要、引用留空），
        被压区间照常替换（fail-open），warning 留痕。"""
        mod = _load_guard_module()
        backend = FlakyBackend({1, 2, 3, 4, 5})
        svc = _make_service(mod, backend)
        msgs = _round_msgs(4)

        result = _run(
            mod.CompressionService._do_compress_round(
                svc, msgs, 10000, {"recent": 300}
            )
        )
        assert result is not None, "写库失败不得阻塞压缩流程"
        compressed, deleted = result
        by_seq = {m["seq"]: m for m in compressed}
        process = by_seq[1]
        snapshot = by_seq[2]
        # 块内容降级为仅内联摘要（内容仍在，LLM 输入不受影响）
        assert "完成了 X 任务" in process["content"]
        assert "<current_state>" in snapshot["content"]
        # 引用留空（诚实：无处可指）
        assert process["metadata"]["compression_ref"]["memory_ids"] == []
        assert snapshot["metadata"]["compression_ref"]["memory_ids"] == []
        # 序列编辑照常完成
        assert sorted(deleted) == [3]
        assert [m["seq"] for m in svc._last_block_msgs] == [1, 2]
        # warning 留痕
        assert "落库失败" in caplog.text

    def test_partial_write_failure_keeps_successful_refs(self) -> None:
        """仅 L1 写入失败 → 过程块缺 L1 引用但保留 L2 引用（逐工件降级）。"""
        mod = _load_guard_module()
        backend = FlakyBackend({1})
        svc = _make_service(mod, backend)
        msgs = _round_msgs(4)

        result = _run(
            mod.CompressionService._do_compress_round(
                svc, msgs, 10000, {"recent": 300}
            )
        )
        assert result is not None
        compressed, _deleted = result
        process = next(m for m in compressed if m["seq"] == 1)
        levels = {
            e["level"]: e["id"]
            for e in process["metadata"]["compression_ref"]["memory_ids"]
        }
        assert "L1" not in levels, "写入失败的工件不得伪造引用"
        assert levels.get("L2") == "mem-2"  # 失败调用也占替身序号，L2 落库拿到 mem-2


# ═══════════════════════════════════════════════════════════
# llm_core 拼消息对块消息零特判（跨模块行为断言）
# ═══════════════════════════════════════════════════════════


def _process_block_fixture() -> dict[str, Any]:
    return {
        "role": "system",
        "name": "compressed",
        "content": '<compressed seq="1-3" level="L1">\n## 过程摘要\n{"workflow": "完成了 X 任务"}\n</compressed>',
        "seq": 1,
        "_context_form": "recall",
        "metadata": {
            "compression_ref": {
                "kind": "process",
                "seq_range": [1, 3],
                "stored_at": "2026-08-28T00:00:00+00:00",
                "memory_ids": [{"level": "L1", "id": "mem-1"}],
            }
        },
    }


def _snapshot_block_fixture() -> dict[str, Any]:
    return {
        "role": "system",
        "name": "state_snapshot",
        "content": "<current_state>\n{\"current_state\": \"进行中\"}\n</current_state>",
        "seq": 2,
        "_context_form": "snapshot",
        "metadata": {
            "compression_ref": {
                "kind": "state_snapshot",
                "memory_ids": [{"level": "state_snapshot", "id": "mem-3"}],
            }
        },
    }


class TestLlmCoreAssemblyZeroSpecialCasing:
    @pytest.mark.parametrize(
        "block",
        [_process_block_fixture(), _snapshot_block_fixture()],
        ids=["process-block", "snapshot-block"],
    )
    def test_block_flows_as_ordinary_history_message(self, block: dict[str, Any]) -> None:
        """块消息在 history 中按普通成员装配：位置与内容原样，仅经既有
        通用内部字段清理（seq/_context_form），无任何块专属分支。"""
        llm_mod = _load_llm_core_module()
        core = llm_mod.LLMCore({})
        state: dict[str, Any] = {
            "messages": [dict(block), {"role": "user", "content": "hi", "seq": 99}]
        }
        out = core._build_messages(state)

        assert len(out) == 2
        head = out[0]
        assert head["role"] == "system"
        assert head["name"] == block["name"]
        assert head["content"] == block["content"], "块内容必须原样进入载荷"
        # 通用内部字段清理（既有机制，非块专属特判）
        assert "seq" not in head
        assert "_context_form" not in head
        # 位置：序列头部（历史顺序保持）
        assert out[1]["content"] == "hi"


# ═══════════════════════════════════════════════════════════
# 插件级集成：execute 产出 set(seq, block) + set(seq, null) ops
# ═══════════════════════════════════════════════════════════


class TestPluginOpsIntegration:
    def test_execute_emits_block_ops_and_null_ops(self) -> None:
        """真实 CompressionService 注入插件：压缩触发后 ops = 头部两槽
        set(seq, 块) + 未占用被压槽 set(seq, null)；未压消息无 op。"""
        mod = _load_guard_module()
        backend = FakeBackend()
        mod.set_memory_backend(backend)

        svc = _make_service(mod, backend)
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})
        messages = _round_msgs(6)
        ctx = mod._make_minimal_ctx(
            state={"context_window": 2000, "messages": messages},
            pipeline_id="pipe-bm",
        )
        ctx._services["context_service"] = svc

        result = _run(plugin.execute(ctx))
        updates = result.state_updates
        ops_list = updates["messages"]["_ops"]
        ops = {op["seq"]: op for op in ops_list if isinstance(op, dict) and "seq" in op}

        # 头部两槽：原位替换为块消息
        assert ops[1]["op"] == "set"
        block_msg = ops[1]["msg"]
        assert block_msg["name"] == "compressed"
        assert block_msg["metadata"]["compression_ref"]["kind"] == "process"
        assert ops[2]["msg"]["name"] == "state_snapshot"
        # 被压区间剩余槽位：留 gap
        for s in (3, 4, 5):
            assert ops[s] == {"op": "set", "seq": s, "msg": None}
        # recent 消息无 op
        assert 6 not in ops
        assert set(ops.keys()) == {1, 2, 3, 4, 5}
