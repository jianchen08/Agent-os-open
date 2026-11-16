#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

review_ops → review_agent 子管道（B 路径）链路。

review_ops(action='trigger') 经 chat.send_message 创建分支派发 review_agent
子管道（与任务执行同构）：带血缘声明（root 根形式）、登记 task.owned.* 到
被复盘任务管道（前端任务树挂载）、review_ops(action='get_report') 按复盘
管道 state 聚合行轮询——真实
完成才落 completed，不产出空 lessons 的假成功。chat 能力缺席或派发失败时
local_degrade 兜底。

agent_id/source 命名沿用 src/memory/maintenance/service.py 约定：
- agent_id = "review_agent"（config/agents/system/review_agent.yaml）
- source = "tool_review"（触发来源溯源）

HTTP 面：审批状态机（review_service/models）与媒体审阅
（media_review_service/media_reviewer）在本插件包内，http.handle 按 path
分发 9 端点（/ext/review_service/reviews/**，语义对齐原
/ext/channel_api/reviews/**）；media-review multipart 经内核透传
raw_body base64 + _parse_multipart（email 标准库）。

[来源: docs/tasks/task_10_system_plugins.md AC-09-4]
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

# `_error` = protocol_error：本插件上传面协议级错误契约（success:true 包
# HTTP status + 结构化错误体），与 error 的扁平失败信封不同。
import media_review_service  # noqa: E402
from _root_api import (  # noqa: E402
    as_dict as _state_as_dict,  # noqa: E402
    build_memory_backend,  # noqa: E402  （共享裸名模块，共享根经 bootstrap 入 path）
    decode_body as _decode_body,
    json_response as _json_response,
    now_iso_utc as _now_iso,  # noqa: E402
    ok as _ok,
    parse_multipart as _parse_multipart,
    protocol_error as _error,
)
from report_store import (  # noqa: E402
    extract_recall_fact as _extract_recall_fact,
    read_report as _read_report_file,
    write_report as _write_report_file,
)
from review_service import get_review_service  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("review_service")

# review_agent 配置 ID（与 config/agents/system/review_agent.yaml 对齐）
_REVIEW_AGENT_ID = "review_agent"
# 触发来源标记（与 src/memory/maintenance/service.py _try_launch_review_agent tags.source 对齐）
_REVIEW_SOURCE = "tool_review"

# 复盘预算缺省上限（§10.5-3）：单复盘管道可纳入的管道数。超出拒绝派发
# （fail-closed），由调用方拆分为多个复盘。可用 plugin_configs 的
# max_pipelines_per_review 覆盖。
_MAX_PIPELINES_PER_REVIEW = 5

# ── 报告存储契约（F9 单通道）──
# 报告本体 = 文件面（被复盘任务 ws 内 docs/working/reviews/{review_id}.json，
# 见 report_store）；state 只存索引：被复盘任务行上的
# ``task.review.{review_id}.{status,pipeline_id,summary,created_at,path}``
# （经 pipeline-state.update 写，本插件 manifest state.writes 声明
# ``task.review.*``）；Hindsight 只留
# 面向召回的抽取事实（report_store.extract_recall_fact）。进程内不持报告
# 副本——get_report = state 索引 → 文件面直读。

# 索引键前缀（锚行 = 被复盘任务的 state 行；pipeline_id 键承载锚行自身 id
# 同名约定——行上 pipeline_id 即被复盘任务 id）。
_REVIEW_IDX_PREFIX = "task.review."

# 长期记忆后端（IMemoryBackend），仅承载面向召回的抽取事实写入。
# 由插件宿主在加载时注入；未注入时跳过记忆写入（文件面与索引不受影响）。
_memory_backend: Any = None


def set_memory_backend(backend: Any) -> None:
    """注入 IMemoryBackend 实例（HindsightBackend）。

    生产环境由 review 插件宿主把 hindsight_memory.get_memory_backend() 产出的
    后端注入进来；测试环境传 AsyncMock。传 None 可重置为未注入。
    """
    global _memory_backend
    _memory_backend = backend


def _idx_key(review_id: str, field: str) -> str:
    """复盘索引扁平键：``task.review.{review_id}.{field}``。"""
    return f"{_REVIEW_IDX_PREFIX}{review_id}.{field}"


async def _update_review_index(
    task_id: str, review_id: str, fields: dict[str, Any]
) -> bool:
    """经 pipeline-state.update 写复盘索引（锚行 = 被复盘任务行）。

    task.* 前缀任务域写面（与 task_evaluate/task_reminder 同通道）。写入
    失败仅告警返回 False——派发已成功的复盘不因索引故障回滚，但该复盘在
    读面不可见（get_report 以索引为唯一定位通道），日志留痕可诊断。

    Returns:
        索引是否写全；task_id 缺席（无锚行可挂）恒 False。
    """
    if not task_id:
        logger.warning(
            "[Review] 复盘索引无锚行可写（task_id 缺席）review_id=%s", review_id
        )
        return False
    try:
        handle = plugin.get_capability("pipeline-state")
        await handle.call(
            "update",
            {
                "pipeline_id": task_id,
                "fields": {_idx_key(review_id, k): v for k, v in fields.items()},
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 索引失败不崩调用方，留痕降级
        logger.warning(
            "[Review] 复盘索引写入失败 review_id=%s task=%s: %s", review_id, task_id, exc
        )
        return False


async def _list_state_rows() -> list[dict[str, Any]] | None:
    """pipeline-state.list 聚合行；能力缺席/调用失败返回 None（读面不可用）。"""
    try:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 读面不可用与「无此复盘」分开表态
        logger.warning("[Review] state 聚合读取失败: %s", exc)
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _task_ws_root(task_row: dict[str, Any]) -> str:
    """锚行 → 被复盘任务执行空间根（task.ws_meta.path；出生即写全的坐标键）。"""
    ws_meta = _state_as_dict(task_row.get("task.ws_meta"), field="task.ws_meta")
    return str(ws_meta.get("path") or "") if ws_meta else ""


async def _store_recall_facts(review_id: str, report: dict[str, Any]) -> None:
    """把复盘结论以抽取事实形态写入 Hindsight（不存报告全文 JSON）。

    未注入后端时跳过；写入失败仅告警——记忆面是召回增强，不是报告真相
    （真相 = 文件面本体 + state 索引）。
    """
    if _memory_backend is None:
        return
    try:
        await _memory_backend.add(
            user_id="review",
            content=_extract_recall_fact(review_id, report),
            memory_type="review",
            tags=[f"review_id:{review_id}", "review_report"],
            source="review_agent",
            metadata={"review_id": review_id},
        )
    except Exception as exc:  # noqa: BLE001 — 记忆后端失败不崩复盘回写
        logger.warning(
            "[Review] 抽取事实落 Hindsight 失败 review_id=%s: %s", review_id, exc
        )


async def store_report(review_id: str, report: dict[str, Any]) -> None:
    """内部方法：报告产出后的旁路登记（抽取事实入 Hindsight）。

    报告本体与状态生命周期不经本方法（文件面 + state 索引在产出点直写）；
    本方法只补写面向召回的抽取事实。保留原名维持内部调用语义（不暴露为
    MCP 工具）。

    Args:
        review_id: trigger 阶段分配的复盘 ID。
        report: 完整报告 dict（抽取事实的素材）。
    """
    await _store_recall_facts(review_id, report)


def _local_degrade_report(
    review_id: str,
    task_id: str,
    summary: str,
    artifacts: list[str],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """能力不可用时的本地降级报告（非 LLM，仅基础结构化，保证不崩）。

    保留极简 metrics 分析仅为兜底，真正分析由 review_agent 做。
    """
    lessons: list[str] = []
    if metrics:
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and value < 0.5:
                lessons.append(f"Metric '{key}' scored low ({value}): consider improvement")
    if not lessons:
        lessons.append("Local degrade mode: pipeline-executor unavailable, LLM review skipped")

    return {
        "review_id": review_id,
        "task_id": task_id,
        "summary": summary,
        "artifacts": artifacts,
        "metrics": metrics,
        "lessons": lessons,
        "recommendations": [
            "Re-run review when pipeline-executor capability is available for LLM analysis"
        ],
        "status": "degraded",
        "mode": "local_degrade",
        "created_at": time.time(),
    }


def _build_review_pipeline_params(
    review_id: str,
    task_id: str,
    summary: str,
    artifacts: list[str],
    metrics: dict[str, Any],
    *,
    target_pipelines: list[str] | None = None,
    budget: int = _MAX_PIPELINES_PER_REVIEW,
) -> dict[str, Any]:
    """构造 review_agent 管道的 chat.send_message 参数（含血缘与 state 预置）。"""
    return {
        "create": True,
        "background": True,
        "message": (
            f"对任务 {task_id} 进行深度复盘（任务摘要/产物清单/指标见 state 的 review.*）。"
            + "\n" + "请产出结构化复盘报告（总结 / 教训 lessons / 建议 recommendations）。"
        ),
        "user_id": "review_system",
        "state": {
            "task.id": task_id,
            "review.id": review_id,
            "review.summary": summary,
            "review.artifacts": artifacts,
            "review.metrics": metrics,
            # 被复盘集合与预算（§10.5-2）：read_execution_detail 的查询边界
            # 依据——只许查集合内管道，越界拒绝。
            "review.target_pipelines": target_pipelines or ([task_id] if task_id else []),
            "review.budget_pipelines": budget,
            # 血缘扁平键：根形式（系统组件，诚实声明复盘来源——不伪造父/默认
            # session）；血缘方案本插件自持，内核零知识
            "lineage.root": True,
            "lineage.origin.kind": "plugin",
            "lineage.origin.source": "review",
        },
    }


async def _register_review_pipeline_owner(chat: Any, task_id: str, pipeline_id: str) -> None:
    """触发方登记（管道树数据链）：把新管道 id 记回任务管道 state。

    复盘管道是任务管道的子管道——按 task.owned.<id>.* 键面写回
    （与 task_submit 同款契约），前端任务树据此挂载；失败仅告警不影响复盘。
    """
    try:
        await chat.call(
            "send_message",
            {
                "pipeline_id": task_id,
                "message": f"登记复盘管道（{pipeline_id}）。",
                "user_id": "review_system",
                "no_dispatch": True,
                "state": {
                    f"task.owned.{pipeline_id}.title": f"复盘 {task_id}",
                    f"task.owned.{pipeline_id}.created_at": _now_iso(),
                    f"task.owned.{pipeline_id}.submitted_by": "review_system",
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — 登记失败不影响复盘
        logger.warning(
            "[Review] 复盘管道登记到任务管道失败（不影响执行）| task=%s | err=%s",
            task_id,
            exc,
        )


async def _dispatch_review_pipeline(
    review_id: str,
    task_id: str,
    summary: str,
    artifacts: list[str],
    metrics: dict[str, Any],
    *,
    target_pipelines: list[str] | None = None,
    budget: int = _MAX_PIPELINES_PER_REVIEW,
) -> dict[str, Any] | None:
    """派发 review_agent 管道并落 running 报告，返回 running 响应或 None 走降级。

    深度复盘经 chat.send_message 创建分支派发；派发成功 → 报告 running，
    get_report 轮询真实完成才落 completed。chat 能力缺席/派发失败 → None。
    """
    try:
        chat = plugin.get_capability("chat")
    except KeyError:
        return None
    if chat is None:
        return None

    params = _build_review_pipeline_params(
        review_id, task_id, summary, artifacts, metrics,
        target_pipelines=target_pipelines, budget=budget,
    )
    try:
        resp = await chat.call("send_message", params)
        pipeline_id = str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
    except Exception as exc:  # noqa: BLE001 — 派发失败降级，不崩复盘入口
        logger.error("[Review] 复盘管道派发失败 review_id=%s: %s", review_id, exc)
        pipeline_id = ""
    if not pipeline_id:
        return None

    if task_id:
        await _register_review_pipeline_owner(chat, task_id, pipeline_id)

    # 复盘索引出生即写全（锚行 = 被复盘任务行）：status/pipeline_id/summary/
    # created_at 一次落位，get_report 据此定位与轮询（报告本体在管道完成后
    # 经文件面落盘、索引补 path）。
    await _update_review_index(
        task_id,
        review_id,
        {
            "status": "running",
            "pipeline_id": pipeline_id,
            "summary": summary,
            "created_at": time.time(),
        },
    )
    logger.info("[Review] 复盘管道已创建 review_id=%s pipeline_id=%s", review_id, pipeline_id)
    return {
        "review_id": review_id,
        "status": "running",
        "mode": "pipeline",
        "pipeline_id": pipeline_id,
    }


# ── review_ops：复盘操作面（trigger / get_report 二合一） ────────────────
# 原 trigger_review / review_get_report 两件按工具面审查 §十 合并计划收敛为一件
# （工具面退役·任务 C）：action 必填枚举，oneOf 按 action 隔离参数集（两向参数集
# 不相交）；实现体原地复用不复制（_trigger_review / _get_report）。
_REVIEW_OPS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["trigger", "get_report"],
            "description": "复盘动作（必填枚举）：trigger=为已完成任务触发复盘并生成"
                           "经验报告；get_report=按 review_id 取报告（真实完成才落 "
                           "completed，可轮询）",
        },
        "task_id": {
            "type": "string",
            "description": "仅 action='trigger'：被复盘任务 ID。缺省=当前任务（系统"
                           "注入）；复盘其它任务时显式填",
        },
        "summary": {
            "type": "string",
            "description": "仅 action='trigger'（必填）：一句话说明本次任务做了什么",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "仅 action='trigger'：产物路径清单（可选）",
        },
        "metrics": {
            "type": "object",
            "additionalProperties": True,
            "default": {},
            "description": "仅 action='trigger'：任务指标（可选，自由对象）",
        },
        "pipeline_ids": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "仅 action='trigger'：多管道复盘的被复盘管道清单（与 "
                           "task_id 去重合并登记；超预算拒绝）",
        },
        "review_id": {
            "type": "string",
            "description": "仅 action='get_report'（必填）：复盘报告 id（trigger 返回）",
        },
    },
    "required": ["action"],
    "oneOf": [
        {
            "properties": {
                "action": {"const": "trigger"},
                "task_id": {},
                "summary": {},
                "artifacts": {},
                "metrics": {},
                "pipeline_ids": {},
            },
            "required": ["action", "summary"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "action": {"const": "get_report"},
                "review_id": {},
            },
            "required": ["action", "review_id"],
            "additionalProperties": False,
        },
    ],
}

_REVIEW_OPS_DESCRIPTION = (
    "复盘操作面（二合一）。"
    "action='trigger'：为已完成任务触发复盘并生成经验报告——深度复盘经 "
    "chat.send_message 派发 review_agent 子管道（返回 status='running'）；"
    "chat 缺席/派发失败降级本地基础报告（status='degraded'，不产空 lessons 假成功）；"
    "被复盘集合超预算（max_pipelines_per_review，缺省 5）拒绝（status='rejected'，"
    "应拆分）。最小示例：review_ops(action='trigger', summary='<一句话说明本次任务"
    "做了什么>')（task_id 缺省=当前任务，复盘别的任务显式填）。"
    "action='get_report'：按 review_id 取报告（轮询复盘管道 state 聚合行，真实完成"
    "才落 completed；本体在被复盘任务 ws 文件面）。最小示例："
    "review_ops(action='get_report', review_id='review_xxxxxxxx')。"
)


@plugin.tool(name="review_ops", schema=_REVIEW_OPS_SCHEMA,
             description=_REVIEW_OPS_DESCRIPTION)
async def review_ops(
    action: str,
    task_id: str = "",
    summary: str = "",
    artifacts: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    pipeline_ids: list[str] | None = None,
    review_id: str = "",
) -> dict[str, Any]:
    """按 action 分发到触发/取报实现体；跨动作参数运行时教学错误（错误教改法）。"""
    if action == "trigger":
        if review_id:
            return {
                "success": False,
                "error": "review_id 仅 action='get_report' 可用；trigger 必填 "
                         "summary（task_id 等可选），示例: review_ops(action='trigger', "
                         "summary='<一句话说明本次任务做了什么>')",
            }
        return await _trigger_review(
            task_id=task_id, summary=summary, artifacts=artifacts,
            metrics=metrics, pipeline_ids=pipeline_ids,
        )
    if action == "get_report":
        foreign = [k for k, v in (("task_id", task_id), ("summary", summary),
                                  ("pipeline_ids", pipeline_ids)) if v]
        if foreign or artifacts or metrics:
            return {
                "success": False,
                "error": "task_id/summary/artifacts/metrics/pipeline_ids 仅 "
                         "action='trigger' 可用；get_report 只需 review_id，示例: "
                         "review_ops(action='get_report', review_id='<复盘id>')",
            }
        if not review_id:
            return {
                "success": False,
                "error": "action='get_report' 缺必填参数: review_id，示例: "
                         "review_ops(action='get_report', review_id='<复盘id>')",
            }
        return await _get_report(review_id)
    return {
        "success": False,
        "error": (f"action 非法: {action!r}（允许: 'trigger' | 'get_report'）；"
                  "示例: review_ops(action='trigger', summary='<一句话说明') 或 "
                  "review_ops(action='get_report', review_id='<复盘id>')"),
    }


async def _trigger_review(
    task_id: str = "",
    summary: str = "",
    artifacts: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    pipeline_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Trigger a post-task review via review_agent sub-pipeline (B path).

    深度复盘经 chat.send_message 创建分支派发 review_agent 管道；派发成功 →
    报告 running，get_report 轮询复盘管道 state 聚合行，真实完成才落
    completed。chat 能力缺席/派发失败 → 本地降级兜底（不产空 lessons 假成功）。

    被复盘集合与预算（§10.5）：pipeline_ids 为多管道复盘的显式清单（与
    task_id 去重合并），派发时整体登记进复盘管道 state（review.target_pipelines
    + review.budget_pipelines），read_execution_detail 据此过滤越界查询；
    集合大小超预算（max_pipelines_per_review 配置，缺省 5）→ 拒绝派发。

    Returns:
        review_id + status:
        - running (pipeline): 复盘管道已派发
        - degraded (local_degrade): 降级产出基础报告（不报 completed，防轮询方误判）
        - rejected: 超复盘预算（fail-closed，调用方应拆分）
    """
    if not summary:
        return {
            "success": False,
            "error": "缺少必需参数: summary（一句话说明本次任务做了什么）",
        }

    review_id = f"review_{uuid.uuid4().hex[:8]}"
    artifacts_l = artifacts or []
    metrics_l = metrics or {}

    # ── 被复盘集合登记与预算执法（§10.5-2/3）：task_id 优先，清单去重合并；
    # 超预算拒绝（fail-closed），不派发不产生报告。──
    target_pipelines = [task_id] if task_id else []
    for pid in pipeline_ids or []:
        if pid and pid not in target_pipelines:
            target_pipelines.append(pid)
    try:
        budget = int(plugin.get_config().get("max_pipelines_per_review") or _MAX_PIPELINES_PER_REVIEW)
    except Exception:  # noqa: BLE001 — 配置读取失败回缺省
        budget = _MAX_PIPELINES_PER_REVIEW
    if len(target_pipelines) > budget:
        return {
            "review_id": review_id,
            "status": "rejected",
            "error": (
                f"复盘预算超限：被复盘管道 {len(target_pipelines)} 个 > 上限 {budget}，"
                "请拆分为多个复盘"
            ),
        }

    # ── 深度复盘经 chat.send_message 起 review_agent 管道；trigger 端点
    # 只报 running/degraded 两种状态，completed 由 get_report 轮询复盘管道
    # 聚合行终结落位（文件面 + 索引）；降级兜底语义见
    # _dispatch_review_pipeline / _local_degrade_report。──
    running = await _dispatch_review_pipeline(
        review_id, task_id, summary, artifacts_l, metrics_l,
        target_pipelines=target_pipelines, budget=budget,
    )
    if running is not None:
        return running

    report = _local_degrade_report(review_id, task_id, summary, artifacts_l, metrics_l)
    await _persist_report_artifact(review_id, task_id, report)
    return {
        "review_id": review_id,
        "status": "degraded",
        "mode": "local_degrade",
        "lessons_count": len(report["lessons"]),
    }


async def _persist_report_artifact(
    review_id: str, task_id: str, report: dict[str, Any]
) -> None:
    """报告产出点的落位三连（终态写面）：文件面本体 → state 索引 → 抽取事实。

    - 文件面：本体落被复盘任务 ws（锚行 task.ws_meta.path 定根）；ws 不可
      解析（无锚行/无坐标）时告警跳过，该报告读面将显式报「产物缺失」。
    - state 索引：status=completed + ws 相对坐标（读面唯一定位通道）。
    - Hindsight：仅抽取事实（report_store.extract_recall_fact）。
    """
    rows = await _list_state_rows()
    ws_root = ""
    if rows is not None and task_id:
        task_row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id), None
        )
        if task_row is not None:
            ws_root = _task_ws_root(task_row)
    rel_path = ""
    if ws_root:
        try:
            rel_path = _write_report_file(ws_root, review_id, report)
        except (OSError, ValueError) as exc:
            logger.warning(
                "[Review] 报告本体落盘失败 review_id=%s task=%s: %s",
                review_id, task_id, exc,
            )
    else:
        logger.warning(
            "[Review] 报告无执行空间锚点可落盘 review_id=%s task=%s", review_id, task_id
        )
    await _update_review_index(
        task_id,
        review_id,
        {
            "status": "completed",
            "path": rel_path,
            "summary": str(report.get("summary") or ""),
            "created_at": time.time(),
        },
    )
    await _store_recall_facts(review_id, report)
    logger.info(
        "[Review] 报告已落位 review_id=%s lessons=%d path=%s",
        review_id,
        len(report.get("lessons") or []),
        rel_path or "(缺锚点)",
    )


async def _get_report(review_id: str) -> dict[str, Any]:
    """Retrieve a review report（单通道：state 索引 → 文件面直读）。

    - state 读面不可用（能力缺席/调用失败）：error=pipeline state unavailable
      （与「无此复盘」分开表态——索引在 state，读面失明不等于复盘不存在）
    - 索引无此 review_id：error=review not found
    - 索引 running：轮询复盘管道聚合行，真实完成才终态化（本体落文件面、
      索引补 path），失败诚实落 failed；仍在跑保持 running
    - 索引 completed：按索引 ws 相对坐标 + 锚行 task.ws_meta.path 直读本体；
      本体缺失/损坏：error=review report artifact missing（状态仍在，
      文件面是本体唯一真相）
    - 索引 failed：返回失败态（无本体——失败复盘不产报告文件）
    """
    rows = await _list_state_rows()
    if rows is None:
        return {
            "success": False,
            "error": "pipeline state unavailable",
            "review_id": review_id,
        }
    status_key = _idx_key(review_id, "status")
    anchor = next(
        (r for r in rows if r.get(status_key) is not None), None
    )
    if anchor is None:
        return {"success": False, "error": "review not found", "review_id": review_id}

    task_id = str(anchor.get("pipeline_id") or "")
    status = str(anchor.get(status_key) or "")
    pipeline_id = str(anchor.get(_idx_key(review_id, "pipeline_id")) or "")

    # running → 轮询复盘管道，真实终态才落位（可重复轮询，幂等）。
    if status == "running" and pipeline_id:
        finalized = await _maybe_finalize_on_pipeline_completion(
            review_id, task_id, pipeline_id, rows, anchor
        )
        if finalized is not None:
            return finalized

    if status == "completed":
        rel_path = str(anchor.get(_idx_key(review_id, "path")) or "")
        ws_root = _task_ws_root(anchor)
        body = (
            _read_report_file(ws_root, review_id) if ws_root and rel_path else None
        )
        if body is not None:
            return body
        return {
            "success": False,
            "error": "review report artifact missing",
            "review_id": review_id,
            "task_id": task_id,
            "report_path": rel_path,
        }

    # running（终态化未就绪/索引缺 pipeline_id）/ failed：索引标量直接成响应。
    response: dict[str, Any] = {
        "review_id": review_id,
        "task_id": task_id,
        "status": status,
        "mode": "pipeline" if pipeline_id else "local_degrade",
        "summary": str(anchor.get(_idx_key(review_id, "summary")) or ""),
        "created_at": anchor.get(_idx_key(review_id, "created_at")),
    }
    if pipeline_id:
        response["pipeline_id"] = pipeline_id
    return response


async def _maybe_finalize_on_pipeline_completion(
    review_id: str,
    task_id: str,
    pipeline_id: str,
    rows: list[dict[str, Any]],
    anchor: dict[str, Any],
) -> dict[str, Any] | None:
    """按复盘管道 state 聚合行终结报告（终态判定保持依赖管道行——正确形态）。

    - 行 status=completed → 报告 completed（内容取 raw_result），本体落文件
      面 + 索引补 path + 抽取事实入 Hindsight；
    - 行 status=failed → 索引落 failed（诚实状态，不伪造完成，无本体文件）；
    - running/行缺失 → None（保持 running，可重复轮询，不崩）。

    终态写失败（索引/文件面）不回滚轮询结论：返回体按已判终态组装，读面
    后续以索引/文件现状表态（completed 而无 path → 产物缺失显式报错）。
    """
    row = next(
        (r for r in rows if r.get("pipeline_id") == pipeline_id), None
    )
    if row is None:
        return None
    # 聚合行无独立 "status" 键：终态判定 = task.status（任务域）或
    # ended/current_phase（执行域），两者任一信号到位即可。
    status = row.get("task.status") or row.get("status")
    ended = row.get("ended") is True or row.get("current_phase") == "exit"

    if status == "completed" or (ended and status != "failed"):
        report: dict[str, Any] = {
            "review_id": review_id,
            "task_id": task_id,
            "summary": str(anchor.get(_idx_key(review_id, "summary")) or ""),
            "status": "completed",
            "mode": "pipeline",
            "pipeline_id": pipeline_id,
            "created_at": anchor.get(_idx_key(review_id, "created_at")),
        }
        raw = row.get("raw_result")
        if isinstance(raw, str) and raw.strip():
            report["summary"] = raw.strip()
            report["lessons"] = [raw.strip()]
        rel_path = ""
        ws_root = _task_ws_root(anchor)
        if ws_root:
            try:
                rel_path = _write_report_file(ws_root, review_id, report)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "[Review] 报告本体落盘失败 review_id=%s task=%s: %s",
                    review_id, task_id, exc,
                )
        else:
            logger.warning(
                "[Review] 报告无执行空间锚点可落盘 review_id=%s task=%s", review_id, task_id
            )
        await _update_review_index(
            task_id,
            review_id,
            {"status": "completed", "path": rel_path},
        )
        await _store_recall_facts(review_id, report)
        logger.info(
            "[Review] 复盘管道完成 review_id=%s pipeline_id=%s path=%s",
            review_id, pipeline_id, rel_path or "(缺锚点)",
        )
        return report

    if status == "failed" or (ended and status == "failed"):
        await _update_review_index(task_id, review_id, {"status": "failed"})
        logger.warning(
            "[Review] 复盘管道失败 review_id=%s pipeline_id=%s", review_id, pipeline_id
        )
        return {
            "review_id": review_id,
            "task_id": task_id,
            "status": "failed",
            "mode": "pipeline",
            "pipeline_id": pipeline_id,
            "summary": str(anchor.get(_idx_key(review_id, "summary")) or ""),
            "created_at": anchor.get(_idx_key(review_id, "created_at")),
        }

    return None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize review service + 注入记忆后端（抽取事实召回面）。"""
    backend = build_memory_backend(plugin)
    if backend:
        set_memory_backend(backend)
        logger.info("[Review] 记忆后端已注入，复盘抽取事实可召回")
    else:
        logger.warning("[Review] 记忆后端未注入，复盘仅文件面+state 索引（无召回增强）")


# ══ reviews 域 HTTP 面 ═══════════════════════════════════════════════════════
# 返回契约与既有 17 插件 http.handle 一致：ToolExecutionResult{success,data}，
# data 为 HttpHandleResponse{status,headers,body,body_encoding}（body base64）。
# 业务错误沿 routes_reviews 源语义：NOT_FOUND/INVALID/INTERNAL 以 200 +
# {"error": {"code", "message"}} 返回（前端 reviewStore 读 data.error 降级）；
# 协议级错误（非 multipart / 缺 file 字段 / multipart 解析失败）400。

_PREFIX = "/ext/review_service/reviews"

# 全局媒体审阅服务实例（与 routes_reviews 同款懒单例）
_media_review_service: Any = None

# 响应封装/请求解析助手（json_response/_ok/_error(protocol_error)/_decode_body/
# _parse_multipart）：公共实现 plugins/shared/http_json.py（文件头已导入）。
# `_error` 沿公共实现 http_json.error 的参数序 (message, status)。


def get_media_review_service() -> Any:
    """获取全局媒体审阅服务单例（懒加载，与 routes_reviews 同款）。"""
    global _media_review_service  # noqa: PLW0603
    if _media_review_service is None:
        _media_review_service = media_review_service.MediaReviewService()
    return _media_review_service


# ── 端点 handler（语义对齐 routes_reviews.py 各路由，剥 FastAPI 装饰器/require_auth）──


async def _reviews_create(body: dict[str, Any]) -> dict[str, Any]:
    """创建审批请求（原 POST /reviews —— body: task_id/thread_id/session_id/...）。"""
    service = get_review_service()
    review = await service.create_review(
        task_id=body.get("task_id", ""),
        thread_id=body.get("thread_id", ""),
        session_id=body.get("session_id", ""),
        tab_id=body.get("tab_id", ""),
        title=body.get("title", ""),
        description=body.get("description", ""),
        artifact_ids=body.get("artifact_ids"),
        priority=body.get("priority", "normal"),
        timeout_seconds=body.get("timeout_seconds"),
        metadata=body.get("metadata"),
    )
    return review.to_dict()


async def _reviews_list(task_id: str, limit: int = 50) -> dict[str, Any]:
    """获取任务的审批列表（原 GET /reviews，query: task_id/limit）。"""
    if not task_id:
        return {"items": [], "total": 0}
    service = get_review_service()
    return await service.list_reviews_by_task(task_id, limit=limit)


async def _reviews_get(review_id: str) -> dict[str, Any]:
    """获取审批详情（原 GET /reviews/{review_id}）。"""
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}
    return review.to_dict()


async def _reviews_submit_feedback(review_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """提交审批反馈（原 POST /reviews/{review_id}/feedback）。"""
    service = get_review_service()
    feedback = await service.submit_feedback(
        review_id=review_id,
        response_type=body.get("response_type", "approved"),
        overall_comment=body.get("overall_comment", ""),
        annotations=body.get("annotations"),
        user_id=body.get("user_id"),
    )
    if not feedback:
        return {"error": {"code": "INVALID", "message": "审批不存在或状态不允许反馈"}}
    return feedback.to_dict()


async def _reviews_mark_viewed(review_id: str) -> dict[str, Any]:
    """标记审批为已查看（原 POST /reviews/{review_id}/viewed）。"""
    service = get_review_service()
    success = await service.mark_as_viewed(review_id)
    return {"id": review_id, "viewed": success}


async def _reviews_cancel(review_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """取消审批（原 POST /reviews/{review_id}/cancel，body: reason）。"""
    service = get_review_service()
    reason = body.get("reason")
    success = await service.cancel_review(review_id, reason=reason)
    return {"id": review_id, "cancelled": success}


async def _reviews_media_review(raw_body: str, headers: dict[str, str] | None) -> dict[str, Any]:
    """上传文件并执行媒体审阅（原 POST /reviews/media-review，multipart: file + media_type）。

    文件经内核透传 raw_body base64 解出后落临时文件，审阅完即清理。
    media_type 留空时按文件扩展名自动推断（image/video）。

    本 handler 直接返回完整 HttpHandleResponse（其余端点由分发层统一包装）——
    协议级错误需携带真实 HTTP 状态（400）而非 200+error 体，故不走统一包装。
    """
    try:
        body_bytes = base64.b64decode(raw_body) if raw_body else b""
    except Exception as exc:  # noqa: BLE001
        return _error(f"invalid upload body: {exc}", 400)

    content_type = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "content-type" and v:
            content_type = str(v)
            break
    if "multipart/form-data" not in content_type:
        return _error("media-review requires multipart/form-data", 400)

    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _error(f"multipart parse failed: {exc}", 400)

    file_field = fields.get("file")
    if not isinstance(file_field, dict) or not file_field.get("data"):
        return _error("missing or empty 'file' field", 400)

    media_type = fields.get("media_type") or ""
    if isinstance(media_type, str):
        media_type = media_type.strip()

    filename = file_field.get("filename") or "upload"
    content: bytes = file_field["data"]
    suffix = os.path.splitext(filename)[1]  # noqa: PTH122
    tmp_dir = tempfile.mkdtemp(prefix="media_review_")
    tmp_path = os.path.join(tmp_dir, filename if filename else f"upload{suffix}")

    try:
        with open(tmp_path, "wb") as f:
            f.write(content)

        effective_media_type = media_type
        if not effective_media_type:
            try:
                effective_media_type = media_review_service._infer_media_type(tmp_path)
            except ValueError:
                return _ok(
                    _json_response(
                        {
                            "error": {
                                "code": "INVALID",
                                "message": (
                                    f"无法推断媒体类型，请显式指定 media_type"
                                    f"（文件: {filename}）"
                                ),
                            }
                        }
                    )
                )

        media_svc = get_media_review_service()
        result = await media_svc.review_media(tmp_path, effective_media_type)
        result_dict = result.to_dict()
        result_dict["media_type"] = effective_media_type
        result_dict["filename"] = filename
        return _ok(_json_response(result_dict))

    except FileNotFoundError as exc:
        return _ok(
            _json_response({"error": {"code": "NOT_FOUND", "message": str(exc)}})
        )
    except ValueError as exc:
        return _ok(_json_response({"error": {"code": "INVALID", "message": str(exc)}}))
    except Exception as exc:  # noqa: BLE001
        logger.error("[review] 媒体审阅失败 | error=%s", exc)
        return _ok(
            _json_response(
                {"error": {"code": "INTERNAL", "message": f"媒体审阅失败: {exc}"}}
            )
        )
    finally:
        if os.path.isfile(tmp_path):  # noqa: PTH113
            os.remove(tmp_path)  # noqa: PTH107
        if os.path.isdir(tmp_dir):  # noqa: PTH112
            os.rmdir(tmp_dir)  # noqa: PTH106


async def _reviews_media_metadata(review_id: str) -> dict[str, Any]:
    """获取审批关联的媒体元数据（原 GET /reviews/{review_id}/media-metadata）。

    优先返回 metadata.media_review_results 已存审阅结果；否则对
    metadata.media_files 逐条重新生成元数据摘要（PIL/PyAV 实时解析）。
    """
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}

    metadata = review.metadata
    review_results = metadata.get("media_review_results", {})
    media_files = metadata.get("media_files", [])

    if review_results:
        return {
            "review_id": review_id,
            "media_metadata": review_results,
        }

    # 否则尝试从 media_files 重新生成元数据
    if not media_files:
        return {
            "review_id": review_id,
            "media_metadata": [],
        }

    media_svc = get_media_review_service()
    metadata_list: list[dict[str, Any]] = []

    for file_info in media_files:
        file_path = file_info if isinstance(file_info, str) else file_info.get("path", "")
        media_type = ""
        if isinstance(file_info, dict):
            media_type = file_info.get("media_type", "")

        if not file_path or not os.path.isfile(file_path):  # noqa: PTH113
            metadata_list.append(
                {
                    "file_path": file_path,
                    "error": "文件不存在或路径无效",
                }
            )
            continue

        try:
            if not media_type:
                media_type = media_review_service._infer_media_type(file_path)
            meta = media_svc.get_media_metadata(file_path, media_type)
            metadata_list.append(meta)
        except (ValueError, FileNotFoundError) as exc:
            metadata_list.append(
                {
                    "file_path": file_path,
                    "error": str(exc),
                }
            )

    return {
        "review_id": review_id,
        "media_metadata": metadata_list,
    }


async def _reviews_add_attachments(review_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """为审批添加媒体附件（原 POST /reviews/{review_id}/attachments，JSON body）。

    body: {"files": [{"path", "media_type"} | str], "auto_review": bool}。
    auto_review=true 时对已存在文件执行媒体审阅并写入 metadata.media_review_results。
    """
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}

    files = body.get("files", [])
    auto_review = body.get("auto_review", False)

    if not files:
        return {"error": {"code": "INVALID", "message": "files 列表不能为空"}}

    media_files = review.metadata.get("media_files", [])
    review_results = review.metadata.get("media_review_results", {})

    added: list[dict[str, Any]] = []
    media_svc = get_media_review_service()

    for file_info in files:
        file_path = file_info.get("path", "") if isinstance(file_info, dict) else file_info
        media_type = file_info.get("media_type", "") if isinstance(file_info, dict) else ""

        if not file_path:
            added.append({"error": "缺少 path 字段"})
            continue

        # 如果未指定 media_type，尝试推断
        if not media_type:
            try:
                media_type = media_review_service._infer_media_type(file_path)
            except ValueError:
                added.append(
                    {
                        "file_path": file_path,
                        "error": "无法推断媒体类型",
                    }
                )
                continue

        entry = {"path": file_path, "media_type": media_type}
        media_files.append(entry)

        # 可选自动审阅
        review_result_dict: dict[str, Any] | None = None
        if auto_review and os.path.isfile(file_path):  # noqa: PTH113
            try:
                result = await media_svc.review_media(file_path, media_type)
                review_result_dict = result.to_dict()
                review_results[file_path] = review_result_dict
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[review] 附件审阅失败 | path=%s | error=%s",
                    file_path,
                    exc,
                )
                review_result_dict = {"error": str(exc)}
                review_results[file_path] = review_result_dict

        added.append(
            {
                **entry,
                "review_result": review_result_dict,
            }
        )

    review.metadata["media_files"] = media_files
    review.metadata["media_review_results"] = review_results

    return {
        "review_id": review_id,
        "added_count": len(added),
        "attachments": added,
    }


@plugin.tool(
    name="review_improvement",
    schema={
        "type": "object",
        "properties": {
            "cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "task_status": {"type": "string"},
                        "criteria": {"type": "object",
                                     "description": "验收指标名 → 是否通过(bool)"},
                        "symptom_note": {"type": "string"},
                        "trajectory_note": {"type": "string"},
                    },
                    "required": ["case_id", "task_status", "criteria"],
                },
            },
        },
        "required": ["cases"],
    },
    description=(
        "复盘改进建议：按分诊规则（config/plugins/review/triage_rules.yaml）"
        "对失败面做机制前置检查、三向分诊与杠杆映射，产出结构化改进建议。"
        "评估原则的数据化承载——改原则改 yaml，不改代码。"
    ),
)
async def review_improvement(cases: list[dict[str, Any]]) -> dict[str, Any]:
    import improvement
    try:
        return {"success": True, **improvement.suggest(cases)}
    except (OSError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description=(
        "HTTP endpoint handler for /ext/review_service/** (reviews domain 9 endpoints)"
    ),
)
async def _route_reviews_collection(
    path: str, method: str, q: dict[str, str], raw_body: str, headers: dict[str, str] | None
) -> dict[str, Any] | None:
    """media-review 与集合级（create/list）路由，未命中返回 None。"""
    if path == f"{_PREFIX}/media-review" and method == "POST":
        # multipart（file + media_type）——handler 自产完整响应
        return await _reviews_media_review(raw_body, headers)
    if path == _PREFIX and method == "POST":
        body = _decode_body(raw_body) or {}
        return _ok(_json_response(await _reviews_create(body)))
    if path == _PREFIX and method == "GET":
        try:
            limit = int(q["limit"]) if "limit" in q else 50
        except (TypeError, ValueError):
            limit = 50
        return _ok(_json_response(await _reviews_list(task_id=q.get("task_id", ""), limit=limit)))
    return None


async def _route_review_item_actions(
    rid: str, action: str, method: str, raw_body: str
) -> dict[str, Any] | None:
    """/{review_id}/{action} 动作表分发，未命中返回 None。"""
    actions: dict[tuple[str, str], Any] = {
        ("feedback", "POST"): lambda: _reviews_submit_feedback(rid, _decode_body(raw_body) or {}),
        ("viewed", "POST"): lambda: _reviews_mark_viewed(rid),
        ("cancel", "POST"): lambda: _reviews_cancel(rid, _decode_body(raw_body) or {}),
        ("media-metadata", "GET"): lambda: _reviews_media_metadata(rid),
        ("attachments", "POST"): lambda: _reviews_add_attachments(rid, _decode_body(raw_body) or {}),
    }
    fn = actions.get((action, method))
    if fn is None:
        return None
    return _ok(_json_response(await fn()))


async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 reviews 域 9 端点（语义对齐原 /ext/channel_api/reviews/**）。

    认证由 plugin.json http_endpoints auth=user 声明（dispatcher 层），
    handler 不读 _user。multipart（media-review）经 headers content-type 识别。
    """
    del plugin_id
    q = query or {}
    try:
        collection = await _route_reviews_collection(path, method, q, raw_body, headers)
        if collection is not None:
            return collection

        # /{review_id} 系列：单级 GET 详情；二级动作表分发
        if path.startswith(_PREFIX + "/"):
            rest = path[len(_PREFIX) + 1 :]
            if "/" not in rest:
                if rest and method == "GET":
                    return _ok(_json_response(await _reviews_get(rest)))
            else:
                rid, action = rest.split("/", 1)
                routed = await _route_review_item_actions(rid, action, method, raw_body)
                if routed is not None:
                    return routed

        logger.warning("review http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("review http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()
