# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""notify_judge `state.reads` 切片投喂回归（触发器来源轮次不弹通知）。

漏声明形态（2026-11-12 修复）：`trigger.origin` 未进 manifest `state.reads`
→ 切片后恒读不到 → 「触发来源轮次由 agent 自主决定是否通知」的收尾门恒不
命中，触发器轮的收尾文本又被自动打标弹一次通知——正是该门要消除的双响
（用户裁定 2026-10-21：收尾文本不再自动打标）。

本用例按 manifest 声明切片投喂（与生产 invoker 同形），断言触发器轮静默、
普通轮照常判定（切片修复不得吞掉正常通知面）。
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

from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "notify_judge_state_reads_slice_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    mod.set_view_checker(None)  # 查看态门不参与本用例
    return mod


MANIFEST_READS = manifest_reads(_DIR)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "pipeline_id": "p-1",
        "messages": [
            {"role": "user", "content": "看下这份报告"},
            {"role": "assistant", "content": "搞定了，报告已生成", "seq": 7},
        ],
    }
    state.update(overrides)
    return state


def _judge(state: dict[str, Any]) -> dict[str, Any]:
    mod = _load_plugin_module()
    plugin = mod.NotifyJudge(config={})
    fed = slice_feed(state, MANIFEST_READS)
    return asyncio.run(plugin.judge(fed))


class TestDeclaration:
    def test_trigger_origin_declared(self) -> None:
        assert "trigger.origin" in visible_keys(MANIFEST_READS), (
            "trigger.origin 未声明 → 切片后触发器轮收尾门恒假（双响回归）"
        )


class TestTriggerRoundUnderSliceFeed:
    def test_trigger_round_sends_no_notification_under_slice_feed(self) -> None:
        updates = _judge(_state(**{"trigger.origin": "trigger"}))

        assert updates == {}, f"触发器轮不得打标通知：{updates}"

    def test_chat_round_still_notifies_under_slice_feed(self) -> None:
        """非触发器轮（origin=chat）照常走快切判定并打标。"""
        updates = _judge(_state(**{"trigger.origin": "chat"}))

        ops = updates["messages"]["_ops"]
        assert ops[0]["seq"] == 7
        assert ops[0]["msg"]["metadata"]["notify_worthy"] is True

    def test_absent_origin_still_notifies_under_slice_feed(self) -> None:
        """无 origin 的管道（历史形态）行为不变。"""
        updates = _judge(_state())

        assert updates["messages"]["_ops"][0]["msg"]["metadata"]["notify_worthy"] is True
