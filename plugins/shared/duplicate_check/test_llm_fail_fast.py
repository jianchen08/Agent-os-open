# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""duplicate_check LLM 失败连击熔断测试（问题 4：连环失败无快速失败）。

契约（2026-10-20 用户裁定）：

- 连续 **identical** 的 llm_core 调用失败 → fail-fast：终态原因如实写
  ``model_unavailable``（模型不可用类）/ ``llm_fail_loop``（其他同类失败），
  **不进**重复输出检测的计数（"同一错误反复"与"输出重复"是两类事）。
- 失败轮的 ``raw_result`` 是上一轮残留（同一文本）——若计入重复输出会被
  误诊 ``duplicate_loop``（2026-10-20 事故：15 条同文 LLM 错误终态被写成
  duplicate_loop，与真实死因不符）。
- 失败计数来自引擎 ``_plugin_error_stats``（run 开头清空、run 内按
  `(plugin_id, message)` 聚合成 count）：本轮新增计数 = 失败轮；成功轮无
  新增 → 连击计数清零（自愈不误伤）。
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
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "duplicate_check_llm_fail_fast_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_plugin_module()
DuplicateCheckPlugin = _MOD.DuplicateCheckPlugin

_MODEL_UNAVAILABLE_MSG = (
    "MODEL_UNAVAILABLE: 模型 model-a 不在可用列表（llm.yaml models 未命中），"
    "拒绝以其他模型静默回落继续对话"
)
_TRANSIENT_MSG = "MCP tool call failed: llm_core: 上游连接超时"


def _quota_429_msg(request_id: str) -> str:
    """2026-10-23 装机实证形态：额度 429 误包进连接错误，每条 request_id 不同。"""
    return (
        "MCP call failed: tool call failed: llm_core.execute: MCP protocol error: "
        "[0] llm.complete_stream 工具执行失败: LLM 供应商调用失败"
        "[网络连接失败（请检查网络与 api_base 端点）]（model=MiniMax-M3.1-Flash）: "
        'APIConnectionError: litellm.APIConnectionError: MinimaxException - '
        '{"type":"error","error":{"type":"rate_limit_error","message":'
        '"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)",'
        f'"http_code":"429"}},"request_id":"{request_id}"}}'
    )


def _make_ctx(state: dict[str, Any]) -> Any:
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=state, config={})


