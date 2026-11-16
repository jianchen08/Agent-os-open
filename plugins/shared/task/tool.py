"""任务管理工具（简化版）"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

from state_machine import InvalidTransitionError
from task_types import TaskModel, TaskStatus

# 跨插件共享类型（ToolExecutionResult / BuiltinTool / Tool 及枚举与结果工厂）已上提到
# SDK 公共依赖层 agentos_plugin_sdk；任务领域类型以 plugins/shared/tasks/
# 为权威（0.2 平铺模块，由 server.py 将该目录注入 sys.path），直接 import。
#
# 注意：system/tasks 为平铺模块目录，其中 ``service`` / ``enum_utils`` 为通用名，
# 在 pytest 整 suite 单进程下会与 pipeline/input/security_check/service.py 等冲突。
# 故仅导入该目录下名称唯一的模块（task_types / state_machine）；``TaskService`` 仅用于
# 类型注解与 _get_task_service 内构造，改用 ``from __future__ import annotations`` +
# TYPE_CHECKING 守卫 + 方法内懒导入，避免在收集期命中被缓存的同名 service 模块。
from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

if TYPE_CHECKING:
    from datetime import datetime

    from service import TaskService

from _root_api import state_fields  # noqa: PLC0415 — plugins/shared 平铺模块

logger = logging.getLogger(__name__)


class StateRowsReadError(RuntimeError):
    """state 聚合读面故障（桥已注入但本次读取失败/返回非列表形态）。

    与「聚合里没有该任务」（各读面返回 None）显式区分：读取故障须在
    execute 边界翻译为 SERVICE_UNAVAILABLE 显式错误，不得落入"任务
    不存在"路径误导 LLM 重建任务。
    """


def _status_value(status: Any) -> str:
    """状态 → 展示值（TaskStatus 取 .value；枚举漂移保留的原串直通）。"""
    return status.value if hasattr(status, "value") else str(status)


def _caller_user_id_params(inputs: dict[str, Any]) -> dict[str, str]:
    """chat.send_message 的 user_id 兼容提示形参（ADR 2026-10-17-reverse-call-
    user-identity-injection）。

    身份真值由内核反调注入（`_user_id` 回带 → 桥 scope → 派发面），此处转发
    tool_args_inject 注入的调用者真实 user_id，仅为旧内核（无注入消费）保
    派发可行；无值时省略键（禁止合成用户——"task_manage" 类手写身份是 1017
    continue 静默无效事故的根因）。
    """
    user_id = inputs.get("user_id")
    if isinstance(user_id, str) and user_id:
        return {"user_id": user_id}
    return {}


def _as_utc(value: datetime) -> datetime:
    """时区归一：naive 视为 UTC 补 tzinfo，aware 原样返回。

    state/DB 读出的时间串（任务 started_at/completed_at、run created_at/
    ended_at）带不带偏移取决于写入方（内核 RFC3339 带偏移，旧路径不带），
    naive/aware 混用会使相减/比较抛 TypeError；凡从 state/DB 读出的时间
    先经此归一再运算，当前时间统一 datetime.now(timezone.utc)。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ④A 返工重开的证据清空 overlay（chat.send_message state 参数，内核恢复合并
# 之后应用）：完成证据键会从热恢复快照复活，不清则闸门信号②（位于工具路由
# 之前）拿上一轮的完成证据当轮收束——返工轮直接被"已完成"结束，纠正指令
# 根本进不了执行。null 值经内核 apply_state_overlay 原样入 run state，插件侧
# state.get() 读到 None 即证据缺席（无需物理删除）；三键均不在
# RESERVED_STATE_KEYS 保留字清单，overlay 校验放行（2026-10-19 已核实）。
_REOPEN_EVIDENCE_CLEAR_OVERLAY: dict[str, Any] = {
    "task_evaluation_completed": None,
    "evaluation.detected_result": None,
    "evaluate_reminder_total": None,
}

# 终态翻转统一清的失败/结束注记键（④A 返工重开与 retry 写面对齐共用）：
# 翻回 running 后残留旧 ended_at/失败原因会让重开轮顶着上一轮终态展示。
_TERMINAL_NOTE_KEYS = ("task.ended_at", "task.failure_class", "task.failure_reason")

# 返工重开同批清零的完成证据键（ADR 2026-10-28 D4-1）。键名以
# task_evaluate 的写面为准（plugins/shared/task_evaluate/tool.py）：
# task.evaluation = 评估结论大对象、task.eval_summary = 结论摘要、
# task.eval_total_calls = 评估调用计数（上限闸）、
# task.eval_retry_count = 逐指标重试计数（带旧值会让新轮直接撞重试上限）。
# 语义：返工重开 = 新的一次尝试，上一轮的评估结论不得对新轮生效。
_REOPEN_EVAL_EVIDENCE_KEYS = (
    "task.evaluation",
    "task.eval_summary",
    "task.eval_total_calls",
    "task.eval_retry_count",
)
# 清零值：结论/摘要/重试计数写 null（pipeline-state.update 无键删除语义，
# null = 证据缺席）；调用计数归零（新一轮的评估计数从 0 重新起）。
_REOPEN_EVAL_EVIDENCE_FIELDS: dict[str, Any] = {
    "task.evaluation": None,
    "task.eval_summary": None,
    "task.eval_total_calls": 0,
    "task.eval_retry_count": None,
}

# resume 入口守卫拦截集（真终态，ADR
# 2026-10-30-task-status-authority-vocabulary-convergence §4.3）：恢复执行
# 的入口（continue on stopped / change 恢复）对真终态拒绝——completed 的
# 裸恢复正是幽灵轮形态（返工重开走 continue 带指令职责面），failed 不可
# 恢复执行（重试走 retry/submit 职责面）。stopped 不在拦截列（恢复的
# 目标态本身就是 stopped → running）。
_RESUME_BLOCKED_STATUSES = frozenset({"completed", "failed"})


def _chat_dispatch_params(
    pipeline_id: str,
    message: str,
    inputs: dict[str, Any],
    state_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """chat.send_message 背景派发参数装配（注入/返工重开/重试共用）。

    身份由内核反调注入（ADR 2026-10-17-reverse-call-user-identity-injection）：
    不手写 user——user_id 形参（tool_args_inject 注入的调用者真实身份）仅为
    旧内核兼容提示，注入面被内核身份覆盖。
    state_overlay：经内核 validate_state_overlay 校验透传的派发 overlay
    （顶层扁平键，恢复合并之后应用——显式派发指令 > 恢复基线）。
    """
    params: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "message": message,
        "background": True,
        **_caller_user_id_params(inputs),
    }
    if state_overlay is not None:
        params["state"] = state_overlay
    return params


def _resolve_status_value(raw: str, task_ref: str) -> Any:
    """task.status 串 → TaskStatus；内核新增状态而本地枚举副本未同步（漂移）时
    保留原串展示（不再静默变 PENDING 触发错误的可操作动作），warning 留痕。"""
    try:
        return TaskStatus(raw)
    except (ValueError, AttributeError):
        logger.warning(
            "[TaskTool] 未知任务状态（枚举副本与内核漂移？），保留原串展示 | status=%s | task=%s",
            raw,
            task_ref,
        )
        return raw


def _pick_session_anchor(pid: str, origin_sess: str, row_thread: str) -> tuple[str, bool]:
    """聚合行会话锚点取舍，返回 (anchor, 是否降级命中 pipeline_id)。

    任务管道无 sessions 行，其 thread_id 恒等于自身 pipeline_id；出生侧
    lineage.origin_session_id 修正后为真 thread id。取「不等于自身
    pipeline_id」的那个，两侧语义偏差互为兜底。
    """
    anchor = (
        origin_sess
        if origin_sess and origin_sess != pid
        else (row_thread if row_thread and row_thread != pid else origin_sess or row_thread)
    )
    return anchor, not anchor or anchor == pid


def _append_workspace_meta(metadata: dict[str, Any], row: dict[str, Any]) -> None:
    """把聚合行的 workspace / task.ws_meta 原样并入任务元数据（原地追加，缺省跳过）。"""
    if row.get("workspace"):
        metadata["workspace"] = str(row["workspace"])
    # 工作空间坐标单键 task.ws_meta（state 单一真相源）；as_dict 兼容跨边界
    # JSON 字符串形态（state_fields 契约）——只 isinstance(dict) 会让字符串
    # 形态静默丢失（前端"打开工作空间"按钮失效）。
    ws_meta = state_fields.as_dict(row.get("task.ws_meta"), field="task.ws_meta")
    if ws_meta:
        metadata["ws_meta"] = ws_meta


def _is_task_pipeline_row(row: dict[str, Any]) -> bool:
    """任务行判定（与 tasks 插件 events.is_task_pipeline_row 同口径，ADR
    2026-11-06 D1，本地副本不引入跨插件 import）：``task.id`` 非空 ∨ 任一
    ``lineage.*`` 键非空。

    任务管道出生协议保证 task.id 非空（= pipeline_id）且带血缘键；会话
    主管道两者皆无——任何 task.* 残留（幽灵 task.status 等）都不改变其
    会话身份。旧口径（"任意非空 task.* 自身键"）会把带残留的会话误判为
    任务（装机 2026-11-06 事故：3 条会话残留进任务面板），升级后仅按
    身份锚命中。"""
    if str(row.get("task.id") or "") != "":
        return True
    return any(
        str(k).startswith("lineage.") and row[k] is not None and row[k] != ""
        for k in row
    )


# ── GAP-1 统一：能力注入点（server.py on_load）──
# chat_sender：chat.send_message（注入/重试驱动）；state_reader：pipeline-state.list
# （任务状态/子链读面）；state_writer：pipeline-state.update（任务域写面——④A 返工
# 重开/重试对齐的终态翻转通道）；pipeline_executor：pipeline-executor（stop→
# suspend_pipeline / resume→resume_pipeline）；traces_reader：service-registry
# traces.list_by_pipeline（执行活动读面，recent_activities/latest 数据源）；
# runs_reader：service-registry pipeline-runs.list_by_pipeline（run 起点/终点，
# elapsed_seconds 数据源——task=pipeline 后无 task.started_at state 键，耗时以首
# run created_at 起）。
_chat_sender: Any = None
_state_reader: Any = None
_state_writer: Any = None
_pipeline_executor: Any = None
_traces_reader: Any = None
_runs_reader: Any = None


def set_chat_sender(sender: Any) -> None:
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender


def set_state_reader(reader: Any) -> None:
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def set_state_writer(writer: Any) -> None:
    global _state_writer  # noqa: PLW0603
    _state_writer = writer


