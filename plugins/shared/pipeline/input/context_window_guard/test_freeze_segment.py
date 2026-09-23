# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""freeze_segment 段冻结测试（消息段模型方案 §2.6/§5.1，P0 压缩原文存档）。

被压原文冻结为段与压缩块写入启用序列是同一事件的两个面（§2.6 不变量 2：
冻结与建块同事务——只写块不冻结 = 原文丢失）。本文件断言插件侧的可观察
行为（输入 → state_updates.messages._ops 批次内容）：

1. 压缩成功批次含 freeze_segment op：members = 被压原消息数组原文、
   visible_to="user"、id 形态 seg_ 前缀、base_seq/base_len 按被压区间、
   preview 取首条 content 截断 200 字符；
2. 块消息 metadata：compression_ref.segment_id（同批次过程块/快照块同值，
   前端按 seq_range 归组一张卡）+ message_style="compression_card"
   （chatMessages 声明命中通用 webview 消息卡容器）；
3. 降级路径（压缩失败/无产出）不冻结——现有 delete ops 行为不变；
4. 多段压缩：单事件多批次各冻结一段（同批次上报）；第二次压缩事件在含旧块
   消息的序列上——旧块（prior_blocks）保持原位、不入段，新段 members 只含
   新被压原消息（级联冻结旧块属方案 §7.1 超块场景，现行分类下旧块永不入批）。

memory backend 属外部依赖，用 FakeBackend 替身；LLM 用 fake llm_call_fn；
断言不 mock 内部函数。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    mod_name = "cwg_freeze_segment_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "plugin.py"))
    assert spec is not None
    assert spec.loader is not None
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


def _compact_compress_json() -> str:
    """紧凑五段 JSON 压缩响应（块开销小，保证 token 收缩判据稳定成立）。"""
    return json.dumps(
        {
            "l1": {
                "session_title": "测试会话",
                "workflow": "完成了 X 任务",
                "errors_and_corrections": None,
                "decisions": None,
                "key_results": None,
            },
            "l2": {"intent": "做 X", "process": "步骤 A 然后 B", "results": "产出 X"},
            "keywords": ["关键词1"],
            "state_snapshot": {"current_state": "进行中"},
            "memory_items": {},
        },
        ensure_ascii=False,
    )


class FakeBackend:
    """记录 add 调用并按序返回 mem-{n} id 的伪 IMemoryBackend（search 空）。"""

    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> str:
        self.add_calls.append(kwargs)
        return f"mem-{len(self.add_calls)}"

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _seq_msgs(n: int, chars: int) -> list[dict[str, Any]]:
    """n 条各约 chars//2 token 的消息（seq 1..n，user/assistant 交替）。"""
    roles = ["user", "assistant"]
    return [
        {"role": roles[i % 2], "content": "A" * chars, "seq": i + 1} for i in range(n)
    ]


def _make_service(mod: Any, llm_response: str) -> Any:
    async def fake_llm(payload: list) -> str:
        return llm_response

    return mod.CompressionService(backend=FakeBackend(), llm_call_fn=fake_llm)


def _execute_compress(mod: Any, svc: Any, state: dict[str, Any]) -> Any:
    """真实服务注入插件的 execute（压缩成功/降级均走此路径）。"""
    plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.55})
    ctx = mod._make_minimal_ctx(state=state, pipeline_id="pipe-fs")
    ctx._services["context_service"] = svc
    return _run(plugin.execute(ctx))


