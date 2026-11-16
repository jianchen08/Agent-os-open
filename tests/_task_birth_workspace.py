"""出生协议第三段（工作空间同步创建）的测试替身，tests/ 各 task_submit 测试复用。

出生协议由三段扩为四段后，任何驱动真实派发的提交测试都必须注入工作空间准备器
（ADR-2026-10-17-task-birth-workspace-inclusion）：出生协议无「跳过准备」路径，
准备器缺席即提交失败。本模块只提供替身与接线函数，断言仍归各测试自身。

替身边界：准备器是**外部依赖**（workspace_lifecycle 插件服务，跨 sidecar 能力
调用），按测试纪律可 mock；本替身不触碰工作空间四形态决策（那是 workspace_lifecycle
插件内部实现，由其自身测试覆盖）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

__all__ = ["make_preparer", "inject_preparer"]


def make_preparer(
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> Any:
    """构造工作空间准备器替身。

    Args:
        result: 成功时返回的坐标 state 更新（默认 = 一个真实存在的临时目录，
            与生产侧「workspace 为真实路径字符串」的契约同形）。
        error: 失败时抛出的异常（模拟创建失败/服务缺席）。

    Returns:
        ``async preparer(*, pipeline_id, state, execution_context) -> dict``
    """
    if result is None:
        ws_dir = Path(tempfile.mkdtemp(prefix="agentos_test_ws_"))
        result = {
            "workspace": str(ws_dir),
            "task.ws_meta": {"mode": "plain", "path": str(ws_dir)},
        }

    async def _preparer(
        *, pipeline_id: str, state: dict[str, Any], execution_context: dict[str, Any]
    ) -> dict[str, Any]:
        if error is not None:
            raise error
        # 复刻 workspace_lifecycle 的幂等语义：出生 state 已带 workspace
        # （继承坐标已就位）→ 跳过创建，返回空坐标更新（无创建动作）。
        if state.get("workspace"):
            return {}
        return dict(result)

    return _preparer


def inject_preparer(tool_module: Any, preparer: Any = None) -> Any:
    """把准备器替身注入 task_submit tool 模块并返回它（默认成功路径替身）。"""
    fn = preparer or make_preparer()
    tool_module.set_workspace_preparer(fn)
    return fn


def reset_preparer(tool_module: Any) -> None:
    """复位模块级注入点（不泄漏到后续用例）。"""
    tool_module._workspace_preparer = None
