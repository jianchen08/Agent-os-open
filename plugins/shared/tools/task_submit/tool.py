"""任务提交工具"""

import inspect
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
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
# - task_worker：已退役（0.1 执行驱动）；任务执行经 chat.send_message
#   创建管道（GAP-1 统一：task = pipeline，run 终态回写任务状态）。
# - agent_registry：由 agent_manager 插件提供——
#   server.py on_load 经 tool-executor（显式 plugin_id=agent_manager）注入
#   _agent_registry_lookup（async agent_id -> config dict | None）；未注入/
#   查询失败 → None，调用方回退磁盘 rglob（行为不劣化）。
# - workspace_lifecycle_manager / execution_record_storage：
#   sidecar 无等价实例 → None（调用方已有降级守卫/文档化降级）。
# 测试可 monkeypatch 模块级 _get_service_provider / set_agent_registry_lookup。

class _ServiceProviderShim:
    """0.2 服务提供者适配：get(key) 返回 0.2 等价或 None（文档化降级）。"""

    def get(self, key: str) -> Any:
        # agent_registry 经 set_agent_registry_lookup 注入的查询钩子承接
        # （_get_agent_config_from_registry 直取 _agent_registry_lookup，
        # 此处不再返回 0.1 式同步 registry 对象）。
        # workspace_lifecycle_manager / execution_record_storage
        # 0.2 sidecar 无等价实例：调用方已有降级守卫。
        return None


def _get_service_provider() -> Any:
    """获取 0.2 服务提供者 shim（sidecar 模式下的服务解析入口）。"""
    return _ServiceProviderShim()


# ── agent_registry 查询钩子（agent_manager 服务注入，P4 收敛）──
_agent_registry_lookup: Any = None


def set_agent_registry_lookup(lookup: Any) -> None:
    """注入 agent 配置查询钩子（server.py on_load 调用）。

    约定签名：``async lookup(agent_id: str) -> dict | None``（返回 agent_manager
    agent.get 服务解析后的 yaml dict；未命中/服务不可用 → None → 磁盘回退）。
    """
    global _agent_registry_lookup  # noqa: PLW0603
    _agent_registry_lookup = lookup
    logger.info("[TaskSubmit] agent_registry 查询钩子已注入（agent_manager 服务）")

# ── GAP-1：chat.send_message 派发器（server.py on_load 注入）──
#
# 任务执行驱动：提交成功后经内核 chat capability 的 send_message（create 分支，
# 引擎生成 pipeline_id）创建任务执行管道——state 出生即带 task.*/lineage.* 扁平
# 键（血缘方案本插件自持）、execution_context 透传。sidecar 进程内模块级注入（能力句柄
# 懒解析在协程内完成）；未注入（capability 缺席/测试）时提交仍落库但话术诚实
# （不声称"异步执行中"），结果携带 warning。
_chat_sender: Any = None


def set_chat_sender(sender: Any) -> None:
    """注入 chat.send_message 派发器（server.py on_load 调用）。

    约定签名：``async sender(params: dict) -> dict``，params 即
    chat.send_message 的入参（create/message/user_id/state/
    execution_context/background）；成功返回含 ``pipeline_id`` 的响应，
    失败抛异常（由调用方记录并降级为 warning 话术）。
    """
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender
    logger.info("[TaskSubmit] chat.send_message 派发器已注入")


def _get_chat_sender() -> Any:
    """获取 chat.send_message 派发器（None = 未注入，测试可 monkeypatch）。"""
    return _chat_sender


def _now_iso() -> str:
    """当前时间 ISO 串（任务登记时间戳）。"""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat()


def _short_id(full_id: str) -> str:
    """全 id → 短 id（LLM 展示用，12 位；短 id 原样返回）。"""
    if not full_id:
        return full_id
    return full_id[:12]


async def _resolve_short_id(rows: list[dict[str, Any]] | None, candidate: str) -> str:
    """短 id → 全 id（state 聚合前缀唯一解析；精确命中原样；歧义返回 AMBIGUOUS:<id>）。

    规则：
    1. 精确命中（pipeline_id / task.owned.<id>）→ 原样；
    2. 候选 ≤12 位 → 前缀匹配（pipeline_id + task.owned.<id>）；
       唯一命中返回全 id，多命中返回 `AMBIGUOUS:<candidate>`，无命中原样。
    """
    if not candidate:
        return candidate
    for row in (rows or []):
        if str(row.get("pipeline_id") or "") == candidate:
            return candidate
        if any(str(k).startswith(f"task.owned.{candidate}.") for k in row.keys()):
            return candidate
    if len(candidate) <= 12:
        hits: list[str] = []
        for row in (rows or []):
            pid = str(row.get("pipeline_id") or "")
            if pid.startswith(candidate):
                hits.append(pid)
            for k in row.keys():
                ks = str(k)
                if ks.startswith("task.owned."):
                    owned_id = ks[len("task.owned."):].split(".", 1)[0]
                    if owned_id.startswith(candidate) and owned_id not in hits:
                        hits.append(owned_id)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return f"AMBIGUOUS:{candidate}"
    return candidate


# ── GAP-1 统一：state 聚合读取器（server.py on_load 注入，依赖校验等读面）──
# 约定签名：``() -> list[dict]``（sync 或 async，返回管道 state 聚合行，
# 行为扁平点号键如 {"pipeline_id": ..., "task.status": ...}）。None = 未注入
# （依赖校验 fail-closed）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader


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


def _metrics_config_path() -> Path:
    """评估指标配置路径（容器根 config/evaluation/evaluation_metrics.yaml）。

    与 task_evaluate 同款读取；指标定义的唯一加载点（提交期校验与
    派发指令详情展开共用）。
    """
    return Path(__file__).resolve().parents[4] / "config" / "evaluation" / "evaluation_metrics.yaml"


def _load_metric_definitions() -> dict[str, dict[str, Any]]:
    """加载指标定义表（name → 定义）；缺文件/坏格式 → 空表（fail-open）。"""
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(_metrics_config_path().read_text(encoding="utf-8")) or {}
        return {
            str(m["name"]): m
            for m in data.get("metrics", []) or []
            if isinstance(m, dict) and m.get("name")
        }
    except Exception as exc:
        logger.warning(
            "[TaskSubmit] 评估指标定义加载失败: %s",
            exc,
        )
        return {}


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
    valid = set(_load_metric_definitions().keys())
    # 空集合视作加载失败（fail-open，不阻断正常提交）
    return valid or None


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


# ── 派发指令构建（0.1 _build_full_task_input 的 0.2 移植）──
#
# 0.1 在 task_worker 侧把描述/验收标准/工作空间提示/路径规则/待办工作法
# 拼成完整输入注入下级。0.2 提交即派发（chat.send_message create 分支），
# 该职责落到 task_submit 派发消息。逐段对照：
# - 描述：0.1「\n\n详细描述：」→ 0.2 派发消息「\n任务描述：」（保留现形）；
# - 重试纠正信息：0.1 metadata.retry_message → 0.2 由 task_manage continue
#   注入消息承载（无重复构建）；
# - 评估指标详情 / 工作空间模式提示 / 路径使用规则 / 进度跟踪工作法：
#   0.2 此前缺失，本次按 0.2 yaml/拓扑口径移植。

_EVALUATION_PROMPT_HEADER = "评估指标详情（你的产出将被以下标准评估）："


