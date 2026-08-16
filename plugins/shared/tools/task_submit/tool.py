"""任务提交工具"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

# 跨插件共享类型（含 safe_enum_value）已上提到 SDK 公共依赖层 agentos_plugin_sdk。
# 任务领域类型以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块，由 server.py
# 将该目录注入 sys.path），在用到处懒加载直接 import。
from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
    safe_enum_value,
)

logger = logging.getLogger(__name__)

# ── 服务提供者解析 ──
#
# 内核能力经 sidecar 注入，服务解析统一走 _get_service_provider：
# - task_worker：无可用实例——pipeline-executor.start_run 为占位能力
#   （占位 run 无 execute_step 驱动、从不真正执行）。任务提交即落库；
#   任务管道执行由会话对话 / chat.send_message → PipelineExecutor 驱动。
# - workspace_lifecycle_manager / agent_registry / execution_record_storage：
#   sidecar 无等价实例 → None（调用方已有降级守卫/磁盘回退，文档化降级）。
# 测试可 monkeypatch 模块级 _get_service_provider 注入 mock。

class _ServiceProviderShim:
    """0.2 服务提供者适配：get(key) 返回 0.2 等价或 None（文档化降级）。"""

    def get(self, key: str) -> Any:
        # workspace_lifecycle_manager / agent_registry / execution_record_storage
        # 0.2 sidecar 无等价实例：调用方已有降级守卫（agent_registry 有磁盘回退）。
        return None


def _get_service_provider() -> Any:
    """获取 0.2 服务提供者 shim（sidecar 模式下的服务解析入口）。"""
    return _ServiceProviderShim()

# ── GAP-1：chat.send_message 派发器（server.py on_load 注入）──
#
# 任务执行驱动：提交成功后经内核 chat capability 的 send_message（create 分支，
# 引擎生成 pipeline_id）创建任务执行管道——state 出生即带 task.* 字段、lineage
# 有父/根二选一、execution_context 透传。sidecar 进程内模块级注入（能力句柄
# 懒解析在协程内完成）；未注入（capability 缺席/测试）时提交仍落库但话术诚实
# （不声称"异步执行中"），结果携带 warning。
_chat_sender: Any = None


def set_chat_sender(sender: Any) -> None:
    """注入 chat.send_message 派发器（server.py on_load 调用）。

    约定签名：``async sender(params: dict) -> dict``，params 即
    chat.send_message 的入参（create/message/user_id/state/lineage/
    execution_context/background）；成功返回含 ``pipeline_id`` 的响应，
    失败抛异常（由调用方记录并降级为 warning 话术）。
    """
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender
    logger.info("[TaskSubmit] chat.send_message 派发器已注入")


def _get_chat_sender() -> Any:
    """获取 chat.send_message 派发器（None = 未注入，测试可 monkeypatch）。"""
    return _chat_sender


# ── 危险目标空间目录列表 ──
# 这些目录是操作系统关键目录，绝不允许作为任务的目标工作空间。
_DANGEROUS_DIRS: set[str] = set()

_DANGEROUS_WINDOWS_DIRS = [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
]

_DANGEROUS_UNIX_DIRS = [
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/tmp",
    "/home",
    "/opt",
]

for _d in _DANGEROUS_WINDOWS_DIRS + _DANGEROUS_UNIX_DIRS:
    _DANGEROUS_DIRS.add(os.path.normpath(_d).lower())


def _get_valid_metric_ids() -> set[str] | None:
    """获取所有合法的评估指标 ID 集合。

    0.2 评估指标的真相来源是 config/evaluation/evaluation_metrics.yaml
    （evaluation 插件同款读取；不再依赖已删除的 0.1 evaluation.loader.MetricLoader）。
    用于在提交期校验 LLM 传入的 acceptance_criteria key 是否为真实存在的指标 ID，
    避免「把 pass_threshold 等 value 子字段误填为指标 ID」导致评估期反复
    METRIC_NOT_FOUND。

    Returns:
        合法指标 ID 集合；加载失败时返回 None，表示跳过校验（fail-open，
        不阻断正常提交）。
    """
    try:
        import yaml  # noqa: PLC0415

        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        yaml_path = project_root / "config" / "evaluation" / "evaluation_metrics.yaml"
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        metrics = data.get("metrics", []) if isinstance(data, dict) else []
        valid = {
            m["name"] for m in metrics if isinstance(m, dict) and m.get("name")
        }
        # 空集合视作加载失败（fail-open，不阻断正常提交）
        return valid or None
    except Exception as exc:
        logger.warning(
            "[TaskSubmit] 评估指标加载失败，跳过 metric_id 校验: %s",
            exc,
        )
        return None


def _validate_metric_ids(
    acceptance_criteria: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """校验 acceptance_criteria 的 key 是否为合法指标 ID。

    - 合法 key 保留；
    - 非法 key 剔除，记入返回的 invalid_ids 列表，由调用方决定如何处理
      （全部无效则拒绝提交，部分无效则降级保留有效项）；
    - 无法获取合法集合（_get_valid_metric_ids 返回 None）时原样返回，
      不剔除任何 key（fail-open）。

    Args:
        acceptance_criteria: 待校验的验收标准字典

    Returns:
        (过滤后的 criteria, 被剔除的无效 key 列表)
    """
    valid_ids = _get_valid_metric_ids()
    if valid_ids is None:
        return acceptance_criteria, []
    if not acceptance_criteria:
        return acceptance_criteria, []

    filtered: dict[str, Any] = {}
    invalid: list[str] = []
    for key, value in acceptance_criteria.items():
        if key in valid_ids:
            filtered[key] = value
        else:
            invalid.append(key)
    return filtered, invalid


def _validate_workspace_path(workspace: str) -> str | None:  # noqa: PLR0911
    """验证目标空间路径的安全性。"""
    if not workspace:
        return None

    # 规范化路径用于比较
    try:
        normalized = os.path.normpath(workspace)
    except (ValueError, TypeError):
        return f"目标空间路径无效: {workspace}"

    Path(normalized)

    # ── 1. 磁盘根目录检查 ──
    if os.name == "nt":
        # Windows: 检查是否为盘符根目录，如 C:\ D:\
        if len(normalized) == 3 and normalized[1] == ":" and normalized[2] == "\\":
            return f"目标空间不能设置为磁盘根目录: {workspace}。请指定具体的项目子目录。"
    # Unix: 检查是否为 /
    elif normalized == "/":
        return f"目标空间不能设置为根目录: {workspace}。请指定具体的项目子目录。"

    # ── 2. 系统危险目录检查 ──
    normalized_lower = normalized.lower()
    if normalized_lower in _DANGEROUS_DIRS:
        return f"目标空间不能设置为系统目录: {workspace}。系统关键目录不允许作为任务的工作空间。"

    # ── 3. 配置文件工作空间根目录检查 ──
    try:
        from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

        ws_root = get_workspace_config_root()
        ws_root_normalized = os.path.normpath(ws_root)
        if normalized_lower == ws_root_normalized.lower():
            return (
                f"目标空间不能设置为当前配置的工作空间根目录: {workspace}。"
                f"该目录是系统管理工作空间的根目录，不允许作为任务目标操作。"
            )
    except Exception as e:
        logger.warning("[TaskSubmit] 读取工作空间配置根目录失败，跳过该检查 | error=%s", e)

    return None


def _normalize_description(value: Any) -> str:
    """将 LLM 返回的 description 归一化为 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value)


