"""「当前管道有没有活跃子任务」的共享 state 读面（单一真相，多消费方同源）。

真相源 = pipeline state 聚合行：``lineage.parent_pipeline_id`` 匹配当前管道
且 ``task.status`` ∈ :data:`ACTIVE_TASK_STATUSES`。消费方（child_task_guard /
task_reminder 等 Output 插件）经各自 server.py on_load 注入的 pipeline-state
读取器取行，判定逻辑全部收敛在本模块——任何消费方不得自建第二套通道序。

``submitted_task_ids``（父管道行上的子任务挂号标记）是**加速标记而非独立
真相**：仅当 state 读面不可用（读取器未注入/读取失败）时作为回退链的廉价
前缀；标记在场不绕过主判定，标记缺失不改变结论。

回退链（读面不可用时）：加速标记 → task_service 枚举。服务获取两级收敛在
:func:`resolve_task_service`（ctx.get_service → tasks.service_access 进程内
单例）；枚举查询异常**上抛不吞**（fail-closed：吞掉会把服务故障伪装成
「无活跃子任务」，让父管道在仍有活跃子任务时提前终止/催评估）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentos_plugin_sdk.enum_utils import safe_enum_value

from .types import ACTIVE_TASK_STATUSES

logger = logging.getLogger(__name__)


async def read_state_rows(state_reader: Any) -> list[dict[str, Any]] | None:
    """经注入的读取器取 state 聚合行；不可用返回 None（调用方走回退链）。

    读取器约定签名 ``() -> list[dict]``（sync 或 async），行 = 扁平点号键。
    未注入（None）或读取异常均归一为 None——读取异常留 warning 痕迹后降级，
    不崩消费方插件步。
    """
    if state_reader is None:
        return None
    try:
        rows = state_reader()
        if asyncio.iscoroutine(rows):
            rows = await rows
        if not isinstance(rows, list):
            return None
        return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001 — 读面降级不崩消费方
        logger.warning("[active_children] state 聚合读取失败: %s", exc)
        return None


def active_child_ids_from_rows(
    rows: list[dict[str, Any]],
    pipeline_id: str,
) -> list[str]:
    """主判定（纯函数）：state 聚合行中当前管道的活跃子任务 id 全集。

    活跃 = ``lineage.parent_pipeline_id`` == 当前管道 且 ``task.status`` 活跃。
    ``pipeline_id`` 为空时不构成任何匹配（空串不会是合法父 id）。
    """
    if not pipeline_id:
        return []
    return sorted(
        str(row.get("pipeline_id") or "")
        for row in rows
        if str(row.get("lineage.parent_pipeline_id") or "") == pipeline_id
        and str(row.get("task.status") or "") in ACTIVE_TASK_STATUSES
        and row.get("pipeline_id")
    )


def resolve_task_service(ctx: Any) -> Any:
    """两级收敛的 TaskService 获取：ctx.get_service → 进程内单例。

    - ``ctx.get_service("task_service")`` 命中（同宿注册）→ 直接返回；
    - KeyError → ``tasks.service_access.get_task_service()`` 懒加载单例；
    - 模块不可达（任务域插件被移除/部署残缺）→ None（调用方按无服务降级），
      不裸抛中断插件步。
    """
    try:
        return ctx.get_service("task_service")
    except KeyError:
        pass
    try:
        from tasks.service_access import get_task_service  # noqa: PLC0415
    except ImportError as exc:
        logger.warning("[active_children] tasks.service_access 不可用: %s", exc)
        return None
    return get_task_service()


def _active_ids_via_task_service(
    task_service: Any,
    pipeline_id: str,
    task_id: str | None,
) -> list[str]:
    """回退枚举：task_service 按状态 × 血缘扫描 + 子任务枚举（fail-closed）。

    查询异常原样上抛——本函数不做任何吞并（见模块 docstring fail-closed 契约）。

    TaskStatus 以裸名 ``task_types`` 导入：可达本函数时 :func:`resolve_task_service`
    已成功加载 tasks.service_access，其自举已把 tasks 插件目录入 sys.path
    （task_types 与 service.py 裸名互导同一解析前提），枚举值形态与
    TaskStorage.list_by_status 的等值比较对齐。
    """
    seen: set[str] = set()
    from task_types import TaskStatus as TS  # noqa: PLC0415,N817

    if pipeline_id:
        for status_val in (TS.RUNNING, TS.PENDING, TS.EVALUATING):
            for t in task_service.list_by_status(status_val):
                if getattr(t, "parent_pipeline_id", None) == pipeline_id:
                    seen.add(t.id)
    if task_id:
        for st in task_service.list_subtasks(task_id):
            if safe_enum_value(st.status) in ACTIVE_TASK_STATUSES:
                seen.add(st.id)
    return sorted(seen)


async def find_active_children(
    pipeline_id: str,
    task_id: str | None,
    *,
    state_reader: Any = None,
    ctx: Any = None,
    submitted_marker: Any = None,
) -> tuple[bool, list[str]]:
    """单通道判定：当前管道是否有活跃子任务，返回 ``(是否有, 活跃 id 全集)``。

    通道序（收敛后唯一形态，所有消费方同源）：
    1. **主判定**：state 聚合行扫描（lineage + task.status，读面可用即定论，
       加速标记不参与）；
    2. 读面不可用 → 加速标记（``submitted_task_ids``，非独立真相：仅在权威
       通道缺席时表态，避免读面故障期误判「无子任务」提前放行）；
    3. 标记也缺失 → task_service 枚举（服务获取两级收敛；查询异常上抛）。
    """
    rows = await read_state_rows(state_reader)
    if rows is not None:
        ids = active_child_ids_from_rows(rows, pipeline_id)
        return (bool(ids), ids)

    if isinstance(submitted_marker, list) and submitted_marker:
        return True, [str(x) for x in submitted_marker]

    task_service = resolve_task_service(ctx)
    if task_service is None:
        return False, []
    ids = _active_ids_via_task_service(task_service, pipeline_id, task_id)
    return (bool(ids), ids)
