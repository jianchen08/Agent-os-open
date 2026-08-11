#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：Chromium 加载前端页面的详细过程（捕获 console/网络失败/逐步加载）"""
import json, os, time
from playwright.sync_api import sync_playwright

CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
BASE = "http://127.0.0.1:5290"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
os.makedirs(OUT, exist_ok=True)

console_msgs = []
page_errors = []
failed_requests = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"])
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.set_default_timeout(30000)
    page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:200]}"))
    page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))
    page.on("requestfailed", lambda r: failed_requests.append(f"{r.url[:100]} => {r.failure}"))

    print("[1] goto (commit)...", flush=True)
    try:
        resp = page.goto(BASE, wait_until="commit", timeout=45000)
        print(f"    commit OK, status={resp.status if resp else 'None'}", flush=True)
    except Exception as e:
        print(f"    goto commit 异常: {type(e).__name__}: {str(e)[:200]}", flush=True)
        page.screenshot(path=f"{OUT}/diag_goto_fail.png")
        browser.close()
        print(json.dumps({"goto_failed": True, "console": console_msgs[-15:],
                          "errors": page_errors, "failed": failed_requests[-10:]}, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    page.wait_for_timeout(5000)
    print(f"[2] URL={page.url}", flush=True)
    print(f"[3] title={page.title()!r}", flush=True)

    # 检查 React 挂载元素
    for i in range(10):
        try:
            lf = page.locator('[data-testid="login-form"]').count()
            ci = page.locator('[data-testid="chat-input-textarea"]').count()
            if lf > 0 or ci > 0:
                print(f"[4] React 挂载成功 (第{i}s): login_form={lf} chat_input={ci}", flush=True)
                break
        except Exception:
            pass
        page.wait_for_timeout(2000)
    else:
        body = page.locator("body").text_content(timeout=5000) if page.locator("body").count() else ""
        print(f"[4] React 未挂载, body前300字: {body[:300]!r}", flush=True)

    page.screenshot(path=f"{OUT}/diag_page_state.png")
    print("[5] console 最近15条:", flush=True)
    for c in console_msgs[-15:]:
        print(f"    {c}", flush=True)
    print("[6] pageerror:", flush=True)
    for e in page_errors:
        print(f"    {e}", flush=True)
    print("[7] 失败请求:", flush=True)
    for r in failed_requests[-10:]:
        print(f"    {r}", flush=True)
    browser.close()

print("=== 诊断完成 ===", flush=True)
