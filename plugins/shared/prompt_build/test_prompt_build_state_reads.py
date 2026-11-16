# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""prompt_build `state.reads` 声明完备性回归（切片投喂契约）。

invoker 按 manifest `state.reads` 切片投喂（kernel/crates/invoker/src/shared.rs
`state_feed_payload`：声明非空 → 控制键白名单 ∪ 声明键切片；未声明键插件永远
读不到，静默缺省无任何日志）。历史事故（2026-10-21 实锤）：agent_config_load
按会话挂靠写入 state.project_roots、prompt_build 的
`{{project_agents_md_anchor}}` 分支读取该键，但 reads 漏声明——项目根
AGENTS.md（项目宪法）对所有会话静默零注入。

本测试机械扫描 plugin.py 源码中 `ctx.state.get(<字面量>)` 全部读取点，逐一
断言 ∈ manifest reads ∪ 控制键白名单。超出静态扫描的动态读取（须手审并保证
声明，漏声明同样静默）：

- `{{state:键}}` 占位符参数（`params.get("state")`）与路由变量 `route_key`
  ——键来自提示词/配置参数，运行期任意；此类用法的引用键须随用法补进 reads。
- `workspace` 命中控制键白名单，无需声明。

控制键白名单镜像自 STATE_SLICE_CONTROL_KEYS（shared.rs），漂移时以 Rust 侧
为真源同步本表。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent

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


def _manifest_reads() -> set[str]:
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    assert reads, "state.reads 已声明（非空声明 = 切片投喂前提），不得回退全量"
    return set(reads)


def test_literal_state_reads_all_declared() -> None:
    """代码读取的每个字面量 state 键必须在 reads ∪ 控制键内（漏声明=静默不可见）。"""
    source = (_DIR / "plugin.py").read_text(encoding="utf-8")
    scanned = set(_LITERAL_GET.findall(source)) | set(_LITERAL_GET_SQ.findall(source))
    assert scanned, "扫描到零读取点=正则失配，测试失效"
    allowed = _manifest_reads() | _CONTROL_KEYS
    undeclared = sorted(scanned - allowed)
    assert undeclared == [], (
        "以下 state 键被代码读取但未声明进 manifest state.reads（切片投喂下"
        f"插件永远读不到，静默缺省）：{undeclared}"
    )


def test_project_roots_declared_for_agents_md_anchor() -> None:
    """项目宪法注入点的前置键单列：{{project_agents_md_anchor}} 读 project_roots。"""
    assert "project_roots" in _manifest_reads()