def _exec_updates(plugin: Any, state: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(plugin.execute(_make_ctx(state))).state_updates


def _llm_error(message: str) -> dict[str, Any]:
    """单次失败记录（按插件执行序交给 ``_state`` 聚合）。"""
    return {
        "plugin_id": "pipeline_llm_core",
        "code": "MCP_CALL_FAILED",
        "message": message,
    }


def _error_stats(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把失败记录聚合成引擎账本形态（同 (plugin_id, message) 累加 count，
    seq 按执行序单调递增）——与 finalize::record_plugin_error 同口径。"""
    stats: list[dict[str, Any]] = []
    seq = 0
    for err in errors:
        seq += 1
        entry = next(
            (
                e
                for e in stats
                if e["plugin_id"] == err["plugin_id"] and e["message"] == err["message"]
            ),
            None,
        )
        if entry is None:
            stats.append(
                {
                    "plugin_id": err["plugin_id"],
                    "code": err.get("code") or "PLUGIN_EXEC_FAILED",
                    "message": err["message"],
                    "count": 1,
                    "seq": seq,
                }
            )
        else:
            entry["count"] += 1
            entry["seq"] = seq
    return stats


def _fingerprint(message: str) -> str:
    """插件侧错误指纹口径（sha1 前 12 位）——跨 run 残留连击的同文判定用。"""
    import hashlib

    return hashlib.sha1(message.encode("utf-8", "replace")).hexdigest()[:12]


def _state(errors: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [],
        "raw_result": "上一轮残留文本",
        "raw_tool_calls": [],
        "tool_results": [],
        "run_id": "run-1",
        "_plugin_error_stats": _error_stats(errors),
    }
    state.update(extra)
    return state


class TestLlmFailFast:
    """LLM 失败连击：identical 失败快速失败，终态原因不写 duplicate_loop。"""

    def test_identical_model_unavailable_failures_fail_fast(self) -> None:
        """连续 3 次同文模型不可用 → 快速失败，署名 model_unavailable。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_MODEL_UNAVAILABLE_MSG)] * 3, agent_level="L1")

        updates = _exec_updates(plugin, state)

        assert updates["should_stop"] is True
        assert updates["router.stop_reason"] == "model_unavailable"
        assert updates["router.stop_reason"] != "duplicate_loop"
        ops = updates["messages"]["_ops"]
        assert any("模型不可用" in str(op) for op in ops)

    def test_below_threshold_keeps_running(self) -> None:
        """未达阈值（2 次）→ 只记账，不终止（留自愈空间）。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_MODEL_UNAVAILABLE_MSG)] * 2, agent_level="L1")

        updates = _exec_updates(plugin, state)

        assert updates.get("should_stop") is not True
        assert "router.stop_reason" not in updates
        assert updates["router.llm_fail_streak"]["count"] == 2

    def test_transient_failures_use_higher_threshold(self) -> None:
        """非模型不可用类（同文瞬态失败）→ 阈值更高（5），3 次不终止。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_TRANSIENT_MSG)] * 3, agent_level="L1")

        updates = _exec_updates(plugin, state)

        assert updates.get("should_stop") is not True
        assert updates["router.llm_fail_streak"]["count"] == 3

    def test_transient_failures_fail_fast_at_hard_threshold(self) -> None:
        """瞬态类连败达阈值（5）→ 快速失败，署名 llm_fail_loop。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_TRANSIENT_MSG)] * 5, agent_level="L1")

        updates = _exec_updates(plugin, state)

        assert updates["should_stop"] is True
        assert updates["router.stop_reason"] == "llm_fail_loop"

    def test_different_failures_do_not_accumulate(self) -> None:
        """错误文本不同 → 连击不累加（不是 identical 不算连环失败）。"""
        plugin = DuplicateCheckPlugin()
        state = _state(
            [
                _llm_error(_MODEL_UNAVAILABLE_MSG),
                _llm_error("别的错误 A"),
                _llm_error("别的错误 B"),
            ],
            agent_level="L1",
        )

        updates = _exec_updates(plugin, state)

        assert updates["router.llm_fail_streak"]["count"] == 1
        assert updates.get("should_stop") is not True

    def test_failure_rounds_not_counted_as_repetitive_output(self) -> None:
        """失败轮不计入重复输出（raw_result 是残留文本，不是模型输出）。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_MODEL_UNAVAILABLE_MSG)] * 2, agent_level="L1")

        updates = _exec_updates(plugin, state)

        assert updates["router.repetitive_count"] == 0
        assert "messages" not in updates, "失败轮不得注入重复输出提示"

    def test_success_round_resets_streak(self) -> None:
        """成功轮（无新增错误）→ 连击清零；后续再失败从 1 起计。"""
        plugin = DuplicateCheckPlugin()
        state = _state([_llm_error(_MODEL_UNAVAILABLE_MSG)] * 2, agent_level="L1")
        first = _exec_updates(plugin, state)
        assert first["router.llm_fail_streak"]["count"] == 2

        # 成功轮：引擎错误列表不再增长
        state = _state([_llm_error(_MODEL_UNAVAILABLE_MSG)] * 2, agent_level="L1")
        state["router.llm_fail_streak"] = first["router.llm_fail_streak"]
        state["router.llm_error_cursor"] = first["router.llm_error_cursor"]
        second = _exec_updates(plugin, state)
        assert second["router.llm_fail_streak"]["count"] == 0

    def test_new_run_resets_cursor(self) -> None:
        """跨 run：run_id 变化 → 错误游标与连击计数归零（新 run 从头计）。"""
        plugin = DuplicateCheckPlugin()
        state = _state(
            [_llm_error(_MODEL_UNAVAILABLE_MSG)] * 2,
            agent_level="L1",
            run_id="run-2",
        )
        # 上一 run 的残留：游标与连击都指向 run-1（连击指纹与本轮错误同文）
        state["router.llm_error_cursor"] = {"run_id": "run-1", "count": 2}
        state["router.llm_fail_streak"] = {
            "run_id": "run-1",
            "fingerprint": _fingerprint(_MODEL_UNAVAILABLE_MSG),
            "count": 2,
        }

        updates = _exec_updates(plugin, state)

        # 跨 run 不累加：本 run 的 2 次同文失败从 2 起计（非 2+2）
        assert updates["router.llm_fail_streak"]["count"] == 2
        assert updates["router.llm_error_cursor"]["count"] == 2


