#!/usr/bin/env python3
"""
真实浏览器验证前端工具调用显示（Playwright sync_api + 指定 Chromium）
- 打开前端 http://127.0.0.1:5290 → 登录 admin/admin12345 → 发送"用计算工具算一下 5+3"
- 轮询 DOM：查找工具调用卡片（data-activity-type="tool_call"）、状态、结果（含 8）
- 产出证据到 /workspace/docs/working/browser_tool_call_evidence/
"""
import glob
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5290"
OUT_DIR = "/workspace/docs/working/browser_tool_call_evidence"
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
USERNAME = os.environ.get("AGENTOS_ADMIN_USER", "admin")
PASSWORD = os.environ.get("AGENTOS_ADMIN_PWD", "admin12345")
MESSAGE = "用计算工具算一下 5+3"
POLL_SECONDS = 60  # 工具执行链路约 10-30s，轮询放宽到 60s


def find_chrome() -> str | None:
    """定位 chromium 可执行文件"""
    if os.path.exists(CHROME):
        return CHROME
    matches = glob.glob("/opt/ms-playwright/chromium-*/chrome-linux64/chrome")
    if matches:
        return matches[0]
    matches = glob.glob("/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")
    if matches:
        return matches[0]
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {
        "passed": False,
        "tool_card_found": False,
        "tool_name": "",
        "status": "",
        "result_text": "",
        "page_snapshot": "",
        "errors": [],
    }
    errors = result["errors"]

    chrome_path = find_chrome()
    if not chrome_path:
        errors.append(f"未找到 chromium 可执行文件（尝试路径: {CHROME}）")
        with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(f"[info] 使用 Chromium: {chrome_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chrome_path,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(30000)

        try:
            # ── 1. 打开前端，等待登录表单（Vite dev 模块多，用 commit 避免等全量加载） ──
            print("[step] 打开前端页面...")
            page.goto(BASE_URL, wait_until="commit", timeout=60000)
            page.wait_for_timeout(3000)  # 给 React 挂载留时间
            # 等待登录表单或聊天界面（可能已登录）
            login_form = page.locator('[data-testid="login-form"]')
            chat_input = page.locator('[data-testid="chat-input-textarea"]')
            try:
                login_form.wait_for(state="visible", timeout=90000)
                print("[step] 检测到登录表单")
            except Exception:
                try:
                    chat_input.wait_for(state="visible", timeout=90000)
                    print("[step] 检测到聊天输入框（已登录状态）")
                except Exception:
                    print("[step] 未检测到登录表单/聊天框，检查当前页面状态...")
                    print("[info] 当前 URL:", page.url)
                    page.screenshot(path=os.path.join(OUT_DIR, "screenshot_early.png"))
                    errors.append(f"未检测到登录表单或聊天输入框，URL={page.url}")

            # ── 2. 登录（若存在登录表单） ──
            if login_form.count() > 0 and login_form.is_visible():
                print(f"[step] 登录 {USERNAME}...")
                username_input = page.locator('[data-testid="login-username-input"]')
                password_input = page.locator('[data-testid="login-password-input"]')
                submit_btn = page.locator('[data-testid="login-submit-button"]')
                username_input.wait_for(state="visible", timeout=15000)
                password_input.wait_for(state="visible", timeout=15000)
                submit_btn.wait_for(state="visible", timeout=15000)
                username_input.fill(USERNAME)
                password_input.fill(PASSWORD)
                submit_btn.click()
                # 等待聊天界面出现（输入框）
                chat_input = page.locator('[data-testid="chat-input-textarea"]')
                chat_input.wait_for(state="visible", timeout=60000)
                print("[ok] 登录成功，聊天输入框可见")
            else:
                # 可能已登录，等待聊天输入框
                chat_input = page.locator('[data-testid="chat-input-textarea"]')
                chat_input.wait_for(state="visible", timeout=60000)
                print("[ok] 已登录状态，聊天输入框可见")

            # 登录成功后再截图（登录后状态）
            page.screenshot(path=os.path.join(OUT_DIR, "screenshot_logged_in.png"))

            # ── 3. 发送消息 ──
            print(f"[step] 发送消息: {MESSAGE}")
            chat_input.click()
            chat_input.fill(MESSAGE)
            # 校验填入成功
            filled = chat_input.input_value()
            print(f"[info] 输入框内容: {filled!r}")
            send_btn = page.locator('[data-testid="chat-send-button"]')
            send_btn.wait_for(state="visible", timeout=15000)
            send_btn.click()
            print("[step] 已点击发送，等待工具执行与前端渲染...")

            # ── 4. 轮询 DOM 查找工具调用卡片 ──
            tool_card = None
            deadline = time.time() + POLL_SECONDS
            while time.time() < deadline:
                cards = page.locator('[data-activity-type="tool_call"]')
                count = cards.count()
                if count > 0:
                    # 取最后一张卡片（当前会话的工具调用）
                    tool_card = cards.last
                    status = tool_card.get_attribute("data-activity-status") or ""
                    print(f"[poll] 发现工具卡片 x{count}, status={status!r}")
                    if status == "completed":
                        break
                else:
                    print(f"[poll] 尚未发现工具卡片 ({int(deadline - time.time())}s 剩余)")
                page.wait_for_timeout(2000)

            # ── 5. 收集工具卡片证据 ──
            if tool_card is not None:
                result["tool_card_found"] = True
                result["status"] = tool_card.get_attribute("data-activity-status") or ""
                # 工具名称（标题在 .font-medium）
                title_el = tool_card.locator(".font-medium").first
                try:
                    result["tool_name"] = (title_el.text_content() or "").strip()
                except Exception as e:
                    errors.append(f"读取工具名称失败: {e}")
                # 尝试展开卡片查看结果
                try:
                    tool_card.click()
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                # 读取卡片完整文本（含 result）
                try:
                    card_text = (tool_card.text_content() or "").strip()
                except Exception as e:
                    card_text = ""
                    errors.append(f"读取卡片文本失败: {e}")
                result["result_text"] = card_text[:2000]
                # 保存卡片 HTML 快照
                try:
                    card_html = tool_card.evaluate("(el) => el.outerHTML")
                    with open(os.path.join(OUT_DIR, "tool_card.html"), "w", encoding="utf-8") as f:
                        f.write(card_html)
                except Exception as e:
                    errors.append(f"保存卡片 HTML 失败: {e}")
                print(f"[result] tool_name={result['tool_name']!r}")
                print(f"[result] status={result['status']!r}")
                print(f"[result] card_text 前300字: {card_text[:300]!r}")

                # 判断 passed：卡片存在 + 状态 completed + 结果含 8
                if result["status"] == "completed" and ("8" in card_text or "5+3" in card_text):
                    result["passed"] = True
            else:
                errors.append(f"等待 {POLL_SECONDS}s 未发现工具调用卡片（data-activity-type=tool_call）")
                print("[result] 未发现工具调用卡片")

            # ── 6. 页面快照与截图 ──
            page.screenshot(path=os.path.join(OUT_DIR, "screenshot.png"))
            # 保存整页 DOM 快照
            try:
                html = page.content()
                with open(os.path.join(OUT_DIR, "page.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                # 摘要：提取含 tool_call / scientific_calculator 的片段
                import re
                snippets = []
                for m in re.finditer(r'data-activity-type="tool_call"', html):
                    start = max(0, m.start() - 200)
                    end = min(len(html), m.end() + 1500)
                    snippets.append(html[start:end])
                result["page_snapshot"] = snippets[0] if snippets else "page.html 中未找到 tool_call 标记"
            except Exception as e:
                errors.append(f"保存 DOM 快照失败: {e}")

        except Exception as e:
            import traceback
            errors.append(f"执行异常: {e}\n{traceback.format_exc()}")
            try:
                page.screenshot(path=os.path.join(OUT_DIR, "screenshot_error.png"))
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # ── 7. 写 result.json ──
    with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 60)
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
