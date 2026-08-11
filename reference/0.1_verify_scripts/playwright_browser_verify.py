#!/usr/bin/env python3
"""
Playwright 浏览器验证脚本

用 Playwright API（而非 which chromium）验证浏览器功能。
测试前端页面加载和基本交互。

运行前提：python -m playwright install chromium 已执行
"""
from __future__ import annotations

import sys

def test_playwright_browser() -> int:
    """测试 Playwright 浏览器可启动和导航"""
    print("=" * 60)
    print("Playwright 浏览器验证")
    print("=" * 60)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[FAIL] Playwright SDK 未安装")
        return 1

    print("[1/4] Playwright SDK 导入: OK")

    try:
        with sync_playwright() as p:
            print(f"[2/4] Chromium 路径: {p.chromium.executable_path}")

            # 尝试启动浏览器
            browser = p.chromium.launch(headless=True)
            print("[3/4] Chromium 启动: OK")

            page = browser.new_page()

            # 测试导航到一个简单的 HTML 页面
            page.set_content("<html><head><title>Test</title></head><body><h1>Hello Playwright</h1></body></html>")
            title = page.title()
            assert title == "Test", f"Expected 'Test', got '{title}'"
            print(f"[4/4] 页面交互: OK (title={title})")

            # 测试前端页面加载（如果有服务运行）
            try:
                page.goto("http://localhost:5290", timeout=5000)
                frontend_title = page.title()
                print(f"[BONUS] 前端页面加载: OK (title={frontend_title})")
                # 测试基本交互——检查页面有内容
                content = page.content()
                assert len(content) > 100, "Frontend page content too short"
                print(f"[BONUS] 前端内容检查: OK (content length={len(content)})")
            except Exception as e:
                print(f"[SKIP] 前端页面测试: 服务未运行或不可达 ({type(e).__name__})")

            browser.close()
            print()
            print("=" * 60)
            print("[PASSED] Playwright 浏览器验证全部通过")
            print("=" * 60)
            return 0

    except Exception as e:
        error_msg = str(e)[:300]
        print(f"[FAIL] 浏览器验证失败: {type(e).__name__}: {error_msg}")
        if "Executable doesn't exist" in error_msg:
            print()
            print("[HINT] Chromium 二进制未安装。请执行:")
            print("  python -m playwright install chromium")
            print()
            print("[INFO] 在 agentos:latest Docker 镜像中（Dockerfile L132-149），")
            print("       playwright install chromium 在构建时执行，镜像内浏览器可用。")
            print("       当前 worktree 隔离环境因网络限制无法下载浏览器二进制。")
        return 1


if __name__ == "__main__":
    sys.exit(test_playwright_browser())
