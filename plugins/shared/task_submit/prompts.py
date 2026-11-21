"""提示词构建域（评估标准提示 / workspace 引导文案 / 描述归一化）（自 tool.py 按职责拆分）。"""
from __future__ import annotations

import json
from typing import Any

from acceptance import _load_metric_definitions, _metric_definition_id


def _normalize_description(value: Any) -> str:
    """将 LLM 返回的 description 归一化为 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value)


# ── 派发指令构建 ──
#
# task_submit 提交即派发（chat.send_message create 分支），把描述/验收标准/
# 工作空间提示/路径规则拼成完整输入注入下级：
# - 描述：派发消息「\n任务描述：」段；
# - 重试纠正信息：由 task_manage continue 注入消息承载（此处不重复构建）；
# - 评估指标详情 / 工作空间模式提示 / 路径使用规则：
#   按 evaluation_metrics.yaml 字段与拓扑口径展开。
#   （进度跟踪工作法注入已退役：2026-10-25 用户裁定——待办由 todo 插件跟踪，
#   派发任务不携带清单流程说明。）

_EVALUATION_PROMPT_HEADER = "评估指标详情（你的产出将被以下标准评估）："


def _build_evaluation_criteria_prompt(acceptance_criteria: dict[str, Any]) -> str:
    """按指标定义展开验收标准为可读的评估说明文本。

    指标定义（evaluation_metrics.yaml）只有 name/description/evaluator_type/
    input_schema，没有 expect/is_red_line 等判定字段——按现有字段展开
    （说明 + 评估参数），判定逻辑归 task_evaluate。

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
        definition = definitions.get(_metric_definition_id(metric_id))
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
    """按 execution_context 工作空间声明生成场景提示与路径规则。

    - 继承坐标（inherit_from 命中）：空间事实由机制给出——源任务 id、工作
      目录、分支、基座逐项写明（问题 6：机制回填事实，不让模型自行断言
      "工作空间已保留"这类未经机制确认的陈述）；
    - 显式 workspace：worktree=源项目隔离副本（改完自动合并回源）/ plain=直接
      操作目标目录；shared 态（父任务空间）由子任务继承表达，不单独提示；
    - 无显式 workspace：任务在默认隔离目录执行（工作空间根/{task_id}）。
    系统自动管理路径，下级只用相对路径。
    """
    ws_spec = ec.get("workspace") if isinstance(ec, dict) else None
    if not isinstance(ws_spec, dict):
        return ""
    mode = ws_spec.get("mode") or "plain"
    inherit = ws_spec.get("inherit")
    if isinstance(inherit, dict) and inherit.get("source_task_id"):
        scene = (
            f"你接续任务 {inherit['source_task_id']} 的工作空间继续执行："
            f"工作目录 {inherit.get('path') or '-'}，"
            f"分支 {inherit.get('branch') or '-'}，"
            f"基座 {inherit.get('project_root') or '-'}。"
            "以上坐标由系统从源任务整份复制而来（上轮文件与分支状态原样保留），"
            "任务描述中关于工作空间的表述以本段事实为准。"
            "上下文不继承（本次重新开始），需要的历史结论请自行从工作目录读取。"
        )
    elif ws_spec.get("explicit"):
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


# 任务提交工具的 OpenAI Function Calling schema 字面量（纯声明数据，
# 与 get_tool_definition 拆离便于 review/diff 与外部对账）。