def _freeze_ops(ops_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 _ops 批次中拣出 freeze_segment op。"""
    return [op for op in ops_list if isinstance(op, dict) and op.get("op") == "freeze_segment"]


def _block_msgs(ops_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 _ops 批次中拣出压缩块消息（set(seq, 块) 的 msg）。"""
    return [
        op["msg"]
        for op in ops_list
        if isinstance(op, dict)
        and op.get("op") == "set"
        and isinstance(op.get("msg"), dict)
        and isinstance(op["msg"].get("metadata"), dict)
        and "compression_ref" in op["msg"]["metadata"]
    ]


# ═══════════════════════════════════════════════════════════
# 1+2. 压缩成功：freeze op 与块 metadata（双触发路径 × 截断两态）
# ═══════════════════════════════════════════════════════════


class TestSuccessBatchFreezesSegment:
    @pytest.mark.parametrize(
        ("msg_count", "chars", "llm_input_tokens", "exp_base_len", "exp_preview"),
        [
            # 全量字符估算触发（无 llm_usage）：old=前 5 条，preview 截断 200
            (6, 400, 0, 5, "A" * 200),
            # prev_input + delta 触发（llm_usage 80k）：old=前 4 条，preview 不截断
            (8, 150, 80_000, 4, "A" * 150),
        ],
        ids=["threshold-trigger-truncated-preview", "prev-input-trigger-full-preview"],
    )
    def test_freeze_op_and_block_metadata(
        self,
        msg_count: int,
        chars: int,
        llm_input_tokens: int,
        exp_base_len: int,
        exp_preview: str,
    ) -> None:
        """压缩成功批次：freeze op 形态按契约，块 metadata 带段入口与卡样式。"""
        mod = _load_guard_module()
        svc = _make_service(mod, _compact_compress_json())
        messages = _seq_msgs(msg_count, chars)
        state: dict[str, Any] = {"context_window": 2000, "messages": messages}
        if llm_input_tokens:
            state["llm_usage"] = {"input_tokens": llm_input_tokens}

        result = _execute_compress(mod, svc, state)
        ops_list = result.state_updates["messages"]["_ops"]

        # freeze op 与块写入同一 _ops 批次（同事务落库的前提）
        freezes = _freeze_ops(ops_list)
        assert len(freezes) == 1
        freeze = freezes[0]
        assert freeze["id"].startswith("seg_")
        assert freeze["visible_to"] == "user"
        # base_seq/base_len 按被压区间；base_len = 成员数（性质断言）
        assert freeze["base_seq"] == 1
        assert freeze["base_len"] == exp_base_len
        assert freeze["base_len"] == len(freeze["members"])
        # members = 被压原消息数组原文（seq 连续 + 逐条 content 一致）
        assert [m["seq"] for m in freeze["members"]] == list(range(1, exp_base_len + 1))
        assert all(m["content"] == "A" * chars for m in freeze["members"])
        # preview 取首条 content 截断 200 字符
        assert freeze["preview"] == exp_preview

        # 块 metadata：segment_id 与 freeze op 一致（前端展开入口），
        # message_style 命中 chatMessages 声明（过程块/快照块都带 → 归组一张卡）
        blocks = _block_msgs(ops_list)
        assert len(blocks) >= 1
        for block in blocks:
            ref = block["metadata"]["compression_ref"]
            assert ref["segment_id"] == freeze["id"]
            assert block["metadata"]["message_style"] == "compression_card"
        # 同批次过程块/快照块共享同一段（同 seq_range 归组）
        assert len({b["metadata"]["compression_ref"]["segment_id"] for b in blocks}) == 1


# ═══════════════════════════════════════════════════════════
# 3. 降级路径：不冻结（现有 delete ops 行为不变）
# ═══════════════════════════════════════════════════════════


class TestDegradePathNoFreeze:
    def test_llm_garbage_response_no_freeze(self) -> None:
        """LLM 返回非 JSON（压缩无产出）→ 降级：无 messages ops、无 freeze op。"""
        mod = _load_guard_module()
        svc = _make_service(mod, "抱歉，我无法按要求输出。")
        messages = _seq_msgs(6, 400)

        result = _execute_compress(
            mod, svc, {"context_window": 2000, "messages": messages}
        )
        assert "messages" not in result.state_updates, "无产出降级不得携带压缩 ops"
        assert result.state_updates.get("messages", {}).get("_ops", []) == []

    def test_service_returns_none_no_freeze(self) -> None:
        """service 返回 None（压缩失败/无 LLM 函数）→ 降级不冻结，管线不阻塞。"""
        mod = _load_guard_module()
        svc = MagicMock()
        svc.setup = MagicMock()
        svc.compress_messages = AsyncMock(return_value=None)
        messages = _seq_msgs(6, 400)

        result = _execute_compress(
            mod, svc, {"context_window": 2000, "messages": messages}
        )
        assert "messages" not in result.state_updates
        assert not result.skip_remaining


# ═══════════════════════════════════════════════════════════
# 4. 多段压缩与旧块在场（第二次冻结）
# ═══════════════════════════════════════════════════════════


class TestMultiBatchSingleEvent:
    def test_one_event_two_batches_two_segments(self) -> None:
        """单次压缩事件按压缩模型窗口切两批：每批各冻结一段（id 可辨），
        同批次过程块/快照块共享段 id，两段同落一个 _ops 批次。

        消息构造（cw=2000，llm_usage 80k 触发）：m1/m2 各 200 tok、m3 500 tok、
        m4 400 tok——recent 预算 360 装不下 m4，old=全部（1300 tok）超单批预算
        1000 → 切 [m1,m2] / [m3,m4] 两批。
        """
        mod = _load_guard_module()
        svc = _make_service(mod, _compact_compress_json())
        messages = [
            {"role": "user", "content": "A" * 400, "seq": 1},
            {"role": "assistant", "content": "A" * 400, "seq": 2},
            {"role": "user", "content": "C" * 1000, "seq": 3},
            {"role": "assistant", "content": "C" * 800, "seq": 4},
        ]
        state = {
            "context_window": 2000,
            "messages": messages,
            "llm_usage": {"input_tokens": 80_000},
        }

        result = _execute_compress(mod, svc, state)
        ops_list = result.state_updates["messages"]["_ops"]

        freezes = _freeze_ops(ops_list)
        assert len(freezes) == 2, "两批各冻结一段，同批次上报"
        first, second = freezes
        assert first["id"] != second["id"]
        assert (first["base_seq"], first["base_len"]) == (1, 2)
        assert [m["seq"] for m in first["members"]] == [1, 2]
        assert (second["base_seq"], second["base_len"]) == (3, 2)
        assert [m["seq"] for m in second["members"]] == [3, 4]

        # 段与块按批对应：批内块共享段 id（同 seq_range 归组一张卡）
        blocks = _block_msgs(ops_list)
        by_seq = {b["seq"]: b for b in blocks}
        assert {1, 2, 3, 4} <= set(by_seq)
        for seq in (1, 2):
            ref = by_seq[seq]["metadata"]["compression_ref"]
            assert ref["segment_id"] == first["id"]
            assert by_seq[seq]["metadata"]["message_style"] == "compression_card"
        for seq in (3, 4):
            assert by_seq[seq]["metadata"]["compression_ref"]["segment_id"] == second["id"]


class TestSecondEventWithPriorBlocks:
    def test_second_freeze_excludes_prior_blocks(self) -> None:
        """第二次压缩事件（序列已含第一次的块消息）：旧块是 prior_blocks——
        保持原位（无 ops 触碰）、不入段；新段 members 只含新被压原消息。

        第一次：m1..m3（200/200/500 tok）→ 块占 seq 1,2、seq 3 留 gap。
        第二次历史 = 块 + 新消息 m4/m5（各 400 tok，seq 4,5）→ 新段冻结 [4,6)。
        """
        mod = _load_guard_module()
        messages = [
            {"role": "user", "content": "A" * 400, "seq": 1},
            {"role": "assistant", "content": "A" * 400, "seq": 2},
            {"role": "user", "content": "C" * 1000, "seq": 3},
        ]
        state = {
            "context_window": 2000,
            "messages": messages,
            "llm_usage": {"input_tokens": 80_000},
        }
        first_result = _execute_compress(mod, _make_service(mod, _compact_compress_json()), state)
        prior_blocks = _block_msgs(first_result.state_updates["messages"]["_ops"])
        assert len(prior_blocks) == 2

        # 应用第一次 ops 后的启用序列（块原位 + 新消息追加）
        second_history = prior_blocks + [
            {"role": "assistant", "content": "D" * 800, "seq": 4},
            {"role": "user", "content": "D" * 800, "seq": 5},
        ]
        second_state = {
            "context_window": 2000,
            "messages": second_history,
            "llm_usage": {"input_tokens": 80_000},
        }
        second_result = _execute_compress(
            mod, _make_service(mod, _compact_compress_json()), second_state
        )
        ops_list = second_result.state_updates["messages"]["_ops"]

        freezes = _freeze_ops(ops_list)
        assert len(freezes) == 1
        freeze = freezes[0]
        assert freeze["id"].startswith("seg_")
        # 新段 = 新被压原消息（seq 4,5），旧块不入段
        assert (freeze["base_seq"], freeze["base_len"]) == (4, 2)
        assert [m["seq"] for m in freeze["members"]] == [4, 5]
        for member in freeze["members"]:
            metadata = member.get("metadata") or {}
            assert "compression_ref" not in metadata

        # 旧块保持原位：第二次批次无 seq 1/2 的 ops（不删不改不冻结）
        touched_seqs = {
            op["seq"] for op in ops_list if isinstance(op, dict) and "seq" in op
        }
        assert {1, 2} & touched_seqs == set()

        # 新块带新段入口（≠ 旧块的段 id）
        prior_ids = {b["metadata"]["compression_ref"]["segment_id"] for b in prior_blocks}
        new_blocks = _block_msgs(ops_list)
        assert {b["seq"] for b in new_blocks} == {4, 5}
        for block in new_blocks:
            ref = block["metadata"]["compression_ref"]
            assert ref["segment_id"] == freeze["id"]
            assert ref["segment_id"] not in prior_ids
            assert block["metadata"]["message_style"] == "compression_card"


# ═══════════════════════════════════════════════════════════
# 5. chatMessages 声明与卡资产管道
# ═══════════════════════════════════════════════════════════


class TestManifestCardDeclaration:
    def test_chat_messages_declaration_wiring(self) -> None:
        """plugin.json 声明 compression_card：id 与代码样式 id 同值，
        htmlPath 走内核静态资产路由约定且指向包内真实存在的文件。"""
        mod = _load_guard_module()
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

        entries = manifest["contributes"]["chatMessages"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == "compression_card"
        assert entry["id"] == mod.COMPRESSION_CARD_STYLE
        props = entry["props"]
        assert props["pluginId"] == manifest["id"]

        # htmlPath 按既有相对路径约定：/assets/** → 内核静态资产直读
        # （{插件包}/web/**），不要求插件自声明 http 端点
        html_path: str = props["htmlPath"]
        assert html_path.startswith("/assets/")
        card_file = _PLUGIN_DIR / "web" / html_path[len("/assets/") :]
        assert card_file.is_file(), f"声明的卡资产不存在: {card_file}"