def _build_evaluation_criteria_prompt(acceptance_criteria: dict[str, Any]) -> str:
    """按指标定义展开验收标准为可读的评估说明文本（0.1 同职移植）。

    0.2 指标定义（evaluation_metrics.yaml）字段与 0.1 MetricLoader 模型不同：
    只有 name/description/evaluator_type/input_schema，没有 expect/is_red_line
    等判定字段——按 0.2 字段展开（说明 + 评估参数），判定逻辑归 task_evaluate。

    Returns:
        格式化后的评估指标说明文本；无验收标准/定义缺失/加载失败 → 空串。
    """
    if not acceptance_criteria or not isinstance(acceptance_criteria, dict):
        return ""
    definitions = _load_metric_definitions()
    if not definitions:
        return ""

    parts: list[str] = []
    for metric_id, config in acceptance_criteria.items():
        definition = definitions.get(metric_id)
        if not definition:
            continue
        lines: list[str] = []
        lines.append(f"- {metric_id}：{definition.get('description') or '(无说明)'}")
        if config and isinstance(config, dict):
            input_params = config.get("input_params")
            if input_params:
                lines.append(f"  评估参数：{json.dumps(input_params, ensure_ascii=False)}")
        parts.append("\n".join(lines))
    if not parts:
        return ""
    return f"\n\n{_EVALUATION_PROMPT_HEADER}\n\n" + "\n".join(parts)


def _build_workspace_guidance(ec: dict[str, Any]) -> str:
    """按 execution_context 工作空间声明生成场景提示与路径规则（0.1 同职移植）。

    - 显式 workspace：worktree=源项目隔离副本（改完自动合并回源）/ plain=直接
      操作目标目录；0.1 的 shared 态（父任务空间）在 0.2 由子任务继承表达，
      不单独提示；
    - 无显式 workspace：任务在默认隔离目录执行（工作空间根/{task_id}）。
    系统自动管理路径，下级只用相对路径。
    """
    ws_spec = ec.get("workspace") if isinstance(ec, dict) else None
    if not isinstance(ws_spec, dict):
        return ""
    mode = ws_spec.get("mode") or "plain"
    if ws_spec.get("explicit"):
        scene = (
            "你在目标项目的隔离副本中执行任务。使用相对路径，修改不影响原始项目；"
            "可运行 pytest/mypy/lint。评估通过后系统自动合并回目标项目"
            if mode == "worktree"
            else "你直接在目标目录中执行任务。使用相对路径。"
        )
    else:
        scene = "你在任务专属的隔离工作目录中执行任务。使用相对路径。"
    return (
        f"\n\n工作空间模式提示：{scene}"
        "\n\n路径使用规则（重要）："
        "\n- 所有文件操作使用相对路径即可，系统会自动锚定到工作目录"
        '\n- 示例：file_write(path="docs/report.md")'
    )


def _build_task_progress_method() -> str:
    """待办工作法提示（0.1 同职移植）：把执行过程展开成可见待办清单推进。"""
    return (
        "\n\n进度跟踪工作法（把你的执行过程展开成可见的待办，方便跟进）："
        "\n1. 把你 system_prompt 执行流程的每一步，按顺序展开成 `- [ ]` 待办清单"
        "\n2. 按该顺序推进，每完成一步标记 `- [x] ✅`"
        "\n3. 全部完成后调用 task_evaluate 提交评估"
        "\n说明：本条只规定「用待办清单推进」这一形式。任务描述里的具体要求"
        "（如约束、产出路径、评估标准）是你的硬指标，"
        "system_prompt 里的专业流程（如先加载技能、TDD 循环）是你的必经步骤，"
        "二者都不得因本待办工作法而跳过或简化。"
    )


