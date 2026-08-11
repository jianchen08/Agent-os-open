import json, time
from playwright.sync_api import sync_playwright
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
errors, console_msgs = [], []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter",
              "--js-flags=--max-old-space-size=512", "--renderer-process-limit=1"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.on("pageerror", lambda e: errors.append(str(e)[:400]))
    pg.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:150]}"))
    pg.goto("http://127.0.0.1:5290", wait_until="commit", timeout=30000)
    for i in range(12):  # 60s
        time.sleep(5)
        try:
            lf = pg.locator('[data-testid="login-form"]').count()
            ci = pg.locator('[data-testid="chat-input-textarea"]').count()
            print(f"t={i*5}s login_form={lf} chat_input={ci} errors={len(errors)}", flush=True)
            if lf > 0 or ci > 0:
                print("REACT_MOUNTED", flush=True)
                break
        except Exception as e:
            print(f"t={i*5}s 异常: {str(e)[:100]}", flush=True)
            break
    print("PAGEERRORS:", json.dumps(errors, ensure_ascii=False), flush=True)
    print("CONSOLE_LAST:", json.dumps(console_msgs[-8:], ensure_ascii=False), flush=True)
    try:
        pg.screenshot(path="/workspace/docs/working/browser_tool_call_evidence/diag_fast.png")
        print("截图已保存", flush=True)
    except Exception as e:
        print(f"截图失败: {str(e)[:100]}", flush=True)
    b.close()
