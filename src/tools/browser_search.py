# -*- coding: utf-8 -*-
"""
浏览器搜索工具 - 基于 Playwright 的开箱即用浏览器搜索与网页抓取

暴露接口：
- BrowserSearchTool：主工具类，继承 BuiltinTool
- get_tool_definition() -> Tool：工具定义
- execute(inputs: dict) -> ToolExecutionResult：工具执行

功能：
1. search: 用浏览器打开搜索引擎，输入关键词搜索，返回结构化搜索结果
2. fetch_page: 访问指定URL，等待JS渲染完成后返回完整页面内容
3. close: 关闭浏览器实例，释放资源
"""

import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import quote_plus

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# ── 真实浏览器 User-Agent 池 ──────────────────────────────────────────
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# ── Stealth 注入脚本（隐藏 Playwright/WebDriver 特征）────────────────
STEALTH_JS = """
() => {
    // 隐藏 webdriver 标志
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 伪造 plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // 伪造 languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en-US', 'en'],
    });

    // 移除 Playwright 特征
    delete window.__playwright;
    delete window.__pw_manual;
}
"""


class BrowserSearchTool(BuiltinTool):
    """
    基于 Playwright 的浏览器搜索工具

    开箱即用：Agent 加载后直接调用，无需手动配置 API Key 或启动服务。
    支持 Google / Bing 搜索引擎，自动处理反爬检测。
    """

    # 类级别浏览器实例（懒初始化，整个进程复用）
    _playwright: Any = None
    _browser: Any = None
    _context: Any = None
    # BUG-FIX: 添加 asyncio.Lock 防止并发调用 _ensure_browser 导致重复启动浏览器
    _lock: asyncio.Lock = asyncio.Lock()

    # ── 工具定义 ──────────────────────────────────────────────────────

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="browser_search",
            description=(
                "基于 Playwright 的浏览器搜索与网页抓取工具，开箱即用无需配置。"
                "支持两种核心操作："
                "1) search - 用真实浏览器打开搜索引擎（Google/Bing）搜索关键词，返回结构化结果（标题+URL+摘要）；"
                "2) fetch_page - 访问指定URL，等待JS渲染完成后返回完整页面文本内容（支持动态加载页面）。"
                "自动处理反爬检测（User-Agent伪装、隐藏WebDriver特征）。"
            ),
            when_to_use=[
                "需要搜索互联网信息且 web_search 结果不理想时",
                "需要抓取 JS 动态渲染的网页内容（如牛客、知乎等）",
                "需要绕过基础反爬检测获取网页内容",
                "中文搜索场景，需要更好的搜索结果质量",
            ],
            when_not_to_use=[
                "简单 HTTP 请求即可获取内容的场景（使用 fetch）",
                "已有 web_search 满足需求时（优先使用 web_search，速度更快）",
                "需要大量批量抓取（不适合高频爬虫场景）",
            ],
            caveats=[
                "首次调用时会启动浏览器实例，约需 2-5 秒",
                "每次搜索约需 5-15 秒（含页面加载和渲染等待）",
                "搜索引擎可能偶尔出现验证码，工具会自动重试",
                "调用完毕后建议调用 close 操作释放资源，或依赖自动清理",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "fetch_page", "close"],
                        "description": (
                            "操作类型："
                            "search=浏览器搜索关键词返回结果列表；"
                            "fetch_page=访问URL返回渲染后的页面内容；"
                            "close=关闭浏览器释放资源"
                        ),
                    },
                    # ── search 参数 ──
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（search 时必填）",
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["google", "bing"],
                        "description": "搜索引擎：google（默认）或 bing",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回的最大搜索结果数量，默认 10",
                    },
                    # ── fetch_page 参数 ──
                    "url": {
                        "type": "string",
                        "description": "目标网页URL（fetch_page 时必填）",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": "返回内容格式：text=纯文本（默认），html=原始HTML",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "额外等待秒数（用于JS渲染），默认 2.0",
                    },
                    # ── 通用参数 ──
                    "timeout": {
                        "type": "integer",
                        "description": "页面加载超时时间（毫秒），默认 30000",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.WEB,
            dangerous_operations=[],
        )

    # ── 浏览器生命周期管理 ─────────────────────────────────────────────

    async def _ensure_browser(self) -> tuple[Any, Any]:
        """确保浏览器实例已启动，返回 (context, page)"""
        # BUG-FIX: 使用 asyncio.Lock 保护并发调用，防止重复启动浏览器
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                await self._launch_browser()
            page = await self._context.new_page()
        return self._context, page

    async def _launch_browser(self) -> None:
        """启动 Playwright 浏览器（懒初始化）"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImportError(
                "Playwright 未安装。请运行: pip install playwright && playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._context = await self._browser.new_context(
            # BUG-FIX: 从 UA 池中随机选择，而非始终使用 USER_AGENTS[0]
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            java_script_enabled=True,
        )
        # 注入 stealth 脚本到所有新页面
        await self._context.add_init_script(STEALTH_JS)
        logger.info("[browser_search] 浏览器实例已启动")

    async def _cleanup(self) -> None:
        """关闭浏览器，释放资源"""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("[browser_search] 浏览器资源已释放")

    # ── 主入口 ────────────────────────────────────────────────────────

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        action = inputs.get("action", "")
        handlers = {
            "search": self._handle_search,
            "fetch_page": self._handle_fetch_page,
            "close": self._handle_close,
        }
        handler = handlers.get(action)
        if not handler:
            return create_failure_result(f"不支持的操作: {action}，可选: search, fetch_page, close")

        try:
            return await handler(inputs)
        except Exception as e:
            logger.error(f"[browser_search] 执行失败: {e}")
            # 浏览器可能处于异常状态，清理后重建
            await self._cleanup()
            return create_failure_result(f"浏览器操作失败: {e}")

    # ── search 实现 ───────────────────────────────────────────────────

    async def _handle_search(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        query = inputs.get("query", "").strip()
        if not query:
            return create_failure_result("search 操作需要 query 参数")

        engine = inputs.get("engine", "google")
        max_results = min(inputs.get("max_results", 10), 20)
        timeout = inputs.get("timeout", 30000)

        # 构建搜索 URL
        search_url = self._build_search_url(query, engine)

        _, page = await self._ensure_browser()
        try:
            # 导航到搜索页面
            await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout)

            # 等待搜索结果容器出现
            try:
                await page.wait_for_selector(
                    self._get_results_selector(engine),
                    timeout=10000,
                )
            except Exception:
                # 某些情况下搜索结果可能很快出现，超时不一定是失败
                await page.wait_for_timeout(2000)

            # 检测是否被重定向到验证页面
            if await self._detect_captcha(page):
                logger.warning("[browser_search] 检测到验证码，尝试等待后继续...")
                await page.wait_for_timeout(3000)

            # 提取搜索结果
            results = await self._extract_search_results(page, engine, max_results)

            return create_success_result(
                data={
                    "query": query,
                    "engine": engine,
                    "result_count": len(results),
                    "results": results,
                    "message": f"搜索完成，返回 {len(results)} 条结果",
                }
            )
        finally:
            await page.close()

    def _build_search_url(self, query: str, engine: str) -> str:
        """构建搜索引擎 URL"""
        # BUG-FIX: 使用 quote_plus 进行完整的 URL 编码，而非仅替换空格
        encoded = quote_plus(query)
        if engine == "bing":
            return f"https://www.bing.com/search?q={encoded}&setlang=zh-CN"
        # 默认 Google
        return f"https://www.google.com/search?q={encoded}&hl=zh-CN"

    def _get_results_selector(self, engine: str) -> str:
        """获取搜索结果容器的 CSS 选择器"""
        if engine == "bing":
            return "#b_content, .b_algo"
        return "#search, .g, [data-sokoban-container]"

    async def _detect_captcha(self, page: Any) -> bool:
        """检测是否遇到了验证码页面"""
        page_content = await page.content()
        captcha_signals = [
            "captcha", "recaptcha", "hcaptcha",
            "验证", "异常流量", "unusual traffic",
            "sorry", "异常活动",
        ]
        content_lower = page_content.lower()
        return any(s in content_lower for s in captcha_signals)

    async def _extract_search_results(
        self, page: Any, engine: str, max_results: int
    ) -> list[dict[str, str]]:
        """从搜索结果页面提取结构化数据"""
        if engine == "bing":
            return await self._extract_bing_results(page, max_results)
        return await self._extract_google_results(page, max_results)

    async def _extract_google_results(
        self, page: Any, max_results: int
    ) -> list[dict[str, str]]:
        """提取 Google 搜索结果"""
        js_extract = """
        (maxResults) => {
            const results = [];

            // 方式1: 标准搜索结果块
            const blocks = document.querySelectorAll('[data-sokoban-container], .g[data-hsn]');

            for (const block of blocks) {
                if (results.length >= maxResults) break;

                const linkEl = block.querySelector('a[href]');
                const titleEl = block.querySelector('h3, [role="heading"]');
                const descEl = block.querySelector(
                    '[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]'
                );

                const href = linkEl ? linkEl.href : '';
                if (!href || href.includes('google.com')) continue;

                results.push({
                    title: titleEl ? titleEl.textContent.trim() : '',
                    url: href,
                    snippet: descEl ? descEl.textContent.trim() : '',
                });
            }

            // 方式2: 备用提取（简单列表）
            if (results.length === 0) {
                const links = document.querySelectorAll('#rso a[href]');
                for (const link of links) {
                    if (results.length >= maxResults) break;
                    const href = link.href;
                    if (!href || href.includes('google.com')) continue;
                    const h3 = link.querySelector('h3');
                    if (!h3) continue;
                    results.push({
                        title: h3.textContent.trim(),
                        url: href,
                        snippet: '',
                    });
                }
            }

            return results;
        }
        """
        raw = await page.evaluate(js_extract, max_results)
        return raw if isinstance(raw, list) else []

    async def _extract_bing_results(
        self, page: Any, max_results: int
    ) -> list[dict[str, str]]:
        """提取 Bing 搜索结果"""
        js_extract = """
        (maxResults) => {
            const results = [];
            const items = document.querySelectorAll('.b_algo');

            for (const item of items) {
                if (results.length >= maxResults) break;

                const linkEl = item.querySelector('h2 a, a[href]');
                const descEl = item.querySelector('.b_caption p, .b_lineclamp2');

                const href = linkEl ? linkEl.href : '';
                if (!href) continue;

                results.push({
                    title: linkEl ? linkEl.textContent.trim() : '',
                    url: href,
                    snippet: descEl ? descEl.textContent.trim() : '',
                });
            }

            return results;
        }
        """
        raw = await page.evaluate(js_extract, max_results)
        return raw if isinstance(raw, list) else []

    # ── fetch_page 实现 ──────────────────────────────────────────────

    async def _handle_fetch_page(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        url = inputs.get("url", "").strip()
        if not url:
            return create_failure_result("fetch_page 操作需要 url 参数")

        output_format = inputs.get("output_format", "text")
        wait_seconds = float(inputs.get("wait_seconds", 2.0))
        timeout = inputs.get("timeout", 30000)

        _, page = await self._ensure_browser()
        try:
            # 导航到目标页面
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            # 等待网络空闲（确保JS动态内容加载）
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout, 10000))
            except Exception:
                pass

            # 额外等待（用于延迟加载的内容）
            if wait_seconds > 0:
                await page.wait_for_timeout(int(wait_seconds * 1000))

            # 获取页面内容
            title = await page.title()
            current_url = page.url

            if output_format == "html":
                content = await page.content()
                # 限制 HTML 大小
                if len(content) > 500000:
                    content = content[:500000] + "\n... [内容已截断]"
            else:
                content = await self._extract_text_content(page)

            return create_success_result(
                data={
                    "url": current_url,
                    "title": title,
                    "content": content,
                    "format": output_format,
                    "content_length": len(content),
                    "message": f"页面抓取成功，内容长度: {len(content)} 字符",
                }
            )
        finally:
            await page.close()

    async def _extract_text_content(self, page: Any) -> str:
        """从页面提取干净的文本内容"""
        js_extract = """
        () => {
            // 移除不需要的元素
            const removeSelectors = [
                'script', 'style', 'noscript', 'iframe',
                'nav', 'footer', 'header',
                '[role="navigation"]', '[role="banner"]',
                '[aria-hidden="true"]',
                '.advertisement', '.ad', '.ads',
                '.sidebar', '.widget',
                '.cookie-banner', '.popup',
            ];
            const clone = document.body.cloneNode(true);
            for (const sel of removeSelectors) {
                clone.querySelectorAll(sel).forEach(el => el.remove());
            }

            // 获取文本并清理多余空白
            let text = clone.innerText || clone.textContent || '';
            // 合并连续空白行为单行
            text = text.replace(/\\n{3,}/g, '\\n\\n');
            return text.trim();
        }
        """
        content = await page.evaluate(js_extract)
        if not isinstance(content, str):
            content = str(content)
        # 限制文本长度
        if len(content) > 200000:
            content = content[:200000] + "\n... [内容已截断]"
        return content

    # ── close 实现 ────────────────────────────────────────────────────

    async def _handle_close(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        await self._cleanup()
        return create_success_result(data={"message": "浏览器资源已释放"})
