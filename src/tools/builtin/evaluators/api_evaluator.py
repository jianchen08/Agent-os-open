"""
API 评估器

检查 API 响应状态码、Schema 等
"""

from typing import Any

import aiohttp

from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class ApiEvaluator:
    """API 评估器"""

    DEFAULT_TIMEOUT = 30  # 默认超时时间（秒）

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="api_evaluator",
            description="API 评估器：检查响应状态码、Schema",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "API URL",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                        "default": "GET",
                    },
                    "check": {
                        "type": "string",
                        "enum": ["status", "schema"],
                        "description": "检查类型",
                        "default": "status",
                    },
                    "expected_status": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "期望的状态码",
                        "default": [200],
                    },
                    "schema": {
                        "type": "object",
                        "description": "期望的响应 Schema",
                    },
                    "headers": {
                        "type": "object",
                        "description": "请求头",
                    },
                    "body": {
                        "description": "请求体",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "超时时间（秒）",
                        "default": 30,
                    },
                },
                "required": ["url"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=False,
            tags=["evaluator", "api", "http"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行 API 检查"""
        url = inputs.get("url")
        method = inputs.get("method", "GET").upper()
        check = inputs.get("check", "status")

        if not url:
            return create_failure_result(
                error="URL 不能为空",
                error_code="MISSING_URL",
            )

        if check == "status":
            return await self._check_status(url, method, inputs)
        if check == "schema":
            return await self._check_schema(url, method, inputs)

        return create_failure_result(error=f"不支持的检查类型: {check}")

    async def _check_status(
        self,
        url: str,
        method: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """检查状态码"""
        expected = inputs.get("expected_status", [200])
        headers = inputs.get("headers", {})
        body = inputs.get("body")
        timeout = inputs.get("timeout", self.DEFAULT_TIMEOUT)

        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {
                    "headers": headers,
                    "timeout": aiohttp.ClientTimeout(total=timeout),
                }
                if body and method in ("POST", "PUT", "PATCH"):
                    kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    status = resp.status
                    passed = status in expected

                    return create_success_result(
                        data={
                            "passed": passed,
                            "score": 100 if passed else 0,
                            "feedback": (
                                f"状态码 {status} 符合预期"
                                if passed
                                else f"状态码 {status} 不在预期范围 {expected}"
                            ),
                            "details": {
                                "status": status,
                                "expected": expected,
                                "url": url,
                                "method": method,
                            },
                        }
                    )
        except aiohttp.ClientError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"请求失败: {str(e)}",
                    "details": {"error": str(e)},
                }
            )
        except TimeoutError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"请求超时（{timeout}s）",
                }
            )

    async def _check_schema(
        self,
        url: str,
        method: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """检查响应 Schema"""
        import jsonschema

        schema = inputs.get("schema")
        if not schema:
            return create_failure_result(error="Schema 不能为空")

        headers = inputs.get("headers", {})
        body = inputs.get("body")
        timeout = inputs.get("timeout", self.DEFAULT_TIMEOUT)

        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {
                    "headers": headers,
                    "timeout": aiohttp.ClientTimeout(total=timeout),
                }
                if body and method in ("POST", "PUT", "PATCH"):
                    kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    if resp.status >= 400:
                        return create_success_result(
                            data={
                                "passed": False,
                                "score": 0,
                                "feedback": f"请求失败，状态码: {resp.status}",
                            }
                        )

                    try:
                        data = await resp.json()
                    except Exception:
                        return create_success_result(
                            data={
                                "passed": False,
                                "score": 0,
                                "feedback": "响应不是有效的 JSON",
                            }
                        )

                    # 验证 Schema
                    try:
                        jsonschema.validate(instance=data, schema=schema)
                        return create_success_result(
                            data={
                                "passed": True,
                                "score": 100,
                                "feedback": "响应符合 Schema",
                                "details": {"response": data},
                            }
                        )
                    except jsonschema.ValidationError as e:
                        return create_success_result(
                            data={
                                "passed": False,
                                "score": 0,
                                "feedback": f"Schema 验证失败: {e.message}",
                                "details": {
                                    "path": list(e.path),
                                    "message": e.message,
                                },
                            }
                        )
        except aiohttp.ClientError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"请求失败: {str(e)}",
                }
            )
        except TimeoutError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"请求超时（{timeout}s）",
                }
            )
