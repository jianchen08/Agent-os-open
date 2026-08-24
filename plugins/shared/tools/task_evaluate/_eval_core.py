"""评估核心类型（0.2 自包含版）。

0.1 的 ``evaluation.types`` / ``evaluation.executor``（归档于
reference/0.1_src/evaluation/，src/ 已删）在 0.2 未迁移为可平铺导入模块——
执行面由本目录 ``_executor.py`` 的 ``PipelineEvaluationExecutor`` 真实承载
（2026-08-24 批次B：tool 型本地执行 + agent 型派评估子管道继承任务工作区）；
evaluation_service 插件保留指标注册表/HTTP 读面（执行面已收编，不再双头）。
本模块就地重建 task_evaluate 工具所需的最小类型面（仿 media/_media_core.py
的「0.2 自包含 + 类型面 + 注入 duck-typing」模式）：

- ``MetricResult`` / ``EvaluationResult``：评估结果数据类（字段与 0.1 对齐）
- ``sanitize_eval_paths``：递归脱敏绝对路径（语义与 0.1 对齐，防止服务器
  内部路径信息泄漏）
- ``EvaluationExecutor``：执行器类型面（生产实现见 ``_executor.py``；本类
  仅保留 duck-typing 契约与空默认行为）

本模块自包含（仅标准库），由 task_evaluate 插件目录以平铺模块方式导入。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["EvaluationExecutor", "EvaluationResult", "MetricResult", "sanitize_eval_paths"]

_CWD_ABS = str(Path.cwd()).replace("\\", "/") + "/"
_CWD_ABS_WIN = str(Path.cwd()) + "\\" if os.name == "nt" else None


def sanitize_eval_paths(data: Any) -> Any:  # noqa: PLR0912
    """递归脱敏评估数据中的绝对路径，将其替换为相对路径。

    遍历字典/列表中所有字符串值，检测包含当前工作目录的绝对路径
    并将其转换为相对路径，防止服务器内部路径信息泄漏。

    Args:
        data: 待脱敏的数据（字典、列表或标量）

    Returns:
        脱敏后的数据（原地 dict/list 会被修改并返回）
    """
    if isinstance(data, dict):
        for key in data:
            data[key] = sanitize_eval_paths(data[key])
        return data
    if isinstance(data, list):
        for i in range(len(data)):
            data[i] = sanitize_eval_paths(data[i])
        return data
    if isinstance(data, str):
        result = data
        if _CWD_ABS_WIN and _CWD_ABS_WIN in result:
            result = result.replace(_CWD_ABS_WIN, "")
        if _CWD_ABS in result:
            result = result.replace(_CWD_ABS, "")
        win_drive_pattern = re.compile(r"[A-Za-z]:\\[^\s\"']*")
        if win_drive_pattern.search(result):
            for m in win_drive_pattern.finditer(result):
                abs_path = m.group()
                try:
                    rel = os.path.relpath(abs_path).replace("\\", "/")
                    result = result.replace(abs_path, rel)
                except ValueError:
                    pass
        posix_abs_pattern = re.compile(r"/(?:home|root|opt|var|tmp|usr)/[^\s\"']*")
        if posix_abs_pattern.search(result):
            for m in posix_abs_pattern.finditer(result):
                abs_path = m.group()
                try:
                    rel = os.path.relpath(abs_path).replace("\\", "/")
                    result = result.replace(abs_path, rel)
                except ValueError:
                    pass
        return result
    return data


@dataclass
class MetricResult:
    """单个指标的评估结果（字段与 0.1 evaluation.types.MetricResult 对齐）。

    Attributes:
        metric_id: 对应的指标 ID
        passed: 评估是否通过
        score: 评分（0-100），-1 表示不支持评分
        message: 结果消息
        details: 详细评估数据
        error: 评估过程中的错误信息
        evaluator_input: 评估器接收的输入参数
        evaluator_output: 评估器的原始输出
        pipeline_run_id: Agent 评估时子管道的运行 ID，仅 agent 类型有值
    """

    metric_id: str
    passed: bool = False
    score: float = -1.0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    evaluator_input: dict[str, Any] = field(default_factory=dict)
    evaluator_output: dict[str, Any] = field(default_factory=dict)
    pipeline_run_id: str | None = None


@dataclass
class EvaluationResult:
    """一次评估的完整结果（字段与 0.1 evaluation.types.EvaluationResult 对齐）。

    Attributes:
        task_id: 关联的任务 ID
        results: 各指标的评估结果
        overall_passed: 综合是否通过
        summary: 评估摘要
    """

    task_id: str
    results: list[MetricResult] = field(default_factory=list)
    overall_passed: bool = False
    summary: str = ""

    def compute_overall(self) -> None:
        """根据各指标结果计算综合判定（与 0.1 语义对齐）。"""
        if not self.results:
            self.overall_passed = False
            self.summary = "无评估指标"
            return
        self.overall_passed = all(r.passed for r in self.results)
        passed_count = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        if self.overall_passed:
            self.summary = f"全部 {total} 项指标通过"
        else:
            self.summary = f"{passed_count}/{total} 项指标通过"


class EvaluationExecutor:
    """评估执行器类型面（0.2 注入版，duck-typing）。

    0.1 ``evaluation.executor.EvaluationExecutor`` 完整实现未迁移（其依赖 0.1
    评估引擎 EvaluationEngine / MetricLoader / ResultMapper）；本类仅提供类型面
    与空默认行为。运行时实例由外部注入（宿主或测试），需实现：

    .. code-block:: python

        async def run_evaluation(
            task_id: str,
            metric_ids: list[str] | None = None,
            input_params: dict[str, dict[str, Any]] | None = None,
            fail_fast: bool = True,
            skip_state_update: bool = False,
        ) -> EvaluationResult
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """类型面占位（无注入时为空实现）。"""

    async def run_evaluation(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        input_params: dict[str, dict[str, Any]] | None = None,
        fail_fast: bool = True,
        skip_state_update: bool = False,
    ) -> EvaluationResult:
        """空默认行为：未注入时抛 RuntimeError（由调用方转为明确错误）。"""
        raise RuntimeError("评估执行器未注入（0.2 评估引擎不可用）")
