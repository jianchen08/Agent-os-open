"""
用户旅程 2：前端页面浏览器验证（Chromium headless + DOM 断言）

使用 Python subprocess 调用 Chromium headless 模式获取页面 DOM。
代码参考: frontend/src/App.tsx, frontend/src/router.tsx, frontend/index.html

注意:
  - 前端使用 0.2 Kernel（端口 9100），不是 0.1 的 8988。
  - 前端 auth.ts 中的登录逻辑需要 0.1 后端，但 0.2 内核没有 /api/v1/auth 端点。
  - 前端登录页面可能无法正常工作，测试如实记录此情况。

测试项:
  2.1 访问 http://localhost:5290/ → 200，HTML 加载成功
  2.2 验证 HTML 中包含 root div 和 script 标签（Vite 标准）
  2.3 用 Chromium headless 截取页面内容，验证 DOM 中有可渲染内容
  2.4 验证前端到 Kernel 的 API 代理工作
"""
import subprocess
import pytest

from e2e_helpers import http_get


class TestFrontendHtmlLoad:
    """2.1 前端页面 HTTP 加载。"""

    def test_frontend_returns_200(self, frontend_url):
        """测试: GET / 应返回 200。"""
        status, body, _ = http_get(f"{frontend_url}/")
        assert status == 200, f"期望 200，实际 {status}"

    def test_frontend_returns_html_content(self, frontend_url):
        """测试: 前端返回 HTML 内容（非空）。"""
        status, body, _ = http_get(f"{frontend_url}/")
        if isinstance(body, str):
            html = body
        elif isinstance(body, dict):
            html = str(body)
        else:
            html = str(body)
        assert "<html" in html.lower() or "<!doctype html" in html.lower(), \
            "响应应为 HTML"


class TestFrontendHtmlStructure:
    """2.2 HTML 结构验证（Vite 标准）。"""

    def test_html_has_root_div(self, frontend_url):
        """测试: HTML 包含 <div id="root"> 标签。"""
        status, body, _ = http_get(f"{frontend_url}/")
        html = body if isinstance(body, str) else str(body)
        assert 'id="root"' in html or "id='root'" in html, \
            "HTML 缺少 <div id='root'>"

    def test_html_has_script_tag(self, frontend_url):
        """测试: HTML 包含 <script> 标签（Vite 模块入口）。"""
        status, body, _ = http_get(f"{frontend_url}/")
        html = body if isinstance(body, str) else str(body)
        assert "<script" in html.lower(), "HTML 缺少 <script> 标签"

    def test_html_has_vite_client_or_main_entry(self, frontend_url):
        """测试: HTML 包含 Vite 客户端或主入口脚本引用。"""
        status, body, _ = http_get(f"{frontend_url}/")
        html = body if isinstance(body, str) else str(body)
        has_vite = ("/@vite/client" in html or
                    "/src/main" in html or
                    "main.tsx" in html)
        assert has_vite, "HTML 缺少 Vite 客户端或主入口脚本引用"


class TestFrontendChromiumDom:
    """2.3 Chromium headless DOM 渲染验证。"""

    def _get_dom(self, url):
        """用 Chromium headless 获取渲染后的 DOM。"""
        try:
            result = subprocess.run(
                [
                    "chromium", "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--dump-dom",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            pytest.skip(f"Chromium 不可用: {e}")

    def test_chromium_renders_root_div(self, frontend_url):
        """测试: Chromium 渲染后 DOM 包含 root div。"""
        dom = self._get_dom(f"{frontend_url}/")
        assert 'id="root"' in dom or "id='root'" in dom, \
            "渲染后 DOM 缺少 root div"

    def test_chromium_renders_body_content(self, frontend_url):
        """测试: Chromium 渲染后 DOM body 非空（有可渲染内容）。

        注意: 前端 auth 依赖 0.1 后端的 /api/v1/auth 端点，
        0.2 内核无此端点，登录页可能无法正常渲染。
        本测试验证 DOM 中至少有 HTML/body 结构可渲染。
        """
        dom = self._get_dom(f"{frontend_url}/")
        assert "<body" in dom.lower(), "渲染后 DOM 缺少 body 标签"
        assert len(dom.strip()) > 100, "渲染后 DOM 内容过少，可能为空页面"


class TestFrontendApiProxy:
    """2.4 前端到 Kernel 的 API 代理验证。"""

    def test_proxy_agents_endpoint(self, frontend_url):
        """测试: 通过前端端口访问 /api/v1/agents，代理到 Kernel 返回 200。

        Vite proxy 配置将 /api/* 代理到 Kernel 9100 端口。
        """
        status, body, _ = http_get(f"{frontend_url}/api/v1/agents")
        assert status == 200, f"通过前端代理访问 /api/v1/agents 期望 200，实际 {status}"

    def test_proxy_schema_endpoint(self, frontend_url):
        """测试: 通过前端端口访问 /api/v1/schema，代理到 Kernel 返回 200。"""
        status, body, _ = http_get(f"{frontend_url}/api/v1/schema")
        assert status == 200, f"通过前端代理访问 /api/v1/schema 期望 200，实际 {status}"
        assert isinstance(body, dict), "schema 响应应为 dict"

    def test_proxy_health_via_api_path(self, frontend_url):
        """测试: 通过前端端口访问 /api/v1/health 的代理行为。

        Vite proxy 将 /api/* 代理到 Kernel 9100。
        Kernel 没有 /api/v1/health 路由（只有 /health），
        因此此端点返回 404 是预期行为，记录代理路径不匹配。
        """
        status, body, _ = http_get(f"{frontend_url}/api/v1/health")
        # Kernel 0.2 只有 /health，没有 /api/v1/health
        # 代理本身工作正常（请求到达 Kernel），但路由不存在
        assert status == 404, (
            f"/api/v1/health 代理后期望 404（Kernel 无此路由），实际 {status}。"
            "Kernel 0.2 健康检查路径为 /health 而非 /api/v1/health"
        )

    def test_proxy_health_root_path(self, frontend_url):
        """测试: 通过前端端口直接访问 /health 的代理行为。

        /health 不在 Vite /api 代理规则中，Vite dev server 直接处理。
        如果配置了直接服务，可能返回 200。
        """
        status, body, _ = http_get(f"{frontend_url}/health")
        # /health 不在 Vite proxy 配置中 (/api, /ws, /media, /uploads)
        # Vite dev server 直接处理，可能返回 Vite 默认页面或 404
        # 这是预期行为，记录 Vite 代理路径配置
        assert status in (200, 404), (
            f"通过前端访问 /health 返回 {status}。"
            "/health 不在 Vite /api 代理规则中，由 dev server 直接处理"
        )
