# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""task_reminder `state.reads` 声明完备性 + 失败轮豁免切片回归（切片投喂契约）。

invoker 按 manifest `state.reads` 切片投喂（kernel/crates/invoker/src/shared.rs
`state_feed_payload`：声明非空 → 控制键白名单 ∪ 声明键切片；未声明键插件永远
读不到，静默缺省无任何日志）。

历史事故（2026-11-12 装机实证，5 条任务陪跑）：失败轮豁免判据读记账基线
``reminder_seen_result_fp``，但 manifest `state.reads` 漏声明该键 → 切片后恒读
不到（None）→ 指纹比对恒不等 → `_is_stale_output` 恒 False。后果：llm_core
每轮失败（`llm.complete_stream ... response channel closed`）都被当成「agent
纯文本轮」，各注入一次提醒并消耗额度；连续 3-5 次失败在 30 秒内烧完 per-run
预算，任务被裁决 `reminder_exhausted`（如 fa819ab6edac 12:30:14→12:30:42 三次
提醒、99d95655613f / c25b15685a2b / 3b92e6d4fcd2 / c6b1087aff26 同形态）。
真实死因是系统侧 LLM 通道故障（应由 duplicate_check 连击熔断署名
`llm_fail_loop`），被错误记成 agent 空转。

两层回归：
1. 机械扫描 plugin.py 全部 state 读取点（含模块级常量键，如
   ``_RESULT_FP_KEY``——本事故正是常量键漏声明）∈ reads ∪ 控制键白名单；
2. 行为面：按 manifest 声明切片投喂真实形态 state，失败轮必须豁免额度、
   禁止注入提醒；成功轮/无 llm_core 错误的复读轮必须照常计额（切片机制
   断裂时本用例红）。

控制键白名单镜像自 STATE_SLICE_CONTROL_KEYS（shared.rs），漂移时以 Rust 侧
为真源同步本表（与 duplicate_check/test_state_reads_declaration.py 同款）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/
_TASKS_DIR = _SHARED / "tasks"  # 裸名 task_types（活跃子任务回退枚举的解析前提）

