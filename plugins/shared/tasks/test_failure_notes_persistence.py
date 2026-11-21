# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""任务失败注记的持久化面声明锁（`persistent_fields` 并集）。

机制（kernel/crates/engine/src/pipeline_loop/state_io.rs
``project_state_snapshot`` + api/server/stage_exec.rs ``with_persistent_fields``）：

- 插件返回的 ``state_updates`` 由引擎 merge 进**内存** state（merge 时不落库）；
- 落库只在每个配置 step 收尾按 ``persistent_fields`` 并集逐键
  ``upsert_state_field`` 投影——**未声明 = 永不落库**（run 终态注销热缓存后
  该键彻底消失，pipeline_state 读面恒缺）。

事故（2026-11-11 装机实证）：提醒耗尽/熔断等失败注记由 task_reminder /
duplicate_check 经 ``state_updates`` 写（task.error / task.failure_class /
task.failure_reason），但全部 manifest 的 persistent_fields 并集未声明这三键 →
DB 只剩声明过的 task.status=failed，归因恒空。实证：8 条 trace 含
reminder_exhausted 的管道，pipeline_state.task.failure_class 全为 null/缺席，
而同一 patch 的 task.status 正常落库（同写面单键对照）。

本测试机械断言：三键 ∈ 全部插件 manifest 的 persistent_fields 并集（内核
投影面单一真值），且由任务域 owner（tasks）声明（落库面与 export_fields
出口面同源）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

# 失败归因三件套（写面口径：duplicate_check._terminate_pipeline /
# task_reminder 裁决写面 / reconcile._follow_write_failed 共用词表）。
FAILURE_NOTE_KEYS = ("task.error", "task.failure_class", "task.failure_reason")


def _manifest(plugin_dir: Path) -> dict:
    return json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))


def _persistent_fields_union() -> set[str]:
    """内核投影面 = 全部声明插件 manifest 的 persistent_fields 并集。"""
    union: set[str] = set()
    for manifest_path in sorted(_SHARED.glob("*/plugin.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        union |= set(manifest.get("persistent_fields") or [])
    return union


@pytest.mark.parametrize("key", FAILURE_NOTE_KEYS)
def test_failure_note_keys_projected_by_persistent_fields(key: str) -> None:
    """失败注记必须进 persistent_fields 并集，否则插件写了也落不了库。"""
    declared = _persistent_fields_union()
    assert key in declared, (
        f"{key} 未在任何 manifest persistent_fields 声明 → 引擎 step 收尾投影不写该键，"
        "pipeline_state 恒缺失败归因（2026-11-11 事故形态）"
    )


@pytest.mark.parametrize("key", FAILURE_NOTE_KEYS)
def test_tasks_domain_owns_persistence(key: str) -> None:
    """任务域 owner（tasks）声明落库面，与同 manifest export_fields 出口面同源。"""
    manifest = _manifest(_DIR)
    assert key in set(manifest.get("persistent_fields") or []), (
        f"tasks/plugin.json persistent_fields 缺 {key}（任务域落库面单一真值）"
    )
    assert key in set(manifest.get("export_fields") or []), (
        f"tasks/plugin.json export_fields 缺 {key}（落库面与出口面须同源）"
    )
