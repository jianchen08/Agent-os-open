"""
资源评估器

暴露接口：
- get_builtin_criteria(rtype: str) -> list[dict[str, Any]]：get_builtin_criteria功能
- merge_criteria(rtype: str, extra: list[dict[str, Any]], skip: bool) -> list[dict[str, Any]]：merge_criteria功能
- get_tool_definition() -> Tool：get_tool_definition功能
- ResourceEvaluator：ResourceEvaluator类
"""

import copy
from typing import Any

from tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from .schema_evaluator import SchemaEvaluator

TOOL_BUILTIN_CRITERIA = [
    {
        "id": "BUILTIN_TOOL_EXISTS",
        "description": "工具文件存在",
        "evaluator_type": "tool",
        "evaluator_id": "file_evaluator",
        "input_params": {"check": "exists"},
        "is_builtin": True,
    },
    {
        "id": "BUILTIN_TOOL_SYNTAX",
        "description": "Python语法正确",
        "evaluator_type": "tool",
        "evaluator_id": "code_evaluator",
        "input_params": {"check": "syntax", "language": "python"},
        "is_builtin": True,
    },
    {
        "id": "BUILTIN_TOOL_NOT_EMPTY",
        "description": "工具文件非空",
        "evaluator_type": "tool",
        "evaluator_id": "file_evaluator",
        "input_params": {"check": "not_empty", "min_size": 100},
        "is_builtin": True,
    },
]

AGENT_BUILTIN_CRITERIA = [
    {
        "id": "BUILTIN_AGENT_EXISTS",
        "description": "Agent配置文件存在",
        "evaluator_type": "tool",
        "evaluator_id": "file_evaluator",
        "input_params": {"check": "exists"},
        "is_builtin": True,
    },
    {
        "id": "BUILTIN_AGENT_YAML",
        "description": "YAML格式正确",
        "evaluator_type": "tool",
        "evaluator_id": "schema_evaluator",
        "input_params": {"format": "yaml"},
        "is_builtin": True,
    },
    {
        "id": "BUILTIN_AGENT_SCHEMA",
        "description": "配置符合Agent Schema",
        "evaluator_type": "tool",
        "evaluator_id": "schema_evaluator",
        "input_params": {
            "format": "schema",
            "schema": {
                "type": "object",
                "required": ["config_id", "name", "system_prompt"],
                "properties": {
                    "config_id": {"type": "string"},
                    "name": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "tool_ids": {"type": "array"},
                    "model_name": {"type": "string"},
                },
            },
        },
        "is_builtin": True,
    },
    {
        "id": "BUILTIN_AGENT_NOT_EMPTY",
        "description": "Agent配置文件非空",
        "evaluator_type": "tool",
        "evaluator_id": "file_evaluator",
        "input_params": {"check": "not_empty", "min_size": 50},
        "is_builtin": True,
    },
]


def get_builtin_criteria(rtype: str) -> list[dict[str, Any]]:
    """获取内置评估标准"""
    if rtype == "tool":
        return copy.deepcopy(TOOL_BUILTIN_CRITERIA)
    if rtype == "agent":
        return copy.deepcopy(AGENT_BUILTIN_CRITERIA)
    return []


def merge_criteria(
    rtype: str,
    extra: list[dict[str, Any]],
    skip: bool = False,
) -> list[dict[str, Any]]:
    """合并内置标准和额外标准"""
    builtin = [] if skip else get_builtin_criteria(rtype)
    return builtin + (extra or [])


