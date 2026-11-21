# @feature: FP-0.2.二 权限校验 | @ci: python-coverage
"""P0-3 task_submit 权限面测试（父任务归属闸，state 层级键）。

安全契约（``_check_parent_ownership``）：L2/L3 可伪造 parent_task_id 指向他人的
同级任务，越权把子任务挂到别人的任务树下（继承其管道/工作空间/上下文）。
归属闸据此防线拦截：

- 层级真值 = 父任务 state 行的 ``task.submitted_by_level``（task_submit 出生
  契约一次写全；任务 = 管道，无 YAML 记录通道）。
- 合法链：子任务只能挂在「比自己更高层级」的祖先任务下（L2→父须 ==1；
  L3→父须 <3；性质：父层级严格低于调用方层级即放行）。
- fail-closed：父任务行缺失 / 层级键缺失 / state 读面不可用 → 一律拒绝，
  权限面不放宽。
- L1 不受约束（可提交根任务或挂在任意已存在任务下，存在性由后续校验保证）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── 路径注入：task_submit 工具目录 ──
# tool.py 顶部仅需 SDK（agentos_plugin_sdk，pip 安装）；state 读面为模块级
# 注入点（_state_reader），测试以假 reader 替换，不触达内核能力。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
for _p in (str(_TS_DIR),):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
# 弹出可能被先前测试缓存为别的 tool.py 的同名模块，确保本次拿到 task_submit 的 tool。
for _m in ("tool",):
    sys.modules.pop(_m, None)

import tool as ts_tool  # noqa: E402

TaskSubmitTool = ts_tool.TaskSubmitTool


def _parent_row(task_id: str, level: int | None) -> dict:
    """构造父任务 state 聚合行（level=None 模拟层级键缺失）。"""
    row: dict = {"pipeline_id": task_id}
    if level is not None:
        row["task.submitted_by_level"] = level
    return row


def _inject_state(monkeypatch: pytest.MonkeyPatch, rows: list[dict] | None) -> None:
    """注入 state 聚合读取器（None 模拟桥未就绪/读取失败）。"""
    monkeypatch.setattr(sys.modules["acceptance"], "_state_reader", (lambda: rows) if rows is not None else (lambda: None))


def _make_tool() -> TaskSubmitTool:
    return TaskSubmitTool()


# ═══════════════════════════════════════════════════════════
# 归属闸行为矩阵（放行 / 越权 / fail-closed）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_level,caller_level",
    [
        # L2 的父任务须由 L1 提交
        (1, 2),
        # L3 的父任务须由 L1/L2 提交
        (1, 3),
        (2, 3),
    ],
)
async def test_allows_ancestor_parent(monkeypatch, parent_level, caller_level) -> None:
    """父任务层级严格低于调用方层级 → 放行（合法父子链）。"""
    _inject_state(monkeypatch, [_parent_row("p1", parent_level)])

    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=caller_level, parent_task_id="p1"
    )

    assert ok is True
    assert err is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_level,caller_level",
    [
        (2, 2),  # 他人同级任务
        (3, 2),  # 更下层任务
        (3, 3),  # 他人同级任务（L3）
    ],
)
async def test_rejects_peer_or_deeper_parent(monkeypatch, parent_level, caller_level) -> None:
    """父任务层级 ≥ 调用方层级 → 越权拒绝（防同级/下级挂载）。"""
    _inject_state(monkeypatch, [_parent_row("p1", parent_level)])

    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=caller_level, parent_task_id="p1"
    )

    assert ok is False
    assert err is not None and "权限不足" in err


@pytest.mark.asyncio
async def test_rejects_missing_parent_for_l2(monkeypatch) -> None:
    """L2 的 parent_task_id 在 state 聚合中无行 → 「父任务不存在」拒绝。"""
    _inject_state(monkeypatch, [{"pipeline_id": "other0000001"}])

    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=2, parent_task_id="ghost"
    )

    assert ok is False
    assert err is not None and "父任务不存在" in err


@pytest.mark.asyncio
async def test_rejects_parent_without_level_key(monkeypatch) -> None:
    """父任务 state 行在场但无 task.submitted_by_level → 拒绝（无法证明归属）。"""
    _inject_state(monkeypatch, [_parent_row("p1", None)])

    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=2, parent_task_id="p1"
    )

    assert ok is False
    assert err is not None


@pytest.mark.asyncio
async def test_rejects_when_state_bridge_down(monkeypatch) -> None:
    """state 读面不可用（桥未注入/读取失败）→ fail-closed 拒绝，不放宽。"""
    _inject_state(monkeypatch, None)

    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=2, parent_task_id="p1"
    )

    assert ok is False
    assert err is not None


@pytest.mark.asyncio
async def test_l1_unconstrained() -> None:
    """L1 不受归属约束（根 Agent，可提交根任务或挂在任意已存在任务下）。"""
    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=1, parent_task_id="anything"
    )

    assert ok is True
    assert err is None


@pytest.mark.asyncio
async def test_skips_when_no_parent_id() -> None:
    """无 parent_task_id（根任务）→ 放行（存在性/层级由其它校验保证）。"""
    ok, err = await _make_tool()._check_parent_ownership(
        parent_agent_level=2, parent_task_id=None
    )

    assert ok is True
    assert err is None


# ═══════════════════════════════════════════════════════════
# 集成：execute() 伪造 parent_task_id → INSUFFICIENT_PERMISSION
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_rejects_forged_parent_task_id(monkeypatch) -> None:
    """execute：L2 提交时 parent_task_id 指向另一 L2 任务 → INSUFFICIENT_PERMISSION。

    归属闸在基础闸门段早拦截（先于目标/依赖校验），伪造的父任务 id 到不了
    派发段。
    """
    _inject_state(monkeypatch, [_parent_row("victim-l2-task", 2)])
    tool = _make_tool()

    result = await tool.execute(
        {
            "goal": {"title": "子任务", "description": "测试子任务描述"},
            "parent_agent_level": 2,
            "parent_task_id": "victim-l2-task",  # 伪造：指向另一 L2 的任务
            "task_scope": "non_container",
        }
    )

    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"