for _d in (str(_DIR), str(_SHARED), str(_TASKS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import asyncio  # noqa: E402

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

# kernel/crates/invoker/src/shared.rs STATE_SLICE_CONTROL_KEYS（恒带控制键）。
_CONTROL_KEYS = {
    "pipeline_id",
    "session_id",
    "thread_id",
    "message_id",
    "run_id",
    "task_id",
    "workspace",
    "conversation_mode",
    "core_type",
    "core_plugin",
    "suspended",
    "ended",
    "next_phase",
    "agent.id",
    "model_tier",
    "agent_level",
}

_LITERAL_GET = re.compile(r'state\.get\(\s*"([^"]+)"')
_LITERAL_GET_SQ = re.compile(r"state\.get\(\s*'([^']+)'")
_LITERAL_ITEM = re.compile(r'state\[\s*"([^"]+)"\s*\]')
_CONST_GET = re.compile(r"state\.get\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]")


def _load_plugin_module() -> Any:
    mod_name = "task_reminder_state_reads_test"
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
TaskReminder = _MOD.TaskReminder
_FP_KEY = _MOD._RESULT_FP_KEY


def _manifest_reads() -> list[str]:
    """manifest 声明原样条目（保留 messages_tail:N 形态供切片组装）。"""
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    assert reads, "state.reads 已声明（非空声明 = 切片投喂前提），不得回退全量"
    return list(reads)


def _declared_keys(reads: list[str]) -> set[str]:
    """切片后插件可见的键集合（messages_tail:N 投喂键仍是 messages）。"""
    return {"messages" if r.startswith("messages_tail:") else r for r in reads}


def _slice_feed(full_state: dict[str, Any], reads: list[str]) -> dict[str, Any]:
    """按内核切片契约组装投喂 state（控制键白名单 ∪ 声明键；缺键省略）。"""
    out: dict[str, Any] = {k: full_state[k] for k in _CONTROL_KEYS if k in full_state}
    for entry in reads:
        if entry.startswith("messages_tail:"):
            n = int(entry.split(":", 1)[1])
            messages = full_state.get("messages")
            if isinstance(messages, list) and n > 0:
                out["messages"] = messages[-n:]
        elif entry in full_state:
            out[entry] = full_state[entry]
    return out


def _execute(full_state: dict[str, Any], **cfg: Any) -> tuple[Any, dict[str, Any]]:
    """切片投喂 → 执行 → 返回 (结果, 本轮实际可见 state)。"""
    fed = _slice_feed(full_state, _manifest_reads())
    svc = SimpleNamespace(
        list_by_status=lambda _status: [],
        list_subtasks=lambda _tid: [],
    )
    ctx = PluginContext(
        state=fed,
        config=cfg,
        _services={"task_service": svc},
    )
    return asyncio.run(TaskReminder(config=cfg).execute(ctx)), fed


def _merge(state: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """引擎同语义 merge：state_updates 写回 working state。"""
    state.update(updates)
    return state


_STALE_TEXT = "阶段性输出——正在收集证据，稍后提交评估"


def _llm_core_error(message: str = "response channel closed", seq: int = 1) -> dict[str, Any]:
    return {
        "plugin_id": "pipeline_llm_core",
        "code": "MCP_CALL_FAILED",
        "message": (
            "MCP call failed: llm_core.execute: MCP protocol error: "
            f"[0] llm.complete_stream 工具执行失败: {message}"
        ),
        "count": 1,
        "seq": seq,
    }


def _task_pipeline_state(**overrides: Any) -> dict[str, Any]:
    """任务管道真实形态 state（上一轮有产出 + 本轮 llm_core 失败）。"""
    state: dict[str, Any] = {
        "pipeline_id": "3b92e6d4fcd2",
        "run_id": "run-1",
        "agent.id": "general_agent",
        "agent_level": "L3",
        "core_type": "llm_call",
        "iteration": 5,
        "task.id": "3b92e6d4fcd2",
        "task.status": "running",
        "max_reminders": 3,
        "messages": [
            {"role": "user", "content": "诊断仓库 git 分支堆积来源与未合并成因"},
            {"role": "assistant", "content": _STALE_TEXT},
        ],
        "raw_result": _STALE_TEXT,
        "raw_tool_calls": [],
        "reminder_seen_result_fp": TaskReminder._result_fingerprint(_STALE_TEXT),
        "evaluate_reminder_count": 0,
        "evaluate_reminder_run": "run-1",
        "evaluate_reminder_total": 0,
        "_plugin_error_stats": [_llm_core_error()],
    }
    state.update(overrides)
    return state


class TestReadsDeclaration:
    """机械扫描：plugin.py 的每个 state 读取点必须在切片可见面内。"""

    def test_all_state_reads_declared(self) -> None:
        reads = _declared_keys(_manifest_reads())
        source = (_DIR / "plugin.py").read_text(encoding="utf-8")
        used: set[str] = set(_LITERAL_GET.findall(source))
        used |= set(_LITERAL_GET_SQ.findall(source))
        used |= set(_LITERAL_ITEM.findall(source))
        for name in _CONST_GET.findall(source):
            value = getattr(_MOD, name, None)
            if isinstance(value, str):
                used.add(value)
        undeclared = sorted(used - reads - _CONTROL_KEYS)
        assert undeclared == [], (
            "以下读取键未在 manifest state.reads 声明（切片投喂下恒读不到，"
            f"行为静默降级）: {undeclared}"
        )

    def test_result_fingerprint_baseline_in_reads(self) -> None:
        """失败轮豁免的记账基线必须声明（2026-11-12 事故根因）。

        基线读不到 → `_is_stale_output` 指纹比对恒不等 → llm_core 失败轮被
        当成 agent 纯文本轮，逐轮注入提醒并耗额度，直至 reminder_exhausted。
        """
        assert _FP_KEY in _declared_keys(_manifest_reads()), (
            f"{_FP_KEY} 未在 state.reads 声明 → 切片后失败轮豁免判据恒假"
        )


class TestFailureRoundExemptionUnderSliceFeed:
    """行为面：切片投喂下失败轮豁免必须成立（与真实管道投喂同形）。"""

    def test_failed_round_exempt_under_slice_feed(self) -> None:
        result, fed = _execute(_task_pipeline_state())

        assert _FP_KEY in fed, "切片必须携带记账基线（声明回归的当轮观测面）"
        assert fed["_plugin_error_stats"], "切片必须携带本 run 的 llm_core 错误条目"
        assert result.state_updates == {"_has_new_llm_input": True}, (
            f"失败轮只置续跑标志，实际 {result.state_updates!r}"
        )
        assert "evaluate_reminder_count" not in result.state_updates, "失败轮不得消耗额度"
        assert "messages" not in result.state_updates, "失败轮不注入提醒"
        assert "_system_failure_round" not in result.state_updates, "内部协议键不得外泄"

    def test_incident_replay_three_failures_do_not_exhaust(self) -> None:
        """事故复现：连续 3 次 llm_core 失败不得烧完 per-run 预算改判 failed。

        现场形态（fa819ab6edac 等 5 条）：每次失败后 2-8 秒内注入一次提醒，
        3-5 次后 task.status -> failed / reminder_exhausted；模型从未收到提醒。
        """
        state = _task_pipeline_state()
        for _ in range(3):
            result, _fed = _execute(state)
            _merge(state, result.state_updates)
            state["_plugin_error_stats"][0]["count"] += 1
            state["_plugin_error_stats"][0]["seq"] += 1
            assert "messages" not in result.state_updates

        assert state.get("task.status") == "running", "系统故障不得裁决任务失败"
        assert int(state.get("evaluate_reminder_count", 0) or 0) == 0, "失败轮不消耗额度"
        assert len(state["messages"]) == 2, "失败轮不得追加提醒消息"

    def test_fresh_output_round_consumes_budget_under_slice(self) -> None:
        """系统恢复轮（新产出）→ 照常计额注入提醒（豁免不得吞掉真实文本轮）。"""
        state = _task_pipeline_state(raw_result="恢复后的新回复（继续审查）")
        result, _fed = _execute(state)

        assert result.state_updates.get("evaluate_reminder_count") == 1, (
            f"新产出轮必须计额，实际 {result.state_updates!r}"
        )
        assert "messages" in result.state_updates, "新产出轮必须注入提醒"
        assert result.state_updates.get("_has_new_llm_input") is True

    def test_repeated_text_without_llm_error_consumes_under_slice(self) -> None:
        """无 llm_core 错误的复读轮 → 计额（防 agent 复读绕过耗尽兜底）。"""
        state = _task_pipeline_state(
            _plugin_error_stats=[
                {
                    "plugin_id": "pipeline_tool_core",
                    "code": "X",
                    "message": "tool failed",
                    "count": 1,
                    "seq": 1,
                },
            ]
        )
        result, _fed = _execute(state)

        assert result.state_updates.get("evaluate_reminder_count") == 1, (
            f"非失败轮的相同文本必须计额，实际 {result.state_updates!r}"
        )

    def test_budget_exhaustion_still_fires_after_real_reminders(self) -> None:
        """真提醒耗尽路径不受豁免影响：已有 3 次额度 + 新产出轮 → 改判 failed。"""
        state = _task_pipeline_state(
            raw_result="又一轮纯文本总结（未提交评估）",
            evaluate_reminder_count=3,
            evaluate_reminder_total=3,
        )
        result, _fed = _execute(state)

        assert result.state_updates.get("task.status") == "failed"
        assert result.state_updates.get("task.failure_class") == "reminder_exhausted"
        assert result.state_updates.get("ended") is True
