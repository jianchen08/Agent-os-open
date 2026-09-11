"""
触发器设置工具

通过 TriggerManager 注册触发器，支持延迟、定时、周期、事件和条件五种触发类型。
周期触发和定时触发到期后，TriggerManager 的后台检查循环会通过管道的
inject_message 接口唤醒挂起的管道，注入预设消息。

触发动作（trigger_action）两档：
- notify（默认）：注入 message 唤醒管道，安全动作免审批；
- command：以参数列表（cmd 形态，不经 shell 解析）执行宿主命令，写入时经
  human-interaction 审批闸（fail-closed：通道不可用/拒绝/超时一律不落库）。

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TriggerSetupTool：触发器设置工具类
- set_capability_provider()：注入审批闸的 capability provider（server.py 接线）
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from triggers.manager import get_trigger_manager
from triggers.types import (
    TriggerConfig,
    TriggerType,
    parse_duration,
)

# 跨插件共享类型已上提到 SDK 公共依赖层 agentos_plugin_sdk。
# 触发器领域代码（triggers.manager / triggers.types）为本工具自有，位于本工具目录下
# 的 triggers/ 子包（server.py 已将本工具目录注入 sys.path），直接 import。
from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
    get_settings,
)

logger = logging.getLogger(__name__)

# 审批闸能力接入：server.py on_load 注入 capability provider（按名取内核能力
# 句柄，如 security_check 的 _get_human_interaction_cap 范式），工具模块不反向
# 依赖 SDK 插件实例。None = 审批通道未接入 → command 动作 fail-closed 拒绝。
_capability_provider: Callable[[str], Any] | None = None

# command 动作审批选项（id 稳定供代码消费、label 供前端按钮；无"同命令免批"
# ——指纹记忆归 security_check 管道层，本闸不重复造记忆面）。
_COMMAND_APPROVAL_OPTIONS: list[dict[str, str]] = [
    {"id": "approved_once", "label": "批准注册执行"},
    {"id": "denied", "label": "拒绝"},
]
_COMMAND_APPROVAL_LABEL_TO_ID = {opt["label"]: opt["id"] for opt in _COMMAND_APPROVAL_OPTIONS}
_COMMAND_APPROVAL_IDS = {opt["id"] for opt in _COMMAND_APPROVAL_OPTIONS}


def set_capability_provider(provider: Callable[[str], Any] | None) -> None:
    """注入/复位审批闸的 capability provider（server.py on_load/on_unload 接线）。"""
    global _capability_provider  # noqa: PLW0603
    _capability_provider = provider


class TriggerSetupTool(BuiltinTool):
    """
    触发器设置工具

    允许 Agent 设置触发器，在指定条件满足时向当前会话注入消息并唤醒管道。

    支持五种触发类型：
    - delay: 延迟触发（几秒后，最大 24 小时）
    - schedule: 定时触发（指定 ISO 8601 时间，最大 7 天）
    - interval: 周期触发（按固定间隔重复触发，支持停止条件）
    - event: 事件触发（监听 task_completed, file_changed 等）
    - condition: 条件触发（如 task_status == 'pending'）

    使用示例:
        # 延迟触发
        trigger_setup(
            trigger_type="delay",
            delay_seconds=300,
            message="请检查任务状态"
        )

        # 定时触发
        trigger_setup(
            trigger_type="schedule",
            schedule_time="2026-03-15T18:00:00",
            message="下班前检查任务进度"
        )

        # 周期触发（每30分钟，最多10次）
        trigger_setup(
            trigger_type="interval",
            interval="30m",
            message="检查服务状态",
            max_count=10
        )

        # 周期触发（每1小时，最多运行3天）
        trigger_setup(
            trigger_type="interval",
            interval="1h",
            message="执行数据同步",
            max_time="3d"
        )
    """

    MAX_DELAY_SECONDS = 86400
    MAX_SCHEDULE_HOURS = 168
    MAX_TRIGGERS_PER_SESSION = 10
    MAX_INTERVAL_SECONDS = 86400 * 30

    def __init__(self):
        """初始化触发器设置工具"""
        self._manager = get_trigger_manager()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="trigger_setup",
            description=(
                "设置、更新或取消触发器，在指定条件满足时向当前会话注入消息并唤醒管道。"
                "触发器只能触发自己所在的会话。"
                "支持延迟触发、定时触发、周期触发、事件触发和条件触发五种类型。"
                "支持通过 action=update 更新已有触发器的次数，多个任务可共用同一触发器。"
                "支持通过 action=cancel 取消已设置的触发器。"
                "触发动作默认 notify（注入消息，免审批）；command（执行宿主命令）"
                "需要用户审批后才会注册。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["setup", "cancel", "update"],
                        "description": (
                            "操作类型: setup=设置触发器(默认), cancel=取消指定触发器, update=更新已有触发器的次数或时长"
                        ),
                    },
                    "trigger_id": {
                        "type": "string",
                        "pattern": "^trigger_[a-z]+_[0-9a-f]{12}$",
                        "description": "触发器 ID（action=cancel/update 时必填，格式 trigger_<类型>_<12位hex>）",
                    },
                    "trigger_type": {
                        "type": "string",
                        "enum": ["delay", "schedule", "interval", "event", "condition"],
                        "description": (
                            "触发类型: "
                            "delay=延迟触发(几秒后), "
                            "schedule=定时触发(指定时间), "
                            "interval=周期触发(按间隔重复), "
                            "event=事件触发, "
                            "condition=条件触发"
                        ),
                    },
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "description": "触发时注入的消息内容",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "description": "延迟秒数（trigger_type=delay 时必填），最小 1 秒，最大 86400 秒（24小时）",
                    },
                    "schedule_time": {
                        "type": "string",
                        "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
                        "description": "定时触发时间（trigger_type=schedule 时必填），ISO 8601 格式，如: 2026-03-15T15:00:00",
                    },
                    "interval": {
                        "type": "string",
                        "pattern": "^[0-9]+[smhd]",
                        "description": (
                            "周期间隔（trigger_type=interval 时必填），"
                            "支持格式: '30s', '5m', '1h', '1d', '1h30m'。"
                            "最小 10 秒，最大 30 天"
                        ),
                    },
                    "max_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "最大触发次数（0 或不填表示无限），到达后触发器自动停止",
                    },
                    "max_time": {
                        "type": "string",
                        "pattern": "^[0-9]+[smhd]",
                        "description": (
                            "最长运行时间（到达后触发器自动停止），支持格式: '30m', '2h', '3d', '1h30m'。不填表示无限"
                        ),
                    },
                    "event_type": {
                        "type": "string",
                        "description": "监听的事件类型（trigger_type=event 时必填），如: task_completed, file_changed",
                    },
                    "condition": {
                        "type": "string",
                        "description": "条件表达式（trigger_type=condition 时必填），如: task_status == 'pending'",
                    },
                    "trigger_action": {
                        "type": "string",
                        "enum": ["notify", "command"],
                        "description": (
                            "触发动作: notify=注入 message 唤醒管道（默认，免审批），"
                            "command=以参数列表执行宿主命令（需用户审批后才注册）"
                        ),
                    },
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "命令参数列表（trigger_action=command 时必填）：首个元素为"
                            "可执行文件路径，其余为字面参数，不经 shell 解析"
                            "（'echo hi' 这类 shell 字符串无效）"
                        ),
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "command 动作超时毫秒数（默认 10000），超时杀进程树",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "command 动作工作目录（可选）",
                    },
                    "name": {
                        "type": "string",
                        "description": "触发器名称（可选），便于识别",
                    },
                },
                "required": ["trigger_type", "message"],
            },
            injected_params=["execution_id", "pipeline_id"],
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["trigger", "automation", "self-trigger", "interval"],
            when_to_use=[
                "需要延迟执行某项任务时",
                "需要在特定时间点执行任务时",
                "需要按固定间隔重复执行任务时（如每30分钟检查一次）",
                "需要监听某个事件并响应时",
                "需要等待某个条件满足时执行任务时",
            ],
            when_not_to_use=[
                "需要立即执行任务时（直接执行即可）",
                "需要触发其他管道时（触发器只能触发自己所在的管道）",
            ],
            caveats=[
                "触发器只能触发自己所在的管道",
                "延迟时间最大为24小时",
                "定时触发时间不能超过7天",
                "周期间隔最小10秒，最大30天",
                "单管道最多设置10个触发器",
                "设置 max_count 或 max_time 后，到达条件时触发器自动停止",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911,PLR0912
        """执行触发器设置或取消"""
        action = inputs.get("action", "setup")
        pipeline_id = inputs.get("pipeline_id")

        # shell 字符串形态（action=command + action_params.command，如 dsh_adapter
        # translate_hooks_config 产出）已停用；不静默降级为 notify（触发意图会静默丢失）。
        if action not in ("setup", "cancel", "update"):
            return create_failure_result(
                error=(
                    f"不支持的操作类型 action={action}（可选 setup/cancel/update）。"
                    "触发命令执行请用 trigger_action=command + command 参数列表"
                    "（旧 action=command + action_params.command shell 字符串格式已停用）"
                ),
                error_code="INVALID_ACTION",
            )

        if action == "cancel":
            return await self._cancel_trigger(inputs, pipeline_id)

        if action == "update":
            return await self._update_trigger(inputs, pipeline_id)

        trigger_type = inputs.get("trigger_type")
        message = inputs.get("message")
        execution_id = inputs.get("execution_id")
        pipeline_id = inputs.get("pipeline_id")

        if not trigger_type:
            return create_failure_result(
                error="缺少必需参数: trigger_type",
                error_code="MISSING_TRIGGER_TYPE",
            )

        if not message:
            return create_failure_result(
                error="缺少必需参数: message",
                error_code="MISSING_MESSAGE",
            )

        if not pipeline_id:
            return create_failure_result(
                error="缺少注入参数: pipeline_id",
                error_code="MISSING_PIPELINE_ID",
            )

        if not execution_id:
            execution_id = f"exec_{uuid.uuid4().hex[:12]}"

        active_count = sum(
            1
            for t in self._manager._triggers.values()
            if t.pipeline_id == pipeline_id and t.status.value in ("active", "pending")
        )
        if active_count >= self.MAX_TRIGGERS_PER_SESSION:
            return create_failure_result(
                error=f"单会话触发器数量已达上限 ({self.MAX_TRIGGERS_PER_SESSION})",
                error_code="TRIGGER_LIMIT_EXCEEDED",
            )

        # 动作模板解析 + command 审批闸（在类型分派前单点完成：拒绝的
        # command 不落库不执行，任何触发类型都无绕过面）
        action, action_params, action_error = await self._resolve_action_template(inputs, pipeline_id)
        if action_error is not None:
            return action_error

        try:
            if trigger_type == "delay":
                return await self._setup_delay_trigger(inputs, execution_id, pipeline_id, message, action, action_params)
            if trigger_type == "schedule":
                return await self._setup_schedule_trigger(inputs, execution_id, pipeline_id, message, action, action_params)
            if trigger_type == "interval":
                return await self._setup_interval_trigger(inputs, execution_id, pipeline_id, message, action, action_params)
            if trigger_type == "event":
                return await self._setup_event_trigger(inputs, execution_id, pipeline_id, message, action, action_params)
            if trigger_type == "condition":
                return await self._setup_condition_trigger(inputs, execution_id, pipeline_id, message, action, action_params)
            return create_failure_result(
                error=f"不支持的触发类型: {trigger_type}",
                error_code="INVALID_TRIGGER_TYPE",
            )

        except Exception as e:
            logger.error(f"[TriggerSetupTool] 设置触发器失败: {e}", exc_info=True)
            return create_failure_result(
                error=f"设置触发器失败: {str(e)}",
                error_code="TRIGGER_SETUP_FAILED",
            )

    async def _resolve_action_template(
        self,
        inputs: dict[str, Any],
        pipeline_id: str | None,
    ) -> tuple[str, dict[str, Any], ToolExecutionResult | None]:
        """解析触发动作模板（trigger_action: notify|command）。

        notify 免审批直接通过；command 校验 cmd 参数列表形状后接
        human-interaction 审批闸（fail-closed：通道不可用/拒绝/超时一律不落库）。

        Returns:
            (action, action_params, 错误结果)；错误结果非 None 时前两项无意义。
        """
        trigger_action = inputs.get("trigger_action") or "notify"
        if trigger_action == "notify":
            return "notify", {}, None
        if trigger_action != "command":
            return "", {}, create_failure_result(
                error=f"不支持的 trigger_action: {trigger_action}（可选 notify/command）",
                error_code="INVALID_TRIGGER_ACTION",
            )

        cmd = inputs.get("command")
        if (
            not isinstance(cmd, list)
            or not cmd
            or not all(isinstance(c, str) and c.strip() for c in cmd)
        ):
            return "", {}, create_failure_result(
                error="command 动作需要非空字符串参数列表（cmd 形态，不经 shell 解析；"
                "shell 字符串请拆为 argv，或改用 notify 动作）",
                error_code="INVALID_COMMAND",
            )

        approved, reason = await self._gate_command_approval(
            cmd, pipeline_id, user_id=str(inputs.get("user_id") or ""))
        if not approved:
            return "", {}, create_failure_result(
                error=reason,
                error_code="COMMAND_APPROVAL_DENIED",
            )

        action_params: dict[str, Any] = {
            "cmd": cmd,
            "timeout_ms": int(inputs.get("timeout_ms") or 10000),
        }
        cwd = inputs.get("cwd")
        if cwd:
            action_params["cwd"] = str(cwd)
        return "command", action_params, None

    async def _gate_command_approval(
        self, cmd: list[str], pipeline_id: str | None, user_id: str = ""
    ) -> tuple[bool, str]:
        """command 动作审批闸：经 human-interaction 弹审批，等待用户裁决。

        与 security_check 的参数级危险判定同构（参数命中 command 类动作即危险），
        但闸门放在工具写入面——触发器命令到期在宿主侧执行，任务隔离容器拦不住
        它，逐管道的安全检查覆盖不到隔离任务。审批为注册时单次裁决（approve-once），
        到期执行不再重复审批。

        fail-closed：审批通道不可用/请求创建失败/等待超时/取消/拒绝 → 一律拒绝。

        Returns:
            (是否批准, 拒绝原因)；批准时原因为空串。
        """
        provider = _capability_provider
        if provider is None:
            return False, "审批通道未接入（capability provider 未注入），command 动作拒绝注册"
        try:
            hi = provider("human-interaction")
        except (KeyError, AttributeError):
            hi = None
        if hi is None:
            return False, "human-interaction 审批能力不可用，command 动作拒绝注册"

        session_id = pipeline_id or ""
        description = (
            "触发器到期将以参数列表执行以下宿主命令（不经 shell 解析）：\n"
            + json.dumps(cmd, ensure_ascii=False)
        )
        try:
            create_res = await hi.call("create_choice", {
                "session_id": session_id,
                "thread_id": session_id,
                "tab_id": "",
                "title": "触发器命令审批",
                "description": description,
                "options": list(_COMMAND_APPROVAL_OPTIONS),
                "priority": "high",
                "timeout_seconds": 86400,
                # 归属归因（M2）：记录创建者用户，审批面归属校验据此放行本人。
                "user_id": user_id,
            })
            if not isinstance(create_res, dict) or create_res.get("error"):
                raise RuntimeError(f"create_choice failed: {create_res}")
            request_id = str(create_res.get("request_id", ""))
            wait_res = await hi.call(
                "wait_for_choice",
                {"request_id": request_id, "timeout": 86400},
                # 长等待语义：内核按本插件 manifest mcp.request_timeout_secs 等待，
                # 此参数仅 SDK 侧提示（与 security_check 审批链同构）。
                timeout=86500.0,
            )
        except Exception as e:
            return False, f"command 动作审批未完成（{e}），已拒绝注册"

        if not isinstance(wait_res, dict) or wait_res.get("error"):
            err = wait_res.get("error") if isinstance(wait_res, dict) else wait_res
            return False, f"command 动作审批未通过（{err}），已拒绝注册"

        raw_selected = wait_res.get("selected_option", "")
        # 前端传 label、自动路径传 id，两者都归一到 id（security_check 同款）
        resolved = _COMMAND_APPROVAL_LABEL_TO_ID.get(raw_selected) or (
            raw_selected if raw_selected in _COMMAND_APPROVAL_IDS else ""
        )
        if resolved == "approved_once":
            logger.info("[TriggerSetupTool] command 动作获批准 | pipeline=%s cmd=%s", pipeline_id, cmd)
            return True, ""
        return False, f"command 动作审批被拒绝（{raw_selected or '未选择'}），未注册"

    async def _update_trigger(
        self,
        inputs: dict[str, Any],
        pipeline_id: str | None,
    ) -> ToolExecutionResult:
        """更新已有触发器的最大触发次数和/或最长运行时间。

        适用于多个任务共用同一个触发器时，延长触发器生命周期的场景。
        已达 FIRED 状态的触发器会自动重新激活。
        """
        trigger_id = inputs.get("trigger_id")

        if not trigger_id:
            return create_failure_result(
                error="缺少必需参数: trigger_id",
                error_code="MISSING_TRIGGER_ID",
            )

        trigger = self._manager._triggers.get(trigger_id)
        if trigger is None:
            return create_failure_result(
                error=f"触发器不存在: {trigger_id}",
                error_code="TRIGGER_NOT_FOUND",
            )

        if pipeline_id and trigger.pipeline_id != pipeline_id:
            return create_failure_result(
                error="只能更新当前管道的触发器",
                error_code="TRIGGER_PIPELINE_MISMATCH",
            )

        max_count = self._parse_max_count(inputs.get("max_count"))
        max_time_seconds = self._parse_max_time(inputs.get("max_time"))

        if max_count == 0 and max_time_seconds == 0:
            return create_failure_result(
                error="update 操作需要提供 max_count 或 max_time 至少一项",
                error_code="MISSING_UPDATE_PARAMS",
            )

        old_max_fires = trigger.max_fires
        old_max_time = trigger.max_time_seconds

        success = self._manager.update_max_fires(
            trigger_id, max_count, max_time_seconds if max_time_seconds > 0 else None
        )
        if not success:
            return create_failure_result(
                error=f"触发器无法更新（可能已取消）: {trigger_id}",
                error_code="TRIGGER_UPDATE_FAILED",
            )

        logger.info(
            f"[TriggerSetupTool] 触发器已更新 | "
            f"trigger_id={trigger_id} | "
            f"max_fires: {old_max_fires}→{max_count} | "
            f"max_time: {old_max_time}s→{max_time_seconds}s"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "action": "update",
                "old_max_fires": old_max_fires,
                "new_max_fires": max_count,
                "old_max_time_seconds": old_max_time,
                "new_max_time_seconds": max_time_seconds,
                "current_fire_count": trigger.fire_count,
                "message": (
                    f"触发器 {trigger_id} 已更新: "
                    f"最大次数 {old_max_fires}→{max_count}"
                    + (f", 最长运行时间 {old_max_time}s→{max_time_seconds}s" if max_time_seconds > 0 else "")
                ),
            },
        )

    async def _cancel_trigger(
        self,
        inputs: dict[str, Any],
        pipeline_id: str | None,
    ) -> ToolExecutionResult:
        """取消触发器"""
        trigger_id = inputs.get("trigger_id")

        if not trigger_id:
            return create_failure_result(
                error="缺少必需参数: trigger_id",
                error_code="MISSING_TRIGGER_ID",
            )

        trigger = self._manager._triggers.get(trigger_id)
        if trigger is None:
            return create_failure_result(
                error=f"触发器不存在: {trigger_id}",
                error_code="TRIGGER_NOT_FOUND",
            )

        if pipeline_id and trigger.pipeline_id != pipeline_id:
            return create_failure_result(
                error="只能取消当前管道的触发器",
                error_code="TRIGGER_PIPELINE_MISMATCH",
            )

        success = self._manager.cancel(trigger_id)
        if not success:
            return create_failure_result(
                error=f"触发器无法取消（可能已触发或已取消）: {trigger_id}",
                error_code="TRIGGER_CANCEL_FAILED",
            )

        logger.info(f"[TriggerSetupTool] 触发器已取消 | trigger_id={trigger_id} | pipeline_id={pipeline_id}")

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "action": "cancel",
                "message": f"触发器 {trigger_id} 已取消",
            },
        )

    def _parse_max_time(self, max_time_str: str | None) -> float:
        """解析 max_time 参数为秒数。

        Args:
            max_time_str: 时长字符串，如 '30m', '2h', '3d'，None 表示无限

        Returns:
            秒数，0 表示无限
        """
        if not max_time_str:
            return 0.0
        return parse_duration(max_time_str)

    def _parse_max_count(self, max_count: Any) -> int:
        """解析 max_count 参数。

        Args:
            max_count: 最大触发次数，None 或 0 表示无限

        Returns:
            整数，0 表示无限
        """
        if max_count is None:
            return 0
        count = int(max_count)
        if count < 0:
            return 0
        return count

    async def _setup_delay_trigger(
        self,
        inputs: dict[str, Any],
        execution_id: str,
        pipeline_id: str | None,
        message: str,
        action: str,
        action_params: dict[str, Any],
    ) -> ToolExecutionResult:
        """设置延迟触发器"""
        delay_seconds = inputs.get("delay_seconds")

        if delay_seconds is None:
            return create_failure_result(
                error="delay 类型触发器需要提供 delay_seconds 参数",
                error_code="MISSING_DELAY_SECONDS",
            )

        if not isinstance(delay_seconds, int) or delay_seconds < 1:
            return create_failure_result(
                error="delay_seconds 必须是大于 0 的整数",
                error_code="INVALID_DELAY_SECONDS",
            )

        if delay_seconds > self.MAX_DELAY_SECONDS:
            return create_failure_result(
                error=f"延迟时间超过最大限制 ({self.MAX_DELAY_SECONDS} 秒 = 24小时)",
                error_code="DELAY_EXCEEDS_LIMIT",
            )

        trigger_id = f"trigger_delay_{uuid.uuid4().hex[:12]}"

        config = TriggerConfig(
            trigger_id=trigger_id,
            name=inputs.get("name", f"延迟触发器-{delay_seconds}s"),
            trigger_type=TriggerType.DELAY,
            delay_seconds=float(delay_seconds),
            max_fires=1,
            message=message,
            action=action,
            action_params=action_params,
            pipeline_id=pipeline_id,
            metadata={
                "execution_id": execution_id,
                "user_id": inputs.get("user_id", ""),
            },
        )

        self._manager.register(config)

        logger.info(
            f"[TriggerSetupTool] 延迟触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"pipeline_id={pipeline_id} | "
            f"delay_seconds={delay_seconds}"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "trigger_type": "delay",
                "message": f"触发器已设置，将在 {delay_seconds} 秒后触发",
            },
        )

    async def _setup_schedule_trigger(
        self,
        inputs: dict[str, Any],
        execution_id: str,
        pipeline_id: str | None,
        message: str,
        action: str,
        action_params: dict[str, Any],
    ) -> ToolExecutionResult:
        """设置定时触发器"""
        schedule_time_str = inputs.get("schedule_time")

        if not schedule_time_str:
            return create_failure_result(
                error="schedule 类型触发器需要提供 schedule_time 参数",
                error_code="MISSING_SCHEDULE_TIME",
            )

        try:
            schedule_time = datetime.fromisoformat(schedule_time_str.replace("Z", "+00:00"))
        except ValueError:
            return create_failure_result(
                error=f"无效的时间格式: {schedule_time_str}，应为 ISO 8601 格式",
                error_code="INVALID_SCHEDULE_TIME",
            )

        # 时区解释：naive（用户未带时区）视为 APP_TIMEZONE 本地时间，
        # aware（用户带了 +08:00 / Z）直接采用。最终统一归一到 aware UTC 存入 scheduled_at。
        # 否则 manager._normalize_datetime 会把 naive 当作 UTC，导致本地时间被误解、触发延迟一个时区偏移。
        if schedule_time.tzinfo is None:
            tz_name = get_settings().timezone
            try:
                local_tz: tzinfo = ZoneInfo(tz_name)
            except Exception:
                logger.warning("[TriggerSetupTool] APP_TIMEZONE=%r 无效，naive 时间回退到 UTC 解释", tz_name)
                local_tz = UTC
            schedule_time = schedule_time.replace(tzinfo=local_tz)
        schedule_time_utc = schedule_time.astimezone(UTC)

        now = datetime.now(UTC)
        if schedule_time_utc < now:
            return create_failure_result(
                error="定时触发时间不能早于当前时间",
                error_code="SCHEDULE_TIME_IN_PAST",
            )

        max_schedule_time = now + timedelta(hours=self.MAX_SCHEDULE_HOURS)
        if schedule_time_utc > max_schedule_time:
            return create_failure_result(
                error=f"定时触发时间超过最大限制 ({self.MAX_SCHEDULE_HOURS} 小时 = 7天)",
                error_code="SCHEDULE_TIME_EXCEEDS_LIMIT",
            )

        trigger_id = f"trigger_schedule_{uuid.uuid4().hex[:12]}"

        config = TriggerConfig(
            trigger_id=trigger_id,
            name=inputs.get("name", f"定时触发器-{schedule_time_str}"),
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=schedule_time_utc,
            max_fires=1,
            message=message,
            action=action,
            action_params=action_params,
            pipeline_id=pipeline_id or "",
            metadata={
                "execution_id": execution_id,
                "user_id": inputs.get("user_id", ""),
                "schedule_time": schedule_time_str,
            },
        )

        self._manager.register(config)

        logger.info(
            f"[TriggerSetupTool] 定时触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"pipeline_id={pipeline_id} | "
            f"schedule_time={schedule_time_str} (UTC={schedule_time_utc.isoformat()})"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "trigger_type": "schedule",
                "message": f"触发器已设置，将在 {schedule_time_str} 触发",
            },
        )

    async def _setup_interval_trigger(
        self,
        inputs: dict[str, Any],
        execution_id: str,
        pipeline_id: str | None,
        message: str,
        action: str,
        action_params: dict[str, Any],
    ) -> ToolExecutionResult:
        """设置周期触发器"""
        interval_str = inputs.get("interval")

        if not interval_str:
            return create_failure_result(
                error="interval 类型触发器需要提供 interval 参数（如 '30m', '1h', '1d'）",
                error_code="MISSING_INTERVAL",
            )

        try:
            interval_seconds = parse_duration(interval_str)
        except ValueError as e:
            return create_failure_result(
                error=f"无效的间隔格式: {e}",
                error_code="INVALID_INTERVAL",
            )

        if interval_seconds < 10:
            return create_failure_result(
                error="周期间隔最小为 10 秒",
                error_code="INTERVAL_TOO_SHORT",
            )

        if interval_seconds > self.MAX_INTERVAL_SECONDS:
            return create_failure_result(
                error=f"周期间隔超过最大限制 ({self.MAX_INTERVAL_SECONDS} 秒 = 30天)",
                error_code="INTERVAL_EXCEEDS_LIMIT",
            )

        max_count = self._parse_max_count(inputs.get("max_count"))
        max_time_seconds = self._parse_max_time(inputs.get("max_time"))

        if max_count == 0 and max_time_seconds == 0:
            max_count = 1

        trigger_id = f"trigger_interval_{uuid.uuid4().hex[:12]}"

        config = TriggerConfig(
            trigger_id=trigger_id,
            name=inputs.get("name", f"周期触发器-{interval_str}"),
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=interval_seconds,
            max_fires=max_count,
            max_time_seconds=max_time_seconds,
            message=message,
            action=action,
            action_params=action_params,
            pipeline_id=pipeline_id or "",
            metadata={
                "execution_id": execution_id,
                "user_id": inputs.get("user_id", ""),
                "interval_str": interval_str,
            },
        )

        self._manager.register(config)

        desc_parts = [f"每 {interval_str} 触发一次"]
        if max_count > 0:
            desc_parts.append(f"最多 {max_count} 次")
        if max_time_seconds > 0:
            desc_parts.append(f"最长运行 {inputs.get('max_time', '')}")
        desc = "，".join(desc_parts)

        logger.info(
            f"[TriggerSetupTool] 周期触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"pipeline_id={pipeline_id} | "
            f"interval={interval_str}({interval_seconds}s) | "
            f"max_count={max_count} | max_time={max_time_seconds}s"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "trigger_type": "interval",
                "interval": interval_str,
                "interval_seconds": interval_seconds,
                "max_count": max_count,
                "max_time_seconds": max_time_seconds,
                "message": f"周期触发器已设置：{desc}",
            },
        )

    async def _setup_event_trigger(
        self,
        inputs: dict[str, Any],
        execution_id: str,
        pipeline_id: str | None,
        message: str,
        action: str,
        action_params: dict[str, Any],
    ) -> ToolExecutionResult:
        """设置事件触发器"""
        event_type = inputs.get("event_type")

        if not event_type:
            return create_failure_result(
                error="event 类型触发器需要提供 event_type 参数",
                error_code="MISSING_EVENT_TYPE",
            )

        max_count = self._parse_max_count(inputs.get("max_count"))
        if max_count == 0:
            max_count = 1

        trigger_id = f"trigger_event_{uuid.uuid4().hex[:12]}"

        config = TriggerConfig(
            trigger_id=trigger_id,
            name=inputs.get("name", f"事件触发器-{event_type}"),
            trigger_type=TriggerType.EVENT,
            event_name=event_type,
            max_fires=max_count,
            message=message,
            action=action,
            action_params=action_params,
            pipeline_id=pipeline_id or "",
            metadata={
                "execution_id": execution_id,
                "user_id": inputs.get("user_id", ""),
                "event_type": event_type,
            },
        )

        self._manager.register(config)

        logger.info(
            f"[TriggerSetupTool] 事件触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"pipeline_id={pipeline_id} | "
            f"event_type={event_type}"
        )

        data = {
            "success": True,
            "trigger_id": trigger_id,
            "trigger_type": "event",
            "message": f"事件触发器已设置，监听事件: {event_type}",
        }

        # GAP-2 防御：域事件桥未就绪（manifest 未声明 domain_event hook 或
        # server.py 处理器未接线）时 EVENT 触发器收不到任何事件——明确警告，
        # 不静默注册成功。
        if not self._manager.is_event_bridge_ready():
            data["warning"] = (
                "域事件桥未就绪：当前 sidecar 未接通内核域事件通道，"
                "此 EVENT 触发器可能永远不会触发（请检查 plugin.json 的 "
                "domain_event lifecycle hook 声明与 server.py 接线）"
            )
            logger.warning(
                "[TriggerSetupTool] EVENT 触发器注册于域事件桥未就绪状态 | trigger_id=%s | event=%s",
                trigger_id,
                event_type,
            )

        return create_success_result(data=data)

    async def _setup_condition_trigger(
        self,
        inputs: dict[str, Any],
        execution_id: str,
        pipeline_id: str | None,
        message: str,
        action: str,
        action_params: dict[str, Any],
    ) -> ToolExecutionResult:
        """设置条件触发器"""
        condition = inputs.get("condition")

        if not condition:
            return create_failure_result(
                error="condition 类型触发器需要提供 condition 参数",
                error_code="MISSING_CONDITION",
            )

        max_count = self._parse_max_count(inputs.get("max_count"))
        if max_count == 0:
            max_count = 1

        trigger_id = f"trigger_condition_{uuid.uuid4().hex[:12]}"

        config = TriggerConfig(
            trigger_id=trigger_id,
            name=inputs.get("name", f"条件触发器-{condition[:30]}"),
            trigger_type=TriggerType.CONDITION,
            condition_expression=condition,
            max_fires=max_count,
            message=message,
            action=action,
            action_params=action_params,
            pipeline_id=pipeline_id or "",
            metadata={
                "execution_id": execution_id,
                "user_id": inputs.get("user_id", ""),
                "condition": condition,
            },
        )

        self._manager.register(config)

        logger.info(
            f"[TriggerSetupTool] 条件触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"pipeline_id={pipeline_id} | "
            f"condition={condition}"
        )

        data = {
            "success": True,
            "trigger_id": trigger_id,
            "trigger_type": "condition",
            "message": f"条件触发器已设置，条件: {condition}",
        }

        # GAP-2 防御：state 聚合桥未就绪（server.py 未注入 state provider）时
        # CONDITION 触发器没有求值上下文——明确警告，不静默注册成功。
        if not self._manager.is_state_provider_ready():
            data["warning"] = (
                "条件求值桥未就绪：当前 sidecar 未接通管道 state 聚合读取，"
                "此 CONDITION 触发器可能永远不会触发（请检查 server.py 的 "
                "state provider 注入与内核 pipeline-state capability）"
            )
            logger.warning(
                "[TriggerSetupTool] CONDITION 触发器注册于 state 聚合桥未就绪状态 | trigger_id=%s | condition=%s",
                trigger_id,
                condition,
            )

        return create_success_result(data=data)
