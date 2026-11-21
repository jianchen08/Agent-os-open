"""任务状态投影纠偏（2026-11-10 装机实证 bug）。

现场：resume 续跑轮在飞（``run_status=running``），但 ``task.status`` 停在上一轮
终态 ``stopped`` → 面板显示"已停止"、``submit`` 被 400 拒（只收 failed/pending）。
判据只在"明确矛盾"的组合上纠偏，用户侧终态（completed）与无 run 簿记的行不动。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 平铺布局：本插件模块与共享根（_root_api 等）都靠裸名导入——共享根必须进
# sys.path，且逐出其它插件目录的同名模块代际（否则 http_api 解析到别家实现）。
_HERE = Path(__file__).resolve().parent
_SHARED_ROOT = _HERE.parent
for _name in ("http_api",):
    sys.modules.pop(_name, None)
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from http_api import derive_task_status  # noqa: E402

_MANIFEST = Path(__file__).resolve().parent / "plugin.json"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # 矛盾组：run 在飞 + 上一轮终态投影 → 纠偏为 running
        ({"task.status": "stopped", "run_status": "running"}, "running"),
        ({"task.status": "failed", "run_status": "running"}, "running"),
        # 边界组：run 簿记缺席 / 挂起 → 投影原样（suspended 是"等交互续投"的
        # 合法态，不得被读成 running）
        ({"task.status": "stopped"}, "stopped"),
        ({"task.status": "stopped", "run_status": "suspended"}, "stopped"),
        # 用户侧终态：不覆盖用户显式裁决
        ({"task.status": "completed", "run_status": "running"}, "completed"),
        # 空投影回落 pending（读面缺键语义）
        ({"run_status": ""}, "pending"),
    ],
)
def test_derive_task_status(row: dict[str, str], expected: str) -> None:
    assert derive_task_status(row) == expected


def test_manifest_declares_task_status_volatile() -> None:
    """task.status 必须是插件声明的 volatile 键。

    投影不许随 run 快照恢复（上一轮的 stopped 会被持久化写回，顶掉本轮复用
    复位）——这正是现场 bug 的根因；声明丢失即回归。
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    volatile = (manifest.get("state") or {}).get("volatile_keys") or []
    assert "task.status" in volatile, (
        "tasks 插件必须声明 state.volatile_keys 含 task.status"
        f"（实际: {volatile!r}）"
    )
