# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""duplicate_check 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py 缺行）：

1. **轮次豁免门**：管道已结束（ended=True）不判定重复——即便本轮带着
   可判重复的工具调用也返回空更新；非 llm_call 且非 tool_execute 的
   core_type（未知/空值）同样零产出——重复判定只在 LLM 产出轮有意义。
2. **失败熔断的空输入面**：tool_execute 轮无 tool_results（键缺失或空表）
   返回空更新不动既有状态；非 dict 形态的结果条目被跳过——只有结构化
   结果才可能进入连败计数（脏 state 不污染熔断账）。
3. **工具调用描述**：无 raw_tool_calls 时描述为空串；有调用时 dict 参数
   渲染为 `k=v` 串、JSON 字符串参数原样嵌入（生产方 llm adapter 的
   两种参数形态都要可读）。
4. **剥离 ops 的不可定位防御**：Level-2 拦截剥离末尾 assistant(tool_calls)
   时，遇无 seq 消息即停止（无 seq 不可定位，不得产出无效 delete op）；
   有 seq 时按序剥离——对照组两种输入的 ops 形态可区分。
5. **评估结论 JSON 豁免**：含 evaluation_result + "passed" 的输出不判重复
   （计数恒 0，避免评估者复读被误拦）；文本超 500 字符时记账基线截断。
6. **相似度升级**：哈希不同但词级 Jaccard 相似度 > 阈值 → 重复计数递增；
   恰好等于阈值不触发（严格大于契约）；连续同文本走哈希路径计数单调递增。
7. **_compute_similarity 边界**：空串/单侧空白 → 0.0；双侧无词元 → 1.0；
   完全相同 → 1.0；完全不同 → 0.0；结果恒落在 [0,1] 且对称。

**不可达行说明（防御分支，保留）**：
- ``_build_tool_call_description`` 的 ``if not tool_calls: return ""``（448）：
  该方法的唯一生产调用点 ``_handle_duplicate_tool_calls`` 仅在
  router.duplicate_count > 0 时被 ``_do_work`` 调用，而该计数只由
  ``_check_duplicate_calls`` 产出——其无调用轮显式清零（`if not tool_calls:
  return {"router.duplicate_count": 0}`）。同一次 _do_work 执行内 ctx.state
  不会被改动，故到达该调用点时 raw_tool_calls 必非空。该守卫按契约
  （"无调用时返回空串"）以直接调用方式覆盖，非死代码但经 execute 不可达。
