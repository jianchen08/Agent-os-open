# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""duplicate_check `state.reads` 声明完备性 + 失败归因切片回归（切片投喂契约）。

invoker 按 manifest `state.reads` 切片投喂（kernel/crates/invoker/src/shared.rs
`state_feed_payload`：声明非空 → 控制键白名单 ∪ 声明键切片；未声明键插件永远
读不到，静默缺省无任何日志）。

历史事故（2026-11-11 装机实证）：熔断终止写面 `_terminate_pipeline` 以
`ctx.state.get("task.id")` 判定任务身份，但 manifest `state.reads` 漏声明
`task.id` → 切片后恒读不到 → `task.error/failure_class/failure_reason` 三件套
静默不写。同日 21/22 条 failed 无 `task.failure_class`（如 eb0ddf4a6a86：
trace 有 `router.stop_reason=llm_fail_loop` 与 `should_stop`，无任何 task.*
注记）。终端投影卡 failed 而复盘分诊无死因。

两层回归：
1. 机械扫描 plugin.py 全部 `ctx.state.get(<字面量/StateKeys.X>)` 读取点 ∈
   reads ∪ 控制键白名单（漏声明即静默，故须静态锁）；
2. 行为面：按 manifest 声明切片投喂真实形态 state，熔断必须落非空
   class/reason/error（切片机制断裂时本用例红）。

控制键白名单镜像自 STATE_SLICE_CONTROL_KEYS（shared.rs），漂移时以 Rust 侧
为真源同步本表（与 agent_config_load/test_agent_config_load_state_reads.py 同款）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
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

from _libs.pipeline_base.types import StateKeys  # noqa: E402

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

_LITERAL_GET = re.compile(r'ctx\.state\.get\(\s*"([^"]+)"')
_LITERAL_GET_SQ = re.compile(r"ctx\.state\.get\(\s*'([^']+)'")
_STATEKEYS_GET = re.compile(r"ctx\.state\.get\(\s*StateKeys\.([A-Z_]+)")
_STATE_GET = re.compile(r'(?<!ctx\.)\bstate\.get\(\s*"([^"]+)"')


def _manifest_reads() -> set[str]:
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    assert reads, "state.reads 已声明（非空声明 = 切片投喂前提），不得回退全量"
    # 切片条目形态归一：messages_tail:N 投喂键仍是 messages（尾部 N 条）。
    return {"messages" if r.startswith("messages_tail:") else r for r in reads}


def _slice_feed(full_state: dict[str, Any], reads: set[str]) -> dict[str, Any]:
    """按内核切片契约组装投喂 state（控制键白名单 ∪ 声明键；缺键省略）。"""
    out: dict[str, Any] = {k: full_state[k] for k in _CONTROL_KEYS if k in full_state}
    for entry in reads:
        if entry.startswith("messages_tail:"):
            n = int(entry.split(":", 1)[1])
            if isinstance(full_state.get("messages"), list):
                out["messages"] = full_state["messages"][-n:] if n else []
        elif entry in full_state:
            out[entry] = full_state[entry]
    return out


def _load_plugin_module() -> Any:
    mod_name = "duplicate_check_state_reads_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _breaker_updates(full_state: dict[str, Any]) -> dict[str, Any]:
    """按 manifest 声明切片投喂 → 执行插件 → 返回 state_updates。"""
    from _libs.pipeline_base.plugin import PluginContext

    mod = _load_plugin_module()
    plugin = mod.DuplicateCheckPlugin()
    ctx = PluginContext(state=_slice_feed(full_state, _manifest_reads()), config={})
    return asyncio.run(plugin.execute(ctx)).state_updates


def _quota_error(request_id: str, seq: int = 1) -> dict[str, Any]:
    """llm_core 失败账本条目形态（引擎 _plugin_error_stats；易变请求号）。"""
    return {
        "plugin_id": "pipeline_llm_core",
        "code": "MCP_CALL_FAILED",
        "message": (
            "MCP call failed: llm_core.execute: 上游连接超时"
            f'{{"http_code":"429","request_id":"{request_id}"}}'
        ),
        "count": 1,
        "seq": seq,
    }


def _task_pipeline_state(**overrides: Any) -> dict[str, Any]:
    """任务管道真实形态 state（task.id 在场 + 5 条同文 LLM 失败达瞬态阈值）。"""
    state: dict[str, Any] = {
        "task.id": "bca69acbfeec",
        "task.status": "running",
        "pipeline_id": "bca69acbfeec",
        "run_id": "run-1",
        "agent_level": "L3",
        "agent.id": "mode_coding/code_reviewer_agent",
        "core_type": "llm_call",
        "delegate_depth": 1,
        "messages": [{"role": "user", "content": "做任务"}],
        "raw_result": "",
        "raw_tool_calls": [],
        "tool_results": [],
        "_plugin_error_stats": [
            _quota_error(f"072a{idx:024x}", idx + 1) for idx in range(5)
        ],
    }
    state.update(overrides)
    return state


class TestReadsDeclaration:
    """机械扫描：plugin.py 的每个 state 读取点必须在切片可见面内。"""

    def test_all_state_reads_declared(self) -> None:
        reads = _manifest_reads()
        source = (_DIR / "plugin.py").read_text(encoding="utf-8")
        used: set[str] = set(_LITERAL_GET.findall(source))
        used |= set(_LITERAL_GET_SQ.findall(source))
        used |= set(_STATE_GET.findall(source))
        for attr in _STATEKEYS_GET.findall(source):
            value = getattr(StateKeys, attr)
            assert isinstance(value, str), f"StateKeys.{attr} 非字符串键，扫描器需扩展"
            used.add(value)
        undeclared = sorted(used - reads - _CONTROL_KEYS)
        assert undeclared == [], (
            "以下读取键未在 manifest state.reads 声明（切片投喂下恒读不到，"
            f"行为静默降级）: {undeclared}"
        )

    def test_task_identity_in_reads(self) -> None:
        """任务身份键必须声明：熔断归因注记（task.error/failure_class/
        failure_reason）以 task.id 在场为前提（2026-11-11 事故根因）。"""
        assert StateKeys.TASK_ID in _manifest_reads(), (
            "task.id 未在 state.reads 声明 → 切片后 _terminate_pipeline 视为"
            "无任务身份，失败归因三件套静默不写"
        )


class TestBreakerAttributionUnderSliceFeed:
    """行为面：切片投喂下熔断必须落非空失败归因（与真实管道投喂同形）。"""

    def test_llm_fail_loop_writes_class_and_reason_under_slice(self) -> None:
        updates = _breaker_updates(_task_pipeline_state())

        assert updates["should_stop"] is True
        assert updates["router.stop_reason"] == "llm_fail_loop"
        assert updates["task.failure_class"] == "llm_fail_loop"
        assert str(updates["task.failure_reason"]).strip(), "失败原因不得为空"
        assert str(updates["task.error"]).strip(), "错误详情不得为空"
        assert "连续" in updates["task.failure_reason"]

    def test_slice_keeps_task_identity_for_session_pipelines_absent(self) -> None:
        """会话管道（无 task.id）：切片后同样无身份，不写任何 task.* 注记。"""
        state = _task_pipeline_state()
        state.pop("task.id")
        state.pop("task.status")
        updates = _breaker_updates(state)

        assert updates["should_stop"] is True
        assert not any(k.startswith("task.") for k in updates), sorted(updates)
