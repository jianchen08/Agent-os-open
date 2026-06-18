"""
Web 操作工具

暴露接口：
- from_config(cls, config_path: str | None) -> 'WebTool'：from_config功能
- get_tool_definition() -> Tool：get_tool_definition功能
- handle_data(self, data)：handle_data功能
- WebTool：WebTool类
"""

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# Suppress trafilatura's verbose logging
for _traf_logger in ("trafilatura", "trafilatura.utils", "trafilatura.core"):
    logging.getLogger(_traf_logger).setLevel(logging.CRITICAL)


class WebTool(BuiltinTool):
    """
    Web 操作工具

    提供：
    - HTTP GET/POST 请求
    - 网页内容抓取
    - 下载文件
    """

    DEFAULT_CONFIG_PATH = "config/tools/web/fetch.yaml"

    def __init__(
        self,
        timeout: int = 30,
        max_response_size: int = 10 * 1024 * 1024,  # 10MB
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        verify_ssl: bool = True,
    ):
        """初始化 Web 工具"""
        self.timeout = timeout
        self.max_response_size = max_response_size
        self.allowed_domains = set(allowed_domains) if allowed_domains else None
        self.blocked_domains = set(blocked_domains or [])
        self.verify_ssl = verify_ssl
        self._proxy_url: str | None = None
        # Check proxy environment variables
        for env_var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            proxy = os.environ.get(env_var)
            if proxy:
                self._proxy_url = proxy
                break

    @classmethod
    def from_config(cls, config_path: str | None = None) -> "WebTool":
        """从配置文件创建 WebTool 实例（通过 ConfigCenter 统一缓存）"""
        from config.config_center import get_config_center
        rel = (config_path or cls.DEFAULT_CONFIG_PATH).replace("config/", "", 1)
        try:
            config = get_config_center().get(rel) or {}
            if not config:
                logger.warning(f"[WebTool] 配置不存在: {rel}，使用默认配置")
                return cls()

            instance = cls(
                timeout=config.get("timeout", 30),
                allowed_domains=config.get("allowed_domains"),
                blocked_domains=config.get("blocked_domains"),
                verify_ssl=config.get("verify_ssl", True),
            )

            logger.info(
                f"[WebTool] 从配置文件加载成功 | "
                f"blocked_domains={len(instance.blocked_domains)}"
            )
            return instance

        except Exception as e:
            logger.error(f"[WebTool] 加载配置文件失败: {e}，使用默认配置")
            return cls()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="fetch",
            description="Web 操作工具：执行 HTTP 请求和网页抓取。支持 GET/POST 请求和网页内容抓取。使用场景：调用外部 API；抓取网页内容；发送 HTTP 请求获取远程数据。限制：请求默认 30 秒超时；响应大小限制为 10MB；某些域名可能被安全策略阻止。",
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
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "integer"},
                    "data": {},
                },
                "required": ["status"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.WEB,
            tags=["web", "http", "scrape"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
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
        if action == "post":
            return await self._http_post(inputs)
        if action == "fetch":
            return await self._fetch_page(inputs)
        return create_failure_result(
            error=f"不支持的操作: {action}",
            error_code="INVALID_ACTION",
        )

    @staticmethod
    def _http_recovery_hint(status_code: int) -> str:
        """根据 HTTP 状态码返回错误恢复建议。"""
        hints = {
            403: "\n建议：该网站拒绝访问，请尝试其他来源或使用 web_search 搜索替代信息。",
            404: "\n建议：页面不存在，请使用 web_search 搜索替代来源。",
        }
        return hints.get(status_code, "")

    def _check_url_security(self, url: str) -> tuple[bool, str | None]:
        """检查 URL 安全性"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # 移除端口号
            if ":" in domain:
                domain = domain.split(":")[0]

            # 检查协议
            if parsed.scheme not in ["http", "https"]:
                return False, f"不支持的协议: {parsed.scheme}"

            # 检查禁止域名（支持子域名匹配）
            # BUG-FIX-fix_20260316: 修复域名检查逻辑误判问题
            # 问题根因: 使用 `in` 操作符会误判，如 blocked="spam.com" 会匹配 "notspam.com"
            # 修复方案: 使用精确匹配或子域名后缀匹配
            for blocked in self.blocked_domains:
                if domain == blocked or domain.endswith("." + blocked):
                    return False, f"域名在禁止列表中: {domain}"

            # 检查允许列表（支持子域名匹配）
            if self.allowed_domains is not None:
                is_allowed = any(
                    domain == allowed or domain.endswith("." + allowed)
                    for allowed in self.allowed_domains
                )
                if not is_allowed:
                    return False, f"域名不在允许列表中: {domain}"

            return True, None

        except Exception as e:
            return False, f"URL 解析失败: {str(e)}"

    async def _http_get(self, inputs: dict[str, Any]) -> ToolResult:
        """HTTP GET 请求"""
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            params = inputs.get("params", {})
            timeout = inputs.get("timeout", self.timeout)

            async with httpx.AsyncClient(verify=self.verify_ssl, proxy=self._proxy_url) as client:
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
                    # 非 JSON：检测 HTML 并提取文本，节省 LLM token
                    text = content.decode("utf-8", errors="ignore")
                    if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
                        try:
                            import trafilatura
                            extracted = trafilatura.extract(
                                text,
                                include_tables=True,
                                include_links=False,
                                include_formatting=False,
                                favor_precision=True,
                            )
                            data = extracted or text[:2000]
                        except Exception:
                            data = text[:2000]
                    else:
                        data = text

                if response.status_code >= 400:
                    hint = self._http_recovery_hint(response.status_code)
                    return create_failure_result(
                        error=f"HTTP {response.status_code}: {data if isinstance(data, str) else str(data)[:500]}{hint}",
                        error_code=f"HTTP_{response.status_code}",
                    )

                return create_success_result(
                    data={
                        "status": response.status_code,
                        "data": data,
                    },
                    metadata={"action": "http_get", "url": str(url)},
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
        """HTTP POST 请求"""
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            data = inputs.get("data", {})
            params = inputs.get("params", {})
            timeout = inputs.get("timeout", self.timeout)

            async with httpx.AsyncClient(verify=self.verify_ssl, proxy=self._proxy_url) as client:
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
                    # 非 JSON：检测 HTML 并提取文本，节省 LLM token
                    text = content.decode("utf-8", errors="ignore")
                    if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
                        try:
                            import trafilatura
                            extracted = trafilatura.extract(
                                text,
                                include_tables=True,
                                include_links=False,
                                include_formatting=False,
                                favor_precision=True,
                            )
                            data = extracted or text[:2000]
                        except Exception:
                            data = text[:2000]
                    else:
                        data = text

                if response.status_code >= 400:
                    hint = self._http_recovery_hint(response.status_code)
                    return create_failure_result(
                        error=f"HTTP {response.status_code}: {data if isinstance(data, str) else str(data)[:500]}{hint}",
                        error_code=f"HTTP_{response.status_code}",
                    )

                return create_success_result(
                    data={
                        "status": response.status_code,
                        "data": data,
                    },
                    metadata={"action": "http_post", "url": str(url)},
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
        """抓取网页内容"""
        try:
            url = inputs["url"]
            headers = inputs.get("headers", {})
            timeout = inputs.get("timeout", self.timeout)
            extract_text = inputs.get("extract_text", True)

            async with httpx.AsyncClient(verify=self.verify_ssl, proxy=self._proxy_url) as client:
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
                    "status": response.status_code,
                }

                # 如果需要提取文本
                if extract_text:
                    import trafilatura

                    extracted = trafilatura.extract(
                        html_text,
                        include_tables=True,
                        include_links=False,
                        include_formatting=False,
                        favor_precision=True,
                    )
                    if extracted:
                        result_data["text"] = extracted
                    else:
                        result_data["text"] = ""
                        logger.warning(f"[WebTool] trafilatura 未能提取正文: {url}")
                else:
                    result_data["html"] = html_text

                if response.status_code >= 400:
                    hint = self._http_recovery_hint(response.status_code)
                    return create_failure_result(
                        error=f"HTTP {response.status_code}{hint}",
                        error_code=f"HTTP_{response.status_code}",
                    )

                return create_success_result(
                    data=result_data,
                    metadata={"action": "fetch_page", "url": str(url)},
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
