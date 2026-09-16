"""security_check 行为测试共享替身：审批通道假 capability。

审批请求在插件内经 human-interaction capability 调用（create_choice /
wait_for_choice）。本替身按 sequence 回放审批结果，并以 create_choice 次数
作为「审批发起次数」的可观测计数——测试断行为（发起几次审批 / 是否放行），
不断插件内部状态。

注入走公开装配缝 ``set_human_interaction_cap``（与生产 on_load 同一接缝），
测试结束须调用 ``unwire_approval_cap`` 恢复自动解析，防跨测试残留。
"""

from __future__ import annotations

from typing import Any

from pipeline.plugin import PluginContext


class ApprovalCounter:
    """create_choice 调用次数的可观测计数（审批发起次数）。"""

    def __init__(self) -> None:
        self._count = 0

    @property
    def calls(self) -> int:
        return self._count


def wire_approval_cap(
    sc_mod: Any, sequence: list[dict[str, Any]]
) -> tuple[Any, ApprovalCounter]:
    """构造假 human-interaction capability 并经公开装配缝注入。

    每次 wait_for_choice 消费 sequence 中一项；sequence 耗尽即断言失败
    （审批发起次数超出测试预期）。

    Returns:
        (cap, counter)——cap 为假 capability，counter.calls 为审批发起次数。
    """
    it = iter(sequence)
    counter = ApprovalCounter()

    class _FakeCap:
        async def call(self, name: str, params: dict, **kwargs: Any) -> Any:  # noqa: ARG002
            if name == "create_choice":
                counter._count += 1  # noqa: SLF001
                return {"request_id": f"req-{counter.calls}"}
            if name == "wait_for_choice":
                try:
                    return next(it)
                except StopIteration as e:
                    raise AssertionError("审批被发起次数超出预期 sequence") from e
            raise AssertionError(f"unexpected cap.call: {name}")

    cap = _FakeCap()
    sc_mod.set_human_interaction_cap(cap)
    return cap, counter


def unwire_approval_cap(sc_mod: Any) -> None:
    """摘除显式注入的审批通道，恢复按 plugin 引用自动解析。"""
    sc_mod.set_human_interaction_cap(None)


def make_tool_ctx(
    tool_name: str,
    args: dict[str, Any],
    *,
    provider: str = "host",
    task_isolated: bool | None = None,
    pipeline_id: str | None = None,
    session_id: str | None = None,
) -> PluginContext:
    """构造危险工具执行的 PluginContext（tool_execute 核 + 单工具调用）。"""
    execution_context: dict[str, Any] = {"tool_name": tool_name, "provider": provider}
    if task_isolated is not None:
        execution_context["task_isolated"] = task_isolated
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": tool_name, "args": args}],
        "execution_contexts": [execution_context],
    }
    if pipeline_id is not None:
        state["pipeline_id"] = pipeline_id
    if session_id is not None:
        state["session_id"] = session_id
    return PluginContext(state=state, _services={})
