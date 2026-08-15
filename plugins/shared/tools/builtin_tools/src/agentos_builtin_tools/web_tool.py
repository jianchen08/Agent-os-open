"""Web 操作工具——HTTP 请求和网页抓取。

核心业务逻辑从 0.1 src/tools/builtin/web/ 迁移。
"""

from __future__ import annotations

from typing import Any

from agentos_builtin_tools.result import ToolResult

WEB_OPERATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "post", "fetch"],
            "description": "操作类型：get=HTTP GET，post=HTTP POST，fetch=网页抓取",
        },
        "url": {"type": "string", "description": "目标 URL"},
        "headers": {"type": "object", "description": "请求头（可选）"},
        "data": {"type": "object", "description": "POST 请求体（可选）"},
        "params": {"type": "object", "description": "URL 查询参数（可选）"},
        "timeout": {"type": "integer", "description": "超时时间（秒），默认 30", "default": 30},
        "extract_text": {"type": "boolean", "description": "fetch 模式是否提取纯文本", "default": True},
    },
    "required": ["action", "url"],
}


async def web_operate(
    action: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
    extract_text: bool = True,
) -> ToolResult:
    """执行 HTTP 请求或网页抓取。"""
    try:
        import aiohttp
    except ImportError:
        return ToolResult.failure_result("aiohttp not installed")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            if action == "get":
                async with session.get(url, headers=headers, params=params) as resp:
                    body = await resp.text()
                    return ToolResult.success_result(
                        {"status": resp.status, "body": body, "url": str(resp.url)},
                        content_type=resp.content_type,
                    )

            if action == "post":
                async with session.post(
                    url, headers=headers, json=data, params=params
                ) as resp:
                    body = await resp.text()
                    return ToolResult.success_result(
                        {"status": resp.status, "body": body, "url": str(resp.url)},
                        content_type=resp.content_type,
                    )

            if action == "fetch":
                async with session.get(url, headers=headers, params=params) as resp:
                    html = await resp.text()
                    if extract_text:
                        text = _strip_html(html)
                    else:
                        text = html
                    return ToolResult.success_result(
                        {"status": resp.status, "text": text, "url": str(resp.url)},
                        content_type=resp.content_type,
                    )

            return ToolResult.failure_result(f"Unknown action: {action}")

    except TimeoutError:
        return ToolResult.failure_result(f"Request timed out after {timeout}s")
    except aiohttp.ClientError as e:
        return ToolResult.failure_result(f"HTTP client error: {e}")


def _strip_html(html: str) -> str:
    """简化的 HTML 标签去除。"""
    import re

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
