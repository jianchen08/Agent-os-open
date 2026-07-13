"""评估系统类型定义。

包含评估指标类型枚举、评估结果数据类和评估配置数据类，
供评估引擎、加载器、执行器等模块共同使用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

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
            str(Path.cwd())
            for m in posix_abs_pattern.finditer(result):
                abs_path = m.group()
                try:
                    rel = os.path.relpath(abs_path).replace("\\", "/")
                    result = result.replace(abs_path, rel)
                except ValueError:
                    pass
        return result
    return data


class EvaluatorType(Enum):
    """评估器类型枚举。

    每种类型对应一种评估器实现：
    - PROGRAMMATIC: 程序化评估（L1）
    - SEMANTIC: 语义评估（L2）
    - UNIFIED: 综合对比（L3）
    """

    PROGRAMMATIC = "programmatic"
    SEMANTIC = "semantic"
    UNIFIED = "unified"


class MetricType(Enum):
    """评估指标类型枚举。

    每种类型对应一种评估器实现：
    - tool: 工具调用型评估（bash/file/api/schema 等）
    - agent: LLM Agent 型评估（semantic_check）
    - human: 人工审核型评估（human_review）
    """

    TOOL = "tool"
    AGENT = "agent"
    HUMAN = "human"


@dataclass
class ExpectCondition:
    """期望条件定义。

    表示评估判断标准中的单个条件。

    Attributes:
        field: 结果字段路径（支持点号分隔的嵌套路径，如 "data.exit_code"）
        operator: 比较操作符（is_true/is_false/equals/not_equals/in/not_in/contains/gt/lt/gte/lte）
        value: 期望值（operator 为 is_true/is_false 时忽略）
    """

    field: str
    operator: str = "is_true"
    value: Any = None


@dataclass
class ExpectSpec:
    """期望判断标准。

    包含一组条件和组合逻辑，决定评估结果是否通过。

    Attributes:
        conditions: 条件列表
        logic: 组合逻辑（and/or）
        pass_message: 通过时的消息
        fail_message: 失败时的消息
    """

    conditions: list[ExpectCondition] = field(default_factory=list)
    logic: str = "and"
    pass_message: str = "评估通过"
    fail_message: str = "评估未通过"


@dataclass
class MetricDefinition:
    """评估指标定义。

    从 YAML 文件加载后的指标完整定义。

    Attributes:
        id: 指标唯一标识
        name: 指标名称
        description: 指标描述
        metric_type: 指标类型（tool/agent/human）
        evaluator_id: 评估器 ID
        default_config: 默认配置
        expect: 期望判断标准
        input_schema: 输入参数定义
        is_red_line: 是否为红线指标（必须通过）
        default_weight: 默认权重
        level: 评估层级
        includes: 包含的前置指标
        requires: 依赖的指标
        tags: 标签列表
        status: 指标状态
    """

    id: str
    name: str = ""
    description: str = ""
    metric_type: MetricType = MetricType.TOOL
    evaluator_id: str = ""
    default_config: dict[str, Any] = field(default_factory=dict)
    expect: ExpectSpec = field(default_factory=ExpectSpec)
    input_schema: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, Any] = field(default_factory=dict)
    is_red_line: bool = False
    default_weight: float = 1.0
    level: int = 1
    includes: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class MetricResult:
    """单个指标的评估结果。

    Attributes:
        metric_id: 对应的指标 ID
        passed: 评估是否通过
        score: 评分（0-100），-1 表示不支持评分
        message: 结果消息
        details: 详细评估数据
        error: 评估过程中的错误信息
        evaluator_input: 评估器接收的输入参数（合并默认配置后的完整参数）
        evaluator_output: 评估器的原始输出（评估器函数的返回值）
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
    """一次评估的完整结果。

    包含多个指标的评估结果和综合判定。

    Attributes:
        task_id: 关联的任务 ID
        results: 各指标的评估结果
        overall_passed: 综合是否通过（所有指标通过则通过）
        summary: 评估摘要
    """

    task_id: str
    results: list[MetricResult] = field(default_factory=list)
    overall_passed: bool = False
    summary: str = ""

    def compute_overall(self) -> None:
        """根据各指标结果计算综合判定。

        红线指标未通过则整体不通过；非红线指标按权重计算。
        当前简化版：所有指标通过则通过。无指标时判定为不通过。
        """
        if not self.results:
            self.overall_passed = False
            self.summary = "无评估指标"
            return

        self.overall_passed = all(r.passed for r in self.results)
        passed_count = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        self.summary = f"{passed_count}/{total} 指标通过"


@dataclass
class EvaluationConfig:
    """评估配置。

    定义评估执行时的参数。

    Attributes:
        metric_ids: 要执行的指标 ID 列表（空则执行所有已加载指标）
        input_params: 评估输入参数（键为指标 ID，值为参数字典）
        timeout: 单个指标评估超时时间（秒）
        fail_fast: 是否在首个指标失败时停止
    """

    metric_ids: list[str] = field(default_factory=list)
    input_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    timeout: float = 600.0
    fail_fast: bool = False
