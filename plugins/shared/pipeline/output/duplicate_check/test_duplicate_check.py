# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""duplicate_check 插件行为测试——签名计数/三级渐进/消息 ops 回传/server 适配层。

契约（plugin.py 头注）：
- 工具调用重复：单调用粒度签名（同名+同参数，str 参数 JSON 归一）在滑动窗口内
  的"多余出现次数"取最大值为重复计数——多工具部分重复/交替循环与单工具完全
  相同同样可达阈值（整组只比上一轮的旧算法对多工具场景恒不计数，已退役）
- 输出重复：前 500 字符哈希相同，或词级 Jaccard 相似度 > 阈值 → 计数+1
- 三级渐进：软提示（计数<阈值，调用仍执行）→ 拦截（清空调用/输出+计数归零+
  拦截数+1+路由回 LLM）→ 终止（拦截数≥硬上限；主 agent 追加用户通知，子 agent 直接终止）
- 消息修改一律经 state_updates["messages"]={"_ops":[...]} 回传（引擎三落点唯一
  通道；直接改 ctx.state 在真实链路上会被 server 适配层的新 dict 丢弃）：
  末尾 tool/assistant/system 按 seq modify 合并 content（不打断
  assistant(tool_calls)→tool 序列），末尾 user/空 追加 user，剥离产 msg=null delete
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "duplicate_check_plugin_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_plugin_module()
DuplicateCheckPlugin = _MOD.DuplicateCheckPlugin


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_ctx(state: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config=config or {})


def _tool_state(calls: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"raw_tool_calls": calls, "messages": []}
    state.update(extra)
    return state


def _execute(plugin: Any, state: dict[str, Any]) -> Any:
    return _run(plugin.execute(_make_ctx(state)))


def _merge_updates(state: dict[str, Any], updates: dict[str, Any]) -> None:
    """模拟引擎 merge 口径：messages 走 slot ops apply，其余键平插。

    引擎契约（apply_slot_ops_to_array）：set 缺 seq = append（引擎分配递增 seq）；
    set(seq, msg) = 同 seq 替换/插入；set(seq, null) = 删除。测试侧用 len(messages)
    充当递增 seq。
    """
    for key, value in updates.items():
        if key == "messages":
            msgs = state.setdefault("messages", [])
            for op in value.get("_ops", []):
                seq = op.get("seq")
                msg = op.get("msg")
                if seq is None:
                    msgs.append(dict(msg, seq=len(msgs)))
                elif msg is None:
                    state["messages"] = [m for m in msgs if m.get("seq") != seq]
                else:
                    for i, m in enumerate(msgs):
                        if m.get("seq") == seq:
                            msgs[i] = dict(msg, seq=seq)
                            break
                    else:
                        msgs.append(dict(msg, seq=seq))
        else:
            state[key] = value


def _hint_ops(updates: dict[str, Any]) -> list[dict[str, Any]]:
    """取 updates 中的消息 ops（无则空表）。"""
    return list(updates.get("messages", {}).get("_ops", []))


# ═══════════════════════════════════════════════════════════
# 签名与计数（重复检测的判定层）
# ═══════════════════════════════════════════════════════════


