"""
Web 操作工具

HTTP GET/POST 与网页正文抓取（trafilatura 抽取），支持域名黑白名单、
默认请求头与代理；SSRF 校验见 _check_url_security（原语来自共享层 url_security）。
"""

import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from url_security import is_private_ip, resolve_hostname_ips

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# Suppress trafilatura's verbose logging
for _traf_logger in ("trafilatura", "trafilatura.utils", "trafilatura.core"):
    logging.getLogger(_traf_logger).setLevel(logging.CRITICAL)

# 默认请求头：注入浏览器 UA，避免 httpx 默认的 python-httpx UA 被反爬网站 403 拒绝。
# 合并优先级：模块默认头 < 构造期 default_headers（fetch.yaml/调用方）< 单次请求 headers。
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


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
        default_headers: dict[str, str] | None = None,
    ):
        """初始化 Web 工具"""
        self.timeout = timeout
        self.max_response_size = max_response_size
        self.allowed_domains = set(allowed_domains) if allowed_domains else None
        self.blocked_domains = set(blocked_domains or [])
        self.verify_ssl = verify_ssl
        # 合并默认头：先复制模块默认头，再用构造期传入的覆盖/新增（不改原常量）。
        # 单次请求 headers 在 _merge_headers 中再次覆盖。
        self._default_headers: dict[str, str] = dict(_DEFAULT_HEADERS)
        if default_headers:
            self._default_headers.update(default_headers)
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
        from config.config_center import get_config_center  # noqa: PLC0415
        # P1-7 DEBT(task_11): 🟢 低危——WebTool 配置直读（缺失走默认配置 cls()）。
        # 迁移需 manifest 加 config_files + web_ext server 加 _on_load 注入。
        # 见 docs/working/p1_7_config_center_migration_checklist.md #12，延后 P6。

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
                default_headers=config.get("default_headers"),
            )

            logger.info(f"[WebTool] 从配置文件加载成功 | blocked_domains={len(instance.blocked_domains)}")
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
            for blocked in self.blocked_domains:
                if domain == blocked or domain.endswith("." + blocked):
                    return False, f"域名在禁止列表中: {domain}"

            # 检查允许列表（支持子域名匹配）
            if self.allowed_domains is not None:
                is_allowed = any(
                    domain == allowed or domain.endswith("." + allowed) for allowed in self.allowed_domains
                )
                if not is_allowed:
                    return False, f"域名不在允许列表中: {domain}"

            # F-TOOLS-MIG：SSRF 防护——DNS 解析后比对内网/回环/元数据 IP
            # （防触达 127.0.0.1 / localhost / 169.254.169.254 / 内网网段）
            ips, resolve_err = resolve_hostname_ips(domain)
            if resolve_err is not None:
                return False, f"SSRF 防护：域名解析失败: {resolve_err}"
            for ip in ips or []:
                if is_private_ip(ip):
                    return False, f"SSRF 防护：目标解析到内网/回环地址 {ip}"

            return True, None

        except Exception as e:
            return False, f"URL 解析失败: {str(e)}"

    def _merge_headers(self, caller_headers: dict[str, Any] | None) -> dict[str, str]:
        """合并请求头：默认头 + 调用方传入（调用方优先）。

        Args:
            caller_headers: 单次请求传入的 headers（可为空/None）。

        Returns:
            合并后的 headers 字典。默认头打底，调用方同 key 覆盖、新 key 追加。
        """
        merged = dict(self._default_headers)
        if caller_headers:
            merged.update(caller_headers)
        return merged

    # ── 响应处理公共层（_http_get/_http_post/_fetch_page 共用，行为不变） ──

    def _response_too_large_msg(self, response: httpx.Response) -> str | None:
        """响应大小检查：超限返回错误消息，未超限返回 None。

        先看声明值 Content-Length 头，再量实际 body（chunked 响应无声明值）。
        """
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > self.max_response_size:
            return f"响应过大: {content_length} 字节"
        if len(response.content) > self.max_response_size:
            return f"响应过大: {len(response.content)} 字节"
        return None

    def _extract_data(self, response: httpx.Response) -> Any:
        """解析响应 body：JSON 优先；HTML 用 trafilatura 抽正文；其余按原文本。

        get/post 共用（fetch_page 走独立的正文抽取路径，不经此方法）。
        """
        content = response.content
        try:
            return response.json()
        except Exception:
            # 非 JSON：检测 HTML 并提取文本，节省 LLM token
            text = content.decode("utf-8", errors="ignore")
            if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
                try:
                    import trafilatura  # noqa: PLC0415

                    extracted = trafilatura.extract(
                        text,
                        include_tables=True,
                        include_links=False,
                        include_formatting=False,
                        favor_precision=True,
                    )
                    return extracted or text[:2000]
                except Exception:
                    return text[:2000]
            return text

    def _http_error_result(self, response: httpx.Response, payload: str | None = None) -> ToolResult:
        """HTTP >= 400 的统一失败结果（payload 非 None 时以 `HTTP {code}: {payload}` 格式回显）。"""
        hint = self._http_recovery_hint(response.status_code)
        detail = f": {payload}" if payload is not None else ""
        return create_failure_result(
            error=f"HTTP {response.status_code}{detail}{hint}",
            error_code=f"HTTP_{response.status_code}",
        )

    def _request_failure(self, exc: Exception, verb: str, error_code: str) -> ToolResult:
        """请求异常统一尾部：超时 / HTTP 传输错误 / 其余按动作命名（行为不变）。"""
        if isinstance(exc, httpx.TimeoutException):
            return create_failure_result(
                error="请求超时",
                error_code="TIMEOUT",
            )
        if isinstance(exc, httpx.HTTPError):
            return create_failure_result(
                error=f"HTTP 请求失败: {str(exc)}",
                error_code="HTTP_ERROR",
            )
        return create_failure_result(
            error=f"{verb}: {str(exc)}",
            error_code=error_code,
        )

    async def _http_get(self, inputs: dict[str, Any]) -> ToolResult:
        """HTTP GET 请求"""
        try:
            url = inputs["url"]
            headers = self._merge_headers(inputs.get("headers"))
            params = inputs.get("params", {})
            timeout = inputs.get("timeout", self.timeout)

            async with httpx.AsyncClient(verify=self.verify_ssl, proxy=self._proxy_url) as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=httpx.Timeout(timeout),
                )

                too_large = self._response_too_large_msg(response)
                if too_large:
                    return create_failure_result(
                        error=too_large,
                        error_code="RESPONSE_TOO_LARGE",
                    )

                data = self._extract_data(response)

                if response.status_code >= 400:
                    payload = data if isinstance(data, str) else str(data)[:500]
                    return self._http_error_result(response, payload)

                return create_success_result(
                    data={
                        "status": response.status_code,
                        "data": data,
                    },
                    metadata={"action": "http_get", "url": str(url)},
                )

        except Exception as e:
            return self._request_failure(e, "GET 请求失败", "GET_FAILED")

    async def _http_post(self, inputs: dict[str, Any]) -> ToolResult:
        """HTTP POST 请求"""
        try:
            url = inputs["url"]
            headers = self._merge_headers(inputs.get("headers"))
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

                too_large = self._response_too_large_msg(response)
                if too_large:
                    return create_failure_result(
                        error=too_large,
                        error_code="RESPONSE_TOO_LARGE",
                    )

                data = self._extract_data(response)

                if response.status_code >= 400:
                    payload = data if isinstance(data, str) else str(data)[:500]
                    return self._http_error_result(response, payload)

                return create_success_result(
                    data={
                        "status": response.status_code,
                        "data": data,
                    },
                    metadata={"action": "http_post", "url": str(url)},
                )

        except Exception as e:
            return self._request_failure(e, "POST 请求失败", "POST_FAILED")

    async def _fetch_page(self, inputs: dict[str, Any]) -> ToolResult:
        """抓取网页内容"""
        try:
            url = inputs["url"]
            headers = self._merge_headers(inputs.get("headers"))
            timeout = inputs.get("timeout", self.timeout)
            extract_text = inputs.get("extract_text", True)

            async with httpx.AsyncClient(verify=self.verify_ssl, proxy=self._proxy_url) as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=httpx.Timeout(timeout),
                )

                too_large = self._response_too_large_msg(response)
                if too_large:
                    return create_failure_result(
                        error=too_large,
                        error_code="RESPONSE_TOO_LARGE",
                    )

                # 读取 HTML
                html_text = response.content.decode("utf-8", errors="ignore")

                result_data: dict[str, Any] = {
                    "status": response.status_code,
                }

                # 如果需要提取文本
                if extract_text:
                    import trafilatura  # noqa: PLC0415

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
                    return self._http_error_result(response)

                return create_success_result(
                    data=result_data,
                    metadata={"action": "fetch_page", "url": str(url)},
                )

        except Exception as e:
            return self._request_failure(e, "抓取网页失败", "FETCH_FAILED")
