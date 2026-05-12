"""
Web 操作工具

提供 HTTP 请求和网页抓取功能
"""

from typing import Any
from urllib.parse import urlparse

import httpx

from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class WebTool:
    """
    Web 操作工具

    提供：
    - HTTP GET/POST 请求
    - 网页内容抓取
    - 下载文件
    """

    def __init__(
        self,
        timeout: int = 30,
        max_response_size: int = 10 * 1024 * 1024,  # 10MB
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ):
        """
        初始化 Web 工具

        Args:
            timeout: 请求超时时间（秒）
            max_response_size: 最大响应大小（字节）
            allowed_domains: 允许访问的域名列表
            blocked_domains: 禁止访问的域名列表
        """
        self.timeout = timeout
        self.max_response_size = max_response_size
        self.allowed_domains = set(allowed_domains) if allowed_domains else None
        self.blocked_domains = set(blocked_domains or [])

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        return Tool(
            name="web_operate",
            description="Web 操作工具：执行 HTTP 请求和网页抓取。支持 GET/POST 请求和网页内容抓取。使用场景：调用外部 API；抓取网页内容；发送 HTTP 请求获取远程数据。限制：请求默认 30 秒超时；响应大小限制为 10MB；某些域名可能被安全策略阻止；需要审批才能执行。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "post", "fetch"],
                        "description": "操作类型：get（HTTP GET 请求）、post（HTTP POST 请求）、fetch（抓取网页并提取内容）",
                    },
                    "url": {
                        "type": "string",
                        "description": "目标 URL，支持 http 和 https 协议",
                    },
                    "headers": {
                        "type": "object",
                        "description": "请求头（可选），自定义 HTTP 请求头，如 {'Authorization': 'Bearer token'}",
                    },
                    "data": {
                        "type": "object",
                        "description": "POST 请求体数据（可选），仅在 action 为 post 时使用，将作为 JSON 发送",
                    },
                    "params": {
                        "type": "object",
                        "description": "URL 查询参数（可选），将自动附加到 URL 后",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒，可选），默认 30 秒",
                        "default": 30,
                    },
                    "extract_text": {
                        "type": "boolean",
                        "description": "是否提取纯文本（可选，仅在 fetch 时有效），默认 true，将去除 HTML 标签返回纯文本",
                        "default": True,
                    },
                },
                "required": ["action", "url"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.WEB,
            requires_approval=True,  # 需要审批
            tags=["web", "http", "scrape"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行工具

        Args:
            inputs: 输入参数

        Returns:
            执行结果
        """
        url = inputs.get("url")
        if not url:
            return create_failure_result(
                error="URL 不能为空",
                error_code="MISSING_URL",
            )

        # 安全检查
        is_safe, error_msg = self._check_url_security(url)
        if not is_safe:
            return create_failure_result(
                error=f"URL 安全检查失败: {error_msg}",
                error_code="SECURITY_CHECK_FAILED",
            )

        action = inputs.get("action")

        if action == "get":
            return await self._http_get(inputs)
        elif action == "post":
            return await self._http_post(inputs)
        elif action == "fetch":
            return await self._fetch_page(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    def _check_url_security(self, url: str) -> tuple[bool, str | None]:
        """
        检查 URL 安全性

        Args:
            url: URL 字符串

        Returns:
            (是否安全, 错误信息)
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # 检查协议
            if parsed.scheme not in ["http", "https"]:
                return False, f"不支持的协议: {parsed.scheme}"

            # 检查禁止域名
            for blocked in self.blocked_domains:
                if blocked in domain:
                    return False, f"域名在禁止列表中: {domain}"

            # 检查允许列表
            if self.allowed_domains is not None:
                if domain not in self.allowed_domains:
                    return False, f"域名不在允许列表中: {domain}"

            return True, None

        except Exception as e:
            return False, f"URL 解析失败: {str(e)}"

    async def _http_get(self, inputs: dict[str, Any]) -> ToolResult:
        """
        HTTP GET 请求

        Args:
            inputs: 输入参数

        Returns:
            请求结果
        """
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            params = inputs.get("params", {})
            timeout = inputs.get("timeout", self.timeout)

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=httpx.Timeout(timeout),
                )

                # 检查响应大小
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {content_length} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 读取响应
                content = response.content

                if len(content) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {len(content)} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 尝试解析为 JSON
                try:
                    data = response.json()
                except Exception:
                    # 如果不是 JSON，返回文本
                    data = content.decode("utf-8", errors="ignore")

                return create_success_result(
                    data={
                        "url": str(url),
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "data": data,
                    },
                    metadata={"action": "http_get"},
                )

        except httpx.TimeoutException:
            return create_failure_result(
                error="请求超时",
                error_code="TIMEOUT",
            )
        except httpx.HTTPError as e:
            return create_failure_result(
                error=f"HTTP 请求失败: {str(e)}",
                error_code="HTTP_ERROR",
            )
        except Exception as e:
            return create_failure_result(
                error=f"GET 请求失败: {str(e)}",
                error_code="GET_FAILED",
            )

    async def _http_post(self, inputs: dict[str, Any]) -> ToolResult:
        """
        HTTP POST 请求

        Args:
            inputs: 输入参数

        Returns:
            请求结果
        """
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            data = inputs.get("data", {})
            params = inputs.get("params", {})
            timeout = inputs.get("timeout", self.timeout)

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=data,
                    params=params,
                    timeout=httpx.Timeout(timeout),
                )

                # 检查响应大小
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {content_length} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 读取响应
                content = response.content

                if len(content) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {len(content)} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 尝试解析为 JSON
                try:
                    data = response.json()
                except Exception:
                    # 如果不是 JSON，返回文本
                    data = content.decode("utf-8", errors="ignore")

                return create_success_result(
                    data={
                        "url": str(url),
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "data": data,
                    },
                    metadata={"action": "http_post"},
                )

        except httpx.TimeoutException:
            return create_failure_result(
                error="请求超时",
                error_code="TIMEOUT",
            )
        except httpx.HTTPError as e:
            return create_failure_result(
                error=f"HTTP 请求失败: {str(e)}",
                error_code="HTTP_ERROR",
            )
        except Exception as e:
            return create_failure_result(
                error=f"POST 请求失败: {str(e)}",
                error_code="POST_FAILED",
            )

    async def _fetch_page(self, inputs: dict[str, Any]) -> ToolResult:
        """
        抓取网页内容

        Args:
            inputs: 输入参数

        Returns:
            网页内容
        """
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            timeout = inputs.get("timeout", self.timeout)
            extract_text = inputs.get("extract_text", True)

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=httpx.Timeout(timeout),
                )

                # 检查响应大小
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {content_length} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 读取 HTML
                html = response.content

                if len(html) > self.max_response_size:
                    return create_failure_result(
                        error=f"响应过大: {len(html)} 字节",
                        error_code="RESPONSE_TOO_LARGE",
                    )

                html_text = html.decode("utf-8", errors="ignore")

                result_data = {
                    "url": str(url),
                    "status_code": response.status_code,
                    "html": html_text,
                }

                # 如果需要提取文本
                if extract_text:
                    # 简单的文本提取（去除 HTML 标签）
                    from html.parser import HTMLParser

                    class TextExtractor(HTMLParser):
                        def __init__(self):
                            super().__init__()
                            self.text = []

                        def handle_data(self, data):
                            self.text.append(data)

                    parser = TextExtractor()
                    parser.feed(html_text)
                    text = " ".join(parser.text)
                    result_data["text"] = text

                return create_success_result(
                    data=result_data,
                    metadata={"action": "fetch_page"},
                )

        except httpx.TimeoutException:
            return create_failure_result(
                error="请求超时",
                error_code="TIMEOUT",
            )
        except httpx.HTTPError as e:
            return create_failure_result(
                error=f"HTTP 请求失败: {str(e)}",
                error_code="HTTP_ERROR",
            )
        except Exception as e:
            return create_failure_result(
                error=f"抓取网页失败: {str(e)}",
                error_code="FETCH_FAILED",
            )
