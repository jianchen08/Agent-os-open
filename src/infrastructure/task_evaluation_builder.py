"""评估提示构建 Mixin。

负责根据验收标准生成评估说明文本，规范化验收标准中的路径，
以及构建任务执行的完整输入字符串。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskEvaluationBuilderMixin:
    """评估提示构建混入类。

    提供 _build_evaluation_criteria_prompt、_normalize_acceptance_criteria_paths
    和 _build_full_task_input 方法，由 TaskWorker 通过多继承组合使用。
    """

    def _build_evaluation_criteria_prompt(  # noqa: PLR0912
        self,
        acceptance_criteria: dict[str, Any],
    ) -> str:
        """根据验收标准中的指标 ID 加载完整指标定义，生成可读的评估说明文本。

        将评估指标的完整信息（名称、描述、判断标准、通过/失败消息、是否红线）
        直接注入到执行 Agent 的 prompt 中，让 Agent 清楚知道自己会被怎么评估。

        Args:
            acceptance_criteria: 验收标准字典，key 为指标 ID，
                                 value 为 {"input_params": {...}, ...}

        Returns:
            格式化后的评估指标说明文本，无验收标准时返回空字符串
        """
        if not acceptance_criteria or not isinstance(acceptance_criteria, dict):
            return ""

        try:
            from evaluation.loader import MetricLoader  # noqa: PLC0415
        except ImportError:
            logger.debug("[TaskWorker] evaluation.loader 不可用，跳过指标原文注入")
            return ""

        try:
            loader = MetricLoader()
            loader.load_all()
        except Exception as e:
            logger.debug("[TaskWorker] MetricLoader 加载失败: %s", e)
            return ""

        parts: list[str] = []
        for metric_id, config in acceptance_criteria.items():
            metric_def = loader.get(metric_id)
            if not metric_def:
                continue

            lines: list[str] = []
            lines.append(f"### {metric_def.name}（{metric_id}）")
            lines.append(f"说明：{metric_def.description}")

            if config and isinstance(config, dict):
                input_params = config.get("input_params", {})
                if input_params:
                    params_desc = json.dumps(input_params, ensure_ascii=False, indent=2)
                    lines.append(f"评估参数：{params_desc}")

            expect = metric_def.expect
            if expect and expect.conditions:
                cond_strs = []
                for cond in expect.conditions:
                    if cond.operator in ("is_true", "is_false"):
                        cond_strs.append(f"{cond.field} 为 {'真' if cond.operator == 'is_true' else '假'}")
                    else:
                        cond_strs.append(f"{cond.field} {cond.operator} {cond.value}")
                logic_word = "且" if expect.logic == "and" else "或"
                lines.append(f"通过条件（{logic_word}）：{', '.join(cond_strs)}")

            if metric_def.is_red_line:
                lines.append("⚠️ 红线指标：未通过则任务直接失败")

            parts.append("\n".join(lines))

        if not parts:
            return ""

        header = "评估指标详情（你的产出将被以下标准评估）："
        return f"\n\n{header}\n\n" + "\n\n".join(parts)

    def _normalize_acceptance_criteria_paths(
        self,
        criteria: dict | list,
        workspace: str,
    ) -> dict | list:
        """递归规范化验收标准中的路径，转为相对于 workspace 的相对路径。

        将验收标准中的绝对/半绝对路径转换为相对于当前 workspace 的路径：
        - 以 workspace 开头的路径：去掉 workspace 前缀
        - workspace 的祖先路径：计算相对路径（如 ../task_plan.md）
        - 已经是相对路径：保持不变

        Args:
            criteria: 验收标准字典或列表
            workspace: 当前工作目录路径

        Returns:
            规范化后的验收标准字典或列表
        """
        workspace_normalized = workspace.replace("\\", "/").rstrip("/")
        from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

        _ws_root_name = Path(get_workspace_config_root()).name + "/"

        def _to_relative(value_normalized: str) -> str:
            if value_normalized.startswith(workspace_normalized + "/"):
                return value_normalized[len(workspace_normalized) + 1 :]
            if value_normalized == workspace_normalized:
                return "."
            if value_normalized.startswith(_ws_root_name) or (
                "/" in value_normalized and not value_normalized.startswith("/")
            ):
                try:
                    ws_parts = workspace_normalized.split("/")
                    val_parts = value_normalized.split("/")
                    common_len = 0
                    for i in range(min(len(ws_parts), len(val_parts))):
                        if ws_parts[i] == val_parts[i]:
                            common_len += 1
                        else:
                            break
                    up_count = len(ws_parts) - common_len
                    down_parts = val_parts[common_len:]
                    rel = "/".join([".."] * up_count + down_parts)
                    return rel or "."
                except Exception:
                    return value_normalized
            return value_normalized

        def _normalize_value(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: _normalize_value(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_normalize_value(item) for item in value]
            if isinstance(value, str):
                value_normalized = value.replace("\\", "/")
                if os.path.isabs(value_normalized):  # noqa: PTH117
                    return value_normalized
                return _to_relative(value_normalized)
            return value

        return _normalize_value(criteria)

    # ───────────────────────────────────────────────────────────────────
    # 任务执行输入构建
    # ───────────────────────────────────────────────────────────────────

    async def _build_full_task_input(
        self,
        task_id: str,
        task_data: dict[str, Any],
        workspace: str,
        ws_meta: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        explicit_workspace: str,
        task_service: Any,
    ) -> str:
        """构建任务执行的完整输入字符串。

        将用户输入、描述、重试信息、目标上下文、验收标准、工作空间提示等
        组合为完整的输入字符串，供 PipelineEngine 使用。

        Args:
            task_id: 任务 ID
            task_data: 任务提交事件中的数据字典
            workspace: 已解析的工作空间路径
            ws_meta: 生命周期钩子返回的工作空间元数据
            acceptance_criteria: 验收标准字典
            explicit_workspace: 任务显式指定的 workspace（原始值）
            task_service: 任务服务实例

        Returns:
            构建完成的完整输入字符串
        """
        user_input = task_data.get("user_input", "")
        description = task_data.get("description", "")

        is_default_workspace = not explicit_workspace

        # 读取 retry_message（由 TaskTool._retry_task 存入 metadata）
        retry_message = None
        if task_service:
            _task_for_retry_msg = task_service.get_task(task_id)
            if _task_for_retry_msg and _task_for_retry_msg.metadata:
                retry_message = _task_for_retry_msg.metadata.get("retry_message")
                if retry_message:
                    # 读取后清除，避免重试后再读到旧消息
                    _task_for_retry_msg.metadata.pop("retry_message", None)
                    await task_service.save_task(_task_for_retry_msg)

        full_input = user_input
        if description:
            full_input += f"\n\n详细描述：{description}"
        if retry_message:
            full_input += f"\n\n[重试纠正信息]：{retry_message}"
        # goal_context 信息不再注入到任务输入中，减少冗余输出
        if acceptance_criteria:
            acceptance_criteria = self._normalize_acceptance_criteria_paths(
                acceptance_criteria,
                workspace,
            )
            eval_prompt = self._build_evaluation_criteria_prompt(acceptance_criteria)
            if eval_prompt:
                full_input += eval_prompt

        if not is_default_workspace:
            full_input += "\n\n工作目录已设置（系统自动管理，无需关注具体路径）"
            full_input += (
                "\n\n路径使用规则（重要）："
                "\n- 所有文件操作使用相对路径即可，系统会自动拼接到工作目录"
                '\n- 示例：file_write(path="docs/report.md")'
            )

        # 注入场景化工作空间提示
        if ws_meta:
            _SCENE_PROMPTS = {  # noqa: N806
                "plain": "你在临时工作目录中执行任务。使用相对路径。完成后直接调用 task_evaluate",
                "worktree": "你在目标项目的隔离副本中执行任务。使用相对路径。修改不影响原始项目。可运行 pytest/mypy/lint。评估通过后系统自动合并回目标项目",
                "shared": "你在父任务的空间中执行任务。使用相对路径。完成后直接调用 task_evaluate",
            }
            _scene_hint = _SCENE_PROMPTS.get(ws_meta.get("mode", ""))
            if _scene_hint:
                full_input += f"\n\n工作空间模式提示：{_scene_hint}"

        # 注入待办工作法提示（服务于 agent 自身执行流程，不替代它）
        full_input += (
            "\n\n进度跟踪工作法（把你的执行过程展开成可见的待办，方便跟进）："
            "\n1. 把你 system_prompt 执行流程的每一步，按顺序展开成 `- [ ]` 待办清单"
            "\n2. 按该顺序推进，每完成一步标记 `- [x] ✅`"
            "\n3. 全部完成后调用 task_evaluate 提交评估"
            "\n说明：本条只规定「用待办清单推进」这一形式。任务描述里的具体要求"
            "（如约束、产出路径、评估标准）是你的硬指标，"
            "system_prompt 里的专业流程（如先加载技能、TDD 循环）是你的必经步骤，"
            "二者都不得因本待办工作法而跳过或简化。"
        )

        return full_input