class TaskSubmitTool(BuiltinTool):
    """任务提交工具。"""

    def __init__(self) -> None:
        """初始化任务提交工具"""
        self._task_service: Any = None

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。"""
        if self._task_service is not None:
            return self._task_service
        from service_access import get_task_service  # noqa: PLC0415

        service = get_task_service()
        if service is not None:
            self._task_service = service
        return self._task_service

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）"""
        return Tool(
            name="task_submit",
            description="""任务提交工具。将任务提交给指定的 Agent 执行，配置验收标准确保结果可验证。""".strip(),
            input_schema={
                "type": "object",
                "properties": {
                    "target_type": {
                        "type": "string",
                        "enum": ["agent"],
                        "description": "目标类型，固定为 agent。non_container 必填，container 不需要",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "目标 Agent ID。non_container 必填，container 不需要。如果系统提供了 Agent 映射表，直接使用映射表中的 ID，不要用 resource_search 搜索",
                    },
                    "goal_title": {
                        "type": "string",
                        "description": "任务标题（必填），简短明确",
                    },
                    "goal_description": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": (
                            "任务描述（上限 2000 字符）。只写目标和背景，"
                            "禁止写执行步骤/工具选择/流程顺序；引用文件只写路径（如 docs/report.md），"
                            "让下级 Agent 自行读取，不要复制文件内容。"
                        ),
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": (
                            "验收标准字典（可选，但推荐填写）。key 为评估指标 ID，value 为配置对象。"
                            "评估指标 ID 必须从下列内置指标中选取，按验证强度递增："
                            "\n- file_check：文件检查（工具自动，验证文件存在性/非空/内容匹配）"
                            "\n- bash_check：命令检查（工具自动，通过命令退出码判定结果）"
                            "\n- semantic_check：语义检查（agent 自动，验证意图覆盖/匹配/幻觉等语义层面）"
                            "\n- human_review：人工审核（人类执行，验证需要人工审批/复核的主观或不可逆判断）"
                            "\n选用规则：用户要求'人类评估/人工审核/人工确认'时必须用 human_review，"
                            "不得用 semantic_check 替代；semantic_check 是 agent 自动语义判断，不涉及人类。"
                            "指标 ID 必须精确匹配，禁止自创或用 value 子字段名（如 pass_threshold）充当 key。"
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "description": "评估指标配置对象",
                            "properties": {
                                "input_params": {
                                    "type": "object",
                                    "description": (
                                        "传递给评估工具的参数。不同指标所需参数不同："
                                        'file_check 需要 {"path": "src/main.py"}；'
                                        'bash_check 需要 {"command": "pytest tests/"}；'
                                        'semantic_check 需要 {"criteria": "评估要求描述"}；'
                                        'human_review 需要 {"title": "审核标题", "mode": "choice"}。'
                                    ),
                                },
                                "expected_output": {
                                    "type": "object",
                                    "description": "预期输出，用于验证评估结果（可选）",
                                },
                                "pass_threshold": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 100,
                                    "description": "任务级别的通过阈值（0-100），优先级高于指标默认阈值",
                                },
                            },
                            "required": [],
                        },
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                        "description": "任务优先级，1-10，数值越大优先级越高",
                    },
                    "max_retries": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 3,
                        "description": "任务失败时的最大重试次数",
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["non_container", "container"],
                        "default": "non_container",
                        "description": (
                            "任务范围：non_container（非容器任务，实际执行的任务，"
                            "必须指定 target_type 和 target_id）。"
                            "container 仅限 L1 Agent 使用（用于组织复杂长期任务的子任务链），"
                            "L2 Agent 禁止使用 container"
                        ),
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": ("父任务 ID。为容器任务创建子任务时需要指定此参数，将子任务关联到对应的容器。"),
                    },
                    "workspace": {
                        "type": "string",
                        "description": (
                            "目标项目路径。指定任务需要操作（读取或修改）的项目目录。"
                            "**重要**：当任务需要对某个特定文件夹进行读取或修改时，"
                            "必须设置此参数为该目标文件夹的路径，否则任务将无法定位到正确的目标目录。"
                            "可用范围（按任务类型）：普通任务可填；容器任务可填（作为容器空间的源项目，"
                            "系统复制到隔离空间操作，不设置则创建空容器空间）；"
                            "容器直接子任务**不可填**——工作空间继承容器，显式指定会被拒绝。"
                            "工作空间拓扑由 workspace_mode 决定（worktree=在目标项目上建隔离副本；"
                            "plain=直接操作目标目录）；执行环境隔离由 isolation_level 决定，两者独立。"
                        ),
                    },
                    "workspace_mode": {
                        "type": "string",
                        "enum": ["worktree", "plain"],
                        "description": (
                            "工作空间拓扑（普通任务可选，默认 worktree）。"
                            "worktree：在目标项目上创建 git worktree 隔离操作，不影响原项目（默认）。"
                            "plain：直接在目标目录工作，不建 worktree、不切分支。"
                            "与 isolation_level（执行环境容器/宿主）相互独立。"
                            "容器任务与容器直接子任务**不可选**（容器不直接执行；子任务继承容器空间）。"
                        ),
                    },
                    "isolation_level": {
                        "type": "string",
                        "enum": ["non_isolated", "isolated"],
                        "description": (
                            "执行环境隔离级别（普通任务可选，默认使用系统配置）。"
                            "non_isolated：非隔离，直接在宿主环境执行。"
                            "isolated：隔离，在容器执行环境中工作。"
                            "只决定执行环境，不决定工作空间拓扑（拓扑由 workspace_mode 决定）。"
                            "容器任务与容器直接子任务**不可选**（容器不直接执行；子任务继承容器）。"
                        ),
                    },
                    "inherit_from": {
                        "type": "string",
                        "description": "源任务 ID（被继承的任务）。设置后需同时指定 inherit_mode",
                    },
                    "inherit_mode": {
                        "description": (
                            "继承模式（需配合 inherit_from 使用）：\n"
                            '- "pipe"：继承对话管道（消息历史），适合改了目标但保留上下文\n'
                            '- "workspace"：继承工作空间（文件目录），适合换方案但保留文件\n'
                            '- ["pipe","workspace"]：同时继承对话与文件（最常见延续场景）'
                        ),
                        "oneOf": [
                            {
                                "type": "string",
                                "enum": ["pipe", "workspace"],
                            },
                            {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["pipe", "workspace"],
                                },
                            },
                        ],
                    },
                },
                "required": ["goal_title"],
                "allOf": [
                    {
                        "if": {
                            "not": {"required": ["task_scope"], "properties": {"task_scope": {"const": "container"}}}
                        },
                        "then": {
                            "required": [
                                "target_type",
                                "target_id",
                            ],
                        },
                    },
                    {
                        "if": {"required": ["task_scope"], "properties": {"task_scope": {"const": "container"}}},
                        "then": {
                            "not": {
                                "anyOf": [
                                    {"required": ["target_type"]},
                                    {"required": ["target_id"]},
                                    {"required": ["parent_task_id"]},
                                ]
                            }
                        },
                    },
                ],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["task", "submit"],
            injected_params=[
                "user_id",
                "session_id",
                "task_id",
                "pipeline_id",
                "dependencies",
                "tool_record_id",
                "parent_agent_level",
            ],
            param_level_restrictions={
                "task_scope": {
                    "enum_restrictions": {
                        "non_container": 0,
                        "container": 1,
                    },
                    "max_visible_level": 1,  # L2/L3 不需要看到此参数，默认 non_container
                },
                "parent_task_id": {
                    "max_visible_level": 1,  # L2/L3 系统自动注入，不应手动指定
                },
                "workspace": {
                    "max_visible_level": 3,  # 普通任务由 agent 直接选（容器直接子任务执行期拒绝）
                },
                "workspace_mode": {
                    "max_visible_level": 3,  # worktree/plain 由 agent 直接选
                },
                "isolation_level": {
                    "max_visible_level": 3,  # 执行环境由 agent 直接选
                },
            },
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911,PLR0912,PLR0915
        """执行任务提交。"""
        import time as _time  # noqa: PLC0415

        _t0 = _time.monotonic()
        task_scope = inputs.get("task_scope", "non_container")
        # ── goal 字段解析（schema 已平铺为 goal_title/goal_description） ──
        # 优先读扁平字段；同时兼容旧的 goal 对象（历史调用方/未刷新 schema 的 LLM）。
        goal = inputs.get("goal")
        if goal is None and (inputs.get("goal_title") is not None):
            # 平铺后的标准入口：把扁平字段重组为下游统一使用的 {title, description}
            goal = {
                "title": inputs.get("goal_title"),
                "description": inputs.get("goal_description", ""),
            }
        elif isinstance(goal, str):
            import json  # noqa: PLC0415

            try:
                goal = json.loads(goal)
            except (json.JSONDecodeError, ValueError):
                # LLM 可能将 goal 作为纯文本标题传递而非 JSON 对象，
                # 此时将其作为 title 使用
                goal = {"title": goal}
                logger.info(
                    "[TaskSubmit] goal 为纯文本，自动包装为 {'title': '%s'}",
                    goal["title"][:80],
                )
        if not isinstance(goal, dict):
            logger.warning(
                "[TaskSubmit] goal 类型异常: %s (value=%s)",
                type(goal).__name__ if goal is not None else "None",
                str(goal)[:200] if goal else "None",
            )
            goal = None
        parent_agent_level = inputs.get("parent_agent_level")

        logger.info(
            "[TaskSubmit] 开始执行 | task_scope=%s | parent_agent_level=%s",
            task_scope,
            parent_agent_level,
        )

        # ── 0. 注入参数校验 ──
        if parent_agent_level is None:
            logger.error("[TaskSubmit] 注入参数缺失 | parent_agent_level 未注入")
            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        # ── 1. 基础参数验证 ──
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 为空")
            return create_failure_result(
                error="必须提供 goal（含 title 字段）",
                error_code="MISSING_GOAL",
            )

        # 容器任务走独立分支（_execute_long_term 内部也有层级校验，
        # 此处提前拦截避免进入容器创建流程）
        if task_scope == "container":
            if parent_agent_level >= 2:
                logger.warning(
                    "[TaskSubmit] L%d Agent 试图创建容器任务，已拦截",
                    parent_agent_level,
                )
                return create_failure_result(
                    error=(
                        "L2/L3 Agent 不能创建 container 任务。"
                        "你已在 non_container 任务中，"
                        "直接使用 task_submit(task_scope='non_container') "
                        "创建子任务即可"
                    ),
                    error_code="L2_CANNOT_SUBMIT_CONTAINER",
                )
            return await self._execute_long_term(inputs)

        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        description = _normalize_description(goal.get("description", ""))
        acceptance_criteria = inputs.get("acceptance_criteria", {})
        parent_task_id = inputs.get("parent_task_id")

        # P0-3 纵深防御：校验 parent_task_id 归属，防 L2/L3 伪造他人父任务越权挂载
        # （继承他人管道/工作空间/上下文）。合法链：父任务必须由更高层级提交。
        _own_ok, _own_err = self._check_parent_ownership(parent_agent_level, parent_task_id)
        if not _own_ok:
            logger.warning(
                "[TaskSubmit] parent_task_id 归属校验失败 | parent=%s | L%d | reason=%s",
                parent_task_id,
                parent_agent_level,
                _own_err,
            )
            return create_failure_result(
                error=_own_err or "parent_task_id 归属校验失败",
                error_code="INSUFFICIENT_PERMISSION",
            )

        logger.info(
            "[TaskSubmit] description 追踪 | has_inputs_desc=%s | has_goal_desc=%s | final_desc_len=%d | preview=%s",
            bool(inputs.get("description")),
            bool(goal.get("description")),
            len(description),
            description[:80] if description else "(empty)",
        )

        # ── 描述长度硬限制（防止超大消息体打爆 LLM API） ──
        _MAX_DESC_LEN = 2000  # noqa: N806
        if len(description) > _MAX_DESC_LEN:
            logger.warning(
                "[TaskSubmit] 描述超长拒绝 | len=%d | max=%d | preview=%.100s",
                len(description),
                _MAX_DESC_LEN,
                description[:100],
            )
            return create_failure_result(
                error=(
                    f"任务描述过长（{len(description)}字符，上限{_MAX_DESC_LEN}字符）。"
                    "请精简描述，只写目标和文件路径，让下级 Agent 自行 file_read 文件内容。"
                ),
                error_code="DESCRIPTION_TOO_LONG",
            )

        if not isinstance(acceptance_criteria, dict):
            logger.warning(
                "[TaskSubmit] acceptance_criteria 类型异常: %s，重置为空 dict",
                type(acceptance_criteria).__name__,
            )
            acceptance_criteria = {}

        # ── 验收标准铁律：不 fallback、不默认、不覆盖，只认大模型输入 ──
        # 评估指标完全由大模型在 acceptance_criteria 中显式指定，
        # 禁止用 agent 配置的 recommended_metrics 自动补全或覆盖，
        # 禁止任何形式的兜底默认值。模型不传 → 无验收标准（不强行补）。
        # recommended_metrics 仅作文档参考，不参与提交期逻辑。
        if acceptance_criteria:
            logger.info(
                "[TaskSubmit] 采用大模型显式传入的验收标准 | metrics=%s",
                list(acceptance_criteria.keys()),
            )

        # ── 评估指标 ID 合法性校验 ──
        # 防止 LLM 把 pass_threshold / $text 等 value 子字段误填为 acceptance_criteria
        # 的 key（即 metric_id），导致评估期 METRIC_NOT_FOUND 反复重试直至失败。
        # 全部无效 → 拒绝提交并引导 LLM 使用正确指标 ID；
        # 部分无效 → 剔除无效项后继续（降级，不阻断）。
        original_keys = list(acceptance_criteria.keys())
        if original_keys:
            acceptance_criteria, invalid_ids = _validate_metric_ids(acceptance_criteria)
            if invalid_ids and not acceptance_criteria:
                valid_ids = _get_valid_metric_ids() or set()
                valid_list = ", ".join(sorted(valid_ids)) if valid_ids else "(指标加载失败)"
                logger.warning(
                    "[TaskSubmit] acceptance_criteria 全部 key 无效，拒绝提交 | invalid=%s | valid=%s",
                    invalid_ids,
                    sorted(valid_ids),
                )
                return create_failure_result(
                    error=(
                        f"acceptance_criteria 的 key（评估指标 ID）全部无效: "
                        f"{invalid_ids}。这些 key 必须是真实存在的评估指标 ID，"
                        f"不能是 pass_threshold / expected_output 等 value 子字段。"
                        f"当前合法指标 ID: {valid_list}。"
                        "请改用合法指标 ID 重新提交（系统不会自动补全或覆盖你传入的指标）。"
                    ),
                    error_code="INVALID_METRIC_ID",
                )
            if invalid_ids:
                logger.warning(
                    "[TaskSubmit] 剔除 acceptance_criteria 中的无效 metric_id | invalid=%s | kept=%s",
                    invalid_ids,
                    list(acceptance_criteria.keys()),
                )

        # ── L2/L3 层级校验：禁止显式指定 parent_task_id ──
        if parent_agent_level >= 2 and task_scope != "container" and parent_task_id is not None:
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 parent_task_id=%s，已拦截",
                parent_agent_level,
                parent_task_id,
            )
            return create_failure_result(
                error=f"L{parent_agent_level} Agent 不能显式指定 parent_task_id（系统自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        injected_task_id = inputs.get("task_id")
        if parent_task_id is None and injected_task_id:
            parent_task_id = injected_task_id
            logger.info(
                "[TaskSubmit] 自动注入 parent_task_id=%s (来自管道所属任务)",
                parent_task_id,
            )

        # ── 任务类型 × 参数可用性（workspace 拓扑与隔离已拆分为显式选择）──
        # - 容器任务：workspace 可填（容器空间源项目）；workspace_mode / isolation_level 不可选
        # - 容器直接子任务：workspace_mode / isolation_level 可自选（决定 worktree 与执行环境）；
        #   workspace 不可设置（工作空间继承容器，无需/不允许指定路径）；
        #   但可 inherit workspace——仅限父容器下的 worktree 源空间（源任务源空间与容器一致）
        # - 普通子任务：只允许 inherit pipe（管道继承）；workspace / workspace_mode /
        #   isolation_level / inherit workspace 一律拒绝（继承父任务）
        # - 普通根任务（无父任务）：三者均可填
        # param_inject 已对 task_submit 跳过 workspace/isolation_level 注入，
        # 此处 inputs 中的值即为 agent 显式选择，按任务类型强制校验。
        _parent_scope = "non_container"
        _parent_ws_root: str | None = None  # 父容器任务的源空间（ws_meta.project_root/path）
        _parent_scope_query_error: str | None = None  # 非 None = 父任务 scope 查询失败（拓扑未知）
        if parent_task_id:
            try:
                _svc = self._get_task_service()
                if _svc:
                    _parent_task = _svc.get_task(parent_task_id)
                    if _parent_task and _parent_task.metadata:
                        _parent_scope = _parent_task.metadata.get("task_scope", "non_container")
                        _parent_ws_meta = _parent_task.metadata.get("ws_meta")
                        if isinstance(_parent_ws_meta, dict):
                            _parent_ws_root = _parent_ws_meta.get("project_root") or _parent_ws_meta.get("path")
            except Exception as exc:
                # 查询失败不再静默降级为 non_container（拓扑未知却按已知校验会放行/误拒）：
                # 标记 unknown，下方受限操作保守拒绝。
                _parent_scope = "unknown"
                _parent_scope_query_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[TaskSubmit] 查询父任务 scope 失败，受限操作将保守拒绝 | parent_task_id=%s | err=%s",
                    parent_task_id,
                    exc,
                )

        def _same_workspace_root(path_a: str | None, path_b: str | None) -> bool:
            """规范化比较两个路径是否同一源空间（Windows 忽略大小写）。"""
            if not path_a or not path_b:
                return False
            try:
                norm = os.path.normpath
                a = norm(path_a).lower() if os.name == "nt" else norm(path_a)
                b = norm(path_b).lower() if os.name == "nt" else norm(path_b)
                return a == b
            except (ValueError, TypeError):
                return False

        def _parse_inherit_modes(inp: dict[str, Any]) -> set[str]:
            """解析 inherit_mode / inherit.mode 为集合（两分支共用口径）。"""
            mode = inp.get("inherit_mode") or (
                inp.get("inherit", {}).get("mode") if isinstance(inp.get("inherit"), dict) else None
            )
            if isinstance(mode, str):
                return {mode}
            return set(mode) if isinstance(mode, (list, tuple)) else set()

        def _inherit_from_id_of(inp: dict[str, Any]) -> str:
            """提取继承源任务 ID（扁平 inherit_from 与旧式 inherit.from 同口径）。"""
            cfg = inp.get("inherit")
            if isinstance(cfg, dict):
                return cfg.get("from", "") or ""
            return inp.get("inherit_from") or ""

        _inherit_modes = _parse_inherit_modes(inputs)

        # ── 父任务 scope 查询失败：受限操作一律保守拒绝（提示 scope 查询失败）──
        # workspace 拓扑参数（workspace / workspace_mode / isolation_level /
        # inherit workspace）的放行与否取决于父任务是否 container，查询失败时
        # 无法判定 → 拒绝而非按默认值放行。
        if _parent_scope_query_error is not None:
            _restricted_params = [p for p in ("workspace", "workspace_mode", "isolation_level") if inputs.get(p)]
            _restricted_inherit = _inherit_from_id_of(inputs) and "workspace" in _inherit_modes
            if _restricted_params or _restricted_inherit:
                logger.warning(
                    "[TaskSubmit] scope 查询失败且请求含受限参数，保守拒绝 | parent_task_id=%s | params=%s | inherit_ws=%s",
                    parent_task_id,
                    _restricted_params,
                    bool(_restricted_inherit),
                )
                return create_failure_result(
                    error=(
                        f"父任务 scope 查询失败（{_parent_scope_query_error}），"
                        "无法校验工作空间拓扑参数，本次受限操作已保守拒绝。请稍后重试；"
                        "如持续失败请检查 task 服务可用性。"
                    ),
                    error_code="PARENT_SCOPE_QUERY_FAILED",
                )

        # ── 容器直接子任务：可自选拓扑/隔离；workspace 不可设置（继承容器）──
        if _parent_scope == "container":
            if inputs.get("workspace"):
                logger.warning(
                    "[TaskSubmit] 容器直接子任务设置 workspace 被拒绝（继承容器）| "
                    "parent_task_id=%s | workspace=%s",
                    parent_task_id,
                    inputs["workspace"],
                )
                return create_failure_result(
                    error=(
                        "容器直接子任务的工作空间继承容器任务，不能设置 workspace。"
                        "请去掉该参数重新提交（不填即继承容器空间）。"
                    ),
                    error_code="CONTAINER_CHILD_PARAM_FORBIDDEN",
                )
            # workspace_mode / isolation_level 允许自选 → 不做任何拦截
            # inherit workspace：允许，但只能继承父容器下的 worktree 源空间
            # （源任务的 worktree 源空间必须与容器任务一致）
            _inherit_from_id = _inherit_from_id_of(inputs)
            if _inherit_from_id and "workspace" in _inherit_modes:
                _inherit_ws_root: str | None = None
                try:
                    _svc2 = self._get_task_service()
                    if _svc2:
                        _src_task = _svc2.get_task(_inherit_from_id)
                        if _src_task and isinstance(_src_task.metadata, dict):
                            _src_ws = _src_task.metadata.get("ws_meta")
                            if isinstance(_src_ws, dict):
                                _inherit_ws_root = _src_ws.get("project_root") or _src_ws.get("path")
                except Exception as exc:
                    # 源空间查询失败 → 无法核对与容器一致性 → 保守拒绝（不静默按 None 比对）
                    logger.warning(
                        "[TaskSubmit] 查询 inherit workspace 源空间失败，保守拒绝 | parent_task_id=%s | inherit_from=%s | err=%s",
                        parent_task_id,
                        _inherit_from_id,
                        exc,
                    )
                    return create_failure_result(
                        error=(
                            f"inherit workspace 源空间查询失败（{exc}），"
                            "无法核对与容器源空间的一致性，本次受限操作已保守拒绝。请稍后重试。"
                        ),
                        error_code="INHERIT_WS_QUERY_FAILED",
                    )
                if not _same_workspace_root(_inherit_ws_root, _parent_ws_root):
                    logger.warning(
                        "[TaskSubmit] 容器直接子任务 inherit workspace 源空间不一致被拒绝 | "
                        "parent_task_id=%s | inherit_from=%s | src_root=%s | container_root=%s",
                        parent_task_id,
                        _inherit_from_id,
                        _inherit_ws_root,
                        _parent_ws_root,
                    )
                    return create_failure_result(
                        error=(
                            "容器直接子任务只能继承父容器下的 worktree 工作空间："
                            "源任务的 worktree 源空间必须与容器任务一致。"
                            f"源任务源空间: {_inherit_ws_root or '未知'}，容器源空间: {_parent_ws_root or '未知'}。"
                            "如需继承对话历史，请使用 inherit_mode=['pipe']。"
                        ),
                        error_code="CONTAINER_CHILD_WORKSPACE_MISMATCH",
                    )

        # ── 普通子任务：只允许 inherit pipe；其余一律继承父任务 ──
        elif parent_task_id and _parent_scope != "container":
            # inherit workspace：普通子任务只能继承管道，工作空间继承被拒绝
            if _inherit_from_id_of(inputs) and "workspace" in _inherit_modes:
                logger.warning(
                    "[TaskSubmit] 普通子任务 inherit workspace 被拒绝（只能继承管道）| "
                    "parent_task_id=%s | inherit_from=%s",
                    parent_task_id,
                    _inherit_from_id_of(inputs),
                )
                return create_failure_result(
                    error=(
                        "普通子任务只能继承管道（inherit_mode=['pipe']，对话历史），"
                        "工作空间一律继承父任务，不能继承其它任务的工作空间。"
                    ),
                    error_code="SUBTASK_INHERITS_PARAMS",
                )
            for _p in ("workspace", "workspace_mode", "isolation_level"):
                if inputs.get(_p):
                    logger.warning(
                        "[TaskSubmit] 普通子任务显式指定 %s 被拒绝（继承父任务）| parent_task_id=%s | value=%s",
                        _p,
                        parent_task_id,
                        inputs.get(_p),
                    )
                    return create_failure_result(
                        error=(
                            f"普通子任务继承父任务的工作空间与隔离配置，不能指定 {_p}。"
                            "如需继承对话历史，请使用 inherit_from + inherit_mode=['pipe']。"
                        ),
                        error_code="SUBTASK_INHERITS_PARAMS",
                    )

        # ── L2/L3 层级校验：自动注入后仍无 parent_task_id → 拒绝创建根任务 ──
        if parent_agent_level >= 2 and task_scope != "container" and parent_task_id is None:
            # L2 调 task_submit 时 parent_task_id 理应自动注入（来自 state["task_id"]）。
            # 此处触发说明注入链断裂。诊断字段定位断裂点：
            # - injected_task_id 空 → param_inject 没注入或 state["task_id"] 为空
            # - inputs 无 task_id 键 → param_inject 完全没处理此调用
            # - task_id 键存在但为空 → state["task_id"] 在引擎 state 中缺失
            _diag_keys = [
                k
                for k in inputs
                if k
                in (
                    "task_id",
                    "parent_task_id",
                    "session_id",
                    "pipeline_id",
                    "parent_agent_level",
                    "workspace",
                )
            ]
            logger.error(
                "[TaskSubmit][DIAG] L%d 无可注入 parent_task_id，注入链断裂诊断 | "
                "injected_task_id=%r | inputs_has_task_id=%s | "
                "inputs[task_id]=%r | diag_keys=%s | all_input_keys=%s",
                parent_agent_level,
                injected_task_id,
                "task_id" in inputs,
                inputs.get("task_id"),
                _diag_keys,
                sorted(inputs.keys()),
            )
            logger.warning(
                "[TaskSubmit] L%d Agent 无可注入的 parent_task_id，拒绝创建根任务",
                parent_agent_level,
            )
            return create_failure_result(
                error=f"L{parent_agent_level} Agent 必须在任务上下文中提交子任务，无法创建根任务",
                error_code="L2_REQUIRES_PARENT_TASK",
            )

        workspace = inputs.get("workspace", "")

        # pipe 继承的源任务 pipeline_run_id（仅 pipe 模式设置）
        _inherit_pipe_pipeline_id = ""

        # ── inherit 参数解析（优先于 inherit_workspace_from） ──
        # inherit 是新的资源继承统一入口，支持 pipe 和 workspace 两种模式。
        # mode 既可传单个字符串（"pipe"/"workspace"），也可传列表（如
        # ["pipe", "workspace"]）同时继承对话管道和工作空间；两种模式相互独立，
        # 可任意组合。
        # 当 inherit 和 inherit_workspace_from 同时存在时，inherit 优先。
        # schema 已平铺为 inherit_from/inherit_mode，这里把扁平字段重组为 inherit 对象，
        # 供 _build_metadata 及管道引擎按既有契约读取（旧式 inherit 对象仍兼容）。
        _inherit_config = inputs.get("inherit")
        if not isinstance(_inherit_config, dict) and (
            inputs.get("inherit_from") is not None or inputs.get("inherit_mode") is not None
        ):
            _inherit_config = {
                "from": inputs.get("inherit_from", ""),
                "mode": inputs.get("inherit_mode", ""),
            }
            inputs["inherit"] = _inherit_config
        if _inherit_config and isinstance(_inherit_config, dict):
            _inherit_from_id = _inherit_config.get("from", "")
            _inherit_mode = _inherit_config.get("mode", "")
            # 规范化 mode 为集合：兼容 str / list / tuple
            if isinstance(_inherit_mode, str):
                _mode_set = {_inherit_mode}
            elif isinstance(_inherit_mode, (list, tuple)):
                _mode_set = set(_inherit_mode)
            else:
                _mode_set = set()
            if not _inherit_from_id or not _mode_set:
                return create_failure_result(
                    error="inherit 参数必须包含 from（源任务 ID）和 mode（pipe/workspace）",
                    error_code="INVALID_INHERIT_PARAMS",
                )
            # 校验：每个 mode 值必须合法
            _invalid_modes = _mode_set - {"pipe", "workspace"}
            if _invalid_modes:
                return create_failure_result(
                    error=(f"inherit.mode 不合法: '{sorted(_invalid_modes)}'，仅支持 pipe/workspace"),
                    error_code="INVALID_INHERIT_MODE",
                )
            # pipe 与 workspace 相互独立，可同时生效
            if "pipe" in _mode_set:
                _inherit_pipe_pipeline_id = ""
                _pipe_task_service = self._get_task_service()
                if _pipe_task_service:
                    try:
                        _source_task = _pipe_task_service.get_task(_inherit_from_id)
                        if _source_task and _source_task.pipeline_run_id:
                            _inherit_pipe_pipeline_id = _source_task.pipeline_run_id
                            logger.info(
                                "[TaskSubmit] inherit pipe | from=%s | source_pipeline=%s",
                                _inherit_from_id,
                                _inherit_pipe_pipeline_id[:12],
                            )
                        else:
                            logger.warning(
                                "[TaskSubmit] inherit pipe | from=%s | 源任务无 pipeline_run_id，对话历史为空",
                                _inherit_from_id,
                            )
                    except Exception as _pipe_err:
                        logger.warning(
                            "[TaskSubmit] inherit pipe 查找源任务失败: %s",
                            _pipe_err,
                        )
                else:
                    logger.warning(
                        "[TaskSubmit] inherit pipe | task_service 不可用，无法查找源任务 %s",
                        _inherit_from_id,
                    )
            if "workspace" in _mode_set:
                # workspace 模式等价于 inherit_workspace_from，复用现有逻辑
                inputs["inherit_workspace_from"] = _inherit_from_id
                logger.info(
                    "[TaskSubmit] inherit workspace | from=%s (覆盖 inherit_workspace_from)",
                    _inherit_from_id,
                )

        # ── inherit_workspace_from 解析 ──
        # 直接复用旧任务的 ws_meta.path，不复制、不初始化。
        # 旧工作空间不存在则报错返回，让 agent 重新提交。
        inherit_from = inputs.get("inherit_workspace_from")
        _inherit_resolved = False
        old_ws_meta = None
        # inherit_workspace_from 显式指定时，覆盖 param_inject 注入的 workspace
        if inherit_from:
            task_service = self._get_task_service()
            if not task_service:
                return create_failure_result(
                    error=(
                        f"无法查找任务 {inherit_from}：任务服务不可用。"
                        "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                    ),
                )
            try:
                old_task = task_service.get_task(inherit_from)
                if not old_task or not old_task.metadata:
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 不存在或无元数据。"
                            "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                        ),
                    )
                old_ws_meta = old_task.metadata.get("ws_meta")
                if not isinstance(old_ws_meta, dict):
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 没有工作空间信息。"
                            "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                        ),
                    )
                from pathlib import Path  # noqa: PLC0415

                # 同容器才能 inherit，避免产出落到错误容器。
                _source_root = old_ws_meta.get("project_root", "") or old_ws_meta.get("path", "")
                _current_container = Path(__file__).resolve().parents[4]
                if _source_root:
                    try:
                        Path(_source_root).resolve().relative_to(_current_container)
                    except ValueError:
                        return create_failure_result(
                            error=(
                                f"任务 {inherit_from} 属于其它容器({_source_root})，"
                                f"不能跨容器继承工作空间。"
                                f"请去掉 inherit_workspace_from 参数重新提交。"
                            ),
                        )
                old_ws_path = old_ws_meta.get("path", "")
                if not old_ws_path or not Path(old_ws_path).exists():
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 的工作空间已不存在: {old_ws_path or '(空)'}。"
                            "无法继承，请去掉 inherit_workspace_from 参数重新提交，"
                            "使用空工作空间开始。"
                        ),
                    )
                # worktree 模式下源 .git 失效则不继承（后续合并找不到 branch），
                # 报错让 agent 自行决定是否捞取已有产物。
                if old_ws_meta.get("mode") == "worktree":
                    if not (Path(old_ws_path) / ".git").exists():
                        return create_failure_result(
                            error=(
                                f"任务 {inherit_from} 的工作空间: git 身份已失效,"
                                f"目录里产物可能仍在可手动读取或者处理: {old_ws_path}。"
                                f"请去掉 inherit_workspace_from 参数重新提交。"
                            ),
                        )
                workspace = old_ws_path
                _inherit_resolved = True
                logger.info(
                    "[TaskSubmit] inherit_workspace_from: task_id=%s, ws_path=%s",
                    inherit_from,
                    old_ws_path,
                )
            except Exception as e:
                logger.warning(
                    "[TaskSubmit] inherit_workspace_from 解析失败: %s",
                    e,
                )
                return create_failure_result(
                    error=f"继承工作空间时出错: {e}。请去掉 inherit_workspace_from 参数重新提交。",
                )
        # 继承成功时回写 inputs，确保 _build_metadata 存储到任务元数据
        if _inherit_resolved:
            inputs["workspace"] = workspace

        # ── 目标空间安全检查 ──
        if workspace:
            ws_error = _validate_workspace_path(workspace)
            if ws_error:
                return create_failure_result(
                    error=ws_error,
                    error_code="UNSAFE_WORKSPACE",
                )

        logger.info(
            "[TaskSubmit] 非容器任务 | target_type=%s | target_id=%s",
            target_type,
            target_id,
        )
        logger.debug(
            "[TaskSubmit] 任务详情 | title=%s | metric_count=%d",
            goal.get("title", "N/A"),
            len(acceptance_criteria),
        )

        # ── 2. 非容器任务必填参数验证 ──
        if not target_type:
            return create_failure_result(
                error="目标类型不能为空",
                error_code="MISSING_TARGET_TYPE",
            )
        if not target_id:
            return create_failure_result(
                error="目标 ID 不能为空",
                error_code="MISSING_TARGET_ID",
            )

        # ── 2.5 目标 Agent 存在性与级别校验 ──
        if target_type == "agent":
            valid, err_msg, err_code = self._validate_target_agent(
                target_id,
                parent_agent_level,
            )
            if not valid:
                logger.warning(
                    "[TaskSubmit] 目标 Agent 校验失败 | target_id=%s | parent_level=L%d | error=%s",
                    target_id,
                    parent_agent_level,
                    err_msg,
                )
                return create_failure_result(error=err_msg, error_code=err_code)
            logger.info(
                "[TaskSubmit] 目标 Agent 校验通过 | target_id=%s | parent_level=L%d",
                target_id,
                parent_agent_level,
            )

        # ── 3. 权限验证 ──
        if not self._validate_parent_task_id(parent_agent_level, parent_task_id, task_scope):
            return create_failure_result(
                error="L2 Agent 不能显式指定 parent_task_id（系统会自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        # ── 4. 依赖任务验证 ──
        dependencies = inputs.get("dependencies", [])
        if dependencies:
            missing_ids = self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error("[TaskSubmit] 依赖验证失败 | 不存在的任务: %s", missing_ids)
                return create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info("[TaskSubmit] 依赖验证通过 | dependencies=%s", dependencies)

        # ── 5. 获取服务 ──
        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="任务服务不可用，请检查系统配置",
                error_code="SERVICE_UNAVAILABLE",
            )

        # 任务类型 × 参数可用性校验已在 parent_task_id 注入后完成（见上文）：
        # - 容器直接子任务：workspace / workspace_mode / isolation_level 已拒绝或清除（继承容器）；
        # - 普通任务：三者保留（agent 显式选择，直接进入 _build_metadata 与 task_data）。

        # ── 6. 创建任务 ──
        raw_priority = inputs.get("priority", 5)
        try:
            from task_types import TaskPriority as TP  # noqa: N817,PLC0415

            TP(raw_priority)
        except (ValueError, AttributeError):
            raw_priority = 5

        try:
            child_agent_level = min(parent_agent_level + 1, 3)
            from agents_types import AgentLevel  # noqa: PLC0415

            level_values = {"L1": 1, "L2": 2, "L3": 3}
            level_str = f"L{child_agent_level}"
            child_level = AgentLevel(level_str) if level_str in level_values else AgentLevel.L3_ATOMIC

            pipeline_id = inputs.get("pipeline_id")
            task = await task_service.create_task(
                title=goal["title"],
                description=description,
                parent_task_id=parent_task_id,
                parent_pipeline_id=pipeline_id,
                target_type=target_type,
                dependencies=dependencies or None,
                priority=raw_priority,
                agent_level=child_level,
                metadata=self._build_metadata(inputs, goal, acceptance_criteria),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 任务创建失败: %s", e)
            return create_failure_result(
                error=f"任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        _t_create = _time.monotonic()
        logger.info("[TaskSubmit] PERF | create_task=%.1fms", (_t_create - _t0) * 1000)

        # ── 7. 提交完成（0.2 收尾：start_run 占位已移除，任务提交即落库；
        #    任务管道执行由会话对话 / chat.send_message → PipelineExecutor 驱动）──
        is_root = True
        if parent_task_id and task_service:
            try:
                parent_task = task_service.get_task(parent_task_id)
                if parent_task and parent_task.metadata:
                    parent_scope = parent_task.metadata.get("task_scope", "non_container")
                    if parent_scope != "container":
                        is_root = False
            except Exception as exc:
                # 任务已创建，此处仅是元数据标记：不回滚、不放行为目的改判——
                # 维持默认 is_root=True（与查询失败前语义一致），仅记录 warning 供排查。
                logger.warning(
                    "[TaskSubmit] 查询父任务 scope 失败，is_root 维持默认 True | parent_task_id=%s | err=%s",
                    parent_task_id,
                    exc,
                )

        if _inherit_resolved:
            is_root = True

        task_data = {
            "task_id": task.id,
            "target_type": target_type,
            "target_id": target_id,
            "user_input": goal.get("title", ""),
            "description": description or goal.get("description", ""),
            "acceptance_criteria": acceptance_criteria,
            "workspace": workspace,
            "priority": inputs.get("priority", 5),
            "is_root": is_root,
            # 工作空间拓扑（worktree/plain，与隔离解耦）：普通任务由 agent 显式选择，
            # 容器直接子任务/未指定时为空（父链解析 + 系统默认）
            "workspace_mode": inputs.get("workspace_mode", ""),
            # 执行环境隔离（容器/宿主，与拓扑解耦）：普通任务显式选择；
            # 容器直接子任务/未指定时为空（系统默认策略）
            "isolation_level": inputs.get("isolation_level", ""),
            "_has_explicit_workspace": bool(workspace),
            "_inherit_workspace_resolved": _inherit_resolved,
            "_source_ws_meta": old_ws_meta if _inherit_resolved else None,
        }

        if _inherit_pipe_pipeline_id:
            task_data["_inherit_pipe_pipeline_id"] = _inherit_pipe_pipeline_id

        logger.info(
            "[TaskSubmit] task_data description 追踪 | task_id=%s | desc_in_task_data=%s | desc_len=%d",
            task.id,
            bool(task_data.get("description")),
            len(task_data.get("description", "")),
        )

        # ── 7. 同步初始化工作空间 ──
        # 工作空间解析必须在 submit 返回前完成，确保 ws_meta 写入 task.metadata。
        # 失败则清理任务记录并返回错误，不让 LLM 误以为任务可执行。
        task, ws_err = await self._init_workspace(
            task,
            workspace,
            task_data,
            task_service,
        )
        if ws_err:
            await task_service.hard_delete(task.id)
            return create_failure_result(error=ws_err, error_code="WORKSPACE_INIT_FAILED")

        # ── 7.5 pipe 继承：同步 clone 源管道历史 ──
        # 历史准备（clone）必须在 submit 返回前完成：clone 失败则任务提交失败，
        # 让父 LLM 知道子任务起不来。预生成 pipeline_id 并 clone 到目标管道，
        # task_executor 复用该 id（不再重复 clone）。
        if _inherit_pipe_pipeline_id:
            import uuid as _uuid  # noqa: PLC0415

            _pre_pipeline_id = _uuid.uuid4().hex[:12]
            try:
                exec_storage = _get_service_provider().get("execution_record_storage")
                if exec_storage:
                    # root_task_id 必须与 task_executor._bind_pipeline_run 的
                    # register_pipeline(pipeline_id, root_id) 一致，否则引擎注册时
                    # 会触发文件迁移，导致 clone 文件和引擎读取文件分裂。
                    _root_task_id = ""
                    if task_service:
                        _root_task_id = task_service.get_root_task_id(task.id) or ""
                    exec_storage.clone_pipeline_records(
                        source_pipeline_id=_inherit_pipe_pipeline_id,
                        target_pipeline_id=_pre_pipeline_id,
                        new_container_task_id=task.id,
                        root_task_id=_root_task_id,
                    )
                    # clone 成功：把预生成 id 传给 task_executor 复用
                    task_data["_pre_pipeline_id"] = _pre_pipeline_id
                    logger.info(
                        "[TaskSubmit] pipe 继承历史 clone 完成 | task=%s | src=%s | dst=%s | root=%s",
                        task.id,
                        _inherit_pipe_pipeline_id[:12],
                        _pre_pipeline_id[:12],
                        _root_task_id[:12] if _root_task_id else "(none)",
                    )
            except Exception as clone_exc:
                # clone 失败：清理任务记录，返回失败给父 LLM
                logger.error(
                    "[TaskSubmit] pipe 继承历史 clone 失败 | task=%s | error=%s",
                    task.id,
                    clone_exc,
                    exc_info=True,
                )
                await task_service.hard_delete(task.id)
                return create_failure_result(
                    error=f"继承管道历史失败：{clone_exc}",
                    error_code="INHERIT_PIPE_FAILED",
                )

        _t_submit = _time.monotonic()
        logger.info(
            "[TaskSubmit] PERF | submit_task=%.1fms | total=%.1fms",
            (_t_submit - _t_create) * 1000,
            (_t_submit - _t0) * 1000,
        )

        logger.info("[TaskSubmit] 任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        # 0.2 推送改走 frontend.emit capability（ADR §3.5），SDK 暂未实现该 capability；
        # 当前 task_status_update 广播静默跳过，0.2 栈不再依赖 0.1 的
        # src/channels/websocket/ws_interaction_notifier（task_11 P2-7）。
        # 待 SDK 实现后改用 ctx.frontend.emit(event="task_status_update", scope=...) 恢复。

        # ── 8. GAP-1：任务执行驱动——经 chat.send_message 创建执行管道 ──
        # run 未真正派发前不得声称"异步执行中"（e2e 缺口 GAP-1 的话术修正）：
        # 派发成功 → 绑定关联 + start_task（started_at 非空）+ 如实报告管道 id；
        # 派发器缺席/失败 → warning 话术（任务保留，可经 task_manage 重试）。
        dispatch = await self._dispatch_task_pipeline(
            task,
            inputs,
            description=task_data.get("description", ""),
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies,
            task_service=task_service,
        )
        if dispatch.get("pipeline_id"):
            try:
                await task_service.bind_pipeline_run(task.id, dispatch["pipeline_id"])
                await task_service.start_task(task.id)
            except Exception as assoc_exc:
                logger.error(
                    "[TaskSubmit] 任务↔管道关联/启动回写失败 | task_id=%s | pipeline_id=%s | err=%s",
                    task.id,
                    dispatch["pipeline_id"],
                    assoc_exc,
                )
                dispatch["warning"] = f"执行管道已创建，但任务状态回写失败：{assoc_exc}"

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "target_id": target_id,
        }
        if dispatch.get("pipeline_id"):
            result_data["pipeline_id"] = dispatch["pipeline_id"]
            result_data["message"] = (
                f"任务 [{task.title}]（ID: {task.id}）已提交，执行管道已创建"
                f"（pipeline {dispatch['pipeline_id']}），任务正在后台执行。"
                "子任务完成后系统会自动通知你并恢复执行。"
                "在此期间请不要再调用任何工具（包括 task_manage），直接输出纯文本等待即可。"
            )
        else:
            result_data["message"] = (
                f"任务 [{task.title}]（ID: {task.id}）已提交并落库，但执行管道未能创建"
                f"（{dispatch.get('warning', '未知原因')}）。"
                "任务当前不会自动执行；可稍后重试提交或调用 task_manage 处理。"
            )
            result_data["warning"] = dispatch.get("warning", "执行管道未创建")

        # 工作空间路径仅对 L1 返回（L2/L3 的 workspace 参数本身被隐藏，回显内部路径属信息泄漏）
        if parent_agent_level == 1:
            result_data["workspace"] = workspace or ""
            result_data["resolved_workspace"] = (task.metadata or {}).get("ws_meta", {}).get("path", "")

        return create_success_result(
            data=result_data,
            metadata={
                "action": "task_submit",
                "task_scope": task_scope,
            },
        )

    async def _dispatch_task_pipeline(  # noqa: PLR0913
        self,
        task: Any,
        inputs: dict[str, Any],
        description: str,
        acceptance_criteria: dict[str, Any],
        dependencies: list[str],
        task_service: Any,
    ) -> dict[str, Any]:
        """GAP-1：经 chat.send_message 创建任务执行管道（引擎生成 id）。

        契约（与内核 chat_send_handler 创建分支对齐）：
        - ``create: true`` + 不传 pipeline_id——引擎生成并在响应返回（三次定案：
          堵 id 冒占），拿返回 id 写任务↔管道关联由调用方完成；
        - ``state``：任务域字段出生即入（task.id/goal/status/description/
          acceptance_criteria/dependencies——扁平点号键，STATE_SUMMARY_KEYS 出口）；
        - ``lineage``：有父形式（parent = 调用方管道，origin_session 同管道——
          主会话 thread_id 与 pipeline_id 同值）/ 根形式（无调用方管道时诚实声明
          plugin 来源，不伪造默认父）二选一；
        - ``execution_context``：workspace/isolation/parent_task_id（task.metadata
          已组装，供 init 体 workspace_lifecycle 消费）；
        - ``background: true``：不阻塞工具调用等待任务完成（派发即返回 id）。

        Returns:
            ``{"pipeline_id": ...}`` 派发成功；``{"warning": ...}`` 派发器缺席/失败
            （任务已创建，由调用方决定话术）。
        """
        sender = _get_chat_sender()
        if sender is None:
            return {
                "warning": "chat capability 未注入（sidecar 未接线），任务未派发执行"
            }

        parent_pipeline_id = inputs.get("pipeline_id") or ""
        if parent_pipeline_id:
            lineage: dict[str, Any] = {
                "parent_pipeline_id": parent_pipeline_id,
                "origin_session_id": parent_pipeline_id,
            }
        else:
            lineage = {
                "root": True,
                "origin": {"kind": "plugin", "source": "task_submit"},
            }

        kickoff = f"执行任务「{task.title}」（任务 ID: {task.id}）。"
        if description:
            kickoff += f"\n任务描述：{description}"
        if acceptance_criteria:
            kickoff += f"\n验收标准：{acceptance_criteria}"

        params: dict[str, Any] = {
            "create": True,
            "message": kickoff,
            "user_id": inputs.get("user_id") or "task_system",
            "state": {
                "task.id": task.id,
                "task.goal": task.title,
                "task.status": "pending",
                "task.description": description or "",
                "task.acceptance_criteria": acceptance_criteria or {},
                "task.dependencies": dependencies or [],
                "task.submitted_by": inputs.get("user_id", ""),
            },
            "lineage": lineage,
            "background": True,
        }
        execution_context = (task.metadata or {}).get("execution_context")
        if execution_context:
            params["execution_context"] = execution_context

        try:
            resp = await sender(params)
        except Exception as exc:
            logger.error(
                "[TaskSubmit] 任务管道派发失败 | task_id=%s | err=%s",
                task.id,
                exc,
                exc_info=True,
            )
            return {"warning": f"chat.send_message 派发失败：{exc}"}

        pipeline_id = ""
        if isinstance(resp, dict):
            pipeline_id = str(resp.get("pipeline_id") or "")
        if not pipeline_id:
            return {"warning": f"派发响应缺少 pipeline_id：{resp!r}"}
        logger.info(
            "[TaskSubmit] 任务执行管道已创建 | task_id=%s | pipeline_id=%s",
            task.id,
            pipeline_id,
        )
        return {"pipeline_id": pipeline_id}

    async def _execute_long_term(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0912,PLR0915
        """处理容器任务提交。"""
        # goal 解析逻辑同 execute()：优先扁平字段，兼容旧式 goal 对象
        goal = inputs.get("goal")
        if goal is None and inputs.get("goal_title") is not None:
            goal = {
                "title": inputs.get("goal_title"),
                "description": inputs.get("goal_description", ""),
            }
        parent_agent_level = inputs.get("parent_agent_level")

        # ── 容器任务参数可用性：workspace_mode / isolation_level 不可选 ──
        # 容器任务只做组织框架、不直接执行：空间拓扑恒为 container_copy（复制到
        # 容器空间），执行环境由系统默认——agent 无法（也无需）选择这两项。
        for _p in ("workspace_mode", "isolation_level"):
            if inputs.get(_p):
                return create_failure_result(
                    error=(
                        f"容器任务不能指定 {_p}（容器不直接执行，工作空间恒为容器副本，"
                        "隔离由系统默认）。请去掉该参数重新提交。"
                    ),
                    error_code="CONTAINER_PARAM_FORBIDDEN",
                )

        # ── 目标空间安全检查 ──
        workspace = inputs.get("workspace", "")
        if workspace:
            ws_error = _validate_workspace_path(workspace)
            if ws_error:
                return create_failure_result(
                    error=ws_error,
                    error_code="UNSAFE_WORKSPACE",
                )

        logger.info(
            "[TaskSubmit] 容器任务提交 | title=%s | parent_agent_level=%s",
            goal.get("title") if goal else "N/A",
            parent_agent_level,
        )

        if parent_agent_level != 1:
            return create_failure_result(
                error="容器任务只能由 L1 Agent 提交",
                error_code="L2_CANNOT_SUBMIT_CONTAINER",
            )

        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="任务服务不可用，请检查系统配置",
                error_code="SERVICE_UNAVAILABLE",
            )

        try:
            description = _normalize_description(goal.get("description", ""))
            pipeline_id = inputs.get("pipeline_id")
            task = await task_service.create_task(
                title=goal["title"],
                description=description,
                parent_pipeline_id=pipeline_id,
                metadata=self._build_metadata(inputs, goal, {}),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 容器任务创建失败: %s", e)
            return create_failure_result(
                error=f"容器任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        # 将当前管道 ID 绑定到容器任务，使子任务完成时能通知父管道
        pipeline_id = inputs.get("pipeline_id")
        if pipeline_id:
            try:
                await task_service.bind_pipeline_run(task.id, pipeline_id)
                logger.info(
                    "[TaskSubmit] 容器任务已绑定管道 | task_id=%s | pipeline_id=%s",
                    task.id,
                    pipeline_id,
                )
                exec_storage = self._get_execution_record_storage()
                if exec_storage:
                    root_id = task_service.get_root_task_id(task.id)
                    if root_id:
                        exec_storage.register_pipeline(pipeline_id, root_id)

                _session_id = inputs.get("session_id", "")
                if _session_id:
                    try:
                        from channels.api.memory_store import store as api_store  # noqa: PLC0415

                        _session = api_store.get_session(_session_id)
                        if _session:
                            _session.register_pipeline(pipeline_id, set_active=False)
                            api_store.set_session(_session_id, _session)
                    except Exception as _reg_exc:
                        logger.warning(
                            "[TaskSubmit] 注册容器管道到 api_store 失败: %s",
                            _reg_exc,
                        )
            except Exception as exc:
                logger.warning(
                    "[TaskSubmit] 容器任务绑定管道失败 | task_id=%s | error=%s",
                    task.id,
                    exc,
                )

        logger.info("[TaskSubmit] 容器任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        # 0.2 推送改走 frontend.emit capability（ADR §3.5），SDK 暂未实现该 capability；
        # 当前 task_status_update 广播静默跳过，0.2 栈不再依赖 0.1 的
        # src/channels/websocket/ws_interaction_notifier（task_11 P2-7）。
        # 待 SDK 实现后改用 ctx.frontend.emit(event="task_status_update", scope=...) 恢复。

        from isolation.workspace import resolve_container_workspace_path  # noqa: PLC0415

        container_workspace_path = resolve_container_workspace_path(
            inputs.get("workspace"),
            task.id,
            # 容器任务不可选隔离（校验已拒绝）→ 恒为隔离复制（isolated）
            isolation_mode="isolated",
        )

        # ── 同步初始化容器工作空间 ──
        # 与非容器任务一致：submit 返回前必须完成工作空间创建。
        # 容器空间恒复制（isolated），不再携带 isolation_mode 分支。
        _container_task_data = {"isolation_mode": "isolated"}
        task, ws_err = await self._init_workspace(
            task,
            inputs.get("workspace") or "",
            _container_task_data,
            task_service,
            is_container=True,
        )
        if ws_err:
            await task_service.hard_delete(task.id)
            return create_failure_result(error=ws_err, error_code="WORKSPACE_INIT_FAILED")

        # on_task_start 可能重算路径，以 ws_meta 为准
        _ws_meta_path = (task.metadata or {}).get("ws_meta", {}).get("path", "")
        if _ws_meta_path:
            container_workspace_path = _ws_meta_path

        result_data = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "task_scope": "container",
            "workspace": inputs.get("workspace") or "",
            "resolved_workspace": container_workspace_path,
        }

        result_data["message"] = (
            f"容器任务 [{task.title}]（ID: {task.id}）已提交"
            + (f"，工作空间：{container_workspace_path}。" if parent_agent_level == 1 else "。")
            + "容器只是组织框架，不直接执行。你现在必须立即继续操作："
            f"下一步——使用 task_submit(parent_task_id='{task.id}', target_type='agent', "
            "target_id='solution_planning_agent') 提交方案规划子任务。"
            "请在同一轮对话中立即调用，不要等待。"
        )

        return create_success_result(
            data=result_data,
            metadata={"action": "task_submit_container"},
        )

    @staticmethod
    async def _init_workspace(
        task: Any,
        workspace: str,
        task_data: dict[str, Any],
        task_service: Any,
        *,
        is_container: bool = False,
    ) -> tuple[Any, str | None]:
        """同步初始化工作空间，确保 ws_meta 写入 task.metadata 后才返回。"""

        provider = _get_service_provider()
        lifecycle = provider.get("workspace_lifecycle_manager") if provider else None
        if lifecycle is None:
            # 0.2 文档化降级（FP-MIGR）：sidecar 无 workspace_lifecycle_manager
            # 实例（0.1 infrastructure 层已废弃）→ 跳过工作空间初始化并记录警告，
            # 不阻塞任务提交（否则 0.2 下所有提交都会硬失败）。
            logger.warning(
                "[TaskSubmit] workspace_lifecycle_manager 不可用，跳过工作空间初始化 | task_id=%s",
                task.id,
            )
            return task, None

        # 兼容注入 isolation_mode（0.1 lifecycle 其余消费者仍读此字段；
        # 拓扑决策已改读 workspace_mode，见 workspace_lifecycle._start_root_task）
        if "isolation_mode" not in task_data:
            iso_level = (task.metadata or {}).get("isolation_level", "")
            if iso_level:
                task_data["isolation_mode"] = iso_level

        fn = lifecycle.init_container_workspace if is_container else lifecycle.on_task_start
        try:
            loop = asyncio.get_running_loop()
            ws_meta = await loop.run_in_executor(None, fn, task.id, workspace, task_data)
        except Exception as ws_err:
            logger.error(
                "[TaskSubmit] 工作空间初始化失败 | task_id=%s | container=%s | error=%s",
                task.id,
                is_container,
                ws_err,
            )
            return task, f"工作空间初始化失败: {ws_err}"

        # on_task_start 内部已调 _persist_ws_meta 写入 task.metadata；
        # init_container_workspace 只写内存 _ws_meta_store，不持久化，
        # 需要在此手动写入 task.metadata 以便后续统一读取。
        if is_container and isinstance(ws_meta, dict) and ws_meta.get("path"):
            task.metadata = task.metadata or {}
            task.metadata["ws_meta"] = ws_meta
            try:
                await task_service.save_task(task)
            except Exception as save_err:
                logger.warning(
                    "[TaskSubmit] 容器 ws_meta 持久化失败 (non-fatal) | task_id=%s | error=%s",
                    task.id,
                    save_err,
                )

        # 重新读取 task 获取 lifecycle 写入的最新 metadata（含 ws_meta）
        refreshed = task_service.get_task(task.id)
        if refreshed:
            task = refreshed
        if not (task.metadata or {}).get("ws_meta"):
            logger.error(
                "[TaskSubmit] 工作空间初始化完成但 ws_meta 缺失 | task_id=%s",
                task.id,
            )
            return task, "工作空间初始化异常：ws_meta 未生成"

        return task, None

    def _check_parent_ownership(
        self,
        parent_agent_level: int,
        parent_task_id: str | None,
    ) -> tuple[bool, str | None]:
        """P0-3 纵深防御：校验 parent_task_id 归属，防 L2/L3 伪造他人父任务越权挂载。

        合法链：子任务只能挂在「比自己更高层级」的祖先任务下——
        - L2 的父任务须由 L1 提交（``submitted_by_level == 1``）；
        - L3 的父任务须由 L1/L2 提交（``submitted_by_level < 3``）。
        父任务 ``submitted_by_level`` 缺失或 ≥ 本层级 → 视为他人同级任务，拒绝。

        L1 不受此约束（根 Agent，可提交根任务或挂在任意已存在任务下，存在性由
        ``_validate_parent_task_id`` 等后续校验保证）。

        Args:
            parent_agent_level: 调用者 Agent 层级
            parent_task_id: 客户端/框架传入的父任务 ID（可能被篡改）

        Returns:
            (ok, error_msg)：ok=False 时 error_msg 为拒绝原因
        """
        if parent_agent_level < 2 or not parent_task_id:
            return True, None
        service = self._get_task_service()
        if service is None:
            # 无服务时不在此阻断，交由后续存在性校验处理
            return True, None
        parent = service.get_task(parent_task_id)
        if parent is None:
            return False, f"父任务不存在: {parent_task_id}"
        parent_level = (getattr(parent, "metadata", None) or {}).get("submitted_by_level")
        if parent_level is None or parent_level >= parent_agent_level:
            return False, (
                f"权限不足：parent_task_id={parent_task_id} 非本 Agent 层级链所有"
                f"（parent submitted_by_level={parent_level}，当前 L{parent_agent_level}），"
                "无法在他人的同级任务下挂载子任务"
            )
        return True, None

    def _validate_parent_task_id(self, parent_agent_level: int, parent_task_id: str | None, task_scope: str) -> bool:
        """验证 parent_task_id 参数的使用权限。"""
        if task_scope == "container":
            if parent_task_id is not None:
                logger.warning("[TaskSubmit] 容器任务不能有父任务 | parent_task_id=%s", parent_task_id)
                return False
            return True

        if parent_agent_level == 1 and parent_task_id is not None:
            task_service = self._get_task_service()
            if task_service and task_service.get_task(parent_task_id) is None:
                logger.error("[TaskSubmit] parent_task_id 不存在: %s", parent_task_id)
                return False

        # L2/L3 non-container: parent_task_id 必须已由自动注入填充
        if parent_agent_level >= 2 and parent_task_id is None:
            logger.warning(
                "[TaskSubmit] L%d Agent 无 parent_task_id（纵深防御拦截）",
                parent_agent_level,
            )
            return False

        return True

    def _check_dependencies_exist(self, dependencies: list[str]) -> list[str]:
        """检查依赖任务是否存在。"""
        if not dependencies:
            return []

        task_service = self._get_task_service()
        if task_service is None:
            logger.warning("[TaskSubmit] TaskService 不可用，跳过依赖检查")
            return []

        missing_ids = []
        for dep_id in dependencies:
            if task_service.get_task(dep_id) is None:
                missing_ids.append(dep_id)
        return missing_ids

    def _build_metadata(  # noqa: PLR0912
        self,
        inputs: dict[str, Any],
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
    ) -> dict[str, Any]:
        """构建任务元数据。"""
        metadata: dict[str, Any] = {}

        # 存储验收标准（供 task_evaluate 使用）
        if acceptance_criteria:
            if isinstance(acceptance_criteria, dict):
                metadata["acceptance_criteria"] = acceptance_criteria
                metadata["evaluation_metric_ids"] = list(acceptance_criteria.keys())
            else:
                logger.warning(
                    "[TaskSubmit] _build_metadata 收到非 dict 的 acceptance_criteria: %s，跳过存储",
                    type(acceptance_criteria).__name__,
                )

        # 存储 session_id（供任务树 API 按会话过滤使用）
        session_id = inputs.get("session_id")
        if session_id:
            metadata["session_id"] = session_id

        # 记录提交者层级（供权限校验：每个 Agent 只能管理自己提交的任务）
        parent_agent_level = inputs.get("parent_agent_level")
        if parent_agent_level:
            metadata["submitted_by_level"] = parent_agent_level

        # 存储执行相关参数
        # workspace：仅存 agent 显式值（param_inject 已跳过注入、容器直接子任务
        # 已被拒绝清除），inherit_workspace_from 回写的旧路径同样落账
        if inputs.get("workspace"):
            metadata["workspace"] = inputs["workspace"]
        if inputs.get("max_retries"):
            metadata["max_retries"] = inputs["max_retries"]
        if inputs.get("task_scope"):
            metadata["task_scope"] = inputs["task_scope"]
        # 工作空间拓扑（worktree/plain）：agent 显式选择，仅普通任务可填
        if inputs.get("workspace_mode"):
            metadata["workspace_mode"] = inputs["workspace_mode"]
        # 执行环境隔离（isolated/non_isolated）：agent 显式选择，仅普通任务可填
        if inputs.get("isolation_level"):
            metadata["isolation_level"] = inputs["isolation_level"]

        # 结构化 execution_context（任务级）：供任务执行器经 chat.send_message
        # 透传 → 内核 initial_state 并入 → init 体 workspace_lifecycle /
        # environment_lifecycle 插件消费。容器任务拓扑恒为 container_copy。
        _ec: dict[str, Any] = {}
        if inputs.get("workspace"):
            _ec["workspace"] = {
                "source_path": inputs["workspace"],
                "mode": inputs.get("workspace_mode")
                or ("container_copy" if inputs.get("task_scope") == "container" else "worktree"),
                "explicit": True,
            }
        elif inputs.get("workspace_mode") or inputs.get("task_scope") == "container":
            # 无显式 workspace（继承父链）但有拓扑/容器声明
            _ec["workspace"] = {
                "source_path": "",
                "mode": inputs.get("workspace_mode")
                or ("container_copy" if inputs.get("task_scope") == "container" else "worktree"),
            }
        if inputs.get("isolation_level"):
            _ec["isolation"] = {"level": inputs["isolation_level"]}
        # 父任务链信息：供 init 体插件做容器直接子任务的父链查询
        # （0.2 sidecar 无 task_service，插件经此重建最小 task_tree）
        if inputs.get("parent_task_id"):
            _ec["parent_task_id"] = inputs["parent_task_id"]
        if _ec:
            metadata["execution_context"] = _ec

        # 存储执行者信息
        if inputs.get("target_id"):
            metadata["target_id"] = inputs["target_id"]

        if inputs.get("user_id"):
            metadata["user_id"] = inputs["user_id"]

        # 存储 inherit 资源继承配置（供管道引擎读取）
        # schema 已平铺：兼容扁平的 inherit_from/inherit_mode 与旧式 inherit 对象
        inherit_config = inputs.get("inherit")
        if not isinstance(inherit_config, dict) and (
            inputs.get("inherit_from") is not None or inputs.get("inherit_mode") is not None
        ):
            inherit_config = {
                "from": inputs.get("inherit_from", ""),
                "mode": inputs.get("inherit_mode", ""),
            }
        if isinstance(inherit_config, dict) and inherit_config.get("from"):
            metadata["inherit"] = inherit_config
            # mode 可能是 "pipe" 字符串，也可能是包含 "pipe" 的列表
            _mode = inherit_config.get("mode", "")
            _is_pipe = _mode == "pipe" or (isinstance(_mode, (list, tuple)) and "pipe" in _mode)
            if _is_pipe:
                metadata["inherit_pipe_from"] = inherit_config["from"]

        return metadata

    def _validate_target_agent(
        self,
        target_id: str,
        parent_agent_level: int,
    ) -> tuple[bool, str, str]:
        """校验目标 Agent 是否存在且级别匹配。"""
        agent_level_str = ""
        agent_level = 0
        # 目标 Agent 是否启用（is_active）。两条查找路径都要拿到它，
        # 用于在级别校验后统一拦截派发给已禁用 Agent 的任务。
        is_active = True

        agent_config = self._get_agent_config_from_registry(target_id)

        if agent_config is not None:
            level_value = safe_enum_value(agent_config.level)
            level_map = {"L1": 1, "L2": 2, "L3": 3}
            agent_level = level_map.get(level_value, 0)
            agent_level_str = level_value
            is_active = getattr(agent_config, "is_active", True)
        else:
            logger.warning(
                "[TaskSubmit] Agent '%s' 未在 registry 中找到，回退到磁盘文件查找",
                target_id,
            )
            found, agent_level_str, agent_level, is_active = self._lookup_agent_from_disk(target_id)
            if not found:
                return (
                    False,
                    f"目标 Agent '{target_id}' 不存在。"
                    f"请检查 target_id 是否正确。如果系统提供了 Agent 映射表，请使用映射表中的 Agent ID。",
                    "TARGET_AGENT_NOT_FOUND",
                )

        if agent_level == 1:
            return (
                False,
                f"不能将任务提交给 L1 Agent（'{target_id}'）。"
                f"L1 是主调度层，只负责接收用户请求和派发任务，不执行具体工作。"
                f"请选择 L2 编排层或 L3 执行层的 Agent。",
                "TARGET_AGENT_IS_L1",
            )

        if agent_level > 0 and agent_level <= parent_agent_level:
            return (
                False,
                f"目标 Agent '{target_id}' 的级别为 {agent_level_str}，"
                f"不能作为 L{parent_agent_level} Agent 的下级执行者。"
                f"任务委托应向下流动：L1→L2→L3，请选择级别更低（L{parent_agent_level + 1}+）的 Agent。",
                "TARGET_AGENT_LEVEL_INVALID",
            )

        if not is_active:
            return (
                False,
                f"目标 Agent '{target_id}' 已禁用（is_active=false），不能作为任务执行者。"
                f"请使用映射表中已启用的 Agent ID。",
                "TARGET_AGENT_INACTIVE",
            )

        return (True, "", "")

    def _get_agent_config_from_registry(self, target_id: str) -> Any | None:
        """从 agent_registry 查找 Agent 配置。"""
        try:
            provider = _get_service_provider()
            agent_registry = provider.get("agent_registry")
            if agent_registry is not None:
                return agent_registry.get(target_id)
        except Exception as exc:
            logger.warning(
                "[_get_agent_config_from_registry] 加载 agent_config 失败 (target_id=%s): %s",
                target_id,
                exc,
            )
        return None

    @staticmethod
    def _lookup_agent_from_disk(target_id: str) -> tuple[bool, str, int, bool]:
        """从磁盘 YAML 文件查找 Agent 配置（回退方案）。"""
        from pathlib import Path  # noqa: PLC0415

        import yaml  # noqa: PLC0415

        _project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        config_dir = _project_root / "config" / "agents"

        yaml_path = None
        for p in config_dir.rglob(f"{target_id}.yaml"):
            yaml_path = p
            break

        if not yaml_path or not yaml_path.exists():
            for p in config_dir.rglob("*.yaml"):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if data.get("config_id", "") == target_id:
                        yaml_path = p
                        break
                except Exception:
                    continue

        if not yaml_path or not yaml_path.exists():
            return (False, "", 0, True)

        try:
            with open(yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            return (False, "", 0, True)

        agent_level_str = config.get("level", "")
        level_map = {"L1": 1, "L2": 2, "L3": 3}
        agent_level = level_map.get(agent_level_str, 0)
        is_active = config.get("is_active", True)
        return (True, agent_level_str, agent_level, is_active)