class ResourceEvaluator:
    """资源评估器"""

    def __init__(self):
        """初始化"""
        self.schema_evaluator = SchemaEvaluator()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="resource_evaluator",
            description="资源评估器",
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["tool", "agent"],
                        "description": "资源类型",
                    },
                    "path": {
                        "type": "string",
                        "description": "资源文件路径",
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "额外验收标准",
                    },
                    "skip_builtin": {
                        "type": "boolean",
                        "description": "跳过内置检查",
                        "default": False,
                    },
                },
                "required": ["resource_type", "path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            tags=["evaluator", "resource"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行资源评估"""
        rtype = inputs.get("resource_type")
        path = inputs.get("path")
        extra = inputs.get("acceptance_criteria", [])
        skip = inputs.get("skip_builtin", False)

        if not rtype or not path:
            return create_failure_result(error="resource_type和path不能为空")

        if rtype not in ("tool", "agent"):
            return create_failure_result(error=f"不支持的资源类型: {rtype}")

        criteria = merge_criteria(rtype, extra, skip)

        if not criteria:
            return create_success_result(
                data={
                    "passed": True,
                    "overall_verdict": "pass",
                    "score": 100,
                    "feedback": "无评估标准，默认通过",
                    "checks": [],
                }
            )

        results = []
        for c in criteria:
            r = await self._eval_one(c, path)
            results.append(r)

        return self._summarize(results, rtype, path)

    async def _eval_one(self, criterion: dict[str, Any], path: str) -> dict[str, Any]:
        """执行单个评估"""
        cid = criterion.get("id", "unknown")
        desc = criterion.get("description", "")
        eid = criterion.get("evaluator_id")
        params = criterion.get("input_params", {})
        builtin = criterion.get("is_builtin", False)

        inputs = {"path": path, **params}

        try:
            result = await self._run_eval(eid, inputs)
            if result is None:
                return {
                    "criterion_id": cid,
                    "description": desc,
                    "passed": False,
                    "message": f"未知评估器: {eid}",
                    "is_builtin": builtin,
                }

            if result.success and result.output:
                passed = result.output.get("passed", False)
                msg = result.output.get("feedback", "")
                score = result.output.get("score", 0)
            else:
                passed = False
                msg = result.error or "评估失败"
                score = 0

            return {
                "criterion_id": cid,
                "description": desc,
                "passed": passed,
                "message": msg,
                "score": score,
                "evaluator_id": eid,
                "is_builtin": builtin,
            }

        except (ValueError, TypeError, KeyError) as e:
            return {
                "criterion_id": cid,
                "description": desc,
                "passed": False,
                "message": f"评估异常: {e}",
                "score": 0,
                "is_builtin": builtin,
            }

    async def _run_eval(self, eid: str, inputs: dict[str, Any]) -> ToolResult | None:
        """运行评估器"""
        if eid == "schema_evaluator":
            return await self.schema_evaluator.execute(inputs)

        return create_failure_result(error=f"评估器 {eid} 暂未实现")

    def _summarize(
        self,
        results: list[dict[str, Any]],
        rtype: str,
        path: str,
    ) -> ToolResult:
        """汇总评估结果"""
        passed_cnt = sum(1 for r in results if r["passed"])
        total = len(results)
        all_pass = all(r["passed"] for r in results)

        avg = sum(r.get("score", 0) for r in results) / total if total > 0 else 100

        fb = self._gen_feedback(results, rtype, all_pass)

        sugg = [f"修复 {r['criterion_id']}: {r['description']}" for r in results if not r["passed"]]

        if all_pass:
            verdict = "pass"
        elif passed_cnt >= total * 0.5:
            verdict = "partial"
        else:
            verdict = "fail"

        return create_success_result(
            data={
                "passed": all_pass,
                "overall_verdict": verdict,
                "score": int(avg),
                "feedback": fb,
                "checks": results,
                "improvement_suggestions": sugg,
                "resource_type": rtype,
                "resource_path": path,
            }
        )

    def _gen_feedback(
        self,
        results: list[dict[str, Any]],
        rtype: str,
        all_pass: bool,
    ) -> str:
        """生成反馈"""
        passed_cnt = sum(1 for r in results if r["passed"])
        total = len(results)

        if all_pass:
            return f"{rtype}资源评估通过（{passed_cnt}项全部通过）"

        failed = [r for r in results if not r["passed"]]
        lines = [
            f"{rtype}资源评估未通过",
            f"通过: {passed_cnt}/{total}",
            "失败项:",
        ]
        for r in failed:
            pre = "[内置]" if r.get("is_builtin") else "[自定义]"
            lines.append(f"  {pre} {r['criterion_id']}: {r['message']}")
        return "\n".join(lines)