class TestSignatureCounting:
    def test_no_tool_calls_and_no_result_is_noop(self) -> None:
        """空状态 → 计数显式清零、无路由（不误伤正常流）。"""
        plugin = DuplicateCheckPlugin()
        result = _execute(plugin, {"messages": []})
        assert result.state_updates == {
            "router.duplicate_count": 0,
            "router.repetitive_count": 0,
        }

    def test_same_tool_args_increment_count_across_iterations(self) -> None:
        """同名同参连续两次 → 第二次计数 1（引擎侧合并 updates 的真实迭代口径）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "file_read", "arguments": {"path": "a.py"}}])
        _merge_updates(state, _execute(plugin, state).state_updates)  # 第一次：写入签名
        _merge_updates(state, _execute(plugin, state).state_updates)  # 第二次：命中
        assert state["router.duplicate_count"] == 1

    def test_different_args_reset_count(self) -> None:
        """参数变化且无窗口历史 → 计数归零（换方法即豁免）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "file_read", "arguments": {"path": "a.py"}}])
        _merge_updates(state, _execute(plugin, state).state_updates)
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1
        state["raw_tool_calls"] = [{"name": "file_read", "arguments": {"path": "b.py"}}]
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 0

    @pytest.mark.parametrize(
        ("first_args", "second_args"),
        [
            ('{"path": "a.py"}', {"path": "a.py"}),  # str JSON 与 dict 等价
            ("not-json", {}),  # 非法 JSON 与空参数等价
            (["x", "y"], {}),  # 非 dict 参数与空参数等价
        ],
    )
    def test_argument_normalization_makes_equivalent_signatures(self, first_args: Any, second_args: Any) -> None:
        """str/非法 JSON/非 dict 参数归一后与等价 dict 同签名 → 计数递增。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "t", "arguments": first_args}])
        _merge_updates(state, _execute(plugin, state).state_updates)
        state["raw_tool_calls"] = [{"name": "t", "arguments": second_args}]
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1

    def test_multi_call_window_counts_per_signature(self) -> None:
        """多调用按单调用粒度入窗计数：连续两轮 [a,b] 后换序 [b,a]，a/b 各第 3 次出现。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        calls_ab = [
            {"name": "a", "arguments": {"x": 1}},
            {"name": "b", "arguments": {"y": 2}},
        ]
        state = _tool_state(calls_ab)
        _merge_updates(state, _execute(plugin, state).state_updates)
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1  # a、b 各第 2 次出现
        state["raw_tool_calls"] = list(reversed(calls_ab))
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 2  # 换序不重置：a、b 各第 3 次出现

    def test_window_slides_old_signatures_out(self) -> None:
        """窗口滚动：签名滚出窗口后不再计入重复（换路后的重访不误拦）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 2, "signature_window_size": 4})
        state = _tool_state([{"name": "t", "arguments": {"q": "a"}}])
        _merge_updates(state, _execute(plugin, state).state_updates)  # a 第 1 次
        _merge_updates(state, _execute(plugin, state).state_updates)  # a 第 2 次 → count 1
        for q in "bcde":  # 4 轮其他调用把 a 滚出 4 条窗口
            state["raw_tool_calls"] = [{"name": "t", "arguments": {"q": q}}]
            _merge_updates(state, _execute(plugin, state).state_updates)
        state["raw_tool_calls"] = [{"name": "t", "arguments": {"q": "a"}}]
        result = _execute(plugin, state)
        assert result.state_updates["router.duplicate_count"] == 0


class TestMultiToolWindow:
    """多工具盲区回归（08-30）：旧整组算法对以下场景恒不计数，永不拦截。"""

    def test_partial_overlap_alternating_combinations_intercepted(self) -> None:
        """部分重复：[t(a),t(b)]/[t(a),t(c)] 交替，公共调用 t(a) 第 4 次出现拦截。"""
        plugin = DuplicateCheckPlugin()
        state = _tool_state([])
        for i in range(4):
            other = "b" if i % 2 == 0 else "c"
            state["raw_tool_calls"] = [
                {"name": "t", "arguments": {"q": "a"}},
                {"name": "t", "arguments": {"q": other}},
            ]
            state["messages"] = []
            _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_intercepts"] == 1
        assert state["router.duplicate_back_llm"] is True
        assert state["raw_tool_calls"] == []

    def test_alternating_single_calls_intercepted(self) -> None:
        """交替循环：t(a)/t(b) 轮流，a 第 4 次出现（第 7 轮）拦截。"""
        plugin = DuplicateCheckPlugin()
        state = _tool_state([])
        for i in range(7):
            q = "a" if i % 2 == 0 else "b"
            state["raw_tool_calls"] = [{"name": "t", "arguments": {"q": q}}]
            state["messages"] = []
            _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_intercepts"] == 1

    def test_intra_round_duplicate_counted_and_intercepted(self) -> None:
        """组内重复：单轮 [t(a),t(a)] 首轮即软提示，次轮同型达阈值拦截。"""
        plugin = DuplicateCheckPlugin()
        calls = [{"name": "t", "arguments": {"q": "a"}}, {"name": "t", "arguments": {"q": "a"}}]
        state = _tool_state(calls)
        result = _execute(plugin, state)
        assert result.state_updates["router.duplicate_count"] == 1  # 组内 1 次多余出现
        assert _hint_ops(result.state_updates), "组内重复首轮即软提示"
        state["messages"] = []
        _merge_updates(state, result.state_updates)
        result = _execute(plugin, state)
        assert result.state_updates["router.duplicate_back_llm"] is True  # 窗口 2 + 本轮 2 → 3

    def test_distinct_tools_each_tracked_independently(self) -> None:
        """多工具各自计数：t(a) 重复 3 次后拦截，此间穿插的 u(x) 不受牵连。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 2})
        state = _tool_state([])
        for i in range(3):
            calls = [{"name": "t", "arguments": {"q": "a"}}]
            if i == 1:  # 中间穿插一个无关调用
                calls.append({"name": "u", "arguments": {"x": 1}})
            state["raw_tool_calls"] = calls
            state["messages"] = []
            _merge_updates(state, _execute(plugin, state).state_updates)
        assert state["router.duplicate_intercepts"] == 1  # t(a) 第 3 次出现（阈值 2）


