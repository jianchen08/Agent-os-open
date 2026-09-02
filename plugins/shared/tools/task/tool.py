"""任务管理工具（简化版）"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from state_machine import InvalidTransitionError
from task_types import TaskModel, TaskStatus

# 跨插件共享类型（ToolExecutionResult / BuiltinTool / Tool 及枚举与结果工厂）已上提到
# SDK 公共依赖层 agentos_plugin_sdk；任务领域类型以 plugins/shared/system/tasks/
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
    from service import TaskService

import state_fields  # noqa: PLC0415 — plugins/shared 平铺模块

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
    """把聚合行的 workspace / ws_meta 原样并入任务元数据（原地追加，缺省跳过）。"""
    if row.get("workspace"):
        metadata["workspace"] = str(row["workspace"])
    # as_dict 兼容跨边界 JSON 字符串形态（state_fields 契约）——只 isinstance(dict)
    # 会让字符串形态静默丢失（前端"打开工作空间"按钮失效）。
    ws_meta = state_fields.as_dict(row.get("ws_meta"), field="ws_meta")
    if ws_meta:
        metadata["ws_meta"] = ws_meta


def _is_task_pipeline_row(row: dict[str, Any]) -> bool:
    """任务行判定，与内核收紧口径一致（kernel server.rs has_task_marker：
    k.startswith("task.") && !k.startswith("task.owned.")）——含 task.* 但不含
    task.owned.* 的行才是任务管道；task.owned.* 的聊天管道不算任务行。"""
    return any(
        str(k).startswith("task.") and not str(k).startswith("task.owned.")
        for k in row
    )


# ── GAP-1 统一：能力注入点（server.py on_load）──
# chat_sender：chat.send_message（注入/重试驱动）；state_reader：pipeline-state.list
# （任务状态/子链读面）；pipeline_executor：pipeline-executor（stop→suspend_pipeline /
# resume→resume_pipeline）；traces_reader：service-registry traces.list_by_pipeline
# （执行活动读面，recent_activities/latest 数据源）；runs_reader：
# service-registry pipeline-runs.list_by_pipeline（run 起点/终点，elapsed_seconds
# 数据源——task=pipeline 后无 task.started_at state 键，耗时以首 run created_at 起）。
_chat_sender: Any = None
_state_reader: Any = None
_pipeline_executor: Any = None
_traces_reader: Any = None
_runs_reader: Any = None


def set_chat_sender(sender: Any) -> None:
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender


def set_state_reader(reader: Any) -> None:
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


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

        started = datetime.fromisoformat(task.started_at)

        if task.completed_at:
            completed = datetime.fromisoformat(task.completed_at)

            return (completed - started).total_seconds()

        return (datetime.now() - started).total_seconds()

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
                created_ats.append(datetime.fromisoformat(created_raw))

            status = str(run.get("status") or "")

            ended_raw = str(run.get("ended_at") or "")

            if ended_raw:
                ended_ats.append(datetime.fromisoformat(ended_raw))
            elif status not in ("completed", "failed", "suspended", "cancelled"):
                all_terminal = False

        if not created_ats:
            return None

        started = min(created_ats)

        if all_terminal and ended_ats:
            return (max(ended_ats) - started).total_seconds()

        return (datetime.now() - started).total_seconds()

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
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None:
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
        进程内单例；server.py 已把 plugins/shared/system/tasks 注入 sys.path）。
        初始化失败返回 None → 转结构化 SERVICE_UNAVAILABLE（execute 的
        except RuntimeError 捕获），不再依赖已废弃的 0.1 infrastructure 层。
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
                "- **continue**：继续执行。可重试失败任务、恢复已停止任务、向运行中任务注入指令\n"
                "- **stop**：停止任务。统一进入 stopped 状态（数据完好，可 continue 恢复）\n"
                "- **delete**：彻底删除任务\n"
                "- **complete**：标记容器任务完成（仅L1）\n"
                "- **fail**：标记容器任务失败（仅L1）\n\n"
                "## continue 的四种行为\n"
                "- 运行中任务 + message：注入指令（不改变状态）\n"
                "- 失败/超时任务：重试（自动继承管道+空间）\n"
                "- 失败/超时任务 + message：重试 + 注入指令\n"
                "- 已停止任务：恢复执行\n\n"
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
                        "description": (
                            "操作类型：\n"
                            "- get：查询任务。不传 task_id 返回列表简表，传 task_id 返回详情\n"
                            "- continue：继续执行（重试/恢复/注入指令，针对非容器任务）\n"
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
                    "session_id": {
                        "type": "string",
                        "description": "会话 ID，用于筛选特定会话的任务",
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
                "required": ["action"],
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

        action = inputs.get("action")

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

        # 检查是否使用批量参数

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
        parent 归属) → 用户显式 parent_task_id → P0-3 权限收口 → project_id 元数据。
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
            # 覆盖既有 ad-hoc 过滤未捕获的边界（如 L2 无 parent_task_id 时的遗留任务）。
            has_permission, _ = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                continue

            if project_id:
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

            message = inputs.get("message", "")

            # ── 场景 1：运行中任务 → 注入指令 ──

            if task.status == TaskStatus.RUNNING:
                return await self._inject_to_running(task, message, parent_agent_level, inputs)

            # ── 场景 2：已停止任务 → 恢复执行 ──

            if task.status == TaskStatus.STOPPED:
                return await self._resume_from_stopped(task, message, service, parent_agent_level, inputs)

            # ── 场景 3：失败/超时任务 → 重试 ──

            if task.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
                return await self._retry_from_terminal(task, message, service, parent_agent_level, inputs)

            return create_failure_result(
                error=f"当前状态 {_status_value(task.status)} 不支持 continue 操作。"
                f"支持的状态：running（注入指令）、stopped（恢复）、failed/timeout（重试）",
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
        """向运行中的任务注入指令（continue 场景 1）。"""

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

        inject_result: dict[str, Any] = {
            "task_id": task.id,
            "injected": True,
            "target_pipeline_id": target_pipeline_id,
            "message_preview": message[:100],
        }

        try:
            # GAP-1 统一：注入走 chat.send_message 注入分支（task_id 即 pipeline_id，
            # background 立即返回——UI/LLM 不阻塞等待 run 完成）。
            if _chat_sender is None:
                inject_result["trigger"] = "failed"
                inject_result["error"] = "chat capability 未注入（sidecar 未接线）"
                return create_success_result(
                    data=inject_result,
                    metadata={"action": "continue_inject"},
                )
            await _chat_sender(
                {
                    "pipeline_id": target_pipeline_id,
                    "message": message,
                    "user_id": "task_manage",
                    "background": True,
                }
            )
            inject_result["trigger"] = "chat.send_message"
            logger.info(
                "[TaskTool] 消息注入完成 | pipeline_id=%s | method=chat.send_message | preview=%s",
                target_pipeline_id,
                message[:80],
            )
        except Exception as _wake_err:
            logger.warning("[TaskTool] 消息注入失败: %s", _wake_err)
            inject_result["trigger"] = "failed"
            inject_result["error"] = str(_wake_err)

        return create_success_result(
            data=inject_result,
            metadata={"action": "continue_inject"},
        )

    async def _resume_from_stopped(
        self,
        task: TaskModel,
        message: str,
        service: TaskService,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult:
        """从 stopped 状态恢复执行（continue 场景 2）。"""

        # P0-3 纵深防御：入口显式校验归属，不依赖调用方 _continue_task 的前置检查。
        has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

        if not has_permission:
            return create_failure_result(
                error=error_msg or "权限不足",
                error_code="INSUFFICIENT_PERMISSION",
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
                logger.warning("[TaskTool] resume_pipeline 失败: %s", exc)
        logger.info("[TaskTool] resume 完成（resume_pipeline）: task_id=%s", task.id)

        # GAP-1 统一：恢复经 resume_pipeline 直接执行（无需 TaskWorker）。
        # 复用 retry 场景的 task_data 构造。_execute_background_task 会从
        # task.pipeline_run_id 取 existing_pipeline_id 复用管道。
        # 0.2 收尾：pipeline-executor.start_run 占位能力已随旧引擎 AdrEngineImpl
        # 移除——resume 仅恢复任务状态，任务管道执行由会话对话 /
        # chat.send_message → PipelineExecutor 驱动。
        logger.info("[TaskTool] resume 完成（仅状态恢复，执行由会话对话驱动）: task_id=%s", task.id)
        execution_warning = None

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "resumed": True,
            "old_status": old_status,
            "new_status": TaskStatus.RUNNING.value,
        }

        if message:
            result_data["message_injected"] = True

        if execution_warning:
            result_data["execution_warning"] = execution_warning

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
        execution_warning = None
        if _chat_sender is not None:
            try:
                _retry_msg = f"重新执行任务「{task.title}」。"
                if message:
                    _retry_msg += "\n纠正信息：" + message
                await _chat_sender(
                    {
                        "pipeline_id": task.id,
                        "message": _retry_msg,
                        "user_id": "task_manage",
                        "background": True,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TaskTool] retry 注入失败: %s", exc)
                execution_warning = f"重试注入失败：{exc}"
        else:
            execution_warning = "chat capability 未注入，重试未派发执行"
        logger.info("[TaskTool] retry 完成（chat.send_message 注入）: task_id=%s", task.id)

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "retried": True,
            "old_status": old_status,
            "new_status": TaskStatus.PENDING.value,
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