"""

from __future__ import annotations

import asyncio
import hashlib
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
    mod_name = "duplicate_check_gaps_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_plugin_module()
DuplicateCheckPlugin = _mod.DuplicateCheckPlugin

from pipeline.plugin import PluginContext  # noqa: E402


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _updates(plugin: Any, state: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(plugin.execute(_ctx(state))).state_updates


def _merge_updates(state: dict[str, Any], updates: dict[str, Any]) -> None:
    """模拟引擎 merge 口径：messages 走 slot ops apply，其余键平插。"""
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


def _ops_of(updates: dict[str, Any]) -> list[dict[str, Any]]:
    return list(updates.get("messages", {}).get("_ops", []))


def _call(tool: str = "bash", args: Any = None) -> dict[str, Any]:
    return {"name": tool, "arguments": {"cmd": "ls"} if args is None else args}


# ═══════════════════════════════════════════════════════════
# 1. 轮次豁免门
# ═══════════════════════════════════════════════════════════


class TestRoundGates:
    """ended / core_type 两道轮次豁免门：不改状态、不产路由。"""

    def _dup_prone_state(self, **extra: Any) -> dict[str, Any]:
        """带可判重复工具调用的 state（对照组无 ended 时必产计数）。"""
        state: dict[str, Any] = {
            "raw_tool_calls": [_call()],
            "raw_result": "正文",
            "messages": [],
            "router.recent_tool_sigs": ["deadbeef"],
        }
        state.update(extra)
        return state

    def test_ended_pipeline_returns_no_updates(self) -> None:
        """管道已结束：post-end 阶段不判定重复，返回空更新。"""
        updates = _updates(DuplicateCheckPlugin(), self._dup_prone_state(ended=True))
        assert updates == {}

    def test_ended_gate_is_the_only_difference(self) -> None:
        """对照：同一 state 去掉 ended 后正常产出计数与窗口。"""
        plugin = DuplicateCheckPlugin()
        live = _updates(plugin, self._dup_prone_state())
        assert "router.duplicate_count" in live
        assert "router.recent_tool_sigs" in live

    @pytest.mark.parametrize("core_type", ["unknown_target", "tool_result", ""])
    def test_unknown_core_type_returns_no_updates(self, core_type: str) -> None:
        """非 llm_call / 非 tool_execute 的轮次零产出（重复判定只属 LLM 产出轮）。"""
        updates = _updates(
            DuplicateCheckPlugin(),
            self._dup_prone_state(core_type=core_type),
        )
        assert updates == {}

    def test_llm_call_round_is_judged(self) -> None:
        """对照：同一 state 标 llm_call 后判定生效。"""
        updates = _updates(
            DuplicateCheckPlugin(), self._dup_prone_state(core_type="llm_call")
        )
        assert updates["router.duplicate_count"] == 0


# ═══════════════════════════════════════════════════════════
# 2. 失败熔断的空输入面
# ═══════════════════════════════════════════════════════════


class TestFailBreakerInputs:
    """tool_execute 轮的连败账只认结构化结果。"""

    @pytest.mark.parametrize("state", [{}, {"tool_results": []}])
    def test_no_tool_results_is_noop(self, state: dict[str, Any]) -> None:
        """键缺失/空表 → 空更新（不动既有状态）。"""
        updates = _updates(
            DuplicateCheckPlugin(), {"core_type": "tool_execute", "messages": [], **state}
        )
        assert updates == {}

    def test_existing_streak_preserved_when_no_results(self) -> None:
        """对照：连败账已在 state 时，空结果轮不写回（不重置也不重复落账）。"""
        plugin = DuplicateCheckPlugin()
        state = {
            "core_type": "tool_execute",
            "tool_results": [],
            "messages": [],
            "router.tool_fail_streak": {"a": 2},
        }
        assert _updates(plugin, state) == {}
        assert state["router.tool_fail_streak"] == {"a": 2}

    @pytest.mark.parametrize("junk", ["junk", 42, None, ["d"]])
    def test_non_dict_result_entries_skipped(self, junk: Any) -> None:
        """非 dict 结果条目被跳过：脏 state 不进连败账。"""
        plugin = DuplicateCheckPlugin()
        state = {
            "core_type": "tool_execute",
            "tool_results": [junk, {"tool_name": "a", "success": False}],
            "messages": [],
        }
        updates = _updates(plugin, state)
        assert updates["router.tool_fail_streak"] == {"a": 1}

    def test_non_dict_entries_do_not_break_accounting(self) -> None:
        """对照：dict 条目夹在脏条目之间时逐个入账（跳过 ≠ 中断）。"""
        plugin = DuplicateCheckPlugin()
        state = {
            "core_type": "tool_execute",
            "tool_results": [
                {"tool_name": "a", "success": False},
                "junk",
                {"tool_name": "a", "success": False},
            ],
            "messages": [],
        }
        updates = _updates(plugin, state)
        assert updates["router.tool_fail_streak"] == {"a": 2}


# ═══════════════════════════════════════════════════════════
# 3. 工具调用描述
# ═══════════════════════════════════════════════════════════


class TestToolCallDescription:
    """描述渲染：无调用空串；dict 参数 k=v；JSON 字符串参数原样嵌入。"""

    def test_empty_tool_calls_yield_empty_description(self) -> None:
        """无 raw_tool_calls → 空串（经 execute 不可达，见模块 docstring）。"""
        plugin = DuplicateCheckPlugin()
        assert plugin._build_tool_call_description(_ctx({"raw_tool_calls": []})) == ""

    def test_missing_key_yields_empty_description(self) -> None:
        """键缺失同样空串（默认 [] 语义与显式空表一致）。"""
        plugin = DuplicateCheckPlugin()
        assert plugin._build_tool_call_description(_ctx({})) == ""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({"path": "a.py", "mode": "r"}, "file_read(path=a.py, mode=r)"),
            ('{"path": "a.py"}', 'file_read({"path": "a.py"})'),
        ],
    )
    def test_description_renders_both_argument_forms(
        self, arguments: Any, expected: str
    ) -> None:
        """两种参数形态都可读渲染——dict 展开、字符串原样嵌入。"""
        plugin = DuplicateCheckPlugin()
        ctx = _ctx({"raw_tool_calls": [{"name": "file_read", "arguments": arguments}]})
        assert plugin._build_tool_call_description(ctx) == expected

    def test_multiple_calls_joined_and_order_preserved(self) -> None:
        """性质：多调用按序以顿号连接，条目数与调用数一致。"""
        plugin = DuplicateCheckPlugin()
        ctx = _ctx(
            {
                "raw_tool_calls": [
                    {"name": "a", "arguments": {"i": 1}},
                    {"name": "b", "arguments": {"i": 2}},
                    {"name": "c", "arguments": {"i": 3}},
                ]
            }
        )
        desc = plugin._build_tool_call_description(ctx)
        assert desc.count("、") == 2
        assert [part.split("(")[0] for part in desc.split("、")] == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════
# 4. 剥离 ops 的不可定位防御
# ═══════════════════════════════════════════════════════════


class TestStripOpsUnlocatableStop:
    """Level-2 拦截：末尾 assistant(tool_calls) 无 seq 时不得产出无效 delete op。"""

    def _warm_duplicate(self) -> tuple[Any, dict[str, Any]]:
        """预热到「本次调用签名与上次相同」边界（第二次执行即计重复）。"""
        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 1})
        state: dict[str, Any] = {"raw_tool_calls": [_call()], "messages": []}
        _merge_updates(state, _updates(plugin, state))  # 轮1：签名入窗
        return plugin, state

    @pytest.mark.parametrize(
        ("seq", "expect_delete"),
        [(None, False), (3, True)],
    )
    def test_unlocatable_assistant_stops_strip(
        self, seq: int | None, expect_delete: bool
    ) -> None:
        """无 seq → 停止剥离（ops 无 delete）；有 seq → 按序剥离（ops 含 delete）。"""
        plugin, state = self._warm_duplicate()
        trailing: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1"}],
        }
        if seq is not None:
            trailing["seq"] = seq
        state["messages"] = [{"role": "user", "content": "go", "seq": 0}, trailing]

        updates = _updates(plugin, state)
        ops = _ops_of(updates)
        deletes = [op for op in ops if op.get("msg") is None]
        appends = [op for op in ops if op.get("seq") is None]
        assert bool(deletes) is expect_delete
        # 拦截语义不受剥离结果影响：调用被清空、拦截计数递增、路由回 LLM
        assert updates["raw_tool_calls"] == []
        assert updates["router.duplicate_intercepts"] == 1
        assert updates["router.duplicate_back_llm"] is True
        # 提醒恒定可达（绝不静默丢失）：不可定位时退化为 append
        assert len(appends) == 1
        assert "[DuplicateCheck]" in appends[0]["msg"]["content"]


# ═══════════════════════════════════════════════════════════
# 5. 评估结论 JSON 豁免 与 相似度升级
# ═══════════════════════════════════════════════════════════


class TestRepetitiveOutputBranches:
    """输出重复的豁免面（评估结论）与相似度面（哈希不同但高度相似）。"""

    def _state(self, raw_result: Any, **extra: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            "raw_tool_calls": [],
            "raw_result": raw_result,
            "messages": [],
        }
        state.update(extra)
        return state

    def test_evaluation_json_output_never_counts_as_repetitive(self) -> None:
        """评估结论 JSON 不判重复：同一文本连跑两轮计数恒 0、无拦截路由。"""
        plugin = DuplicateCheckPlugin()
        text = '评估完成：{"evaluation_result": {"passed": true, "score": 90}}'
        state = self._state(text)
        first = _updates(plugin, state)
        _merge_updates(state, first)
        second = _updates(plugin, state)

        assert first["router.repetitive_count"] == 0
        assert second["router.repetitive_count"] == 0, "复读评估结论不得升级为重复"
        assert second.get("router.duplicate_back_llm") is not True
        assert "messages" not in second

    def test_same_text_without_evaluation_marker_does_count(self) -> None:
        """对照：同形态文本去掉评估标记后，第二轮即计重复（豁免确有区分度）。"""
        plugin = DuplicateCheckPlugin()
        text = '评估完成：{"result": {"passed": true, "score": 90}}'
        state = self._state(text)
        _merge_updates(state, _updates(plugin, state))
        second = _updates(plugin, state)
        assert second["router.repetitive_count"] == 1

    def test_evaluation_json_baseline_truncated_to_500(self) -> None:
        """记账基线截断：last_response_text 取前 500 字符、哈希对应该截断文本。"""
        plugin = DuplicateCheckPlugin()
        prose = "x" * 600
        text = prose + '{"evaluation_result": {"passed": false}}'
        updates = _updates(plugin, self._state(text))
        recorded = updates["router.last_response_text"]
        assert len(recorded) == 500
        assert recorded.startswith("x" * 500)
        assert updates["router.last_response"] == hashlib.md5(recorded.encode()).hexdigest()[:8]

    def test_high_similarity_distinct_text_escalates(self) -> None:
        """哈希不同但词级 Jaccard > 阈值 → 重复计数递增（10/11 ≈ 0.909 > 0.9）。"""
        plugin = DuplicateCheckPlugin()
        shared = " ".join(f"w{i}" for i in range(10))
        state = self._state(
            shared + " extra",
            **{
                "router.last_response": "00000000",  # 与当前文本哈希必不同
                "router.last_response_text": shared,
            },
        )
        updates = _updates(plugin, state)
        assert updates["router.repetitive_count"] == 1
        assert updates["router.last_response_text"] == shared + " extra"

    def test_similarity_exactly_at_threshold_does_not_escalate(self) -> None:
        """边界：相似度恰好等于阈值不触发（严格大于契约）→ 计数重置 0。"""
        plugin = DuplicateCheckPlugin()
        shared = " ".join(f"w{i}" for i in range(9))  # 9/10 == 0.9 == 阈值
        state = self._state(
            shared + " extra",
            **{
                "router.last_response": "00000000",
                "router.last_response_text": shared,
                "router.repetitive_count": 3,
            },
        )
        updates = _updates(plugin, state)
        assert updates["router.repetitive_count"] == 0

    def test_repetitive_count_monotonic_across_rounds(self) -> None:
        """性质：连续相同输出计数逐轮递增（哈希路径），达阈值轮升级为拦截。

        第 1 轮无历史基线（相似度分支因 last_text 为空不触发）→ 计数 0；
        第 2、3 轮命中哈希 → 1、2（软提示，调用不清零）；第 4 轮计数 3 达阈值
        → 清空输出 + 计数归零 + 路由回 LLM。
        """
        plugin = DuplicateCheckPlugin(config={"max_repetitive_output": 3})
        state = self._state("同一段输出文本")
        counts = []
        for _ in range(3):
            updates = _updates(plugin, state)
            _merge_updates(state, updates)
            counts.append(updates["router.repetitive_count"])
        assert counts == [0, 1, 2]
        assert counts == sorted(counts)

        intercept = _updates(plugin, state)
        assert intercept["router.repetitive_count"] == 0
        assert intercept["router.duplicate_intercepts"] == 1
        assert intercept["router.duplicate_back_llm"] is True
        assert intercept["raw_result"] == ""

    def test_disjoint_output_resets_counter(self) -> None:
        """对照：完全不同输出 → 计数从历史值重置为 0。"""
        plugin = DuplicateCheckPlugin()
        state = self._state(
            "alpha beta gamma",
            **{
                "router.last_response": "00000000",
                "router.last_response_text": "delta epsilon zeta",
                "router.repetitive_count": 2,
            },
        )
        assert _updates(plugin, state)["router.repetitive_count"] == 0


# ═══════════════════════════════════════════════════════════
# 6. _compute_similarity 边界与性质
# ═══════════════════════════════════════════════════════════


class TestComputeSimilarity:
    """词级 Jaccard 相似度：空输入/无词元/相同/相异四类边界的返回值契约。"""

    @pytest.mark.parametrize(
        ("text1", "text2", "expected"),
        [
            ("", "abc", 0.0),
            ("abc", "", 0.0),
            ("   ", "abc def", 0.0),  # 单侧无词元
            ("abc def", "\t\n", 0.0),
            ("   ", "\t\t", 1.0),  # 双侧无词元（空白不算词）
            ("a b", "a b", 1.0),
            ("a b c", "x y z", 0.0),
            ("a b", "a b c", 2 / 3),
        ],
    )
    def test_boundary_values(self, text1: str, text2: str, expected: float) -> None:
        plugin = DuplicateCheckPlugin()
        assert plugin._compute_similarity(text1, text2) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text1", "text2"),
        [
            ("alpha beta", "alpha beta gamma"),
            ("", "x"),
            ("one two three", "four five six seven"),
        ],
    )
    def test_similarity_is_bounded_and_symmetric(self, text1: str, text2: str) -> None:
        """性质：值域 [0,1] 且对称 f(x,y)==f(y,x)。"""
        plugin = DuplicateCheckPlugin()
        forward = plugin._compute_similarity(text1, text2)
        backward = plugin._compute_similarity(text2, text1)
        assert 0.0 <= forward <= 1.0
        assert forward == pytest.approx(backward)

    def test_similarity_reached_through_execute_path(self) -> None:
        """执行路径同样能触发相似度分支（空白基线 + 有词输出 → 0.0 → 清零）。"""
        plugin = DuplicateCheckPlugin()
        state = {
            "raw_tool_calls": [],
            "raw_result": "word output",
            "messages": [],
            "router.last_response": "00000000",
            "router.last_response_text": "   ",
            "router.repetitive_count": 2,
        }
        assert _updates(plugin, state)["router.repetitive_count"] == 0