# ═══════════════════════════════════════════════════════════
# 陈旧计数不跨轮误判
# ═══════════════════════════════════════════════════════════


class TestStaleStateReset:
    """历史轮 level-1 软提示遗留的计数不得误伤后续无输入轮次。

    场景：早前轮次 file_read 重复触发软提示（level-1 不清零），
    router.duplicate_count=1 遗留 state；后续纯文本回复轮（无工具调用）
    若继承该计数，提示会被合并进正常回复正文且工具名为空串。
    """

    def test_stale_duplicate_count_not_inherited_on_tool_free_round(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "raw_tool_calls": [],
            "messages": [{"role": "assistant", "content": "散文正文", "seq": 41}],
            "router.duplicate_count": 1,  # 历史轮遗留
            "router.recent_tool_sigs": ["abc12345"],
        }
        updates = _execute(plugin, state).state_updates
        assert updates["router.duplicate_count"] == 0
        assert "messages" not in updates  # 不向回复正文追加/合并任何提示
        # 滑动窗口保留：无调用轮不产签名，也不动窗口
        assert "router.recent_tool_sigs" not in updates

    def test_window_still_detects_repeat_after_tool_free_round(self) -> None:
        """纯文本轮清零不清窗：下一轮同参调用仍由窗口重数检出。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        call = [{"name": "file_read", "arguments": {"path": "a.py"}}]
        state = _tool_state(call)
        _merge_updates(state, _execute(plugin, state).state_updates)  # 轮1：签名入窗
        state["raw_tool_calls"] = []
        _merge_updates(state, _execute(plugin, state).state_updates)  # 轮2：纯文本
        assert state["router.duplicate_count"] == 0
        state["raw_tool_calls"] = call
        _merge_updates(state, _execute(plugin, state).state_updates)  # 轮3：同参复现
        assert state["router.duplicate_count"] == 1

    def test_stale_repetitive_count_not_inherited_on_output_free_round(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "raw_tool_calls": [],
            "raw_result": None,
            "messages": [],
            "router.repetitive_count": 1,  # 历史轮遗留
            "router.last_response": "abc12345",
            "router.last_response_text": "上一轮输出",
        }
        updates = _execute(plugin, state).state_updates
        assert updates["router.repetitive_count"] == 0
        assert "messages" not in updates
        # 上次输出签名保留在 state，不入 updates（供下一个有输出轮对比）
        assert state["router.last_response"] == "abc12345"
        assert "router.last_response" not in updates


# ═══════════════════════════════════════════════════════════
# 三级渐进
# ═══════════════════════════════════════════════════════════


class TestEscalation:
    def _dup_state(self, **config: Any) -> tuple[Any, dict[str, Any]]:
        """预热到「本次工具调用签名与上次相同、计数仍为 0」的边界。

        只并入第一次执行的 updates（写入签名）；第二次执行命中签名（计数
        存在于其返回值、不并入 state），并清掉预热注入的提示消息。
        """
        plugin = DuplicateCheckPlugin(config=config or None)
        calls = [{"name": "bash", "arguments": {"cmd": "ls"}}]
        state = _tool_state(calls)
        _merge_updates(state, _execute(plugin, state).state_updates)
        _execute(plugin, state)
        state["messages"] = []
        return plugin, state

    def test_level1_soft_hint_keeps_tool_calls(self) -> None:
        """第一级：计数 1<3 → 软提示经 ops 回传，工具调用不动、不路由。"""
        plugin, state = self._dup_state()
        result = _execute(plugin, state)
        updates = result.state_updates
        assert updates.get("router.duplicate_back_llm") is not True
        assert "raw_tool_calls" not in updates  # 调用保留
        ops = _hint_ops(updates)
        assert len(ops) == 1
        assert ops[0].get("seq") is None  # 空 messages → append（缺 seq=append 契约）
        assert ops[0]["msg"]["role"] == "user"
        assert "[DuplicateCheck]" in ops[0]["msg"]["content"]
        assert "1 次" in ops[0]["msg"]["content"]  # 计数入提示

    def test_level1_hint_template_escalates_wording(self) -> None:
        """性质断言：计数升高提示措辞升级（第 2 次出现「立即停止」）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "bash", "arguments": {"cmd": "ls"}}])
        _merge_updates(state, _execute(plugin, state).state_updates)  # 写入签名
        _merge_updates(state, _execute(plugin, state).state_updates)  # count=1 → 提示 1
        content1 = state["messages"][-1]["content"]
        assert "考虑换一种方式" in content1
        assert "立即停止" not in content1
        state["messages"] = []
        _merge_updates(state, _execute(plugin, state).state_updates)  # count=2 → 提示 2
        assert "立即停止" in state["messages"][-1]["content"]

    def test_level2_intercepts_and_reroutes(self) -> None:
        """第二级：计数≥阈值 → 清空调用、计数归零、拦截+1、路由回 LLM。"""
        plugin, state = self._dup_state(max_duplicate_calls=1)
        result = _execute(plugin, state)
        assert result.state_updates.get("router.duplicate_back_llm") is True
        assert result.state_updates["raw_tool_calls"] == []
        assert result.state_updates["router.duplicate_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    def test_level2_strips_trailing_tool_call_assistants(self) -> None:
        """拦截时同步剥离末尾连续 assistant(tool_calls)（delete ops），避免未配对消息。"""
        plugin, state = self._dup_state(max_duplicate_calls=1)
        state["messages"] = [
            {"role": "user", "content": "go", "seq": 0},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}], "seq": 1},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t2"}], "seq": 2},
        ]
        result = _execute(plugin, state)
        ops = _hint_ops(result.state_updates)
        deleted_seqs = [op["seq"] for op in ops if op.get("msg") is None]
        assert deleted_seqs == [1, 2]  # 两条 assistant(tool_calls) 按序剥离
        appended = [op for op in ops if op.get("seq") is None]
        assert appended and "已跳过执行" in appended[0]["msg"]["content"]

    def test_level3_main_agent_notifies_and_ends(self) -> None:
        """第三级（主 agent）：append 用户通知 op + 终止路由。"""
        plugin, state = self._dup_state(max_duplicate_calls=1, hard_limit_intercepts=1)
        state["router.duplicate_intercepts"] = 1
        state["agent_level"] = "L1"
        result = _execute(plugin, state)
        assert result.state_updates.get("should_stop") is True
        assert result.state_updates.get("router.stop_reason") == "duplicate_loop"
        ops = _hint_ops(result.state_updates)
        assert len(ops) == 1 and ops[0].get("seq") is None
        assert ops[0]["msg"]["role"] == "assistant"
        assert "重复" in ops[0]["msg"]["content"]

    def test_level3_sub_agent_ends_silently(self) -> None:
        """第三级（子 agent）：不注入消息，直接终止。"""
        plugin, state = self._dup_state(max_duplicate_calls=1, hard_limit_intercepts=1)
        state["router.duplicate_intercepts"] = 1
        state["agent_level"] = "L2"
        state["delegate_depth"] = 1
        result = _execute(plugin, state)
        assert result.state_updates.get("should_stop") is True
        assert result.state_updates.get("router.stop_reason") == "duplicate_loop"
        assert "messages" not in result.state_updates  # 无通知注入

    def test_repetitive_level2_clears_raw_result(self) -> None:
        """输出重复第二级：raw_result 清空 + 计数归零 + 路由回 LLM。"""
        plugin = DuplicateCheckPlugin(config={"max_repetitive_output": 1})
        state: dict[str, Any] = {"raw_result": "same", "messages": []}
        _merge_updates(state, _execute(plugin, state).state_updates)
        result = _execute(plugin, state)
        assert result.state_updates.get("router.duplicate_back_llm") is True
        assert result.state_updates["raw_result"] == ""
        assert result.state_updates["router.repetitive_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    def test_repetitive_level1_hint_only(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {"raw_result": "same", "messages": []}
        _merge_updates(state, _execute(plugin, state).state_updates)
        result = _execute(plugin, state)
        assert result.state_updates.get("router.duplicate_back_llm") is not True
        assert "raw_result" not in result.state_updates
        ops = _hint_ops(result.state_updates)
        assert ops and "相似内容" in ops[0]["msg"]["content"]

    def test_repetitive_level3_terminates(self) -> None:
        """输出重复第三级：拦截数达硬上限 → 终止（与工具路径同构）。"""
        plugin = DuplicateCheckPlugin(config={"max_repetitive_output": 1, "hard_limit_intercepts": 1})
        state: dict[str, Any] = {"raw_result": "same", "messages": [], "agent_level": "L2", "delegate_depth": 1}
        _merge_updates(state, _execute(plugin, state).state_updates)
        state["router.duplicate_intercepts"] = 1
        result = _execute(plugin, state)
        assert result.state_updates.get("should_stop") is True
        assert result.state_updates.get("router.stop_reason") == "duplicate_loop"

    def test_hint_shows_tool_with_string_arguments(self) -> None:
        """str 参数（合法 JSON）同样参与签名；提示文本带工具名。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "bash", "arguments": '{"cmd": "ls"}'}])
        _merge_updates(state, _execute(plugin, state).state_updates)
        _merge_updates(state, _execute(plugin, state).state_updates)
        assert "bash" in state["messages"][-1]["content"]

    def test_declared_plugin_contract_properties(self) -> None:
        """声明面契约：名称/默认优先级/配置覆盖优先级/路由信号枚举。"""
        assert DuplicateCheckPlugin().name == "duplicate_check"
        assert DuplicateCheckPlugin().priority == 4
        assert DuplicateCheckPlugin(config={"priority": 9}).priority == 9
        assert DuplicateCheckPlugin().route_signals == ["next_llm", "end"]

    def test_full_natural_escalation_to_termination(self) -> None:
        """端到端性质断言：持续重复同一调用，必经软提示→多次拦截→终止。"""
        plugin = DuplicateCheckPlugin()  # 默认 3/3/4
        calls = [{"name": "bash", "arguments": {"cmd": "ls"}}]
        state = _tool_state(calls)
        seen_intercepts: list[bool] = []
        terminated = False
        for _ in range(24):
            # 模拟顽固 LLM：每轮重发同一工具调用（拦截清空后由引擎从模型重新获得）
            state["raw_tool_calls"] = calls
            state["messages"] = []
            result = _execute(plugin, state)
            _merge_updates(state, result.state_updates)
            if result.state_updates.get("should_stop"):
                terminated = True
                break
            if result.state_updates.get("router.duplicate_back_llm"):
                seen_intercepts.append(True)
        assert terminated, "持续重复最终必须终止"
        assert seen_intercepts, "终止前必先经历二级拦截（回 LLM）"
        assert state["router.duplicate_intercepts"] >= 4  # 硬上限达成


# ═══════════════════════════════════════════════════════════
# 提醒合并 ops（不打断 tool 序列）
# ═══════════════════════════════════════════════════════════


class TestMessageMergeOps:
    def _warm_state(self, messages: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
        """预热到下次执行命中重复签名，并把 messages 重置为给定序列。"""
        plugin = DuplicateCheckPlugin()
        state = _tool_state([{"name": "bash", "arguments": {"cmd": "ls"}}])
        _merge_updates(state, _execute(plugin, state).state_updates)
        _execute(plugin, state)
        state["messages"] = messages
        return plugin, state

    @pytest.mark.parametrize("role", ["tool", "assistant", "system"])
    def test_merges_into_trailing_protected_roles_by_seq(self, role: str) -> None:
        """末尾 tool/assistant/system（带 seq）→ set modify 同 seq 合并 content，不新增消息。"""
        plugin, state = self._warm_state([{"role": role, "content": "orig", "seq": 7}])
        result = _execute(plugin, state)
        ops = _hint_ops(result.state_updates)
        assert len(ops) == 1
        assert ops[0]["op"] == "set" and ops[0]["seq"] == 7
        assert ops[0]["msg"]["content"].startswith("orig")
        assert "[DuplicateCheck]" in ops[0]["msg"]["content"]

    def test_appends_user_when_trailing_user(self) -> None:
        plugin, state = self._warm_state([{"role": "user", "content": "q", "seq": 0}])
        result = _execute(plugin, state)
        ops = _hint_ops(result.state_updates)
        assert len(ops) == 1
        assert ops[0].get("seq") is None  # append
        assert ops[0]["msg"]["role"] == "user"

    def test_untracked_trailing_message_falls_back_to_append(self) -> None:
        """末尾受保护角色但无 seq（不可定位）→ 退化为 append，绝不静默丢失提醒。"""
        plugin, state = self._warm_state([{"role": "tool", "content": "orig"}])
        result = _execute(plugin, state)
        ops = _hint_ops(result.state_updates)
        assert len(ops) == 1 and ops[0].get("seq") is None
        assert "[DuplicateCheck]" in ops[0]["msg"]["content"]


# ═══════════════════════════════════════════════════════════
# server.py 适配层
# ═══════════════════════════════════════════════════════════


def _load_server_module() -> Any:
    sys.modules.pop("plugin", None)
    mod_name = "duplicate_check_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServerAdapter:
    def _server(self) -> Any:
        srv = _load_server_module()
        srv.plugin.get_config = lambda: {"max_duplicate_calls": 1}  # type: ignore[method-assign]
        return srv

    def test_execute_intercepts_via_state_key(self) -> None:
        """OutputResult → dict 展开 state_updates；二级拦截经状态键回路由。"""
        srv = self._server()
        state: dict[str, Any] = {
            "raw_tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}}],
            "messages": [],
        }
        resp1 = _run(srv.execute(state))  # 第一次写入签名
        _merge_updates(state, resp1["state_updates"])  # 引擎口径：updates 并回 state
        resp = _run(srv.execute(state))  # 第二次 → 计数 1 ≥ max=1 → 拦截
        assert resp["state_updates"]["router.duplicate_back_llm"] is True
        assert "route_signal" not in resp
        assert resp["state_updates"]["raw_tool_calls"] == []

    def test_execute_carries_message_ops_envelope(self) -> None:
        """server 适配层原样携带 messages._ops 信封——消息修改经 state_updates 抵达引擎。"""
        srv = self._server()
        state: dict[str, Any] = {
            "raw_tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}}],
            "messages": [
                {"role": "user", "content": "go", "seq": 0},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}], "seq": 1},
            ],
        }
        resp1 = _run(srv.execute(state))
        _merge_updates(state, resp1["state_updates"])
        resp = _run(srv.execute(state))  # 拦截轮：剥 assistant + 注入警告
        assert resp["state_updates"]["raw_tool_calls"] == []
        ops = resp["state_updates"]["messages"]["_ops"]
        assert ops, "拦截轮必须经 state_updates 携带消息 ops"
        assert any(op.get("msg") is None for op in ops)  # 剥离 op
        assert any(op.get("msg") and "已跳过执行" in op["msg"].get("content", "") for op in ops)

    def test_execute_dict_result_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """core 型 dict 返回原样透传（适配层双形态契约）。"""
        srv = self._server()

        class _DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"k": "v"}

        monkeypatch.setattr(srv, "get_instance", lambda: _DictPlugin())
        resp = _run(srv.execute({"messages": []}))
        assert resp == {"k": "v"}

    def test_execute_skip_remaining_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """skip_remaining=True → 信封带 skip_remaining 标记。"""
        from pipeline.plugin import OutputResult

        srv = self._server()

        class _SkipPlugin:
            async def execute(self, ctx: Any) -> OutputResult:
                return OutputResult(state_updates={"a": 1}, skip_remaining=True)

        monkeypatch.setattr(srv, "get_instance", lambda: _SkipPlugin())
        resp = _run(srv.execute({"messages": []}))
        assert resp["state_updates"] == {"a": 1}
        assert resp["skip_remaining"] is True

    def test_on_load_preheats_and_unload_clears_cache(self) -> None:
        srv = self._server()
        _run(srv._on_load({}))
        first = srv.get_instance()
        assert srv.get_instance() is first  # 单例缓存复用
        _run(srv._on_unload({}))
        assert srv.get_instance() is not first  # 清缓存后重建
