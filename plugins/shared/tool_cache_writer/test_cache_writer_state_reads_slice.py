# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""tool_cache_writer `state.reads` 切片投喂回归（工具结果缓存写入）。

漏声明形态（2026-11-12 修复）：`tool_results`（`StateKeys.TOOL_RESULTS` 常量
读点）未进 manifest `state.reads` → 切片后恒读不到 → writer 只看得到
`_full_tool_results`（spill 全文面）：非 spill 形态的普通工具结果全被丢弃，
`tool_cache` 的写入断路重新断开（下一轮相同调用照旧真实执行，缓存形同关闭）。
读取形态是 `_full_tool_results or tool_results` 的两级链，漏声明只废掉第二级
——单看代码「有回退」极易误判为无需声明。

本用例按 manifest 声明切片投喂（与生产 invoker 同形）断言第二级真实可用。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402
from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "tool_cache_writer_state_reads_slice_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_plugin_module()


MANIFEST_READS = manifest_reads(_DIR)


def _global_cache() -> dict[Any, Any]:
    from agentos_plugin_sdk import tool_result_cache as trc

    return trc.global_cache()


def _state(**overrides: Any) -> dict[str, Any]:
    """post 链真实形态：执行快照 + 结果（无 spill 全文面）。"""
    state: dict[str, Any] = {
        "pipeline_id": "p-1",
        "session_id": "s-1",
        "user_id": "u-1",
        "_executed_tool_calls": [
            {"name": "web_search", "arguments": '{"query": "agentos"}'}
        ],
        StateKeys.TOOL_RESULTS: [{"content": "hello world"}],
    }
    state.update(overrides)
    return state


def _run(state: dict[str, Any]) -> None:
    fed = slice_feed(state, MANIFEST_READS)
    asyncio.run(_MOD.ToolCacheWriter(config={}).execute(PluginContext(state=fed)))


class TestDeclaration:
    def test_tool_results_declared(self) -> None:
        assert StateKeys.TOOL_RESULTS in visible_keys(MANIFEST_READS), (
            "tool_results 未声明 → 切片后两级读链的第二级恒空，普通结果永不入缓存"
        )


class TestCacheWriteUnderSliceFeed:
    def test_plain_tool_results_written_under_slice_feed(self) -> None:
        _global_cache().clear()

        _run(_state())

        assert len(_global_cache()) == 1, "普通工具结果未写入缓存（读点漏声明时恒空）"

    def test_full_results_take_precedence_under_slice_feed(self) -> None:
        """spill 全文面在场时优先（两级链第一级语义不变）。"""
        _global_cache().clear()

        _run(
            _state(
                _full_tool_results=[{"content": "full text from spill"}],
                **{StateKeys.TOOL_RESULTS: [{"content": "truncated"}]},
            )
        )

        assert len(_global_cache()) == 1
        entry = next(iter(_global_cache().values()))
        assert "full text from spill" in json.dumps(entry, ensure_ascii=False, default=str)
