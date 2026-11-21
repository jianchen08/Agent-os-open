"""AC 接受标准与指标校验域（metrics 定义加载 / 扁平展开 / 占位符与必填参数校验）（自 tool.py 按职责拆分）。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from _libs.evaluation_metrics_path import probe_metrics_config_path

from agentos_plugin_sdk import ToolExecutionResult, create_failure_result

logger = logging.getLogger(__name__)

_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader


def _metrics_config_path() -> Path:
    """评估指标配置路径（config/plugins/evaluation/evaluation_metrics.yaml）。

    探测算法单源 ``_libs.evaluation_metrics_path``（祖先逐级探测，首个含该
    yaml 的祖先即根；三布局单算法通吃 + 全 miss 回落向上 3 级拼相对尾），
    本侧只提供探测坐标（本插件目录）与 Path 返回形态。
    指标定义的唯一加载点（提交期校验与派发指令详情展开共用）。
    """
    here = Path(__file__).resolve().parent
    return Path(probe_metrics_config_path(str(here)))


def _load_metric_definitions() -> dict[str, dict[str, Any]]:
    """加载指标定义表（name → 定义）；缺文件/坏格式 → 空表（fail-open）。"""
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(_metrics_config_path().read_text(encoding="utf-8")) or {}
        return {str(m["name"]): m for m in data.get("metrics", []) or [] if isinstance(m, dict) and m.get("name")}
    except Exception as exc:
        logger.warning(
            "[TaskSubmit] 评估指标定义加载失败: %s",
            exc,
        )
        return {}


def _get_valid_metric_ids() -> set[str] | None:
    """获取所有合法的评估指标 ID 集合。

    评估指标的真相来源是 config/plugins/evaluation/evaluation_metrics.yaml
    （evaluation 插件同款读取）。
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


# ── 白话验收标准（扁平字段，唯一宣传写法；用户裁定 2026-09-29，ADR
# docs/decisions/2026-09-29-acceptance-flat-fields.md）──
# 老指标名 dict 形态静默兼容（不宣传）；两形态混用拒绝（语义归属不明）。
_FLAT_ACCEPTANCE_FIELDS = ("files", "expect", "command")
_FLAT_INSTANCE_SEP = "::"


def _metric_definition_id(key: str) -> str:
    """acceptance_criteria 键 → 指标定义 ID。

    白话展开的多实例键形如 ``file_check::chapters/chapter_025.md``（::<实例>
    后缀仅区分实例，评估参数自带真实坐标），取首段即定义 ID。
    """
    return key.split(_FLAT_INSTANCE_SEP, 1)[0]


