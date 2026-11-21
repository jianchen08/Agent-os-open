# @feature: FP-0.2.〇 管道引擎与插件执行模型（切片投喂契约测试侧） | @ci: python-coverage
"""按 manifest `state.reads` 组装投喂 state（切片投喂契约的测试侧单点）。

内核 invoker 按 manifest `state.reads` 切片投喂（`kernel/crates/invoker/src/shared.rs`
`state_feed_payload` / `assemble_state_slice`）：声明非空 → 控制键白名单 ∪ 声明键
切片；未声明键插件永远读不到，静默缺省无任何日志。插件行为测试必须**按声明
切片投喂**才与生产同形——直接喂全量 state 会把「读点漏声明」的静默降级盖住
（2026-11-11 duplicate_check / 2026-11-12 task_reminder 两起事故形态）。

两个消费要点：
1. `visible_keys()`——声明归一后的可见键集（`messages_tail:N` 投喂键仍是
   `messages`），机械扫描断言读点 ∈ 该集；
2. `slice_feed()`——切片组装（缺键省略，与 Rust 侧 `resolve_read_entry` 同语义）。

控制键镜像 `STATE_SLICE_CONTROL_KEYS`（Rust 侧为真源）：漂移由
`tests/gates/test_check_state_reads_declaration.py` 的镜像一致性用例执法。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 镜像 kernel/crates/invoker/src/shared.rs `STATE_SLICE_CONTROL_KEYS`。
CONTROL_KEYS: tuple[str, ...] = (
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
)


def manifest_reads(plugin_dir: str | Path) -> list[str]:
    """manifest `state.reads` 原样条目（保留 `messages_tail:N` 形态）。"""
    manifest = json.loads((Path(plugin_dir) / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    return [str(r) for r in reads]


def visible_keys(reads: list[str]) -> set[str]:
    """切片后插件可见的声明键集合（`messages_tail:N` 投喂键仍是 `messages`）。"""
    return {"messages" if r.startswith("messages_tail:") else r for r in reads}


def slice_feed(full_state: dict[str, Any], reads: list[str]) -> dict[str, Any]:
    """按内核切片契约组装投喂 state（控制键白名单 ∪ 声明键；缺键省略）。"""
    fed: dict[str, Any] = {k: full_state[k] for k in CONTROL_KEYS if k in full_state}
    for entry in reads:
        if entry.startswith("messages_tail:"):
            n = int(entry.split(":", 1)[1])
            messages = full_state.get("messages")
            if isinstance(messages, list) and n > 0:
                fed["messages"] = messages[-n:]
        elif entry in full_state:
            fed[entry] = full_state[entry]
    return fed
