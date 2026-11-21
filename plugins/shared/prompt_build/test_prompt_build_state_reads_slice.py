# @feature: FP-0.2.〇 管道引擎与插件执行模型（条件注入引擎） | @ci: python-coverage
"""prompt_build `state.reads` 切片投喂回归（条件注入规则的身份/工具条件）。

漏声明形态（2026-11-12 修复）：`agent_id` 与 `tool_ids` 未进 manifest
`state.reads` → 切片后恒读不到 → `ContributionEngine._matches` 的 `when.agents`
恒不命中（按 agent 定制的注入块对任何 agent 都不生效）、`when.tools` 恒不命中
（按工具面定制的注入块永不生效）。规则文本没错、日志无异常——纯静默空注入。

读点在 `contributions.py`（同插件进程内消费 `ctx.state`），故本用例按
prompt_build 的 manifest 声明切片投喂后直接驱动引擎（与 plugin.py 调用
`self._contributions.render(ctx.state)` 同一入参形态）。
"""

from __future__ import annotations

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

from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_engine_module() -> Any:
    mod_name = "prompt_build_contributions_state_reads_slice_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "contributions.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


MANIFEST_READS = manifest_reads(_DIR)


_DOC = """rules:
  - name: agent-scoped
    when:
      agents: ["code_writer_agent"]
    inject:
      order: 1
      content: 写码 agent 专属纪律
  - name: tool-scoped
    when:
      tools: ["file_read", "file_write"]
    inject:
      order: 2
      content: 有文件工具时的读写纪律
"""


def _engine(tmp_path: Path) -> Any:
    mod = _load_engine_module()
    path = tmp_path / "contributions.yaml"
    path.write_text(_DOC, encoding="utf-8")
    return mod.ContributionEngine(path)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "pipeline_id": "p-1",
        "agent_id": "code_writer_agent",
        "tool_ids": ["file_read", "file_write", "bash"],
    }
    state.update(overrides)
    return state


class TestDeclaration:
    def test_slice_carried_keys_declared(self) -> None:
        declared = visible_keys(MANIFEST_READS)
        for key in ("agent_id", "tool_ids"):
            assert key in declared, f"{key} 未在 state.reads 声明 → 切片后条件恒不命中"


class TestContributionConditionsUnderSliceFeed:
    def test_agent_condition_hits_under_slice_feed(self, tmp_path: Path) -> None:
        fed = slice_feed(_state(), MANIFEST_READS)

        out = _engine(tmp_path).render(fed)

        assert "写码 agent 专属纪律" in out, f"when.agents 未命中（读点漏声明时恒空）：{out!r}"

    def test_tool_condition_hits_under_slice_feed(self, tmp_path: Path) -> None:
        fed = slice_feed(_state(), MANIFEST_READS)

        out = _engine(tmp_path).render(fed)

        assert "有文件工具时的读写纪律" in out, f"when.tools 未命中：{out!r}"

    def test_conditions_miss_for_other_agent_and_thin_tool_face(self, tmp_path: Path) -> None:
        """条件不匹配时零注入（切片修复不得让条件恒真）。"""
        fed = slice_feed(
            _state(agent_id="community_ops", tool_ids=["file_read"]), MANIFEST_READS
        )

        assert _engine(tmp_path).render(fed) == ""