def set_traces_reader(reader: Any) -> None:
    global _traces_reader  # noqa: PLW0603
    _traces_reader = reader


def set_pipeline_executor(executor: Any) -> None:
    global _pipeline_executor  # noqa: PLW0603
    _pipeline_executor = executor


def set_runs_reader(reader: Any) -> None:
    global _runs_reader  # noqa: PLW0603
    _runs_reader = reader


class TaskTool(BuiltinTool):
    """任务管理工具（简化版）。"""

    def __init__(self) -> None:
        """初始化任务管理工具。

        0.2 收尾：pipeline-executor.start_run 占位能力已随旧引擎移除——
        resume/retry 仅改任务状态，任务管道执行由会话对话 / chat.send_message
        → PipelineExecutor 驱动（不再经 capability 提交占位 run）。
        """

        self._task_service: TaskService | None = None

    async def _load_activity_entries(self, pipeline_id: str) -> list[dict]:
        """traces.list 读面 → 执行活动条目（时间正序）。

        执行活动唯一数据源是内核 traces 表（0.1 ExecutionRecordStorage 已随
        infrastructure 层退役）。条目形态：{iteration, action, action_type,
        summary, at}。
        - core 步骤：_executed_tool_calls × tool_results 按序配对 → tool 条目
          （summary 取 tool_result 的 metadata.message，缺则参数摘要）；无工具
          调用的轮次 → 一条 thinking 条目（用 llm_usage 概括）。
        - post 步骤：router.last_response_text → ai 条目（LLM 回复摘要）。
        reader 未注入或无管道 id → []；查询失败 → warning 降级空列表（活动是
        详情的附属字段，不阻断任务主数据返回）。
        """

        if not pipeline_id or _traces_reader is None:
            return []

        try:
            traces = _traces_reader(pipeline_id)

            if asyncio.iscoroutine(traces):
                traces = await traces
        except Exception as exc:
            logger.warning(
                "[TaskTool] 执行活动读取失败（降级为空）| pipeline=%s err=%s",
                pipeline_id,
                exc,
            )
            return []

        if not isinstance(traces, list):
            logger.warning(
                "[TaskTool] traces.list 返回非列表形态 | type=%s", type(traces).__name__
            )
            return []

        entries: list[dict] = []
        iteration = 0

        for trace in traces:
            if not isinstance(trace, dict):
                continue

            plugin_id = str(trace.get("plugin_id") or "")
            created_at = trace.get("created_at") or ""

            try:
                raw = trace.get("patch_data")
                data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                data = {}

            if not isinstance(data, dict):
                continue

            if plugin_id == "core":
                iteration += 1
                calls = data.get("_executed_tool_calls") or []
                results = data.get("tool_results") or []

                for i, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue

                    entries.append(
                        {
                            "iteration": iteration,
                            "action": str(call.get("name") or "tool"),
                            "action_type": "tool",
                            "summary": self._tool_activity_summary(
                                results[i] if i < len(results) else None, call
                            ),
                            "at": created_at,
                        }
                    )

                if not calls:
                    usage = data.get("llm_usage") or {}

                    entries.append(
                        {
                            "iteration": iteration,
                            "action": "thinking",
                            "action_type": "ai",
                            "summary": (
                                f"LLM 轮次（输入 {usage.get('input_tokens', '-')} / "
                                f"输出 {usage.get('output_tokens', '-')} tokens）"
                                if usage
                                else "LLM 轮次"
                            ),
                            "at": created_at,
                        }
                    )

            elif plugin_id == "post":
                text = str(data.get("router.last_response_text") or "")

                if text:
                    entries.append(
                        {
                            "iteration": iteration,
                            "action": "thinking",
                            "action_type": "ai",
                            "summary": text[:100],
                            "at": created_at,
                        }
                    )

        return entries

    @staticmethod
    def _tool_activity_summary(result: Any, call: dict) -> str:
        """单条工具活动的摘要：结果 message 优先，缺则参数摘要。"""

        if isinstance(result, dict):
            meta = result.get("metadata")

            if isinstance(meta, dict) and meta.get("message"):
                return str(meta["message"])[:100]

            data = result.get("data")

            if data:
                return json.dumps(data, ensure_ascii=False, default=str)[:100]

        args = call.get("args") or call.get("arguments")

        if args:
            return json.dumps(args, ensure_ascii=False, default=str)[:100]

        return ""

    @staticmethod
    def _calc_elapsed_seconds(task: TaskModel) -> float | None:
        """计算任务已耗时（秒）——旧 service 回落路径（TaskModel 带 started_at）。"""

        if not task.started_at:
            return None

        from datetime import datetime  # noqa: PLC0415

        started = _as_utc(datetime.fromisoformat(task.started_at))

        if task.completed_at:
            completed = _as_utc(datetime.fromisoformat(task.completed_at))

            return (completed - started).total_seconds()

        return (datetime.now(UTC) - started).total_seconds()

    async def _calc_elapsed_from_runs(self, pipeline_id: str) -> float | None:
        """任务耗时（秒）——state 桥路径：run 记录起点终点直算。

        task=pipeline 后无 task.started_at state 键（用户裁定：不新增字段，
        管道 id 唯一，直接查 runs）：起点 = 最早 run created_at；终点 = 全部
        run 已终态时最晚 ended_at（任一 run 仍在跑则任务在执行，用当前时间）。
        runs reader 未注入 / 查询失败 / 无 run 记录 → None（详情字段缺失
        不阻断任务主数据）。
        """

        if not pipeline_id or _runs_reader is None:
            return None

        try:
            runs = _runs_reader(pipeline_id)

            if asyncio.iscoroutine(runs):
                runs = await runs
        except Exception as exc:
            logger.warning(
                "[TaskTool] run 记录读取失败（耗时降级 None）| pipeline=%s err=%s",
                pipeline_id,
                exc,
            )
            return None

        if not isinstance(runs, list) or not runs:
            return None

        from datetime import datetime  # noqa: PLC0415

        created_ats: list[datetime] = []
        ended_ats: list[datetime] = []
        all_terminal = True

        for run in runs:
            if not isinstance(run, dict):
                continue

            created_raw = str(run.get("created_at") or "")

            if created_raw:
                created_ats.append(_as_utc(datetime.fromisoformat(created_raw)))

            status = str(run.get("status") or "")

            ended_raw = str(run.get("ended_at") or "")

            if ended_raw:
                ended_ats.append(_as_utc(datetime.fromisoformat(ended_raw)))
            elif status not in ("completed", "failed", "suspended", "cancelled"):
                all_terminal = False

        if not created_ats:
            return None

        started = min(created_ats)

        if all_terminal and ended_ats:
            return (max(ended_ats) - started).total_seconds()

        return (datetime.now(UTC) - started).total_seconds()

    @staticmethod
    def _format_elapsed(seconds: float | None) -> str:
        """将秒数格式化为可读字符串。"""

        if seconds is None:
            return "-"

        if seconds < 60:
            return f"{int(seconds)}s"

        minutes = int(seconds / 60)

        if minutes < 60:
            return f"{minutes}m"

        hours = minutes // 60

        remain_minutes = minutes % 60

        return f"{hours}h{remain_minutes}m"

    async def _get_latest_activity(self, task: TaskModel) -> dict | None:
        """获取任务的最新一条执行活动摘要。"""

        entries = await self._load_activity_entries(task.pipeline_run_id or "")

        if not entries:
            return None

        latest = entries[-1]

        return {
            "iteration": latest["iteration"],
            "action": latest["action"],
            "summary": latest["summary"][:100],
            "at": latest["at"],
        }

    async def _get_recent_activities(
        self, task: TaskModel, limit: int = 5
    ) -> list[dict]:
        """获取任务最近 N 条执行活动摘要（最新在前）。"""

        if limit <= 0:
            return []

        entries = await self._load_activity_entries(task.pipeline_run_id or "")

        return list(reversed(entries[-limit:]))

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list）。

        返回 None = 桥未注入（injection point 未接线，调用方按既有回落
        路径处理）；抛 StateRowsReadError = 桥已注入但本次读取失败（桥内
        异常 / 返回非列表）——与「聚合里没有该任务」严格区分。
        """
        reader = _state_reader
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
        except Exception as exc:
            logger.warning("[TaskTool] state 聚合读取失败: %s", exc)
            raise StateRowsReadError(f"state 聚合读取失败：{exc}") from exc
        if not isinstance(rows, list):
            logger.warning(
                "[TaskTool] state 聚合返回非列表形态 | type=%s", type(rows).__name__
            )
            raise StateRowsReadError("state 聚合返回非列表形态")
        return [r for r in rows if isinstance(r, dict)]

    async def _get_task_from_state(self, task_id: str) -> TaskModel | None:
        """从 state 聚合行组装任务对象（GAP-1 统一：task = pipeline）。

        必须组装完整 TaskModel（缺省字段吃 dataclass 默认值），不得用只带
        部分属性的 SimpleNamespace——消费面（get 详情 _calc_elapsed_seconds
        的 started_at / 列表 priority 列 / L2 过滤 parent_pipeline_id）按
        TaskModel 全形状访问，残缺对象会让 state 桥接下所有 get 必崩。

        行解析身份门（ADR 2026-11-06 D5 N8）：无 task.id 且无 lineage.* 的
        行（会话主管道 + 幽灵残留）不解析为任务对象 → None（调用方按结构化
        not found 处理）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None or not _is_task_pipeline_row(row):
            return None
        return self._state_row_to_task(row, log_degraded_anchor=True)

    async def _list_tasks_from_state(self) -> list[TaskModel] | None:
        """从 state 聚合批量组装任务对象（GAP-1 单一真值的列表读面）。

        只含有 task.* 字段的行是任务管道（无该字段段的管道不是任务，跳过）；
        按 task.ended_at/创建序倒序近似——聚合行无稳定时间戳键时保序。
        None = 桥未就绪/无任务行（调用方回落旧 service）。
        同 _get_task_from_state：组装完整 TaskModel，禁 SimpleNamespace
        （列表消费面访问 priority/parent_task_id 等全形状字段）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None
        out: list[TaskModel] = []
        for row in rows:
            # 任务行判定与内核收紧口径一致（kernel server.rs has_task_marker）：
            # 含 task.* 但不含 task.owned.* 的行才是任务管道；task.owned.*
            # （task.owned.*）的聊天管道不算任务行，不返给 LLM。
            if not _is_task_pipeline_row(row):
                continue
            pid = str(row.get("pipeline_id") or "")
            if not pid:
                continue
            out.append(self._state_row_to_task(row))
        return out

    def _state_row_to_task(
        self, row: dict[str, Any], *, log_degraded_anchor: bool = False
    ) -> TaskModel:
        """聚合行 → TaskModel（两个 state 读面共用的字段装配）。

        会话锚点取舍：任务管道无 sessions 行，其 thread_id 恒等于自身
        pipeline_id；出生侧 lineage.origin_session_id 修正后为真 thread id。
        取「不等于自身 pipeline_id」的那个，两侧语义偏差互为兜底。
        log_degraded_anchor=True 时（单任务 get 面）对锚点降级打 debug 留痕。
        """
        pid = str(row.get("pipeline_id") or "")
        anchor, degraded = _pick_session_anchor(
            pid,
            str(row.get("lineage.origin_session_id") or ""),
            str(row.get("thread_id") or ""),
        )
        if degraded and log_degraded_anchor:
            # 三段式兜底命中 pid 充当 session_id：语义降级，debug 留痕
            logger.debug("[TaskTool] 会话锚点兜底命中 pipeline_id 充当 session_id | task=%s", row.get("pipeline_id") or "")
        metadata = {
            "session_id": anchor,
            "target_id": str(row.get("task.submitted_by") or ""),
        }
        # 项目挂靠键（ADR 2026-08-27）：state 行 task.parent_project_id 是唯一
        # 真值（task_submit 出生写全），装配时映射到 metadata.project_id 供
        # 列表项目过滤消费（遗留 YAML 行自带同键，两来源同形）。
        project_key = str(row.get("task.parent_project_id") or "")
        if project_key:
            metadata["project_id"] = project_key
        _append_workspace_meta(metadata, row)
        parent_pipe = str(row.get("lineage.parent_pipeline_id") or "") or None
        raw_error = row.get("raw_error")
        return TaskModel(
            id=pid,
            title=str(row.get("task.goal") or pid),
            status=_resolve_status_value(str(row.get("task.status") or "pending"), pid),
            metadata=metadata,
            pipeline_run_id=pid,
            parent_pipeline_id=parent_pipe,
            parent_task_id=parent_pipe,
            completed_at=str(row.get("task.ended_at") or "") or None,
            agent_name="",
            error=str(raw_error) if raw_error else None,
        )


    def _get_task_service(self) -> TaskService:
        """获取共享的 TaskService 实例。

        0.2 走 tasks 插件包的 service_access.get_task_service()（M3 自包含实例化，
        进程内单例；server.py 已把 plugins/shared/tasks 注入 sys.path）。
        初始化失败返回 None → 转结构化 SERVICE_UNAVAILABLE（execute 的
        except RuntimeError 捕获）。
        """

        if self._task_service is not None:
            return self._task_service

        from service_access import get_task_service  # noqa: PLC0415

        service = get_task_service()

        if service is not None:
            self._task_service = service

            return self._task_service

        raise RuntimeError("任务服务初始化失败")

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""

        return Tool(
            name="task_manage",
            description=(
                "任务管理工具：用于查询和控制任务的生命周期。\n\n"
                "## 6 个操作\n"
                "- **get**：查询任务。不传 task_id → 返回列表简表；传 task_id → 返回单个任务详情\n"
                "- **continue**：继续执行。可重试失败任务、恢复已停止任务、向运行中任务注入指令、返工重开已完成任务。失败恢复选型：环境类失败（timeout/model_unavailable/llm_fail_loop）优先 continue 原地续跑；方向性失败（方案/评估问题）才重派（inherit_from）；同因 continue 3 次仍败 → 升级重派\n"
                "- **stop**：停止任务。统一进入 stopped 状态（数据完好，可 continue 恢复）\n"
                "- **delete**：彻底删除任务\n"
                "- **complete**：标记容器任务完成（仅L1）\n"
                "- **fail**：标记容器任务失败（仅L1）\n\n"
                "## continue 的五种行为\n"
                "- 运行中任务 + message：注入指令（不改变状态）\n"
                "- 失败/超时任务：重试（自动继承管道+空间）\n"
                "- 失败/超时任务 + message：重试 + 注入指令\n"
                "- 已停止任务：恢复执行\n"
                "- 已完成任务 + message：返工重开（翻回进行中重新评估；无 message 拒绝）\n\n"
                "## 权限\n"
                "- L1：默认只显示自己提交的任务；传 show_all=true 可递归查看当前会话所有任务\n"
                "- L2：只能管理自己提交的子任务"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "continue", "stop", "delete", "change"],
                        "default": "get",
                        "description": (
                            "操作类型（缺省 get）：\n"
                            "- get：查询任务。不传 task_id 返回列表简表，传 task_id 返回详情\n"
                            "- continue：继续执行（重试/恢复/注入指令/已完成任务带 message 返工重开，针对非容器任务）\n"
                            "- stop：停止任务（统一进入 stopped 状态，针对非容器任务）\n"
                            "- delete：删除任务\n"
                            "- change：变更任务运行状态（仅L1）。"
                            "通过 status 参数指定目标（suspended=挂起/running=恢复）。"
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "目标任务 ID",
                    },
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "批量任务 ID 列表（与 task_id 二选一）。适用于 continue/stop/delete 操作",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "running",
                            "stopped",
                            "completed",
                            "failed",
                            "timeout",
                        ],
                        "description": (
                            "双重用途：\n"
                            "- get 列表模式：按状态筛选\n"
                            "- change 操作：目标状态（必填），如 completed/failed/pending/running/stopped/timeout"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "操作原因说明（stop/delete 时推荐填写）",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "注入的指令内容（continue 操作时可选）。\n"
                            "该消息会以 user 角色注入到子任务的下一轮对话中。\n"
                            "【内容粒度规则】\n"
                            "1. 常规检查/提醒：只给方向性提示，不给具体执行步骤\n"
                            "2. 纠正性注入（下级理解偏了、方向错了）：给出具体的纠正意见\n"
                            "3. 错误修正（提交参数有误、路径错误）：给出具体修正内容\n"
                            "4. 用户指令传递（用户有新要求或变更）：给出用户的具体要求\n"
                            "禁止任何情况下给出工作流程级别的建议，下级 Agent 比你更清楚怎么执行。"
                        ),
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": (
                            "验收指标整组覆盖（仅 continue 时可选；2026-10-23 用户裁定）。\n"
                            "传入即整体替换 task.acceptance_criteria，同时清零评估进度"
                            "（task.evaluation/重试计数，防旧判定串联新指标），覆盖留痕"
                            " task.acceptance_criteria_overridden（时间+调用方层级）。\n"
                            "缺省 = 不改动。权限沿用 continue 既有管理面"
                            "（管理面=自己的下级；主管道 L1 管所有）。"
                        ),
                    },
                    "container_reason": {
                        "type": "string",
                        "description": "变更原因（change 操作时填写，记录到任务 metadata）",
                    },
                    "include_details": {
                        "type": "boolean",
                        "description": "是否包含详细信息（get 详情模式生效）。设为 true 时返回 recent_activities 和 elapsed_seconds",
                        "default": False,
                    },
                    "include_agent_calls": {
                        "type": "boolean",
                        "description": "是否只返回工具调用类型的活动记录（get 详情模式生效，自动启用详细信息）",
                        "default": False,
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "父任务 ID（get 列表模式时传入可筛选其下子任务）",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，用于筛选特定项目的任务",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制，默认为50，最大100",
                        "default": 50,
                        "maximum": 100,
                    },
                    "show_all": {
                        "type": "boolean",
                        "description": "是否显示当前会话的所有任务（含子任务的子任务）。默认 false，L1 只显示自己提交的任务。仅 L1 生效。",
                        "default": False,
                    },
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            tags=["task", "management", "L1", "L2", "status", "control"],
            injected_params=["session_id", "user_id", "_session", "pipeline_id"],
            param_level_restrictions={
                "action": {
                    "enum_restrictions": {
                        "get": 0,
                        "continue": 0,
                        "stop": 0,
                        "delete": 0,
                        "change": 1,
                    },
                },
            },
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """执行任务管理操作。"""

        action = inputs.get("action") or "get"

        parent_agent_level = inputs.get("parent_agent_level")

        if parent_agent_level is None:
            logger.error("[TaskTool] 注入参数缺失 | parent_agent_level 未注入")

            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        try:
            self._get_task_service()

        except RuntimeError as e:
            return create_failure_result(
                error=str(e),
                error_code="SERVICE_UNAVAILABLE",
            )

        task_ids = inputs.get("task_ids")

        # ── 短 id 入参解析（LLM 工具面 id 短化契约）──
        # LLM 回传的 task_id / task_ids / parent_task_id 可能是短 id（12 位前缀），
        # 统一经 state 聚合前缀唯一解析回全 id（精确命中原样；多命中歧义报错；
        # 无命中且桥健康时由读取层判定存在性）。解析在 action 分派前收口。
        #
        # state 聚合读面故障的统一翻译点：任何 action 在读取层失败都回
        # SERVICE_UNAVAILABLE 显式错误信封，绝不落入"任务不存在"分支。
        try:
            resolve_err = await self._resolve_input_ids(inputs)
            if resolve_err:
                return create_failure_result(
                    error=resolve_err,
                    error_code="AMBIGUOUS_TASK_ID",
                )

            if task_ids and isinstance(task_ids, list) and action in ("continue", "stop", "delete"):
                return await self._batch_tasks(inputs, parent_agent_level)

            if action == "get":
                return await self._get_task(inputs, parent_agent_level)

            if action == "continue":
                return await self._continue_task(inputs, parent_agent_level)

            if action == "stop":
                return await self._stop_task(inputs, parent_agent_level)

            if action == "delete":
                return await self._delete_task(inputs, parent_agent_level)

            if action == "change":
                return await self._change_status(inputs, parent_agent_level)
        except StateRowsReadError as exc:
            return create_failure_result(
                error=(
                    f"任务状态聚合暂不可读（{exc}）。这是读面故障而非任务缺失，"
                    "请稍后重试；不要据此重建任务。"
                ),
                error_code="SERVICE_UNAVAILABLE",
            )

        return create_failure_result(
            error=f"不支持的操作: {action}",
            error_code="INVALID_ACTION",
        )

    async def _resolve_input_ids(self, inputs: dict[str, Any]) -> str | None:
        """LLM 入参任务 id 前缀唯一解析（短 id → 全 id，就地改写 inputs）。

        Returns:
            None = 解析完成；str = 歧义错误信息（多命中短前缀）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            # 桥未注入（injection point 未接线）：无聚合数据可解析，原样放行
            # 走调用方既有的回落路径。读取故障走 StateRowsReadError，不在此列。
            return None

        from id_utils import resolve_id  # noqa: PLC0415

        tid = inputs.get("task_id")
        if isinstance(tid, str) and tid:
            resolved = await resolve_id(rows, tid)
            if resolved.startswith("AMBIGUOUS:"):
                return f"任务 ID '{tid}' 匹配到多个任务，请使用完整 ID 重试"
            inputs["task_id"] = resolved

        pids = inputs.get("task_ids")
        if isinstance(pids, list):
            resolved_ids: list[str] = []
            for p in pids:
                if isinstance(p, str):
                    r = await resolve_id(rows, p)
                    if r.startswith("AMBIGUOUS:"):
                        return f"任务 ID '{p}' 匹配到多个任务，请使用完整 ID 重试"
                    resolved_ids.append(r)
                else:
                    resolved_ids.append(p)
            inputs["task_ids"] = resolved_ids

        ptid = inputs.get("parent_task_id")
        if isinstance(ptid, str) and ptid:
            resolved = await resolve_id(rows, ptid)
            if resolved.startswith("AMBIGUOUS:"):
                return f"任务 ID '{ptid}' 匹配到多个任务，请使用完整 ID 重试"
            inputs["parent_task_id"] = resolved

        return None

    @staticmethod
    def _short(full_id: str) -> str:
        """全 id → 短 id（LLM 展示用，12 位）。"""
        from id_utils import short_id  # noqa: PLC0415

        return short_id(full_id)

    @staticmethod
    def _check_permission(  # noqa: PLR0911
        task: TaskModel,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """检查任务操作权限。"""

        if parent_agent_level == 1:
            session_id = inputs.get("session_id")

            if session_id and task.metadata.get("session_id") != session_id:
                return False, (
                    f"任务不属于当前会话：task.session_id={task.metadata.get('session_id')}，"
                    f"当前 session_id={session_id}"
                )

            return True, None

        if parent_agent_level == 2:
            submitted_by = (task.metadata or {}).get("submitted_by_level")

            if submitted_by is not None:
                if submitted_by != parent_agent_level:
                    return False, (
                        f"权限不足：本任务由 L{submitted_by} Agent 提交，当前 L{parent_agent_level} Agent 无法管理"
                    )

                return True, None

            pipeline_id = inputs.get("pipeline_id")

            if pipeline_id:
                if pipeline_id not in (task.parent_pipeline_id, task.pipeline_run_id):
                    return False, (
                        f"任务不属于当前管道：task.parent_pipeline_id={task.parent_pipeline_id}，"
                        f"当前 pipeline_id={pipeline_id}"
                    )

                return True, None

            parent_task_id = inputs.get("parent_task_id")

            if parent_task_id:
                if task.parent_task_id == parent_task_id:
                    return True, None

                return False, (
                    f"L2 只能管理自己提交的子任务：task.parent_task_id={task.parent_task_id}，"
                    f"当前 parent_task_id={parent_task_id}"
                )

            return False, "L2 缺少 parent_task_id 参数，无法验证权限"

        return False, f"只有 L1 和 L2 Agent 能使用 task_manage 工具，当前层级：L{parent_agent_level}"

    async def _get_all_tasks(self, limit: int = 5) -> list[TaskModel]:
        """获取全部任务列表（按创建时间倒序）。"""

        service = self._get_task_service()

        return await service.list_all(limit=limit, reverse=True)

    async def _list_all_tasks_sorted(self) -> list[TaskModel]:
        """拉取全部任务，按创建时间倒序返回（不做截断）。

        GAP-1 单一真值：优先 state 聚合（task = pipeline），桥未就绪回落
        旧 service 存储。
        """
        from_state = await self._list_tasks_from_state()
        if from_state is not None:
            return from_state

        service = self._get_task_service()

        return await service.list_all(limit=10_000, reverse=True)

    async def _task_to_dict(
        self,
        task: TaskModel,
        include_details: bool = False,
        include_agent_calls: bool = False,
        activity_limit: int = 5,
    ) -> dict[str, Any]:
        """将 TaskModel 转换为工具返回的字典格式。"""

        result = {
            # 短 id（引擎生成即 12 位短 id；_short 幂等，工具入口前缀解析兼容）
            "task_id": self._short(task.id),
            "title": task.title,
            "status": _status_value(task.status),
            "error": task.error,
        }

        if task.metadata:
            metadata = dict(task.metadata)

            # ── 工作空间信息（直接从任务元数据读取，不二次解析） ──

            result["workspace"] = metadata.get("workspace", "")

            ws_meta = metadata.get("ws_meta")

            if isinstance(ws_meta, dict) and ws_meta.get("path"):
                result["resolved_workspace"] = ws_meta["path"]

            eval_summary = None

            if "evaluation_history" in metadata:
                history = metadata.pop("evaluation_history")

                if history:
                    last = history[-1]

                    eval_summary = {
                        "passed": last.get("passed"),
                        "summary": last.get("summary", ""),
                        "attempt_count": len(history),
                        "evidence": last.get("evidence", []),
                        "suggestions": last.get("suggestions", []),
                        "score": last.get("score"),
                        "metrics": last.get("metrics", []),
                    }

            if eval_summary:
                result["evaluation_summary"] = eval_summary

            fail_reason = metadata.get("fail_reason") or metadata.get("container_reason")

            if fail_reason:
                result["fail_reason"] = fail_reason

            retry_count = metadata.get("retry_count")

            max_retries = metadata.get("max_retries")

            if retry_count is not None:
                result["retry_count"] = retry_count

            if max_retries is not None:
                result["max_retries"] = max_retries

        if include_details or include_agent_calls:
            # 耗时双源：state 桥路径以 run 记录直算（task.started_at 已随
            # task=pipeline 退役，用户裁定不新增字段）；旧 service 回落
            # （TaskModel 带 started_at）保留原计算。run 源 None 时回退旧源。
            elapsed = await self._calc_elapsed_from_runs(task.pipeline_run_id or "")

            if elapsed is None:
                elapsed = self._calc_elapsed_seconds(task)

            result["elapsed_seconds"] = elapsed

            activities = await self._get_recent_activities(task, limit=activity_limit)

            if include_agent_calls:
                activities = [a for a in activities if a.get("action_type") == "tool"]

            result["recent_activities"] = activities

        return result

    # ── get：查询任务（合并旧 get/list/status）──

    async def _get_task(self, inputs: dict[str, Any], parent_agent_level: int) -> ToolExecutionResult:
        """查询任务。"""

        task_id = inputs.get("task_id")

        if task_id:
            return await self._get_task_detail(inputs, parent_agent_level, task_id)

        return await self._get_task_list(inputs, parent_agent_level)

    async def _get_task_detail(
        self, inputs: dict[str, Any], parent_agent_level: int, task_id: str
    ) -> ToolExecutionResult:
        """查询单个任务详情（GAP-1 单一真值：state 聚合优先，回落旧 service）。"""

        try:
            task = await self._get_task_from_state(task_id)
            if task is None:
                service = self._get_task_service()
                task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                return create_failure_result(
                    error=error_msg or "权限不足",
                    error_code="INSUFFICIENT_PERMISSION",
                )

            # 详情模式 limit 语义 = 活动条数上限（列表模式的 limit 是行数上限，
            # 两模式各自取值）；未传用默认 5
            raw_limit = inputs.get("limit")
            activity_limit = int(raw_limit) if raw_limit else 5

            task_dict = await self._task_to_dict(
                task,
                include_details=inputs.get("include_details", False),
                include_agent_calls=inputs.get("include_agent_calls", False),
                activity_limit=activity_limit,
            )

            task_dict["hint"] = "任务正在后台执行中，请勿频繁调用此工具查看状态，任务完成后会自动更新。"

            return create_success_result(
                data=task_dict,
                metadata={"action": "get_task"},
            )

        except Exception as e:
            logger.error("[TaskTool] 获取任务失败: %s", e)

            return create_failure_result(
                error=f"获取任务失败: {str(e)}",
                error_code="GET_FAILED",
            )

    async def _get_task_list(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """获取任务列表简表。

        顺序契约：全量拉取 → 过滤 → 排序（_list_all_tasks_sorted 已排）→
        末端截断。limit 不能先于过滤应用：先截断会拿到「最老的 N 条」，
        当前 session 的任务集中在新创建批次时会被全部过滤掉返回空列表。
        """
        try:
            tasks = await self._list_all_tasks_sorted()
            filtered = self._filter_visible_tasks(tasks, inputs, parent_agent_level)

            # 末端截断：在所有过滤维度都通过之后才应用 limit（顺序契约见 docstring）
            limit = inputs.get("limit", 50)
            if limit and len(filtered) > limit:
                filtered = filtered[:limit]

            rows = []

            for t in filtered:
                rows.append(await self._task_list_row(t))

            return create_success_result(
                data={
                    "d": rows,
                    # task_id 短化在 _task_list_row 内完成：LLM 展示/回传用短 id，
                    # 入口前缀解析恢复全 id
                    "hint": "任务正在后台执行中，请勿频繁调用此工具查看状态，任务完成后会自动更新。",
                },
                metadata={"action": "get_task_list"},
            )

        except Exception as e:
            logger.error("[TaskTool] 列出任务失败: %s", e)

            return create_failure_result(
                error=f"列出任务失败: {str(e)}",
                error_code="LIST_FAILED",
            )

    def _filter_visible_tasks(
        self,
        tasks: list[TaskModel],
        inputs: dict[str, Any],
        parent_agent_level: int,
    ) -> list[TaskModel]:
        """列表可见性过滤：状态/session/层级归属/权限/项目五维，逐任务短路跳过。

        过滤语义保持拆分前顺序（首个命中的 continue 生效即止）：
        状态 → L1(session/非根提交过滤,show_all 放行) → L2(pipeline/显式
        parent 归属) → 用户显式 parent_task_id → P0-3 权限收口 → 项目挂靠
        （metadata.project_id，state 行 task.parent_project_id 装配映射）。
        """
        status_filter = inputs.get("status")
        pipeline_id = inputs.get("pipeline_id")
        requested_parent_task_id = inputs.get("parent_task_id")
        session_id_val = inputs.get("session_id")
        project_id = inputs.get("project_id")

        filtered: list[TaskModel] = []
        for task in tasks:
            if status_filter and _status_value(task.status) != status_filter:
                continue

            if parent_agent_level == 1:
                if session_id_val and task.metadata.get("session_id") != session_id_val:
                    continue

                if not inputs.get("show_all", False):
                    submitted_by = (task.metadata or {}).get("submitted_by_level")

                    if submitted_by is not None and submitted_by != 1:
                        continue

            elif parent_agent_level == 2:
                if pipeline_id and pipeline_id not in (task.parent_pipeline_id, task.pipeline_run_id):
                    continue

                explicit_parent = inputs.get("parent_task_id")
                if explicit_parent and task.parent_task_id != explicit_parent:
                    continue

            if requested_parent_task_id and task.parent_task_id != requested_parent_task_id:
                continue

            # P0-3 纵深防御：用统一的 _check_permission 收口越权过滤，
            # L2 无 parent_task_id 的遗留任务也纳入本过滤。
            has_permission, _ = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                continue

            if project_id:
                # 项目过滤消费装配面映射的挂靠键（state 行 task.parent_project_id
                # / 遗留 YAML 行自带 project_id，同键同形）
                meta_project = task.metadata.get("project_id")

                if meta_project != project_id:
                    continue

            filtered.append(task)
        return filtered

    async def _task_list_row(self, task: TaskModel) -> list[Any]:
        """单任务 → 7 列简表行（短id/标题/状态/优先级/目标/最近动作/耗时）。"""
        activity = await self._get_latest_activity(task)

        # 耗时双源与详情一致：run 记录直算优先，None 回退旧 service 路径
        elapsed = await self._calc_elapsed_from_runs(task.pipeline_run_id or "")

        if elapsed is None:
            elapsed = self._calc_elapsed_seconds(task)

        priority = (
            task.priority.value if hasattr(task.priority, "value") else task.priority
        )
        return [
            self._short(task.id),
            task.title,
            _status_value(task.status),
            priority,
            task.metadata.get("target_name", ""),
            activity["action"] if activity else "-",
            self._format_elapsed(elapsed),
        ]


    # ── continue：继续执行（合并旧 retry/inject/resume）──

    async def _continue_task(  # noqa: PLR0911
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """继续执行任务。"""

        try:
            task_id = inputs.get("task_id")

            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()

            task = await self._get_task_from_state(task_id)
            if task is None and service is not None:
                task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                return create_failure_result(
                    error=error_msg or "权限不足",
                    error_code="INSUFFICIENT_PERMISSION",
                )

            # ── 验收指标整组覆盖（2026-10-23 用户裁定）：continue 可选携带
            # acceptance_criteria，传即整体替换 task.acceptance_criteria 并清零
            # 评估进度（task.evaluation/重试计数——防旧判定串联新指标免检），
            # 覆盖留痕 task.acceptance_criteria_overridden（时间+调用方层级）。
            # 权限沿用上方 _check_permission（管理面=自己的下级；主管道管所有），
            # 执行者对自身任务无管理权 → 无"改指标凑通过"自评口子。
            override_note = ""
            override = inputs.get("acceptance_criteria")
            if override not in (None, {}, []):
                target_pipeline_id = task.pipeline_run_id
                if not target_pipeline_id:
                    return create_failure_result(
                        error="任务未绑定执行管道（pipeline_run_id 缺失），无法覆盖验收指标",
                        error_code="MISSING_PIPELINE_ID",
                    )
                if _state_writer is None:
                    return create_failure_result(
                        error="pipeline-state 写面未注入（sidecar 未接线），无法覆盖验收指标",
                        error_code="SERVICE_UNAVAILABLE",
                    )
                try:
                    from datetime import datetime  # noqa: PLC0415 — 对齐模块内局部导入惯例

                    await _state_writer(
                        target_pipeline_id,
                        {
                            "task.acceptance_criteria": override,
                            "task.evaluation": None,
                            "task.eval_total_calls": 0,
                            "task.acceptance_criteria_overridden": (
                                f"{datetime.now(UTC).isoformat()} L{parent_agent_level}"
                            ),
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — 覆盖失败显式报错，不静默降级续跑
                    return create_failure_result(
                        error=f"验收指标覆盖写 state 失败: {exc}",
                        error_code="STATE_WRITE_FAILED",
                    )
                override_note = (
                    "验收指标已整组覆盖并清零评估进度"
                    "（旧指标判定作废）。新验收指标如下，以此为验收唯一依据：\n"
                    + json.dumps(override, ensure_ascii=False, indent=2)
                )

            message = inputs.get("message", "")
            if override_note:
                # 覆盖必须让执行面知情（否则按旧认知干活的轮次会被新指标误判）
                message = f"{message}\n{override_note}" if message else override_note

            # ── 场景 1：运行中任务 → 注入指令 ──

            if task.status == TaskStatus.RUNNING:
                return await self._inject_to_running(task, message, parent_agent_level, inputs)

            # ── 场景 2：已停止任务 → 恢复执行 ──

            if task.status == TaskStatus.STOPPED:
                return await self._resume_from_stopped(task, message, service, parent_agent_level, inputs)

            # ── 场景 3：失败/超时任务 → 重试 ──

            if task.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
                return await self._retry_from_terminal(task, message, service, parent_agent_level, inputs)

            # ── 场景 4：已完成任务 + message → 返工重开（④A）──
            # 用户裁定（2026-10-19）：continue 的设计场景就是「上级发现任务实际
            # 未完成 → 保留管道上下文与工作区返工」；completed+message=返工重开，
            # 无 message 的裸恢复仍拒绝（无指令的返工没有意义，且正是幽灵轮形态）。
            # 状态机放行 completed→running 仅此一条路径；写面翻转经
            # pipeline-state.update（内核无任务状态机，转换校验点即本分派）。

            if task.status == TaskStatus.COMPLETED:
                if not message:
                    return create_failure_result(
                        error="当前状态 completed 不支持无指令的 continue。"
                        "返工重开必须携带 message（纠正/新指令）；恢复执行请对 stopped 任务操作",
                        error_code="INVALID_STATUS",
                    )
                return await self._reopen_completed(task, message, parent_agent_level, inputs)

            return create_failure_result(
                error=f"当前状态 {_status_value(task.status)} 不支持 continue 操作。"
                f"支持的状态：running（注入指令）、stopped（恢复）、failed/timeout（重试）、"
                f"completed（返工重开，必须携带 message）",
                error_code="INVALID_STATUS",
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"continue 失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )

        except Exception as e:
            logger.error("[TaskTool] continue 失败: %s", e)

            return create_failure_result(
                error=f"continue 失败: {str(e)}",
                error_code="CONTINUE_FAILED",
            )

    async def _inject_to_running(
        self,
        task: TaskModel,
        message: str,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult:
        """向运行中的任务注入指令（continue 场景 1）。

        如实回报契约（20261019 事故②）：`injected` 只在 chat.send_message 成功
        返回后置位；capability 缺席/发送异常一律 INJECT_FAILED 显式失败——
        硬编码 true 曾让上级拿谎报汇报成功，纠正消息实际丢失。成功结果注明
        队列语义：注入=受理入队、run 边界消费、停止会退回（防"已注入"被当
        "已消费"）。
        """

        # P0-3 纵深防御：入口显式校验归属，即便被直接调用或新增调用路径也不裸奔。
        has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

        if not has_permission:
            return create_failure_result(
                error=error_msg or "权限不足",
                error_code="INSUFFICIENT_PERMISSION",
            )

        if not message:
            return create_failure_result(
                error="运行中的任务 continue 需要提供 message 参数（注入指令内容）",
                error_code="MISSING_MESSAGE",
            )

        target_pipeline_id = task.pipeline_run_id

        if not target_pipeline_id:
            return create_failure_result(
                error="任务尚未启动或 pipeline_run_id 未绑定，无法注入",
                error_code="MISSING_PIPELINE_ID",
            )

        if _chat_sender is None:
            return create_failure_result(
                error="chat capability 未接线（sidecar 未注入），消息未入队，指令未生效；请稍后重试或上报",
                error_code="INJECT_FAILED",
                metadata={"task_id": task.id, "action": "continue_inject"},
            )

        try:
            # GAP-1 统一：注入走 chat.send_message 注入分支（task_id 即 pipeline_id，
            # background 立即返回——UI/LLM 不阻塞等待 run 完成）。
            await _chat_sender(_chat_dispatch_params(target_pipeline_id, message, inputs))
        except Exception as _wake_err:
            logger.warning("[TaskTool] 消息注入失败: %s", _wake_err)
            return create_failure_result(
                error=f"消息注入失败（消息未入队，指令未生效）: {_wake_err}",
                error_code="INJECT_FAILED",
                metadata={"task_id": task.id, "action": "continue_inject"},
            )

        logger.info(
            "[TaskTool] 消息注入完成 | pipeline_id=%s | method=chat.send_message | preview=%s",
            target_pipeline_id,
            message[:80],
        )
        return create_success_result(
            data={
                "task_id": task.id,
                "injected": True,
                "target_pipeline_id": target_pipeline_id,
                "message_preview": message[:100],
                "trigger": "chat.send_message",
                "semantics": "queued_until_run_boundary",
            },
            metadata={"action": "continue_inject"},
        )

    async def _terminal_snapshot(
        self,
        task_id: str,
        known_status: str,
        extra_keys: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], str]:
        """翻转前读原终态值 + 父锚点（④A 失败回滚的依据 + D4-2 重新挂号锚）。

        行源 = state 聚合（与任务读面同源）；聚合桥未注入/行缺失/读取故障 →
        仅携带已知 status 兜底（status 是回滚的承重键——派发失败后任务不得
        停在 running），父锚点空串（无父管道可挂号，不凭空造父键）。行内缺失
        的注记键记 None：回滚写 null=恢复其原始「无注记」形态；行都读不到的
        键不参与回滚写回（不虚构值）。extra_keys = 同批清零的完成证据键
        （返工重开用），一并进快照 → 失败回滚时原样写回（真回滚，不留半清
        状态）。父锚点 = state 行 lineage.parent_pipeline_id（子任务血缘键，
        由 task_submit / agent_config_load 声明，读面可见）。
        """
        snapshot: dict[str, Any] = {"task.status": known_status}
        try:
            rows = await self._read_state_rows()
        except StateRowsReadError as exc:
            logger.warning(
                "[TaskTool] 终态快照读取失败（回滚仅恢复 status）| task=%s err=%s",
                task_id,
                exc,
            )
            rows = None
        row = None
        if rows:
            row = next(
                (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
                None,
            )
        if row is None:
            return snapshot, ""
        for key in _TERMINAL_NOTE_KEYS:
            snapshot[key] = row.get(key)
        for key in extra_keys:
            snapshot[key] = row.get(key)
        return snapshot, str(row.get("lineage.parent_pipeline_id") or "")

    async def _reopen_completed(
        self,
        task: TaskModel,
        message: str,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult:
        """已完成任务返工重开（continue 场景 4，④A）。

        四步时序（与修复③的契约：写面先行）：
        1. 写面翻转：pipeline-state.update 把 task.status=running 落 DB，清
           ended_at/失败注记，**并同批清零上一轮的完成证据**（ADR 2026-10-28
           D4-1：返工 = 新的一次尝试，上一轮的评估结论不得对新轮生效）——
           终态翻转在动作成功瞬间对用户可见（面板立即显示进行中，语义诚实）；
        2. 父管道重新挂号（ADR D4-2）：返工在语义上就是一次新的子任务提交 →
           父管道写 task.subtasks_pending.<子管道 id>，上级重新进入「等待子
           任务」语义、终态回执面重新武装（补发面才能检出这一轮的漏发）；
        3. 带指令派发：chat.send_message 注入纠正消息，state 携带证据清空
           overlay（完成证据键会从恢复快照复活，不清则闸门信号②当轮收束，
           纠正指令根本进不了执行）；
        4. 失败回滚：派发失败 → 写回第 1 步前读到的原终态，工具显式失败
           （INJECT_FAILED 口径），父管道挂号随撤销（父不得空等一个不会跑
           的子任务）。
        """

        # P0-3 纵深防御：入口显式校验归属，不依赖调用方 _continue_task 的前置检查。
        has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

        if not has_permission:
            return create_failure_result(
                error=error_msg or "权限不足",
                error_code="INSUFFICIENT_PERMISSION",
            )

        target_pipeline_id = task.pipeline_run_id

        if not target_pipeline_id:
            return create_failure_result(
                error="任务未绑定执行管道（pipeline_run_id 缺失），无法返工重开",
                error_code="MISSING_PIPELINE_ID",
            )

        if _state_writer is None:
            return create_failure_result(
                error="pipeline-state 写面未注入（sidecar 未接线），无法返工重开："
                "不翻转 DB 终态就派发，返工轮会被完成闸门按 DB=completed 误收束",
                error_code="SERVICE_UNAVAILABLE",
            )

        old_status = _status_value(task.status)
        snapshot, parent_anchor = await self._terminal_snapshot(
            target_pipeline_id,
            old_status,
            extra_keys=_REOPEN_EVAL_EVIDENCE_KEYS,
        )
        # 父锚点：state 行 lineage.parent_pipeline_id 为准（子任务血缘键，
        # task_submit / agent_config_load 的 export_fields 声明，读面可见）；
        # 取不到 = 根任务（或读面故障）→ 跳过挂号并留痕，不凭空造父管道键。
        parent_pipeline_id = parent_anchor

        # 1. 写面翻转（先于派发——修复③时序契约：返工轮闸门读到的 DB 终态须已是 running）
        flip_fields: dict[str, Any] = {"task.status": TaskStatus.RUNNING.value}
        for key in _TERMINAL_NOTE_KEYS:
            flip_fields[key] = None
        # 完成证据同批清零（D4-1）：返工轮不得顶着上一轮的评估通过结论。
        flip_fields.update(_REOPEN_EVAL_EVIDENCE_FIELDS)
        try:
            await _state_writer(target_pipeline_id, flip_fields)
        except Exception as exc:  # noqa: BLE001
            logger.error("[TaskTool] 返工重开写面翻转失败: %s", exc)
            return create_failure_result(
                error=f"返工重开失败：终态翻转写面失败（任务保持 {old_status}）: {exc}",
                error_code="CONTINUE_FAILED",
                metadata={"task_id": task.id, "action": "continue_reopen"},
            )

        # 2. 父管道重新挂号（D4-2）。挂号先于派发：派发后子任务可能瞬间终态，
        #    终态事件会把父挂号写 null（回执）——晚挂号会留下一个永远等不到
        #    的挂号（父挂起等一个已经跑完的子任务）。
        if parent_pipeline_id:
            try:
                await self._register_reopened_task(parent_pipeline_id, target_pipeline_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("[TaskTool] 返工重开父管道挂号失败: %s", exc)
                # 挂号失败 = 上级既不等它、也收不到它的终态回执 → 整轮重开
                # 不成立（返工重开的前提就是「上级重新进入等待语义」）：回滚
                # 到原终态并显式失败（CONTINUE_FAILED），上级可重试。
                return await self._rollback_after_failed_dispatch(
                    target_pipeline_id,
                    snapshot,
                    old_status,
                    f"父管道挂号写面失败（上级等不到本轮终态）: {exc}",
                    "continue_reopen",
                    error_code="CONTINUE_FAILED",
                )
        else:
            logger.info(
                "[TaskTool] 返工重开无父锚点（根任务）→ 跳过父管道重新挂号 | task=%s",
                task.id,
            )

        # 3. 带指令派发（复用注入分支：chat.send_message 背景 + 证据清空 overlay）
        try:
            await _chat_sender(
                _chat_dispatch_params(
                    target_pipeline_id,
                    message,
                    inputs,
                    state_overlay=dict(_REOPEN_EVIDENCE_CLEAR_OVERLAY),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TaskTool] 返工派发失败（回滚原终态）: %s", exc)
            cause = str(exc)
            if parent_pipeline_id:
                cleanup_err = await self._clear_reopen_registration(
                    parent_pipeline_id, target_pipeline_id
                )
                if cleanup_err:
                    cause = f"{cause}；父管道挂号撤销也失败: {cleanup_err}"
            return await self._rollback_after_failed_dispatch(
                target_pipeline_id, snapshot, old_status, cause, "continue_reopen"
            )

        logger.info(
            "[TaskTool] 返工重开完成（写面翻转+指令派发）| task_id=%s | preview=%s",
            task.id,
            message[:80],
        )
        return create_success_result(
            data={
                "task_id": task.id,
                "reopened": True,
                "old_status": old_status,
                "new_status": TaskStatus.RUNNING.value,
                "injected": True,
                "target_pipeline_id": target_pipeline_id,
                "message_preview": message[:100],
                "trigger": "chat.send_message",
                "semantics": "queued_until_run_boundary",
            },
            metadata={"action": "continue_reopen"},
        )

    async def _register_reopened_task(
        self, parent_pipeline_id: str, child_pipeline_id: str
    ) -> None:
        """返工重开 → 父管道重新挂号（ADR 2026-10-28 D4-2）。

        写面与 task_submit 的提交挂号同款（键名/值形态/时间戳来源对齐）：
        ``task.subtasks_pending.<子管道 id>`` = 提交时间戳 ISO（信号③
        「已提交待回执」；子任务终态事件写 null 即回执——补发面据此检出
        本轮漏发）。owned 镜像已退役（ADR 2026-11-02-state-key-governance）：
        父会话面板 join 子行取真值，无镜像可刷。

        写失败向上抛（调用方按显式失败口径处理，不吞异常）。
        """
        from datetime import datetime  # noqa: PLC0415 — 对齐模块内局部导入惯例

        await _state_writer(
            parent_pipeline_id,
            {
                f"task.subtasks_pending.{child_pipeline_id}": datetime.now(UTC).isoformat(),
            },
        )

    async def _clear_reopen_registration(
        self, parent_pipeline_id: str, child_pipeline_id: str
    ) -> str:
        """撤销返工重开的父管道挂号（派发失败：父不得空等一个不会跑的子任务）。

        挂号键写 null（pipeline-state.update 无键删除语义，null 即已回执）。
        owned 镜像已退役，无镜像回写面。

        Returns:
            "" = 撤销成功；非空 = 失败原因文本（调用方并入失败回执，首因不被
            掩盖——不静默吞掉清理失败）。
        """
        try:
            await _state_writer(
                parent_pipeline_id,
                {
                    f"task.subtasks_pending.{child_pipeline_id}": None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[TaskTool] 返工派发失败后父管道挂号撤销失败（父可能空等）| task=%s err=%s",
                child_pipeline_id,
                exc,
            )
            return str(exc)
        return ""

    async def _rollback_after_failed_dispatch(
        self,
        task_id: str,
        snapshot: dict[str, Any],
        old_status: str,
        cause: str,
        action: str,
        error_code: str = "INJECT_FAILED",
    ) -> ToolExecutionResult:
        """派发失败后的终态回滚（④A 第 3 步，返工重开/重试共用）。

        写回翻转前读到的原终态；回滚写面也失败 → 显式失败并保留现场说明
        （任务可能停在 running，不得吞掉谎报成功）。INJECT_FAILED 口径与
        修复②一致：上级可直接看到派发未生效。error_code 覆盖给「失败发生
        在派发之前」的路径（返工重开的父管道挂号失败 → CONTINUE_FAILED）。
        调用前提：写面翻转已成功（即 _state_writer 已接线），本方法不再重复
        判空。
        """
        try:
            await _state_writer(task_id, dict(snapshot))
        except Exception as rb_exc:  # noqa: BLE001
            logger.error(
                "[TaskTool] 终态回滚写面也失败 | task=%s err=%s", task_id, rb_exc
            )
            return create_failure_result(
                error=(
                    f"操作失败（{cause}），且终态回滚也失败（{rb_exc}）；"
                    f"任务 state 可能停在 running，请人工核对后重试或恢复 {old_status}"
                ),
                error_code=error_code,
                metadata={"task_id": task_id, "action": action},
            )
        return create_failure_result(
            error=f"操作失败，已回滚原终态 {old_status}: {cause}",
            error_code=error_code,
            metadata={"task_id": task_id, "action": action},
        )

    async def _read_authoritative_task_status(self, task_id: str) -> str | None:
        """恢复守卫读面：state 聚合行 task.status（权威投影，越过分派读缓存）。

        None = 行缺席 / 桥未注入 / 读面故障（降级放行——恢复链是主路径，
        投影读失败不得拦死恢复，tasks http_api._read_task_status 同风格；
        分歧场景由 reconcile 启动对账兜底）。
        """
        try:
            rows = await self._read_state_rows()
        except StateRowsReadError as exc:
            logger.warning(
                "[TaskTool] 恢复守卫读面故障（降级放行）| task=%s err=%s", task_id, exc
            )
            return None
        if rows is None:
            return None
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None:
            return None
        return str(row.get("task.status") or "") or None

    async def _resume_from_stopped(
        self,
        task: TaskModel,
        message: str,
        service: TaskService,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult:
        """从 stopped 状态恢复执行（continue 场景 2）。

        resume 入口守卫（ADR 2026-10-30 §4.3）：派发前重读权威投影行
        task.status（越过分派读的缓存窗口），真终态（completed/failed）
        拒绝恢复并留痕——分派读到 stopped 与派发之间投影可能已被外部写面
        （评估闸门/reconcile 补随写）推到真终态，resume 不得推翻。
        """

        # P0-3 纵深防御：入口显式校验归属，不依赖调用方 _continue_task 的前置检查。
        has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

        if not has_permission:
            return create_failure_result(
                error=error_msg or "权限不足",
                error_code="INSUFFICIENT_PERMISSION",
            )

        authoritative_status = await self._read_authoritative_task_status(task.id)
        if authoritative_status in _RESUME_BLOCKED_STATUSES:
            logger.warning(
                "[TaskTool] 拒绝恢复真终态任务（resume 入口守卫）| task=%s | status=%s",
                task.id,
                authoritative_status,
            )
            if authoritative_status == "completed":
                hint = "已完成任务无可恢复对象；如需返工请带 message 继续（返工重开）"
            else:
                hint = "失败任务不可恢复执行；请使用 retry 重试或重新提交"
            return create_failure_result(
                error=f"任务已进入终态 {authoritative_status}，拒绝恢复执行：{hint}",
                error_code="INVALID_STATUS",
                metadata={"task_id": task.id, "action": "continue_resume"},
            )

        old_status = _status_value(task.status)

        if message:
            if not task.metadata:
                task.metadata = {}

            task.metadata["retry_message"] = message

            logger.info(
                "[TaskTool] resume 携带注入信息 | task_id=%s | preview=%s",
                task.id,
                message[:80],
            )

        # GAP-1 统一：恢复 = resume_pipeline（按管道恢复最新 suspended run）
        if _pipeline_executor is not None:
            try:
                await _pipeline_executor(
                    {
                        "method": "resume_pipeline",
                        "params": {"pipeline_id": task.id},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[TaskTool] resume_pipeline 失败: %s", exc)
                # 管道 run 仍挂起，不得谎报 resumed=True/new_status=RUNNING
                return create_failure_result(
                    error=f"resume_pipeline 失败，任务保持 {old_status}: {exc}",
                    error_code="RESUME_PIPELINE_FAILED",
                    metadata={"task_id": task.id, "resumed": False},
                )
        logger.info("[TaskTool] resume 完成（resume_pipeline）: task_id=%s", task.id)

        # 恢复经 resume_pipeline 直接执行（无需 TaskWorker），仅恢复任务状态；
        # 任务管道执行由会话对话 / chat.send_message → PipelineExecutor 驱动。
        logger.info("[TaskTool] resume 完成（仅状态恢复，执行由会话对话驱动）: task_id=%s", task.id)

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "resumed": True,
            "old_status": old_status,
            "new_status": TaskStatus.RUNNING.value,
        }

        if message:
            result_data["message_injected"] = True

        return create_success_result(
            data=result_data,
            metadata={"action": "continue_resume"},
        )

    async def _retry_from_terminal(  # noqa: PLR0912
        self,
        task: TaskModel,
        message: str,
        service: TaskService,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult:
        """从 failed/timeout 状态重试（continue 场景 3）。"""

        # P0-3 纵深防御：入口显式校验归属，拒绝前不泄露重试次数等状态。
        has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

        if not has_permission:
            return create_failure_result(
                error=error_msg or "权限不足",
                error_code="INSUFFICIENT_PERMISSION",
            )

        if not task.metadata:
            task.metadata = {}

        retry_count = task.metadata.get("retry_count", 0)

        max_retries = task.metadata.get("max_retries", 6)

        if retry_count >= max_retries:
            return create_failure_result(
                error=(
                    f"任务已达到最大重试次数 ({retry_count}/{max_retries})，"
                    f"无法继续重试。请考虑其他方案或标记任务失败。"
                ),
                error_code="MAX_RETRIES_EXCEEDED",
            )

        old_status = _status_value(task.status)

        # 将纠正信息存入 metadata

        if message:
            task.metadata["retry_message"] = message

            logger.info(
                "[TaskTool] retry 携带纠正信息 | task_id=%s | preview=%s",
                task.id,
                message[:80],
            )

        # GAP-1 统一：重试 = chat.send_message 注入分支重新驱动一轮
        # （pipeline_id = task_id，background 立即返回；新 run 终态由内核回写）。
        # retry_count 为本次尝试序数（state 无写通道，历史计数不跨轮持久化）。
        retry_count = retry_count + 1

        # ④A 写面对齐（同族分裂收敛）：派发前把 DB 终态翻 running 并清失败
        # 注记（现状 DB 留 failed 属分裂，重试轮顶着上一轮失败原因）。写面
        # 未接线 → 降级按现状派发（降级面无 DB 真值可翻，恢复 legacy 行为）。
        flipped = False
        snapshot: dict[str, Any] = {"task.status": old_status}
        parent_anchor = ""
        if _state_writer is not None:
            # retry 与返工重开同族（ADR-1028 D4，用户裁定 2026-10-28「任务失败
            # 重新 continue 也要清上一轮的评估状态」）：快照同批收完成证据键，
            # 失败回滚才能原样写回。
            snapshot, parent_anchor = await self._terminal_snapshot(
                task.id, old_status, extra_keys=_REOPEN_EVAL_EVIDENCE_KEYS
            )
            flip_fields: dict[str, Any] = {"task.status": TaskStatus.RUNNING.value}
            for key in _TERMINAL_NOTE_KEYS:
                flip_fields[key] = None
            # 上一轮的评估结论/计数不得对新轮生效：计数残留会让重试轮未跑先撞
            # 评估调用上限与逐指标重试上限（task_evaluate 读这两键判上限）。
            flip_fields.update(_REOPEN_EVAL_EVIDENCE_FIELDS)
            try:
                await _state_writer(task.id, flip_fields)
            except Exception as exc:  # noqa: BLE001
                logger.error("[TaskTool] retry 写面翻转失败: %s", exc)
                return create_failure_result(
                    error=f"重试失败：终态翻转写面失败（任务保持 {old_status}）: {exc}",
                    error_code="CONTINUE_FAILED",
                    metadata={"task_id": task.id, "action": "continue_retry"},
                )
            flipped = True

            # 父管道重新挂号（retry = 新的一次尝试，D4-2 同族）：上级重新进入
            # 「等本轮终态」语义；挂号先于派发——派发后子任务可能瞬间终态，
            # 终态事件会写 null 回执，晚挂号会留下永远等不到的挂号。
            if parent_anchor:
                try:
                    await self._register_reopened_task(parent_anchor, task.id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[TaskTool] retry 父管道挂号失败: %s", exc)
                    return await self._rollback_after_failed_dispatch(
                        task.id,
                        snapshot,
                        old_status,
                        f"父管道挂号写面失败（上级等不到本轮终态）: {exc}",
                        "continue_retry",
                        error_code="CONTINUE_FAILED",
                    )
            else:
                logger.info(
                    "[TaskTool] retry 无父锚点（根任务）→ 跳过父管道重新挂号 | task=%s",
                    task.id,
                )

        execution_warning = None
        _retry_msg = f"重新执行任务「{task.title}」。"
        if message:
            _retry_msg += "\n纠正信息：" + message
        if _chat_sender is None:
            if flipped:
                # 已翻转但派发通道缺席：回滚原终态 + 撤销父挂号（任务不得停在
                # 无人驱动的 running，上级也不得空等一个没跑的重试轮）
                cause = "chat capability 未接线（sidecar 未注入），重试未派发"
                if parent_anchor:
                    cleanup_err = await self._clear_reopen_registration(
                        parent_anchor, task.id
                    )
                    if cleanup_err:
                        cause = f"{cause}；父管道挂号撤销也失败: {cleanup_err}"
                return await self._rollback_after_failed_dispatch(
                    task.id, snapshot, old_status, cause, "continue_retry"
                )
            execution_warning = "chat capability 未注入，重试未派发执行"
        else:
            try:
                # 身份由内核反调注入（ADR 2026-10-17-reverse-call-user-identity-
                # injection）：1017 事故根因即手写 user_id="task_manage" 被内核
                # fail-closed 拒绝且 background 假成功——禁止再手写合成用户。
                # 证据清空 overlay（与返工重开同款）：上一轮 task_evaluate 派生的
                # 在飞证据键会从恢复快照复活，不清则闸门信号②当轮收束重试轮。
                await _chat_sender(
                    _chat_dispatch_params(
                        task.id,
                        _retry_msg,
                        inputs,
                        state_overlay=dict(_REOPEN_EVIDENCE_CLEAR_OVERLAY),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                if flipped:
                    logger.warning("[TaskTool] retry 注入失败（回滚原终态）: %s", exc)
                    cause = str(exc)
                    if parent_anchor:
                        cleanup_err = await self._clear_reopen_registration(
                            parent_anchor, task.id
                        )
                        if cleanup_err:
                            cause = f"{cause}；父管道挂号撤销也失败: {cleanup_err}"
                    return await self._rollback_after_failed_dispatch(
                        task.id, snapshot, old_status, cause, "continue_retry"
                    )
                logger.warning("[TaskTool] retry 注入失败: %s", exc)
                execution_warning = f"重试注入失败：{exc}"
        logger.info("[TaskTool] retry 完成（chat.send_message 注入）: task_id=%s", task.id)

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "retried": True,
            "old_status": old_status,
            # 写面已翻转 → running（翻转即真值）；降级面（写面缺席）保持 legacy 报值
            "new_status": TaskStatus.RUNNING.value if flipped else TaskStatus.PENDING.value,
            "retry_count": retry_count + 1,
            "max_retries": max_retries,
        }

        if execution_warning:
            result_data["warning"] = execution_warning

        return create_success_result(
            data=result_data,
            metadata={"action": "continue_retry"},
        )

    # ── stop：停止任务（合并旧 pause/cancel，统一设 STOPPED）──

    async def _stop_task(  # noqa: PLR0911
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """停止任务（GAP-1 统一：stop = 挂起任务管道，suspend_pipeline）。

        task = pipeline：挂起该管道最新 run（run 终态 suspended → 内核回写
        task.status=suspended）；级联子任务 = state 聚合中 lineage 子管道逐个
        挂起。任务状态读 state 聚合（task.status），不再写 YAML。
        """

        try:
            task_id = inputs.get("task_id")

            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            task = await self._get_task_from_state(task_id)

            if task is None:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                return create_failure_result(
                    error=error_msg or "权限不足",
                    error_code="INSUFFICIENT_PERMISSION",
                )

            status = getattr(task, "status", None)
            status_str = str(getattr(status, "value", None) or status or "pending")
            stoppable = {"pending", "running", "suspended"}
            if status_str not in stoppable:
                return create_failure_result(
                    error=f"当前状态 {status_str} 无法停止。可停止的状态：pending/running/suspended",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求停止")
            old_status = status_str

            if _pipeline_executor is None:
                return create_failure_result(
                    error="pipeline-executor capability 未注入（sidecar 未接线），无法停止任务",
                    error_code="CONTINUE_FAILED",
                )

            try:
                await _pipeline_executor(
                    {
                        "method": "suspend_pipeline",
                        "params": {"pipeline_id": task_id},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TaskTool] suspend_pipeline 失败（继续尝试级联）: %s", exc)

            cascaded = 0
            try:
                rows = await self._read_state_rows()
            except StateRowsReadError as exc:
                # 主流程（任务已挂起）之后的尽力而为级联：读取失败不回卷
                # stop 结果，warning 留痕跳过级联枚举——区别于"无子管道"。
                logger.warning(
                    "[TaskTool] 级联子管道枚举读取失败（主流程已完成挂起）: %s", exc
                )
                rows = []
            for row in rows or []:
                if str(row.get("lineage.parent_pipeline_id") or "") == task_id:
                    child_id = str(row.get("pipeline_id") or "")
                    if not child_id:
                        continue
                    try:
                        await _pipeline_executor(
                            {
                                "method": "suspend_pipeline",
                                "params": {"pipeline_id": child_id},
                            }
                        )
                        cascaded += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[TaskTool] 级联挂起子管道失败 | pipeline_id=%s | err=%s",
                            child_id,
                            exc,
                        )

            result_data: dict[str, Any] = {
                "task_id": task_id,
                "stopped": True,
                "old_status": old_status,
                "new_status": "suspended",
                "reason": reason,
            }

            if cascaded > 0:
                result_data["cascaded_subtasks"] = cascaded

            return create_success_result(
                data=result_data,
                metadata={"action": "task_stop"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"stop 失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )

        except Exception as e:
            logger.error("[TaskTool] stop 失败: %s", e)
            return create_failure_result(
                error=f"stop 失败: {str(e)}",
                error_code="STOP_FAILED",
            )

    async def _delete_task(self, inputs: dict[str, Any], parent_agent_level: int) -> ToolExecutionResult:
        """删除任务，根据任务类型执行不同策略。

        0.2 任务 = 管道（state 单一真值）——state 聚合优先；state 查不到时
        回退 0.1 YAML 存储判定存在性（遗留任务兜底）。
        state 任务删除 = 内核 delete_pipeline 清管道全部执行数据 + 清同名
        YAML 镜像（评估写面残留，防删除后镜像"复活"任务）。
        两个分支统一走 _check_permission 收口。
        """

        try:
            task_id = inputs.get("task_id")

            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()

            task = await self._get_task_from_state(task_id)
            from_state = task is not None

            if task is None:
                # 0.1 YAML 遗留任务兜底（非 state 任务，0.2 无此新建路径）
                task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                return create_failure_result(
                    error=error_msg or "权限不足",
                    error_code="INSUFFICIENT_PERMISSION",
                )

            # state 任务删除 = 内核删管道数据（runs/traces/messages/state/checkpoints）
            if from_state:
                if _pipeline_executor is None:
                    return create_failure_result(
                        error="pipeline-executor capability 未注入（sidecar 未接线），无法删除任务",
                        error_code="SERVICE_UNAVAILABLE",
                    )
                # 删除前取父锚点与本管道 owned 键清单（删后行没了无从枚举）
                parent_pipeline_id = ""
                owned_keys: list[str] = []
                try:
                    rows = await self._read_state_rows()
                    row = next(
                        (
                            r
                            for r in (rows or [])
                            if isinstance(r, dict)
                            and str(r.get("pipeline_id") or "") == task_id
                        ),
                        None,
                    )
                    if isinstance(row, dict):
                        parent_pipeline_id = str(
                            row.get("lineage.parent_pipeline_id") or ""
                        )
                        owned_keys = [
                            str(k)
                            for k in row
                            if str(k).startswith(f"task.owned.{task_id}.")
                        ]
                except Exception as exc:  # noqa: BLE001 — 读面故障不拦删除
                    logger.warning(
                        "[TaskTool] 删除前父锚点读取失败（父侧清理跳过，reconcile 兜底）"
                        "| task=%s | err=%s",
                        task_id,
                        exc,
                    )
                try:
                    await _pipeline_executor(
                        {"method": "delete_pipeline", "params": {"pipeline_id": task_id}}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("[TaskTool] delete_pipeline 失败: %s", exc)
                    return create_failure_result(
                        error=f"任务管道删除失败: {exc}",
                        error_code="DELETE_FAILED",
                    )
                # 父管道侧在线清理：owned 登记键 + 提交挂号写 null（谁删除谁
                # 随清——离线兜底 = reconcile 第六刀幽灵清扫，ADR 2026-11-02
                # -state-key-governance）。写失败降级告警（删除本体已落）。
                if parent_pipeline_id and _state_writer is not None:
                    clear_fields: dict[str, Any] = dict.fromkeys(owned_keys, None)
                    clear_fields[f"task.subtasks_pending.{task_id}"] = None
                    try:
                        await _state_writer(parent_pipeline_id, clear_fields)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[TaskTool] 父管道 owned 登记清理失败（reconcile 兜底）"
                            "| parent=%s | task=%s | err=%s",
                            parent_pipeline_id,
                            task_id,
                            exc,
                        )
                # 清同名 YAML 镜像（评估写面残留文件），防删除后镜像"复活"任务
                try:
                    await service.hard_delete(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[TaskTool] 清理 YAML 镜像失败（非致命）: %s", exc)
                return create_success_result(
                    data={"task_id": task_id, "deleted": True},
                    metadata={"action": "delete_task"},
                )

            reason = inputs.get("reason", "用户请求删除")

            result_data = await service.hard_delete_task(task_id, reason=reason)

            return create_success_result(
                data=result_data,
                metadata={"action": "delete_task"},
            )

        except Exception as e:
            logger.error("[TaskTool] 删除任务失败: %s", e)

            return create_failure_result(
                error=f"删除任务失败: {str(e)}",
                error_code="DELETE_FAILED",
            )

    # ── change：变更容器任务状态（L1）──

    async def _change_status(  # noqa: PLR0911
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """变更任务状态（GAP-1 统一：状态真值在引擎——手动改状态退役）。

        统一后的合法映射（由管道能力表达，不再写 YAML 状态机）：
        - suspended → suspend_pipeline（挂起 = 停止）
        - running → resume_pipeline（恢复）
        - completed/failed/pending/timeout → 拒绝（终态由 run 终态决定，
          不可手动改写；timeout 由引擎超时表达）
        """

        if parent_agent_level != 1:
            return create_failure_result(
                error="容器状态变更仅限 L1 主 Agent 执行",
                error_code="PERMISSION_DENIED",
            )

        task_id = inputs.get("task_id")

        if not task_id:
            return create_failure_result(
                error="change 操作必须提供 task_id",
                error_code="MISSING_TASK_ID",
            )

        target_status = str(inputs.get("target_status") or "").lower()

        if target_status in ("suspended", "stopped", "paused"):
            if _pipeline_executor is None:
                return create_failure_result(
                    error="pipeline-executor capability 未注入，无法挂起任务",
                    error_code="CONTINUE_FAILED",
                )
            try:
                await _pipeline_executor(
                    {
                        "method": "suspend_pipeline",
                        "params": {"pipeline_id": task_id},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                return create_failure_result(
                    error=f"挂起任务失败: {exc}",
                    error_code="INVALID_TRANSITION",
                )
            return create_success_result(
                data={"task_id": task_id, "changed": True, "new_status": "suspended"},
                metadata={"action": "task_change"},
            )

        if target_status in ("running", "resumed"):
            if _pipeline_executor is None:
                return create_failure_result(
                    error="pipeline-executor capability 未注入，无法恢复任务",
                    error_code="CONTINUE_FAILED",
                )
            try:
                await _pipeline_executor(
                    {
                        "method": "resume_pipeline",
                        "params": {"pipeline_id": task_id},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                return create_failure_result(
                    error=f"恢复任务失败: {exc}",
                    error_code="INVALID_TRANSITION",
                )
            return create_success_result(
                data={"task_id": task_id, "changed": True, "new_status": "running"},
                metadata={"action": "task_change"},
            )

        return create_failure_result(
            error=(
                f"目标状态 {target_status or '(空)'} 不可手动设置：统一后任务状态"
                "由 run 终态/挂起恢复决定（可设置：suspended/running）"
            ),
            error_code="INVALID_STATUS",
        )

    async def _batch_tasks(self, inputs: dict[str, Any], parent_agent_level: int) -> ToolExecutionResult:
        """批量任务操作，每个任务独立返回结果。"""

        action = inputs.get("action")

        task_ids = inputs.get("task_ids", [])

        results = []

        for task_id in task_ids:
            file_inputs = dict(inputs)

            file_inputs["task_id"] = task_id

            file_inputs.pop("task_ids", None)

            # P0-3 纵深防御：批量入口先逐任务预检权限，越权任务短路返回，
            # 不委派子动作（即便未来新增无自身鉴权的批量动作也在此收口）。
            service = self._get_task_service()
            # state 真值优先（task = pipeline），YAML 镜像仅作 0.1 遗留兜底
            pre_task = await self._get_task_from_state(task_id)
            if pre_task is None:
                pre_task = service.get_task(task_id)
            if pre_task is not None:
                pre_ok, pre_err = self._check_permission(pre_task, parent_agent_level, file_inputs)
                if not pre_ok:
                    results.append(
                        {
                            "task_id": task_id,
                            "success": False,
                            "data": None,
                            # 与既有逐任务结果契约一致（error 为字符串），前缀错误码便于识别
                            "error": f"[INSUFFICIENT_PERMISSION] {pre_err}",
                        }
                    )
                    continue

            if action == "continue":
                result = await self._continue_task(file_inputs, parent_agent_level)

            elif action == "stop":
                result = await self._stop_task(file_inputs, parent_agent_level)

            elif action == "delete":
                result = await self._delete_task(file_inputs, parent_agent_level)

            else:
                result = create_failure_result(
                    error=f"不支持的批量操作: {action}",
                    error_code="INVALID_ACTION",
                )

            results.append(
                {
                    "task_id": task_id,
                    "success": result.success,
                    "data": result.output if result.success else None,
                    "error": result.error if not result.success else None,
                }
            )

        success_count = sum(1 for r in results if r["success"])

        failed_count = len(results) - success_count

        return create_success_result(
            data={
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": failed_count,
                },
            },
            metadata={"action": f"batch_{action}"},
        )
