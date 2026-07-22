"""任务提交工具"""

import logging
import os
from pathlib import Path
from typing import Any

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)

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

    通过 MetricLoader 从 config/evaluation_metrics/ 加载。
    用于在提交期校验 LLM 传入的 acceptance_criteria key 是否为真实存在的指标 ID，
    避免「把 pass_threshold 等 value 子字段误填为指标 ID」导致评估期反复
    METRIC_NOT_FOUND。

    Returns:
        合法指标 ID 集合；加载失败时返回 None，表示跳过校验（fail-open，
        不阻断正常提交）。
    """
    try:
        from evaluation.loader import MetricLoader  # noqa: PLC0415

        loader = MetricLoader()
        if not loader.metrics:
            loader.load_all()
        return set(loader.metrics.keys())
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
        from tasks.service_access import get_task_service  # noqa: PLC0415

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
                    "goal": {
                        "type": "object",
                        "description": "任务目标（必填）",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "任务标题（必填），简短明确",
                            },
                            "description": {
                                "type": "string",
                                "maxLength": 2000,
                                "description": (
                                    "任务描述（上限2000字符）。只写目标和背景，"
                                    "禁止写执行步骤、工具选择、流程顺序。"
                                    "引用文件只写路径（如 docs/report.md），"
                                    "禁止复制文件内容进来，让下级 Agent 用 file_read 自行读取。"
                                    "正确：'实现用户登录API，参考 docs/auth_spec.md'"
                                    "错误：'先用file_write创建login.py，再...'"
                                ),
                            },
                        },
                        "required": ["title"],
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
                            "目标项目路径。指定任务需要操作（读取或修改）的项目目录，"
                            "系统会在工作空间中基于该目录创建隔离的 worktree 副本进行操作。"
                            "**重要**：当任务需要对某个特定文件夹进行读取或修改时，"
                            "必须设置此参数为该目标文件夹的路径，否则任务将无法定位到正确的目标目录。"
                            "容器任务：传入目标项目目录时，系统会复制到隔离空间中操作；"
                            "不设置则创建空的工作空间。"
                            "非容器任务（子任务）：不设置时系统自动基于父容器空间创建 worktree；"
                            "传入目标项目路径时基于该项目创建 worktree。"
                            "如需直接在目标项目目录工作（不隔离），设置 isolation_level 为 non_isolated。"
                        ),
                    },
                    "isolation_level": {
                        "type": "string",
                        "enum": ["non_isolated", "isolated"],
                        "description": (
                            "隔离级别（可选，默认使用系统配置）。"
                            "non_isolated：非隔离，直接在原空间工作。适合在已有项目上直接修改。"
                            "isolated：隔离，在隔离的工作空间中工作，不影响原项目。"
                        ),
                    },
                    "inherit": {
                        "type": "object",
                        "description": (
                            "资源继承配置：从指定源任务继承资源，新任务在源任务的成果上继续工作。"
                            "不传此参数 = 创建全新任务（默认行为）。\n"
                            '继承内容由 mode 决定，mode 可传单个字符串（如 "pipe"），'
                            '也可传列表（如 ["pipe", "workspace"]）同时继承管道和工作空间。\n'
                            "如何选择 mode（什么时候用哪种继承）：\n"
                            "- 不需要任何继承 → 不传 inherit。\n"
                            '- 只想接着对话（改了目标/验收标准，但上下文还要用）→ mode="pipe"。'
                            "继承源任务的对话管道（消息历史），新任务从上次对话上下文继续，但不复用工作空间。\n"
                            '- 只想复用文件（换了实现方案，但已产出的代码/文档要留着）→ mode="workspace"。'
                            "继承源任务的工作空间（文件目录），新任务能看到并继续修改已有文件，但对话历史清空。\n"
                            '- 既想接着对话、又想复用文件（最常见的延续场景）→ mode=["pipe", "workspace"]。'
                            "同时继承对话历史和工作空间，新任务等同于在源任务现场继续。\n"
                            "from 指定源任务 ID。"
                        ),
                        "properties": {
                            "from": {
                                "type": "string",
                                "description": "源任务 ID（被继承的任务）",
                            },
                            "mode": {
                                "description": (
                                    '继承模式：传 "pipe" 或 "workspace" 继承单一资源；'
                                    '传 ["pipe", "workspace"] 同时继承对话管道和工作空间。'
                                ),
                                "oneOf": [
                                    {
                                        "type": "string",
                                        "enum": ["pipe", "workspace"],
                                        "description": (
                                            "单值继承："
                                            "pipe = 继承对话管道（消息历史），适合改了目标但保留上下文；"
                                            "workspace = 继承工作空间（文件目录），适合换方案但保留文件。"
                                        ),
                                    },
                                    {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "enum": ["pipe", "workspace"],
                                        },
                                        "description": (
                                            "多值继承：同时继承多种资源，"
                                            '如 ["pipe", "workspace"] 既继承对话历史又继承文件。'
                                        ),
                                    },
                                ],
                            },
                        },
                        "required": ["from", "mode"],
                    },
                },
                "required": ["goal"],
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
                    "max_visible_level": 1,  # L2/L3 子任务自动继承父工作空间
                },
                "isolation_level": {
                    "max_visible_level": 1,  # L2/L3 子任务自动继承隔离级别
                },
            },
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911,PLR0912,PLR0915
        """执行任务提交。"""
        import time as _time  # noqa: PLC0415

        _t0 = _time.monotonic()
        task_scope = inputs.get("task_scope", "non_container")
        goal = inputs.get("goal")
        if isinstance(goal, str):
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

        # ── 子任务：清除自动注入的 workspace ──
        # ParamInjectPlugin 会将当前管道的 workspace 注入所有工具调用，
        # 但子任务的工作空间由父任务链自动解析，不能使用注入值，否则路径双重嵌套。
        if parent_task_id and inputs.get("workspace"):
            logger.info(
                "[TaskSubmit] 子任务清除自动注入的 workspace | parent_task_id=%s | workspace=%s",
                parent_task_id,
                inputs["workspace"],
            )
            del inputs["workspace"]

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
        _inherit_config = inputs.get("inherit")
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

        # ── 5.5 解析子任务隔离模式（继承规则）──
        # 源空间（workspace）沿父任务链解析，与隔离模式无关；此处只决定隔离模式：
        # - 父任务是 container（容器任务）→ 直接子任务不继承，使用默认隔离（isolated）模式，
        #   清除 LLM 传入值。
        # - 父任务非 container（含容器孙任务、非容器根任务的子任务）→ 子任务继承直接父任务的
        #   isolation_level，一律忽略 LLM 显式传入的值（隔离模式为系统控制项）。
        # 最终「实际 ws」由隔离模式决定：non_isolated → 源空间本身；isolated → 源空间的 worktree。
        if parent_task_id:
            _parent_task = None
            try:
                _parent_task = task_service.get_task(parent_task_id)
            except Exception:
                pass
            _parent_meta = (_parent_task.metadata or {}) if _parent_task else {}
            _parent_scope = _parent_meta.get("task_scope", "non_container")

            if _parent_scope == "container":
                # 容器直接子任务：清除 LLM 值 → 走默认隔离模式
                if inputs.get("isolation_level"):
                    logger.info(
                        "[TaskSubmit] 容器直接子任务不继承 isolation_level，清除 LLM 值 | "
                        "parent_task_id=%s | isolation_level=%s",
                        parent_task_id,
                        inputs["isolation_level"],
                    )
                    del inputs["isolation_level"]
            else:
                # 非容器父任务的子任务：继承父任务 isolation_level，忽略 LLM 值
                _parent_iso = _parent_meta.get("isolation_level")
                if inputs.get("isolation_level"):
                    logger.info(
                        "[TaskSubmit] 非容器子任务忽略 LLM 传入的 isolation_level，改为继承父任务 | "
                        "parent_task_id=%s | llm_value=%s | inherited=%s",
                        parent_task_id,
                        inputs["isolation_level"],
                        _parent_iso,
                    )
                    del inputs["isolation_level"]
                if _parent_iso:
                    inputs["isolation_level"] = _parent_iso

        # ── 6. 创建任务 ──
        raw_priority = inputs.get("priority", 5)
        try:
            from tasks.types import TaskPriority as TP  # noqa: N817,PLC0415

            TP(raw_priority)
        except (ValueError, AttributeError):
            raw_priority = 5

        try:
            child_agent_level = min(parent_agent_level + 1, 3)
            from agents.types import AgentLevel  # noqa: PLC0415

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

        # ── 7. 提交到后台执行器 ──
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        task_worker = get_service_provider().get("task_worker")
        if not task_worker:
            await task_service.hard_delete(task.id)
            return create_failure_result(
                error="后台执行器不可用，任务提交失败",
                error_code="SUBMIT_FAILED",
            )

        is_root = True
        if parent_task_id and task_service:
            try:
                parent_task = task_service.get_task(parent_task_id)
                if parent_task and parent_task.metadata:
                    parent_scope = parent_task.metadata.get("task_scope", "non_container")
                    if parent_scope != "container":
                        is_root = False
            except Exception:
                pass

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
                exec_storage = get_service_provider().get("execution_record_storage")
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

        if not task_worker.submit_task(task_data):
            await task_service.hard_delete(task.id)
            return create_failure_result(
                error="后台执行器未启动，任务提交失败",
                error_code="SUBMIT_FAILED",
            )

        _t_submit = _time.monotonic()
        logger.info(
            "[TaskSubmit] PERF | submit_task=%.1fms | total=%.1fms",
            (_t_submit - _t_create) * 1000,
            (_t_submit - _t0) * 1000,
        )

        logger.info("[TaskSubmit] 任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        try:
            _user_id = inputs.get("user_id", "") or ""
            from channels.websocket.ws_handler import ws_interaction_notifier as _ws_notifier  # noqa: PLC0415

            if _ws_notifier and _user_id and hasattr(_ws_notifier, "send_to_user"):
                await _ws_notifier.send_to_user(
                    _user_id,
                    {
                        "type": "task_status_update",
                        "data": {
                            "task_id": task.id,
                            "old_status": "",
                            "new_status": "pending",
                            "current_phase": "prepare",
                        },
                    },
                )
                logger.info(
                    "[TaskSubmit] task_status_update 已广播 | task_id=%s | user=%s | status=pending",
                    task.id,
                    _user_id[:12],
                )
            else:
                logger.warning(
                    "[TaskSubmit] 跳过广播 | notifier=%s user=%s",
                    bool(_ws_notifier),
                    _user_id[:12] if _user_id else "(empty)",
                )
        except Exception as _ws_exc:
            logger.warning(
                "[TaskSubmit] task_status_update 广播失败 | task_id=%s | error=%s",
                task.id,
                _ws_exc,
            )

        _t_ws = _time.monotonic()
        logger.info(
            "[TaskSubmit] PERF | ws_broadcast=%.1fms | total=%.1fms", (_t_ws - _t_submit) * 1000, (_t_ws - _t0) * 1000
        )

        result_data: dict[str, Any] = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "target_id": target_id,
            "message": (
                f"任务 [{task.title}]（ID: {task.id}）已提交，目标执行者：{target_id}，状态：异步执行中。"
                "该任务需要一定时间完成。"
                "子任务完成后系统会自动通知你并恢复执行。"
                "在此期间请不要再调用任何工具（包括 task_manage），直接输出纯文本等待即可。"
            ),
        }

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

    async def _execute_long_term(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0912,PLR0915
        """处理容器任务提交。"""
        goal = inputs.get("goal")
        parent_agent_level = inputs.get("parent_agent_level")

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

        try:
            _user_id = inputs.get("user_id", "") or ""
            from channels.websocket.ws_handler import ws_interaction_notifier as _ws_notifier  # noqa: PLC0415

            if _ws_notifier and _user_id and hasattr(_ws_notifier, "send_to_user"):
                await _ws_notifier.send_to_user(
                    _user_id,
                    {
                        "type": "task_status_update",
                        "data": {
                            "task_id": task.id,
                            "old_status": "",
                            "new_status": "pending",
                            "current_phase": "prepare",
                        },
                    },
                )
                logger.info(
                    "[TaskSubmit] 容器 task_status_update 已广播 | task_id=%s | user=%s | status=pending",
                    task.id,
                    _user_id[:12],
                )
            else:
                logger.warning(
                    "[TaskSubmit] 容器跳过广播 | notifier=%s user=%s",
                    bool(_ws_notifier),
                    _user_id[:12] if _user_id else "(empty)",
                )
        except Exception as _ws_exc:
            logger.warning(
                "[TaskSubmit] 容器 task_status_update 广播失败 | task_id=%s | error=%s",
                task.id,
                _ws_exc,
            )

        from isolation.workspace import resolve_container_workspace_path  # noqa: PLC0415

        container_workspace_path = resolve_container_workspace_path(
            inputs.get("workspace"),
            task.id,
            isolation_mode=inputs.get("isolation_level"),
        )

        # ── 同步初始化容器工作空间 ──
        # 与非容器任务一致：submit 返回前必须完成工作空间创建。
        _container_task_data = {"isolation_mode": inputs.get("isolation_level", "")}
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

        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        task_worker = get_service_provider().get("task_worker")
        if task_worker:
            task_worker.submit_task(
                {
                    "task_id": task.id,
                    "target_type": None,
                    "target_id": None,
                    "user_input": goal.get("title", ""),
                    "description": description or goal.get("description", ""),
                    "acceptance_criteria": {},
                    "workspace": inputs.get("workspace"),
                    "parent_task_id": None,
                    "is_root": True,
                }
            )

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
        import asyncio  # noqa: PLC0415

        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        lifecycle = provider.get("workspace_lifecycle_manager") if provider else None
        if lifecycle is None:
            return task, "工作空间管理器不可用，任务提交失败"

        # 注入 isolation_mode（lifecycle 内部依赖此字段决策 worktree/non_isolated 模式）
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
        if inputs.get("workspace") and (not inputs.get("parent_task_id") or inputs.get("inherit_workspace_from")):
            metadata["workspace"] = inputs["workspace"]
        if inputs.get("max_retries"):
            metadata["max_retries"] = inputs["max_retries"]
        if inputs.get("task_scope"):
            metadata["task_scope"] = inputs["task_scope"]
        if inputs.get("isolation_level"):
            metadata["isolation_level"] = inputs["isolation_level"]

        # 存储执行者信息
        if inputs.get("target_id"):
            metadata["target_id"] = inputs["target_id"]

        if inputs.get("user_id"):
            metadata["user_id"] = inputs["user_id"]

        # 存储 inherit 资源继承配置（供管道引擎读取）
        inherit_config = inputs.get("inherit")
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
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
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
