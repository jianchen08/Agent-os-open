# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""todo_track manifest 持久化声明契约（1.2.26 状态丢失根修）。

内核每轮终态注销热缓存（server.rs run 收尾 ``registry.remove``），下轮冷
恢复只恢复 ``pipeline_state`` 表里的 ``persistent_fields`` 声明键——未声明
的键每轮丢一次。``context.todo_text`` 未声明 = merge 基座恒空，清单只剩最
新一轮增量（前端角标/计划页「只显示最新的」实测根因，2026-10-28）。

本文件同时是回归闸：删声明 = 面板退化回单轮增量，测试必须红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_MANIFEST = _REPO / "plugins" / "shared" / "todo_track" / "plugin.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


class TestPersistentFields:
    """累计状态必须落库：冷恢复只认 persistent_fields 声明键。"""

    @pytest.mark.parametrize(
        ("key", "why"),
        [
            ("context.todo_text", "清单累计真值——缺则每轮 merge 基座为空"),
            (
                "context.todo_state_v",
                "一次性自愈标记——缺则每轮重放近轮消息（状态永远停在旧版本判定）",
            ),
        ],
    )
    def test_key_declared(self, manifest: dict, key: str, why: str) -> None:
        persistent = manifest.get("persistent_fields", [])
        assert key in persistent, f"persistent_fields 缺 {key}（{why}）"

    def test_not_declared_volatile(self, manifest: dict) -> None:
        """per-run 易变键才进 volatile_keys：清单状态跨轮存活是契约本身。"""
        volatile = manifest.get("state", {}).get("volatile_keys", [])
        assert "context.todo_text" not in volatile
        assert "context.todo_state_v" not in volatile


class TestExtractStepWiring:
    """双步骤挂载面不变（fold 输入 / extract 输出）。"""

    def test_steps_declared(self, manifest: dict) -> None:
        steps = {s["name"] for s in manifest["capabilities"]["steps"]}
        assert steps == {"todo.fold", "todo.extract"}
