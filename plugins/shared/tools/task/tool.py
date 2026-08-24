"""任务管理工具（简化版）"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)


def _status_value(status: Any) -> str:
    """状态 → 展示值（TaskStatus 取 .value；枚举漂移保留的原串直通）。"""
    return status.value if hasattr(status, "value") else str(status)


# ── GAP-1 统一：能力注入点（server.py on_load）──
# chat_sender：chat.send_message（注入/重试驱动）；state_reader：pipeline-state.list
# （任务状态/子链读面）；pipeline_executor：pipeline-executor（stop→suspend_pipeline /
# resume→resume_pipeline）。
_chat_sender: Any = None
_state_reader: Any = None
_pipeline_executor: Any = None


def set_chat_sender(sender: Any) -> None:
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender


def set_state_reader(reader: Any) -> None:
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def set_pipeline_executor(executor: Any) -> None:
    global _pipeline_executor  # noqa: PLW0603
    _pipeline_executor = executor


class TaskTool(BuiltinTool):
    """任务管理工具（简化版）。"""

    def __init__(self) -> None:
        """初始化任务管理工具。

        0.2 收尾：pipeline-executor.start_run 占位能力已随旧引擎移除——
        resume/retry 仅改任务状态，任务管道执行由会话对话 / chat.send_message
        → PipelineExecutor 驱动（不再经 capability 提交占位 run）。
        """

        self._task_service: TaskService | None = None

    def _get_execution_record_storage(self):
        """获取全局 ExecutionRecordStorage 实例。

        0.2 sidecar 无 execution_record_storage 服务（0.1 infrastructure 层已废弃），
        返回 None 优雅降级——调用方已有 `if not storage: return None` 守卫，
        活动摘要显示 "-"。
        """

    @staticmethod
    def _calc_elapsed_seconds(task: TaskModel) -> float | None:
        """计算任务已耗时（秒）。"""

        if not task.started_at:
            return None

        from datetime import datetime  # noqa: PLC0415

        started = datetime.fromisoformat(task.started_at)

        if task.completed_at:
            completed = datetime.fromisoformat(task.completed_at)

            return (completed - started).total_seconds()

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

    def _get_latest_activity(self, task: TaskModel) -> dict | None:
        """获取任务的最新一条执行活动摘要。"""

        storage = self._get_execution_record_storage()

        if not storage or not task.pipeline_run_id:
            return None

        records = storage.list_by_pipeline(task.pipeline_run_id)[0]

        if not records:
            return None

        latest = records[-1]

        return {
            "iteration": latest.iteration,
            "action": latest.name or latest.type,
            "summary": (latest.content or "")[:100],
            "at": latest.created_at,
        }

    def _get_recent_activities(self, task: TaskModel, limit: int = 5) -> list[dict]:
        """获取任务最近 N 条执行活动摘要。"""

        storage = self._get_execution_record_storage()

        if not storage or not task.pipeline_run_id:
            return []

        records = storage.list_by_pipeline(task.pipeline_run_id)[0]

        recent = records[-limit:] if len(records) > limit else records

        recent.reverse()

        return [
            {
                "iteration": r.iteration,
                "action": r.name or ("thinking" if r.type == "ai" else r.type),
                "action_type": r.type,
                "summary": (r.content or "")[:100],
                "at": r.created_at,
            }
            for r in recent
        ]

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list；None = 桥未就绪）。"""
        reader = _state_reader
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩
            logger.warning("[TaskTool] state 聚合读取失败: %s", exc)
            return None

    async def _get_task_from_state(self, task_id: str) -> TaskModel | None:
        """从 state 聚合行组装任务对象（GAP-1 统一：task = pipeline）。

        必须组装完整 TaskModel（缺省字段吃 dataclass 默认值），不得用只带
        部分属性的 SimpleNamespace——消费面（get 详情 _calc_elapsed_seconds
        的 started_at / 列表 priority 列 / L2 过滤 parent_pipeline_id）按
        TaskModel 全形状访问，残缺对象会让 state 桥接下所有 get 必崩
        （2026-08-23 真机：'types.SimpleNamespace' object has no attribute）。
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
        status_str = str(row.get("task.status") or "pending")
        try:
            status: Any = TaskStatus(status_str)
        except (ValueError, AttributeError):
            # 内核新增状态而本地枚举副本未同步 → 保留原串展示（不再静默变
            # PENDING 触发错误的可操作动作），warning 留痕提示同步枚举
            logger.warning(
                "[TaskTool] 未知任务状态（枚举副本与内核漂移？），保留原串展示 | status=%s | task=%s",
                status_str,
                task_id,
            )
            status = status_str
        # 会话锚点取舍：任务管道无 sessions 行，其 thread_id 恒等于自身
        # pipeline_id；出生侧 lineage.origin_session_id 修正后为真 thread id。
        # 取「不等于自身 pipeline_id」的那个，两侧语义偏差互为兜底。
        pid = str(row.get("pipeline_id") or "")
        origin_sess = str(row.get("lineage.origin_session_id") or "")
        row_thread = str(row.get("thread_id") or "")
        anchor = (
            origin_sess
            if origin_sess and origin_sess != pid
            else (row_thread if row_thread and row_thread != pid else origin_sess or row_thread)
        )
        if not anchor or anchor == pid:
            # 三段式兜底命中 pid 充当 session_id：语义降级，debug 留痕
            logger.debug("[TaskTool] 会话锚点兜底命中 pipeline_id 充当 session_id | task=%s", task_id)
        metadata: dict[str, Any] = {
            "session_id": anchor,
            "target_id": str(row.get("task.submitted_by") or ""),
            "retry_count": 0,
            "max_retries": 6,
        }
        if row.get("task.scope"):
            metadata["task_scope"] = str(row["task.scope"])
        if row.get("workspace"):
            metadata["workspace"] = str(row["workspace"])
        if isinstance(row.get("ws_meta"), dict):
            metadata["ws_meta"] = row["ws_meta"]
        # task = pipeline：父管道即父任务坐标（L2 归属过滤/权限校验消费）
        parent_pipe = str(row.get("lineage.parent_pipeline_id") or "") or None
        raw_error = row.get("raw_error")
        return TaskModel(
            id=task_id,
            title=str(row.get("task.goal") or task_id),
            status=status,
            metadata=metadata,
            pipeline_run_id=pid,
            parent_pipeline_id=parent_pipe,
            parent_task_id=parent_pipe,
            completed_at=str(row.get("task.ended_at") or "") or None,
            agent_name="",
            error=str(raw_error) if raw_error else None,
        )

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
            # 任务行判定与内核收紧口径一致（kernel server.rs has_task_marker：
            # k.startswith("task.") && !k.startswith("task.owned.")）——含
            # task.* 但不含 task.owned.* 的行才是任务管道；只登记过容器子任务
            # （task.owned.*）的聊天管道不算任务行，不返给 LLM。
            if not any(
                str(k).startswith("task.") and not str(k).startswith("task.owned.")
                for k in row
            ):
                continue
            pid = str(row.get("pipeline_id") or "")
            if not pid:
                continue
            status_raw = str(row.get("task.status") or "pending")
            try:
                status: Any = TaskStatus(status_raw)
            except (ValueError, AttributeError):
                # 同 _get_task_from_state：枚举漂移保留原串展示 + warning
                logger.warning(
                    "[TaskTool] 未知任务状态（枚举副本与内核漂移？），保留原串展示 | status=%s | task=%s",
                    status_raw,
                    pid,
                )
                status = status_raw
            origin_sess = str(row.get("lineage.origin_session_id") or "")
            row_thread = str(row.get("thread_id") or "")
            session_anchor = (
                origin_sess
                if origin_sess and origin_sess != pid
                else (row_thread if row_thread and row_thread != pid else origin_sess or row_thread)
            )
            metadata: dict[str, Any] = {
                "session_id": session_anchor,
                "target_id": str(row.get("task.submitted_by") or ""),
            }
            if row.get("task.scope"):
                metadata["task_scope"] = str(row["task.scope"])
            if row.get("workspace"):
                metadata["workspace"] = str(row["workspace"])
            if isinstance(row.get("ws_meta"), dict):
                metadata["ws_meta"] = row["ws_meta"]
            parent_pipe = str(row.get("lineage.parent_pipeline_id") or "") or None
            raw_error = row.get("raw_error")
            out.append(
                TaskModel(
                    id=pid,
                    title=str(row.get("task.goal") or pid),
                    status=status,
                    metadata=metadata,
                    pipeline_run_id=pid,
                    parent_pipeline_id=parent_pipe,
                    parent_task_id=parent_pipe,
                    completed_at=str(row.get("task.ended_at") or "") or None,
                    agent_name="",
                    error=str(raw_error) if raw_error else None,
                )
            )
        return out

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
                            "- change：变更容器任务状态（仅L1，仅容器任务）。"
                            "通过 status 参数指定目标状态，容器只是子任务集合，"
                            "状态可自由变更（completed/failed/pending/running/stopped/timeout）。"
                            "status=completed 时会清理子任务 worktree。"
                        ),
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["all", "container", "non_container"],
                        "description": "任务范围过滤（get 列表模式时生效）",
                        "default": "all",
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
                        "description": "容器操作原因（change 操作时填写，记录到任务 metadata）",
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
                        "description": "父任务 ID（get 列表模式时传入可筛选该容器下的子任务）",
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

        # ── 短 id 入参解析（2026-08-22 用户要求：LLM 工具面 id 短化）──
        # LLM 回传的 task_id / task_ids / parent_task_id 可能是短 id（12 位前缀），
        # 统一经 state 聚合前缀唯一解析回全 id（精确命中原样；多命中歧义报错；
        # 无命中原样让既有"任务不存在"路径处理）。解析在 action 分派前收口。
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
            return None  # 聚合不可用：原样放行，既有"任务不存在"路径处理

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

    def _task_to_dict(
        self, task: TaskModel, include_details: bool = False, include_agent_calls: bool = False
    ) -> dict[str, Any]:
        """将 TaskModel 转换为工具返回的字典格式。"""

        result = {
            # 短 id（2026-08-22 用户要求：LLM 工具面 id 短化；内部权威 id 不动，
            # 回传时工具入口经前缀解析恢复全 id）
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
            result["elapsed_seconds"] = self._calc_elapsed_seconds(task)

            activities = self._get_recent_activities(task)

            if include_agent_calls and not include_details:
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

            task_dict = self._task_to_dict(
                task,
                include_details=inputs.get("include_details", False),
                include_agent_calls=inputs.get("include_agent_calls", False),
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

    async def _get_task_list(  # noqa: PLR0912,PLR0915
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """获取任务列表简表。"""

        try:
            status_filter = inputs.get("status")

            pipeline_id = inputs.get("pipeline_id")

            user_parent_task_id = inputs.get("parent_task_id")

            limit = inputs.get("limit", 50)

            show_all = inputs.get("show_all", False)

            # 列表顺序：先拉全量 → 过滤 → 排序（list_all 已做）→ 末端截断。

            # 不能先按 limit 截断再过滤：那样会拿到「最老的 N 条」而非「最新的 N 条」，

            # 当当前 session 的任务集中在新创建批次时，截断后会被全部过滤掉返回空列表。

            tasks = await self._list_all_tasks_sorted()

            # 过滤

            filtered = []

            for task in tasks:
                if status_filter and _status_value(task.status) != status_filter:
                    continue

                if parent_agent_level == 1:
                    session_id_val = inputs.get("session_id")

                    if session_id_val and task.metadata.get("session_id") != session_id_val:
                        continue

                    if not show_all:
                        submitted_by = (task.metadata or {}).get("submitted_by_level")

                        if submitted_by is not None and submitted_by != 1:
                            continue

                elif parent_agent_level == 2:
                    if pipeline_id:  # noqa: SIM102
                        if pipeline_id not in (task.parent_pipeline_id, task.pipeline_run_id):
                            continue

                    if inputs.get("parent_task_id"):  # noqa: SIM102
                        if task.parent_task_id != inputs["parent_task_id"]:
                            continue

                if user_parent_task_id and task.parent_task_id != user_parent_task_id:
                    continue

                # P0-3 纵深防御：用统一的 _check_permission 收口越权过滤，
                # 覆盖既有 ad-hoc 过滤未捕获的边界（如 L2 无 parent_task_id 时的遗留任务）。
                has_permission, _ = self._check_permission(task, parent_agent_level, inputs)

                if not has_permission:
                    continue

                task_scope = inputs.get("task_scope", "all")

                if task_scope != "all":
                    scope = task.metadata.get("task_scope", "non_container")

                    if scope != task_scope:
                        continue

                project_id = inputs.get("project_id")

                if project_id:
                    meta_project = task.metadata.get("project_id")

                    if meta_project != project_id:
                        continue

                filtered.append(task)

            # 末端截断：在所有过滤维度都通过之后才应用 limit，避免截断窗口

            # 落在被过滤掉的老任务上导致返回空集合。

            if limit and len(filtered) > limit:
                filtered = filtered[:limit]

            # 构建简表（task_id 短化：LLM 展示/回传用短 id，入口前缀解析恢复全 id）

            task_ids = [self._short(t.id) for t in filtered]

            titles = [t.title for t in filtered]

            statuses = [_status_value(t.status) for t in filtered]

            priorities = [t.priority.value if hasattr(t.priority, "value") else t.priority for t in filtered]

            target_names = [t.metadata.get("target_name", "") for t in filtered]

            latest_actions = []

            elapsed_list = []

            for t in filtered:
                activity = self._get_latest_activity(t)

                latest_actions.append(activity["action"] if activity else "-")

                elapsed_list.append(self._format_elapsed(self._calc_elapsed_seconds(t)))

            return create_success_result(
                data={
                    "d": [
                        [
                            task_ids[i],
                            titles[i],
                            statuses[i],
                            priorities[i],
                            target_names[i],
                            latest_actions[i],
                            elapsed_list[i],
                        ]
                        for i in range(len(task_ids))
                    ],
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
            rows = await self._read_state_rows()
            if rows is not None:
                for row in rows:
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

        GAP-1 统一（2026-08-24 修复）：0.2 任务 = 管道（state 单一真值，无
        YAML 记录）——YAML 存储查不到时回退 state 聚合判定存在性，删除 =
        调内核 pipeline-executor.delete_pipeline 清管道全部执行数据。
        """

        try:
            task_id = inputs.get("task_id")

            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()

            task = service.get_task(task_id)

            if not task:
                # 0.2 任务：state 聚合回退（task = pipeline，无 YAML 记录）
                task = await self._get_task_from_state(task_id)
                if task is None:
                    return create_failure_result(
                        error=f"任务不存在: {task_id}",
                        error_code="TASK_NOT_FOUND",
                    )
                # 0.2 任务删除 = 内核删管道数据（runs/traces/messages/state/checkpoints）
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
                return create_success_result(
                    data={"task_id": task_id, "deleted": True},
                    metadata={"action": "delete_task"},
                )

            has_permission, error_msg = self._check_permission(task, parent_agent_level, inputs)

            if not has_permission:
                return create_failure_result(
                    error=error_msg or "权限不足",
                    error_code="INSUFFICIENT_PERMISSION",
                )

            reason = inputs.get("reason", "用户请求删除")

            if task.metadata.get("task_scope") == "container":
                result_data = await service.soft_delete_container(task_id, reason=reason)

                return create_success_result(
                    data=result_data,
                    metadata={"action": "soft_delete_container"},
                )

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
            pre_task = service.get_task(task_id)
            if pre_task is None:
                # GAP-1 统一（2026-08-22）：state 任务（task.* 行 / task.owned 容器）
                # 不在 YAML 存储——预检加 state 兜底，否则批量操作绕过权限收口
                pre_task = await self._get_task_from_state(task_id)
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
