"""
Bing Web Search MCP Server (极简版)

纯 Python 标准库实现，零外部依赖。通过 HTML 抓取 bing.com 搜索结果，
无需 API Key、Docker、SearXNG 或任何外部服务。

协议：JSON-RPC 2.0 over stdio（与 MCP 规范兼容）
工具：web_search(query, max_results=10)

适用场景：网络环境无法直连 duckduckgo/google，但 bing.com 可达（如国内环境）。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from html import unescape
from typing import Any

BING_URL = "https://cn.bing.com/search"
TOOL_NAME = "web_search"
SERVER_INFO = {"name": "bing-search", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": TOOL_NAME,
        "description": (
            "搜索互联网信息。基于 Bing 搜索引擎，返回标题、链接和摘要。"
            "适用于需要查找实时信息、文档、教程等场景。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数（1-10，默认 10）",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    }
]


# ---------------------------------------------------------------------------
# Bing 搜索抓取
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _search_bing(query: str, count: int = 10) -> list[dict[str, str]]:
    """抓取 bing.com 搜索结果，返回去重后的 [{title, url, snippet}]。"""
    # 多取一些补偿去重损失
    fetch_count = min(count * 2, 20)
    params = urllib.parse.urlencode({"q": query, "count": fetch_count})
    req = urllib.request.Request(
        f"{BING_URL}?{params}",
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    seen_urls: set[str] = set()
    results: list[dict[str, str]] = []
    blocks = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.S)

    for block in blocks:
        if len(results) >= count:
            break
        # 标题 + URL
        title_m = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not title_m:
            continue
        url, title_raw = title_m.group(1), title_m.group(2)
        title = unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()
        if not title:
            continue
        # URL 去重
        norm_url = url.rstrip("/").lower()
        if norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)

        # 摘要：优先取 <p class="b_lineclamp...">，回退到任意 <p>
        snippet = ""
        for pattern in [
            r'<p class="b_lineclamp\d*"[^>]*>(.*?)</p>',
            r'<p[^>]*>(.*?)</p>',
        ]:
            p_m = re.search(pattern, block, re.S)
            if p_m:
                raw = re.sub(r"<[^>]+>", "", p_m.group(1))
                # 清理 &#0183 等实体和 &ensp 空白
                raw = re.sub(r"&[a-z]+;", " ", raw)
                raw = re.sub(r"&#\d+;", "", raw)
                raw = re.sub(r"\s{2,}", " ", raw)
                snippet = unescape(raw).strip()
                if snippet:
                    break

        results.append({"title": title, "url": url, "snippet": snippet})

    return results


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------

def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str, level: str = "debug") -> None:
    if level == "error":
        sys.stderr.write(f"[bing-search:error] {msg}\n")
        sys.stderr.flush()


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单个 JSON-RPC 请求。通知返回 None。"""
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params") or {}

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            }

        if method in ("initialized", "notifications/initialized"):
            return None

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}

            if tool_name != TOOL_NAME:
                return _error(req_id, -32601, f"Unknown tool: {tool_name}")

            query = str(arguments.get("query") or "").strip()
            if not query:
                return _error(req_id, -32602, "query 不能为空")

            count = min(max(int(arguments.get("max_results", 10)), 1), 10)
            results = _search_bing(query, count)

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"query": query, "results": results, "total": len(results)},
                                ensure_ascii=False,
                            ),
                        }
                    ]
                },
            }

        return _error(req_id, -32601, f"Method not found: {method}")

    except Exception as e:  # noqa: BLE001
        _log(f"{method} 失败: {e}", "error")
        return _error(req_id, -32603, f"搜索失败: {e}")


def _ensure_utf8_stdio() -> None:
    """强制 stdin/stdout/stderr 使用 UTF-8。

    Windows 中文系统默认 stdout 编码为 cp936(GBK)，父进程（MCPClient）按 UTF-8
    解码会因双字节汉字（如 0xCA continuation byte）抛 UnicodeDecodeError。
    在 stdio JSON-RPC 协议下，统一 UTF-8 是必须的。reconfigure() 仅 Python 3.7+。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> None:
    """stdio 主循环。"""
    _ensure_utf8_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