class TestVolatileFieldBlindness:
    """每调用易变字段（请求号）不得致盲连击（2026-10-23 额度 429 空转事故）。

    事故形态：额度 429 连败 175 次熔断不触发——每条错误仅 request_id 不同，
    旧全文指纹把「同一失败」判成异文，连击永远停在 1，任务无限空转。
    """

    def test_request_id_variance_still_accumulates_and_trips(self) -> None:
        """仅 request_id 不同的同类失败 → 视为同文连击，达瞬态阈值 5 即终止。"""
        plugin = DuplicateCheckPlugin()
        errors = [_llm_error(_quota_429_msg(f"072a{id_:024x}")) for id_ in range(5)]
        state = _state(errors, agent_level="L2")

        updates = _exec_updates(plugin, state)

        assert updates["should_stop"] is True
        assert updates["router.stop_reason"] == "llm_fail_loop"

    def test_request_id_variance_accumulates_across_rounds(self) -> None:
        """真实节奏（每轮一条失败）：跨轮连击延续计数，第 5 轮触发终止。"""
        plugin = DuplicateCheckPlugin()

        first = _exec_updates(
            plugin,
            _state(
                [_llm_error(_quota_429_msg("072a" + "a" * 20)) for _ in range(2)],
                agent_level="L2",
            ),
        )
        assert first.get("should_stop") is not True
        assert first["router.llm_fail_streak"]["count"] == 2

        # 引擎 _plugin_error_stats run 内聚合：第二轮 = 前 2 次旧失败（同文
        # 聚成 count=2）+ 3 次新失败（各自独立 request_id → 3 条 count=1）
        state = _state(
            [_llm_error(_quota_429_msg("072a" + "a" * 20)) for _ in range(2)]
            + [_llm_error(_quota_429_msg(f"072b{id_:024x}")) for id_ in range(3)],
            agent_level="L2",
        )
        state["router.llm_fail_streak"] = first["router.llm_fail_streak"]
        state["router.llm_error_cursor"] = first["router.llm_error_cursor"]

        second = _exec_updates(plugin, state)

        assert second["should_stop"] is True
        assert second["router.stop_reason"] == "llm_fail_loop"

    def test_stable_text_strips_both_quote_styles(self) -> None:
        """易变字段剥除兼容原始/转义两种引号形态（引擎记录面可能带转义）。"""
        plain = '"request_id":"072aabcd1234"'
        escaped = '\\"request_id\\":\\"072aabcd1234\\"'
        for text in (plain, escaped):
            assert "<volatile>" in _MOD._stable_error_text(text), text
            assert "072aabcd1234" not in _MOD._stable_error_text(text), text

    def test_stable_detail_carries_provider_message(self) -> None:
        """终止注记带供应商原文（额度上限话术）且不含每调用请求号原文。"""
        plugin = DuplicateCheckPlugin()
        errors = [_llm_error(_quota_429_msg(f"072a{id_:024x}")) for id_ in range(5)]
        state = _state(errors, agent_level="L2", **{"task.id": "task-1"})

        updates = _exec_updates(plugin, state)

        reason = updates["task.failure_reason"]
        assert "连续 5 次相同失败" in reason
        assert "已达到 Token Plan 用量上限" in reason
        assert "072a" not in updates["task.error"]
        assert updates["task.failure_class"] == "llm_fail_loop"

    def test_termination_writes_failure_notes_only_with_task_identity(self) -> None:
        """有任务身份 → 写归因三件套；无任务身份（纯会话管道）→ 不写。"""
        plugin = DuplicateCheckPlugin()
        errors = [_llm_error(_quota_429_msg(f"072a{id_:024x}")) for id_ in range(5)]

        with_task = _exec_updates(plugin, _state(errors, agent_level="L2", **{"task.id": "t1"}))
        assert with_task["task.failure_class"] == "llm_fail_loop"
        assert with_task["task.failure_reason"]
        assert with_task["task.error"]

        without_task = _exec_updates(plugin, _state(errors, agent_level="L2"))
        assert not any(k.startswith("task.") for k in without_task), sorted(without_task)

    def test_termination_does_not_prewrite_task_status(self) -> None:
        """熔断方不得预写 task.status：终态投影归 run 收尾派生（越权抑制
        task_failed 派生会让上级收不到失败通知）。"""
        plugin = DuplicateCheckPlugin()
        errors = [_llm_error(_quota_429_msg(f"072a{id_:024x}")) for id_ in range(5)]
        state = _state(errors, agent_level="L2", **{"task.id": "t1"})

        updates = _exec_updates(plugin, state)

        assert "task.status" not in updates

    def test_genuinely_different_quota_messages_do_not_accumulate(self) -> None:
        """剔除请求号后仍不同的失败（真异文）→ 不累加（区分度守恒）。"""
        plugin = DuplicateCheckPlugin()
        errors = [
            _llm_error(_quota_429_msg("072aaa").replace("Token Plan", f"Token Plan-{i}"))
            for i in range(5)
        ]
        state = _state(errors, agent_level="L2")

        updates = _exec_updates(plugin, state)

        assert updates.get("should_stop") is not True
        assert updates["router.llm_fail_streak"]["count"] == 1


