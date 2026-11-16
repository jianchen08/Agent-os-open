# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""agent_config_load `state.reads` 声明完备性回归（切片投喂契约）。

invoker 按 manifest `state.reads` 切片投喂（kernel/crates/invoker/src/shared.rs
`state_feed_payload`：声明非空 → 控制键白名单 ∪ 声明键切片；未声明键插件永远
读不到，静默缺省无任何日志）。历史事故（2026-10-21 实锤）：任务挂靠键
`task.parent_project_id` 漏声明 → 任务管道 project_roots 恒零注入；
`lineage.*` 前缀扫描在切片下只见已声明键。

本测试机械扫描 plugin.py 源码中 `ctx.state.get(<字面量>)` 与
`ctx.state.get(StateKeys.<ATTR>)` 全部读取点，逐一断言 ∈ manifest reads ∪
控制键白名单。超出静态扫描的动态读取（须手审并保证声明，漏声明同样静默）：

- 运行时参数循环 `for _key in ("model_tier", "max_iterations", "max_reminders",
  "timeout_seconds")`——四键均已声明；
- `lineage.` 前缀扫描（`startswith("lineage.")`）——声明全集五键
  （parent_pipeline_id/origin/origin_session_id/parent_ws_meta/root）；
- `workspace` 命中控制键白名单，无需声明。

控制键白名单镜像自 STATE_SLICE_CONTROL_KEYS（shared.rs），漂移时以 Rust 侧
为真源同步本表。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

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


def _manifest_reads() -> set[str]:
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    assert reads, "state.reads 已声明（非空声明 = 切片投喂前提），不得回退全量"
    return set(reads)


def _statekeys_literals(source: str) -> set[str]:
    resolved = set()
    for attr in _STATEKEYS_GET.findall(source):
        value = getattr(StateKeys, attr)
        assert isinstance(value, str), f"StateKeys.{attr} 非字符串键，扫描器需扩展"
        resolved.add(value)
    return resolved


def test_literal_state_reads_all_declared() -> None:
    """代码读取的每个字面量/StateKeys state 键必须在 reads ∪ 控制键内。"""
    source = (_DIR / "plugin.py").read_text(encoding="utf-8")
    scanned = (
        set(_LITERAL_GET.findall(source))
        | set(_LITERAL_GET_SQ.findall(source))
        | _statekeys_literals(source)
    )
    assert scanned, "扫描到零读取点=正则失配，测试失效"
    allowed = _manifest_reads() | _CONTROL_KEYS
    undeclared = sorted(scanned - allowed)
    assert undeclared == [], (
        "以下 state 键被代码读取但未声明进 manifest state.reads（切片投喂下"
        f"插件永远读不到，静默缺省）：{undeclared}"
    )


def test_task_attach_and_lineage_keys_declared() -> None:
    """任务挂靠键与 lineage 前缀扫描全集单列：漏声明即任务管道项目挂靠静默断链。"""
    reads = _manifest_reads()
    assert "task.parent_project_id" in reads
    for key in (
        "lineage.parent_pipeline_id",
        "lineage.origin",
        "lineage.origin_session_id",
        "lineage.parent_ws_meta",
        "lineage.root",
    ):
        assert key in reads, f"lineage 前缀扫描依赖键未声明：{key}"
