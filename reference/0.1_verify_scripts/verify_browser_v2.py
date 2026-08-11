#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器验证 v2：等待 React 挂载 + 登录 + 发消息 + 轮询工具卡片
工作目录内版本（/tmp 会被环境重置清除）"""
import json, os, sys, time, re
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5290"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
USER, PWD, MSG = "admin", "admin12345", "用计算工具算一下 5+3"
os.makedirs(OUT, exist_ok=True)

result = {"passed": False, "tool_card_found": False, "tool_name": "", "status": "",
          "result_text": "", "page_snapshot": "", "errors": [], "events_log": []}
console_msgs = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"])
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.set_default_timeout(20000)
    page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:150]}"))
    page.on("pageerror", lambda e: result["errors"].append(f"pageerror: {str(e)[:200]}"))

    # 1. 打开页面，等待 React 挂载
    print("[1] 打开前端页面...", flush=True)
    page.goto(BASE, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(2000)
    mounted = False
    for i in range(60):  # 最多 120s
        try:
            if page.locator('[data-testid="login-form"]').count() > 0 or \
               page.locator('[data-testid="chat-input-textarea"]').count() > 0:
                mounted = True
                print(f"[2] React 挂载成功 (第 {i*2}s)", flush=True)
                break
        except Exception:
            pass
        page.wait_for_timeout(2000)
    if not mounted:
        result["errors"].append("等待 120s React 未挂载（无 login-form/chat-input）")
        page.screenshot(path=f"{OUT}/screenshot_no_mount.png")
        result["page_snapshot"] = (page.locator("body").text_content() or "")[:800]
        print("[FAIL] React 未挂载", flush=True)
    else:
        # 2. 登录
        login_form = page.locator('[data-testid="login-form"]')
        if login_form.count() > 0 and login_form.is_visible():
            print("[3] 执行登录...", flush=True)
            page.locator('[data-testid="login-username-input"]').fill(USER)
            page.locator('[data-testid="login-password-input"]').fill(PWD)
            page.locator('[data-testid="login-submit-button"]').click()
            try:
                page.locator('[data-testid="chat-input-textarea"]').wait_for(state="visible", timeout=45000)
                print("[4] 登录成功，聊天输入框可见", flush=True)
            except Exception as e:
                result["errors"].append(f"登录后未出现聊天框: {str(e)[:150]}")
                page.screenshot(path=f"{OUT}/screenshot_after_login.png")
        else:
            print("[3] 已登录状态（无登录表单）", flush=True)

        # 3. 发送消息
        chat_input = page.locator('[data-testid="chat-input-textarea"]')
        if chat_input.count() == 0:
            result["errors"].append("未找到聊天输入框")
        else:
            chat_input.click()
            chat_input.fill(MSG)
            page.locator('[data-testid="chat-send-button"]').click()
            print(f"[5] 已发送消息: {MSG}", flush=True)

            # 4. 轮询工具卡片（最长 90s）
            tool_card = None
            for i in range(45):
                cards = page.locator('[data-activity-type="tool_call"]')
                cnt = cards.count()
                if cnt > 0:
                    tool_card = cards.last
                    st = tool_card.get_attribute("data-activity-status") or ""
                    print(f"[poll {i*2}s] 工具卡片 x{cnt} status={st!r}", flush=True)
                    if st == "completed":
                        break
                page.wait_for_timeout(2000)
            if tool_card is not None:
                result["tool_card_found"] = True
                result["status"] = tool_card.get_attribute("data-activity-status") or ""
                try:
                    result["tool_name"] = (tool_card.locator(".font-medium").first.text_content() or "").strip()
                except Exception:
                    pass
                try:
                    card_text = (tool_card.text_content() or "").strip()
                except Exception:
                    card_text = ""
                result["result_text"] = card_text[:2000]
                try:
                    html = tool_card.evaluate("(el) => el.outerHTML")
                    with open(f"{OUT}/tool_card.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
                if result["status"] == "completed" and ("8" in card_text or "5+3" in card_text):
                    result["passed"] = True
                print(f"[result] tool_name={result['tool_name']!r} status={result['status']!r}", flush=True)
                print(f"[result] card_text 前300字: {card_text[:300]!r}", flush=True)
            else:
                result["errors"].append("等待 90s 未发现工具调用卡片")
                print("[FAIL] 未发现工具调用卡片", flush=True)

            # 5. 截图 + DOM 快照
            page.screenshot(path=f"{OUT}/screenshot.png")
            try:
                html = page.content()
                with open(f"{OUT}/page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                snippets = []
                for m in re.finditer(r'data-activity-type="tool_call"', html):
                    start = max(0, m.start() - 200)
                    end = min(len(html), m.end() + 1500)
                    snippets.append(html[start:end])
                result["page_snapshot"] = snippets[0] if snippets else "page.html 中未找到 tool_call 标记"
            except Exception as e:
                result["errors"].append(f"保存 DOM 快照失败: {str(e)[:150]}")

    # 6. console 消息摘要
    result["events_log"] = console_msgs[-20:]
    browser.close()

with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("=" * 60)
print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
print("=" * 60)
sys.exit(0 if result["passed"] else 1)