class TestRepetitiveOutputUnchanged:
    """正常重复输出检测语义不回退（问题 4 修复不得误伤既有判定）。"""

    def test_same_output_twice_still_counted(self) -> None:
        """连续两轮相同输出 → 重复计数 1（既有语义保持）。"""
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "messages": [],
            "raw_result": "同样的输出",
            "raw_tool_calls": [],
            "tool_results": [],
            "run_id": "run-1",
            "router.last_response": "",
            "router.last_response_text": "",
            "router.repetitive_count": 0,
        }

        first = _exec_updates(plugin, state)
        assert first["router.repetitive_count"] == 0

        state["router.last_response"] = first["router.last_response"]
        state["router.last_response_text"] = first["router.last_response_text"]
        second = _exec_updates(plugin, state)
        assert second["router.repetitive_count"] == 1

    def test_different_output_resets_count(self) -> None:
        """输出不同 → 重复计数清零（既有语义保持）。"""
        plugin = DuplicateCheckPlugin()
        state: dict[str, Any] = {
            "messages": [],
            "raw_result": "新的输出内容",
            "raw_tool_calls": [],
            "tool_results": [],
            "run_id": "run-1",
            "router.last_response": "deadbeef",
            "router.last_response_text": "完全不同的旧输出",
            "router.repetitive_count": 2,
        }

        updates = _exec_updates(plugin, state)

        assert updates["router.repetitive_count"] == 0


class TestCamelCaseRequestIdStripping:
    """camelCase ``requestId`` 易变字段剥除（2026-11-18 装机 634adfc6a364 实证）。

    事故形态：CPA gateway 额度 429 错误正文用 camelCase ``requestId``（无
    下划线），旧正则只匹配 ``request_id``——同型失败逐调用异文，连击永不
    达阈，169 连败未触发熔断，任务全家僵死 80+ 分钟。
    """

    @staticmethod
    def _gateway_429_msg(request_id: str) -> str:
        """2026-11-18 装机实证形态：gateway service_down 包裹的额度 429，
        camelCase requestId 每调用不同（文本其余部分相同）。引号形态与引擎
        PARSE_ERROR 嵌入面一致 = JSON 字符串单反斜杠转义。"""
        inner = (
            '{"error":{"message":"upstream 429: '
            "额度已用尽，请访问以下链接，购买加油包以获取更多额度："
            'https://example.com/profile",'
            f'"requestId":"{request_id}",'
            '"type":"server_error","code":"internal_server_error"}}'
        )
        return (
            "RuntimeError: llm.complete_stream 工具执行失败: "
            '{"success": false, "error": "原错误: _PluginProviderFailure: '
            "[llm_failure code=service_down] [wire http 500] "
            + inner.replace('"', '\\"')
            + '"}}'
        )

    def test_stable_text_strips_camel_case_request_id(self) -> None:
        """camelCase requestId 剥除后无原文残留（两种引号形态都收）。"""
        plain = '"requestId":"fc21488c-70d1-450d-99d5-4c1ab19e77f2"'
        escaped = '\\"requestId\\":\\"fc21488c-70d1-450d-99d5-4c1ab19e77f2\\"'
        for text in (plain, escaped):
            stable = _MOD._stable_error_text(text)
            assert "<volatile>" in stable, text
            assert "fc21488c" not in stable, text

    def test_camel_case_variance_accumulates_and_trips(self) -> None:
        """仅 camelCase requestId 不同的同类失败 → 同文连击，达瞬态阈值 5 终止。"""
        plugin = DuplicateCheckPlugin()
        errors = [
            _llm_error(self._gateway_429_msg(f"fc21488c-{i:04d}-450d"))
            for i in range(5)
        ]
        state = _state(errors, agent_level="L2")

        updates = _exec_updates(plugin, state)

        assert updates["should_stop"] is True
        assert updates["router.stop_reason"] == "llm_fail_loop"

    def test_camel_case_variance_real_pace_across_rounds(self) -> None:
        """真实节奏（每轮一条失败）：camelCase 异文跨轮延续计数，第 5 轮终止。"""
        plugin = DuplicateCheckPlugin()

        first = _exec_updates(
            plugin,
            _state(
                [_llm_error(self._gateway_429_msg("fc21488c-aaaa-450d")) for _ in range(2)],
                agent_level="L2",
            ),
        )
        assert first.get("should_stop") is not True
        assert first["router.llm_fail_streak"]["count"] == 2

        state = _state(
            [_llm_error(self._gateway_429_msg("fc21488c-aaaa-450d")) for _ in range(2)]
            + [
                _llm_error(self._gateway_429_msg(f"fc21488c-{i:04d}-450d"))
                for i in range(3)
            ],
            agent_level="L2",
        )
        state["router.llm_fail_streak"] = first["router.llm_fail_streak"]
        state["router.llm_error_cursor"] = first["router.llm_error_cursor"]

        second = _exec_updates(plugin, state)

        assert second["should_stop"] is True
        assert second["router.stop_reason"] == "llm_fail_loop"
