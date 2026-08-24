#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

trigger_review → review_agent 子管道（B 路径）链路。

trigger_review 经 chat.send_message 创建分支派发 review_agent 子管道
（与任务执行同构）：带血缘声明（root 根形式）、登记 task.owned.* 到被复盘
任务管道（前端任务树挂载）、get_report 按复盘管道 state 聚合行轮询——真实
完成才落 completed，不产出空 lessons 的假成功。chat 能力缺席或派发失败时
local_degrade 兜底。

agent_id/source 命名沿用 src/memory/maintenance/service.py 约定：
- agent_id = "review_agent"（config/agents/system/review_agent.yaml）
- source = "tool_review"（触发来源溯源）

channel_api 退役批次 5（2026-08-21，review P1-2 sidecar 化）：
- src/review/ 的审批状态机（review_service/models）与媒体审阅
  （media_review_service/media_reviewer）已迁入本插件包，http.handle 按
  path 分发 9 端点（/ext/review_service/reviews/**，语义对齐原
  /ext/channel_api/reviews/**——routes_reviews.py handler 迁入）。
- media-review multipart 经内核透传 raw_body base64 + _parse_multipart
  （email 标准库，与 multimodal/channel_api 同构）。

[来源: docs/tasks/task_10_system_plugins.md AC-09-4]
[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 5]
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

# hindsight_memory 插件目录（wiring.py 所在处）加入 sys.path
_HINDSIGHT_MEMORY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hindsight_memory"))
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)

# 审批域（P1-2 接真，channel_api 退役批次 5）：状态机 + 媒体审阅迁入本插件包后
# 平铺导入（server.py 运行目录即插件目录，与 workspace_service 同款惯例）。
import media_review_service  # noqa: E402
from review_service import get_review_service  # noqa: E402
from wiring import build_memory_backend  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("review_service")

# review_agent 配置 ID（与 config/agents/system/review_agent.yaml 对齐）
_REVIEW_AGENT_ID = "review_agent"
# 触发来源标记（与 src/memory/maintenance/service.py _try_launch_review_agent tags.source 对齐）
_REVIEW_SOURCE = "tool_review"

# 复盘报告存储：review_id -> report dict
# report 含 status: pending(子管道已起,报告未回写) / running(子管道进行中) /
# completed(子管道真实完成,报告已落) / failed(子管道失败)
_reports: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    """当前时间 ISO 串（复盘管道登记时间戳）。"""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat()

# 长期记忆后端（IMemoryBackend），用于把复盘报告持久化到 Hindsight，供跨会话检索/注入。
# 由插件宿主在加载时注入；未注入时 store_report 仅写内存 _reports（降级，不崩）。
_memory_backend: Any = None


def set_memory_backend(backend: Any) -> None:
    """注入 IMemoryBackend 实例（HindsightBackend）。

    生产环境由 review 插件宿主把 hindsight_memory.get_memory_backend() 产出的
    后端注入进来；测试环境传 AsyncMock。传 None 可重置为未注入（仅内存路径）。
    """
    global _memory_backend
    _memory_backend = backend


async def store_report(review_id: str, report: dict[str, Any]) -> None:
    """内部方法：回写 review_agent 子管道产出的报告。

    触发时机：review_agent 子管道跑完，通过 memory.store 工具或 event-bus 完成事件
    回调本方法（事件监听接线留 TODO）。当前由 get_report 按需查询 / 外部调用方直接
    注入。不暴露为 MCP 工具（内部 API）。

    落库策略：
    - 内存：始终写 ``_reports[review_id]``，供 get_report 立即轮询。
    - 长期记忆：若 ``_memory_backend`` 已注入，调用 ``backend.add`` 把整份报告
      （JSON）落到 Hindsight，memory_type=``review``，tags 含 ``review_id:<id>`` 与
      ``review_report``，source=``review_agent``，供后续会话检索/注入。

    Args:
        review_id: trigger 阶段分配的复盘 ID。
        report: review_agent 产出的完整报告（lessons/improvements/recommendations 等）。
    """
    existing = _reports.get(review_id, {})
    existing.update(report)
    existing["review_id"] = review_id
    existing["status"] = "completed"
    existing["updated_at"] = time.time()
    _reports[review_id] = existing

    # 持久化到长期记忆后端（Hindsight）。未注入时降级，仅保留内存路径。
    # user_id 固定 "review" bank：冷读时调用方只有 review_id、不知道 task_id，
    # 无法定向查 task_id bank——bank 固定后 get_report 冷读才能定向检索
    # （task_id 保留在 content JSON 内不丢；存量 task_id bank 报告接受召回不全）。
    # metadata 携带 review_id 定向键：hindsight-client 0.9.x aretain 的
    # metadata 是 dict[str,str] pydantic 校验，tags 以 list 塞入曾致写入
    # 必炸（报告从未真正持久化，2026-08-19 批 C 取证）——tags 序列化与
    # 定向键组装由 HindsightBackend.add 装配处负责（键值全 str）。
    if _memory_backend is not None:
        try:
            await _memory_backend.add(
                user_id="review",
                content=json.dumps(existing, ensure_ascii=False),
                memory_type="review",
                tags=[f"review_id:{review_id}", "review_report"],
                source="review_agent",
                metadata={"review_id": review_id},
            )
        except Exception as exc:  # noqa: BLE001 — 记忆后端失败不崩复盘回写
            logger.warning(
                "[Review] 报告落 Hindsight 失败 review_id=%s: %s", review_id, exc
            )

    logger.info(
        "[Review] 报告已回写 review_id=%s lessons=%d",
        review_id,
        len(existing.get("lessons", [])),
    )


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
        "status": "completed",
        "mode": "local_degrade",
        "created_at": time.time(),
    }


@plugin.tool(
    name="trigger_review",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "summary": {"type": "string"},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
            "metrics": {"type": "object", "default": {}},
        },
        "required": ["task_id", "summary"],
    },
    description="Trigger a review for a completed task and generate experience report",
)
async def trigger_review(
    task_id: str,
    summary: str,
    artifacts: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a post-task review via review_agent sub-pipeline (B path).

    深度复盘经 chat.send_message 创建分支派发 review_agent 管道；派发成功 →
    报告 running，get_report 轮询复盘管道 state 聚合行，真实完成才落
    completed。chat 能力缺席/派发失败 → 本地降级兜底（不产空 lessons 假成功）。

    Returns:
        review_id + status:
        - running (pipeline): 复盘管道已派发
        - completed (local_degrade): 降级产出基础报告
    """
    review_id = f"review_{uuid.uuid4().hex[:8]}"
    artifacts = artifacts or []
    metrics = metrics or {}

    # ── GAP-1：深度复盘经 chat.send_message 起 review_agent 管道 ──
    # 不再"启动即 completed（乐观，空 lessons）"：派发成功 → 报告 running，
    # get_report 轮询复盘管道状态（pipeline-state 聚合）真实完成才落 completed。
    # chat capability 缺席 / 派发失败 → local_degrade 兜底（保留既有降级语义）。
    try:
        chat = plugin.get_capability("chat")
    except KeyError:
        chat = None
    if chat is not None:
        params = {
            "create": True,
            "background": True,
            "message": (
                f"对任务 {task_id} 进行深度复盘。" + "\n" + f"任务摘要：{summary}"
                + ("\n" + "产物：" + ", ".join(artifacts) if artifacts else "")
                + ("\n" + f"指标：{json.dumps(metrics, ensure_ascii=False)}" if metrics else "")
                + "\n" + "请产出结构化复盘报告（总结 / 教训 lessons / 建议 recommendations）。"
            ),
            "user_id": "review_system",
            "state": {
                "task.id": task_id,
                "review.id": review_id,
                "review.summary": summary,
                "review.artifacts": artifacts,
                "review.metrics": metrics,
            },
            # 血缘：根形式（系统组件，诚实声明复盘来源——不伪造父/默认 session）
            "lineage": {"root": True, "origin": {"kind": "plugin", "source": "review"}},
        }
        try:
            resp = await chat.call("send_message", params)
            pipeline_id = (
                str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
            )
        except Exception as exc:  # noqa: BLE001 — 派发失败降级，不崩复盘入口
            logger.error(
                "[Review] 复盘管道派发失败 review_id=%s: %s", review_id, exc
            )
            pipeline_id = ""
        if pipeline_id:
            # 触发方登记（管道树数据链）：复盘管道是任务管道的子管道——把新管道
            # id 记回任务管道 state（task.owned.<id>.*，与 task_submit 同款契约），
            # 前端任务树据此把复盘管道挂到被复盘任务下。
            if task_id:
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
                                f"task.owned.{pipeline_id}.status": "running",
                                f"task.owned.{pipeline_id}.scope": "non_container",
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
            _reports[review_id] = {
                "review_id": review_id,
                "task_id": task_id,
                "summary": summary,
                "artifacts": artifacts,
                "metrics": metrics,
                "lessons": [],
                "recommendations": [],
                "status": "running",
                "mode": "pipeline",
                "pipeline_id": pipeline_id,
                "created_at": time.time(),
            }
            logger.info(
                "[Review] 复盘管道已创建 review_id=%s pipeline_id=%s",
                review_id,
                pipeline_id,
            )
            return {
                "review_id": review_id,
                "status": "running",
                "mode": "pipeline",
                "pipeline_id": pipeline_id,
            }

    report = _local_degrade_report(review_id, task_id, summary, artifacts, metrics)
    _reports[review_id] = report
    return {
        "review_id": review_id,
        "status": "completed",
        "mode": "local_degrade",
        "lessons_count": len(report["lessons"]),
    }


@plugin.tool(
    name="review.get_report",
    schema={
        "type": "object",
        "properties": {
            "review_id": {"type": "string"},
        },
        "required": ["review_id"],
    },
    description="Get review report by ID (polls sub-pipeline run status: completed only after the run truly finishes)",
)
async def get_report(review_id: str) -> dict[str, Any]:
    """Retrieve a stored review report.

    - 报告已回写（store_report 调用过）：返回完整报告，status=completed
    - 子管道进行中：经 pipeline-executor.get_run_status 惰性轮询 run 状态——
      run 真实完成才落 completed，失败落 failed，进行中保持 running
    - 内存未命中（sidecar 重启/回收丢内存）：冷读兜底——经记忆后端从
      Hindsight 取回已持久化报告（见 _cold_read_report）
    - 未找到：返回 error
    """
    report = _reports.get(review_id)
    if report is None:
        report = await _cold_read_report(review_id)
        if report is None:
            return {"error": "review not found", "review_id": review_id}
    # 子管道进行中：轮询复盘管道状态，真实完成才落 completed。
    if report.get("status") == "running" and report.get("pipeline_id"):
        await _maybe_finalize_on_pipeline_completion(report)
    return report


async def _cold_read_report(review_id: str) -> dict[str, Any] | None:
    """冷读兜底：sidecar 重启后从 Hindsight 取回已持久化报告（G3）。

    主路径（缺陷②修复）：走 HindsightBackend.get_documents（hindsight
    documents API）按 ``review_id:<id>`` tag 服务端精确过滤取回文档**原文**
    ``original_text``。recall 返回的是抽取后事实（world/observation/
    experience），原文 JSON 永不命中、``types=['memory']`` 原文召回模式
    不存在（422）——旧相似度 search 路径对 hindsight 后端恒 miss
    （2026-08-19 批 C 真实 API 取证）。

    回落路径：后端无 get_documents（旧形态/非 hindsight）→ 既有相似度
    ``search(query, user_id, top_k, memory_type)`` 路径——以 review_id 为
    query 在固定 "review" bank 召回 top_k=5，逐条解析 content JSON，
    **精确匹配 review_id 字段 == 入参**才采纳（防相似度误召回）。

    - ``_memory_backend`` 未注入（降级）→ 返回 None（not found 语义不变）；
    - 检索失败（RuntimeError 形态）→ 告警并降级，不崩 get_report
      （与写侧落库失败降级同风格）；
    - 命中 → 反序列化回填 ``_reports[review_id]``（后续轮询语义照常）并返回。

    Args:
        review_id: 要取回的复盘 ID。

    Returns:
        完整报告 dict；无命中/降级/失败返回 None。
    """
    if _memory_backend is None:
        return None

    parsed = await _cold_read_via_documents(review_id)
    if parsed is not None:
        return parsed

    # 回落：相似度 search（兼容仅 search 的旧形态后端）
    try:
        rows = await _memory_backend.search(
            query=review_id,
            user_id="review",
            top_k=5,
            memory_type="review",
        )
    except Exception as exc:  # noqa: BLE001 — 检索失败退回 not found，不崩冷读
        logger.warning(
            "[Review] 报告冷读 Hindsight 失败 review_id=%s: %s", review_id, exc
        )
        return None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("review_id") == review_id:
            _reports[review_id] = parsed
            logger.info("[Review] 报告冷读回填 review_id=%s", review_id)
            return parsed
    return None


async def _cold_read_via_documents(review_id: str) -> dict[str, Any] | None:
    """冷读主路径：documents API 按 tag 精确取回报告原文。

    get_documents 是 HindsightBackend 的扩展方法（非 IMemoryBackend 端口
    面）——后端未提供（旧形态替身/非 hindsight 后端）时返回 None 交回落
    路径；提供则按 ``review_id:<id>`` tag any_strict 服务端过滤，逐条解析
    ``original_text``，**精确匹配 review_id 字段 == 入参**才采纳（防脏数据
    误中）。

    Args:
        review_id: 要取回的复盘 ID。

    Returns:
        完整报告 dict；后端无该方法/无命中/失败返回 None。
    """
    get_docs = getattr(_memory_backend, "get_documents", None)
    if not callable(get_docs):
        return None
    try:
        docs = await get_docs(
            user_id="review",
            tags=[f"review_id:{review_id}"],
            tags_match="any_strict",
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — 文档面失败回落 search 路径
        logger.warning(
            "[Review] 报告冷读 documents 面失败 review_id=%s: %s", review_id, exc
        )
        return None
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        content = doc.get("original_text")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("review_id") == review_id:
            _reports[review_id] = parsed
            logger.info(
                "[Review] 报告冷读回填（documents 原文）review_id=%s", review_id
            )
            return parsed
    return None


async def _maybe_finalize_on_pipeline_completion(report: dict[str, Any]) -> None:
    """按复盘管道 state 聚合行终结报告状态（派发路径的轮询）。

    - 行 status=completed → 报告 completed（mode=pipeline，内容取 raw_result）
    - 行 status=failed → 报告 failed（诚实状态，不伪造完成）
    - running/查询失败/行缺失 → 保持 running（可重复轮询，不崩）
    """
    pipeline_id = report.get("pipeline_id") or ""
    try:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 轮询失败保持现状
        logger.warning(
            "[Review] 复盘管道状态轮询失败 pipeline_id=%s: %s", pipeline_id, exc
        )
        return
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("pipeline_id") == pipeline_id),
        None,
    )
    if row is None:
        return
    # 聚合行无独立 "status" 键（此前读它恒 None → 报告永久卡 running，
    # 2026-08-19 e2e 实测）：终态判定 = task.status（任务域）或
    # ended/current_phase（执行域），两者任一信号到位即可。
    status = row.get("task.status") or row.get("status")
    ended = row.get("ended") is True or row.get("current_phase") == "exit"
    if status == "completed" or (ended and status != "failed"):
        report["status"] = "completed"
        report["mode"] = "pipeline"
        raw = row.get("raw_result")
        if isinstance(raw, str) and raw.strip():
            report["summary"] = raw
            report["lessons"] = [raw.strip()]
        logger.info(
            "[Review] 复盘管道完成 review_id=%s pipeline_id=%s",
            report.get("review_id"),
            pipeline_id,
        )
    elif status == "failed" or (ended and status == "failed"):
        report["status"] = "failed"
        logger.warning(
            "[Review] 复盘管道失败 review_id=%s pipeline_id=%s",
            report.get("review_id"),
            pipeline_id,
        )


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize review service + 注入记忆后端。"""
    backend = build_memory_backend(plugin)
    if backend:
        set_memory_backend(backend)
        logger.info("[Review] 记忆后端已注入，复盘报告将持久化")
    else:
        logger.warning("[Review] 记忆后端未注入，复盘报告仅存内存（降级）")


# ══ reviews 域 HTTP 面（channel_api 退役批次 5：P1-2 接真）═══════════════════
# 返回契约与既有 17 插件 http.handle 一致：ToolExecutionResult{success,data}，
# data 为 HttpHandleResponse{status,headers,body,body_encoding}（body base64）。
# 业务错误沿 routes_reviews 源语义：NOT_FOUND/INVALID/INTERNAL 以 200 +
# {"error": {"code", "message"}} 返回（前端 reviewStore 读 data.error 降级）；
# 协议级错误（非 multipart / 缺 file 字段 / multipart 解析失败）400。

_PREFIX = "/ext/review_service/reviews"

# 全局媒体审阅服务实例（与 routes_reviews 同款懒单例）
_media_review_service: Any = None


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把任意 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def _error(status: int, message: str) -> dict[str, Any]:
    """协议级错误响应：{success:true, data:{status, body}}（插件全权控制响应形态）。"""
    return _ok(_json_response({"error": {"code": str(status), "message": message}}, status))


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _parse_multipart(content_type: str, body_bytes: bytes) -> dict[str, Any]:
    """解析 multipart/form-data（内核透传的 raw_body base64 解码后的字节）。

    返回 {字段名: 值}；文件字段值为 {filename, content_type, data(bytes)}，
    普通字段为 str。用 email.parser 解析（标准库，无外部依赖）——
    与 multimodal/channel_api server._parse_multipart 同构随迁。
    """
    import email  # noqa: PLC0415
    from email.policy import default as default_policy  # noqa: PLC0415

    header = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body_bytes, policy=default_policy)
    fields: dict[str, Any] = {}
    if not msg.is_multipart():
        return fields
    parts = msg.get_payload()
    if not isinstance(parts, list):  # pragma: no cover —— 防御 typeshed
        return fields
    for part in parts:
        if not isinstance(part, email.message.Message):  # pragma: no cover
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        filename = part.get_filename()
        if filename is not None:
            data = part.get_payload(decode=True) or b""
            fields[name] = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            }
        else:
            payload = part.get_payload(decode=True)
            fields[name] = (
                payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
            )
    return fields


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
        return _error(400, f"invalid upload body: {exc}")

    content_type = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "content-type" and v:
            content_type = str(v)
            break
    if "multipart/form-data" not in content_type:
        return _error(400, "media-review requires multipart/form-data")

    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _error(400, f"multipart parse failed: {exc}")

    file_field = fields.get("file")
    if not isinstance(file_field, dict) or not file_field.get("data"):
        return _error(400, "missing or empty 'file' field")

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
        # 清理临时文件
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

    # 如果有存储的审阅结果，直接返回
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

    # 更新 media_files
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

    # 更新审批的 metadata
    review.metadata["media_files"] = media_files
    review.metadata["media_review_results"] = review_results

    return {
        "review_id": review_id,
        "added_count": len(added),
        "attachments": added,
    }


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
        "HTTP endpoint handler for /ext/review_service/** (reviews domain 9 endpoints, "
        "channel_api 退役批次 5 P1-2 接真)"
    ),
)
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

    def _qint(key: str, default: int) -> int:
        try:
            return int(q[key]) if key in q else default
        except (TypeError, ValueError):
            return default

    try:
        # POST /media-review（multipart：file + media_type）
        # POST /media-review（multipart：file + media_type）——handler 自产完整响应
        if path == f"{_PREFIX}/media-review" and method == "POST":
            return await _reviews_media_review(raw_body, headers)
        # POST ""（create）
        if path == _PREFIX and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await _reviews_create(body)))
        # GET ""（list，query: task_id/limit）
        if path == _PREFIX and method == "GET":
            return _ok(
                _json_response(
                    await _reviews_list(
                        task_id=q.get("task_id", ""),
                        limit=_qint("limit", 50),
                    )
                )
            )
        # /{review_id} 系列
        if path.startswith(_PREFIX + "/"):
            rest = path[len(_PREFIX) + 1 :]
            if "/" not in rest:
                if rest and method == "GET":
                    return _ok(_json_response(await _reviews_get(rest)))
            else:
                rid, action = rest.split("/", 1)
                if action == "feedback" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await _reviews_submit_feedback(rid, body)))
                if action == "viewed" and method == "POST":
                    return _ok(_json_response(await _reviews_mark_viewed(rid)))
                if action == "cancel" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await _reviews_cancel(rid, body)))
                if action == "media-metadata" and method == "GET":
                    return _ok(_json_response(await _reviews_media_metadata(rid)))
                if action == "attachments" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await _reviews_add_attachments(rid, body)))

        logger.warning("review http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("review http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()
