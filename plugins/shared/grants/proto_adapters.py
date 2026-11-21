"""三种 proto-grant 只读投影——统一账本查询面而不动既有存储（ADR D3）。

设计真值：执行方案 §8 投影规格表 + 调研 §4（合并表）。

| proto-grant | 真源（v1 不动） | 投影形态 |
|---|---|---|
| 管道级免审批 | ``permission_modes_pipeline.json``（ADR 2026-11-12） | grant(subject_kind=pipeline, granularity=task, tool='*') |
| 永久命令白名单 | ``permanent_whitelist.json``（同上） | grant(granularity=standing, tool=命令指纹) |
| 写区授权 | zone entries / ``task.authorized_read_zones``（ADR 2026-09-24） | grant(scope_json.resource_pattern) |

规则：投影**只读、实时**——每次查询现读真源，JSON 删改即时反映；不落 grants.db、
不产生账本事件；对账测试防漂移由 P1 落（授予面双写后）。

写区授权的真源（zone entries）经 ``project.load_zones`` 服务读取（ADR
2026-09-24）；P1 起由 security_check 侧
``approval_scope.zone_entries_via_project_service`` 提供真源 loader 注入本模块
（同步 ``project_zone_grants`` / 异步 ``project_zone_grants_async`` 双形态）。

读面缺省语义：真源文件缺失/损坏/条目畸形 → 投影为空或跳条目（真源缺省 = 无
授予，fail-closed 方向——投影只收缩授权面，不放大）。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: 投影 grant 的 source 固定值（执行方案 §7 source 枚举之一）。
PROTO_SOURCE = "proto_projection"

#: 管道级权限档合法值（security_check 四档；表外值跳过 = 不投影 = 收缩授权面）。
_PIPELINE_MODES = ("default", "accept_edits", "auto", "bypass")


def _grant(
    *,
    proto_kind: str,
    grant_id: str,
    subject_kind: str,
    subject_id: str,
    tool: str,
    action: str,
    scope: dict[str, Any],
    granularity: str,
    pipeline_id: str | None = None,
) -> dict[str, Any]:
    """投影 grant 形态 = grants 表行同构（source=proto_projection；uses=0 无库态）。"""
    return {
        "grant_id": grant_id,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "tool": tool,
        "action": action,
        "scope": scope,
        "granularity": granularity,
        "status": "active",
        "max_uses": None,
        "uses": 0,
        "created_at": None,
        "expires_at": None,
        "source": PROTO_SOURCE,
        "evidence": None,
        "pipeline_id": pipeline_id,
        "parent_task_id": None,
        "proto_kind": proto_kind,
    }


def _read_json(path: str | Path) -> Any | None:
    """实时读真源；缺失/损坏 → None（调用方投影为空）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def project_pipeline_modes(path: str | Path) -> list[dict[str, Any]]:
    """管道级免审批投影：``{pipeline_id: mode}`` → task grant（tool='*'）。

    假设（形态标注）：subject_kind=pipeline、subject_id=pipeline_id、
    granularity=task、scope.mode=档位值——档位到授权面的语义映射由 P1 消费方定义。
    """
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    grants: list[dict[str, Any]] = []
    for pipeline_id, mode in data.items():
        if not isinstance(pipeline_id, str) or not pipeline_id:
            continue
        if not isinstance(mode, str) or mode not in _PIPELINE_MODES:
            continue
        grants.append(
            _grant(
                proto_kind="pipeline_mode",
                grant_id=f"proto:pipeline_mode:{pipeline_id}",
                subject_kind="pipeline",
                subject_id=pipeline_id,
                tool="*",
                action="*",
                scope={"mode": mode},
                granularity="task",
                pipeline_id=pipeline_id,
            )
        )
    return grants


def project_whitelist(path: str | Path) -> list[dict[str, Any]]:
    """永久命令白名单投影：``[signature, ...]`` → standing grant（tool=命令指纹）。

    假设（形态标注）：真源无主体字段，v1 白名单全局有效 → subject=user/*；
    action='*'（指纹已含命令全形，工具维度不限定）。
    """
    data = _read_json(path)
    if not isinstance(data, list):
        return []
    grants: list[dict[str, Any]] = []
    for signature in data:
        if not isinstance(signature, str) or not signature.strip():
            continue
        grants.append(
            _grant(
                proto_kind="command_whitelist",
                grant_id=f"proto:command_whitelist:{signature}",
                subject_kind="user",
                subject_id="*",
                tool=signature,
                action="*",
                scope={"kind": "command_signature"},
                granularity="standing",
            )
        )
    return grants


def project_zone_grants(loader: Callable[[], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """写区授权投影（同步 loader）：zone entries → grant(scope.resource_pattern)。

    loader 契约（P1 起经 security_check 既有读取路径接线，见
    ``security_check.approval_scope.zone_entries_via_project_service``）：无参调用，
    返回条目列表，每条至少 ``{"path": <授权路径模式>}``，可选 ``pipeline_id``；
    缺 ``path`` 的条目跳过。loader=None（服务承接面未接线）→ 空投影。
    服务承接面天然异步，异步 loader 走 :func:`project_zone_grants_async`。
    """
    if loader is None:
        return []
    try:
        entries = loader()
    except Exception:  # noqa: BLE001 — 真源服务不可达 = 投影为空（fail-closed 方向）
        return []
    return _zone_entries_to_grants(entries)


async def project_zone_grants_async(
    loader: Callable[[], list[dict[str, Any]]] | Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """写区授权投影（loader 可为同步或异步可调用；P1 真源接线入口）。

    ``project.load_zones`` 服务包装是 async 形态——本函数对其 await（同步
    loader 形态语义与 :func:`project_zone_grants` 完全一致：异常/None → 空投影，
    缺 ``path`` 条目跳过）。
    """
    if loader is None:
        return []
    try:
        entries = loader()
        if inspect.isawaitable(entries):
            entries = await entries
    except Exception:  # noqa: BLE001 — 真源服务不可达 = 投影为空（fail-closed 方向）
        return []
    return _zone_entries_to_grants(entries)


def _zone_entries_to_grants(entries: Any) -> list[dict[str, Any]]:
    """zone 条目 → 投影 grant（同步/异步 loader 共用整形面）。"""
    grants: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        pipeline_id = entry.get("pipeline_id")
        subject_id = pipeline_id if isinstance(pipeline_id, str) and pipeline_id else "*"
        grants.append(
            _grant(
                proto_kind="write_zone",
                grant_id=f"proto:write_zone:{subject_id}:{path}",
                subject_kind="pipeline",
                subject_id=subject_id,
                tool="*",
                action="write",
                scope={"resource_pattern": path},
                granularity="task",
                pipeline_id=subject_id if subject_id != "*" else None,
            )
        )
    return grants


def project_all(
    *,
    pipeline_modes_file: str | Path | None = None,
    whitelist_file: str | Path | None = None,
    zone_loader: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """统一查询面：三种 proto-grant 投影合并（未提供的源不计入）。"""
    grants: list[dict[str, Any]] = []
    if pipeline_modes_file is not None:
        grants.extend(project_pipeline_modes(pipeline_modes_file))
    if whitelist_file is not None:
        grants.extend(project_whitelist(whitelist_file))
    grants.extend(project_zone_grants(zone_loader))
    return grants