def _validate_metric_ids(
    acceptance_criteria: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """校验 acceptance_criteria 的 key 是否为合法指标 ID（实例键按定义 ID 首段）。

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
        if _metric_definition_id(key) in valid_ids:
            filtered[key] = value
        else:
            invalid.append(key)
    return filtered, invalid


def _invalid_flat_field(field: str, expectation: str) -> ToolExecutionResult:
    """白话字段形态非法的失败信封（自描述期望形态）。"""
    return create_failure_result(
        error=f"acceptance_criteria 的白话字段 {field} 形态非法，需要{expectation}。",
        error_code="INVALID_ACCEPTANCE_FIELD",
    )


def _apply_legacy_metric_aliases(acceptance_criteria: dict[str, Any]) -> dict[str, Any]:
    """深格式别名容错（就地归一，INFO 日志透明告知）——限 semantic_check 条目。

    - expected_output → expected（dict/非串形态 json.dumps/str 归一）；
    - criteria → 并入 expected（与既有值换行拼接，不覆盖）；
    归一结果落 input_params（缺席则创建），提交期必填校验按归一后判定——
    旧 criteria 形态（此前必然被拒）由此直接可用。别名不越指标定义：
    expected 是 semantic_check 的定义参数，其余指标不改写。
    """
    for metric_id, config in acceptance_criteria.items():
        if _metric_definition_id(metric_id) != "semantic_check" or not isinstance(config, dict):
            continue
        targets: list[dict[str, Any]] = [config]
        if isinstance(config.get("input_params"), dict):
            targets.append(config["input_params"])
        merged: list[str] = []
        for target in targets:
            for alias in ("expected_output", "criteria"):
                if alias not in target:
                    continue
                value = target.pop(alias)
                if isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False)
                elif value is not None and not isinstance(value, str):
                    value = str(value)
                if isinstance(value, str) and value.strip():
                    merged.append(value)
                    logger.info(
                        "[TaskSubmit] 深格式别名归一 | metric=%s | %s→expected",
                        metric_id,
                        alias,
                    )
        if merged:
            params = config.setdefault("input_params", {})
            existing = str(params.get("expected") or "")
            params["expected"] = "\n".join([existing, *merged]) if existing else "\n".join(merged)
    return acceptance_criteria


def _expand_flat_acceptance_criteria(
    acceptance_criteria: dict[str, Any],
) -> tuple[dict[str, Any], ToolExecutionResult | None]:
    """白话扁平字段展开（主形态）+ 深格式别名容错。

    - 白话字段 files/command 任选组合（零指标知识零嵌套）：
      files → 逐文件 file_check（存在且非空，实例键 ``file_check::<路径>``）；
      command → bash_check(command=command)；
      expect → semantic_check 仅保留存量兼容（已从 LLM 工具面撤下，不广告）；
    - 与指标名键混用 → MIXED_ACCEPTANCE_CRITERIA 拒绝（两形态语义归属不明）；
    - 白话字段全空（无任何可执行标准）→ EMPTY_ACCEPTANCE_CRITERIA 拒绝，
      错误动态附全部可用指标+必填（复用 _metric_required_examples，不另造清单）；
    - 纯深格式 → 静默兼容原样（semantic_check 条目做别名容错）。
    """
    flat_present = [k for k in _FLAT_ACCEPTANCE_FIELDS if k in acceptance_criteria]
    if not flat_present:
        return _apply_legacy_metric_aliases(acceptance_criteria), None

    metric_keys = sorted(k for k in acceptance_criteria if k not in _FLAT_ACCEPTANCE_FIELDS)
    if metric_keys:
        logger.warning(
            "[TaskSubmit] acceptance_criteria 白话字段与指标名混用，拒绝 | flat=%s | metric_keys=%s",
            flat_present,
            metric_keys,
        )
        return {}, create_failure_result(
            error=(
                f"acceptance_criteria 混用了白话字段 {flat_present} 与指标名键 {metric_keys}，"
                "两种写法不能同时使用。请只用白话字段（files/command 任选组合），"
                "或只用指标名 dict 形态（老写法，仍兼容）。"
            ),
            error_code="MIXED_ACCEPTANCE_CRITERIA",
        )

    expanded: dict[str, Any] = {}

    files = acceptance_criteria.get("files")
    if files is not None:
        if isinstance(files, str):
            files = [files]
        elif isinstance(files, dict):
            # 包装 dict 走样（LLM 常见发射形态）→ 与裸字符串同梯级容错：
            # 单键通用键对象（{"item": 路径}）或数字键顺序对象（{"0": a,...}）
            # 解包收编；多键/空洞键/dict 值意图不明，仍走下方形态闸拒绝。
            if len(files) == 1:
                only = next(iter(files.values()))
                if isinstance(only, list):
                    files = only
                elif isinstance(only, (str, int, float, bool)):
                    files = [only]
            elif files and all(k.isdigit() for k in files):
                keys = sorted(int(k) for k in files)
                if keys == list(range(len(files))):
                    files = [files[str(k)] for k in keys]
        if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
            return {}, _invalid_flat_field("files", "非空字符串数组（产出文件相对路径）")
        for path in files:
            clean = path.strip()
            expanded[f"file_check{_FLAT_INSTANCE_SEP}{clean}"] = {"input_params": {"path": clean, "check": "not_empty"}}

    expect = acceptance_criteria.get("expect")
    if expect is not None:
        if not isinstance(expect, str):
            return {}, _invalid_flat_field("expect", "非空字符串（验收期望的白话描述）")
        if expect.strip():
            expanded["semantic_check"] = {"input_params": {"expected": expect}}

    command = acceptance_criteria.get("command")
    if command is not None:
        if not isinstance(command, str):
            return {}, _invalid_flat_field("command", "非空字符串（验收命令，退出码 0=通过）")
        if command.strip():
            expanded["bash_check"] = {"input_params": {"command": command}}

    if not expanded:
        available = _metric_required_examples(sorted(_load_metric_definitions().keys()))
        menu = f"当前可用指标及必填参数：{available}。" if available else ""
        logger.warning(
            "[TaskSubmit] acceptance_criteria 白话字段全空，拒绝提交 | flat=%s",
            flat_present,
        )
        return {}, create_failure_result(
            error=(
                "acceptance_criteria 白话字段全部为空，没有任何可执行的验收标准。"
                "可用白话字段：files（产出文件相对路径数组，逐文件检查存在且非空）、"
                "command（验收命令，退出码 0 = 通过）。" + menu
            ),
            error_code="EMPTY_ACCEPTANCE_CRITERIA",
        )
    return expanded, None


def _metric_required_examples(metric_ids: list[str]) -> str:
    """按指标定义动态生成必填参数示例（单源 = evaluation_metrics.yaml）。

    示例形如 ``file_check 需要 {"path": "<文件或目录路径>"}``；占位说明取自
    input_schema.properties.<field>.description。定义增删改自动跟随，永不与
    yaml 漂移。定义缺失/无必填的指标不生成条目。
    """
    definitions = _load_metric_definitions()
    examples: list[str] = []
    for metric_id in metric_ids:
        schema = (definitions.get(metric_id) or {}).get("input_schema") or {}
        required = schema.get("required") or []
        if not required:
            continue
        properties = schema.get("properties") or {}
        sample = {field: f"<{(properties.get(field) or {}).get('description') or field}>" for field in required}
        examples.append(f"{metric_id} 需要 {json.dumps(sample, ensure_ascii=False)}")
    return "；".join(examples)


def _validate_required_metric_params(
    acceptance_criteria: dict[str, Any],
) -> ToolExecutionResult | None:
    """校验各指标的必填 input_params（数据源 = 指标定义 input_schema.required）。

    缺必填参数返回失败信封（INVALID_METRIC_PARAMS，逐指标列出缺失字段，
    修复示例由同一份定义动态生成），全部齐备返回 None。校验纯数据驱动——
    新增/修改指标定义自动生效。
    """
    definitions = _load_metric_definitions()
    if not definitions:
        return None
    problems: list[str] = []
    problem_ids: list[str] = []
    for metric_id, config in acceptance_criteria.items():
        schema = (definitions.get(_metric_definition_id(metric_id)) or {}).get("input_schema") or {}
        required = schema.get("required") or []
        if not required:
            continue
        params = config.get("input_params") if isinstance(config, dict) else None
        params = params if isinstance(params, dict) else {}
        missing = [field for field in required if params.get(field) in (None, "")]
        if missing:
            problem_ids.append(metric_id)
            problems.append(f"{metric_id} 缺必填参数 {missing}")
    if not problems:
        return None
    logger.warning(
        "[TaskSubmit] acceptance_criteria 必填 input_params 缺失，拒绝提交 | problems=%s",
        problems,
    )
    examples = _metric_required_examples(problem_ids)
    hint = f"（如 {examples}）" if examples else ""
    return create_failure_result(
        error=(
            "acceptance_criteria 的评估指标缺少必填 input_params："
            + "；".join(problems)
            + "。缺失字段会在评估期每轮失败且无法自愈，"
            "请在对应指标的 input_params 中补齐后重新提交" + hint + "。"
        ),
        error_code="INVALID_METRIC_PARAMS",
    )


# 单花括号标识符占位符（全匹配，排除正则量词 \d{3}、format 规格 {:.2f} 等形态）
_PLACEHOLDER_NAME_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
# 双花括号评估期可解析形态（{{workspace}}/{{task_id}}），扫描前整体剥除
_DOUBLE_BRACE_RE = re.compile(r"\{\{[^{}]*\}\}")
# 评估期可解析的占位符白名单：{tool_id}=工作区唯一工具候选裁决
# （task_evaluate._apply_tool_id_templates）、{title}=任务标题
# （task_evaluate._inject_workspace_and_placeholders）
_RESOLVABLE_PLACEHOLDERS = frozenset({"tool_id", "title"})


def _reject_unresolvable_placeholders(
    acceptance_criteria: dict[str, Any],
) -> ToolExecutionResult | None:
    """拒绝含评估期无法解析占位符的验收参数（fail-closed）。

    派发侧把 agent 配置/编排映射表的路径模板（{output_path}/{agent_id}/
    {novel_title}/{chapter_number}/{chapter_title} 等）字面照抄进
    acceptance_criteria 时，评估期无人替换——file_check 永久查字面量路径
    必败，且参数烙在出生 state 里 LLM 无法自修，必须提交期拒绝并明示
    改写为具体路径。递归扫描 input_params 的字符串值（含嵌套 dict/list）。
    """
    found: dict[str, list[str]] = {}

    def _scan(metric_id: str, value: Any, where: str) -> None:
        if isinstance(value, str):
            stripped = _DOUBLE_BRACE_RE.sub("", value)
            for name in _PLACEHOLDER_NAME_RE.findall(stripped):
                if name not in _RESOLVABLE_PLACEHOLDERS:
                    found.setdefault(name, []).append(f"{metric_id}.{where}")
        elif isinstance(value, dict):
            for k, v in value.items():
                _scan(metric_id, v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _scan(metric_id, v, f"{where}[{i}]")

    for metric_id, config in acceptance_criteria.items():
        if isinstance(config, dict):
            for key, value in config.items():
                _scan(metric_id, value, key)
    if not found:
        return None
    detail = "；".join(f"{{{name}}} ← {locs}" for name, locs in sorted(found.items()))
    logger.warning(
        "[TaskSubmit] acceptance_criteria 含不可解析占位符，拒绝提交 | found=%s",
        sorted(found),
    )
    return create_failure_result(
        error=(
            "acceptance_criteria 含评估期无法解析的占位符：" + detail + "。"
            "路径模板需在派发时改写为具体路径（写入 goal 的同一具体值）；"
            f"可用占位符仅 {sorted(_RESOLVABLE_PLACEHOLDERS)}"
            "（{tool_id}=工作区唯一工具、{title}=任务标题）。请改写后重新提交。"
        ),
        error_code="UNRESOLVED_PLACEHOLDER",
    )