# 任务提交工具的 OpenAI Function Calling schema 字面量（纯声明数据，
# 与 get_tool_definition 拆离便于 review/diff 与外部对账）。
_TASK_SUBMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_type": {
            "type": "string",
            "enum": ["agent"],
            "description": "目标类型，固定为 agent（必填）",
        },
        "target_id": {
            "type": "string",
            "minLength": 1,
            "description": "目标 Agent ID（必填）。如果系统提供了 Agent 映射表，直接使用映射表中的 ID，不要用 resource_search 搜索",
        },
        "goal_title": {
            "type": "string",
            "minLength": 1,
            "description": "任务标题（必填），简短明确",
        },
        "goal_description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "description": (
                "任务描述（必填，1-2000 字符）。只写目标和背景，"
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
        "project_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": (
                "挂靠项目 ID（可选）。指定后任务在项目文件夹下执行"
                "（默认 worktree 从项目仓库分叉）；缺省为独立任务。"
                "项目经 projects 域 API 创建（= 真实文件夹 + 登记）"
            ),
        },
        "parent_task_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": (
                "父任务 ID（task=pipeline 统一后即引擎管道身份，12 位 hex）。"
                "创建任务链子任务时指定此参数关联父任务（复盘/分层链）。"
            ),
        },
        "workspace": {
            "type": "string",
            "description": (
                "目标项目路径。指定任务需要操作（读取或修改）的项目目录。"
                "**重要**：当任务需要对某个特定文件夹进行读取或修改时，"
                "必须设置此参数为该目标文件夹的路径，否则任务将无法定位到正确的目标目录。"
                "可用范围（按任务类型）：根任务可填；普通子任务不可填（继承父任务）。"
                "工作空间拓扑由 workspace_mode 决定（worktree=在目标项目上建隔离副本；"
                "plain=直接操作目标目录）；执行环境隔离由 isolation_level 决定，两者独立。"
            ),
        },
        "workspace_mode": {
            "type": "string",
            "enum": ["worktree", "plain"],
            "description": (
                "工作空间拓扑（根任务可选，默认 worktree）。"
                "worktree：在目标项目上创建 git worktree 隔离操作，不影响原项目（默认）。"
                "plain：直接在目标目录工作，不建 worktree、不切分支。"
                "与 isolation_level（执行环境容器/宿主）相互独立。"
                "普通子任务不可选（继承父任务空间）。"
            ),
        },
        "isolation_level": {
            "type": "string",
            "enum": ["non_isolated", "isolated"],
            "description": (
                "执行环境隔离级别（根任务可选，默认使用系统配置）。"
                "non_isolated：非隔离，直接在宿主环境执行。"
                "isolated：隔离，在容器执行环境中工作。"
                "只决定执行环境，不决定工作空间拓扑（拓扑由 workspace_mode 决定）。"
                "普通子任务不可选（继承父任务）。"
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
    "required": ["goal_title", "goal_description", "target_type", "target_id"],
}


def _parse_inherit_modes(inp: dict[str, Any]) -> set[str]:
    """解析 inherit_mode / inherit.mode 为集合（扁平字段与旧式嵌套对象同口径）。"""
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
        """获取工具定义（标准 OpenAI Function Calling 格式）；schema 见模块级 _TASK_SUBMIT_INPUT_SCHEMA。"""
        return Tool(
            name="task_submit",
            description="任务提交工具。将任务提交给指定的 Agent 执行，配置验收标准确保结果可验证。",
            input_schema=_TASK_SUBMIT_INPUT_SCHEMA,
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
                "project_id": {
                    "max_visible_level": 1,  # 项目挂靠由 L1 组织（L2/L3 子任务继承项目）
                },
                "parent_task_id": {
                    "max_visible_level": 1,  # L2/L3 系统自动注入，不应手动指定
                },
                "workspace": {
                    "max_visible_level": 3,  # 任务工作空间由 agent 直接选（普通子任务执行期拒绝）
                },
                "workspace_mode": {
                    "max_visible_level": 3,  # worktree/plain 由 agent 直接选
                },
                "isolation_level": {
                    "max_visible_level": 3,  # 执行环境由 agent 直接选
                },
            },
        )

    @staticmethod
    def _parse_goal_input(inputs: dict[str, Any]) -> dict[str, Any] | None:
        """goal 字段解析（schema 已平铺为 goal_title/goal_description）。

        优先读扁平字段；同时兼容旧的 goal 对象（历史调用方/未刷新 schema 的 LLM）
        以及 goal 作为纯文本标题的容错包装。异常形态归一为 None 并告警。
        """
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
        return goal

    def _gate_base_submission(
        self,
        inputs: dict[str, Any],
        goal: dict[str, Any] | None,
        parent_agent_level: Any,
    ) -> tuple[dict[str, Any], str, ToolExecutionResult | None]:
        """基础字段与归属闸门：标题/描述必填 → parent_task_id 归属 → 描述长度上限。

        返回 (验证通过的 goal, 规范化后的 description, 失败结果)；失败结果非 None
        时调用方短路返回，此时前两个值无意义（恒为空占位）。
        """
        # ── 1. 基础参数验证 ──
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 为空")
            return {}, "", create_failure_result(
                error="必须提供 goal（含 title 字段）",
                error_code="MISSING_GOAL",
            )

        # ── 1.2 任务描述非空：派发给下级 Agent 的任务只有标题没有描述 =
        # 下级无目标上下文，标题承载不了执行语义，一律拒绝。
        # （priority/max_retries 参数已退役——执行层零消费者，见 ADR
        # 2026-08-24-task-submit-param-diet；显式传入按未知参数忽略。）
        description = _normalize_description(goal.get("description", ""))
        if not description.strip():
            logger.warning("[TaskSubmit] goal.description 缺失或空白 | title=%s", goal.get("title"))
            return {}, "", create_failure_result(
                error="必须提供任务描述（goal_description，1-2000 字符）",
                error_code="MISSING_DESCRIPTION",
            )

        parent_task_id = inputs.get("parent_task_id")
        # P0-3 纵深防御：校验 parent_task_id 归属，防 L2/L3 伪造他人父任务越权挂载
        # （继承他人管道/工作空间/上下文）。合法链：父任务必须由更高层级提交。
        own_ok, own_err = self._check_parent_ownership(parent_agent_level, parent_task_id)
        if not own_ok:
            logger.warning(
                "[TaskSubmit] parent_task_id 归属校验失败 | parent=%s | L%d | reason=%s",
                parent_task_id,
                parent_agent_level,
                own_err,
            )
            return {}, "", create_failure_result(
                error=own_err or "parent_task_id 归属校验失败",
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
        max_desc_len = 2000
        if len(description) > max_desc_len:
            logger.warning(
                "[TaskSubmit] 描述超长拒绝 | len=%d | max=%d | preview=%.100s",
                len(description),
                max_desc_len,
                description[:100],
            )
            return {}, "", create_failure_result(
                error=(
                    f"任务描述过长（{len(description)}字符，上限{max_desc_len}字符）。"
                    "请精简描述，只写目标和文件路径，让下级 Agent 自行 file_read 文件内容。"
                ),
                error_code="DESCRIPTION_TOO_LONG",
            )
        return goal, description, None

    @staticmethod
    def _normalize_acceptance_criteria(
        acceptance_criteria: Any,
    ) -> tuple[dict[str, Any], ToolExecutionResult | None]:
        """验收标准归一化：类型重置 + 指标 ID 合法性校验。

        全部 key 无效 → 拒绝提交并引导 LLM 使用正确指标 ID；
        部分无效 → 剔除无效项后继续（降级，不阻断）。
        返回 (归一化后的 criteria, 失败结果)。
        """
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
        original_keys = list(acceptance_criteria.keys())
        if original_keys:
            normalized, invalid_ids = _validate_metric_ids(acceptance_criteria)
            if invalid_ids and not normalized:
                valid_ids = _get_valid_metric_ids() or set()
                valid_list = ", ".join(sorted(valid_ids)) if valid_ids else "(指标加载失败)"
                logger.warning(
                    "[TaskSubmit] acceptance_criteria 全部 key 无效，拒绝提交 | invalid=%s | valid=%s",
                    invalid_ids,
                    sorted(valid_ids),
                )
                return {}, create_failure_result(
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
                    list(normalized.keys()),
                )
            return normalized, None
        return acceptance_criteria, None

    @staticmethod
    def _apply_parent_task_injection(
        parent_agent_level: Any,
        parent_task_id: str | None,
        inputs: dict[str, Any],
    ) -> tuple[str | None, ToolExecutionResult | None]:
        """L2/L3 层级闸门 + 注入回退：显式指定 parent_task_id 一律拦截；
        未指定时回退管道注入的 task_id 作为父任务。
        返回 (生效的 parent_task_id, 失败结果)。"""
        # ── L2/L3 层级校验：禁止显式指定 parent_task_id ──
        if parent_agent_level >= 2 and parent_task_id is not None:
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 parent_task_id=%s，已拦截",
                parent_agent_level,
                parent_task_id,
            )
            return None, create_failure_result(
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
        return parent_task_id, None

    async def _resolve_project_binding(
        self,
        inputs: dict[str, Any],
        parent_task_id: str | None,
        parent_agent_level: int,
        explicit_workspace_raw: str,
    ) -> tuple[str, ToolExecutionResult | None]:
        """项目挂靠解析（project_id）：L1 显式指定；L2/L3 沿父链由系统继承。

        项目 = 文件夹 + 登记（ADR 2026-08-27）：挂靠键 task.parent_project_id
        由 task_submit 唯一写入（state 行），每级子任务继承同值——深层任务
        仍归属项目（任务树分组/制品聚合按单跳键覆盖全深度）。

        校验序：L2/L3 显式指定拒绝 → 登记存在性 → 文件夹存在性 → 与 agent
        显式 workspace 冲突拒绝；全部通过后把 inputs["workspace"] 覆写为项目文件夹。
        返回 (project_id（空串 = 不挂靠）, 失败结果)。
        """
        project_id = str(inputs.get("project_id") or "")
        if project_id and parent_agent_level >= 2:
            # 防伪造：L2/L3 的项目归属由系统继承，显式指定一律拒绝
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 project_id=%s，已拦截（项目归属沿父链继承）",
                parent_agent_level,
                project_id,
            )
            return "", create_failure_result(
                error=(
                    f"L{parent_agent_level} Agent 不能显式指定 project_id——"
                    "子任务的项目归属由系统沿父链自动继承，直接提交子任务即可。"
                ),
                error_code="L2_CANNOT_SPECIFY_PROJECT_ID",
            )
        if not project_id and parent_task_id:
            project_id = await self._inherit_project_id(parent_task_id)
            if project_id:
                logger.info(
                    "[TaskSubmit] 子任务继承项目归属 | parent_task_id=%s | project_id=%s",
                    parent_task_id,
                    project_id,
                )
        if project_id:
            # 共享层自举（plugins/shared/ —— project_registry 所在）
            shared_root = str(Path(__file__).resolve().parents[2])
            if shared_root not in sys.path:
                sys.path.insert(0, shared_root)
            from project_registry import load_project_paths  # noqa: PLC0415

            project_path = str(load_project_paths().get(project_id) or "")
            if not project_path:
                logger.error(
                    "[TaskSubmit] project_id 不在登记中（可能已删除）| project_id=%s",
                    project_id,
                )
                return "", create_failure_result(
                    error=f"项目 {project_id} 不存在（登记中无此 id，可能已被删除）",
                    error_code="PROJECT_NOT_FOUND",
                )
            if not os.path.isdir(project_path):
                return "", create_failure_result(
                    error=f"项目文件夹不存在: {project_path}（项目可能已被删除，请检查项目登记）",
                    error_code="PROJECT_FOLDER_MISSING",
                )
            # 挂项目任务的 workspace 恒为项目文件夹（worktree 从它分叉）；
            # 显式给了不同路径 = 语义冲突，拒绝而非二选一猜测。
            if explicit_workspace_raw:
                norm = os.path.normpath
                path_a = norm(explicit_workspace_raw)
                path_b = norm(project_path)
                same = path_a.lower() == path_b.lower() if os.name == "nt" else path_a == path_b
                if not same:
                    return "", create_failure_result(
                        error=(
                            f"挂靠项目（{project_id}）的任务工作空间恒为项目文件夹 {project_path}，"
                            f"不能另指定 workspace={explicit_workspace_raw}。请去掉 workspace 参数重新提交。"
                        ),
                        error_code="PROJECT_WS_CONFLICT",
                    )
            inputs["workspace"] = project_path
        return project_id, None

    def _gate_subtask_param_inheritance(
        self,
        inputs: dict[str, Any],
        parent_task_id: str | None,
        explicit_workspace_raw: str,
    ) -> ToolExecutionResult | None:
        """子任务参数闸门：普通子任务只允许 inherit pipe；其余一律继承父任务。

        param_inject 已对 task_submit 跳过 workspace/isolation_level 注入，
        此处 inputs 中的值即为 agent 显式选择，按任务类型强制校验。
        workspace 用 agent 原始入参判定（项目解析注入的值不算显式指定）。
        """
        if not parent_task_id:
            return None
        inherit_source = _inherit_from_id_of(inputs)
        if inherit_source and "workspace" in _parse_inherit_modes(inputs):
            # inherit workspace：普通子任务只能继承管道，工作空间继承被拒绝
            logger.warning(
                "[TaskSubmit] 普通子任务 inherit workspace 被拒绝（只能继承管道）| "
                "parent_task_id=%s | inherit_from=%s",
                parent_task_id,
                inherit_source,
            )
            return create_failure_result(
                error=(
                    "普通子任务只能继承管道（inherit_mode=['pipe']，对话历史），"
                    "工作空间一律继承父任务，不能继承其它任务的工作空间。"
                ),
                error_code="SUBTASK_INHERITS_PARAMS",
            )
        if explicit_workspace_raw:
            logger.warning(
                "[TaskSubmit] 普通子任务显式指定 workspace 被拒绝（继承父任务）| "
                "parent_task_id=%s | value=%s",
                parent_task_id,
                explicit_workspace_raw,
            )
            return create_failure_result(
                error=(
                    "普通子任务继承父任务的工作空间与隔离配置，不能指定 workspace。"
                    "如需继承对话历史，请使用 inherit_from + inherit_mode=['pipe']。"
                ),
                error_code="SUBTASK_INHERITS_PARAMS",
            )
        for param in ("workspace_mode", "isolation_level"):
            if inputs.get(param):
                logger.warning(
                    "[TaskSubmit] 普通子任务显式指定 %s 被拒绝（继承父任务）| parent_task_id=%s | value=%s",
                    param,
                    parent_task_id,
                    inputs.get(param),
                )
                return create_failure_result(
                    error=(
                        f"普通子任务继承父任务的工作空间与隔离配置，不能指定 {param}。"
                        "如需继承对话历史，请使用 inherit_from + inherit_mode=['pipe']。"
                    ),
                    error_code="SUBTASK_INHERITS_PARAMS",
                )
        return None

    def _reject_subroot_without_parent(
        self,
        parent_agent_level: Any,
        parent_task_id: str | None,
        injected_task_id: Any,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult | None:
        """L2/L3 层级校验：自动注入后仍无 parent_task_id → 拒绝创建根任务。

        触发即说明注入链断裂，诊断字段用于定位断裂点：
        - injected_task_id 空 → param_inject 没注入或 state["task_id"] 为空
        - inputs 无 task_id 键 → param_inject 完全没处理此调用
        - task_id 键存在但为空 → state["task_id"] 在引擎 state 中缺失
        """
        if not (parent_agent_level >= 2 and parent_task_id is None):
            return None
        diag_keys = [
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
            diag_keys,
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

    async def _resolve_resource_inheritance(
        self,
        inputs: dict[str, Any],
        workspace: str,
    ) -> tuple[str, ToolExecutionResult | None]:
        """inherit 资源解析统一入口（pipe / workspace / inherit_workspace_from）。

        - schema 已平铺为 inherit_from/inherit_mode，这里把扁平字段重组为 inherit
          对象，供 _build_metadata 及管道引擎按既有契约读取（旧式 inherit 对象仍兼容）；
        - mode 可传单字符串或列表（如 ["pipe","workspace"]），两种模式相互独立可组合；
        - pipe：查源任务的 pipeline_run_id（日志观测，管道身份由引擎生成）；
        - workspace：等价于 inherit_workspace_from，直接复用旧任务的 ws_meta.path，
          不复制、不初始化；源不存在/跨容器/git 身份失效则报错让 agent 重提。
        当 inherit 和 inherit_workspace_from 同时存在时，inherit 优先。
        返回 (最终 workspace, 失败结果)。
        """
        inherit_config = inputs.get("inherit")
        if not isinstance(inherit_config, dict) and (
            inputs.get("inherit_from") is not None or inputs.get("inherit_mode") is not None
        ):
            inherit_config = {
                "from": inputs.get("inherit_from", ""),
                "mode": inputs.get("inherit_mode", ""),
            }
            inputs["inherit"] = inherit_config
        if isinstance(inherit_config, dict):
            inherit_from_id = inherit_config.get("from", "")
            inherit_mode = inherit_config.get("mode", "")
            # 规范化 mode 为集合：兼容 str / list / tuple
            if isinstance(inherit_mode, str):
                mode_set = {inherit_mode}
            elif isinstance(inherit_mode, (list, tuple)):
                mode_set = set(inherit_mode)
            else:
                mode_set = set()
            if not inherit_from_id or not mode_set:
                return workspace, create_failure_result(
                    error="inherit 参数必须包含 from（源任务 ID）和 mode（pipe/workspace）",
                    error_code="INVALID_INHERIT_PARAMS",
                )
            # 校验：每个 mode 值必须合法
            invalid_modes = mode_set - {"pipe", "workspace"}
            if invalid_modes:
                return workspace, create_failure_result(
                    error=(f"inherit.mode 不合法: '{sorted(invalid_modes)}'，仅支持 pipe/workspace"),
                    error_code="INVALID_INHERIT_MODE",
                )
            # pipe 与 workspace 相互独立，可同时生效
            if "pipe" in mode_set:
                await self._lookup_source_pipeline_run(inherit_from_id)
            if "workspace" in mode_set:
                # workspace 模式等价于 inherit_workspace_from，复用现有逻辑
                inputs["inherit_workspace_from"] = inherit_from_id
                logger.info(
                    "[TaskSubmit] inherit workspace | from=%s (覆盖 inherit_workspace_from)",
                    inherit_from_id,
                )

        # ── inherit_workspace_from 解析 ──
        inherit_from = inputs.get("inherit_workspace_from")
        if not inherit_from:
            return workspace, None
        task_service = self._get_task_service()
        if not task_service:
            return workspace, create_failure_result(
                error=(
                    f"无法查找任务 {inherit_from}：任务服务不可用。"
                    "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                ),
            )
        try:
            old_ws_path, ws_fail = self._extract_inherited_workspace(inherit_from, task_service)
            if ws_fail is not None:
                return workspace, ws_fail
            # 继承成功时回写 inputs，确保 _build_metadata 存储到任务元数据
            workspace = old_ws_path or ""
            inputs["workspace"] = workspace
            logger.info(
                "[TaskSubmit] inherit_workspace_from: task_id=%s, ws_path=%s",
                inherit_from,
                old_ws_path,
            )
        except Exception as resolve_err:
            logger.warning(
                "[TaskSubmit] inherit_workspace_from 解析失败: %s",
                resolve_err,
            )
            return workspace, create_failure_result(
                error=f"继承工作空间时出错: {resolve_err}。请去掉 inherit_workspace_from 参数重新提交。",
            )
        return workspace, None

    async def _lookup_source_pipeline_run(self, inherit_from_id: str) -> str:
        """查询 pipe 继源源任务的 pipeline_run_id（日志观测用途）。

        服务不可用 / 源任务缺失 / 查询异常均降级为 warning 日志，不阻断提交。
        """
        task_service = self._get_task_service()
        if not task_service:
            logger.warning(
                "[TaskSubmit] inherit pipe | task_service 不可用，无法查找源任务 %s",
                inherit_from_id,
            )
            return ""
        try:
            source_task = task_service.get_task(inherit_from_id)
            if source_task and source_task.pipeline_run_id:
                source_pipeline_id = source_task.pipeline_run_id
                logger.info(
                    "[TaskSubmit] inherit pipe | from=%s | source_pipeline=%s",
                    inherit_from_id,
                    source_pipeline_id[:12],
                )
                return source_pipeline_id
            logger.warning(
                "[TaskSubmit] inherit pipe | from=%s | 源任务无 pipeline_run_id，对话历史为空",
                inherit_from_id,
            )
            return ""
        except Exception as pipe_err:
            logger.warning(
                "[TaskSubmit] inherit pipe 查找源任务失败: %s",
                pipe_err,
            )
            return ""

    def _extract_inherited_workspace(
        self,
        inherit_from: str,
        task_service: Any,
    ) -> tuple[str | None, ToolExecutionResult | None]:
        """校验被继承任务的 ws_meta 并取出旧工作空间路径（复用旧路径，不复制不初始化）。

        校验序：源任务存在且有元数据 → ws_meta 形态 → 同容器归属 → 目录存在 →
        worktree 模式 git 身份有效。任一步失败返回失败信封，引导 agent 去掉
        inherit_workspace_from 后重新提交。
        返回 (旧工作空间路径, 失败结果)；路径有效性由调用方按目录语义使用。
        """
        old_task = task_service.get_task(inherit_from)
        if not old_task or not old_task.metadata:
            return None, create_failure_result(
                error=(
                    f"任务 {inherit_from} 不存在或无元数据。"
                    "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                ),
            )
        old_ws_meta = old_task.metadata.get("ws_meta")
        if not isinstance(old_ws_meta, dict):
            return None, create_failure_result(
                error=(
                    f"任务 {inherit_from} 没有工作空间信息。"
                    "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                ),
            )
        # 同容器才能 inherit，避免产出落到错误容器。
        source_root = old_ws_meta.get("project_root", "") or old_ws_meta.get("path", "")
        current_container = Path(__file__).resolve().parents[4]
        if source_root:
            try:
                Path(source_root).resolve().relative_to(current_container)
            except ValueError:
                return None, create_failure_result(
                    error=(
                        f"任务 {inherit_from} 属于其它容器({source_root})，"
                        f"不能跨容器继承工作空间。"
                        f"请去掉 inherit_workspace_from 参数重新提交。"
                    ),
                )
        old_ws_path = old_ws_meta.get("path", "")
        if not old_ws_path or not Path(old_ws_path).exists():
            return None, create_failure_result(
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
                return None, create_failure_result(
                    error=(
                        f"任务 {inherit_from} 的工作空间: git 身份已失效,"
                        f"目录里产物可能仍在可手动读取或者处理: {old_ws_path}。"
                        f"请去掉 inherit_workspace_from 参数重新提交。"
                    ),
                )
        return old_ws_path, None

    async def _gate_target_and_dependencies(
        self,
        target_type: Any,
        target_id: Any,
        parent_agent_level: Any,
        parent_task_id: str | None,
        dependencies: list[Any],
    ) -> tuple[list[Any], ToolExecutionResult | None]:
        """目标与依赖闸门：target 必填 → 目标 Agent 存在性/级别 → parent 权限复核 → 依赖存在性。

        返回 (dependencies, 失败结果)。
        """
        # ── 2. 必填参数验证 ──
        if not target_type:
            return dependencies, create_failure_result(
                error="目标类型不能为空",
                error_code="MISSING_TARGET_TYPE",
            )
        if not target_id:
            return dependencies, create_failure_result(
                error="目标 ID 不能为空",
                error_code="MISSING_TARGET_ID",
            )

        # ── 2.5 目标 Agent 存在性与级别校验 ──
        if target_type == "agent":
            valid, err_msg, err_code = await self._validate_target_agent(
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
                return dependencies, create_failure_result(error=err_msg, error_code=err_code)
            logger.info(
                "[TaskSubmit] 目标 Agent 校验通过 | target_id=%s | parent_level=L%d",
                target_id,
                parent_agent_level,
            )

        # ── 3. 权限验证 ──
        if not await self._validate_parent_task_id(parent_agent_level, parent_task_id):
            return dependencies, create_failure_result(
                error="L2 Agent 不能显式指定 parent_task_id（系统会自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        # ── 4. 依赖任务验证 ──
        if dependencies:
            missing_ids = await self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error("[TaskSubmit] 依赖验证失败 | 不存在的任务: %s", missing_ids)
                return dependencies, create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info("[TaskSubmit] 依赖验证通过 | dependencies=%s", dependencies)
        return dependencies, None

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务提交：参数解析与闸门 → 项目/继承/工作空间解析 → 目标与依赖校验 → 派发。

        各阶段拆为同名职责的私有方法（_parse_goal_input / _gate_* / _resolve_* /
        _normalize_acceptance_criteria）；任一阶段返回失败信封即短路返回，
        本方法只保留阶段序列与派发编排。
        """
        parent_agent_level = inputs.get("parent_agent_level")

        # ── 0.5 短 id 入参解析：LLM 回传的 parent_task_id / task_id 可能是 12 位
        # 短 id，经 state 聚合前缀唯一解析回全 id（容器 project id 生成即短，天然命中）。
        await self._resolve_short_input_ids(inputs)

        logger.info(
            "[TaskSubmit] 开始执行 | project_id=%s | parent_agent_level=%s",
            inputs.get("project_id") or "-",
            parent_agent_level,
        )

        # ── 0. 注入参数校验 ──
        if parent_agent_level is None:
            logger.error("[TaskSubmit] 注入参数缺失 | parent_agent_level 未注入")
            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        # ── 1. goal 解析 + 基础字段/归属/长度闸门 ──
        parsed_goal = self._parse_goal_input(inputs)
        goal, description, base_fail = self._gate_base_submission(inputs, parsed_goal, parent_agent_level)
        if base_fail is not None:
            return base_fail

        acceptance_criteria, acc_fail = self._normalize_acceptance_criteria(
            inputs.get("acceptance_criteria", {})
        )
        if acc_fail is not None:
            return acc_fail

        parent_task_id, parent_fail = self._apply_parent_task_injection(
            parent_agent_level, inputs.get("parent_task_id"), inputs
        )
        if parent_fail is not None:
            return parent_fail

        # agent 原始 workspace 入参先留底：项目解析可能注入 workspace（项目文件夹），
        # 子任务闸门只认原始入参，系统注入值不算显式指定。
        explicit_workspace_raw = str(inputs.get("workspace") or "")
        project_id, project_fail = await self._resolve_project_binding(
            inputs,
            parent_task_id,
            parent_agent_level,
            explicit_workspace_raw,
        )
        if project_fail is not None:
            return project_fail

        subtask_gate_fail = self._gate_subtask_param_inheritance(
            inputs, parent_task_id, explicit_workspace_raw
        )
        if subtask_gate_fail is not None:
            return subtask_gate_fail

        injected_task_id = inputs.get("task_id")
        root_gate_fail = self._reject_subroot_without_parent(
            parent_agent_level, parent_task_id, injected_task_id, inputs
        )
        if root_gate_fail is not None:
            return root_gate_fail

        workspace = inputs.get("workspace", "")
        workspace, inherit_fail = await self._resolve_resource_inheritance(inputs, workspace)
        if inherit_fail is not None:
            return inherit_fail

        # ── 目标空间安全检查 ──
        if workspace:
            ws_error = _validate_workspace_path(workspace)
            if ws_error:
                return create_failure_result(
                    error=ws_error,
                    error_code="UNSAFE_WORKSPACE",
                )

        # 显式 Any：与拆分前一致——入参不在此处强转，类型闸门在 _gate_target_and_dependencies
        target_type: Any = inputs.get("target_type")
        target_id: Any = inputs.get("target_id")
        logger.info(
            "[TaskSubmit] 任务提交 | target_type=%s | target_id=%s",
            target_type,
            target_id,
        )
        logger.debug(
            "[TaskSubmit] 任务详情 | title=%s | metric_count=%d",
            goal.get("title", "N/A"),
            len(acceptance_criteria),
        )
        dependencies, gate_fail = await self._gate_target_and_dependencies(
            target_type,
            target_id,
            parent_agent_level,
            parent_task_id,
            inputs.get("dependencies", []),
        )
        if gate_fail is not None:
            return gate_fail

        # ── 5. GAP-1 统一（state 单一真值）：任务即管道，直接经 chat.send_message
        #    创建执行管道（引擎生成 pipeline_id = task.id）——不再创建 YAML 任务
        #    记录（task_service.create_task 退役，YAML 降级只读镜像）。
        #    - 依赖校验读 state 聚合（pipeline-state.list）而非 YAML 树；
        #    - 工作空间初始化归执行管道的 workspace_lifecycle 插件（execution_context
        #      随派发透传），提交期不再同步初始化；
        #    - 任务状态（task.status/ended_at）由内核 run 终态回写 state。
        dispatch = await self._dispatch_task_pipeline(
            title=goal["title"],
            description=description,
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies,
            inputs=inputs,
            agent_id=(target_id if target_type == "agent" else ""),
            # 挂靠项目时管道 state 带 task.parent_project_id（project id）——
            # 前端任务树据此把子任务挂到项目节点下（项目不是管道，不能当
            # lineage 父）；L1 显式指定与 L2/L3 父链继承同值。
            parent_project_id=project_id,
        )
        if dispatch.get("pipeline_id"):
            task_id = dispatch["pipeline_id"]
            # 引擎生成即 12 位短 id（uuid v4 simple 前 12 位，全链路统一）；
            # _short_id 幂等（短 id 原样返回），工具入口前缀解析兼容。
            short_task_id = _short_id(task_id)
            result_data: dict[str, Any] = {
                "task_id": short_task_id,
                "title": goal["title"],
                "status": "running",
                "target_id": target_id,
            }
            result_data["pipeline_id"] = short_task_id
            result_data["message"] = (
                f"任务 [{goal['title']}]（ID: {short_task_id}）已提交，执行管道已创建，"
                "任务正在后台执行。"
                "子任务完成后系统会自动通知你并恢复执行。"
                "在此期间请不要再调用任何工具（包括 task_manage），直接输出纯文本等待即可。"
            )
            # 工作空间路径仅对 L1 返回（L2/L3 的 workspace 参数本身被隐藏，回显内部路径属信息泄漏）；
            # 统一后无 YAML ws_meta（空间初始化归执行管道 workspace_lifecycle），仅回显提交参数。
            if parent_agent_level == 1:
                result_data["workspace"] = workspace or ""

            return create_success_result(
                data=result_data,
                metadata={
                    "action": "task_submit",
                    "project_id": project_id,
                },
            )

        # 派发失败（管道未创建）= 核心流程失败：任务不存在，失败信封让调用方
        # 感知并可重试，不得以 success 掩盖。
        return create_failure_result(
            error=(
                f"任务 [{goal['title']}] 已校验，但执行管道未能创建"
                f"（{dispatch.get('warning', '未知原因')}）。"
                "任务当前不会自动执行；可稍后重试提交。"
            ),
            error_code="DISPATCH_FAILED",
            metadata={
                "action": "task_submit",
                "project_id": project_id,
            },
        )


    async def _dispatch_task_pipeline(  # noqa: PLR0913
        self,
        title: str,
        description: str,
        acceptance_criteria: dict[str, Any],
        dependencies: list[str],
        inputs: dict[str, Any],
        agent_id: str = "",
        parent_project_id: str = "",
    ) -> dict[str, Any]:
        """GAP-1 统一：经 chat.send_message 创建任务执行管道（引擎生成 id = task.id）。

        契约（与内核 chat_send_handler 创建分支对齐）：
        - ``create: true`` + 不传 pipeline_id——引擎生成并在响应返回（三次定案：
          堵 id 冒占）；**task.id 由引擎注入 state**（身份权威统一，调用方
          派发时还不知道 id），task = pipeline，无独立 YAML 记录；
        - ``state``：任务域字段出生即入（task.goal/status/description/
          acceptance_criteria/dependencies——扁平点号键，STATE_SUMMARY_KEYS
          出口）；task.status 终态由内核 run 终态回写（completed/failed/suspended）；
        - ``state`` 的血缘扁平键：有父形式（lineage.parent_pipeline_id = 调用方
          管道，lineage.origin_session_id 同管道）/ 根形式（lineage.root + 来源
          声明，无调用方管道时诚实声明 plugin 来源，不伪造默认父）二选一；
        - ``execution_context``：workspace/isolation（本地组装，供执行管道
          init 体 workspace_lifecycle 消费——空间初始化不再在提交期做）；
        - ``background: true``：不阻塞工具调用等待任务完成（派发即返回 id）。

        Returns:
            ``{"pipeline_id": ...}`` 派发成功（即 task.id）；``{"warning": ...}``
            派发器缺席/失败（无 YAML 记录可清理——管道未创建即无任务）。
        """
        sender = _get_chat_sender()
        if sender is None:
            return {
                "warning": "chat capability 未注入（sidecar 未接线），任务未派发执行"
            }

        parent_pipeline_id = inputs.get("pipeline_id") or ""
        if parent_pipeline_id:
            # origin_session_id = 根人类会话锚点：内核 param_inject 注入的
            # session_id/thread_id（pipeline id 非会话 id，会话归属校验按
            # 会话 id 匹配）。
            origin_session = (
                inputs.get("session_id")
                or inputs.get("thread_id")
                or parent_pipeline_id
            )
            lineage_keys: dict[str, Any] = {
                "lineage.parent_pipeline_id": parent_pipeline_id,
                "lineage.origin_session_id": origin_session,
            }
        else:
            lineage_keys = {
                "lineage.root": True,
                "lineage.origin.kind": "plugin",
                "lineage.origin.source": "task_submit",
            }

        kickoff = f"执行任务「{title}」。"
        if description:
            kickoff += f"\n任务描述：{description}"
        if acceptance_criteria:
            kickoff += f"\n验收标准：{acceptance_criteria}"
        execution_context = self._build_execution_context(inputs)
        kickoff += _build_evaluation_criteria_prompt(acceptance_criteria)
        kickoff += _build_workspace_guidance(execution_context)
        kickoff += _build_task_progress_method()

        params: dict[str, Any] = {
            "create": True,
            "message": kickoff,
            "user_id": inputs.get("user_id") or "task_system",
            "state": {
                "task.goal": title,
                "task.status": "pending",
                "task.description": description or "",
                "task.acceptance_criteria": acceptance_criteria or {},
                "task.dependencies": dependencies or [],
                "task.submitted_by": inputs.get("user_id", ""),
                **lineage_keys,
            },
            "background": True,
        }
        # 挂靠项目（项目非管道）：任务管道 state 带 parent_project_id——
        # 前端任务树据此挂项目节点下；lineage 有父形式仍指向提交者管道
        # （项目不是管道，不能当 lineage 父）。
        if parent_project_id:
            params["state"]["task.parent_project_id"] = parent_project_id
        # 目标 agent 传导：target_type=agent 时执行管道按该 agent 配置跑
        # （人格/tool_ids）——内核 chat_send_handler 创建分支消费。缺失回退主 agent。
        if agent_id:
            params["agent_id"] = agent_id
        if execution_context:
            params["execution_context"] = execution_context

        try:
            resp = await sender(params)
        except Exception as exc:
            logger.error(
                "[TaskSubmit] 任务管道派发失败 | title=%s | err=%s",
                title,
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
            "[TaskSubmit] 任务执行管道已创建 | task_id=%s | title=%s",
            pipeline_id,
            title,
        )
        # 语义统一：任务 id 写"自己的管道"（提交者管道）state——
        # task.owned.<id> 自持（本管道插件也能读它处理它）；执行管道 state 收
        # task.assigned（收到上级的任务 id，引擎注入 task.id 即管道身份）。
        # 写入通道：chat.send_message 注入分支 + no_dispatch（只写 state 不派发）。
        if parent_pipeline_id:
            try:
                await sender(
                    {
                        "pipeline_id": parent_pipeline_id,
                        "message": f"登记任务「{title}」（{pipeline_id}）。",
                        "user_id": inputs.get("user_id") or "task_system",
                        "no_dispatch": True,
                        "state": {
                            f"task.owned.{pipeline_id}.title": title,
                            f"task.owned.{pipeline_id}.status": "running",
                            f"task.owned.{pipeline_id}.created_at": _now_iso(),
                            f"task.owned.{pipeline_id}.submitted_by": inputs.get("user_id", ""),
                        },
                    }
                )
            except Exception as exc:
                logger.warning(
                    "[TaskSubmit] 任务登记到提交者管道失败（不影响执行）| task=%s | err=%s",
                    pipeline_id,
                    exc,
                )
        return {"pipeline_id": pipeline_id}

    def _build_execution_context(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """结构化 execution_context（GAP-1 统一：随派发透传，不写 YAML metadata）。

        对齐 0.1 执行语义（task_executor）：任务默认隔离执行——
        - isolation 默认 isolated（0.1 coordinator.default_level；显式
          isolation_level 优先）。
        - workspace 声明语义：无显式 workspace（含 workspace_mode 未选）时
          mode 留空——执行管道落「工作空间根/{task_id}」默认目录（plain 拓扑，
          workspace_lifecycle._bootstrap 的 mode 缺省 plain）。worktree 拓扑
          仅在显式 workspace 或显式 workspace_mode='worktree' 下成立
          （源 = workspace 路径 / 项目根）；显式 workspace 时 mode 缺省 worktree。
        """
        _ec: dict[str, Any] = {}
        if inputs.get("workspace"):
            _ec["workspace"] = {
                "source_path": inputs["workspace"],
                "mode": inputs.get("workspace_mode") or "worktree",
                "explicit": True,
            }
        else:
            _ec["workspace"] = {
                "source_path": "",
                "mode": inputs.get("workspace_mode") or "",
                "explicit": False,
            }
        level = inputs.get("isolation_level") or "isolated"
        if level:
            _ec["isolation"] = {"level": level}
        return _ec

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

    async def _validate_parent_task_id(self, parent_agent_level: int, parent_task_id: str | None) -> bool:
        """验证 parent_task_id 参数的使用权限。"""
        if parent_agent_level == 1 and parent_task_id is not None:
            task_service = self._get_task_service()
            if task_service and task_service.get_task(parent_task_id) is None:
                # GAP-1 统一：登记型任务 = 提交者管道自持的声明
                # （task.owned.*），不在 YAML 存储——存在性校验加 state 聚合兜底
                if not await self._parent_exists_in_state(parent_task_id):
                    logger.error("[TaskSubmit] parent_task_id 不存在: %s", parent_task_id)
                    return False

        # L2/L3: parent_task_id 必须已由自动注入填充
        if parent_agent_level >= 2 and parent_task_id is None:
            logger.warning(
                "[TaskSubmit] L%d Agent 无 parent_task_id（纵深防御拦截）",
                parent_agent_level,
            )
            return False

        return True

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list capability；None = 桥未就绪）。

        读取失败/未注入返回 None（fail-closed 语义由调用方决定）；
        返回 list[dict] 时行为扁平点号键（pipeline_id/task.*/lineage.*）。
        """
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if inspect.isawaitable(rows):
                rows = await rows  # type: ignore[misc]
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩提交
            logger.warning("[TaskSubmit] state 聚合读取失败，依赖校验降级: %s", exc)
            return None

    async def _inherit_project_id(self, parent_task_id: str) -> str:
        """项目归属的父链继承：读父任务 state 行的 task.parent_project_id（单跳）。

        每级任务提交时都写自己的 parent_project_id（L1 显式 / 子级继承），
        单跳读父行即得全链归属；父行无键（独立任务链）返回空。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return ""
        for row in rows:
            if str(row.get("pipeline_id") or "") != parent_task_id:
                continue
            return str(row.get("task.parent_project_id") or "")
        return ""

    async def _resolve_short_input_ids(self, inputs: dict[str, Any]) -> None:
        """LLM 入参短 id → 全 id（state 聚合前缀唯一解析，就地改写 inputs）。

        容器 project id（task.owned.<id>，生成即短）与普通任务 id（pipeline_id
        前 12 位）都可被 LLM 以短形式回传；精确命中原样，多命中/无命中保持原样
        让既有校验路径报错。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return
        for key in ("parent_task_id", "task_id", "pipeline_id", "dependencies"):
            if key == "dependencies":
                if isinstance(inputs.get(key), list):
                    resolved: list[str] = []
                    for d in inputs[key]:
                        if isinstance(d, str):
                            r = await _resolve_short_id(rows, d)
                            if not r.startswith("AMBIGUOUS:"):
                                resolved.append(r)
                            else:
                                resolved.append(d)
                        else:
                            resolved.append(d)
                    inputs[key] = resolved
                continue
            val = inputs.get(key)
            if not isinstance(val, str) or not val:
                continue
            r = await _resolve_short_id(rows, val)
            if not r.startswith("AMBIGUOUS:"):
                inputs[key] = r

    async def _parent_exists_in_state(self, parent_task_id: str) -> bool:
        """父任务存在性 state 兜底：容器任务（task.owned.*）或执行管道（task.* 行）。"""
        rows = await self._read_state_rows()
        if rows is None:
            return False
        for row in rows:
            if str(row.get("pipeline_id") or "") == parent_task_id:
                return True
            if any(
                str(k).startswith(f"task.owned.{parent_task_id}.")
                for k in row.keys()
            ):
                return True
        return False

    async def _check_dependencies_exist(self, dependencies: list[str]) -> list[str]:
        """检查依赖任务是否存在。"""

        rows = await self._read_state_rows()
        if rows is None:
            logger.warning(
                "[TaskSubmit] state 聚合不可用，依赖校验 fail-closed | dependencies=%s",
                dependencies,
            )
            return list(dependencies)
        known = {str(r.get("pipeline_id") or "") for r in rows}
        return [d for d in dependencies if d not in known]
    def _build_metadata(
        self,
        inputs: dict[str, Any],
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
    ) -> dict[str, Any]:
        """构建任务元数据：按「验收标准 → 会话/层级 → 执行直传字段 →
        execution_context → 执行者 → inherit 资源继承」六组装配。"""
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

        # 显式执行类直传字段：真值才落账（workspace 仅存 agent 显式值——param_inject
        # 已跳过注入、容器直接子任务已被拒绝清除；inherit_workspace_from 回写的旧
        # 路径同样落账；project_id 为项目挂靠键，state 面 task.parent_project_id
        # 同值双写；workspace_mode/isolation_level 为 agent 显式选择）
        for key in (
            "workspace",
            "max_retries",
            "project_id",
            "workspace_mode",
            "isolation_level",
            "target_id",
            "user_id",
        ):
            if inputs.get(key):
                metadata[key] = inputs[key]

        task_ec = self._task_level_execution_context(inputs)
        if task_ec:
            metadata["execution_context"] = task_ec

        self._attach_inherit_metadata(metadata, inputs)
        return metadata

    @staticmethod
    def _task_level_execution_context(inputs: dict[str, Any]) -> dict[str, Any]:
        """结构化 execution_context（任务级）：供任务执行器经 chat.send_message
        透传 → 内核 initial_state 并入 → init 体 workspace_lifecycle /
        environment_lifecycle 插件消费。

        - workspace：显式路径带 source_path；无显式（继承父链）但有拓扑声明时
          source_path 留空；
        - isolation：执行环境隔离级别；
        - parent_task_id：供 init 体插件做容器直接子任务的父链查询
          （0.2 sidecar 无 task_service，插件经此重建最小 task_tree）。
        """
        ec: dict[str, Any] = {}
        if inputs.get("workspace"):
            ec["workspace"] = {
                "source_path": inputs["workspace"],
                "mode": inputs.get("workspace_mode") or "worktree",
                "explicit": True,
            }
        elif inputs.get("workspace_mode"):
            # 无显式 workspace（继承父链）但有拓扑声明（显式选择才落）
            ec["workspace"] = {
                "source_path": "",
                "mode": inputs["workspace_mode"],
            }
        if inputs.get("isolation_level"):
            ec["isolation"] = {"level": inputs["isolation_level"]}
        if inputs.get("parent_task_id"):
            ec["parent_task_id"] = inputs["parent_task_id"]
        return ec

    @staticmethod
    def _attach_inherit_metadata(metadata: dict[str, Any], inputs: dict[str, Any]) -> None:
        """存储 inherit 资源继承配置（供管道引擎读取）；pipe 模式附带宽照键
        ``inherit_pipe_from``（mode 可能是字符串或含 "pipe" 的列表）。"""
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
            mode_value = inherit_config.get("mode", "")
            is_pipe = mode_value == "pipe" or (
                isinstance(mode_value, (list, tuple)) and "pipe" in mode_value
            )
            if is_pipe:
                metadata["inherit_pipe_from"] = inherit_config["from"]

    async def _validate_target_agent(
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

        agent_config = await self._get_agent_config_from_registry(target_id)

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
            found, agent_level_str, agent_level, is_active, corrupt_path = (
                self._lookup_agent_from_disk(target_id)
            )
            # agent yaml 损坏是配置故障，与"Agent 不存在"分开归因，
            # 避免误导用户去检查 target_id
            if corrupt_path:
                return (
                    False,
                    f"agent 配置文件损坏: {corrupt_path}",
                    "TARGET_AGENT_CONFIG_CORRUPT",
                )
            if not found:
                return (
                    False,
                    f"目标 Agent '{target_id}' 不存在。"
                    f"请检查 target_id 是否正确。如果系统提供了 Agent 映射表，请使用映射表中的 Agent ID。",
                    "TARGET_AGENT_NOT_FOUND",
                )

        # level 缺失/非法 → 0 会让下方两道层级闸门全部短路（0≠1 且不>0），
        # 必须显式拒绝而非静默放行
        if agent_level == 0:
            return (
                False,
                f"Agent level 缺失/非法（agent='{target_id}'，level={agent_level_str!r}）。"
                f"层级闸门无法判定，拒绝派发——请修复该 Agent 的 level 配置（合法值：L1/L2/L3）。",
                "TARGET_AGENT_LEVEL_MISSING",
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

    async def _get_agent_config_from_registry(self, target_id: str) -> Any | None:
        """从 agent_registry（agent_manager 的 agent.get 服务）查找 Agent 配置。

        P4 收敛：查询钩子由 server.py on_load 注入（tool-executor
        显式 plugin_id=agent_manager）；未注入/服务不可用/未命中 → None（磁盘回退）。
        """
        lookup = _agent_registry_lookup
        if lookup is None:
            return None
        try:
            config = await lookup(target_id)
        except Exception as exc:  # noqa: BLE001 — 服务故障降级磁盘回退
            logger.warning(
                "[_get_agent_config_from_registry] agent_manager 服务查询失败 (target_id=%s): %s",
                target_id,
                exc,
            )
            return None
        if not isinstance(config, dict):
            return None
        # 0.1 调用方期待属性访问（.level/.is_active）——轻量命名空间适配
        return SimpleNamespace(
            level=config.get("level", ""),
            is_active=config.get("is_active", True),
        )

    @staticmethod
    def _lookup_agent_from_disk(target_id: str) -> tuple[bool, str, int, bool, str]:
        """从磁盘 YAML 文件查找 Agent 配置（回退方案）。

        Returns:
            (found, level 串, level 数值, is_active, 损坏文件路径)。
            末位非空表示找到了匹配的 yaml 但解析失败（配置损坏，与"不存在"区分归因）。
        """
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
            return (False, "", 0, True, "")

        try:
            with open(yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            return (False, "", 0, True, str(yaml_path))

        agent_level_str = config.get("level", "")
        level_map = {"L1": 1, "L2": 2, "L3": 3}
        agent_level = level_map.get(agent_level_str, 0)
        is_active = config.get("is_active", True)
        return (True, agent_level_str, agent_level, is_active, "")
