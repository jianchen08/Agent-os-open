# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""duplicate_check 插件行为测试——签名计数/三级渐进/消息合并/server 适配层。

契约（plugin.py 头注）：
- 工具调用重复：同名+同参数签名（str 参数 JSON 归一）与上次相同 → 计数+1，不同重置
- 输出重复：前 500 字符哈希相同，或词级 Jaccard 相似度 > 阈值 → 计数+1
- 三级渐进：软提示（计数<阈值，调用仍执行）→ 拦截（清空调用/输出+计数归零+
  拦截数+1+路由回 LLM）→ 终止（拦截数≥硬上限；主 agent 追加用户通知，子 agent 直接终止）
- 提醒合并：不打断 assistant(tool_calls)→tool 序列——末尾 tool/assistant/system
  合并 content，空/末尾 user 追加 user
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


# ═══════════════════════════════════════════════════════════
# 签名与计数（重复检测的判定层）
# ═══════════════════════════════════════════════════════════


class TestSignatureCounting:
    def test_no_tool_calls_and_no_result_is_noop(self) -> None:
        """空状态 → 无更新、无路由（不误伤正常流）。"""
        plugin = DuplicateCheckPlugin()
        result = _execute(plugin, {"messages": []})
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_same_tool_args_increment_count_across_iterations(self) -> None:
        """同名同参连续两次 → 第二次计数 1（引擎侧合并 updates 的真实迭代口径）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "file_read", "arguments": {"path": "a.py"}}])
        state.update(_execute(plugin, state).state_updates)  # 第一次：写入签名
        state.update(_execute(plugin, state).state_updates)  # 第二次：命中
        assert state["router.duplicate_count"] == 1

    def test_different_args_reset_count(self) -> None:
        """参数变化 → 计数归零（换方法即豁免）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "file_read", "arguments": {"path": "a.py"}}])
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1
        state["raw_tool_calls"] = [{"name": "file_read", "arguments": {"path": "b.py"}}]
        state.update(_execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 0

    @pytest.mark.parametrize(
        ("first_args", "second_args"),
        [
            ('{"path": "a.py"}', {"path": "a.py"}),  # str JSON 与 dict 等价
            ("not-json", {}),  # 非法 JSON 与空参数等价
            (["x", "y"], {}),  # 非 dict 参数与空参数等价
        ],
    )
    def test_argument_normalization_makes_equivalent_signatures(
        self, first_args: Any, second_args: Any
    ) -> None:
        """str/非法 JSON/非 dict 参数归一后与等价 dict 同签名 → 计数递增。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "t", "arguments": first_args}])
        state.update(_execute(plugin, state).state_updates)
        state["raw_tool_calls"] = [{"name": "t", "arguments": second_args}]
        state.update(_execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1

    def test_multi_call_signature_is_order_sensitive(self) -> None:
        """多调用签名按顺序拼接：顺序互换视为不同调用序列 → 重置。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        calls_ab = [
            {"name": "a", "arguments": {"x": 1}},
            {"name": "b", "arguments": {"y": 2}},
        ]
        state = _tool_state(calls_ab)
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 1
        state["raw_tool_calls"] = list(reversed(calls_ab))
        state.update(_execute(plugin, state).state_updates)
        assert state["router.duplicate_count"] == 0


class TestRepetitiveOutput:
    def test_identical_output_increments_count(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {"raw_result": "same answer", "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 1

    def test_signature_truncated_to_first_500_chars(self) -> None:
        """性质断言：仅前 500 字符参与签名——第 501 字符起不同不改变判定。"""
        plugin = DuplicateCheckPlugin()
        base = "x" * 500
        state: dict[str, Any] = {"raw_result": base + "aaa", "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state["raw_result"] = base + "bbb"
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 1

    def test_word_level_similarity_above_threshold_counts(self) -> None:
        """词级 Jaccard：20 词仅 1 词不同（19/21≈0.905>0.9）→ 计数。"""
        plugin = DuplicateCheckPlugin()
        words = [f"w{i}" for i in range(20)]
        text1 = " ".join(words)
        text2 = " ".join(words[:9] + ["different"] + words[10:])
        state: dict[str, Any] = {"raw_result": text1, "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state["raw_result"] = text2  # 哈希必不等 → 走相似度路径
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 1

    def test_clearly_different_output_resets(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {"raw_result": "alpha beta gamma", "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 1
        state["raw_result"] = "completely unrelated content here"
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 0


class TestExemptionGates:
    """豁免门：ENDED / core_type / evaluation_result。"""

    def test_ended_state_skips_all_detection(self) -> None:
        """管道已结束（ENDED=True）→ 不判定，不写任何计数。"""
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "ended": True,
            "raw_tool_calls": [{"name": "file_read", "arguments": {"path": "/a"}}],
            "raw_result": "same",
            "messages": [],
        }
        result = _execute(plugin, state)
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_tool_execute_core_type_skips_output_detection(self) -> None:
        """core_type=tool_execute → 工具结果文本不参与输出重复判定。"""
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "core_type": "tool_execute",
            "raw_result": "tool output text",
            "messages": [],
        }
        result = _execute(plugin, state)
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_evaluation_result_json_exempt_from_repetition(self) -> None:
        """含 evaluation_result + passed 的 JSON 输出不判重复（只更新哈希不计数）。"""
        plugin = DuplicateCheckPlugin()
        payload = '{"evaluation_result": {"passed": true, "score": 0.9}}'
        state: dict[str, Any] = {"raw_result": payload, "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 0
        assert state["router.last_response"]  # 哈希仍更新，供后续非豁免输出比对

    def test_evaluation_result_without_passed_still_detected(self) -> None:
        """evaluation_result 但无 passed 键 → 不豁免，正常重复判定。"""
        plugin = DuplicateCheckPlugin()
        payload = '{"evaluation_result": {"score": 0.9}}'
        state: dict[str, Any] = {"raw_result": payload, "messages": []}
        state.update(_execute(plugin, state).state_updates)
        state.update(_execute(plugin, state).state_updates)
        assert state["router.repetitive_count"] == 1

    @pytest.mark.parametrize(
        ("t1", "t2", "expected"),
        [
            ("a b c", "a b c", 1.0),
            ("a b c", "x y z", 0.0),
            # 空串走首守卫返 0.0（both-empty→1.0 分支只对非空但无词文本可达）
            ("", "", 0.0),
            ("a b", "", 0.0),
            (" ", " ", 1.0),  # 非空文本 split 后双空词集 → 1.0
            (" ", "a b", 0.0),  # 单侧无词 → 0.0
        ],
    )
    def test_similarity_edge_values(self, t1: str, t2: str, expected: float) -> None:
        plugin = DuplicateCheckPlugin()
        assert plugin._compute_similarity(t1, t2) == expected

    def test_similarity_monotone_with_overlap(self) -> None:
        """性质断言：重叠词越多相似度单调不降。"""
        plugin = DuplicateCheckPlugin()
        base = " ".join(f"w{i}" for i in range(10))
        half = " ".join(f"z{i}" for i in range(5)) + " " + " ".join(base.split()[:5])
        s_half = plugin._compute_similarity(base, half)
        s_full = plugin._compute_similarity(base, base)
        assert 0.0 < s_half < 1.0
        assert s_half < s_full == 1.0


# ═══════════════════════════════════════════════════════════
# 三级渐进策略
# ═══════════════════════════════════════════════════════════


class TestEscalationLevels:
    def _dup_state(self, **config: Any) -> tuple[Any, dict[str, Any]]:
        """预热到「本次工具调用签名与上次相同、计数仍为 0」的边界。

        只并入第一次执行的 updates（写入签名）；第二次执行命中签名（计数
        存在于其返回值、不并入 state），并清掉预热注入的提示消息。
        """
        plugin = DuplicateCheckPlugin(config=config or None)
        calls = [{"name": "bash", "arguments": {"cmd": "ls"}}]
        state = _tool_state(calls)
        state.update(_execute(plugin, state).state_updates)
        _execute(plugin, state)
        state["messages"] = []
        return plugin, state

    def test_level1_soft_hint_keeps_tool_calls(self) -> None:
        """第一级：计数 1<3 → 软提示注入 messages，工具调用不动、不路由。"""
        plugin, state = self._dup_state()
        result = _execute(plugin, state)
        assert result.route_signal is None
        assert "raw_tool_calls" not in result.state_updates  # 调用保留
        msgs = state["messages"]
        assert msgs and msgs[-1]["role"] == "user"
        assert "[DuplicateCheck]" in msgs[-1]["content"]
        assert "1 次" in msgs[-1]["content"]  # 计数入提示

    def test_level1_hint_template_escalates_wording(self) -> None:
        """性质断言：计数升高提示措辞升级（第 2 次出现「立即停止」）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "bash", "arguments": {"cmd": "ls"}}])
        state.update(_execute(plugin, state).state_updates)  # 写入签名
        result = _execute(plugin, state)  # count=1 → 提示 1
        state.update(result.state_updates)  # 计数并入（引擎口径）
        assert "考虑换一种方式" in state["messages"][-1]["content"]
        assert "立即停止" not in state["messages"][-1]["content"]
        state["messages"] = []
        _execute(plugin, state)  # count=2 → 提示 2
        assert "立即停止" in state["messages"][-1]["content"]

    def test_level2_intercepts_and_reroutes(self) -> None:
        """第二级：计数≥阈值 → 清空调用、计数归零、拦截+1、路由回 LLM。"""
        plugin, state = self._dup_state(max_duplicate_calls=1)
        result = _execute(plugin, state)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert "1" in result.route_signal.reason
        assert result.state_updates["raw_tool_calls"] == []
        assert result.state_updates["router.duplicate_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    def test_level2_strips_trailing_tool_call_assistants(self) -> None:
        """拦截时同步剥离末尾连续 assistant(tool_calls)，避免未配对消息。"""
        plugin, state = self._dup_state(max_duplicate_calls=1)
        state["messages"] = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t2"}]},
        ]
        _execute(plugin, state)
        roles = [m["role"] for m in state["messages"]]
        # 两条 assistant(tool_calls) 被剥离；随后强警告以 user 追加（末尾非受保护角色）
        assert roles == ["user", "user"]
        assert "已跳过执行" in state["messages"][-1]["content"]

    def test_level3_main_agent_notifies_and_ends(self) -> None:
        """第三级（主 agent）：追加用户通知消息 + 终止路由。"""
        plugin, state = self._dup_state(
            max_duplicate_calls=1, hard_limit_intercepts=1
        )
        state["router.duplicate_intercepts"] = 1
        state["agent_level"] = "L1"
        result = _execute(plugin, state)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert state["messages"][-1]["role"] == "assistant"
        assert "重复" in state["messages"][-1]["content"]

    def test_level3_sub_agent_ends_silently(self) -> None:
        """第三级（子 agent）：不注入消息，直接终止。"""
        plugin, state = self._dup_state(
            max_duplicate_calls=1, hard_limit_intercepts=1
        )
        state["router.duplicate_intercepts"] = 1
        state["agent_level"] = "L2"
        state["delegate_depth"] = 1
        n_msgs = len(state["messages"])
        result = _execute(plugin, state)
        assert result.route_signal.route_type == "end"
        assert len(state["messages"]) == n_msgs  # 无通知注入

    def test_repetitive_level2_clears_raw_result(self) -> None:
        """输出重复第二级：raw_result 清空 + 计数归零 + 路由回 LLM。"""
        plugin = DuplicateCheckPlugin(config={"max_repetitive_output": 1})
        state: dict[str, Any] = {"raw_result": "same", "messages": []}
        state.update(_execute(plugin, state).state_updates)
        result = _execute(plugin, state)
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["raw_result"] == ""
        assert result.state_updates["router.repetitive_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    def test_repetitive_level1_hint_only(self) -> None:
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {"raw_result": "same", "messages": []}
        state.update(_execute(plugin, state).state_updates)
        result = _execute(plugin, state)
        assert result.route_signal is None
        assert "raw_result" not in result.state_updates
        assert "相似内容" in state["messages"][-1]["content"]

    def test_repetitive_level3_terminates(self) -> None:
        """输出重复第三级：拦截数达硬上限 → 终止（与工具路径同构）。"""
        plugin = DuplicateCheckPlugin(
            config={"max_repetitive_output": 1, "hard_limit_intercepts": 1}
        )
        state: dict[str, Any] = {"raw_result": "same", "messages": [], "agent_level": "L2", "delegate_depth": 1}
        state.update(_execute(plugin, state).state_updates)
        state["router.duplicate_intercepts"] = 1
        result = _execute(plugin, state)
        assert result.route_signal.route_type == "end"
        assert "重复输出" in result.route_signal.reason  # 终止原因带重复维度描述

    def test_hint_shows_tool_with_string_arguments(self) -> None:
        """str 参数（合法 JSON）同样参与签名；提示文本带工具名。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 99})
        state = _tool_state([{"name": "bash", "arguments": '{"cmd": "ls"}'}])
        state.update(_execute(plugin, state).state_updates)
        _execute(plugin, state)
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
        seen_route_types: list[str] = []
        terminated = False
        for _ in range(24):
            # 模拟顽固 LLM：每轮重发同一工具调用（拦截清空后由引擎从模型重新获得）
            state["raw_tool_calls"] = calls
            result = _execute(plugin, state)
            state.update(result.state_updates)
            if result.route_signal is not None:
                seen_route_types.append(result.route_signal.route_type)
                if result.route_signal.route_type == "end":
                    terminated = True
                    break
        assert terminated, "持续重复最终必须终止"
        assert seen_route_types[0] == "next_llm"  # 先经历拦截
        assert seen_route_types[-1] == "end"
        assert state["router.duplicate_intercepts"] >= 4  # 硬上限达成


# ═══════════════════════════════════════════════════════════
# 提醒合并语义（不打断 tool 序列）
# ═══════════════════════════════════════════════════════════


class TestMessageMerge:
    def _warm_state(self, messages: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
        """预热到下次执行命中重复签名，并把 messages 重置为给定序列。"""
        plugin = DuplicateCheckPlugin()
        state = _tool_state([{"name": "bash", "arguments": {"cmd": "ls"}}])
        state.update(_execute(plugin, state).state_updates)
        _execute(plugin, state)
        state["messages"] = messages
        return plugin, state

    @pytest.mark.parametrize("role", ["tool", "assistant", "system"])
    def test_merges_into_trailing_protected_roles(self, role: str) -> None:
        """末尾 tool/assistant/system → 合并 content，不新增消息（序列长度不变）。"""
        plugin, state = self._warm_state([{"role": role, "content": "orig"}])
        _execute(plugin, state)
        assert len(state["messages"]) == 1
        assert state["messages"][0]["content"].startswith("orig")
        assert "[DuplicateCheck]" in state["messages"][0]["content"]

    def test_appends_user_when_trailing_user(self) -> None:
        plugin, state = self._warm_state([{"role": "user", "content": "q"}])
        _execute(plugin, state)
        assert [m["role"] for m in state["messages"]] == ["user", "user"]


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

    def test_execute_expands_route_signal(self) -> None:
        """OutputResult → dict 展开 state_updates + route_signal 序列化。"""
        srv = self._server()
        state: dict[str, Any] = {
            "raw_tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}}],
            "messages": [],
        }
        resp1 = _run(srv.execute(state))  # 第一次写入签名
        state.update(resp1["state_updates"])  # 引擎口径：updates 并回 state
        resp = _run(srv.execute(state))  # 第二次 → 计数 1 ≥ max=1 → 拦截
        assert resp["route_signal"]["route_type"] == "next_llm"
        assert "reason" in resp["route_signal"]
        assert resp["state_updates"]["raw_tool_calls"] == []

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
